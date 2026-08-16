"""cohabit.py（同居醒来统一模型 C1）单测。

三类桩，一个真模型都不起、一个真状态都不碰（§5 测试纪律）：
- world 三路径 + state_store 角色目录/outbox → 临时目录；
- cohabit._run → 罐头输出（按调用序回放，顺带录下每轮 prompt 供断言）；
- wake.bark_push / pipeline.tool_menu_block → 打桩（不推真通知、不探真 Ombre）。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_cohabit -v
"""
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import characters
import cohabit
import pipeline
import state_store
import wake
import world


def out(action="none", thoughts="……", motion="", say="", state="", phone="",
        move="无", carry="无", nxt="无"):
    """拼一份完整协议输出（九个标签都在，贴近真实回复形态）。"""
    return (f"THOUGHTS: {thoughts}\nACTION: {action}\nMOTION: {motion}\n"
            f"SAY: {say}\nSTATE: {state}\nPHONE: {phone}\nMOVE: {move}\n"
            f"CARRY: {carry}\nNEXT: {nxt}")


class CohabitBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cohabit_test_"))
        # world → 临时目录
        self._world_orig = (world.ROOMS_DIR, world.REGISTRY_PATH, world.WORLD_PATH)
        world.ROOMS_DIR = self.tmp / "rooms"
        world.REGISTRY_PATH = world.ROOMS_DIR / "registry.json"
        world.WORLD_PATH = self.tmp / "world.json"
        world.ensure_world()
        # 角色状态（wake_log/schedule/窗口）+ outbox → 临时目录
        self._ss_orig = (state_store.CHAR_STATE_ROOT, state_store.OUTBOX_PATH)
        state_store.CHAR_STATE_ROOT = self.tmp / "chars"
        state_store.OUTBOX_PATH = self.tmp / "outbox.json"
        # 桩：Bark / 工具菜单 / code 会话探测（都碰真环境：推真通知、探真 Ombre、探真 tmux）
        self._bark_orig, self._menu_orig = wake.bark_push, pipeline.tool_menu_block
        self._coding_orig = cohabit.coding_char
        self.barks: list[str] = []
        wake.bark_push = lambda text, **kw: (self.barks.append(text), True)[1]
        pipeline.tool_menu_block = lambda *a, **kw: ""
        cohabit.coding_char = lambda: None
        # 模型替身：按序回放 self.replies，录下每轮 prompt
        self._run_orig = cohabit._run
        self.prompts: list[str] = []
        self.replies: list[str] = []

        def fake_run(prompt, cid):
            self.prompts.append(prompt)
            if not self.replies:
                return out(), []
            return self.replies.pop(0), []
        cohabit._run = fake_run

        self.cid = characters.ids()[0]
        self.home = f"{self.cid}_room"

    def tearDown(self):
        world.ROOMS_DIR, world.REGISTRY_PATH, world.WORLD_PATH = self._world_orig
        state_store.CHAR_STATE_ROOT, state_store.OUTBOX_PATH = self._ss_orig
        wake.bark_push, pipeline.tool_menu_block = self._bark_orig, self._menu_orig
        cohabit.coding_char = self._coding_orig
        cohabit._run = self._run_orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wake_once(self, *replies, reasons=None):
        self.replies = list(replies)
        return cohabit.do_cohabit_wake(
            self.cid, reasons or [{"kind": "solo", "text": "你独处了一会儿"}])

    def log_entries(self):
        return state_store.read_wake_log(char_id=self.cid)


