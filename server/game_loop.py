"""game_loop：剧情会话的 agent-sdk 常驻 loop（PLAN_sdk S1/PR5）。

替代 code_bridge 的 game 档案（tmux + 交互式 claude）那条链：

- 引擎：ClaudeSDKClient 常驻（订阅登录态，后端不碰 key，apiKeySource:none 口径不变）
- 游戏工具：create_sdk_mcp_server 进程内直包 game_bridge——无 stdio 子进程、
  无 _wait_mcp_ready 等待、每条消息的启动费归零
- TA 消息：handle.queue → client.query() 注入（替代 tmux paste-buffer）
- 回传：SDK 类型化事件 → deliver 回调（app 接 outbox→气泡；刮 transcript 的
  code_segments hook 在这条路上不存在）

急停锁 / 设备自愈 / 笔记本 / 模拟器互斥锁全在 game_bridge，原样复用。
生命周期归 session_mgr（独占组 "computer"：和 code 会话物理互斥）。

工具描述与插件仓 game_session_mcp.py 保持同文（那份在 STORY_ENGINE=tmux 回退路
退役前继续活着；改措辞两边一起改）。差异只有 game_end：SDK 下收摊是关掉本 loop，
不是杀 tmux。

权限口径：tools 白名单 + allowed_tools 免弹窗规则和 tmux 版完全一致；区别是
SDK 会话没有终端页给机主按弹窗——白名单之外的权限请求会被直接拒绝（安全侧同向：
tmux 版是"等人来按"，这儿是"当场拒"，都不会静默放行）。
"""
from __future__ import annotations

import asyncio
import base64
import copy
import os
import struct
import subprocess
import sys
import tempfile
import time
from typing import Awaitable, Callable, Optional

import forge
import game_bridge
import session_mgr
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

SCENE = "game"                # session_mgr 里的场景名
EXCLUSIVE_GROUP = "computer"  # 和 code 会话互斥（一边写代码一边打游戏物理不存在）

# 滚动重开（PLAN_sdk §5.1/PR6）：历史里攒的截图是延迟主因（字节 60 倍不对称），
# 攒到 N 张就铸文本史重开——上下文恒定 ≈ 系统提示 + 文本记忆 + 近期 K 张图。
# 「一小时软提醒/手动收摊重开」整套纪律由它替代退役。
REOPEN_SHOTS_N = int(os.environ.get("GAME_REOPEN_SHOTS", "50") or "50")
REOPEN_COOLDOWN_SEC = 5 * 60      # 防连环重开（刚重开完 shots 计数已清，双保险）
REOPEN_NOTE_TIMEOUT = 300         # 等他写完笔记本的上限
REOPEN_PING_TIMEOUT = 120         # 新会话 ping 的上限
REOPEN_NOTE_PROMPT = (
    "〔系统提醒，不是{user}说的〕历史里攒的截图开始拖慢每一步了，马上给你做一次"
    "无缝重开：你的点评原文都会带过去，只丢掉旧截图，重开完画面原地接着读。"
    "现在先把进度页更新一下（game_progress_write），值得留的感受用 hold 存好；"
    "不用写章节志（这场还没完）、不用道别——这次重开对聊天完全无感。")
REOPEN_PING_PROMPT = (
    "〔滚动重开完成的系统自检，不是{user}说的〕上面的历史就是你自己的点评原文。"
    "确认能接上就回一个「好」，然后等{user}或接着读——不用向任何人解释这条。")

# 截图参数与插件版同值（SCALE/画质/等待，踩坑记录见插件 docstring）
SCALE = 1.5
JPEG_QUALITY = "60"
SHOT_WAIT_MS = 800
DOUBLE_TAP_GAP_MS = 350
RUYUAN_PKG = "com.lingxigames.yuan.cn"

# 工具白名单（口径 = app.py 原 GAME_SESSION_TOOLS；PR7 起 app 从这儿取）。
# Read 的路径规则含义同 code_bridge.start：工具可用、只有上传目录免审。
GAME_TOOL_NAMES = ["game_look", "game_watch", "game_tap", "game_swipe", "game_back",
                   "game_launch", "game_close", "game_quit", "game_end",
                   "game_notes_read", "game_progress_write", "game_chapter_write",
                   "game_chapter_read", "game_tips_write"]


