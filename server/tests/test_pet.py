"""pet_store / pet_engine（PLAN_pet P1）单测。

基座沿用 tests.test_world.WorldBase（世界三路径 + 角色状态全在临时目录），再加两样：
宠物状态根目录指临时区、DeepSeek 打桩（罐头 JSON 按调用序回放，顺带录 prompt）。
一个真模型都不起、一个真状态都不碰（§5 测试纪律）。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_pet -v
"""
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import pet_engine
import pet_store
import world
from tests.test_world import WorldBase


class PetBase(WorldBase):
    def setUp(self):
        super().setUp()
        self._psr_orig = pet_store.PETS_STATE_ROOT
        pet_store.PETS_STATE_ROOT = self.tmp / "petstate"
        self.pid = self._register_pet()          # tuantuan / 团团 + 猫房补种
        self.cat_room = f"{self.pid}_room"
        self._call_orig = pet_engine._call_model
        self.prompts: list[str] = []
        self.replies: list[dict] = []

        def fake_call(pid, prompt):
            self.prompts.append(prompt)
            return json.dumps(self.replies.pop(0) if self.replies else {})
        pet_engine._call_model = fake_call

    def tearDown(self):
        pet_store.PETS_STATE_ROOT = self._psr_orig
        pet_engine._call_model = self._call_orig
        super().tearDown()

    def force_state(self, **kw):
        """直写 baseline（绕过 mutate 的 updated_at=now），模拟时间流逝用。"""
        s = pet_store._load_raw(self.pid)
        s.update(kw)
        pet_store._write_json(pet_store._dir(self.pid) / "state.json", s)


class TestStore(PetBase):
    def test_decay_awake_grows_pressure(self):
        self.force_state(satiety=50.0, poop_pressure=0.0, asleep=False,
                         updated_at=time.time() - 7200)   # 2 小时前
        s = pet_store.read_state(self.pid)
        self.assertAlmostEqual(s["satiety"], 40, delta=1)          # -5/h
        s_raw = pet_store._apply_decay(pet_store._load_raw(self.pid), time.time())
        self.assertAlmostEqual(s_raw["poop_pressure"], 18, delta=1)  # +9/h

    def test_decay_asleep_pauses_and_autowakes(self):
        self.force_state(satiety=50.0, energy=80.0, poop_pressure=10.0, asleep=True,
                         updated_at=time.time() - 3600)
        s = pet_store.read_state(self.pid)
        self.assertAlmostEqual(s["satiety"], 50, delta=1)   # 睡着不掉
        self.assertFalse(s["asleep"])                       # 80+20 → 睡够自醒
        raw = pet_store._apply_decay(pet_store._load_raw(self.pid), time.time())
        self.assertAlmostEqual(raw["poop_pressure"], 10, delta=1)   # 憋屎也暂停

    def test_needs_thresholds(self):
        kinds = lambda s: [n["kind"] for n in pet_store.needs(s)]
        base = dict(satiety=80, hydration=80, energy=80, mood=50,
                    litter=1, asleep=False, poop_pressure=0)
        self.assertEqual(kinds(base), [])
        self.assertIn("hungry", kinds({**base, "satiety": 30}))
        self.assertIn("thirsty", kinds({**base, "hydration": 30}))
        self.assertIn("sleepy", kinds({**base, "energy": 10}))
        self.assertIn("poop", kinds({**base, "poop_pressure": 70}))
        # 砂盆满了：想拉变成「去找人铲」——物理化薅醒的种子
        self.assertIn("poop_blocked",
                      kinds({**base, "poop_pressure": 70, "litter": 3}))


