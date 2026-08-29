"""world.py（同居世界 C0）单测。

全部跑在临时目录上——world 的三个路径在 setUp 里换掉、tearDown 换回来，
绝不写真 state/（§5 测试纪律：别污染 Cass 和 TA 的活系统）。
characters/settings 是只读依赖（实体列表、称呼），读真的没关系。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_world -v
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import characters
import config
import pets
import world


class WorldBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="world_test_"))
        self._orig = (world.ROOMS_DIR, world.REGISTRY_PATH, world.WORLD_PATH,
                      world.HOUSE_SETTINGS_PATH)
        world.ROOMS_DIR = self.tmp / "rooms"
        world.REGISTRY_PATH = world.ROOMS_DIR / "registry.json"
        world.WORLD_PATH = self.tmp / "world.json"
        # 总开关也必须指临时区：漏掉它=读真 state 的 house_settings.json，机主把真
        # 小屋闸拨关的那天整片 pet/world 用例齐挂（2026-08-29 实翻过车，别再漏）
        world.HOUSE_SETTINGS_PATH = self.tmp / "house_settings.json"
        # 角色状态目录也指临时区：append_event 现在顺手写经历流（experience.jsonl），
        # 不改这个真 state 会被测试事件污染。
        import state_store
        self._csr_orig = state_store.CHAR_STATE_ROOT
        state_store.CHAR_STATE_ROOT = self.tmp / "chars"
        # 宠物注册表也指临时区（默认空目录 = 没有宠物，存量用例行为不变）
        self._pets_orig = pets.PETS_DIR
        pets.PETS_DIR = self.tmp / "pets"
        world.ensure_world()
        self.chars = characters.ids()
        self.c1 = self.chars[0]                    # 真实注册表里的第一个角色
        self.c1_room = f"{self.c1}_room"

    def tearDown(self):
        import state_store
        (world.ROOMS_DIR, world.REGISTRY_PATH, world.WORLD_PATH,
         world.HOUSE_SETTINGS_PATH) = self._orig
        state_store.CHAR_STATE_ROOT = self._csr_orig
        pets.PETS_DIR = self._pets_orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register_pet(self, pid="tuantuan", name="团团"):
        d = pets.PETS_DIR / pid
        d.mkdir(parents=True, exist_ok=True)
        (d / "pet.json").write_text(json.dumps({"display_name": name},
                                               ensure_ascii=False), "utf-8")
        world.ensure_world()   # 猫房补种
        return pid

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

    def test_away(self):
        r = world.move(world.USER_ID, world.AWAY)
        self.assertTrue(r["ok"])
        self.assertEqual(world.location_of(world.USER_ID), world.AWAY)
        self.assertEqual(world.read_events("mm_room")[-1]["kind"], "leave")
        self.assertNotIn(world.USER_ID, world.occupants("mm_room"))
        # 回家：从 away 直接进房间，正常过门禁、落 enter
        self.assertTrue(world.move(world.USER_ID, "living_room")["ok"])
        self.assertEqual(world.read_events("living_room")[-1]["kind"], "enter")
        # 走廊已摘除（2026-08-17）：当成普通的不认识的房间
        with self.assertRaises(KeyError):
            world.move(world.USER_ID, "hallway")

    def test_no_bedroom_defaults_to_living_room(self):
        # 没有自己卧室的实体（比如注册表被手编掉了卧室）：默认落客厅，不站走廊
        reg = world.load_registry()
        del reg[self.c1_room]
        world._write_json(world.REGISTRY_PATH, reg)
        world.WORLD_PATH.unlink()          # 清位置记录，逼出默认值
        world.ensure_world()
        self.assertEqual(world.location_of(self.c1), "living_room")

    def test_move_unknown_room(self):
        with self.assertRaises(KeyError):
            world.move(world.USER_ID, "basement_lab")

    def test_carry_moves_both_with_single_event(self):
        world.move("user", self.c1_room)
        self._reset_events()
        mv = world.move(self.c1, "living_room", carry="user")
        self.assertEqual(mv["carry"], "user")
        self.assertEqual(world.location_of(self.c1), "living_room")
        self.assertEqual(world.location_of("user"), "living_room")
        leave = world.read_events(self.c1_room)[-1]
        enter = world.read_events("living_room")[-1]
        self.assertIn("抱着", leave["text"])
        self.assertEqual(enter["with"], ["user"])
        # 被抱者的在场区间从「抱着进来」那条起：看得见随后发生的事
        world.act(self.c1, "living_room", speech="到啦")
        vis = world.visible_events("living_room", "user")
        self.assertEqual(vis[0]["kind"], "enter")
        self.assertIn("到啦", vis[-1]["text"])

    def _reset_events(self):
        pass   # 事件文件按测试目录隔离，无需真清；占位保语义

    def test_carry_validations(self):
        with self.assertRaises(ValueError):
            world.move(self.c1, "living_room", carry=self.c1)      # 抱自己
        with self.assertRaises(ValueError):
            world.move(self.c1, "living_room", carry="user")       # 不同屋
        # 门锁着：谁都没动
        world.move("user", self.c1_room)
        self._set_lock("wash_room", 1)
        mv = world.move(self.c1, "wash_room", carry="user")
        self.assertFalse(mv["ok"])
        self.assertEqual(world.location_of(self.c1), self.c1_room)
        self.assertEqual(world.location_of("user"), self.c1_room)


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
        # 通知三操作统一格式：「谁「那段文字」」（2026-08-16 机主拍板）
        r = world.state_change(world.USER_ID, "mm_room", "add", text="桌上剩了半杯牛奶")
        eid = r["entry"]["id"]
        self.assertEqual(r["entry"]["author"], world.USER_ID)
        self.assertEqual(r["event"]["kind"], "state")
        self.assertIn("「桌上剩了半杯牛奶」", r["event"]["text"])
        self.assertNotIn("添加", r["event"]["text"])

        world.move(self.c1, "living_room")
        world.move(self.c1, "mm_room")   # 角色也能改，前提是在场
        r = world.state_change(self.c1, "mm_room", "edit", entry_id=eid, text="牛奶只剩杯底了")
        self.assertEqual(r["entry"]["author"], self.c1)   # author = 现在这句话是谁写的
        self.assertIn("「牛奶只剩杯底了」", r["event"]["text"])

        # remove 带叙事：快照删条目，叙事进事件流（同一格式）
        r = world.state_change(self.c1, "mm_room", "remove", entry_id=eid,
                               text="把凉透的牛奶端走倒了")
        self.assertEqual(world.room_state("mm_room"), [])   # 快照只留结果
        self.assertIn("「把凉透的牛奶端走倒了」", r["event"]["text"])
        self.assertNotIn("清掉", r["event"]["text"])

        # remove 不带叙事＝静默清理（2026-08-17 机主拍板）：快照删掉，不落事件、不惊动在场者
        r = world.state_change(world.USER_ID, "mm_room", "add", text="窗台上一只纸飞机")
        n_before = len(world.read_events("mm_room"))
        r = world.state_change(world.USER_ID, "mm_room", "remove", entry_id=r["entry"]["id"])
        self.assertIsNone(r["event"])
        self.assertEqual(world.room_state("mm_room"), [])
        self.assertEqual(len(world.read_events("mm_room")), n_before)

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


class TestStripQuotes(unittest.TestCase):
    """说话只存话本身（2026-08-19：小卡说了句「「「…」」」，引号越滚越多）。"""

    def test_peels_every_layer(self):
        self.assertEqual(world.strip_quotes("「「「今天够了。」」」"), "今天够了。")
        self.assertEqual(world.strip_quotes("「在忙吗」"), "在忙吗")
        self.assertEqual(world.strip_quotes(' "好啊" '), "好啊")
        self.assertEqual(world.strip_quotes("『试试』"), "试试")

    def test_keeps_quotes_inside(self):
        # 整句没被裹住：首引号在中间就闭了，剥了会把「和」吃掉
        self.assertEqual(world.strip_quotes("「甲」和「乙」"), "「甲」和「乙」")
        # 裹住但里面还有引号：只剥外面那层，里面的是内容
        self.assertEqual(world.strip_quotes("「他说「不」」"), "他说「不」")
        self.assertEqual(world.strip_quotes("没有引号"), "没有引号")
        self.assertEqual(world.strip_quotes(""), "")

    def test_speech_events_store_bare_text(self):
        segs = world.split_mixed("*站起来* 「我不走」")
        self.assertEqual(segs, [("action", "站起来"), ("speech", "我不走")])


class TestDeleteTurn(WorldBase):
    """机主的橡皮擦（2026-08-19）：整轮删，进出场留骨架，经历流同删。"""

    def _turn_in_room(self):
        world.move(self.c1, "living_room")
        with world.turn():
            world.move("user", "living_room")          # enter（骨架）
            world.act_mixed("user", "living_room", "*坐下* 在忙吗")
        return world.read_events("living_room")

    def test_deletes_whole_turn_but_keeps_presence(self):
        evs = self._turn_in_room()
        speech = next(e for e in evs if e["type"] == "speech")
        res = world.delete_turn("living_room", speech["id"])
        self.assertEqual(res["deleted"], 2)            # 动作 + 说话
        self.assertEqual(res["kept_presence"], 1)      # enter 留着
        left = world.read_events("living_room")
        self.assertEqual([e["kind"] for e in left if e.get("kind")], ["enter", "enter"])
        self.assertNotIn("在忙吗", "".join(e["text"] for e in left))
        # 可见区间的起点还在 → AI 不会因为删记录反而看见更早的历史
        vis = world.visible_events("living_room", world.USER_ID)
        self.assertEqual(vis[0]["kind"], "enter")

    def test_experience_stream_deleted_too(self):
        evs = self._turn_in_room()
        speech = next(e for e in evs if e["type"] == "speech")
        self.assertIn("在忙吗", [e["text"] for e in world.read_experience(self.c1)])
        world.delete_turn("living_room", speech["id"])
        self.assertNotIn("在忙吗", [e["text"] for e in world.read_experience(self.c1)])

    def test_untagged_event_deletes_alone(self):
        world.move("user", "living_room")
        a = world.act("user", "living_room", speech="第一句")
        world.act("user", "living_room", speech="第二句")
        world.delete_turn("living_room", a["events"][0]["id"])
        texts = [e["text"] for e in world.read_events("living_room")]
        self.assertNotIn("第一句", texts)
        self.assertIn("第二句", texts)      # 没有 turn 的老事件只删它自己

    def test_deleted_lines_are_kept_on_disk(self):
        evs = self._turn_in_room()
        world.delete_turn("living_room", next(e for e in evs if e["type"] == "speech")["id"])
        trash = (world.ROOMS_DIR / "living_room" / "events.deleted.jsonl").read_text("utf-8")
        self.assertIn("在忙吗", trash)

    def test_unknown_id_is_keyerror(self):
        world.move("user", "living_room")
        with self.assertRaises(KeyError):
            world.delete_turn("living_room", "deadbeef")


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

    def test_last_interval_readback_after_leaving(self):
        # 离场后可回看自己最后一段在场区间（2026-08-17 机主）：亲历过的不因离开而
        # 蒸发；走之后发生的照旧看不见——补看上一屋不再要上帝视角。
        world.move(world.USER_ID, "living_room")
        world.move(self.c1, "living_room")
        world.act(self.c1, "living_room", speech="你在的时候说的")
        world.move(world.USER_ID, "mm_room")
        world.act(self.c1, "living_room", speech="你走之后说的")
        vis = world.visible_events("living_room", world.USER_ID)
        texts = "".join(e["text"] for e in vis)
        self.assertEqual(vis[0]["kind"], "enter")      # 区间从自己进屋那条起
        self.assertEqual(vis[-1]["kind"], "leave")     # 到自己离开那条为止
        self.assertIn("你在的时候说的", texts)
        self.assertNotIn("你走之后说的", texts)

    def test_carried_out_keeps_witnessed_events(self):
        # 被抱走的人：自己在 leave 事件的 with 里也算自己的离场，回看同一口径
        world.move(world.USER_ID, self.c1_room)
        world.act(self.c1, self.c1_room, speech="马上带你去个地方")
        world.move(self.c1, "living_room", carry=world.USER_ID)
        vis = world.visible_events(self.c1_room, world.USER_ID)
        self.assertIn("马上带你去个地方", "".join(e["text"] for e in vis))
        self.assertEqual(vis[-1]["kind"], "leave")
        # 新房间从「抱着进来」那条起照常可见（在场路径不变）
        self.assertEqual(world.visible_events("living_room", world.USER_ID)[0]["kind"],
                         "enter")


class TestPets(WorldBase):
    """PLAN_pet P0：pet 是第三类实体——有位置、可被抱、猫洞豁免、没有经历流。"""

    def test_pet_entity_room_and_default_location(self):
        pid = self._register_pet()
        self.assertIn(pid, world.entity_ids())
        self.assertEqual(world.entity_name(pid), "团团")
        r = world.room(f"{pid}_room")
        self.assertEqual((r["name"], r["owner"]), ("猫房", pid))
        self.assertEqual(world.location_of(pid), f"{pid}_room")   # 默认待在猫房

    def test_room_seeding_is_additive_only(self):
        pid = self._register_pet()
        reg = world.load_registry()
        reg[f"{pid}_room"]["name"] = "团团的小窝"     # 机主手编
        reg[f"{pid}_room"]["floor"] = 1
        world._write_json(world.REGISTRY_PATH, reg)
        world.ensure_world()                          # 再跑绝不覆盖
        r = world.room(f"{pid}_room")
        self.assertEqual((r["name"], r["floor"]), ("团团的小窝", 1))

    def test_cat_flap_ignores_lock(self):
        pid = self._register_pet()
        self._set_lock(self.c1_room, 1)
        self.assertTrue(world.move(pid, self.c1_room)["ok"])      # 锁挡人不挡猫
        self.assertFalse(world.move(world.USER_ID, self.c1_room)["ok"])   # 人照样吃闭门羹

    def test_user_carries_pet(self):
        pid = self._register_pet()
        world.move(pid, "mm_room")
        mv = world.move(world.USER_ID, "living_room", carry=pid)
        self.assertEqual(mv["carry"], pid)
        self.assertEqual(world.location_of(pid), "living_room")
        self.assertEqual(world.read_events("living_room")[-1].get("with"), [pid])

    def test_pet_has_no_experience_stream(self):
        # 「猫的上下文只含当前房间」的结构性封堵：事件永远不写进宠物的经历流
        import state_store
        pid = self._register_pet()
        world.move(pid, "living_room")
        world.move(self.c1, "living_room")
        world.act(self.c1, "living_room", speech="猫不该记住这句")
        streams = [p.parent.name for p in
                   Path(state_store.CHAR_STATE_ROOT).rglob("experience.jsonl")]
        self.assertIn(self.c1, streams)               # 角色照常写
        self.assertNotIn(pid, streams)                # 宠物一条不写


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

    def test_delete_turn_via_http(self):
        self.client.post("/world/move", json={"to": "living_room"}, headers=self.h)
        self.client.post("/rooms/living_room/act", json={"text": "*挥手* 删我试试"},
                         headers=self.h)
        evs = self.client.get("/rooms/living_room/events", headers=self.h).json()["events"]
        speech = next(e for e in evs if e["type"] == "speech")
        r = self.client.post("/rooms/living_room/events/delete",
                             json={"id": speech["id"]}, headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["deleted"], 2)      # 动作 + 说话（一次发送 = 一轮）
        left = self.client.get("/rooms/living_room/events", headers=self.h).json()["events"]
        self.assertEqual([e["type"] for e in left], ["system"])   # 进出场留着
        # 不认识的 id → 404
        r = self.client.post("/rooms/living_room/events/delete",
                             json={"id": "nope"}, headers=self.h)
        self.assertEqual(r.status_code, 404)

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
