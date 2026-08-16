"""cohabit_queue.py（同居触发与闸 C2）单测。

基座沿用 test_cohabit.CohabitBase（world/角色状态/outbox 全在临时目录，模型是罐头替身），
再加四样：开关强制打开、事件钩子 install/uninstall、队列内存态清零、
code 会话探测和随机数打桩（不碰真 tmux、判定可复现）。worker 线程不起，
_drain / _solo_tick 直接同步调——测的就是它们的逻辑，线程壳没有逻辑。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_cohabit_queue -v
"""
import sys
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cohabit
import cohabit_queue as cq
import config
import pipeline
import state_store
import wake
import world
from tests.test_cohabit import CohabitBase, out


class QueueBase(CohabitBase):
    def setUp(self):
        super().setUp()
        self._enabled_orig = config.COHABIT_ENABLED
        config.COHABIT_ENABLED = True
        self._code_open_orig = wake.code_session_open
        wake.code_session_open = lambda: False
        self._random_orig = cq.random
        cq.random = types.SimpleNamespace(random=lambda: 0.0)   # 概率必中，判定可复现
        cq.install()
        self._reset_queue()
        import characters
        self.chars = characters.ids()

    def tearDown(self):
        cq.uninstall()
        self._reset_queue()
        config.COHABIT_ENABLED = self._enabled_orig
        wake.code_session_open = self._code_open_orig
        cq.random = self._random_orig
        super().tearDown()

    @staticmethod
    def _reset_queue():
        with cq._lock:
            cq._pending.clear()
            cq._order.clear()
            cq._syswake_run.clear()
            cq._gate_hit.clear()
            cq._defer_hit.clear()
        cq._signal.clear()
        # 探测缓存必须一并清：5 秒 TTL 会把上一个测试的 owner 带进下一个测试
        cq._code_cache.update(ts=0.0, owner=None)

    def set_char_settings(self, cid, **kw):
        state_store._write_json(state_store._char_path("settings.json", cid), kw)


class TestGates(QueueBase):
    def test_disabled_is_inert(self):
        config.COHABIT_ENABLED = False
        self.assertFalse(cq.enqueue(self.cid, {"kind": "event", "text": "x"}))
        self.assertIsNone(cq.chat_move(self.cid, "living_room"))
        self.assertEqual(cq.chat_move_hint(self.cid), "")
        world.act("user", "mm_room", speech="没人醒")   # 钩子挂着但开关关着
        self.assertEqual(cq._pending, {})

    def test_single_pending_merges_and_dedupes(self):
        cq.enqueue(self.cid, {"kind": "event", "text": "甲"})
        cq.enqueue(self.cid, {"kind": "event", "text": "乙"})
        cq.enqueue(self.cid, {"kind": "event", "text": "甲"})   # 同文去重
        self.assertEqual(len(cq._pending[self.cid]), 2)
        self.assertEqual(cq._order, [self.cid])                 # 还是一个 pending

    def test_chain_cap_blocks_system_not_solo(self):
        cq._syswake_run[self.cid] = config.COHABIT_CHAIN_N
        self.assertFalse(cq.enqueue(self.cid, {"kind": "event", "text": "x"}))
        self.assertTrue(cq.enqueue(self.cid, {"kind": "solo", "text": "y"}, system=False))
        self._reset_queue()
        cq._syswake_run[self.cid] = config.COHABIT_CHAIN_N
        cq.external_input()                                     # 外部输入 → 计数清零
        self.assertTrue(cq.enqueue(self.cid, {"kind": "event", "text": "x"}))


