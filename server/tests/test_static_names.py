"""undefined-name 静态扫描（2026-08-30 task NameError 复盘的规避规则机械化）。

事故：任务转述删除（015f699）删了赋值、漏了 _game_story_start_sdk 里的回显
引用——运行时语言删东西的单位是**引用链**不是定义处，而这条路由没有任何测试
覆盖、部署又是「生效等重启」，坏代码带着定时引信入了库。这类错误静态就能查
（pyflakes 的 undefined name），所以把「删完 grep 一遍」升级成常驻测试。

pyflakes 没装就 skip（装法在 skip 信息里）；只认 undefined name 一类，
风格项（unused import 之类）不归这管。
"""
import subprocess
import sys
import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent


class UndefinedNameScanTest(unittest.TestCase):
    def test_no_undefined_names(self):
        try:
            import pyflakes  # noqa: F401
        except ImportError:
            self.skipTest("pyflakes 没装：.venv/bin/python -m pip install pyflakes")
        files = ([str(p) for p in SERVER.glob("*.py")]
                 + [str(p) for p in (SERVER / "tools").glob("*.py")])
        r = subprocess.run([sys.executable, "-m", "pyflakes", *files],
                           capture_output=True, text=True, timeout=120)
        bad = [l for l in r.stdout.splitlines() if "undefined name" in l]
        self.assertEqual(bad, [], "undefined name（task NameError 同类）：\n"
                         + "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
