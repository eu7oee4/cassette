"""PR 工具的 MCP（stdio，PLAN_prs §3）。真身在 prs.py，这里只是壳。

主仓内置、不走插件商店（同 skills_mcp / pet_mcp 口径）。身份从 env CASSETTE_CHAR_ID 认
（pipeline 下发）；cwd 是恒空目录，import 必须锚 __file__。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP

import prs

CHAR_ID = os.environ.get("CASSETTE_CHAR_ID", "default")

mcp = FastMCP("prs")


def _call(fn, *args):
    try:
        return fn(*args)
    except prs.PRError as e:
        return f"错误：{e}"
    except Exception as e:
        return f"错误：PR 工具出了岔子（{type(e).__name__}: {e}）"


@mcp.tool()
def pr_start(slug: str, discard: bool = False) -> dict | str:
    """开始（或接着）改一个 PR：把你自己的工作副本切到这条 PR 分支上，返回工作副本路径。
    之后用 Read/Edit/Write 在这个路径下改，改的是副本，碰不到正在跑的代码。
    slug 是这次改动的短名（小写字母、数字、连字符，如 fix-wake-log）：新名字 = 从最新 main
    开一条新分支；你已经开着的 PR 的名字 = 切回去接着改（main 动过会帮你重放）。
    工作副本里有没提交的改动时切不走：要留就先 pr_open，不要了就带 discard=true。"""
    return _call(prs.start, CHAR_ID, slug, discard)


@mcp.tool()
def pr_test() -> dict | str:
    """在你的工作副本里跑一遍后端测试，回：跑了几条、新挂了哪些。跑的是副本当前的样子
    （没提交的也算）。干净检出里本来就挂的那批不算你的。"""
    return _call(prs.test, CHAR_ID)


@mcp.tool()
def pr_open(title: str, description: str) -> dict | str:
    """把工作副本里的改动提成 PR（会自动提交并跑一遍测试）。同一条分支再调 = 更新同一个 PR。
    合不合并、什么时候生效，由机主决定。
    description 必填，写给不读代码的人看：做了什么 / 为什么 / 合并后机主会感觉到什么变化；
    技术细节放在后面或者不放。"""
    return _call(prs.open_pr, CHAR_ID, title, description)


@mcp.tool()
def pr_close(id: int, reason: str) -> dict | str:
    """撤回你自己提的一个 PR（写一句为什么）。别人的关不了。"""
    return _call(prs.close, CHAR_ID, id, reason)


@mcp.tool()
def pr_list() -> list | str:
    """看所有 PR（谁提的、标题、状态）。没办完的在前。"""
    return _call(prs.list_prs)


@mcp.tool()
def pr_view(id: int) -> dict | str:
    """看一个 PR 的全部：描述、状态、改了哪些文件、测试结果、完整 diff。
    机主问起「讲讲 #N」时用它查。"""
    return _call(prs.view, id)


if __name__ == "__main__":
    mcp.run()
