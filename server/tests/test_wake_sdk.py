"""wake_sdk（PLAN_sdk PR12 醒来升 A）单测：抽样器、〔〕分流、投递收尾、
maybe_wake 分路、chat_loop 醒来轮、PreToolUse 禁用面。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_wake_sdk -v
"""
import asyncio
import random
import shutil
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chat_loop
import config
import state_store
import wake
import wake_sdk

from tests.test_chat_loop import (ChatLoopTest, FakeClient, _m, _result,
                                  _text_events)


class SampleGapTest(unittest.TestCase):
    """抽时刻分布：边界、昼夜因子、静默单调性。"""

    def test_bounds(self):
        rng = random.Random(7)
        for silence in (0, 3600, 24 * 3600):
            for hour in (3, 15):
                for _ in range(200):
                    g = wake_sdk.sample_gap(silence, hour, rng)
                    self.assertGreaterEqual(g, wake_sdk.AUTO_GAP_MIN_SEC)
                    self.assertLessEqual(g, wake_sdk.AUTO_GAP_MAX_SEC)

    def test_mean_shrinks_with_silence(self):
        """间隔越久 hazard 越高（想 TA）：静默拉长 → 平均间隔变短。"""
        self.assertGreater(wake_sdk._mean_gap(0, 15),
                           wake_sdk._mean_gap(12 * 3600, 15))
        self.assertGreaterEqual(wake_sdk._mean_gap(48 * 3600, 15),
                                wake_sdk.MEAN_FLOOR_SEC)

    def test_night_factor(self):
        """深夜均值略升（世界没动静+省额度，不是他的作息）。"""
        self.assertAlmostEqual(wake_sdk._mean_gap(3600, 3),
                               wake_sdk._mean_gap(3600, 15) * wake_sdk.NIGHT_FACTOR)


class SplitMusingsTest(unittest.TestCase):
    """〔〕内=心里活动不发出去，〔〕外=消息。"""

    def test_plain_text_is_message(self):
        self.assertEqual(wake_sdk.split_musings("想你了"), ("想你了", ""))

    def test_all_musing_is_quiet(self):
        said, mus = wake_sdk.split_musings("〔就这么醒了一会儿，挺好〕")
        self.assertEqual(said, "")
        self.assertEqual(mus, "就这么醒了一会儿，挺好")

    def test_mixed(self):
        said, mus = wake_sdk.split_musings("〔犹豫了下〕还是想说一句：晚安〔说完踏实了〕")
        self.assertEqual(said, "还是想说一句：晚安")
        self.assertEqual(mus, "犹豫了下\n说完踏实了")

    def test_multiline_musing(self):
        said, mus = wake_sdk.split_musings("〔第一件事\n第二件事〕")
        self.assertEqual(said, "")
        self.assertIn("第一件事", mus)

    def test_empty(self):
        self.assertEqual(wake_sdk.split_musings(""), ("", ""))
        self.assertEqual(wake_sdk.split_musings(None), ("", ""))