class TestEventWake(QueueBase):
    def test_author_exclusion_and_user_never_woken(self):
        for cid in self.chars:
            world.move(cid, "living_room")
        world.move("user", "living_room")
        self._reset_queue()                     # 清掉 enter 事件引发的入队，只看 act
        world.act("user", "living_room", speech="我回来啦")
        self.assertEqual(set(cq._pending), set(self.chars))     # 在场 AI 全醒，用户不算
        world.act(self.chars[0], "living_room", speech="欢迎回家")
        self.assertNotIn("user", cq._pending)
        # 作者排除：自己的发言没有给自己再加原因
        self.assertEqual(len([r for r in cq._pending[self.chars[0]]
                              if "欢迎回家" in r["text"]]), 0)

    def test_reason_text_carries_room_and_speaker(self):
        world.move("user", self.home)
        self._reset_queue()
        world.act("user", self.home, action="敲了敲门框", speech="在忙吗")
        texts = "".join(r["text"] for r in cq._pending[self.cid])
        self.assertIn("敲了敲门框", texts)
        self.assertIn("在忙吗", texts)
        self.assertIn("的房间", texts)          # 房间名进原因

    def test_move_events_wake_both_rooms(self):
        # c1 和 cass 各在自己房间；c1 去 cass 的房间 → enter 事件唤 cass；
        # 留在原房的没人 → leave 无人可唤。
        if len(self.chars) < 2:
            self.skipTest("单角色环境")
        c1, c2 = self.chars[0], self.chars[1]
        self._reset_queue()
        world.move(c1, f"{c2}_room")
        self.assertIn(c2, cq._pending)
        self.assertNotIn(c1, cq._pending)       # 移动者不被自己的进出事件唤醒


class TestDrain(QueueBase):
    def test_drain_executes_counts_and_logs(self):
        self.replies = [out("none")]
        cq.enqueue(self.cid, {"kind": "event", "text": "有动静"})
        cq._drain()
        self.assertEqual(cq._syswake_run[self.cid], 1)
        self.assertEqual(cq._pending, {})
        self.assertIn("有动静", self.prompts[0])
        self.assertEqual(self.log_entries()[-1]["action"], "none")
        self.assertTrue(self.log_entries()[-1]["trigger"].startswith("cohabit:event"))

    def test_move_chain_goes_through_queue(self):
        self.replies = [out("none", move="living_room"), out("act", say="有人吗")]
        cq.enqueue(self.cid, {"kind": "event", "text": "x"})
        cq._drain()
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("你刚到「客厅」", self.prompts[1])
        self.assertEqual(cq._syswake_run[self.cid], 2)          # 补醒也计连发
        kinds = [e.get("kind", e["type"]) for e in world.read_events("living_room")]
        self.assertEqual(kinds, ["enter", "speech"])

    def test_chain_cap_stops_move_before_teleport(self):
        targets = ["living_room", self.home] * 3
        self.replies = [out("none", move=t) for t in targets]
        cq.enqueue(self.cid, {"kind": "event", "text": "x"})
        cq._drain()
        self.assertEqual(len(self.prompts), config.COHABIT_CHAIN_N)   # 只醒了 N 轮
        self.assertEqual(cq._syswake_run[self.cid], config.COHABIT_CHAIN_N)
        # 第 N 轮的 move 在瞬移前被拦：位置停在第 N-1 轮的目的地
        self.assertEqual(world.location_of(self.cid), targets[config.COHABIT_CHAIN_N - 2])
        self.assertFalse(cq.enqueue(self.cid, {"kind": "event", "text": "again"}))

    def test_chat_turn_defers_execution(self):
        self.replies = [out("none")]
        cq.enqueue(self.cid, {"kind": "event", "text": "x"})
        wake.chat_turn_begin(self.cid)
        try:
            cq._drain()
            self.assertIn(self.cid, cq._pending)                # 避让：pending 保留
            self.assertEqual(self.prompts, [])
        finally:
            wake.chat_turn_end(self.cid)
        cq._drain()
        self.assertEqual(len(self.prompts), 1)

    def test_error_sets_cooldown_and_blocks_enqueue(self):
        cohabit._run = lambda prompt, cid: (None, [])
        cq.enqueue(self.cid, {"kind": "event", "text": "x"})
        cq._drain()
        sched = state_store.read_schedule(self.cid)
        self.assertGreater(sched["cooldown_until"], time.time())
        self.assertFalse(cq.enqueue(self.cid, {"kind": "event", "text": "y"}))


