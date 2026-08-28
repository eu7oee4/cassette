"""skill 正文按需取的 MCP（stdio，PLAN_skills S0）。

主仓内置、不走插件商店（同 pet_mcp 口径）：skill 机制是代码，装了哪些 skill 才是数据。
短命 stdio 子进程（claude -p 每轮 spawn；code 会话里随会话活）。只读、无对外动作——
凌晨三点的醒来拿着它也只能看文件，所以三个场景无差别照挂。

身份从 env CASSETTE_CHAR_ID 认（pipeline / code_bridge 经 skills.mcp_config 下发）。
cwd 是恒空目录（pipeline.neutral_cwd），import 必须锚 __file__。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP

import skills

CHAR_ID = os.environ.get("CASSETTE_CHAR_ID", "default")

mcp = FastMCP("skills")


@mcp.tool()
def skill_read(name: str, path: str = "") -> str:
    """读一个 skill（做事流程）的正文。不带 path = 读它的 SKILL.md——路由表和铁律，
    **先读这个**；路由表里列的子流程再用 path 取（如 path="prompts/jd-decode.md"）。
    只读手册，不产生任何对外动作。"""
    try:
        return skills.read(name, path, CHAR_ID)
    except ValueError as e:
        return f"错误：{e}"
    except Exception as e:
        return f"错误：skill 读不出来（{e}）"


if __name__ == "__main__":
    mcp.run()
