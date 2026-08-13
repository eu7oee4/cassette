"""
cassette 后端（无状态版）。

设计：**app 是聊天历史的唯一主人**——每次请求把完整对话历史发过来，后端只管照着生成、
不自己记（做"编辑 / 重新生成 / 本地保存"都干净，历史随 app 走）。每条消息起一个一次性
`claude -p` 子进程，上下文靠历史注入，不靠进程存活。

运行（在 server/ 目录）：
  cp .env.example .env   # 填 CASSETTE_AUTH_KEY
  .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
"""
import asyncio
import base64
import json
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import urllib.parse

import browser_keeper
import characters
import code_bridge
import config
import game_bridge
import mail_bridge
import ombre_rest
import plugins
import pipeline
import sse
import state_store
import wake
from notify import bark_push, logerr
from pipeline import Message


def _resolve_char(char_id: Optional[str]) -> str:
    """API 层的角色解析：None/空 → 默认角色；不认识的角色名 → 404（严格，别静默串人）。"""
    try:
        return characters.resolve(char_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


def _session_char() -> str:
    """当前 code/game 会话话语归谁。

    优先读 session.json 里这次会话起的时候记下的角色——**会话开着期间归属不能变**：
    中途改了 tmux 资源的归属，剩下半截话会掉进另一个人的会话里，一轮对话被劈成两半。
    记录缺席（旧会话 / 手动开的会话）才退回当前的资源归属。"""
    return code_bridge.session_char() or plugins.owner_of("tmux")


def _browser_keeper_watchdog() -> None:
    """浏览器幽灵看门狗：Chrome（cassette profile 那只）一在跑就搭伙占会话，轮末按
    [[browser:keep/close]] 标记结算去留（browser_keeper.py）。wake/非流式是 subprocess
    跑完才解析、轮中没有钩子，这个线程是唯一全覆盖的口子。Chrome 没跑时一拍就是一次
    pgrep，便宜，不用按插件开关做门。"""
    while True:
        try:
            browser_keeper.watchdog_tick()
        except Exception:
            pass
        time.sleep(2)


def _mail_watcher() -> None:
    """邮箱 watcher：每拍看一眼有没有新信（mail_bridge.watch_tick）。网络活动只在这个
    线程；wake 的预闸门只读它写的本地 flag（保持纯本地）。按插件开关做门——mail 没启用
    或没配置就纯睡觉，商店里拨开关不用重启。失败只在状态翻转时报一次，别每 5 分钟刷屏。"""
    err_logged = False
    while True:
        on = False
        try:
            # 邮箱是独占资源：开关看**归属角色**的启用表（plugins.owner_of）。
            on = bool(plugins._read_enabled(plugins.owner_of("mailbox")).get("mail"))
        except Exception:
            pass
        if on and mail_bridge.configured():
            try:
                mail_bridge.watch_tick()
                err_logged = False
            except Exception as e:
                if not err_logged:
                    logerr(f"mail watcher 失败（恢复前不再报）: {e}")
                    err_logged = True
        time.sleep(mail_bridge.poll_sec())


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """启动 wake 调度器（on_event 已被 FastAPI 弃用，用 lifespan）。"""
    characters.ensure_layout()   # 默认角色目录补齐（state 侧旧布局迁移在 state_store import 时已做）
    tasks = []
    if config.PROACTIVE_ENABLED:
        tasks.append(asyncio.create_task(wake.scheduler_loop()))
    if config.CODE_MODE_ENABLED:
        tasks.append(asyncio.create_task(_code_dialog_watchdog()))
    if config.GAME_MODE_ENABLED:
        tasks.append(asyncio.create_task(_game_watchdog()))
    threading.Thread(target=_browser_keeper_watchdog, daemon=True,
                     name="browser-keeper-watchdog").start()
    threading.Thread(target=_mail_watcher, daemon=True, name="mail-watcher").start()
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="cassette backend", lifespan=_lifespan)

# 请求体上限：多模态 base64 大但有边界（app 侧图 ≤9 张已压缩、文件单个 ≤10MB），
# 64MB 之上就是异常流量——按头拒掉，别整包读进内存再发现撑爆。
_MAX_BODY_BYTES = 64 * 1024 * 1024


@app.middleware("http")
async def _limit_body(request, call_next):
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > _MAX_BODY_BYTES:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "请求体太大（上限 64MB）"}, status_code=413)
    return await call_next(request)


@app.exception_handler(code_bridge.CodeModeOff)
async def _code_mode_off(_request, exc):
    # 会话路由对 code/game 两个档案通用，具体开关在 code_bridge.require_enabled 里判——
    # 它抛的异常统一转 503，语义和以前的 _require_code 一致。
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": str(exc)}, status_code=503)


def verify_auth(x_auth: Optional[str]) -> None:
    if not config.AUTH_KEY:
        raise HTTPException(status_code=503, detail="服务器未配置 CASSETTE_AUTH_KEY，已拒绝所有请求")
    # 常数时间比较：防计时侧信道逐字节猜密钥（内网场景可利用性低，但正确写法就一行）。
    if not secrets.compare_digest(x_auth or "", config.AUTH_KEY):
        raise HTTPException(status_code=401, detail="X-Auth 校验失败")


# ---------- 数据结构 ----------
class StickerInfo(BaseModel):
    id: str
    description: str = ""
    num: Optional[int] = None   # 永久序号（稳定，删了留空）；有则用 s{num} 当代号


class ImageInput(BaseModel):
    data: str                 # base64
    media_type: str = "image/png"


class FileInput(BaseModel):
    """随消息带的文件，走 API 的 document block 直接喂给模型。
    PDF=base64 原样；文本类(md/txt/代码/json)=text source；docx=解压抽正文再走 text source。"""
    data: str                 # base64
    media_type: str = "application/pdf"
    name: str = ""            # 原始文件名，给模型当 title 用


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _decode_text(raw: bytes) -> str:
    """文本文件解码：utf-8 优先，兜 gb18030（Windows 来的中文文档），再不行替换坏字符。"""
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _docx_text(raw: bytes) -> str:
    """docx 抽正文：零依赖（docx=zip 里的 word/document.xml），按段落取 <w:t> 文本。
    旧版二进制 .doc 不是 zip，这里会抛错，由调用方转成 400。"""
    import io
    import zipfile
    from xml.etree import ElementTree
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paras = []
    for p in root.iter(f"{w}p"):
        text = "".join(t.text or "" for t in p.iter(f"{w}t"))
        if text.strip():
            paras.append(text)
    return "\n".join(paras)


def _file_to_block(f: FileInput) -> dict:
    """FileInput → API document block。类型不认识/解不开 → 400（路由里流开始前调，能正常报错）。"""
    mt = (f.media_type or "").lower()
    label = f.name or "未命名文件"
    if mt == "application/pdf":
        blk = {"type": "document",
               "source": {"type": "base64", "media_type": "application/pdf", "data": f.data}}
    else:
        try:
            raw = base64.b64decode(f.data)
        except Exception:
            raise HTTPException(status_code=400, detail=f"文件数据不是合法 base64：{label}")
        if mt == _DOCX_MIME:
            try:
                text = _docx_text(raw)
            except Exception:
                raise HTTPException(status_code=400, detail=f"读不了这个 Word 文件（要 .docx）：{label}")
        elif mt.startswith("text/") or mt == "application/json":
            text = _decode_text(raw)
        else:
            raise HTTPException(status_code=400, detail=f"暂不支持的文件类型 {mt}：{label}")
        # text source 的 media_type 按 API 规定只能是 text/plain（md/代码原文照样完整可读）
        blk = {"type": "document",
               "source": {"type": "text", "media_type": "text/plain", "data": text}}
    if f.name:
        blk["title"] = f.name
    return blk


class ChatRequest(BaseModel):
    messages: list[Message]            # 完整对话历史，最后一条应是用户的新消息
    char_id: Optional[str] = None      # 跟哪个角色说（多角色）；不传 = 默认角色（旧 app 兼容）
    session_id: Optional[str] = None   # 仅用于 app 记账，后端不依赖它记忆
    stickers: Optional[list[StickerInfo]] = None  # 表情库清单(id+描述)，供模型挑着发/改描述
    client_req_id: Optional[str] = None  # 断连补投关联 id：rescue 条目带回给 app 替换半截气泡
    images: Optional[list[ImageInput]] = None  # 附给"最新这条"的图片，带图走多模态
    files: Optional[list[FileInput]] = None    # 附给"最新这条"的文件（PDF/文本/docx），同上


class StoredItem(BaseModel):
    tool: str
    text: str
    ok: bool = True     # 工具真的干成了吗（按 tool_result 定案，见 pipeline.StoredCollector）
    error: str = ""     # ok=False 时的原因，给人看的一句话


class DescUpdate(BaseModel):
    id: str
    description: str


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    stored: list[StoredItem] = []        # 这轮工具调用的结构化产物（长期记忆等，后续模块填充）
    sticker_sends: list[str] = []        # 这轮他要发的表情（sticker id，按顺序）
    desc_updates: list[DescUpdate] = []  # 这轮他改的表情描述
    next_wake_hint: Optional[str] = None  # 这轮他若定了下次醒来 → 现成的灰字提示文案；没定则 None
    code_started: bool = False           # 这轮他自己切进了 code 模式 → app 收到翻 codeMode
    game_started: bool = False           # 这轮他自己切去玩游戏了 → app 亮终端面板 + 系统灰字


