"""PR 工具的 MCP（stdio，PLAN_prs §3）。真身在 prs.py，这里只是壳。

主仓内置、不走插件商店（同 skills_mcp / pet_mcp 口径）。身份从 env CASSETTE_CHAR_ID 认
（pipeline 下发）；cwd 是恒空目录，import 必须锚 __file__。
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP

import prs

CHAR_ID = os.environ.get("CASSETTE_CHAR_ID", "default")

# 派活（PLAN_delegate §2）：这个壳是 stdio 子进程，session 不能在它里面跑——pr_delegate 三件只做
# 转发，runner 在后端进程里（delegate.py）。接线同 game-story / pet_mcp：后端的 .env 一路继承下来。
BACKEND_URL = (os.environ.get("CASSETTE_BACKEND_URL") or "http://127.0.0.1:8000").rstrip("/")
AUTH_KEY = os.environ.get("CASSETTE_AUTH_KEY", "")
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 显式空代理（全仓同坑）

mcp = FastMCP("prs")


def _rest(path: str, body: dict) -> dict | str:
    req = urllib.request.Request(BACKEND_URL + path, data=json.dumps(body).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Auth", AUTH_KEY)
    try:
        with _OPENER.open(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        try:
            d = json.loads(detail).get("detail", detail)
            detail = d.get("error", d) if isinstance(d, dict) else d
        except Exception:
            pass
        return f"错误：{detail}"
    except Exception as e:
        return f"错误：够不着后端（{e}）"


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
    """看所有 PR（谁提的、标题、状态）。没办完的在前。合并了的带 effective：
    true=已经在正在跑的代码里生效，false=合了还没生效（等重启）。
    末尾附你派出去的活（kind=delegate：跑着 / 等你回答 asking（带 question）/ 提了 pr_id / 失败）。"""
    def _both():
        import delegate
        today = prs._now()[:10]
        extra = [r for r in delegate.rows(CHAR_ID)
                 if r["status"] in delegate.ACTIVE or (r.get("ended") or "")[:10] == today]
        return prs.list_prs() + extra
    return _call(_both)


@mcp.tool()
def pr_delegate(slug: str, model: str, brief: str) -> dict | str:
    """把一件改代码的活派出去：挑一个模型、写一份说明，它在自己的工作副本里改完、跑测试、提 PR。
    不占你的轮，派完就能接着干别的；做完 / 停下来问 / 失败都会出现在你下一轮开头（〔PR：…〕）。
    slug 同 pr_start 的规则（小写字母数字连字符）。
    model 怎么挑：haiku=改文案 / 常量 / 单文件小修；sonnet=大多数 bug 修复和小功能，默认它；
    opus=跨模块、要读很多代码、要做设计判断的；fable（claude-fable-5-1）=复杂的、会改很多文件的，
    以及机主点名「让 fable 做」的时候。
    brief 写清四件事：要解决什么（现象或目标）、你已经查到的位置（文件:行）、哪些不能动、怎么算做完。
    对方看不到你们的聊天，说明就是它知道的全部。它拿不准会停下来问——问题一来你就会醒过来看到。"""
    return _rest("/prs/delegate", {"char": CHAR_ID, "slug": slug, "model": model, "brief": brief})


@mcp.tool()
def pr_delegate_reply(slug: str, answer: str) -> dict | str:
    """派出去的活停下来问了你（pr_list 里 status=asking 的那条）：把回答递回去，它从停下的地方接着做。
    把它列的问题逐条答；答不了的明说「这条你定」，别留空让它猜。要机主拍板的，先发消息问机主，
    机主回了再回它。一个派活最多来回 3 次。"""
    return _rest("/prs/delegate/reply", {"char": CHAR_ID, "slug": slug, "answer": answer})


@mcp.tool()
def pr_delegate_cancel(slug: str, reason: str = "") -> dict | str:
    """撤回一个派出去的活（跑着的掐掉、等回答的收掉）：副本拆掉、分支删掉，slug 可以再用。"""
    return _rest("/prs/delegate/cancel", {"char": CHAR_ID, "slug": slug, "reason": reason})


@mcp.tool()
def pr_view(id: int) -> dict | str:
    """看一个 PR 的全部：描述、状态、改了哪些文件、测试结果、完整 diff；合并了的
    带 effective（生没生效，当场算的）。机主问起「讲讲 #N」时用它查。"""
    return _call(prs.view, id)


if __name__ == "__main__":
    mcp.run()