class TestSolo(QueueBase):
    def test_alone_probability_wake(self):
        cq._solo_tick(time.time())
        self.assertIn(self.cid, cq._pending)    # 各自在自己房间 = 独处，概率必中
        self.assertEqual(cq._pending[self.cid][0]["kind"], "solo")

    def test_not_alone_no_probability_wake(self):
        world.move("user", self.home)
        self._reset_queue()
        cq._solo_tick(time.time())
        self.assertNotIn(self.cid, cq._pending)

    def test_scheduled_next_fires_even_with_company(self):
        world.move("user", self.home)
        self._reset_queue()
        state_store.write_schedule({"next_wake_at": time.time() - 5}, self.cid)
        cq._solo_tick(time.time())
        self.assertEqual(cq._pending[self.cid][0]["kind"], "scheduled")

    def test_budget_blocks_solo(self):
        self.set_char_settings(self.cid, wake_daily_budget=1)
        state_store.append_wake_log({"ts": int(time.time()), "source": "wake",
                                     "trigger": "cohabit:solo", "action": "none"},
                                    char_id=self.cid)
        cq._solo_tick(time.time())
        self.assertNotIn(self.cid, cq._pending)

    def test_code_session_defers_owner_only(self):
        # 归属角色在电脑前：他的自主醒跳过，别的角色照常（M2 后不再全体避让）
        cohabit.coding_char = lambda: (self.cid, "code")
        cq._code_cache["ts"] = 0.0              # 探测缓存作废，立刻认新桩
        cq._solo_tick(time.time())
        self.assertNotIn(self.cid, cq._pending)
        if len(self.chars) > 1:
            self.assertIn(self.chars[1], cq._pending)


class TestCodeSessionDefer(QueueBase):
    """选项 3（2026-08-16 拍板）：归属角色在 code/game 会话里 → 醒来延后、原因照攒，
    收工那一刻整批补醒；在场者的注入里能看到他「正在敲代码，先别打扰」。"""

    def _busy(self, cid, prof="code"):
        cohabit.coding_char = lambda: (cid, prof)
        cq._code_cache["ts"] = 0.0              # 缓存作废：测试里换桩要立刻生效

    def _idle(self):
        cohabit.coding_char = lambda: None
        cq._code_cache["ts"] = 0.0

    def test_event_wake_deferred_until_close(self):
        self._busy(self.cid)
        self.replies = [out("none")]
        cq.enqueue(self.cid, {"kind": "event", "text": "有人说话"})
        cq.enqueue(self.cid, {"kind": "event", "text": "又有人说话"})
        cq._drain()
        self.assertEqual(self.prompts, [])                       # 干活期间不醒
        self.assertEqual(len(cq._pending[self.cid]), 2)          # 原因照攒
        self._idle()
        cq.code_session_closed()                                 # 收工踢一脚
        cq._drain()
        self.assertEqual(len(self.prompts), 1)                   # 一次醒补上全部
        self.assertIn("有人说话", self.prompts[0])
        self.assertIn("又有人说话", self.prompts[0])

    def test_other_char_not_deferred(self):
        if len(self.chars) < 2:
            self.skipTest("单角色环境")
        other = self.chars[1]
        self._busy(self.cid)
        self.replies = [out("none")]
        cq.enqueue(other, {"kind": "event", "text": "别人的动静"})
        cq._drain()
        self.assertEqual(len(self.prompts), 1)                   # 不在电脑前的照常醒

    def test_occupants_annotation_and_honest_note(self):
        if len(self.chars) < 2:
            self.skipTest("单角色环境")
        other = self.chars[1]
        world.move(other, self.home)                             # 俩人同屋
        self._busy(other)
        p = cohabit.cohabit_prompt(self.cid, [{"kind": "event", "text": "x"}],
                                   state_store.load_settings(self.cid))
        self.assertIn("正在电脑前敲代码", p)                       # 在场者看得到状态
        self.assertIn("先别打扰", p)
        self.assertNotIn("按避让规则这次醒来本该等你收工", p)       # 自己没在干活，无兜底段
        self._busy(self.cid)                                     # 换成自己在干活（兜底路径）
        p = cohabit.cohabit_prompt(self.cid, [{"kind": "event", "text": "x"}],
                                   state_store.load_settings(self.cid))
        self.assertIn("本该等你收工", p)

    def test_world_route_reports_status(self):
        from fastapi.testclient import TestClient
        import app as app_module
        self._busy(self.cid)
        h = {"X-Auth": config.AUTH_KEY}
        client = TestClient(app_module.app)
        r = client.get("/world", headers=h).json()
        self.assertEqual(r["entities"][self.cid]["status"], "code")
        self.assertIsNone(r["entities"]["user"]["status"])
        # 手机侧同一份事实：会话列表也带 status（UI 显示「正在敲代码，可能无法及时回复」）
        items = {x["id"]: x for x in client.get("/characters", headers=h).json()["items"]}
        self.assertEqual(items[self.cid]["status"], "code")