class WakeStateBase(unittest.TestCase):
    """临时角色状态目录 + 全部外设打桩（不碰生产 outbox/Bark/浏览器/设置）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wakesdk_test_"))
        self._root_orig = state_store.CHAR_STATE_ROOT
        state_store.CHAR_STATE_ROOT = self.tmp / "chars"
        self.cid = "default"
        self.outbox: list[dict] = []
        self.barks: list[str] = []
        self.settings = {"enabled": True, "active_start": "10:00",
                         "active_end": "24:00", "day_freq": "mid",
                         "night_freq": "low"}
        self._orig = (state_store.outbox_append, state_store.load_settings,
                      state_store.read_sticker_catalog, wake_sdk.bark_push,
                      wake.is_daytime)
        import browser_keeper
        self._bk_orig = browser_keeper.apply_choice
        browser_keeper.apply_choice = lambda *a, **kw: None
        state_store.outbox_append = lambda item: self.outbox.append(item)
        state_store.load_settings = lambda cid=None: dict(self.settings)
        state_store.read_sticker_catalog = lambda: []
        wake_sdk.bark_push = lambda text, title=None: self.barks.append(text) or True
        wake.is_daytime = lambda settings, now: True

    def tearDown(self):
        import browser_keeper
        (state_store.outbox_append, state_store.load_settings,
         state_store.read_sticker_catalog, wake_sdk.bark_push,
         wake.is_daytime) = self._orig
        browser_keeper.apply_choice = self._bk_orig
        state_store.CHAR_STATE_ROOT = self._root_orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wake_log(self):
        return state_store.read_wake_log(char_id=self.cid)


class FinishWakeTurnTest(WakeStateBase):
    def test_message_delivered(self):
        d = wake_sdk.finish_wake_turn(self.cid, "auto", False, 5000,
                                      "〔想了想〕睡前想说一句：晚安", [])
        self.assertEqual(d, "睡前想说一句：晚安")
        self.assertEqual(len(self.outbox), 1)
        self.assertEqual(self.outbox[0]["origin"], "wake")
        self.assertEqual(self.outbox[0]["ts"], 5000)
        # 窗口跟上（:342 铁律）
        win = state_store.read_recent_window(self.cid)
        self.assertEqual(win[-1]["role"], "assistant")
        self.assertEqual(win[-1]["text"], "睡前想说一句：晚安")
        # 白天 → Bark 推了
        self.assertEqual(len(self.barks), 1)
        log = self.wake_log()
        self.assertEqual(log[-1]["action"], "message")
        self.assertEqual(log[-1]["engine"], "sdk")
        self.assertEqual(log[-1]["thoughts"], "想了想")
        # 醒完重抽：auto_wake_at 落了新点
        self.assertIsNotNone(state_store.read_schedule(self.cid).get("auto_wake_at"))

    def test_quiet_wake(self):
        d = wake_sdk.finish_wake_turn(self.cid, "auto", False, 5000,
                                      "〔没什么想说的，接着歇〕", [])
        self.assertEqual(d, "")
        self.assertEqual(self.outbox, [])
        self.assertEqual(self.barks, [])
        log = self.wake_log()
        self.assertEqual(log[-1]["action"], "none")
        self.assertIn("接着歇", log[-1]["thoughts"])

    def test_night_holds_bark_not_message(self):
        """夜间收敛只收 Bark（§0.3）：消息照进 outbox，TA 早上看得到。"""
        wake.is_daytime = lambda settings, now: False
        wake_sdk.finish_wake_turn(self.cid, "auto", False, 5000, "凌晨的念头", [])
        self.assertEqual(len(self.outbox), 1)
        self.assertEqual(self.barks, [])

    def test_forced_barks_at_night(self):
        wake.is_daytime = lambda settings, now: False
        wake_sdk.finish_wake_turn(self.cid, "mail", True, 5000, "有封要紧的信", [])
        self.assertEqual(len(self.barks), 1)

    def test_next_wake_pinned_with_todo(self):
        before = int(time.time())
        wake_sdk.finish_wake_turn(self.cid, "auto", False, 5000,
                                  "〔静〕[[next_wake:2小时|给安瞬回信]]", [])
        sched = state_store.read_schedule(self.cid)
        self.assertAlmostEqual(sched["next_wake_at"], before + 7200, delta=5)
        self.assertEqual(sched["next_wake_todo"], "给安瞬回信")
        # 标记剥干净：这轮没说话（安静醒着）
        self.assertEqual(self.outbox, [])
        self.assertEqual(self.wake_log()[-1]["action"], "none")

    def test_scheduled_point_consumed(self):
        state_store.write_schedule({"next_wake_at": int(time.time()) - 5,
                                    "next_wake_todo": "回信"}, self.cid)
        wake_sdk.finish_wake_turn(self.cid, "scheduled", False, 5000, "〔到点醒了〕", [])
        sched = state_store.read_schedule(self.cid)
        self.assertIsNone(sched["next_wake_at"])
        self.assertEqual(sched["next_wake_todo"], "")

    def test_future_point_survives_early_wake(self):
        at = int(time.time()) + 3600
        state_store.write_schedule({"next_wake_at": at,
                                    "next_wake_todo": "回信"}, self.cid)
        wake_sdk.finish_wake_turn(self.cid, "auto", False, 5000, "〔提前醒了下〕", [])
        sched = state_store.read_schedule(self.cid)
        self.assertEqual(sched["next_wake_at"], at)
        self.assertEqual(sched["next_wake_todo"], "回信")

    def test_acts_recorded(self):
        """wake_log 扩展字段（行为留痕先行，PR13 归一账本）。"""
        stored = [{"tool": "mail_read", "ok": True, "text": "安瞬的信"}]
        wake_sdk.finish_wake_turn(self.cid, "auto", False, 5000, "〔读了信〕", stored)
        log = self.wake_log()[-1]
        self.assertEqual(log["acts"][0]["tool"], "mail_read")

    def test_no_acts_mirror_from_stored(self):
        """08-31：行为账只有执行层一个写入点。醒来轮也走常驻聊天 session，
        这儿再镜像一遍就是记两遍；而且 stored 是「他说他做了什么」，拿自述
        当留痕判据正是事故里靠不住的那一边。"""
        import activity_log
        orig = activity_log.ACT_DIR
        activity_log.ACT_DIR = self.tmp / "activity"
        try:
            stored = [{"tool": "mail", "ok": True, "text": "给安瞬：回信"},
                      {"tool": "hold", "ok": True, "text": "一条记忆"}]
            wake_sdk.finish_wake_turn(self.cid, "auto", False, 5000,
                                      "〔回了封信〕", stored)
            self.assertEqual(activity_log.recent_acts(self.cid), [])
            # 心流日志那本（自述）照旧留着
            self.assertEqual(self.wake_log()[-1]["acts"][0]["tool"], "mail")
        finally:
            activity_log.ACT_DIR = orig

    def test_dead_writes_cooldown(self):
        wake_sdk._wake_dead(self.cid, "auto")
        sched = state_store.read_schedule(self.cid)
        self.assertGreater(float(sched["cooldown_until"]), time.time())
        self.assertEqual(self.wake_log()[-1]["action"], "error")

    def test_dead_auto_leaves_the_nail_alone(self):
        at = int(time.time()) + 3600
        state_store.write_schedule({"next_wake_at": at, "next_wake_todo": "回信"}, self.cid)
        wake_sdk._wake_dead(self.cid, "auto")
        self.assertEqual(state_store.read_schedule(self.cid)["next_wake_at"], at)

    def test_dead_scheduled_pushes_the_nail_to_cooldown_end(self):
        """到点的钟醒失败 → 挪到冷却结束（2026-09-01）。钉子不丢，
        又不会因为「冷却不再拦 scheduled」变成每 tick 对着坏引擎硬试一次。"""
        state_store.write_schedule({"next_wake_at": int(time.time()) - 5,
                                    "next_wake_todo": "回信"}, self.cid)
        wake_sdk._wake_dead(self.cid, "scheduled")
        sched = state_store.read_schedule(self.cid)
        self.assertEqual(sched["next_wake_at"], sched["cooldown_until"])
        self.assertEqual(sched["next_wake_todo"], "回信")   # 活跟着钟走，没做成不等于不算数


class SeenItemsFilterTest(WakeStateBase):
    def test_sdk_wake_thoughts_not_reinjected(self):
        """engine=sdk 的醒来内心不进 seen_items：内心已活在 transcript 里，
        注回去=重铸后闲念头变档案复活（「推断回灌固化」的载体，08-30
        Cassius「你昨晚五点睡的」实锤——05:05 一句猜测回灌两轮变成事实）。"""
        import pipeline
        now = int(time.time())
        state_store.append_wake_log({"ts": now - 100, "source": "wake",
                                     "action": "none", "thoughts": "老路的念头"},
                                    char_id=self.cid)
        state_store.append_wake_log({"ts": now - 50, "source": "wake",
                                     "action": "none", "engine": "sdk",
                                     "thoughts": "session 里的念头"},
                                    char_id=self.cid)
        texts = [t for _, t in pipeline.seen_items(self.cid)]
        self.assertTrue(any("老路的念头" in t for t in texts))
        self.assertFalse(any("session 里的念头" in t for t in texts))


class MaybeAutoTest(WakeStateBase):
    def test_first_call_samples_not_wakes(self):
        calls = []
        orig = wake_sdk.enqueue_wake
        wake_sdk.enqueue_wake = lambda *a, **kw: calls.append(a) or True
        try:
            self.assertFalse(wake_sdk.maybe_auto(self.cid))
            self.assertIsNotNone(state_store.read_schedule(self.cid)["auto_wake_at"])
            self.assertEqual(calls, [])
        finally:
            wake_sdk.enqueue_wake = orig

    def test_new_interaction_resamples(self):
        """anchor 变了（TA 说了新话）→ 重抽不醒，哪怕旧点已到。"""
        state_store.write_schedule({"auto_wake_at": int(time.time()) - 5,
                                    "auto_anchor_ts": 111}, self.cid)
        state_store.write_recent_window(
            [{"role": "user", "text": "在忙吗", "ts": 222}], self.cid)
        calls = []
        orig = wake_sdk.enqueue_wake
        wake_sdk.enqueue_wake = lambda *a, **kw: calls.append(a) or True
        try:
            self.assertFalse(wake_sdk.maybe_auto(self.cid))
            sched = state_store.read_schedule(self.cid)
            self.assertEqual(sched["auto_anchor_ts"], 222)
            self.assertGreater(float(sched["auto_wake_at"]), time.time())
            self.assertEqual(calls, [])
        finally:
            wake_sdk.enqueue_wake = orig

    def test_due_point_fires_and_consumed(self):
        state_store.write_recent_window(
            [{"role": "user", "text": "在忙吗", "ts": 222}], self.cid)
        state_store.write_schedule({"auto_wake_at": int(time.time()) - 5,
                                    "auto_anchor_ts": 222}, self.cid)
        calls = []
        orig = wake_sdk.enqueue_wake
        wake_sdk.enqueue_wake = lambda cid, trigger, **kw: calls.append(trigger) or True
        try:
            self.assertTrue(wake_sdk.maybe_auto(self.cid))
            self.assertEqual(calls, ["auto"])
            self.assertIsNone(state_store.read_schedule(self.cid)["auto_wake_at"])
        finally:
            wake_sdk.enqueue_wake = orig


class MaybeWakeRoutingTest(unittest.IsolatedAsyncioTestCase, WakeStateBase):
    """sdk 角色分路：邮件硬触发/scheduled/auto 走 wake_sdk；-p 角色一行不改。"""

    def setUp(self):
        WakeStateBase.setUp(self)
        self.cid = "default"
        self.enq: list[tuple] = []
        self.auto: list[str] = []
        self.old_path: list[str] = []
        self._route_orig = (wake_sdk.enqueue_wake, wake_sdk.maybe_auto,
                            wake.game_session_owner, wake._mail_wake_note,
                            config.CHAT_ENGINE, wake.do_wake_sync_locked)
        # 老路本体必须打桩：不打的话走到老路的用例会在线程池里起**真的 claude -p**
        # （首版实踩：test_sdk_off 一条用例跑了 40 秒、烧了一次真调用）。
        wake.do_wake_sync_locked = (lambda settings, trigger, *a, **kw:
                                    self.old_path.append(trigger) or {"action": "none"})
        import world
        self._house_orig = world.house_active
        world.house_active = lambda: False
        wake_sdk.enqueue_wake = (lambda cid, trigger, note="", force=False:
                                 self.enq.append((cid, trigger, force)) or True)
        wake_sdk.maybe_auto = lambda cid, now=None: self.auto.append(cid) or False
        wake.game_session_owner = lambda: None
        wake._mail_wake_note = lambda cid: ""
        config.CHAT_ENGINE = "sdk"
        chat_loop.SDK_CHAT_OFF.clear()

    def tearDown(self):
        import world
        (wake_sdk.enqueue_wake, wake_sdk.maybe_auto,
         wake.game_session_owner, wake._mail_wake_note,
         config.CHAT_ENGINE, wake.do_wake_sync_locked) = self._route_orig
        world.house_active = self._house_orig
        chat_loop.SDK_CHAT_OFF.clear()
        WakeStateBase.tearDown(self)

    async def test_sdk_auto_path(self):
        await wake.maybe_wake(self.cid)
        self.assertEqual(self.enq, [])
        self.assertEqual(self.auto, [self.cid])

    async def test_sdk_scheduled(self):
        state_store.write_schedule({"next_wake_at": int(time.time()) - 5}, self.cid)
        await wake.maybe_wake(self.cid)
        self.assertEqual(self.enq, [(self.cid, "scheduled", False)])

    async def test_sdk_mail_hard_trigger(self):
        wake._mail_wake_note = lambda cid: "来自安瞬的新邮件"
        await wake.maybe_wake(self.cid)
        self.assertEqual(self.enq, [(self.cid, "mail", True)])

    async def test_sdk_defers_when_his_session_open(self):
        """他自己的 code/game 会话开着 → 攒着（段中插入归 PR13）。"""
        wake.game_session_owner = lambda: self.cid
        state_store.write_schedule({"next_wake_at": int(time.time()) - 5}, self.cid)
        await wake.maybe_wake(self.cid)
        self.assertEqual(self.enq, [])
        wake._game_avoid.pop(self.cid, None)

    async def test_sdk_scheduled_fires_mid_pump(self):
        """PR13 泵中插入：占用=骑在他自己聊天上的 game 泵 → NEXT 定时醒照入队
        （队列串行，下个短轮到）；自发抽签醒仍不掷（他醒着在玩）。"""
        import session_mgr as sm
        wake.game_session_owner = lambda: self.cid
        ch = sm.LoopHandle(char_id=self.cid, scene="chat")
        ch.meta["game_pump"] = {"seg_id": "s", "start_ts": 1.0}
        sm._registry[(self.cid, "chat")] = ch
        try:
            state_store.write_schedule({"next_wake_at": int(time.time()) - 5},
                                       self.cid)
            await wake.maybe_wake(self.cid)
            self.assertEqual(self.enq, [(self.cid, "scheduled", False)])
            # 没到点时：auto 不掷、也不入队
            self.enq.clear()
            state_store.write_schedule({}, self.cid)
            await wake.maybe_wake(self.cid)
            self.assertEqual(self.enq, [])
            self.assertEqual(self.auto, [])
        finally:
            sm._registry.pop((self.cid, "chat"), None)
            wake._game_avoid.pop(self.cid, None)

    async def test_sdk_defers_when_other_chars_pump(self):
        """占用是**别人**的泵/会话 → 照旧避让（别往不在场的人聊天里塞醒来）。"""
        import session_mgr as sm
        wake.game_session_owner = lambda: self.cid   # 探出归属=他（资源语义）
        ch = sm.LoopHandle(char_id="cass", scene="chat")   # 但泵骑在别人 chat 上
        ch.meta["game_pump"] = {"seg_id": "s", "start_ts": 1.0}
        sm._registry[("cass", "chat")] = ch
        try:
            state_store.write_schedule({"next_wake_at": int(time.time()) - 5},
                                       self.cid)
            await wake.maybe_wake(self.cid)
            self.assertEqual(self.enq, [])
        finally:
            sm._registry.pop(("cass", "chat"), None)
            wake._game_avoid.pop(self.cid, None)

    async def test_sdk_ignores_chat_turn_active(self):
        """撞轮=队列天然串行：chat 轮进行中照样入队（老路才避让）。"""
        wake.chat_turn_begin(self.cid)
        try:
            state_store.write_schedule({"next_wake_at": int(time.time()) - 5},
                                       self.cid)
            await wake.maybe_wake(self.cid)
            self.assertEqual(self.enq, [(self.cid, "scheduled", False)])
        finally:
            wake.chat_turn_end(self.cid)

    # ---------- 错误退避只退避自发的（2026-09-01 收窄）----------
    # 欠的账：08-31 22:34 一次自醒死掉 → 冷却压到 23:04:59，冷却窗内到点的钟被这道闸
    # 整个吞掉、日志里一个字没有。失败退避是「别对着坏引擎硬试」，不是「他答应的时刻可以顺延」。

    async def test_cooldown_still_blocks_auto(self):
        state_store.write_schedule({"cooldown_until": time.time() + 600}, self.cid)
        await wake.maybe_wake(self.cid)
        self.assertEqual(self.auto, [])
        self.assertEqual(self.enq, [])

    async def test_cooldown_lets_the_due_nail_through(self):
        state_store.write_schedule({"cooldown_until": time.time() + 600,
                                    "next_wake_at": int(time.time()) - 5}, self.cid)
        await wake.maybe_wake(self.cid)
        self.assertEqual(self.enq, [(self.cid, "scheduled", False)])

    async def test_cooldown_with_future_nail_blocks_everything(self):
        """钟还没到点 → 冷却照旧全拦（放行的是「到点」，不是「有钟」）。"""
        state_store.write_schedule({"cooldown_until": time.time() + 600,
                                    "next_wake_at": int(time.time()) + 600}, self.cid)
        await wake.maybe_wake(self.cid)
        self.assertEqual(self.enq, [])
        self.assertEqual(self.auto, [])

    async def test_cooldown_does_not_consume_mail_flag(self):
        """冷却期不碰邮件 flag：读了就是消费掉，而这会儿引擎正坏着，醒不成信就白丢了。"""
        seen: list[str] = []
        wake._mail_wake_note = lambda cid: seen.append(cid) or "来自安瞬的新邮件"
        state_store.write_schedule({"cooldown_until": time.time() + 600,
                                    "next_wake_at": int(time.time()) - 5}, self.cid)
        await wake.maybe_wake(self.cid)
        self.assertEqual(seen, [])
        self.assertEqual(self.enq, [(self.cid, "scheduled", False)])

    async def test_cooldown_old_path_also_lets_the_nail_through(self):
        config.CHAT_ENGINE = ""                  # -p 角色：两条路同一个口径
        state_store.write_schedule({"cooldown_until": time.time() + 600,
                                    "next_wake_at": int(time.time()) - 5}, self.cid)
        await wake.maybe_wake(self.cid)
        self.assertEqual(self.old_path, ["scheduled"])

    async def test_cooldown_old_path_still_blocks_probability(self):
        config.CHAT_ENGINE = ""
        state_store.write_schedule({"cooldown_until": time.time() + 600}, self.cid)
        orig = wake.random.random
        wake.random.random = lambda: 0.0         # 掷骰必中，被拦住才算数
        try:
            await wake.maybe_wake(self.cid)
        finally:
            wake.random.random = orig
        self.assertEqual(self.old_path, [])

    async def test_random_wake_off_skips_auto(self):
        self.settings["random_wake"] = False
        await wake.maybe_wake(self.cid)
        self.assertEqual(self.auto, [])
        self.assertEqual(self.enq, [])

    async def test_p_char_stays_on_old_path(self):
        config.CHAT_ENGINE = "sdk:cass"          # default 不在灰度里
        self.settings["random_wake"] = False     # 老路走到概率前返回，不起模型
        await wake.maybe_wake(self.cid)
        self.assertEqual(self.enq, [])
        self.assertEqual(self.auto, [])

    async def test_sdk_off_falls_back_to_old_path(self):
        chat_loop.SDK_CHAT_OFF.add(self.cid)
        state_store.write_schedule({"next_wake_at": int(time.time()) - 5}, self.cid)
        await wake.maybe_wake(self.cid)
        self.assertEqual(self.enq, [])                  # 熄火：sdk 路一步不走
        self.assertEqual(self.old_path, ["scheduled"])  # 老路接住了到点的 NEXT


class WakeTurnPumpTest(ChatLoopTest):
    """chat_loop 里的醒来轮：镜像开 session、投递才入账、安静轮账不动、轮死 on_dead。"""

    MIRROR = [{"role": "user", "text": "早", "ts": 1000},
              {"role": "assistant", "text": "早，小狗", "ts": 1001}]

    async def asyncSetUp(self):
        await ChatLoopTest.asyncSetUp(self)
        import pipeline
        self._rw_orig = state_store.read_recent_window
        self._sc_orig = state_store.read_sticker_catalog
        self._mt_orig = pipeline.mounted_tool_names
        state_store.read_recent_window = lambda cid=None: list(self.MIRROR)
        state_store.read_sticker_catalog = lambda: []
        pipeline.mounted_tool_names = lambda ctx, cid=None: ["mcp__ombre__breath"]

    async def asyncTearDown(self):
        import pipeline
        state_store.read_recent_window = self._rw_orig
        state_store.read_sticker_catalog = self._sc_orig
        pipeline.mounted_tool_names = self._mt_orig
        await ChatLoopTest.asyncTearDown(self)

    def _wake_turn(self, delivered, on_dead=None):
        calls = {"finalize": []}

        def fin(reply, stored):
            calls["finalize"].append(reply)
            return {"reply": delivered}

        t = chat_loop.Turn(rid="w1", history=[], new_msg={}, injection="",
                           finalize=fin, kind="wake",
                           injection_factory=lambda: "〔现在是深夜两点〕",
                           on_dead=on_dead)
        return t, calls

    async def test_wake_opens_from_mirror_and_ledgers_delivery(self):
        turn, calls = self._wake_turn("想你了")
        await self._play(turn, *_text_events("〔醒了〕想你了"),
                         _result("〔醒了〕想你了"))
        # 镜像开的 session：铸的是 recent_window
        self.assertEqual([m["text"] for m in self.forged[0]],
                         [chat_loop._stamp_times([{"role": "user", "text": "早",
                                                   "ts": 1000}])[0]["text"],
                          "早，小狗"])   # TA 的话带时间锚，他自己的不带
        # 注入=执行时组装的感知白描
        sent = self.clients[0].queries[0][0]["message"]["content"][0]["text"]
        self.assertIn("〔现在是深夜两点〕", sent)
        # 投递了 → 账追加恰好一条 assistant（没有 user 条）
        led = self.handle.meta["ledger"]
        self.assertEqual([e["r"] for e in led], ["user", "assistant", "assistant"])
        self.assertEqual(led[-1]["h"], chat_loop._h("想你了"))
        self.assertEqual(calls["finalize"], ["〔醒了〕想你了"])
        # 轮来源标签用完即收（门的默认态=放行）
        self.assertNotIn("turn_kind", self.handle.meta)
        self.assertFalse(self.clients[0].disconnected)

    async def test_quiet_wake_leaves_ledger_alone(self):
        turn, _ = self._wake_turn("")
        await self._play(turn, *_text_events("〔安静醒着〕"), _result("〔安静醒着〕"))
        led = self.handle.meta["ledger"]
        self.assertEqual([e["r"] for e in led], ["user", "assistant"])
        # 安静轮不算失败：session 活着，下一轮复用
        self.assertFalse(self.clients[0].disconnected)
        turn2, _ = self._wake_turn("这回说话了")
        await self._play(turn2, *_text_events("这回说话了"), _result("这回说话了"))
        self.assertEqual(len(self.clients), 1)

    async def test_wake_turn_death_calls_on_dead(self):
        dead = []
        turn, calls = self._wake_turn("不会送到", on_dead=lambda: dead.append(1))
        await self._play(turn, _result("", is_error=True,
                                       subtype="error_during_execution"))
        self.assertEqual(dead, [1])
        self.assertEqual(calls["finalize"], [])        # 收尾没跑到
        self.assertTrue(self.clients[0].disconnected)  # 关 session 惰性重起

    async def test_wake_sets_gate_inputs_during_turn(self):
        """轮进行中 meta 带 turn_kind=wake + 醒来允许集（PreToolUse 门的输入）。"""
        turn, _ = self._wake_turn("x")
        self.handle.queue.put_nowait(turn)
        for _ in range(20):
            await asyncio.sleep(0)
            if self.clients and self.clients[-1].queries:
                break
        self.assertEqual(self.handle.meta.get("turn_kind"), "wake")
        self.assertEqual(self.handle.meta.get("wake_tools"), {"mcp__ombre__breath"})
        self.clients[-1].feed(*_text_events("x"), _result("x"))
        while await turn.out.get() is not None:
            pass


class WakeGateTest(unittest.IsolatedAsyncioTestCase):
    """PreToolUse 门：只在醒来轮收禁用面，其余轮全放行。"""

    async def test_gate_matrix(self):
        import session_mgr as sm
        handle = sm.LoopHandle(char_id="default", scene="chat")
        gate = chat_loop._wake_gate(handle)
        # 聊天轮（无标签）：放行
        self.assertEqual(await gate({"tool_name": "mcp__mail__mail_send"}, None, {}), {})
        # 醒来轮：允许集内放行、外面拒
        handle.meta["turn_kind"] = "wake"
        handle.meta["wake_tools"] = {"mcp__ombre__breath"}
        self.assertEqual(await gate({"tool_name": "mcp__ombre__breath"}, None, {}), {})
        out = await gate({"tool_name": "mcp__codemode__codemode"}, None, {})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        # 标签收掉后恢复放行
        handle.meta.pop("turn_kind")
        self.assertEqual(await gate({"tool_name": "mcp__codemode__codemode"}, None, {}), {})

    def test_wake_contract_wording(self):
        """契约只讲机制不讲存在论（08-30 拍板的口径延续）。"""
        h = chat_loop._wake_contract()
        self.assertIn("〔〕", h)
        self.assertNotIn("进程", h)
        self.assertNotIn("只有这一轮", h)


if __name__ == "__main__":
    unittest.main()