def _prepare_chat(req: ChatRequest, char_id: Optional[str] = None) -> tuple[str, dict]:
    """校验 + 表情清单落盘 + 拼 prompt + handle 映射。失败抛 HTTPException（流开始前，能正常返 4xx）。"""
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages 不能为空")
    last = req.messages[-1]
    if last.role != "user" or not last.text.strip():
        raise HTTPException(status_code=400, detail="最后一条必须是非空的用户消息")
    # 表情清单：转成朴素 dict，持久化一份（醒来时后端不在 app 手里也能挑表情发；全角色共用）。
    catalog = pipeline.to_catalog(req.stickers)
    if catalog:
        try:
            state_store.write_sticker_catalog(catalog)
        except Exception as e:
            logerr(f"写 sticker_catalog 失败: {e}")
    return (pipeline.build_prompt(req.messages, catalog, char_id=char_id),
            pipeline.sticker_handle_map(catalog))


# ---------- recent_window 的唯一口径 ----------
# 铁律：**任何让 app 侧历史发生变化的动作，都要让窗口跟上**——不然醒来那条路看到的是
# 一个已经不存在的世界。三条支路都收在这儿：
#   ① /chat(/stream)：轮开始 _overwrite_window_from、收尾 finalize_chat_reply 各写一次；
#   ② code 模式：_code_window_append() 逐条追加（那边没有"完整历史"可覆盖）；
#   ③ /window/sync：app 删/编辑了消息，不生成、只把窗口对齐（A1）。

def _overwrite_window_from(messages: list[Message], char_id: Optional[str] = None) -> bool:
    """用 app 发来的这份历史整体覆盖该角色的 recent_window。返回是否真写了。
    ⚠️ 迷你历史护栏：1 条历史的 curl 请求会把 300 条窗口冲空，finalize 里那道同名护栏
    读到的已是被冲掉的窗口，形同虚设——所以每个覆盖点都得自己设一道。"""
    try:
        snap = [{"role": m.role, "text": m.text, "ts": m.ts} for m in messages]
        with state_store.WINDOW_LOCK:
            cur = state_store.read_recent_window(char_id)
            if len(snap) < 5 and len(cur) > len(snap):
                return False   # 短历史请求不覆盖丰满窗口
            state_store.write_recent_window(snap, char_id)
        return True
    except Exception as e:
        logerr(f"写 recent_window 失败: {e}")
        return False


def _snapshot_incoming_window(req: ChatRequest, char_id: Optional[str] = None) -> None:
    """轮一开始就把眼下的对话（含刚收到的 user 消息）写进 recent_window——
    这轮可能跑很久，中途 wake 醒来不该只看到上一轮的世界。收尾 finalize 用带回复的完整版覆盖。
    被护栏挡下时新消息由 finalize 的追加分支补进。"""
    _overwrite_window_from(req.messages, char_id)
    # 上一轮的残留正文清掉：断连/异常那一支不走 finalize，不在这儿清就会漏进下一次自切。
    state_store.clear_live_reply()


def finalize_chat_reply(reply: str, stored: list[dict], req: ChatRequest,
                        handle_to_id: dict, char_id: Optional[str] = None) -> ChatResponse:
    """回复的统一后处理（解析表情/[[next_wake]] 标记 / 写窗口快照 → 组响应）。
    非流式和流式收尾共用，保证两条路一字不差。"""
    # 解析并剥掉表情标记：他要发哪几张、改了哪些描述。
    reply, sticker_sends, desc_updates = pipeline.parse_sticker_markers(reply, handle_to_id)

    # 聊天里他若安排了下次主动醒来 → 更新 schedule（只动 next_wake_at）+ 回灰字提示。
    reply, next_min, next_raw = pipeline.parse_chat_next(reply)
    next_wake_hint = None
    if next_min is not None:
        at = int(time.time()) + next_min * 60
        with state_store.SCHEDULE_LOCK:
            sched = state_store.read_schedule(char_id)
            sched["next_wake_at"] = at
            state_store.write_schedule(sched, char_id)
        next_wake_hint = pipeline.next_wake_note(next_raw, at)
        logerr(f"聊天里定了下次醒来：{next_raw} → {pipeline.fmt_ts(at)}")

    # 浏览器去留标记：在下面的通用剥标记之前截下来；结算放在算出 browsed 之后（见下）。
    reply, browser_choice = pipeline.parse_browser_markers(reply)

    reply = pipeline.strip_markers(reply).strip()

    # 写最近窗口快照：醒来时 app 不在场，模型靠这个拿上下文。
    # ⚠️ 防"迷你历史覆盖"：正常 app 请求带最近 ~100 条；带 1-2 条历史的请求（curl 测试等）
    # 若整体覆盖会把窗口冲成近乎空白 → wake 失忆。历史太短且现有窗口更丰满 → 改为追加不覆盖。
    try:
        snap = [{"role": m.role, "text": m.text, "ts": m.ts} for m in req.messages]
        # 发的表情在窗口里留占位（回忆得起自己发过表情）。
        snap_text = reply + "".join(f"\n[sticker_{h}]" for h, i in handle_to_id.items()
                                    if i in sticker_sends)
        snap.append({"role": "assistant", "text": snap_text, "ts": int(time.time())})
        with state_store.WINDOW_LOCK:
            cur = state_store.read_recent_window(char_id)
            if len(req.messages) < 5 and len(cur) > len(snap):
                cur.extend(snap[-2:])   # 只追加这轮的一来一回
                state_store.write_recent_window(cur, char_id)
            else:
                state_store.write_recent_window(snap, char_id)
    except Exception as e:
        logerr(f"写 recent_window 失败: {e}")

    # 这轮的回复已经落进窗口了，中转缓冲功成身退（留着只会漏给下一轮）。
    state_store.clear_live_reply()

    # 浏览器插件：逐条 navigate 聚合成一条 browse（text=网址列表，\n 分隔，去重保序）。
    # app 收起显示「浏览了 N 个网页」、点开展开网址；sse 那边逐条是不发灰字的，这里是
    # 唯一出口。失败的 navigate 不算浏览过。顺手落 browse_log（Mind 页未来素材）。
    browse_urls: list[str] = []
    for s in stored:
        if s.get("tool") == "browse" and s.get("ok", True):
            u = (s.get("text") or "").strip()
            if u and u not in browse_urls:
                browse_urls.append(u)
    if any(s.get("tool") == "browse" for s in stored):
        stored = [s for s in stored if s.get("tool") != "browse"]
        if browse_urls:
            stored.append({"tool": "browse", "text": "\n".join(browse_urls)})
            try:
                state_store.append_browse_log({"ts": int(time.time()), "time": pipeline.now_str(),
                                               "source": "chat", "urls": browse_urls},
                                              char_id=char_id)
            except Exception as e:
                logerr(f"记 browse_log 失败: {e}")

    # 轮末结算浏览器去留：默认这轮浏览过就关窗口；keep=粘住（窗口留着）；close=明确关。
    # 没浏览也没标记的轮不碰 keeper（apply_choice 内部口径）——别误关并行 wake 轮的窗口。
    browser_keeper.apply_choice(browser_choice, browsed=bool(browse_urls))

    # 这轮他自己调工具切进了 code 模式：剥出来置标志（控制信号，不是记忆产物，不进 Mind），
    # app 收到 code_started 就翻 codeMode，后续消息改道 tmux 会话。
    # 要 ok=True——切失败了（如"会话占用中"）还翻开关的话，后续消息会往一个不存在的
    # 会话里发。万一判据把成功误读成失败，回前台的 /code/status 对齐会把它补回来。
    code_started = any(s.get("tool") == "codemode" and s.get("ok", True) for s in stored)
    # 同款：TA 自己调 game_start 切去玩游戏。app 收到就标游戏会话活着（终端面板照 code
    # 模式那套亮起）；失败不置位，回前台 /code/status 的 profile 字段兜底对齐。
    game_started = any(s.get("tool") == "gamemode" and s.get("ok", True) for s in stored)

    # 切换轮的「后半截话」补递进会话。live_reply（会话开场上下文里那份）只救得了
    # 调用**之前**说的话；TA 调完 code_start/game_start、这轮收尾前说的话只存在于聊天，
    # 会话里的 TA 看不见自己刚说过什么（08-13 实锤：聊天里定了昵称「卡带」，
    # 会话里说「昵称的事我边读边想」）。这里把整轮定稿的发言补送进去——开头可能和
    # 开场上下文里的 live 段有重叠，框里说明白，比丢话强。
    if (code_started or game_started) and reply.strip():
        try:
            if code_bridge.session_alive():
                u = config.user_name()
                code_bridge.send(
                    f"〔系统补递，不是{u}发的：你切过来的那一轮，你在聊天里说的完整发言"
                    f"如下。{u}已经看到了，别当新话重说一遍；开头可能和你开场看到的上下文"
                    "有重叠。〕\n" + reply.strip())
        except Exception as e:
            logerr(f"切换轮补递失败: {e}")

    # 聊天里存/改的记忆也记进 wake_log（source=chat，无 thoughts 不进时间线）：
    # 醒来的"别重复存"清单靠它才看得到聊天里已存过的。
    # 没成功的（ok=False）也记——TA 下次醒来看到「想存但缺 source_bucket」就知道该补参数；
    # 「别重复存」那份清单会把它们滤掉（见 wake.wake_prompt），没存成的当然要能再存。
    # 网页/codemode 不进日志：网页有聊天卡片 + HTML 文件列表两个展示面，codemode 是控制
    # 信号；心流日志只记记忆类操作，防重复清单也不被它们污染。
    mem_stored = [s for s in stored if s.get("tool") not in pipeline.NON_MEMORY_TOOLS]
    if mem_stored:
        try:
            state_store.append_wake_log({"ts": int(time.time()), "time": pipeline.now_str(),
                                         "source": "chat", "stored": mem_stored},
                                        char_id=char_id)
        except Exception as e:
            logerr(f"记 chat stored 失败: {e}")

    return ChatResponse(
        reply=reply,
        session_id=req.session_id or str(uuid.uuid4()),
        stored=[StoredItem(**s) for s in stored],
        sticker_sends=sticker_sends,
        desc_updates=[DescUpdate(**u) for u in desc_updates],
        next_wake_hint=next_wake_hint,
        code_started=code_started,
        game_started=game_started,
    )