class TestChatMove(QueueBase):
    def test_parse_chat_move(self):
        cleaned, target = pipeline.parse_chat_move("好嘞，这就过去[[move:living_room]]")
        self.assertEqual((cleaned, target), ("好嘞，这就过去", "living_room"))
        cleaned, target = pipeline.parse_chat_move("不挪了")
        self.assertEqual((cleaned, target), ("不挪了", None))

    def test_chat_move_executes_and_enqueues_result(self):
        mv = cq.chat_move(self.cid, "living_room")
        self.assertTrue(mv["ok"])
        self.assertEqual(world.location_of(self.cid), "living_room")
        self.assertEqual(cq._pending[self.cid][0]["kind"], "move_result")

    def test_chat_move_unknown_target_is_noop(self):
        self.assertIsNone(cq.chat_move(self.cid, "basement_lab"))
        self.assertEqual(world.location_of(self.cid), self.home)

    def test_house_context_snapshot_and_timeline_events(self):
        world.move("user", self.home)
        world.act_mixed("user", self.home, "*敲了敲门框* 在忙吗")
        world.state_change("user", self.home, "add", text="门口放了杯咖啡")
        # 现场快照：在场者 + 地点状态 + 通道框定；事件原文不在这儿（B 案：进时间线）
        ctx = cohabit.house_context_for_chat(self.cid)
        self.assertIn("小屋现场", ctx)
        self.assertIn("门口放了杯咖啡", ctx)
        self.assertIn("手机短信", ctx)
        self.assertNotIn("敲了敲门框", ctx)
        # 时间线三路合并：经历流按 ts 带（房间名）前缀入列，跨离场也保留
        world.move(self.cid, "living_room")   # 离开自己房间——经历不随离场消失
        tl = pipeline.build_context_timeline([], char_id=self.cid, experience_limit=40)
        self.assertIn("敲了敲门框", tl)
        self.assertIn("在忙吗", tl)
        self.assertIn("的房间）", tl)          # （房间名）前缀
        # 关开关：现场快照没了；时间线的 experience_limit=0 也不带房间事件
        config.COHABIT_ENABLED = False
        self.assertEqual(cohabit.house_context_for_chat(self.cid), "")
        tl0 = pipeline.build_context_timeline([], char_id=self.cid)
        self.assertNotIn("敲了敲门框", tl0)
        config.COHABIT_ENABLED = True

    def test_chat_move_hint_lists_rooms_not_people(self):
        world.move("user", "living_room")
        hint = cq.chat_move_hint(self.cid)
        self.assertIn("[[move:", hint)
        self.assertIn("living_room", hint)
        self.assertNotIn(f"{self.cid}_room", hint.split("可选：")[1])  # 不含自己所在房
        # 认知边界：清单里绝不出现谁在哪
        self.assertNotIn("客厅（", hint)


if __name__ == "__main__":
    unittest.main()
