"""outbox 按设备记 ack（10-13：模拟器抢先 ack，手机五天只收到 Bark）。

跑法（cwd = server/）：
    .venv/bin/python -m unittest tests.test_outbox_devices -v
"""
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import state_store


class OutboxDevicesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = (state_store.OUTBOX_PATH, state_store.OUTBOX_DEVICES_PATH)
        state_store.OUTBOX_PATH = self.tmp / "outbox.json"
        state_store.OUTBOX_DEVICES_PATH = self.tmp / "outbox_devices.json"

    def tearDown(self):
        state_store.OUTBOX_PATH, state_store.OUTBOX_DEVICES_PATH = self._orig
        shutil.rmtree(self.tmp)

    def _add(self, id_, ts=None, **kw):
        state_store.outbox_append({"id": id_, "ts": ts or int(time.time()), "text": id_,
                                   "char_id": "cass", "delivered": False, **kw})

    def _ids(self, device):
        return [x["id"] for x in state_store.outbox_pending(device)]

    def test_one_device_ack_does_not_eat_the_other(self):
        # 事故本身：两台都露过面之后来一条，模拟器 ack 了，手机照样拿得到
        self._ids("sim"), self._ids("phone")
        self._add("a")
        state_store.outbox_ack(["a"], "sim")
        self.assertEqual(self._ids("sim"), [])
        self.assertEqual(self._ids("phone"), ["a"])
        state_store.outbox_ack(["a"], "phone")
        self.assertEqual(self._ids("phone"), [])

    def test_legacy_client_is_its_own_device(self):
        # 旧包不带头 = legacy 一台：新包模拟器 ack 不影响旧包手机
        self._ids(None), self._ids("sim")
        self._add("a")
        state_store.outbox_ack(["a"], "sim")
        self.assertEqual(self._ids(None), ["a"])
        state_store.outbox_ack(["a"], None)
        self.assertEqual(self._ids(""), [])   # 空头同样算 legacy

    def test_new_device_skips_backlog_already_taken(self):
        # 别人领走的旧条目不补给后来才露面的设备（不然整批历史再拉一遍）
        old = int(time.time()) - 3600
        self._add("taken", ts=old)
        self._add("untaken", ts=old)
        state_store.outbox_ack(["taken"], "phone")
        self.assertEqual(self._ids("new-sim"), ["untaken"])

    def test_delivered_flag_kept_for_other_readers(self):
        self._add("a")
        state_store.outbox_ack(["a"], "phone")
        self.assertTrue(state_store.read_outbox()[0]["delivered"])

    def test_bad_device_header_falls_back_to_legacy(self):
        self.assertEqual(state_store._norm_device("../../etc"), state_store.LEGACY_DEVICE)
        self.assertEqual(state_store._norm_device("ABC-123"), "ABC-123")


if __name__ == "__main__":
    unittest.main()
