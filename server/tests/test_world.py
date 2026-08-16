"""world.py（同居世界 C0）单测。

全部跑在临时目录上——world 的三个路径在 setUp 里换掉、tearDown 换回来，
绝不写真 state/（§5 测试纪律：别污染 Cass 和 TA 的活系统）。
characters/settings 是只读依赖（实体列表、称呼），读真的没关系。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_world -v
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import characters
import config
import world


class WorldBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="world_test_"))
        self._orig = (world.ROOMS_DIR, world.REGISTRY_PATH, world.WORLD_PATH)
        world.ROOMS_DIR = self.tmp / "rooms"
        world.REGISTRY_PATH = world.ROOMS_DIR / "registry.json"
        world.WORLD_PATH = self.tmp / "world.json"
        # 角色状态目录也指临时区：append_event 现在顺手写经历流（experience.jsonl），
        # 不改这个真 state 会被测试事件污染。
        import state_store
        self._csr_orig = state_store.CHAR_STATE_ROOT
        state_store.CHAR_STATE_ROOT = self.tmp / "chars"
        world.ensure_world()
        self.chars = characters.ids()
        self.c1 = self.chars[0]                    # 真实注册表里的第一个角色
        self.c1_room = f"{self.c1}_room"

    def tearDown(self):
        import state_store
        world.ROOMS_DIR, world.REGISTRY_PATH, world.WORLD_PATH = self._orig
        state_store.CHAR_STATE_ROOT = self._csr_orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _set_lock(self, room_id, lock, keys=None):
        reg = world.load_registry()
        reg[room_id]["lock"] = lock
        if keys is not None:
            reg[room_id]["keys"] = keys
        world._write_json(world.REGISTRY_PATH, reg)


class TestBootstrap(WorldBase):
    def test_rooms_and_initial_locations(self):
        reg = world.load_registry()
        for rid in ("mm_room", "wash_room", "living_room"):
            self.assertIn(rid, reg)
        for cid in self.chars:
            self.assertIn(f"{cid}_room", reg)
            self.assertEqual(reg[f"{cid}_room"]["owner"], cid)
            self.assertEqual(world.location_of(cid), f"{cid}_room")
        self.assertEqual(world.location_of(world.USER_ID), "mm_room")
        # 一期默认全 0：没有任何房间上锁
        self.assertTrue(all(not r["lock"] for r in reg.values()))

    def test_ensure_world_idempotent_keeps_edits(self):
        self._set_lock("wash_room", 1)
        world.ensure_world()   # 再跑一次不能把手编的注册表覆盖回默认
        self.assertEqual(world.room("wash_room")["lock"], 1)

    def test_unknown_room_is_keyerror(self):
        with self.assertRaises(KeyError):
            world.room("basement_lab")


class TestMove(WorldBase):
    def test_move_writes_leave_and_enter(self):
        r = world.move(world.USER_ID, "living_room")
        self.assertTrue(r["ok"])
        self.assertEqual(world.location_of(world.USER_ID), "living_room")
        self.assertIn(world.USER_ID, world.occupants("living_room"))
        leave = world.read_events("mm_room")[-1]
        enter = world.read_events("living_room")[-1]
        self.assertEqual((leave["kind"], leave["actor"]), ("leave", world.USER_ID))
        self.assertEqual((enter["kind"], enter["actor"]), ("enter", world.USER_ID))

    def test_move_to_same_room_is_noop(self):
        r = world.move(world.USER_ID, "mm_room")
        self.assertTrue(r["ok"] and r.get("noop"))
        self.assertEqual(world.read_events("mm_room"), [])

    def test_locked_door_blocks_and_leaves_no_trace(self):
        self._set_lock(self.c1_room, 1)
        r = world.move(world.USER_ID, self.c1_room)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "locked")
        self.assertIn("门锁着", r["text"])
        self.assertEqual(world.location_of(world.USER_ID), "mm_room")   # 没动
        self.assertEqual(world.read_events(self.c1_room), [])           # 吃闭门羹不留痕
        self.assertEqual(world.read_events("mm_room"), [])

    def test_key_and_owner_open_locked_door(self):
        self._set_lock(self.c1_room, 1, keys=[world.USER_ID])
        self.assertTrue(world.move(world.USER_ID, self.c1_room)["ok"])
        # 主人进出自己锁着的房间不受限
        world.move(self.c1, "living_room")
        self.assertTrue(world.move(self.c1, self.c1_room)["ok"])

    def test_away_and_hallway(self):
        r = world.move(world.USER_ID, world.AWAY)
        self.assertTrue(r["ok"])
        self.assertEqual(world.location_of(world.USER_ID), world.AWAY)
        self.assertEqual(world.read_events("mm_room")[-1]["kind"], "leave")
        self.assertNotIn(world.USER_ID, world.occupants("mm_room"))
        # 回家：从 away 直接进房间，正常过门禁、落 enter
        self.assertTrue(world.move(world.USER_ID, "living_room")["ok"])
        self.assertEqual(world.read_events("living_room")[-1]["kind"], "enter")
        # hallway：在楼里但不在任何房间
        self.assertTrue(world.move(world.USER_ID, world.HALLWAY)["ok"])
        self.assertEqual(world.location_of(world.USER_ID), world.HALLWAY)

    def test_move_unknown_room(self):
        with self.assertRaises(KeyError):
            world.move(world.USER_ID, "basement_lab")


class TestAct(WorldBase):
    def test_act_requires_presence(self):
        with self.assertRaises(world.NotPresent):
            world.act(world.USER_ID, "living_room", speech="有人吗")

    def test_act_writes_action_and_speech(self):
        world.move(world.USER_ID, "living_room")
        out = world.act(world.USER_ID, "living_room",
                        action="窝进沙发", speech="今天好冷")
        types = [e["type"] for e in out["events"]]
        self.assertEqual(types, ["action", "speech"])
        evs = world.read_events("living_room")
        self.assertEqual([e["type"] for e in evs], ["system", "action", "speech"])

    def test_act_rejects_empty(self):
        world.move(world.USER_ID, "living_room")
        with self.assertRaises(ValueError):
            world.act(world.USER_ID, "living_room")

    def test_split_mixed_interleaves(self):
        segs = world.split_mixed("*坐下* 今天好冷 *拉过毯子* 你也过来")
        self.assertEqual(segs, [("action", "坐下"), ("speech", "今天好冷"),
                                ("action", "拉过毯子"), ("speech", "你也过来")])
        self.assertEqual(world.split_mixed("就说一句"), [("speech", "就说一句")])
        self.assertEqual(world.split_mixed("*只做动作*"), [("action", "只做动作")])
        self.assertEqual(world.split_mixed("   "), [])   # 纯空白无段

    def test_split_mixed_crosses_newlines(self):
        # 2026-08-16 真机实锤的形态：星号和内容跨行写，旧正则配不上导致孤星号漏进气泡
        raw = ("「小狗你可以不叫，*\n汪我留着\n*。那声是你自己长的。」\n"
               "*尾巴往她手心里蹭了半寸*「毛你继续薅。汪。」")
        segs = world.split_mixed(raw)
        self.assertEqual([k for k, _ in segs],
                         ["speech", "action", "speech", "action", "speech"])
        self.assertEqual(segs[1][1], "汪我留着")
        self.assertNotIn("*", "".join(t for _, t in segs))   # 星号一个不剩

    def test_split_mixed_odd_asterisk_degrades_gracefully(self):
        segs = world.split_mixed("说着说着*忘了闭合星号")
        self.assertEqual(segs, [("speech", "说着说着忘了闭合星号")])
        segs = world.split_mixed("*完整动作* 然后一句话带个*孤星")
        self.assertEqual(segs, [("action", "完整动作"), ("speech", "然后一句话带个孤星")])

    def test_act_mixed_writes_ordered_events(self):
        world.move(world.USER_ID, "living_room")
        out = world.act_mixed(world.USER_ID, "living_room", "*窝进沙发* 好冷 *搓手*")
        self.assertEqual([e["type"] for e in out["events"]], ["action", "speech", "action"])
        with self.assertRaises(ValueError):
            world.act_mixed(world.USER_ID, "living_room", "  ")
        with self.assertRaises(world.NotPresent):
            world.act_mixed(world.USER_ID, "mm_room", "喂")


class TestRoomState(WorldBase):
    def test_add_edit_remove_with_notices(self):
        r = world.state_change(world.USER_ID, "mm_room", "add", text="桌上剩了半杯牛奶")
        eid = r["entry"]["id"]
        self.assertEqual(r["entry"]["author"], world.USER_ID)
        self.assertEqual(r["event"]["kind"], "state")
        self.assertIn("添加", r["event"]["text"])

        world.move(self.c1, "living_room")
        world.move(self.c1, "mm_room")   # 角色也能改，前提是在场
        r = world.state_change(self.c1, "mm_room", "edit", entry_id=eid, text="牛奶只剩杯底了")
        self.assertEqual(r["entry"]["author"], self.c1)   # author = 现在这句话是谁写的
        self.assertIn("改成", r["event"]["text"])

        r = world.state_change(self.c1, "mm_room", "remove", entry_id=eid)
        self.assertEqual(world.room_state("mm_room"), [])   # 快照只留结果
        self.assertIn("清掉", r["event"]["text"])

    def test_requires_presence_and_valid_entry(self):
        with self.assertRaises(world.NotPresent):
            world.state_change(world.USER_ID, "living_room", "add", text="x")
        with self.assertRaises(KeyError):
            world.state_change(world.USER_ID, "mm_room", "remove", entry_id="nope")
        with self.assertRaises(ValueError):
            world.state_change(world.USER_ID, "mm_room", "add", text="")

    def test_soft_cap_flag(self):
        for i in range(world.ROOM_STATE_SOFT_CAP):
            r = world.state_change(world.USER_ID, "mm_room", "add", text=f"东西{i}")
            self.assertFalse(r["over_cap"])
        r = world.state_change(world.USER_ID, "mm_room", "add", text="压垮软上限的那根稻草")
        self.assertTrue(r["over_cap"])
        self.assertEqual(len(world.room_state("mm_room")),
                         world.ROOM_STATE_SOFT_CAP + 1)   # 软上限：不硬删


class TestExperience(WorldBase):
    def test_present_chars_record_user_not(self):
        if len(self.chars) < 2:
            self.skipTest("单角色环境")
        c1, c2 = self.chars[0], self.chars[1]
        world.move(c1, "living_room")
        world.move(c2, "living_room")
        world.move("user", "living_room")
        world.act_mixed("user", "living_room", "*放下杯子* 都在呢")
        for cid in (c1, c2):
            texts = [e["text"] for e in world.read_experience(cid)]
            self.assertIn("放下杯子", texts)
            self.assertIn("都在呢", texts)
        # 用户没有经历流文件（用户的经历面是 app）
        self.assertEqual(world.read_experience("user"), [])

    def test_actor_records_own_acts_with_room(self):
        world.act_mixed(self.c1, self.c1_room, "自言自语一句")
        exp = world.read_experience(self.c1)
        self.assertEqual(exp[-1]["text"], "自言自语一句")
        self.assertEqual(exp[-1]["room"], self.c1_room)

    def test_absent_char_records_nothing(self):
        world.act_mixed(self.c1, self.c1_room, "没人听见")
        others = [c for c in self.chars if c != self.c1]
        for cid in others:
            self.assertNotIn("没人听见",
                             [e["text"] for e in world.read_experience(cid)])


class TestVisibility(WorldBase):
    def test_presence_interval(self):
        # c1 先到客厅说了话；user 后进——离场期间的事件永远看不见
        world.move(self.c1, "living_room")
        world.act(self.c1, "living_room", speech="没人的时候先自言自语一句")
        self.assertEqual(world.visible_events("living_room", world.USER_ID), [])
        world.move(world.USER_ID, "living_room")
        vis = world.visible_events("living_room", world.USER_ID)
        self.assertEqual(vis[0]["kind"], "enter")           # 区间从自己的 enter 起
        self.assertEqual(vis[0]["actor"], world.USER_ID)
        self.assertNotIn("自言自语", "".join(e["text"] for e in vis))
        # 进屋之后的发言看得见
        world.act(self.c1, "living_room", speech="回来啦")
        vis = world.visible_events("living_room", world.USER_ID)
        self.assertIn("回来啦", vis[-1]["text"])
        # 偷看（上帝视角）= 全部历史
        allevs = world.read_events("living_room")
        self.assertIn("自言自语", "".join(e["text"] for e in allevs))

    def test_bootstrap_resident_sees_all(self):
        # 开局就在自己房间、从没有 enter 事件的人 = 一直在场，看得见整个文件
        world.state_change(self.c1, self.c1_room, "add", text="窗台上有只纸飞机")
        vis = world.visible_events(self.c1_room, self.c1)
        self.assertEqual(len(vis), len(world.read_events(self.c1_room)))

    def test_reenter_resets_interval(self):
        world.move(self.c1, "living_room")
        world.act(self.c1, "living_room", speech="第一段")
        world.move(self.c1, self.c1_room)
        world.move(self.c1, "living_room")
        vis = world.visible_events("living_room", self.c1)
        self.assertNotIn("第一段", "".join(e["text"] for e in vis))


class TestRoutes(WorldBase):
    """路由层：真 HTTP 语义（TestClient 不跑 lifespan，watcher 线程不会起）。"""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import app as app_module
        cls.client = TestClient(app_module.app)
        cls.h = {"X-Auth": config.AUTH_KEY}

    def test_auth_required(self):
        self.assertEqual(self.client.get("/world").status_code, 401)

    def test_world_and_room_views(self):
        r = self.client.get("/world", headers=self.h).json()
        self.assertIn("mm_room", [x["id"] for x in r["rooms"]])
        self.assertEqual(r["entities"][world.USER_ID]["location"], "mm_room")
        room = self.client.get("/rooms/mm_room", headers=self.h).json()
        self.assertIn(world.USER_ID, room["occupants"])
        self.assertEqual(self.client.get("/rooms/nope", headers=self.h).status_code, 404)

    def test_move_act_state_events_flow(self):
        r = self.client.post("/world/move", json={"to": "living_room"}, headers=self.h)
        self.assertTrue(r.json()["ok"])
        r = self.client.post("/rooms/living_room/act",
                             json={"action": "把包放在门口", "speech": "我回来了"},
                             headers=self.h)
        self.assertEqual(r.status_code, 200)
        evs = self.client.get("/rooms/living_room/events", headers=self.h).json()["events"]
        self.assertEqual([e["type"] for e in evs], ["system", "action", "speech"])
        r = self.client.post("/rooms/living_room/state",
                             json={"op": "add", "text": "门口放着一只包"}, headers=self.h)
        self.assertEqual(r.status_code, 200)
        # 偷看别的房间的全部历史（scope=all 不要求在场）
        evs = self.client.get(f"/rooms/{self.c1_room}/events?scope=all",
                              headers=self.h).json()["events"]
        self.assertIsInstance(evs, list)

    def test_act_elsewhere_is_409(self):
        r = self.client.post("/rooms/living_room/act", json={"speech": "喂"}, headers=self.h)
        self.assertEqual(r.status_code, 409)

    def test_locked_door_via_http(self):
        self._set_lock(self.c1_room, 1)
        r = self.client.post("/world/move", json={"to": self.c1_room}, headers=self.h).json()
        self.assertFalse(r["ok"])
        self.assertIn("门锁着", r["text"])


if __name__ == "__main__":
    unittest.main()