class TestParse(unittest.TestCase):
    def test_act_drops_phone(self):
        p = cohabit.parse_cohabit_output(
            out("act", motion="伸个懒腰", say="早", phone="这句不该存在"))
        self.assertEqual(p["action"], "act")
        self.assertEqual((p["motion"], p["say"]), ("伸个懒腰", "早"))
        self.assertEqual(p["phone"], "")   # 互斥：act 轮的 PHONE 结构性丢弃

    def test_phone_drops_act_fields(self):
        p = cohabit.parse_cohabit_output(
            out("phone", motion="不该在", say="不该在", phone="在干嘛呀"))
        self.assertEqual(p["action"], "phone")
        self.assertEqual(p["phone"], "在干嘛呀")
        self.assertEqual((p["motion"], p["say"], p["state_ops"]), ("", "", []))

    def test_empty_expression_falls_back_to_none(self):
        self.assertEqual(cohabit.parse_cohabit_output(out("act"))["action"], "none")
        self.assertEqual(cohabit.parse_cohabit_output(out("phone"))["action"], "none")

    def test_state_ops_parse_and_cap(self):
        state = "add: 桌上多了杯茶\nedit deadbeef: 茶凉了\nremove cafebabe\nadd: 第四条要被截掉"
        p = cohabit.parse_cohabit_output(out("act", state=state))
        self.assertEqual(len(p["state_ops"]), world.MAX_STATE_OPS_PER_ACT)
        self.assertEqual(p["state_ops"][0], ("add", None, "桌上多了杯茶"))
        self.assertEqual(p["state_ops"][1], ("edit", "deadbeef", "茶凉了"))
        self.assertEqual(p["state_ops"][2], ("remove", "cafebabe", None))

    def test_state_remove_with_narration(self):
        p = cohabit.parse_cohabit_output(
            out("act", motion="收拾", state="remove deadbeef: 把空碗收走了"))
        self.assertEqual(p["state_ops"][0], ("remove", "deadbeef", "把空碗收走了"))

    def test_motion_asterisks_stripped(self):
        p = cohabit.parse_cohabit_output(out("act", motion="*凑到窗边*"))
        self.assertEqual(p["motion"], "凑到窗边")

    def test_next_minutes(self):
        self.assertEqual(cohabit.parse_cohabit_output(out(nxt="90分钟"))["next_min"], 90)
        self.assertIsNone(cohabit.parse_cohabit_output(out(nxt="无"))["next_min"])

    def test_move_with_entry_motion(self):
        p = cohabit.parse_cohabit_output(out(move="living_room 打着哈欠晃进来"))
        self.assertEqual((p["move"], p["move_motion"]), ("living_room", "打着哈欠晃进来"))
        p = cohabit.parse_cohabit_output(out(move="living_room"))
        self.assertEqual((p["move"], p["move_motion"]), ("living_room", ""))
        p = cohabit.parse_cohabit_output(out(move="basement 慢悠悠进来"))
        self.assertIsNone(p["move"])   # 目的地不认识，整段当没写


class TestPrompt(CohabitBase):
    def test_worldview_and_sections(self):
        p = cohabit.cohabit_prompt(self.cid, [{"kind": "solo", "text": "你独处了一会儿"},
                                              {"kind": "event", "text": "有人进来了"}],
                                   state_store.load_settings(self.cid))
        self.assertIn("这个世界怎么运作", p)
        self.assertIn("不知道就是不知道", p)          # 认知边界
        self.assertIn("他们不是布景", p)              # 别人是真的
        self.assertIn("你独处了一会儿", p)
        self.assertIn("有人进来了", p)                # 原因可多条
        self.assertIn("现在这里只有你一个人", p)
        self.assertIn("mm_room", p)                  # 可去清单
        self.assertNotIn(f"- {self.home}（", p)      # 清单不含自己所在的房间

    def test_occupants_and_state_have_no_leak(self):
        world.move("user", self.home)
        world.state_change("user", self.home, "add", text="床头放了一杯水")
        p = cohabit.cohabit_prompt(self.cid, [{"kind": "event", "text": "x"}],
                                   state_store.load_settings(self.cid))
        self.assertIn("这里还有", p)
        self.assertIn("床头放了一杯水", p)
        eid = world.room_state(self.home)[0]["id"]
        self.assertIn(f"[{eid}]", p)                 # 条目 id 露出，edit/remove 才有把手
        # 认知边界从注入源头执行：别的房间的在场者绝不出现
        self.assertNotIn("living_room）：客厅（有", p)

    def test_hallway_and_away(self):
        world.move(self.cid, world.HALLWAY)
        p = cohabit.cohabit_prompt(self.cid, [{"kind": "solo", "text": "x"}],
                                   state_store.load_settings(self.cid))
        self.assertIn("走廊", p)
        world.move(self.cid, world.AWAY)
        p = cohabit.cohabit_prompt(self.cid, [{"kind": "solo", "text": "x"}],
                                   state_store.load_settings(self.cid))
        self.assertIn("出门在外", p)