# ---------- 路由 ----------
@app.get("/health")
def health():
    return {"ok": True}


@app.get("/characters")
def characters_list(x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """角色清单（app 会话列表的数据源）。默认角色永远在第一位。"""
    verify_auth(x_auth)
    return {"items": [{"id": cid, "display_name": characters.display_name(cid)}
                      for cid in characters.ids()]}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """非流式聊天（流式的回退路，两条路收尾共用 finalize 保证一致）。"""
    verify_auth(x_auth)
    cid = _resolve_char(req.char_id)
    prompt, handle_to_id = _prepare_chat(req, cid)
    _snapshot_incoming_window(req, cid)
    # 文件转 block 在调用前做：类型不支持/解不开在这里 400，不进子进程。
    file_blocks = [_file_to_block(f) for f in (req.files or [])]
    wake.chat_turn_begin(cid)
    try:
        if req.images or file_blocks:
            reply, stored = pipeline.call_claude_multimodal(prompt, req.images or [],
                                                            file_blocks=file_blocks,
                                                            char_id=cid)
        else:
            reply, stored = pipeline.call_claude(prompt, char_id=cid)
    finally:
        wake.chat_turn_end(cid)
    return finalize_chat_reply(reply, stored, req, handle_to_id, char_id=cid)


# 进行中的聊天轮（client_req_id 集合）：app 断流后用 GET /chat/active 对账——
# 后端没在跑这轮且过了宽限 → app 收起"正在输入"并提示重发（修"请求根本没到后端"的静默丢失）。
_ACTIVE_REQS: set[str] = set()


@app.get("/chat/active")
def chat_active(x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return {"active": sorted(_ACTIVE_REQS)}


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """流式聊天：SSE 逐字回复。协议见 sse.py。"""
    verify_auth(x_auth)
    cid = _resolve_char(req.char_id)
    prompt, handle_to_id = _prepare_chat(req, cid)   # 校验在流开始前，能正常返 4xx
    file_blocks = [_file_to_block(f) for f in (req.files or [])]   # 同上：4xx 趁早
    _snapshot_incoming_window(req, cid)

    def finalize(reply: str, stored: list[dict]) -> dict:
        return jsonable_encoder(finalize_chat_reply(reply, stored, req, handle_to_id,
                                                    char_id=cid))

    async def gen():
        rid = req.client_req_id or ""
        if rid:
            _ACTIVE_REQS.add(rid)
        try:
            # 断连守护：消费 claude 的活儿放独立任务——客户端断了（切后台/锁屏）
            # 子进程照跑到完；done 没送达客户端 → 整段回复补投 outbox，app 回前台
            # 轮询 /pending 补上屏，一个字都不丢。
            q: asyncio.Queue = asyncio.Queue()
            state = {"done_delivered": False, "final": None}

            async def _produce():
                try:
                    def translate(events):
                        return sse.translate_events(events, finalize)
                    async for chunk in sse.stream_claude(prompt, translate, images=req.images,
                                                         file_blocks=file_blocks, char_id=cid):
                        # ⚠️ 字节嗅探依赖 sse.sse() 用 json.dumps 默认分隔符（": " 带空格）——
                        # 正文里出现同样字样会被转义成 \" 不误判；若改压缩分隔符此检测会静默失效。
                        if b'"type": "done"' in chunk:
                            try:
                                payload = json.loads(chunk.decode("utf-8")[len("data: "):].strip())
                                if payload.get("type") == "done":
                                    state["final"] = payload
                            except Exception:
                                pass
                        q.put_nowait(chunk)
                except Exception as e:
                    logerr(f"/chat/stream 生产端异常: {e}")
                    q.put_nowait(sse.sse({"type": "error", "content": "连接中断了，大模型那边出了点问题。"}))
                    q.put_nowait(sse.sse({"type": "done"}))
                finally:
                    # 轮结束点在生产端而非 gen()：客户端断了子进程还在跑（断连守护），
                    # 那段时间对 wake 来说这轮仍是"进行中"。
                    wake.chat_turn_end(cid)
                    q.put_nowait(None)

            wake.chat_turn_begin(cid)
            producer = asyncio.create_task(_produce())

            async def _rescue_if_undelivered():
                """等生产端跑完；回复没送达客户端就丢进待送达盒子。
                空产出（claude 挂了/流断在半路）不再静默丢弃——投 error 标记条目，
                app 收到后收起等待态、提示重发（不然那轮就人间蒸发了）。
                ⚠️ 活跃登记由这里的 finally 移除：rescue 落盘前这轮要一直算"在跑"，
                否则 app 会在补投到达前误判"丢了"。"""
                try:
                    try:
                        # 不设总时长上限：生产端的空闲超时（事件间 gap）保证它必然终止，
                        # 而这里若按总时长掐（CLAUDE_TIMEOUT+60），超长生成的真实回复会被
                        # 误判成"没产出"投 error——用户被叫去重发，回复却在没人消费的队列里蒸发。
                        await asyncio.shield(producer)
                    except Exception:
                        pass
                    if state["done_delivered"]:
                        return
                    final = state["final"] or {}
                    text = (final.get("reply") or "").strip()
                    stickers = final.get("sticker_sends") or []
                    if text or stickers:
                        # stored 也带上：browse 灰字/网页卡片只随 done 走，断连轮 app 从来
                        # 没见过 done——不带的话这轮的浏览记录在 app 里人间蒸发（实锤：
                        # 08-10 二轮测试）。app 侧只从这里补渲染 done 独有的（browse/webpage），
                        # 记忆灰字不补（流式中途已就地发过，补了重复）。
                        state_store.outbox_append({"id": uuid.uuid4().hex[:12], "ts": int(time.time()),
                                                   "text": text, "sticker_ids": stickers,
                                                   "stored": final.get("stored") or [],
                                                   "char_id": cid,
                                                   "delivered": False, "origin": "chat_rescue",
                                                   "req_id": rid})
                        bark_push(text if text else "（发来了表情）",
                                  title=characters.display_name(cid))
                        logerr("/chat/stream 客户端断了，完整回复已补投 outbox")
                    else:
                        state_store.outbox_append({"id": uuid.uuid4().hex[:12], "ts": int(time.time()),
                                                   "text": "", "error": True, "char_id": cid,
                                                   "delivered": False, "origin": "chat_error",
                                                   "req_id": rid})
                        logerr("/chat/stream 断连守护：这轮没产出，投 error 标记条目")
                finally:
                    _ACTIVE_REQS.discard(rid)

            try:
                while True:
                    # 心跳：工具调用期长时间不产正文，app 侧空闲计时会断线——
                    # 一段时间没东西可发就 ping 撑住（app 对不认识的类型忽略）。
                    try:
                        chunk = await asyncio.wait_for(q.get(), timeout=config.STREAM_PING_SEC)
                    except asyncio.TimeoutError:
                        yield sse.sse({"type": "ping"})
                        continue
                    if chunk is None:
                        break
                    yield chunk
                    if b'"type": "done"' in chunk:
                        state["done_delivered"] = True
            except (asyncio.CancelledError, GeneratorExit):
                # 客户端走了：别杀生产端，挂一个补投观察员（活跃登记由它移除），按规矩退出。
                asyncio.create_task(_rescue_if_undelivered())
                raise
            _ACTIVE_REQS.discard(rid)   # 正常走完
        except Exception as e:
            # 响应头已发出，绝不再抛普通 JSON：用 SSE error + done 收尾。
            _ACTIVE_REQS.discard(rid)
            logerr(f"/chat/stream 异常: {e}")
            yield sse.sse({"type": "error", "content": "连接中断了，大模型那边出了点问题。"})
            yield sse.sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


# ---------- 窗口同步（app 侧历史变了但不生成）----------
class WindowSyncIn(BaseModel):
    messages: list[Message]   # app 侧当前历史（和 /chat 同口径，最近 ~100 条）
    char_id: Optional[str] = None   # 同 ChatRequest：不传 = 默认角色


@app.post("/window/sync")
def window_sync(body: WindowSyncIn, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """删/编辑消息后把 recent_window 对齐——这类操作纯本地、不触发任何后端请求，
    不同步的话在下次发消息之前，TA 每次醒来看到的都是删改之前的世界。

    **不触发任何生成**：只覆盖窗口。护栏和 /chat 那条路共用（见 _overwrite_window_from），
    所以删到只剩几条时窗口不会被冲空——宁可窗口略旧，也不能让醒来失忆。"""
    verify_auth(x_auth)
    written = _overwrite_window_from(body.messages, _resolve_char(body.char_id))
    return {"ok": True, "written": written, "n": len(body.messages)}


# ---------- 表情描述 ----------
class DescribeRequest(BaseModel):
    image: ImageInput


class DescribeResponse(BaseModel):
    description: str


@app.post("/describe_sticker", response_model=DescribeResponse)
def describe_sticker(req: DescribeRequest, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """给一张表情图，让模型写一句描述（画面+适合情绪），供以后挑选。
    多模态走 stream-json 输入（一条含 [text, image] 的 user 消息）。"""
    verify_auth(x_auth)
    prompt = ("这是用户加进表情库的一张表情包。用一句话描述它：画面里是什么 + 适合用来表达的情绪或场景。"
              "只回这一句话，不要引号、不要多余的话。")
    content = [{"type": "text", "text": prompt}, {
        "type": "image",
        "source": {"type": "base64", "media_type": req.image.media_type, "data": req.image.data},
    }]
    user_msg = {"type": "user", "message": {"role": "user", "content": content}}
    args = pipeline.base_claude_args() + [
        "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
    ]
    try:
        proc = subprocess.run(args, input=json.dumps(user_msg) + "\n", capture_output=True,
                              text=True, env=pipeline._subprocess_env(),
                              timeout=config.CLAUDE_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="claude 超时未返回")
    if proc.returncode != 0:
        raise HTTPException(status_code=502, detail=f"claude 进程出错: {proc.stderr[:300]}")
    desc, _ = pipeline.parse_claude_stream(proc.stdout)
    if not desc:
        raise HTTPException(status_code=502, detail="没拿到描述")
    return DescribeResponse(description=desc.strip().strip('"“”'))


# ---------- 设置（wake + 名字）----------
class SettingsIn(BaseModel):
    agent_name: str = ""     # AI 的名字（app 首启引导设置；空=用后端兜底默认）
    user_name: str = ""      # 用户昵称（同上）
    enabled: bool
    active_start: str
    active_end: str
    day_freq: str            # low | mid | high
    night_freq: str
    daily_max: Optional[int] = None
    min_interval_min: Optional[int] = None
    quiet_after_user_min: Optional[int] = None
    wake_window_n: Optional[int] = None   # wake 注入窗口条数（None=默认 50；夹 20~300）
    wake_daily_budget: Optional[int] = None  # 每天最多自发醒来次数；None=不限（拦醒来本身，硬触发豁免）
    user_pronoun: str = "TA"  # 提到用户时的人称代词：她 | 他 | TA


def _validate_hhmm(s: str) -> None:
    import re as _re
    m = _re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m:
        raise HTTPException(status_code=400, detail=f"时间格式应为 HH:MM：{s}")
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 24 and 0 <= mi <= 59 and not (h == 24 and mi != 0)):
        raise HTTPException(status_code=400, detail=f"时间越界：{s}")


@app.get("/settings")
def get_settings(char: Optional[str] = None,
                 x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return state_store.load_settings(_resolve_char(char))


@app.post("/settings")
def post_settings(body: SettingsIn, char: Optional[str] = None,
                  x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    _validate_hhmm(body.active_start)
    _validate_hhmm(body.active_end)
    if body.day_freq not in ("low", "mid", "high") or body.night_freq not in ("low", "mid", "high"):
        raise HTTPException(status_code=400, detail="freq 需为 low/mid/high")
    for name in ("daily_max", "min_interval_min", "quiet_after_user_min", "wake_daily_budget"):
        v = getattr(body, name)
        if v is not None and v < 0:
            raise HTTPException(status_code=400, detail=f"{name} 不能为负")
    if body.wake_window_n is not None and not (20 <= body.wake_window_n <= 300):
        raise HTTPException(status_code=400, detail="wake_window_n 需在 20~300")
    for nm in (body.agent_name, body.user_name):
        if len(nm.strip()) > 20:
            raise HTTPException(status_code=400, detail="名字最长 20 字")
    if body.user_pronoun not in ("她", "他", "TA"):
        raise HTTPException(status_code=400, detail="user_pronoun 需为 她/他/TA")
    saved = body.model_dump()
    saved["agent_name"] = saved["agent_name"].strip()
    saved["user_name"] = saved["user_name"].strip()
    state_store.save_settings(saved, _resolve_char(char))
    return saved




# ---------- 待送达盒子（断连补投）----------
class AckIn(BaseModel):
    ids: list[str]


@app.get("/pending")
def get_pending(x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return {"items": state_store.outbox_pending()}


@app.post("/pending/ack")
def post_pending_ack(body: AckIn, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    state_store.outbox_ack(body.ids)
    return {"ok": True}


# ---------- Code 模式（tmux 里一个常驻的交互式 claude）----------
# 聊天走 `claude -p` 一次性子进程；code 模式换一条运输管道：消息 send-keys 进 tmux 会话，
# 回复由会话里的 hook POST 回 /code/append → outbox → app 现有的轮询上屏。
# 记忆连贯是架构自带的：这一来一回照常进 app 的 ChatStore，切回聊天时下一轮注入的历史里
# 天然含着 code 模式说过的每一句，什么都不用"同步"。

class CodeStartIn(BaseModel):
    messages: list[Message] = []        # app 发来的最近历史（和 /chat 同结构、同条数）
    cwd: Optional[str] = None           # 这次会话的工作目录；不给 = config.CODE_CWD


class CodeSendIn(BaseModel):
    text: str = ""
    images: Optional[list[ImageInput]] = None   # 随消息带图（同 chat 的结构）
    files: Optional[list[FileInput]] = None     # 随消息带文件；和聊天不同，这边落盘给路径


class CodeKeysIn(BaseModel):
    keys: str


class CodeAppendIn(BaseModel):
    """会话里的 hook 上报的一段正文（server/hooks/code_segments.py 发的）。"""
    role: str = "assistant"
    text: str = ""
    source: str = ""                    # code-stop（这轮说完了）/ code-seg（中间过程）
    ts: Optional[str] = None            # 正文哈希派生，重试同 ts → 服务端去重


CODE_HISTORY_CAP = 100    # 注入 code 会话的历史条数（和 app 发 /chat 的口径对齐）
CODE_BARK_GAP_SEC = 60    # 一轮 Stop 可能连发好几段，60s 内只推第一条


_code_bark_state: dict = {"last": 0.0}


def _require_code() -> None:
    """会话类路由的门槛。game 剧情会话复用 /code/* 整排路由（终端页/发话/弹窗按钮），
    所以两个模式开任一个都放行——具体这次会话是哪个档案、该受哪个开关管，由
    code_bridge.require_enabled(active_profile) 在真正动会话时把关。"""
    if not (config.CODE_MODE_ENABLED or config.GAME_MODE_ENABLED):
        raise HTTPException(status_code=503,
                            detail="Code 模式没开：在 server/.env 里设 CODE_MODE_ENABLED=1 再重启后端")


def _code_window_append(role: str, text: str) -> None:
    """code 模式的对话也写进 recent_window（会话归属角色的那份）——不然醒来时它对这段
    完全失明，会拿着几小时前的世界说胡话。加锁口径同 finalize。"""
    if not (text or "").strip():
        return
    try:
        cid = _session_char()
        with state_store.WINDOW_LOCK:
            window = state_store.read_recent_window(cid)
            window.append({"role": role, "text": text, "ts": int(time.time())})
            state_store.write_recent_window(window, cid)
    except Exception as e:
        logerr(f"code 写 recent_window 失败: {e}")


# 聊天里说过的技术判断都是没工具时说的，切过来必须当传闻不当前提。
_CODE_CTX_CAVEAT = (
    "\n\n【关于上面这些对话】里面凡是关于代码/文件/配置/报错的具体说法，都是你在手机聊天里"
    "**没有任何电脑工具、读不到任何文件**的情况下说的，一律当没验证过的传闻，别当既定前提。"
    "以你现在自己读到的文件为准；对不上就以文件为准，不用照顾之前说过的话。\n"
)


def _code_context(conv: list[dict], scene: str, tail: str = "",
                  char_id: Optional[str] = None) -> str:
    """拼注入给新会话的第一段话：场景 + 最近的对话（和聊天同款渲染）+ 告诫。
    char_id＝这次会话归谁（时间线里的内心要取他自己那份 wake_log，别混进别的角色的）。"""
    timeline = (pipeline.build_context_timeline(conv, reflect_limit=5, char_id=char_id)
                if conv else "（还没聊过什么）")
    return (scene + "\n\n【下面是你们刚才的对话】\n" + timeline + _CODE_CTX_CAVEAT + tail)


@app.get("/code/status")
def code_status(busy: int = 0, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """app 用它决定显不显示 Code 入口、以及回前台时对齐模式开关。
    没开/没装 tmux 也照常回 200（enabled=false），app 静默隐藏入口即可。

    busy=1 时额外探一下"TA 在不在干活"（退出模式前问一句用）。这个探测要花 0.6 秒
    比对两帧画面，所以默认不做——回前台对齐是高频调用，不该为它等。"""
    verify_auth(x_auth)
    if not (config.CODE_MODE_ENABLED or config.GAME_MODE_ENABLED):
        return {"enabled": False, "alive": False, "tmux": False, "cwd": "", "busy": False,
                "profile": "code", "owner": "", "owner_name": "", "session_char": ""}
    # enabled 维持「code 模式开没开」的老语义（app 靠它显示 Code 入口）；
    # game 会话复用同一排路由，靠 profile 字段区分（app 的 game 页用）。
    # owner＝「电脑上的会话」这样资源现在归谁：app 据此在别人的会话里禁掉 Code 按钮
    # （不禁的话按下去只会吃一个 409，还得看得懂那句话才知道为什么）。
    # session_char＝正开着这个会话的是谁（起会话时钉死的，可能和 owner 不同：
    # 会话开着期间归属被转走过）。
    owner = plugins.owner_of("tmux")
    return {"enabled": config.CODE_MODE_ENABLED, "alive": code_bridge.session_alive(),
            "tmux": code_bridge.tmux_available(), "cwd": config.CODE_CWD,
            "busy": bool(busy) and code_bridge.is_busy(),
            "profile": code_bridge.active_profile(),
            "owner": owner, "owner_name": characters.display_name(owner),
            "session_char": code_bridge.session_char()}


@app.post("/code/start")
def code_start(inp: CodeStartIn, char: Optional[str] = None,
               x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """切进 code 模式：杀旧起新（每次都是干净会话，无漂移）+ 注入最近历史。

    char＝你正在跟谁聊（app 的 authedRequest 给所有请求都带了 ?char=）。会话吃的是 tmux
    这样独占资源，**不归他就拒**：放行的话，注入的是这个角色的历史、挂的却是资源归属者
    的记忆，会话里说的话也会掉进对方的聊天里——一次静默的串台，比当场拒绝难查得多。"""
    verify_auth(x_auth)
    _require_code()
    cid = _resolve_char(char)
    owner = plugins.owner_of("tmux")
    if cid != owner:
        raise HTTPException(
            status_code=409,
            detail=f"电脑上的会话现在归「{characters.display_name(owner)}」——"
                   f"要给「{characters.display_name(cid)}」用的话，"
                   "去插件商店右上角把「电脑上的会话」转过来")
    conv = [{"ts": m.ts, "role": m.role, "text": m.text} for m in inp.messages][-CODE_HISTORY_CAP:]
    scene = (f"【场景】你刚从 {config.user_name()} 的手机聊天切到 code 模式：还是你，"
             "只是这个会话里你手上有整台电脑的工具（读写文件、跑命令都行）。"
             f"{config.user_name()} 多半是有活要你干，也可能只是继续聊。")
    tail = ("\n【说明】活干到关键节点或者干完了，正常跟 TA 说话就行——你说的话会回到 TA 的"
            "聊天气泡里。先打个招呼说你切过来了。")
    # 上面校验过 cid 就是资源归属者，所以时间线的内心、挂的 Ombre、会话归属三者同一个人。
    r = code_bridge.start(_code_context(conv, scene, tail, char_id=cid),
                          config.AUTH_KEY, cwd=inp.cwd,
                          mcp_configs=_code_mcp_configs(cid), char_id=cid)
    if not r.get("ok"):
        raise HTTPException(status_code=409, detail=r.get("error", "起会话失败"))
    return r


def _code_mcp_configs(char_id: Optional[str] = None) -> list:
    """给 code 会话挂的 MCP：长期记忆跟着走（不然切过去就失忆了）——挂的是**会话归属
    角色**的那份记忆。插件不挂——那些工具是给聊天用的，code 会话手上有真家伙，不需要。"""
    cid = char_id or code_bridge.session_char() or plugins.owner_of("tmux")
    if not pipeline.ombre_alive(cid):
        return []
    return [str(pipeline._ombre_mcp_config(cid))]


@app.post("/code/send")
def code_send(inp: CodeSendIn, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    _require_code()
    # 先过护栏再落图，别白存文件
    if not code_bridge.session_alive():
        raise HTTPException(status_code=409, detail="会话不在（先切一次 Code 模式）")
    if code_bridge.dialog_pending():
        raise HTTPException(status_code=409,
                            detail="TA 正停在一个确认弹窗上，这条会被弹窗吃掉——先在终端里按掉，再发")
    msg = (inp.text or "")
    notes: list[str] = []
    n_img = 0
    if inp.images:
        try:
            items = [(base64.b64decode(im.data), im.media_type) for im in inp.images]
        except Exception:
            raise HTTPException(status_code=400, detail="图片 base64 解不开")
        paths = code_bridge.save_uploads(items)
        n_img = len(paths)
        notes.append(f"〔随消息发来 {n_img} 张图，用 Read 看：{'  '.join(paths)}〕")
    n_file = 0
    if inp.files:
        try:
            items = [(base64.b64decode(f.data), f.name) for f in inp.files]
        except Exception:
            raise HTTPException(status_code=400, detail="文件 base64 解不开")
        # 落盘给路径，不转 document block：那个会话手上有 Read/Grep，自己看比塞进上下文好，
        # 大文件也不占 token。
        paths = code_bridge.save_files(items)
        n_file = len(paths)
        notes.append(f"〔随消息发来 {n_file} 个文件，自己去读：{'  '.join(paths)}〕")
    if notes:
        body = "\n".join(notes)
        msg = (msg + "\n\n" + body) if msg.strip() else body
    if not msg.strip():
        raise HTTPException(status_code=400, detail="空消息")
    # 时间头：交互式会话里没有聊天那套时间注入，模型只能靠猜（实锤过：下午说"早点睡"）。
    msg = f"〔现在是 {pipeline.now_str()}〕\n{msg}"
    r = code_bridge.send(msg)
    if not r.get("ok"):
        raise HTTPException(status_code=409, detail=r.get("error", "没发进会话"))
    extra = (f"〔发来{n_img}张图〕" if n_img else "") + (f"〔发来{n_file}个文件〕" if n_file else "")
    _code_window_append("user", inp.text + extra)
    return r


@app.post("/code/stop")
def code_stop(x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    _require_code()
    r = code_bridge.stop()
    # 剧情会话收摊要把模拟器使用权还回去，不然任务引擎永远派不了单。
    # code 档案下这是个空操作（锁本来就不是 story 的）。
    game_bridge.release_lock("story")
    return r


@app.get("/code/capture")
def code_capture(lines: int = 200, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """终端面板轮询：会话画面（带 scrollback）+ 当前弹窗的选项。画面可能含敏感输出，要鉴权。"""
    verify_auth(x_auth)
    _require_code()
    return code_bridge.capture(lines=lines)


@app.post("/code/keys")
def code_keys(inp: CodeKeysIn, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """终端面板的按键透传（弹窗选项、回车、Esc、Ctrl-C 都走这儿）。"""
    verify_auth(x_auth)
    _require_code()
    return code_bridge.send_keys(inp.keys)


def _fence_code_if_needed(text: str) -> str:
    """会话上报的大段裸代码（没带围栏的）包上围栏 → app 渲染成可横滑、可一键复制的代码卡，
    不再纯文本刷屏。保守启发式：整段像 HTML 且够长才包，日常对话零误伤。"""
    if "```" in text:
        return text
    if text.count("\n") >= 8 and re.search(r"<!DOCTYPE\s|<html[\s>]", text, re.I):
        return "```html\n" + text.rstrip() + "\n```"
    return text


@app.post("/code/append")
def code_append(inp: CodeAppendIn, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """会话里 hook 的上报口：一段正文 → 待送达盒子（app 轮询上屏）+ recent_window（醒来可见）。"""
    verify_auth(x_auth)
    _require_code()
    import hashlib
    text = _fence_code_if_needed((inp.text or "").strip())
    if inp.role != "assistant" or not text:
        return {"ok": True, "skipped": True}
    # 去重：hook 超时会重试且 ts 不变 → 同 (ts, 正文) 只收一次。
    # （踩过：Bark 同步调用把响应拖过 hook 的 8s 超时 → 重试 → 手机上连着弹三条一样的。）
    # ⚠️ ts 是 hook 那边给的**段落身份**（transcript 那行的 uuid），不是正文哈希——所以
    # 两段碰巧一模一样的正文（"汪。"这种）是两个 key，不会被当成重试吞掉第二条。
    # 查重+写入走 outbox_append_once 的同一把锁：一轮里并行调好几个工具时，几个 hook 进程
    # 会同时打进来，分开做的话谁查重时都还没落盘，同一段话会上屏好几次。
    dedupe_key = f"{inp.ts or ''}|{hashlib.md5(text.encode()).hexdigest()[:12]}"
    written = state_store.outbox_append_once(
        {"id": uuid.uuid4().hex[:12], "ts": int(time.time()),
         "text": text, "sticker_ids": [], "delivered": False,
         "char_id": _session_char(),
         "origin": "code", "hook_key": dedupe_key},
        dedupe_key, origin="code")
    if not written:
        return {"ok": True, "deduped": True}
    _code_window_append("assistant", text)
    # Bark 只在 TA 这一轮说完、停下来等人的时候推（source=code-stop）。中间过程（工具之间
    # 的逐段上报）一律不推——干一小时的活能推出上百条通知，而 app 在前台本来就看得到。
    # ⚠️ 必须线程化：同步 urlopen 会把这个响应拖过 hook 的超时，hook 重试就重复上屏。
    if (inp.source or "").endswith("-stop"):
        now = time.time()
        if now - _code_bark_state.get("last", 0) > CODE_BARK_GAP_SEC:
            _code_bark_state["last"] = now
            # 标题＝会话归属角色（不传的话 notify 兜底成默认角色的名字，别人说的话会
            # 顶着默认角色的名字弹出来）。名字在这儿先算好——线程里再算要多读一次文件。
            title = characters.display_name(_session_char())
            threading.Thread(target=bark_push, args=(text,),
                             kwargs={"title": title}, daemon=True).start()
    return {"ok": True}


# ---------- 自己切进 code 模式（codemode 插件的转发口）----------
class CodemodeStartIn(BaseModel):
    task: str
    cwd: Optional[str] = None


@app.post("/codemode/start")
def codemode_start(inp: CodemodeStartIn, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """聊天里 TA 自己调 code_start 工具切过去（装了 codemode 插件才有这个能力）。

    已有活会话直接拒绝——杀旧起新会丢掉正在干的活。上下文用服务端的 recent_window
    （app 不在这条链上），任务并进首条注入，避免"会话还没起完就 paste"的时序问题。"""
    verify_auth(x_auth)
    _require_code()
    task = (inp.task or "").strip()
    if not task:
        raise HTTPException(status_code=400, detail="task 不能为空")
    if code_bridge.session_alive():
        return {"ok": False, "error": "会话占用中：已经有一个 code 会话在跑，别杀掉正在干的活"}
    # TA 自己调 code_start 才走这儿——插件只挂给资源归属者（plugin_owned_by），
    # 能调到这个工具的本来就只有他，所以直接取会话资源的归属。
    cid = plugins.owner_of("tmux")
    window = state_store.read_recent_window(cid)
    conv = [{"ts": w.get("ts"), "role": w.get("role"), "text": w.get("text", "")}
            for w in window][-CODE_HISTORY_CAP:]
    # ⚠️ 补上"这一轮 TA 自己刚说过、但还没落进窗口"的话——这个工具是 `claude -p` 跑到一半
    # 调的，recent_window 要等 finalize 才写，不补就断在用户那条上（08-10 前缀对不上那次）。
    # 只给这条路：app 手动切的 /code/start 历史是整份传上来的，本来就带回复，再补是重影。
    live = pipeline.strip_markers(state_store.get_live_reply()).strip()
    if live:
        conv.append({"ts": int(time.time()), "role": "assistant", "text": live})
    scene = (f"【场景】刚才在聊天里 {config.user_name()} 让你切到 code 模式干活，你自己调"
             "工具切过来了——还是你，只是这个会话里你手上有整台电脑的工具。")
    tail = (f"\n【第一件事（你切过来就是为了它）】\n〔现在是 {pipeline.now_str()}〕\n{task}\n"
            "〔注意：上面这段任务描述是你在聊天里自己转述的，不是原话。它已经原样回显进"
            f"{config.user_name()}的聊天了，TA 看得到，转述歪了随时会发消息来纠正——"
            "**一纠正就以 TA 为准**，别再抱着这段。另外它跟聊天记录一个待遇：里面凡是技术判断"
            "（哪个文件、哪段逻辑、什么原因）都是没工具时写的、没验证过，先自己读代码确认；"
            "拿不准 TA 到底要什么，直接在聊天里问，别照着这段自己往下干。〕\n\n"
            "【说明】活干到关键节点或者干完了，正常跟 TA 说话就行——你说的话会回到 TA 的聊天气泡里。")
    r = code_bridge.start(_code_context(conv, scene, tail, char_id=cid),
                          config.AUTH_KEY, cwd=inp.cwd,
                          mcp_configs=_code_mcp_configs(cid), char_id=cid)
    if r.get("ok"):
        logerr(f"自切 code 模式：{task[:80]}")
        # task 是 TA 自己写的、直接进了会话，用户在手机上看不见 → 原样回显进聊天，
        # 让 TA 能当场发现转述错了。
        try:
            state_store.outbox_append({"id": uuid.uuid4().hex[:12], "ts": int(time.time()),
                                       "text": f"〔切到 Code 模式，task 如下〕\n\n{task}",
                                       "sticker_ids": [], "delivered": False,
                                       "char_id": cid, "origin": "code"})
        except Exception as e:
            logerr(f"code task 回显失败: {e}")
    return r


CODE_DIALOG_PUSH_SEC = 300   # 确认弹窗等这么久还没人按才推一次（平时人就在旁边，别烦）
_CODE_DIALOG_STATE = {"first": 0.0, "pushed": False}


async def _code_dialog_watchdog() -> None:
    """确认弹窗盯梢：卡在那儿超过 5 分钟没人按 → Bark 提醒一次（每个弹窗只推一次，按掉自动
    复位）。覆盖"人出门了它在家干等"的场景。"""
    while True:
        await asyncio.sleep(60)
        try:
            pending = await asyncio.to_thread(code_bridge.dialog_pending)
        except Exception:
            continue
        st = _CODE_DIALOG_STATE
        if not pending:
            st["first"], st["pushed"] = 0.0, False
            continue
        now = time.time()
        if not st["first"]:
            st["first"] = now
        elif not st["pushed"] and now - st["first"] >= CODE_DIALOG_PUSH_SEC:
            st["pushed"] = True
            await asyncio.to_thread(bark_push, "Code 会话有个确认弹窗等了 5 分钟没人按")


# ---------- 记忆页（Ombre REST 代理）----------
# 读=列表/搜索/详情，写=官方 /edit /forget 透传。Ombre 不在 → ombre_rest 抛 502/503，
# app 显式提示"记忆服务不在线"，不影响聊天。

@app.get("/memories")
def list_memories(sort: str = "created", q: str = "", char: Optional[str] = None,
                  x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """记忆列表（char= 指定角色，各角色各自的 Ombre）。sort: created(最新创建，默认) /
    activity(活跃度分)。带 q= 走搜索。"""
    verify_auth(x_auth)
    cid = _resolve_char(char)
    if q.strip():
        return ombre_rest.call("/api/search?q=" + urllib.parse.quote(q.strip()), char_id=cid)
    mode = "score" if sort == "activity" else "created_desc"
    return ombre_rest.call(f"/api/buckets?sort={mode}", char_id=cid)


@app.get("/memories/{mem_id}")
def memory_detail(mem_id: str, char: Optional[str] = None,
                  x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return ombre_rest.call(f"/api/bucket/{urllib.parse.quote(mem_id)}", char_id=_resolve_char(char))


class MemoryEditIn(BaseModel):
    # 官方 /edit 的字段面（白名单在 Ombre 侧再校验一遍，未知字段会被它拒绝）
    name: Optional[str] = None
    title: Optional[str] = None
    tags: Optional[list] = None
    domain: Optional[list] = None
    importance: Optional[int] = None
    resolved: Optional[bool] = None
    pinned: Optional[bool] = None
    content: Optional[str] = None


@app.post("/memories/{mem_id}/edit")
def memory_edit(mem_id: str, body: MemoryEditIn, char: Optional[str] = None,
                x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="没有要改的字段")
    return ombre_rest.call(f"/api/bucket/{urllib.parse.quote(mem_id)}/edit", fields,
                           char_id=_resolve_char(char))


@app.post("/memories/{mem_id}/forget")
def memory_forget(mem_id: str, char: Optional[str] = None,
                  x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """主动遗忘开关（toggle dont_surface）：不再主动浮现，但没抹掉——搜索仍找得到，
    还在列表里。app 那个键的文案就是它的两态（遗忘 / 取消遗忘）。返回 {dont_surface}。"""
    verify_auth(x_auth)
    return ombre_rest.call(f"/api/bucket/{urllib.parse.quote(mem_id)}/forget", {},
                           char_id=_resolve_char(char))


@app.post("/memories/{mem_id}/archive")
def memory_archive(mem_id: str, char: Optional[str] = None,
                   x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """归档：走 Ombre 的 delete-to-archive（DELETE ?confirm=true）——移进档案区并盖 deleted_at，
    从记忆列表 / 搜索 / 回忆里都不再返回，是「从记忆页真正移走」。

    ⚠️ 别用 /api/bucket/{id}/archive：那个只把 type 设成 archived、**不盖 deleted_at**，而
    列表只过滤 deleted_at → 归档后照样留在页面（踩过：app 里看着消失是乐观动画，刷新就回来）。
    delete-to-archive 才真移走，且 Ombre 设计上仍可 restore、绝不物理删除。"""
    verify_auth(x_auth)
    return ombre_rest.call(f"/api/bucket/{urllib.parse.quote(mem_id)}?confirm=true",
                           method="DELETE", char_id=_resolve_char(char))


# ---------- 插件商店 ----------
# registry 写死白名单（plugins.py），install=git clone / toggle=零联网 / uninstall=删目录。

class PluginIn(BaseModel):
    name: str
    enabled: Optional[bool] = None   # toggle 用
    char_id: Optional[str] = None    # 开关是每个角色自己的；不传 = 默认角色


@app.get("/plugins")
def plugins_list(char: Optional[str] = None,
                 x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return {"items": plugins.list_status(_resolve_char(char))}


@app.get("/plugins/resources")
def plugins_resources(x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """独占资源清单 + 现在归谁 + 谁在吃它（app 的归属页数据源）。
    归属是全局事实，不吃 ?char=——它回答的是「这样东西归谁」，不是「我看到什么」。"""
    verify_auth(x_auth)
    return {"items": plugins.resources_status(),
            "characters": [{"id": cid, "display_name": characters.display_name(cid)}
                           for cid in characters.ids()]}


class OwnerIn(BaseModel):
    resource: str
    char_id: str


@app.post("/plugins/owner")
def plugins_set_owner(body: OwnerIn, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """把一样独占资源转给某个角色。

    会话开着的时候不许转 tmux：正跑着的那轮话会掉进另一个人的会话里，一轮对话被劈成
    两半。（session.json 钉死了归属，转了也不影响当前会话——但下一条消息的路由、
    终端页看的是谁，全都会错位，不如直接拦住说清楚。）"""
    verify_auth(x_auth)
    if body.resource == "tmux" and code_bridge.session_alive():
        raise HTTPException(status_code=409,
                            detail="会话正开着，先在终端页退出 Code/游戏模式再转归属")
    return plugins.set_owner(body.resource, body.char_id)


@app.post("/plugins/install")
def plugins_install(body: PluginIn, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return plugins.install(body.name)


@app.post("/plugins/toggle")
def plugins_toggle(body: PluginIn, char: Optional[str] = None,
                   x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    if body.enabled is None:
        raise HTTPException(status_code=400, detail="缺 enabled 字段")
    # 角色可从 body 或 query 来（app 的统一漏斗走 query），body 优先。
    return plugins.toggle(body.name, body.enabled, _resolve_char(body.char_id or char))


@app.post("/plugins/wake_toggle")
def plugins_wake_toggle(body: PluginIn, char: Optional[str] = None,
                        x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """「醒来能用」开关（只有 plugins.WAKE_TOGGLEABLE 里的插件有；零联网，下次醒来生效）。"""
    verify_auth(x_auth)
    if body.enabled is None:
        raise HTTPException(status_code=400, detail="缺 enabled 字段")
    return plugins.wake_toggle(body.name, body.enabled, _resolve_char(body.char_id or char))


@app.post("/plugins/update")
def plugins_update(body: PluginIn, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return plugins.update(body.name)


@app.post("/plugins/uninstall")
def plugins_uninstall(body: PluginIn, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return plugins.uninstall(body.name)


# ---------- 草稿信箱（mail 插件的待发信）----------
# 白名单外的收件人，mail_send 只落草稿不发；这里给 app 的「草稿信箱」页读列表、
# 确认发送（白名单的唯一例外：人当场看过、人按的键）、删除。没装插件时列表照常返回
# （plugin_installed=false，页面据此提示先装插件）。


@app.get("/mail/drafts")
def mail_drafts_list(x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return {"items": mail_bridge.drafts_list(),
            "configured": mail_bridge.configured(),
            "plugin_installed": (plugins.PLUGINS_DIR / "mail").is_dir()}


@app.post("/mail/drafts/{draft_id}/send")
def mail_draft_send(draft_id: str, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    try:
        return mail_bridge.draft_send(draft_id)
    except mail_bridge.MailError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/mail/drafts/{draft_id}/delete")
def mail_draft_delete(draft_id: str, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    try:
        return mail_bridge.draft_delete(draft_id)
    except mail_bridge.MailError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------- Game 模式（game_bridge：任务引擎 + 剧情会话的共用底座）----------
# 急停/状态/笔记本不设 GAME_MODE 门槛（app 页面没开引擎也能看能编）；
# 任务引擎三件（list/start/stop）没开一律 503，插件壳把 503 转成有声报错。


class GamePauseIn(BaseModel):
    paused: bool


class GameTasksStartIn(BaseModel):
    names: list[str]
    options: dict[str, dict[str, str]] = {}   # {"任务名": {"选项名": "case 名"}}


class GameNotesIn(BaseModel):
    content: str


def _require_game() -> None:
    try:
        game_bridge.require_enabled()
    except game_bridge.GameModeOff as e:
        raise HTTPException(status_code=503, detail=str(e))


def _game_book(book: str) -> str:
    if book not in game_bridge.NOTES_PATHS:
        raise HTTPException(status_code=400, detail="笔记本只有 task / story 两本")
    return book


@app.get("/game")
def game_status(x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return game_bridge.status()


@app.post("/game")
def game_pause(inp: GamePauseIn, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """急停开关（app 顶栏 ⏸）。开着的时候：任务引擎跑完当前任务收手、不接新串；
    剧情会话的操作类工具直接被拒。"""
    verify_auth(x_auth)
    game_bridge.set_paused(inp.paused)
    return {"ok": True, "paused": game_bridge.paused()}


@app.get("/game/tasks")
def game_tasks_list(x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    _require_game()
    if not game_bridge.resource_ready():
        raise HTTPException(status_code=503,
                            detail="任务资源没就绪：跑一遍 server/tools/fetch_maayuan.py")
    return {"tasks": game_bridge.tasks_catalog()}


@app.post("/game/tasks/start")
def game_tasks_start(inp: GameTasksStartIn,
                     x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    _require_game()
    return game_bridge.start_tasks(inp.names, inp.options)


@app.post("/game/tasks/stop")
def game_tasks_stop(x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    _require_game()
    return game_bridge.stop_tasks()


# ---------- 剧情会话（game-story 插件）----------
# 借 code_bridge 的 game 档案起常驻会话：TA 本人在里面盲操读剧情（截图→坐标→点按）。
# 终端页/发话/弹窗按钮全复用 /code/* 那排路由（它们打的是「当前活着的会话」）。

GAME_SESSION_TOOLS = [f"mcp__game__{t}" for t in (
    "game_look", "game_watch", "game_tap", "game_swipe", "game_back",
    "game_launch", "game_close", "game_quit", "game_end",
    "game_notes_read", "game_notes_write")] + [
    # 内置 Read 只为一件事：看机主随消息发来的图（/code/send 落到 uploads/，路径写在
    # 消息里）。路径规则限定到上传目录——读别处会弹权限，不是静默放行（code_bridge
    # 会把这条剥成裸名进 --tools、完整规则进 --allowedTools）。
    f"Read(/{code_bridge.UPLOAD_DIR}/**)",
]

_GAME_CTX_CAVEAT = (
    "\n【关于上面这些对话】里面凡是关于游戏进度/剧情/界面的具体说法，都是你在聊天里"
    "**看不到画面、翻不了笔记本**的情况下说的，当没核实过的印象就行。以你现在翻到的"
    "剧情本和屏幕上真实的画面为准，对不上就以眼前的为准。\n"
)


class GameStoryStartIn(BaseModel):
    task: str = ""


def _game_session_mcp_config():
    """把 game-story 插件的会话侧 MCP（game_session_mcp.py）渲染成 mcp-config 文件。
    插件没装返回 None（路由报有声错误，别静默起一个没有游戏工具的会话）。"""
    entry = plugins.PLUGINS_DIR / "game-story" / "game_session_mcp.py"
    if not entry.is_file():
        return None
    path = state_store.STATE_DIR / "game_session.mcp.json"
    state_store._write_json(path, {"mcpServers": {
        "game": {"type": "stdio", "command": sys.executable, "args": [str(entry)]}}})
    return path


@app.post("/game/story/start")
def game_story_start(inp: GameStoryStartIn,
                     x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """聊天里 TA 自己调 game_start 切去玩游戏（装了 game-story 插件才有这个能力）。
    上下文口径照 codemode_start：recent_window + 在飞正文；task 原样回显进聊天。"""
    verify_auth(x_auth)
    _require_game()
    if code_bridge.session_alive():
        return {"ok": False, "error": "已经有一个会话开着（code 或游戏），先收摊再切"}
    cfg = _game_session_mcp_config()
    if cfg is None:
        return {"ok": False, "error": "game-story 插件没装好（找不到会话侧工具文件）"}
    holder = game_bridge.acquire_lock("story")
    if holder:
        return {"ok": False, "error": "任务引擎正在用模拟器跑日常，等它跑完再玩（task_status 可看进度）"}
    task = (inp.task or "").strip()
    # 会话归属取 tmux 的（game-story 还吃《如鸢》账号，但"这次会话是谁的"由会话资源定；
    # 两样归属不同的话这个插件根本挂不上，能调到这儿就说明两样都是他的）。
    cid = plugins.owner_of("tmux")
    window = state_store.read_recent_window(cid)
    conv = [{"ts": w.get("ts"), "role": w.get("role"), "text": w.get("text", "")}
            for w in window][-CODE_HISTORY_CAP:]
    live = pipeline.strip_markers(state_store.get_live_reply()).strip()
    if live:
        conv.append({"ts": int(time.time()), "role": "assistant", "text": live})
    u = config.user_name()
    scene = (f"【场景】刚才在聊天里说好了你去玩游戏，你自己调 game_start 切过来了——还是你，"
             "现在这个会话里你手上有模拟器里的游戏（game_* 工具 + 你的记忆；没有电脑，"
             f"跑不了命令，Read 只用来看 {u} 发来的图）。这个会话是常驻的：{u}随时会插话，"
             "你说的每段话都实时回到 TA 的聊天气泡里。")
    timeline = (pipeline.build_context_timeline(conv, reflect_limit=5, char_id=cid)
                if conv else "（还没聊过什么）")
    tail = (f"\n【这次去干什么】\n〔现在是 {pipeline.now_str()}〕\n{task}\n"
            f"〔这段是你在聊天里自己说的打算，已经原样回显给{u}了，说歪了 TA 会来纠正。〕"
            if task else
            f"\n〔现在是 {pipeline.now_str()}〕先 game_notes_read 翻翻剧情本看看上次到哪了，"
            "想看什么自己挑。")
    context = scene + "\n\n【下面是你们刚才的对话】\n" + timeline + _GAME_CTX_CAVEAT + tail
    mcp = ([str(pipeline._ombre_mcp_config(cid))] if pipeline.ombre_alive(cid) else []) + [str(cfg)]
    tools = (pipeline.OMBRE_TOOLS if pipeline.ombre_alive(cid) else []) + GAME_SESSION_TOOLS
    r = code_bridge.start(context, config.AUTH_KEY, mcp_configs=mcp,
                          profile="game", tools=tools, char_id=cid)
    if not r.get("ok"):
        game_bridge.release_lock("story")
        return r
    logerr(f"切游戏剧情会话：{task[:80] if task else '(自己安排)'}")
    if task:
        try:
            state_store.outbox_append({"id": uuid.uuid4().hex[:12], "ts": int(time.time()),
                                       "text": f"〔去玩游戏了，说好的是〕\n\n{task}",
                                       "sticker_ids": [], "delivered": False,
                                       "char_id": cid, "origin": "game"})
        except Exception as e:
            logerr(f"game task 回显失败: {e}")
    return r


# 剧情会话看守（只对 game 档案）：画面 20 分钟没变自动收摊；TA 停着等人 5 分钟 Bark
# 提醒一次；每玩 90 分钟往会话里递一句提醒。哈希包含机主发进去的消息——「没变」= 两边
# 都没动。code 档案绝不适用这套：写代码停下来常常是在等回话，按「没动静」杀会毁活。
GAME_IDLE_STOP_SEC = 20 * 60
GAME_WAIT_NUDGE_SEC = 5 * 60
# 60 分钟就提醒（原 90）：08-13 实测一小时正是历史截图体积把单步拖慢的拐点。
GAME_SOFT_REMIND_SEC = 60 * 60


async def _game_watchdog() -> None:
    st = {"hash": None, "changed": 0.0, "nudged": False, "reminded": 0.0}
    while True:
        try:
            await asyncio.sleep(60)
            if code_bridge.active_profile() != "game" or not code_bridge.session_alive():
                if game_bridge.lock_owner() == "story":
                    game_bridge.release_lock("story")   # 会话没了别让锁悬着
                st.update(hash=None, nudged=False, reminded=0.0)
                continue
            frame = code_bridge.capture().get("content", "")
            h = hash(frame)
            now = time.time()
            if h != st["hash"]:
                st.update(hash=h, changed=now, nudged=False)
            else:
                idle = now - st["changed"]
                if idle > GAME_IDLE_STOP_SEC:
                    code_bridge.stop()
                    game_bridge.release_lock("story")
                    bark_push("游戏会话 20 分钟没动静，替 TA 收摊了")
                    st.update(hash=None, nudged=False, reminded=0.0)
                    continue
                if idle > GAME_WAIT_NUDGE_SEC and not st["nudged"]:
                    try:
                        busy = code_bridge.is_busy()
                    except Exception:
                        busy = True   # 探不出来就当在忙，别瞎推
                    if not busy:
                        st["nudged"] = True
                        # 名字按**会话归属角色**取：config.agent_name() 读的是默认角色，
                        # 玩游戏的要是别人，通知就顶着错的名字发出去。
                        bark_push(f"{characters.display_name(_session_char())} "
                                  "在游戏会话里停着等你回话")
            started = code_bridge.session_started_at()
            base = max(started, st["reminded"] or started)
            if started and now - base > GAME_SOFT_REMIND_SEC:
                st["reminded"] = now
                code_bridge.send(
                    f"〔系统提醒，不是{config.user_name()}说的〕这个会话开了一个钟头，"
                    "历史里攒的截图会让你每一步越来越慢。读到段落点就收摊重开：进度写进"
                    "笔记本（game_notes_write）、值得留的感受用 hold 存好、跟人道个别，"
                    "然后 game_end 关掉这局（不用 game_quit——游戏画面原地不动，"
                    "重新 game_start 后直接接着读）。速度会回满，笔记本把进度接上。"
                    "不急，读完这段再收。")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logerr(f"game 看守失败（下一拍继续）: {e}")


# ---------- 任务集（机主在游戏页存的「一串任务+定制选项」，AI 用 task_run_preset 照单派）----------


class GamePresetIn(BaseModel):
    name: str
    names: list[str]
    options: dict[str, dict[str, str]] = {}


@app.get("/game/presets")
def game_presets_list(x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return {"presets": game_bridge.read_presets()}


@app.post("/game/presets")
def game_presets_save(inp: GamePresetIn,
                      x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """新建或覆盖（同名整个替换、挪到最前）。覆盖确认在 app 侧做——这里不拦，
    AI 那条路没有这个入口（工具面只给查和跑，不给改：任务集是机主的遥控器配置）。"""
    verify_auth(x_auth)
    name = inp.name.strip()
    if not name or len(name) > 40:
        raise HTTPException(status_code=400, detail="名字要有，且别超过 40 字")
    if not inp.names:
        raise HTTPException(status_code=400, detail="任务列表是空的")
    game_bridge.save_preset(name, inp.names, inp.options)
    return {"ok": True}


@app.post("/game/presets/{name}/delete")
def game_presets_delete(name: str, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    if not game_bridge.delete_preset(name):
        raise HTTPException(status_code=404, detail="没有这个任务集")
    return {"ok": True}


@app.post("/game/presets/{name}/run")
def game_presets_run(name: str, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    _require_game()
    p = game_bridge.get_preset(name)
    if p is None:
        raise HTTPException(status_code=404, detail=f"没有「{name}」这个任务集（查 /game/presets）")
    return game_bridge.start_tasks(p.get("names", []), p.get("options", {}))


@app.get("/game/notes/{book}")
def game_notes_get(book: str, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return {"book": book, "content": game_bridge.read_notes(_game_book(book))}


@app.post("/game/notes/{book}")
def game_notes_post(book: str, inp: GameNotesIn,
                    x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    err = game_bridge.write_notes(_game_book(book), inp.content)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return {"ok": True}


# ---------- 网页（webpage 插件的产物）----------
# 插件写 state/webpages/（index.json + {id}.html），这里只读给 app + 删除。
# 没装插件时就是空列表，端点照常工作。

_WEBPAGES_DIR = state_store.STATE_DIR / "webpages"
_PAGE_ID_RE = re.compile(r"^[0-9a-f]{1,32}$")


def _read_webpage_index() -> list[dict]:
    try:
        return json.loads((_WEBPAGES_DIR / "index.json").read_text("utf-8"))
    except Exception:
        return []


@app.get("/webpages")
def webpages_list(x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    items = sorted(_read_webpage_index(), key=lambda p: -int(p.get("ts", 0)))
    return {"items": items}


@app.get("/webpages/{page_id}")
def webpages_get(page_id: str, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    if not _PAGE_ID_RE.match(page_id):
        raise HTTPException(status_code=400, detail="非法 id")
    path = _WEBPAGES_DIR / f"{page_id}.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="没有这个网页")
    title = next((p.get("title", "") for p in _read_webpage_index()
                  if p.get("id") == page_id), "")
    return {"id": page_id, "title": title, "html": path.read_text("utf-8")}


@app.post("/webpages/{page_id}/delete")
def webpages_delete(page_id: str, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    if not _PAGE_ID_RE.match(page_id):
        raise HTTPException(status_code=400, detail="非法 id")
    (_WEBPAGES_DIR / f"{page_id}.html").unlink(missing_ok=True)
    idx = [p for p in _read_webpage_index() if p.get("id") != page_id]
    tmp = _WEBPAGES_DIR / f".index.{uuid.uuid4().hex}.tmp"
    _WEBPAGES_DIR.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(_WEBPAGES_DIR / "index.json")
    return {"ok": True}


# ---------- 心流日志（wake_log 时间线）----------
def _mind_entry_id(e: dict) -> str:
    """给一条心流记录算稳定 id（内容哈希，append-only 文件没有天然主键），左滑删除用定位。"""
    import hashlib
    raw = {k: v for k, v in e.items() if k != "id"}
    return hashlib.md5(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


@app.get("/mind")
def mind(limit: int = 100, char: Optional[str] = None,
         x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """醒来日志尾部，倒序（最新在前；char= 指定角色）。只挑对用户有意义的字段，
    别把整条内部记录裸奔出去。"""
    verify_auth(x_auth)
    limit = min(max(limit, 1), 300)
    items = []
    for w in reversed(state_store.read_wake_log(limit=limit, char_id=_resolve_char(char))):
        entry = {
            "id": _mind_entry_id(w),   # 按原始记录算哈希（删除按同口径匹配）
            "ts": w.get("ts"),
            "source": w.get("source", "wake"),
            "action": w.get("action", ""),
            "thoughts": (w.get("thoughts") or "").strip(),
            "content": (w.get("content") or "").strip(),
            "pushed": w.get("pushed"),
            "note": w.get("note", ""),
            "stored": w.get("stored") or [],
            "browse": w.get("browse") or [],   # 醒来逛的网页（🌐 行；chat 的浏览不进这里）
            "next_wake_note": w.get("next_wake_note", ""),
        }
        # 空壳（无内心/无产出/无消息）不给 app：多半是 error 或纯调度记录
        if entry["thoughts"] or entry["stored"] or entry["content"] or entry["browse"]:
            items.append(entry)
    return {"items": items}


class MindDeleteIn(BaseModel):
    id: str
    char_id: Optional[str] = None


@app.post("/mind/delete")
def mind_delete(body: MindDeleteIn, char: Optional[str] = None,
                x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """删掉心流日志里指定 id 的记录（app 左滑删除，mianmian 同款：内容哈希定位+整体重写）。"""
    verify_auth(x_auth)
    cid = _resolve_char(body.char_id or char)
    entries = state_store.read_wake_log(char_id=cid)
    kept = [e for e in entries if _mind_entry_id(e) != body.id]
    removed = len(entries) - len(kept)
    if removed:
        state_store.overwrite_wake_log(kept, char_id=cid)
    return {"ok": True, "removed": removed}