class TestInteract(PetBase):
    def test_requires_copresence(self):
        with self.assertRaises(pet_engine.PetNotHere):     # c1 在自己房间，猫在猫房
            pet_engine.interact(self.pid, self.c1, feed="罐罐")

    def test_feed_flow(self):
        world.move(self.c1, self.cat_room)
        self.replies = [{"reply": "*凑过来蹭了蹭* 喵",
                         "stats": {"satiety": 80, "mood": 70}, "log": "吃了罐罐"}]
        res = pet_engine.interact(self.pid, self.c1, feed="罐罐")
        self.assertIn("蹭", res["reply"])
        kinds = [(e["type"], e.get("kind"), e["actor"])
                 for e in world.read_events(self.cat_room)]
        self.assertIn(("system", "pet_feed", self.c1), kinds)      # 喂食系统提示
        self.assertIn(("action", None, self.pid), kinds)           # 猫的动作
        self.assertIn(("speech", None, self.pid), kinds)           # 喵
        s = pet_store.read_state(self.pid)
        self.assertEqual(s["satiety"], 80)
        self.assertAlmostEqual(s["poop_pressure"], pet_store.POOP_MEAL_BOOST, delta=1)
        texts = [r["text"] for r in pet_store.read_petlog(self.pid)]
        self.assertTrue(any("喂了罐罐" in t for t in texts))
        self.assertIn("吃了罐罐", texts[-1])

    def test_text_interaction_lands_actor_action(self):
        world.move(self.c1, self.cat_room)
        self.replies = [{}]                                # 猫爱答不理
        pet_engine.interact(self.pid, self.c1, text="挠了挠团团的下巴")
        evs = world.read_events(self.cat_room)
        self.assertEqual((evs[-1]["type"], evs[-1]["actor"]), ("action", self.c1))
        self.assertIn("挠了挠", evs[-1]["text"])
        # 猫没反应：没有以猫为 actor 的新事件
        self.assertFalse([e for e in evs if e["actor"] == self.pid])

    def test_poop_only_in_cat_room(self):
        world.move(self.pid, "living_room")
        world.move(self.c1, "living_room")
        self.replies = [{"action": "拉屎"}]
        res = pet_engine.interact(self.pid, self.c1, text="逗了逗团团")
        self.assertEqual(res["action"], "none")            # 客厅没猫砂盆，当没拉
        self.assertEqual(pet_store.read_state(self.pid)["litter"], 1)
        world.move(self.pid, self.cat_room)
        world.move(self.c1, self.cat_room)
        self.force_state(poop_pressure=80.0)
        self.replies = [{"action": "拉屎"}]
        res = pet_engine.interact(self.pid, self.c1, text="看着团团")
        self.assertEqual(res["action"], "拉屎")
        s = pet_store.read_state(self.pid)
        self.assertEqual(s["litter"], 2)
        self.assertLess(s["poop_pressure"], 5)             # 拉完清零

    def test_litter_full_refuses_poop(self):
        world.move(self.c1, self.cat_room)
        self.force_state(litter=3, poop_pressure=80.0)
        self.replies = [{"action": "拉屎"}]
        res = pet_engine.interact(self.pid, self.c1, text="看着团团")
        self.assertEqual(res["action"], "none")
        s = pet_store.read_state(self.pid)
        self.assertEqual(s["litter"], 3)
        self.assertGreater(s["poop_pressure"], 70)         # 继续憋着 → needs 会催去找人


class TestWakeMovePose(PetBase):
    def test_pose_then_move_cleans_up(self):
        self.replies = [{"pose": "蜷在猫窝里打盹", "sleep": True}]
        pet_engine.wake(self.pid, ["你困了"])
        st = world.room_state(self.cat_room)
        self.assertTrue(any("蜷在猫窝里" in e["text"] for e in st))
        self.assertTrue(pet_store.read_state(self.pid)["asleep"])
        # 醒来挪去客厅：旧姿态条目清掉、新房间落新姿态
        self.replies = [{"sleep": False, "move": "living_room", "pose": "趴在地毯上晒肚子"}]
        pet_engine.wake(self.pid, ["有动静吵醒了你"])
        self.assertEqual(world.location_of(self.pid), "living_room")
        self.assertFalse([e for e in world.room_state(self.cat_room)
                          if "蜷在猫窝里" in e["text"]])
        self.assertTrue(any("趴在地毯上" in e["text"]
                            for e in world.room_state("living_room")))
        kinds = [e.get("kind") for e in world.read_events("living_room")]
        self.assertIn("enter", kinds)

    def test_wake_context_is_room_scoped(self):
        # 信息通道封堵：别的房间的对话绝不进猫的注入
        world.move(world.USER_ID, "living_room")
        world.act(world.USER_ID, "living_room", speech="这句话猫不该知道")
        self.replies = [{}]
        pet_engine.wake(self.pid, ["你饿了——去猫房吃口猫粮"])
        self.assertIn("你饿了", self.prompts[-1])
        self.assertNotIn("这句话猫不该知道", self.prompts[-1])
        self.assertIn("你说不出人话", self.prompts[-1])     # 协议无说话字段的纪律在场


class TestScoop(PetBase):
    def test_scoop_requires_actor_in_cat_room(self):
        self.force_state(litter=3)
        with self.assertRaises(pet_engine.PetNotHere):   # c1 在自己房间
            pet_engine.scoop(self.pid, self.c1)
        world.move(self.c1, self.cat_room)
        r = pet_engine.scoop(self.pid, self.c1)
        self.assertEqual(r["state"]["litter"], 1)
        ev = world.read_events(self.cat_room)[-1]
        self.assertEqual(ev.get("kind"), "pet_scoop")
        self.assertIn("铲了猫砂盆", ev["text"])

    def test_scoop_does_not_need_cat_present(self):
        world.move(self.pid, "living_room")              # 猫不在，盆还在
        world.move(self.c1, self.cat_room)
        self.assertEqual(pet_engine.scoop(self.pid, self.c1)["state"]["litter"], 1)