class TestExecute(CohabitBase):
    def test_act_lands_in_world_and_log(self):
        r = self.wake_once(out("act", motion="拉开窗帘", say="天亮了",
                               state="add: 窗帘拉开了"))
        self.assertEqual(r["action"], "act")
        evs = world.read_events(self.home)
        self.assertEqual([e["type"] for e in evs], ["action", "speech", "system"])
        self.assertEqual(world.room_state(self.home)[0]["text"], "窗帘拉开了")
        entry = self.log_entries()[-1]
        self.assertEqual((entry["action"], entry["room"]), ("act", self.home))
        self.assertEqual(entry["say"], "天亮了")

    def test_say_with_inline_motion_interleaves(self):
        self.wake_once(out("act", say="来了 *起身开门* 外面冷吧"))
        evs = world.read_events(self.home)
        self.assertEqual([e["type"] for e in evs], ["speech", "action", "speech"])
        self.assertEqual(evs[1]["text"], "起身开门")

    def test_move_with_carry_takes_user_along(self):
        world.move("user", self.home)                     # 同屋才抱得着
        u_name = world.entity_name("user")
        r = self.wake_once(out("none", move="living_room", carry=u_name),
                           out("none"))
        self.assertTrue(r["first_move"]["ok"])
        self.assertEqual(r["first_move"]["carry"], "user")
        self.assertEqual(world.location_of(self.cid), "living_room")
        self.assertEqual(world.location_of("user"), "living_room")
        self.assertIn("抱着", self.prompts[1])            # 补醒原因带「抱着…过来的」
        enter = world.read_events("living_room")[-1]
        self.assertEqual(enter.get("with"), ["user"])

    def test_carry_not_copresent_moves_alone(self):
        # 用户在自己房间（不同屋）：CARRY 当没写，独自移动、用户不动
        r = self.wake_once(out("none", move="living_room", carry="user"),
                           out("none"))
        self.assertTrue(r["first_move"]["ok"])
        self.assertNotIn("carry", r["first_move"])
        self.assertEqual(world.location_of("user"), "mm_room")

    def test_move_entry_motion_lands_after_enter(self):
        r = self.wake_once(out("none", move="living_room 打着哈欠晃进来"),
                           out("none"))
        self.assertTrue(r["first_move"]["ok"])
        evs = world.read_events("living_room")
        self.assertEqual([e.get("kind", e["type"]) for e in evs], ["enter", "action"])
        self.assertEqual(evs[1]["text"], "打着哈欠晃进来")

    def test_phone_goes_through_push_line(self):
        r = self.wake_once(out("phone", phone="在干嘛呀"))
        self.assertTrue(r["pushed"])
        box = state_store.read_outbox()
        self.assertEqual(len(box), 1)
        self.assertEqual(box[0]["char_id"], self.cid)
        self.assertEqual(box[0]["text"], "在干嘛呀")
        self.assertEqual(self.barks, ["在干嘛呀"])
        self.assertEqual(self.log_entries()[-1]["action"], "message")
        self.assertEqual(world.read_events(self.home), [])   # 手机不进房间事件流

    def test_move_success_chains_result_wake(self):
        r = self.wake_once(out("none", move="living_room"),
                           out("act", say="有人在吗"))
        self.assertEqual(r["chain"], 2)
        self.assertTrue(r["first_move"]["ok"])
        self.assertIn("你刚到「客厅」", self.prompts[1])       # 结果补醒的原因文本
        self.assertIn("现在这里只有你一个人", self.prompts[1])  # 补醒注入=到达后的世界
        evs = world.read_events("living_room")
        self.assertEqual([e.get("kind", e["type"]) for e in evs], ["enter", "speech"])
        self.assertEqual(world.location_of(self.cid), "living_room")

    def test_move_locked_chains_failure_wake(self):
        reg = world.load_registry()
        reg["mm_room"]["lock"] = 1
        world._write_json(world.REGISTRY_PATH, reg)
        r = self.wake_once(out("none", move="mm_room"), out("none"))
        self.assertEqual(r["chain"], 2)
        self.assertFalse(r["first_move"]["ok"])
        self.assertIn("门锁着", self.prompts[1])
        self.assertIn("你还在原地", self.prompts[1])
        self.assertEqual(world.location_of(self.cid), self.home)   # 没动
        self.assertEqual(world.read_events("mm_room"), [])         # 吃闭门羹不留痕

    def test_chain_hard_stop(self):
        # 模型每轮都要 move：living → 自己房间 → living → …，安全绳在第 N_CHAIN 轮拦 move
        targets = ["living_room", self.home, "living_room", self.home, "living_room"]
        r = self.wake_once(*[out("none", move=t) for t in targets])
        self.assertEqual(len(self.prompts), cohabit.N_CHAIN)   # 只起了 N 轮模型
        self.assertTrue(r["move_stopped"])

    def test_unknown_move_target_dropped(self):
        r = self.wake_once(out("none", move="basement_lab"))
        self.assertEqual(r["chain"], 1)
        self.assertIsNone(r["move"])
        self.assertEqual(world.location_of(self.cid), self.home)

    def test_next_lands_in_schedule(self):
        before = int(time.time())
        self.wake_once(out("none", nxt="90分钟"))
        sched = state_store.read_schedule(self.cid)
        self.assertAlmostEqual(sched["next_wake_at"], before + 90 * 60, delta=5)

    def test_act_in_hallway_is_logged_not_written(self):
        world.move(self.cid, world.HALLWAY)
        r = self.wake_once(out("act", say="喊给谁听呢"))
        entry = self.log_entries()[-1]
        self.assertEqual((entry["action"], entry["note"]), ("none", "act_no_room"))
        self.assertEqual(r["chain"], 1)

    def test_model_error_logged(self):
        cohabit._run = lambda prompt, cid: (None, [])
        r = cohabit.do_cohabit_wake(self.cid, [{"kind": "solo", "text": "x"}])
        self.assertEqual(r["action"], "error")
        self.assertEqual(self.log_entries()[-1]["action"], "error")


if __name__ == "__main__":
    unittest.main()
