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
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import urllib.parse

import config
import ombre_rest
import plugins
import pipeline
import sse
import state_store
import wake
from notify import bark_push, logerr
from pipeline import Message

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """启动 wake 调度器（on_event 已被 FastAPI 弃用，用 lifespan）。"""
    task = asyncio.create_task(wake.scheduler_loop()) if config.PROACTIVE_ENABLED else None
    yield
    if task:
        task.cancel()


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
    session_id: Optional[str] = None   # 仅用于 app 记账，后端不依赖它记忆
    stickers: Optional[list[StickerInfo]] = None  # 表情库清单(id+描述)，供模型挑着发/改描述
    client_req_id: Optional[str] = None  # 断连补投关联 id：rescue 条目带回给 app 替换半截气泡
    images: Optional[list[ImageInput]] = None  # 附给"最新这条"的图片，带图走多模态
    files: Optional[list[FileInput]] = None    # 附给"最新这条"的文件（PDF/文本/docx），同上


class StoredItem(BaseModel):
    tool: str
    text: str


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


def _prepare_chat(req: ChatRequest) -> tuple[str, dict]:
    """校验 + 表情清单落盘 + 拼 prompt + handle 映射。失败抛 HTTPException（流开始前，能正常返 4xx）。"""
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages 不能为空")
    last = req.messages[-1]
    if last.role != "user" or not last.text.strip():
        raise HTTPException(status_code=400, detail="最后一条必须是非空的用户消息")
    # 表情清单：转成朴素 dict，持久化一份（醒来时后端不在 app 手里也能挑表情发）。
    catalog = pipeline.to_catalog(req.stickers)
    if catalog:
        try:
            state_store.write_sticker_catalog(catalog)
        except Exception as e:
            logerr(f"写 sticker_catalog 失败: {e}")
    return pipeline.build_prompt(req.messages, catalog), pipeline.sticker_handle_map(catalog)


def _snapshot_incoming_window(req: ChatRequest) -> None:
    """轮一开始就把眼下的对话（含刚收到的 user 消息）写进 recent_window——
    这轮可能跑很久，中途 wake 醒来不该只看到上一轮的世界。收尾 finalize 用带回复的完整版覆盖。
    ⚠️ 迷你历史护栏在这里也要设：不然 1 条历史的 curl 请求在轮开始就把 300 条窗口冲空，
    finalize 里那道同名护栏读到的已是被冲掉的窗口，形同虚设。"""
    try:
        snap = [{"role": m.role, "text": m.text, "ts": m.ts} for m in req.messages]
        with state_store.WINDOW_LOCK:
            cur = state_store.read_recent_window()
            if len(snap) < 5 and len(cur) > len(snap):
                return   # 短历史请求不覆盖丰满窗口；新消息由 finalize 的追加分支补进
            state_store.write_recent_window(snap)
    except Exception as e:
        logerr(f"写 recent_window(轮开始) 失败: {e}")


def finalize_chat_reply(reply: str, stored: list[dict], req: ChatRequest,
                        handle_to_id: dict) -> ChatResponse:
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
            sched = state_store.read_schedule()
            sched["next_wake_at"] = at
            state_store.write_schedule(sched)
        next_wake_hint = pipeline.next_wake_note(next_raw, at)
        logerr(f"聊天里定了下次醒来：{next_raw} → {pipeline.fmt_ts(at)}")

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
            cur = state_store.read_recent_window()
            if len(req.messages) < 5 and len(cur) > len(snap):
                cur.extend(snap[-2:])   # 只追加这轮的一来一回
                state_store.write_recent_window(cur)
            else:
                state_store.write_recent_window(snap)
    except Exception as e:
        logerr(f"写 recent_window 失败: {e}")

    # 聊天里存/改的记忆也记进 wake_log（source=chat，无 thoughts 不进时间线）：
    # 醒来的"别重复存"清单靠它才看得到聊天里已存过的。
    # 网页操作不进日志（眠眠定）：它有聊天卡片 + HTML 文件列表两个展示面，
    # 心流日志只记记忆类操作，防重复清单也不被网页标题污染。
    mem_stored = [s for s in stored if s.get("tool") != "webpage"]
    if mem_stored:
        try:
            state_store.append_wake_log({"ts": int(time.time()), "time": pipeline.now_str(),
                                         "source": "chat", "stored": mem_stored})
        except Exception as e:
            logerr(f"记 chat stored 失败: {e}")

    return ChatResponse(
        reply=reply,
        session_id=req.session_id or str(uuid.uuid4()),
        stored=[StoredItem(**s) for s in stored],
        sticker_sends=sticker_sends,
        desc_updates=[DescUpdate(**u) for u in desc_updates],
        next_wake_hint=next_wake_hint,
    )


