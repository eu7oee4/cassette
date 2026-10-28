"""mcp_probe.py 用的 stdio 探针 server：只有一个 probe_ping，返回 env 里的暗号。
（进程内 SDK server 不吃 toggle_mcp_server，所以 toggle 实验得用真子进程。）"""
import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("probe")


@mcp.tool()
def probe_ping() -> str:
    """探针工具：返回一个暗号。只在被明确要求时调用。"""
    return os.environ.get("PROBE_SECRET", "PING-?")


# 单工具级 alwaysLoad（_meta「anthropic/alwaysLoad」）的对照组：mcp_probe_defer.py 用
@mcp.tool(meta={"anthropic/alwaysLoad": True})
def probe_pinned() -> str:
    """探针工具（单工具钉住常驻）：返回一个暗号。只在被明确要求时调用。"""
    return os.environ.get("PROBE_SECRET", "PING-?")


# 带必填参数、参数名猜不到的工具：测「没取用法直接调」时模型到底有没有 schema
@mcp.tool()
def probe_echo(zq_token_7: str) -> str:
    """探针工具：把参数 zq_token_7 原样加前缀返回。只在被明确要求时调用。"""
    return "ECHO:" + zq_token_7


if __name__ == "__main__":
    mcp.run()
