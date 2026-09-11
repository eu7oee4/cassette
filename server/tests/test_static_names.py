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


class BasicsMcpTest(unittest.TestCase):
    """基础工具 MCP（now/fetch）：无条件挂 + SSRF 闸。"""

    def test_mounted_unconditionally(self):
        import pipeline
        names = pipeline.mounted_tool_names("chat", "cass")
        for t in pipeline.BASICS_MCP_TOOLS:
            self.assertIn(t, names)
        self.assertIn("mcp__basics__now", pipeline.mounted_tool_names("wake", "cass"))

    def test_fetch_blocks_local_and_tailnet(self):
        """一个能取任意 URL 的工具指回内网就是 SSRF：后端在 :8000、mianmian 在
        :8765、手机在 tailnet（100.64/10）。域名先解析再判，防 DNS 指回本机。"""
        import basics_mcp
        for bad in ("http://localhost:8000/chat", "http://127.0.0.1:8765/",
                    "http://192.168.1.1/", "http://10.0.0.5/",
                    "http://100.64.0.1:8000/"):
            with self.assertRaises(ValueError, msg=bad):
                basics_mcp.fetch(bad)
        for bad in ("file:///etc/passwd", "ftp://x/y", "notaurl"):
            with self.assertRaises(ValueError, msg=bad):
                basics_mcp.fetch(bad)

    def test_fetch_allows_proxy_mapped_range(self):
        """198.18/15 是 IANA benchmarking 段，Python 把它算进 is_private——但这台
        机器的 DNS 代理把公网域名映射进那儿（example.com → 198.18.0.57）。用
        is_private 一刀切会把**所有**站点挡掉，所以按网段点名挡。"""
        import ipaddress
        import basics_mcp
        ip = ipaddress.ip_address("198.18.0.57")
        self.assertTrue(ip.is_private)          # 一刀切的话就是它挡的
        self.assertFalse(any(ip in n for n in basics_mcp._BLOCKED))

    def test_now_reads_the_clock(self):
        import basics_mcp
        import time as _t
        r = basics_mcp.now()
        self.assertAlmostEqual(r["ts"], int(_t.time()), delta=5)
        self.assertRegex(r["stamp"], r"^\d\d-\d\d 周. \d\d:\d\d$")

    def test_html_to_text_drops_scripts(self):
        import basics_mcp
        out = basics_mcp._html_to_text(
            "<html><head><title>t</title></head><body><script>evil()</script>"
            "<p>正文一</p><p>正文二 &amp; 三</p></body></html>")
        self.assertNotIn("evil", out)
        self.assertIn("正文一", out)
        self.assertIn("正文二 & 三", out)