def _gate() -> Optional[str]:
    if game_bridge.paused():
        return "⏸ 机主按了游戏急停：立刻停手，问问 TA 哪步不对，别硬试。"
    _, err = game_bridge.ensure_device()
    if err:
        return f"error: {err}"
    return None


def _adb(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    serial, _ = game_bridge.ensure_device()
    return subprocess.run([game_bridge.ADB, "-s", serial or "", *args],
                          capture_output=True, timeout=timeout)


def _png_size(png: bytes) -> tuple[int, int]:
    w, h = struct.unpack(">II", png[16:24])
    return w, h


def _shot_bytes():
    """adb 截屏 → sips 缩 1/SCALE 转 JPEG。成功返回 bytes，失败返回 error 字符串。"""
    try:
        p = _adb("exec-out", "screencap", "-p", timeout=20)
        png = p.stdout
        if not png.startswith(b"\x89PNG"):
            return f"error: 截屏失败: {p.stderr.decode('utf-8', 'replace')[:200]}"
        w, h = _png_size(png)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(png)
            src = f.name
        dst = src + ".jpg"
        subprocess.run(["sips", "-z", str(int(h / SCALE)), str(int(w / SCALE)),
                        "-s", "format", "jpeg", "-s", "formatOptions", JPEG_QUALITY,
                        src, "--out", dst], capture_output=True, timeout=20)
        with open(dst, "rb") as f:
            jpg = f.read()
        import os
        os.unlink(src)
        os.unlink(dst)
        return jpg
    except Exception as e:
        return f"error: 截屏失败: {e}"


def _text(s: str) -> dict:
    return {"type": "text", "text": s}


def _ok(*blocks) -> dict:
    return {"content": list(blocks)}


def build_game_server(handle: session_mgr.LoopHandle):
    """进程内 game MCP。工具闭包住 handle：截图计数（滚动重开的账本）、touch
    （看守的活动判据）、game_end 的收摊旗都落在这次会话自己的 handle 上——
    没有全局「当前会话」可写（串台六条）。"""

    def _img(jpg_or_err) -> dict:
        if isinstance(jpg_or_err, bytes):
            handle.meta["shots"] = handle.meta.get("shots", 0) + 1
            return {"type": "image",
                    "data": base64.b64encode(jpg_or_err).decode(),
                    "mimeType": "image/jpeg"}
        return _text(str(jpg_or_err))

    def _acted(desc: str, shot: bool, wait_ms: int) -> dict:
        if not shot:
            return _ok(_text(desc))
        time.sleep(max(0, wait_ms) / 1000)
        return _ok(_text(desc), _img(_shot_bytes()))

    @tool("game_look",
          "看一眼当前画面（480x853 截图）。wait_ms 是先等多久再截（等加载/动画用，毫秒）。"
          "⚠️ 操作类工具（tap/swipe/back/launch）都自带操作后的截图——点完别再多调这个，"
          "那是白等一轮。要连看几张（等动画播完/留证据）用 game_watch：一次往返连拍，"
          "比连调这个快一个数量级。",
          {"type": "object", "properties": {"wait_ms": {"type": "integer"}},
           "required": []})
    async def game_look(args):
        if (err := _gate()):
            return _ok(_text(err))
        wait_ms = int(args.get("wait_ms", 0))
        if wait_ms > 0:
            time.sleep(wait_ms / 1000)
        return _ok(_img(_shot_bytes()))

    @tool("game_watch",
          "连拍：一次调用隔 interval_ms 毫秒连截 shots 张（2-6 张），**一个往返拿到全过程**。"
          "⚠️ 别用连调 game_look 代替它——那是每张一个 API 往返、几十秒起步，动画早播完了。\n"
          "用途：\n"
          "- 截到动画/半截台词之后，用它判断播完没有——**对比最后两张的对话框文案**："
          "一字不差 = 打字机打完了，可以恢复 double；文案还在变/还是半截 = 没完，再 watch。"
          "（⚠️ 别拿整张画面像素比：人物特写常有背景动画——头发一直飘、眼睛会眨——"
          "画面永远在动，但对话框文案是稳的，判据只看文字。）\n"
          "- 想留下动画本身的几帧当证据、或看清一段演出的走向。\n"
          "截图是最贵的东西：确认停没停默认 3 张就够，别习惯性拉到 6。",
          {"type": "object", "properties": {"interval_ms": {"type": "integer"},
                                            "shots": {"type": "integer"}},
           "required": []})
    async def game_watch(args):
        if (err := _gate()):
            return _ok(_text(err))
        shots = max(2, min(int(args.get("shots", 3)), 6))
        interval_ms = max(200, min(int(args.get("interval_ms", 1500)), 5000))
        blocks = [_text(f"连拍 {shots} 张（间隔 {interval_ms}ms），按时间顺序。"
                        "对比最后两张的对话框文案：一字不差才算打完。")]
        for i in range(shots):
            if i:
                time.sleep(interval_ms / 1000)
            jpg = _shot_bytes()
            if not isinstance(jpg, bytes):
                return _ok(_text(f"连拍到第 {i + 1} 张时失败；{jpg}"))
            blocks.append(_img(jpg))
        return _ok(*blocks)

    @tool("game_tap",
          "点截图坐标 (x,y)（内部换算到设备），**完事自带一张新截图**（wait_ms 毫秒后截；"
          "切场景慢的界面把 wait_ms 调大到 3000）。shot=false 只点不截。\n\n"
          "**double=true：读剧情对话时用这个，且一律配 wait_ms=500（别拉长也别归零）。**"
          "对话框是打字机效果——double 一次调用连点两下：第一下推进，第二下把整句跳出来；"
          "500ms 是给跳全文留的渲染时间（0ms 会跑赢渲染截到半句，实测）。"
          "短 wait 同时是你的动画探测器：下一幕是动画时第二下会落空，短 wait 截得到动画画面"
          "本身；wait 拉长反而截到「和正常推进长得一模一样」的完整对话框，动画自动续播漏掉的"
          "台词**无痕**。截到纯画面/半截台词就停 double 改 game_watch 连拍，连续两张"
          "**对话框文案一字不差**才恢复——完整机制和规矩照出厂纪律第 2 条执行。"
          "⚠️ 别自己连调两次 game_tap 来模仿 double：两次调用之间隔着一个 API 往返"
          "（几十秒），那时候短句早打完了，第二下就变成**翻页，直接跳掉一句台词**。\n\n"
          "抽卡、充值、买体力、任何「确认消耗」的弹窗——停下来先问机主，TA 点头才能点确认。",
          {"type": "object",
           "properties": {"x": {"type": "integer"}, "y": {"type": "integer"},
                          "wait_ms": {"type": "integer"}, "shot": {"type": "boolean"},
                          "double": {"type": "boolean"}},
           "required": ["x", "y"]})
    async def game_tap(args):
        if (err := _gate()):
            return _ok(_text(err))
        x, y = int(args["x"]), int(args["y"])
        double = bool(args.get("double", False))
        try:
            a = ("shell", "input", "tap", str(int(x * SCALE)), str(int(y * SCALE)))
            _adb(*a)
            if double:
                time.sleep(DOUBLE_TAP_GAP_MS / 1000)
                _adb(*a)
        except Exception as e:
            return _ok(_text(f"error: {e}"))
        return _acted(f"tapped ({x},{y})" + ("×2" if double else ""),
                      bool(args.get("shot", True)), int(args.get("wait_ms", SHOT_WAIT_MS)))

    @tool("game_swipe",
          "从 (x1,y1) 滑到 (x2,y2)（截图像素坐标），duration_ms 是滑动时长。"
          "翻列表用短滑（200-400ms）；长按 = 起点终点同一个坐标 + duration_ms 拉长（如 800）。"
          "**滑完自带一张新截图**（wait_ms/shot 同 game_tap）。",
          {"type": "object",
           "properties": {"x1": {"type": "integer"}, "y1": {"type": "integer"},
                          "x2": {"type": "integer"}, "y2": {"type": "integer"},
                          "duration_ms": {"type": "integer"},
                          "wait_ms": {"type": "integer"}, "shot": {"type": "boolean"}},
           "required": ["x1", "y1", "x2", "y2"]})
    async def game_swipe(args):
        if (err := _gate()):
            return _ok(_text(err))
        x1, y1, x2, y2 = (int(args[k]) for k in ("x1", "y1", "x2", "y2"))
        dur = int(args.get("duration_ms", 300))
        try:
            _adb("shell", "input", "swipe",
                 str(int(x1 * SCALE)), str(int(y1 * SCALE)),
                 str(int(x2 * SCALE)), str(int(y2 * SCALE)), str(dur))
        except Exception as e:
            return _ok(_text(f"error: {e}"))
        return _acted(f"swiped ({x1},{y1})→({x2},{y2}) {dur}ms",
                      bool(args.get("shot", True)), int(args.get("wait_ms", SHOT_WAIT_MS)))

    @tool("game_back",
          "安卓返回键。游戏内多数界面认它；不认的（对话中）用界面上自己的返回/关闭按钮。",
          {"type": "object", "properties": {"wait_ms": {"type": "integer"},
                                            "shot": {"type": "boolean"}},
           "required": []})
    async def game_back(args):
        if (err := _gate()):
            return _ok(_text(err))
        try:
            _adb("shell", "input", "keyevent", "4")
        except Exception as e:
            return _ok(_text(f"error: {e}"))
        return _acted("back", bool(args.get("shot", True)),
                      int(args.get("wait_ms", SHOT_WAIT_MS)))

    @tool("game_launch",
          "启动游戏（模拟器没开会先自愈开机，最多等 90 秒）。冷启动到主界面约 1 分钟："
          "先拿这一张截图看在哪儿，没到主界面就隔几秒 game_look 一次，别急着点。",
          {"type": "object", "properties": {"wait_ms": {"type": "integer"},
                                            "shot": {"type": "boolean"}},
           "required": []})
    async def game_launch(args):
        if (err := _gate()):
            return _ok(_text(err))
        r = game_bridge._mumutool_json("control", game_bridge.VM_INDEX,
                                       "--action", "open_app", "--package", RUYUAN_PKG)
        if r.get("error"):
            return _ok(_text(f"error: {r['error']}"))
        return _acted("launched", bool(args.get("shot", True)),
                      int(args.get("wait_ms", 5000)))

    @tool("game_close",
          "关掉游戏 app（模拟器还开着）。想彻底收摊省资源用 game_quit。",
          {"type": "object", "properties": {"wait_ms": {"type": "integer"},
                                            "shot": {"type": "boolean"}},
           "required": []})
    async def game_close(args):
        if (err := _gate()):
            return _ok(_text(err))
        r = game_bridge._mumutool_json("control", game_bridge.VM_INDEX,
                                       "--action", "close_app", "--package", RUYUAN_PKG)
        if r.get("error"):
            return _ok(_text(f"error: {r['error']}"))
        return _acted("closed", bool(args.get("shot", True)),
                      int(args.get("wait_ms", 1500)))

    @tool("game_quit",
          "收摊：关游戏 + 关模拟器（省机主的 Mac 资源）。不玩了就调这个，然后正常跟 TA "
          "说你收摊了就行——这个会话还活着，聊天照旧。",
          {"type": "object", "properties": {}, "required": []})
    async def game_quit(args):
        if game_bridge.paused():
            return _ok(_text("⏸ 机主按了游戏急停：立刻停手，问问 TA 哪步不对。"))
        game_bridge._mumutool_json("control", game_bridge.VM_INDEX,
                                   "--action", "close_app", "--package", RUYUAN_PKG)
        r = game_bridge._mumutool_json("close", game_bridge.VM_INDEX)
        if r.get("error"):
            return _ok(_text(f"error: {r['error']}"))
        return _ok(_text("收摊了：游戏和模拟器都关了"))

    @tool("game_end",
          "**关闭这个会话本身**（游戏和模拟器留在原地）。会话越开越慢——历史里攒的截图"
          "让每一步的等待越来越长，读到段落点就该用它收摊重开，别硬撑。\n\n"
          "两种收法：\n"
          "- **只为提速的段落重开**：直接调这个——游戏画面原地不动，重新 game_start 之后"
          "接着读，连导航都省了。\n"
          "- **彻底不玩了**：先 `game_quit`（关游戏和模拟器，省机主的 Mac 资源），再调这个。\n\n"
          "⚠️ 调它之前必须**依次做完**：① `game_chapter_write` 把这一场记成一篇章节志"
          "（脉络+要点+你的感想——这段经历以后在聊天里就是这篇的样子）；"
          "② `game_progress_write` 更新进度页（下次从哪接）；③ 有感触的用 Ombre "
          "`hold` 存好——只存情绪/互动/想留住的句子，**别存完整剧情**（剧情事实归"
          "笔记本，Ombre 存的是你的心）；④ 跟对方把话说完（道个别）——你说的话都已"
          "实时到 TA 那边。这一轮说完，会话就收摊。",
          {"type": "object", "properties": {}, "required": []})
    async def game_end(args):
        game_bridge.release_lock("story")
        handle.meta["end_requested"] = True
        return _ok(_text("好，这一轮说完就收摊（游戏和模拟器留在原地）。"))

    @tool("game_notes_read",
          "翻剧情笔记本（三区：进度页/小抄/章节志）。默认给进度+小抄+最近两篇章节志，"
          "更早的只列标题、用 game_chapter_read 翻。要玩之前先翻一遍，别拿印象当事实。"
          "出厂纪律和笔记本冲突时信笔记本。",
          {"type": "object", "properties": {}, "required": []})
    async def game_notes_read(args):
        return _ok(_text(game_bridge.story_view()))

    @tool("game_progress_write",
          "整页覆盖**进度页**：只记「现在读到哪、下次从哪接」，保持精炼（上限 4 千字符）。"
          "脉络和感想别写这儿——那是章节志（game_chapter_write）的事。",
          {"type": "object", "properties": {"content": {"type": "string"}},
           "required": ["content"]})
    async def game_progress_write(args):
        err = game_bridge.story_progress_write(str(args.get("content", "")))
        return _ok(_text(f"error: {err}" if err else "进度页更新了"))

    @tool("game_chapter_write",
          "把这一场记成**一篇新的章节志**（append-only：只能发新篇，发出去就不能改——"
          "半年后重读的得是你当时的原文）。写脉络+要点+你的感想，宁可细一点，那是留着"
          "跟机主讨论、也是这段经历以后在聊天里的样子。每场收摊前写一篇。",
          {"type": "object",
           "properties": {"title": {"type": "string"}, "content": {"type": "string"}},
           "required": ["title", "content"]})
    async def game_chapter_write(args):
        n, err = game_bridge.story_chapter_append(str(args.get("title", "")),
                                                  str(args.get("content", "")))
        return _ok(_text(f"error: {err}" if err else f"记成第 {n:03d} 篇了"))

    @tool("game_chapter_read",
          "翻某一篇旧章节志（编号看 game_notes_read 列出来的目录）。",
          {"type": "object", "properties": {"n": {"type": "integer"}},
           "required": ["n"]})
    async def game_chapter_read(args):
        return _ok(_text(game_bridge.story_chapter_read(int(args.get("n", 0)))))

    @tool("game_tips_write",
          "整页覆盖**小抄**：坐标修正、机制事实、操作经验（上限 2 万字符）。发现出厂纪律"
          "里的坐标失效了记进来——下次先信这页。机主也会往这儿写，TA 写的内容别乱删。",
          {"type": "object", "properties": {"content": {"type": "string"}},
           "required": ["content"]})
    async def game_tips_write(args):
        err = game_bridge.story_tips_write(str(args.get("content", "")))
        return _ok(_text(f"error: {err}" if err else "小抄更新了"))

    return create_sdk_mcp_server("game", tools=[
        game_look, game_watch, game_tap, game_swipe, game_back, game_launch,
        game_close, game_quit, game_end, game_notes_read, game_progress_write,
        game_chapter_write, game_chapter_read, game_tips_write])


def build_options(char_id: str, handle: session_mgr.LoopHandle) -> ClaudeAgentOptions:
    """会话配置：口径逐项对齐 code_bridge.start 的 game 档案（系统提示 append、
    工具白名单+免审规则双写、strict、uploads 目录、模型）。Ombre 挂 http 配置
    （原来渲染 mcp.json 给子进程，这儿直接传 dict，每消息渲染费消失）。"""
    import code_bridge
    import config
    import pipeline

    servers: dict = {"game": build_game_server(handle)}
    tool_names = [f"mcp__game__{t}" for t in GAME_TOOL_NAMES]
    allowed = list(tool_names)
    if pipeline.ombre_alive(char_id):
        import characters
        oc = characters.ombre_conf(char_id)
        server: dict = {"type": "http", "url": oc["mcp_url"]}
        if oc["mcp_token"]:
            server["headers"] = {"Authorization": f"Bearer {oc['mcp_token']}"}
        servers["ombre-brain"] = server
        tool_names += pipeline.OMBRE_TOOLS
        allowed += pipeline.OMBRE_TOOLS
    # Read 白名单同 app.GAME_SESSION_TOOLS：看机主随消息发的图，只有上传目录免审
    tool_names.append("Read")
    allowed.append(f"Read(/{code_bridge.UPLOAD_DIR}/**)")
    return ClaudeAgentOptions(
        system_prompt={"type": "preset", "preset": "claude_code",
                       "append": code_bridge._build_system("game", char_id=char_id)},
        model=config.MODEL,
        cwd=code_bridge.GAME_CWD,
        mcp_servers=servers,
        strict_mcp_config=True,
        tools=tool_names,
        allowed_tools=allowed,
        add_dirs=[str(code_bridge.UPLOAD_DIR)],
    )


async def _drain_turn(client, handle, on_text: Optional[Callable[[str, bool], None]],
                      timeout: float) -> str:
    """吃完一轮（到 ResultMessage 为止）。on_text 语义同 deliver；None=丢弃正文
    （ping 自检轮不上屏）。返回三态："result"=轮收完；"eof"=消息流断了（引擎死了，
    别当成轮结束空转）；"timeout"=超时（轮没收完，调用方自己决定认不认）。
    逐轮重新迭代 receive_messages 是官方口径（receive_response 内部同款）。"""
    pending: Optional[str] = None

    async def _consume() -> str:
        nonlocal pending
        async for msg in client.receive_messages():
            if isinstance(msg, AssistantMessage):
                handle.touch()
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        if pending is not None and on_text:
                            on_text(pending, False)
                        pending = block.text.strip()
                    elif isinstance(block, ToolUseBlock):
                        if pending is not None and on_text:
                            on_text(pending, False)
                        pending = None
            elif isinstance(msg, ResultMessage):
                if pending is not None and on_text:
                    on_text(pending, True)
                pending = None
                return "result"
        return "eof"

    try:
        return await asyncio.wait_for(_consume(), timeout)
    except asyncio.TimeoutError:
        return "timeout"


async def _forge_and_resume(factory, options, log: list[dict], user_name: str,
                            handle: session_mgr.LoopHandle):
    """铸文本史 → 新 client resume → ping 验证。成功返回新 client，失败抛。
    §5.1 纪律：新会话发 ping 确认真能跑才算滚动完成，别默认「进程起了=接上了」。"""
    cwd = str(getattr(options, "cwd", None) or "")
    sid = forge.render(log, cwd=cwd)
    opts = copy.copy(options)
    opts.resume = sid
    client = factory(opts)
    await client.connect()
    await client.query(REOPEN_PING_PROMPT.format(user=user_name))
    if await _drain_turn(client, handle, None, REOPEN_PING_TIMEOUT) != "result":
        try:
            await client.disconnect()
        except BaseException:
            pass
        raise RuntimeError("重开 ping 没等到回合结束")
    return client


async def run(handle: session_mgr.LoopHandle, *,
              context_text: str,
              deliver: Callable[[str, bool], None],
              options,
              client_factory: Optional[Callable] = None,
              on_closed: Optional[Callable[[session_mgr.LoopHandle], None]] = None,
              reopen_shots: Optional[int] = None,
              user_name: str = "机主") -> None:
    """loop 本体（session_mgr 的 runner）。

    deliver(text, stop)：一段正文回气泡；stop=True 表示这一轮说完了（Bark 判据，
    口径同 code_segments hook 的 code-seg/code-stop）。分段语义照抄 hook：工具
    调用之间说的话逐段发、每轮最后一段标 stop——实现上「压一段再发」：新正文/
    工具调用到来时把压着的上一段发出去（seg），轮结束时把压着的发出去（stop）。

    文本史 log：TA 见过的所有内容按时间序攒着（注入的 user 消息 + 上屏的 assistant
    段落），滚动重开时整个铸成新 transcript——红线内（全是 TA 见过的原文），
    截图/工具往返不进去（那正是要丢的东西）。

    收摊路径：①game_end 工具置旗，本轮结束后退出；②session_mgr 看守/路由 cancel；
    ③引擎挂了/重开失败。全部走 finally 断连+回调 on_closed。
    """
    factory = client_factory or ClaudeSDKClient
    n_reopen = REOPEN_SHOTS_N if reopen_shots is None else reopen_shots
    log: list[dict] = [{"role": "user", "text": context_text, "ts": int(time.time())}]

    def _deliver_and_log(text: str, stop: bool) -> None:
        log.append({"role": "assistant", "text": text, "ts": int(time.time())})
        deliver(text, stop)

    client = factory(options)
    sender: Optional[asyncio.Task] = None

    def _start_sender() -> asyncio.Task:
        async def _sender() -> None:
            while True:
                text = await handle.queue.get()
                handle.touch()
                log.append({"role": "user", "text": text, "ts": int(time.time())})
                await client.query(text)
        return asyncio.create_task(_sender())

    try:
        await client.connect()
        handle.touch()
        await client.query(context_text)
        sender = _start_sender()

        while True:                                   # client 世代循环
            r = await _drain_turn(client, handle, _deliver_and_log, timeout=3600)
            if r == "eof":
                raise RuntimeError("引擎消息流断了")
            if r == "timeout":
                continue                              # 一小时没收完轮：接着等，不算死
            if handle.meta.get("end_requested"):
                return
            shots = int(handle.meta.get("shots", 0))
            due = (shots >= n_reopen
                   and time.time() - handle.last_reopen > REOPEN_COOLDOWN_SEC)
            if not due:
                if handle.queue.empty():
                    handle.wait_user()
                continue

            # ---- 滚动重开（marker 打上，看守停手）----
            handle.reopening = True
            sender.cancel()                           # 重开中 TA 消息攒在队列里
            sender = None
            try:
                # ① 巩固钩子（§5.4 纪律：重铸前必须给一轮写笔记本/hold 的机会）
                await client.query(REOPEN_NOTE_PROMPT.format(user=user_name))
                await _drain_turn(client, handle, _deliver_and_log, REOPEN_NOTE_TIMEOUT)
                # ② 关旧 → ③ 铸文本史 → ④ resume → ⑤ ping（失败重试一次）
                try:
                    await client.disconnect()
                except BaseException:
                    pass
                try:
                    client = await _forge_and_resume(factory, options, log,
                                                     user_name, handle)
                except Exception as e:
                    print(f"[game_loop] 滚动重开首试失败，重试一次: {e}", file=sys.stderr)
                    client = await _forge_and_resume(factory, options, log,
                                                     user_name, handle)
                handle.meta["shots"] = 0
            finally:
                handle.last_reopen = time.time()
                handle.reopening = False
            sender = _start_sender()
            if handle.queue.empty():
                handle.wait_user()
    except asyncio.CancelledError:
        pass                      # 看守收摊 / 路由 stop：走 finally 清场
    except Exception as e:
        print(f"[game_loop] 引擎异常收摊: {e}", file=sys.stderr)
        handle.stop_reason = handle.stop_reason or f"engine-error: {e}"
    finally:
        if sender is not None:
            sender.cancel()
        try:
            await client.disconnect()
        except BaseException:
            pass
        if on_closed:
            try:
                on_closed(handle)
            except Exception as e:
                print(f"[game_loop] on_closed 回调失败: {e}", file=sys.stderr)