class TestRoutesP3(PetBase):
    """/pets/* 路由：UI 和 pet MCP 共用的那套（TestClient 不跑 lifespan，无 worker）。"""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import app as app_module
        cls.client = TestClient(app_module.app)
        cls.h = {"X-Auth": config.AUTH_KEY}

    def test_state_dash_alias_and_presence(self):
        r = self.client.get("/pets/-", headers=self.h).json()
        self.assertEqual((r["id"], r["name"]), (self.pid, "团团"))
        self.assertEqual(r["location"], self.cat_room)
        self.assertEqual(self.client.get("/pets/nope", headers=self.h).status_code, 404)
        # 角色查看要求同地点（用户是玩家面，不带 actor 随时可看）
        r = self.client.get(f"/pets/-?actor={self.c1}", headers=self.h)
        self.assertEqual(r.status_code, 409)
        world.move(self.c1, self.cat_room)
        self.assertEqual(self.client.get(f"/pets/-?actor={self.c1}",
                                         headers=self.h).status_code, 200)

    def test_interact_route(self):
        world.move("user", self.cat_room)
        self.replies = [{"reply": "*翻了个身* 喵", "log": "被喂了"}]
        r = self.client.post("/pets/-/interact", json={"feed": "罐罐"}, headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertIn("喵", r.json()["reply"])
        r = self.client.post("/pets/-/interact", json={"feed": "猫粮"}, headers=self.h)
        self.assertEqual(r.status_code, 422)             # 猫粮是自助的，不在投喂清单
        r = self.client.post("/pets/-/interact", json={}, headers=self.h)
        self.assertEqual(r.status_code, 422)             # 投喂或互动至少一样

    def test_scoop_route_and_room_pets(self):
        r = self.client.post("/pets/-/scoop", json={}, headers=self.h)
        self.assertEqual(r.status_code, 409)             # 用户不在猫房
        world.move("user", self.cat_room)
        r = self.client.post("/pets/-/scoop", json={}, headers=self.h)
        self.assertEqual(r.status_code, 200)
        room = self.client.get(f"/rooms/{self.cat_room}", headers=self.h).json()
        self.assertIn(self.pid, room["pets"])            # 照顾入口的开关


class TestMount(PetBase):
    def test_pet_mcp_mounts_only_with_pets_and_enabled(self):
        import pipeline
        import pets as pets_mod
        orig = config.COHABIT_ENABLED
        try:
            config.COHABIT_ENABLED = True
            self.assertTrue(pipeline._pet_mcp_mounted())
            cfg = json.loads(pipeline._pet_mcp_config("default").read_text("utf-8"))
            srv = cfg["mcpServers"]["pets"]
            self.assertTrue(srv["args"][0].endswith("pet_mcp.py"))
            self.assertEqual(srv["env"]["CASSETTE_CHAR_ID"], "default")
            config.COHABIT_ENABLED = False
            self.assertFalse(pipeline._pet_mcp_mounted())   # 开关关着不挂
            config.COHABIT_ENABLED = True
            pets_dir = pets_mod.PETS_DIR
            pets_mod.PETS_DIR = self.tmp / "no_pets"
            try:
                self.assertFalse(pipeline._pet_mcp_mounted())   # 没猫不挂，菜单也不提
            finally:
                pets_mod.PETS_DIR = pets_dir
        finally:
            config.COHABIT_ENABLED = orig


class TestEnforce(PetBase):
    def test_force_eat_walks_to_cat_room(self):
        world.move(self.pid, "living_room")
        self.force_state(satiety=5.0)
        self.assertEqual(pet_engine.enforce(self.pid), "force_eat")
        self.assertEqual(world.location_of(self.pid), self.cat_room)
        s = pet_store.read_state(self.pid)
        self.assertGreater(s["satiety"], 50)
        evs = world.read_events(self.cat_room)
        self.assertTrue(any("猫粮" in e["text"] for e in evs))   # 行为是真实事件

    def test_force_poop(self):
        self.force_state(poop_pressure=96.0, litter=1)
        self.assertEqual(pet_engine.enforce(self.pid), "force_poop")
        s = pet_store.read_state(self.pid)
        self.assertEqual(s["litter"], 2)
        self.assertLess(s["poop_pressure"], 5)

    def test_no_intervention_when_fine(self):
        self.assertIsNone(pet_engine.enforce(self.pid))


if __name__ == "__main__":
    unittest.main()
