"""宠物照料 MCP（stdio，PLAN_pet P3）——给聊天/醒来的 claude 用的四件套。

跟插件一样是短命 stdio 子进程（claude -p 每轮 spawn），但它是主仓内置：宠物系统
是代码、谁家有猫才是数据（挂载条件在 pipeline._pet_mcp_mounted：没猫整个不挂）。
工具内部走 HTTP 调本机后端 /pets/* 端点（同一台机、带 X-Auth）——逻辑只有一份，
app 的照顾面板和这里共用。身份从 env CASSETTE_CHAR_ID 认（pipeline 下发，口径同
plugins.mounted）；鉴权 key 从继承环境读（后端 .env 已装载）。
"""
import json
import os
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

BACKEND_URL = os.environ.get("CASSETTE_BACKEND_URL", "http://127.0.0.1:8000")
AUTH_KEY = os.environ.get("CASSETTE_AUTH_KEY", "")
CHAR_ID = os.environ.get("CASSETTE_CHAR_ID", "default")
TIMEOUT = 90   # interact 要等猫引擎（DeepSeek），给足

mcp = FastMCP("pets")

# 禁用代理：macOS 的系统代理常把 localhost 也转发出去导致连不上（mianmian 实锤坑）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _req(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BACKEND_URL + path, data=data, method=method)
    req.add_header("X-Auth", AUTH_KEY)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def pet_state(pet: str = "-") -> dict:
    """看猫现在的状态：饱腹/水分/精力/心情(0-100)、猫砂盆(1干净/2有屎/3满了)、
    睡醒、它当下的需求、养到第几天。前提：**它就在你身边**（同一个房间才看得清，
    不同屋会 409）。pet 默认 "-" = 家里唯一那只。"""
    return _req("GET", f"/pets/{pet}?actor={CHAR_ID}")


@mcp.tool()
def pet_feed(food: str, pet: str = "-") -> dict:
    """喂它。food 三选一：罐罐 / 猫条 / 冻干（猫粮和水它自己在猫房有自助的，
    不用你操心，但它挑食、爱吃你手里的）。前提：同一个房间。
    喂食会自动落一条房间系统提示（「你 给它喂了罐罐」，在场的人都看得见）——
    **别再在 MOTION/SAY 里重复描述喂它这个动作**。它的反应直接在返回里。"""
    return _req("POST", f"/pets/{pet}/interact",
                {"actor": CHAR_ID, "feed": food})


@mcp.tool()
def pet_interact(text: str, pet: str = "-") -> dict:
    """跟它互动：你对它做了什么，用一句第三人称白描（例「挠了挠团团的下巴」
    「把逗猫棒在它眼前晃了晃」「让它去找眠眠」）。前提：同一个房间。
    这句白描会**替你落进房间事件**（在场的人都看得见）——别再在 MOTION/SAY 里
    重复。它的反应在返回里；它是猫：听不懂复杂的话，只对语气和熟词有反应，
    理不理你它自己定。"""
    return _req("POST", f"/pets/{pet}/interact",
                {"actor": CHAR_ID, "text": text})


@mcp.tool()
def pet_scoop(pet: str = "-") -> dict:
    """铲猫砂盆（置回干净）。前提：**你人得在猫房**——盆在那儿，不在会 409，
    先 MOVE 过去。不要求猫在场（铲的是盆不是猫）。会落系统提示，别重复描述。"""
    return _req("POST", f"/pets/{pet}/scoop", {"actor": CHAR_ID})


if __name__ == "__main__":
    mcp.run()