# ---------- 路由 ----------
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """非流式聊天（流式的回退路，两条路收尾共用 finalize 保证一致）。"""
    verify_auth(x_auth)
    prompt, handle_to_id = _prepare_chat(req)
    _snapshot_incoming_window(req)
    # 文件转 block 在调用前做：类型不支持/解不开在这里 400，不进子进程。
    file_blocks = [_file_to_block(f) for f in (req.files or [])]
    wake.chat_turn_begin()
    try:
        if req.images or file_blocks:
            reply, stored = pipeline.call_claude_multimodal(prompt, req.images or [],
                                                            file_blocks=file_blocks)
        else:
            reply, stored = pipeline.call_claude(prompt)
    finally:
        wake.chat_turn_end()
    return finalize_chat_reply(reply, stored, req, handle_to_id)


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
    prompt, handle_to_id = _prepare_chat(req)   # 校验在流开始前，能正常返 4xx
    file_blocks = [_file_to_block(f) for f in (req.files or [])]   # 同上：4xx 趁早
    _snapshot_incoming_window(req)

    def finalize(reply: str, stored: list[dict]) -> dict:
        return jsonable_encoder(finalize_chat_reply(reply, stored, req, handle_to_id))

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
                                                         file_blocks=file_blocks):
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
                    wake.chat_turn_end()
                    q.put_nowait(None)

            wake.chat_turn_begin()
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
                        state_store.outbox_append({"id": uuid.uuid4().hex[:12], "ts": int(time.time()),
                                                   "text": text, "sticker_ids": stickers,
                                                   "delivered": False, "origin": "chat_rescue",
                                                   "req_id": rid})
                        bark_push(text if text else "（发来了表情）")
                        logerr("/chat/stream 客户端断了，完整回复已补投 outbox")
                    else:
                        state_store.outbox_append({"id": uuid.uuid4().hex[:12], "ts": int(time.time()),
                                                   "text": "", "error": True,
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
def get_settings(x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return state_store.load_settings()


@app.post("/settings")
def post_settings(body: SettingsIn, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    _validate_hhmm(body.active_start)
    _validate_hhmm(body.active_end)
    if body.day_freq not in ("low", "mid", "high") or body.night_freq not in ("low", "mid", "high"):
        raise HTTPException(status_code=400, detail="freq 需为 low/mid/high")
    for name in ("daily_max", "min_interval_min", "quiet_after_user_min"):
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
    state_store.save_settings(saved)
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


# ---------- 记忆页（Ombre REST 代理）----------
# 读=列表/搜索/详情，写=官方 /edit /forget 透传。Ombre 不在 → ombre_rest 抛 502/503，
# app 显式提示"记忆服务不在线"，不影响聊天。

@app.get("/memories")
def list_memories(sort: str = "created", q: str = "",
                  x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """记忆列表。sort: created(最新创建，默认) / activity(活跃度分)。带 q= 走搜索。"""
    verify_auth(x_auth)
    if q.strip():
        return ombre_rest.call("/api/search?q=" + urllib.parse.quote(q.strip()))
    mode = "score" if sort == "activity" else "created_desc"
    return ombre_rest.call(f"/api/buckets?sort={mode}")


@app.get("/memories/{mem_id}")
def memory_detail(mem_id: str, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return ombre_rest.call(f"/api/bucket/{urllib.parse.quote(mem_id)}")


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
def memory_edit(mem_id: str, body: MemoryEditIn,
                x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="没有要改的字段")
    return ombre_rest.call(f"/api/bucket/{urllib.parse.quote(mem_id)}/edit", fields)


@app.post("/memories/{mem_id}/forget")
def memory_forget(mem_id: str, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return ombre_rest.call(f"/api/bucket/{urllib.parse.quote(mem_id)}/forget", {})


# ---------- 插件商店 ----------
# registry 写死白名单（plugins.py），install=git clone / toggle=零联网 / uninstall=删目录。

class PluginIn(BaseModel):
    name: str
    enabled: Optional[bool] = None   # toggle 用


@app.get("/plugins")
def plugins_list(x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return {"items": plugins.list_status()}


@app.post("/plugins/install")
def plugins_install(body: PluginIn, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return plugins.install(body.name)


@app.post("/plugins/toggle")
def plugins_toggle(body: PluginIn, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    if body.enabled is None:
        raise HTTPException(status_code=400, detail="缺 enabled 字段")
    return plugins.toggle(body.name, body.enabled)


@app.post("/plugins/update")
def plugins_update(body: PluginIn, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return plugins.update(body.name)


@app.post("/plugins/uninstall")
def plugins_uninstall(body: PluginIn, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    verify_auth(x_auth)
    return plugins.uninstall(body.name)


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
def mind(limit: int = 100, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """醒来日志尾部，倒序（最新在前）。只挑对用户有意义的字段，别把整条内部记录裸奔出去。"""
    verify_auth(x_auth)
    limit = min(max(limit, 1), 300)
    items = []
    for w in reversed(state_store.read_wake_log(limit=limit)):
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
            "next_wake_note": w.get("next_wake_note", ""),
        }
        # 空壳（无内心/无产出/无消息）不给 app：多半是 error 或纯调度记录
        if entry["thoughts"] or entry["stored"] or entry["content"]:
            items.append(entry)
    return {"items": items}


class MindDeleteIn(BaseModel):
    id: str


@app.post("/mind/delete")
def mind_delete(body: MindDeleteIn, x_auth: Optional[str] = Header(default=None, alias="X-Auth")):
    """删掉心流日志里指定 id 的记录（app 左滑删除，mianmian 同款：内容哈希定位+整体重写）。"""
    verify_auth(x_auth)
    entries = state_store.read_wake_log()
    kept = [e for e in entries if _mind_entry_id(e) != body.id]
    removed = len(entries) - len(kept)
    if removed:
        state_store.overwrite_wake_log(kept)
    return {"ok": True, "removed": removed}
