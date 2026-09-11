"""chat_loop：常驻聊天引擎（PLAN_sdk S2/PR10a——agent-sdk 每角色一条 session + forge）。

架构（设计定稿在 PLAN_sdk §10 S2 两份设计稿）：
- 权威=手机消息历史（每次请求全量带 req.messages），transcript 只是铸出来的缓存，
  recent_window 照旧只服务 wake，不参与这里的任何判断。
- **判脏=发送前比对**：已铸账（本 session 铸过/追加过的 (role, hash) 序列）vs 本次
  请求带来的历史。一致=纯追加只注入新消息；分歧=从权威整体重铸（截尾语义 §6）。
  脏不是状态机，是比对结果——没有跨轮标记，也就没有标记丢失/竞态。
- **任何一轮没有正常收尾（finalize 没跑到）→ 关 session，下次发送惰性重起+重铸**。
  比 PR9 的「轮死≠session 死」保守一档，但零暧昧状态：账和 transcript 永不可能
  悄悄分叉——所有异常都收敛到「下一条多等几秒」。
- SSE 协议零改动：把 SDK 类型化消息转回 CLI stream-json 的 dict 形状，喂给现有
  sse.translate_events（text_break/MarkerStreamFilter/StoredCollector 原样复用）。

红线：铸造输入只有权威窗口（req.messages）——绝不读回 transcript；tick/注入
包装文字（时间感/叮嘱）不进账，重铸时按权威原文重渲染，包装自然脱落。
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Optional

from claude_agent_sdk import (
    AssistantMessage,
    CanUseToolShadowedWarning,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import StreamEvent, ToolResultBlock, UserMessage

# allowed_tools（只读直放面）和 can_use_tool（写类审批面）组合用是故意的
# （PLAN_native §1）：遮蔽只影响**进了**白名单的工具，写类四件不进名单，
# 落 ask 路由到回调。SDK 的提醒对这套形状是误报，静掉。
import warnings
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

import config
import forge
import session_mgr
import sse
import state_store
from forge import estimate_tokens

SCENE = "chat"
LEDGER_DIR = state_store.STATE_DIR / "chat_sessions"
# 浅层错位容忍：请求发出时上一轮回复还没落进 app 历史（连发竞态）/wake 气泡还没
# 被 app 拉走——账尾多出的**纯 assistant** 条目不算脏。超过这个深度按脏处理。
# 泵开着时容忍按「未拉走的 outbox 条数」上浮（PR13：点评连发快过 TA 拉取时不误脏）。
STALE_TOLERANCE = 8

# ---- game 泵（PLAN_sdk 设计稿三：game 不是轮来源，是他自己时间里的一件事）----
# 泵状态 handle.meta["game_pump"]={seg_id,note,start_ts,deliver}；tick 轮
# turn_kind="wake"（存在论归他自己的时间），注入「·」谁都没见过、不进账不进铸造。
# 常量与 game_loop 同一套 env（回退路共用口径）。
GAME_TICK_PAUSE = float(os.environ.get("GAME_TICK_PAUSE", "2") or "2")
GAME_TICK_PROMPT = "·"
# 每 N 个 tick 报一次时——「·」不带时间，一场游戏几十上百轮下来他手上唯一的钟是
# 开场那一句，越打越偏。逐轮报时是浪费（tick 才 1 个字），每 10 轮一次≈几分钟一个
# 锚点，够了。重铸时这些注入照旧整条蒸发（不进账），时间由 _stamp_times 从
# assistant 自己的 ts 补回来。
GAME_TICK_STAMP_EVERY = int(os.environ.get("GAME_TICK_STAMP_EVERY", "10") or "10")
# 连续两条 assistant 隔这么久 = 中间有过一个没进账的注入槽（醒来/泵），重铸时补锚。
# 10 分钟：回 TA 的话是秒级落地，自己开口最短也是分钟级往上（AUTO_GAP_MIN_SEC=45min），
# 中间这一档留得很宽，宁可漏补不可错补（错补＝把"回她的话"标成"自己开的口"）。
SELF_OPEN_GAP_SEC = int(os.environ.get("SELF_OPEN_GAP_SEC", "600") or "600")
GAME_REOPEN_SHOTS_N = int(os.environ.get("GAME_REOPEN_SHOTS", "70") or "70")
GAME_REOPEN_COOLDOWN = 5 * 60
GAME_K_SHOTS = int(os.environ.get("GAME_REFORGE_KEEP_SHOTS", "8") or "8")
GAME_TOKENS_PER_SHOT = 560          # 480x853 官方 patch 公式（§0.1），只喂 ctx_est
GAME_TURN_EVENT_TIMEOUT = 600       # 泵轮单事件间隔上限（watch 链最慢一步的量级）

# ---- chat 重铸节奏（§4 chat 条，08-30 拍板）----
# 时机轴只有一条：TA 静默 ≥1h 的轮间隙——对话进行中永不铸（缓存正值钱）；
# 1h 恰好是缓存 TTL 边界，过了它缓存横竖已死，此时铸的缓存成本严格为零。
# 压力轴现在只有「窗口超软阈」一条可用（脏走发送前比对；可折活动段等 PR13）。
# 硬阈=马拉松对话没间隙也得铸（轮尾强铸）——聊天正文**绝不退** harness
# auto-compact（摘要压缩=信感复发）。门槛三值施工中调（plan 原话）。
# 09-05 改判：硬阈强铸**让路排队消息**——app 去掉了生成中禁发（outbox 串行），
# 轮尾先等一口 grace（done 刚发出去、TA 排队的下一条还在网上），队列非空就把
# 铸期推迟到队列清空的那个轮尾（ctx 仍超阈，每个轮尾都会重判，必然收敛）。
# panic 阈是无条件铸的天花板：让路理论上可被连续排队无限推迟，这条挡住上下文
# 奔着 auto-compact/模型上限跑。正常永远够不到。
# ⚠️ 08-31：两个阈值现在量的是**真实上下文**（_ctx_from_usage，含系统提示 +
# 工具 schema + 工具入参返回 + thinking）。此前量的是只数对话文本的估算值，
# 实测偏小 4.4 倍——这两个数字是在那把偏小的尺子上定的，换尺之后触发频率会
# 真的上来（那晚 21:24 的 100,139 就已经过软阈了）。真机跑一段再调。
CHAT_REFORGE_IDLE_SEC = int(os.environ.get("CHAT_REFORGE_IDLE_SEC", "3600"))
CHAT_SOFT_TOKENS = int(os.environ.get("CHAT_SOFT_TOKENS", "100000"))
CHAT_HARD_TOKENS = int(os.environ.get("CHAT_HARD_TOKENS", "150000"))
CHAT_PANIC_TOKENS = int(os.environ.get("CHAT_PANIC_TOKENS", "175000"))
HARD_FORGE_GRACE_SEC = float(os.environ.get("HARD_FORGE_GRACE_SEC", "2"))
IDLE_CHECK_SEC = 60           # 泵在轮间隙醒来看一眼的周期
CONSOLIDATE_TIMEOUT = 300

# 泵启动的踢脚哨兵：泵在轮间隙被打开时把 loop 从长等里叫醒（不是轮，直接跳过）
_KICK = object()


@dataclass
class PumpNote:
    """泵开着时从 /code/send 进来的 TA 消息（08-31 真机实锤：app 在游戏态把
    **聊天框**的消息也走终端口，不只是终端页——iOS 改路由前这是插话的唯一
    通道，必须接住不能拒）。语义同独立 loop 的队列注入：轮尾吃掉、回复走
    outbox。text=注入正文（带时间头）；ledger_text=app 自己历史里存的原文
    （发送前比对要逐字对上，注入包装不进账）。"""
    text: str
    ledger_text: str = ""

# SDK 聊天路的熄火开关（连败 3 次自动回 -p，见 app._engine_chunks）。放这儿不放
# app.py：wake_sdk 分路也要认它——聊天路都熄了火，醒来还往 session 里塞就是往
# 一个起不来的引擎里灌（计数器 _SDK_CHAT_FAILS 仍归 app，它只在请求路上加减）。
SDK_CHAT_OFF: set[str] = set()

# 巩固钩子（§5.4 睡眠周期纪律；措辞感知式·无感，不点破重铸）。产物不投递——
# 独处时的整理，没对 TA 说话；重铸后这轮从 transcript 消失，人不记得「记住」
# 这个动作本身，前提是内容真进了 Ombre（hold）。
CONSOLIDATE_PROMPT = ("〔安静下来了。趁这会儿把最近聊的过一遍，值得留住的用 hold "
                      "存好——回忆就好，不用说话。〕")
# 开局引子（§5.4① 开局包的 breath 半边；行为清单半边等活动账本落地 PR13）。
# 动作式、无条件——2026-08-13 教训：「想不起来时再搜」= 最需要搜的时候恰恰不觉得需要。
OPENING_NUDGE = "〔接话之前先 breath 一下，把最近的记忆过一遍。〕"


def _h(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def norm_history(messages: list[dict]) -> list[dict]:
    """权威窗口标准化：只留 role/text/ts、滤空气泡（forge.render 也会拒空）。"""
    out = []
    for m in messages:
        t = (m.get("text") or "")
        if t.strip() and m.get("role") in ("user", "assistant"):
            out.append({"role": m["role"], "text": t, "ts": m.get("ts")})
    return out


def divergence(ledger: list[dict], history: list[dict],
               stale_depth: Optional[int] = None) -> Optional[str]:
    """None=干净（纯追加）；"stale"=账尾多出纯 assistant（窗口没跟上，不脏）；
    "dirty"=编辑/删除/账亡，需要重铸。
    干净判据：history 恰好等于 ledger 的尾部切片（窗口滑动天然满足——账只会比窗长）。
    stale_depth：容忍深度（默认 STALE_TOLERANCE；泵开着时按未拉走 outbox 条数上浮）。
    """
    led = [(e["r"], e["h"]) for e in ledger]
    hist = [(m["role"], _h(m["text"])) for m in history]
    if not led:
        return "dirty" if hist else None
    if not hist:
        return "dirty"   # 账里有货、窗口全空=app 清了历史，必须重铸
    tol = STALE_TOLERANCE if stale_depth is None else stale_depth
    for drop in range(0, min(tol, len(led)) + 1):
        end = len(led) - drop
        start = end - len(hist)
        if start < 0 or end <= 0:
            break
        if led[start:end] == hist:
            if drop == 0:
                return None
            if all(r == "assistant" for r, _ in led[end:]):
                return "stale"
            return "dirty"
    return "dirty"


ABSORB_MAX = 8      # 一次发送最多长出几条占位气泡（09-01 那次是三张图）


def absorbable_tail(ledger: list[dict], history: list[dict]) -> list[dict]:
    """窗口尾巴上那几条**账没记过、但不算脏**的消息，返回它们（判脏前先摘掉）。

    2026-09-02：图和文字一起发，必判脏、必全量重铸——每一次。因为 app 一次发送
    在本地长两条气泡（`[图片]` + 那句话），而账一次只记一条 `turn.new_msg`。
    下一次比对时窗口比账长一条，`divergence` 的尾部切片对不上，判脏。

    只吸收 `[图片]` 占位这一种，别的一律照旧判脏。分寸在这儿：占位符的**真内容
    这一轮已经用 req.images 送到他眼前了**，账补记一笔就对得上；而任何别的多出来
    的消息，都意味着有话他没见过——那种情况重铸才是对的，不能为了省一次重铸，
    把「账上有、他没见过」做成常态（行为账那一类事故正是这个形状）。

    还要求严格更新（ts 大于账尾）：追加必然更新，ts 没往前走的是编辑不是追加。"""
    if not ledger or not history:
        return []
    last_ts = int(ledger[-1].get("ts") or 0)
    out: list[dict] = []
    for m in reversed(history[-ABSORB_MAX:]):
        if (m.get("role") != "user"
                or (m.get("text") or "").strip() != state_store.IMAGE_PLACEHOLDER
                or int(m.get("ts") or 0) <= last_ts):
            break
        out.append(m)
    out.reverse()
    return out


def dirty_reason(ledger: list[dict], history: list[dict]) -> str:
    """判脏之后说清楚**哪儿脏了**（只在日志里用，不参与判定）。

    2026-09-02：一次重铸把整段历史的时间头抹掉、间接害他把 3 分钟前说的话说成
    「昨天」，而日志里只有「判脏，重铸」五个字——回头查花了半小时才定位到是一张
    图。重铸是这套里最重的动作（换 client、缓存作废、注入全蒸发），它不能是一句
    没有宾语的话。"""
    led = [(e["r"], e["h"]) for e in ledger]
    hist = [(m["role"], _h(m["text"])) for m in history]
    if not led:
        return "账是空的（session 刚起，或上一轮死了把账清了）"
    if not hist:
        return "窗口全空、账里有货——app 清了历史"
    if len(hist) > len(led):
        n = len(hist) - len(led)
        return (f"窗口({len(hist)})比账({len(led)})长 {n} 条：app 历史里有账没记下的"
                f"消息。最常见是一次发送落成好几条气泡（图+文），而账一次只记"
                f"一条 new_msg")
    start = len(led) - len(hist)
    for i, (h_pair, l_pair) in enumerate(zip(hist, led[start:])):
        if h_pair != l_pair:
            role = h_pair[0]
            preview = (history[i].get("text") or "")[:24].replace("\n", " ")
            return (f"第 {i} 条对不上（窗口里是 {role}「{preview}…」）："
                    f"这条被编辑或删过")
    return "尾部对不上（账尾多出来的不全是 assistant）"


@dataclass
class Turn:
    """一次注入。out 收 SSE bytes，None 收尾——每个 Turn 恰好一个结局
    （done 正常走完 ∣ error+done），补投/error 盒仍归 app.py 的 rescue 观察者。

    kind="wake"（PR12 醒来升 A）：没有请求方——out 没人读、history/new_msg 空着；
    注入由 injection_factory 在**执行开始时**组装（排队几分钟后时间那句不能是死的）；
    finalize 里做投递（wake_sdk.finish_wake_turn），返回的 reply=真投递出去的正文
    （空串=安静醒着，账不动）；轮没走完 → on_dead（记 error 心流+冷却）。"""
    rid: str
    history: list[dict]                  # 权威窗口（不含新消息），已 norm
    new_msg: dict                        # {"role":"user","text","ts"}（入账用原文）
    injection: str                       # 实际注入的包装文本（不进账）
    finalize: Callable[[str, list], dict]
    images: Optional[list] = None
    file_blocks: Optional[list] = None
    catalog: Optional[list] = None       # 表情目录：重铸/起 session 时进系统提示
    kind: str = "chat"                   # "chat" | "wake"（轮来源标签，投递路由靠它）
    injection_factory: Optional[Callable[[], str]] = None
    on_dead: Optional[Callable[[], None]] = None
    wake_trigger: str = ""               # kind="wake" 时的醒因（去重槽位的键，09-12）
    # 判脏时从窗口尾巴摘下来的 `[图片]` 占位（absorbable_tail）：不算脏，但账要补记。
    # 轮内填，轮尾和 new_msg 一起进账。
    absorbed: list = field(default_factory=list)
    out: asyncio.Queue = field(default_factory=asyncio.Queue)


# ---------- options ----------

def _merge_mcp_file(servers: dict, cfg_path: str) -> None:
    try:
        data = json.loads(open(cfg_path, encoding="utf-8").read())
        servers.update(data.get("mcpServers", {}))
    except Exception as e:
        print(f"[chat_loop] MCP 配置解析失败（跳过 {cfg_path}）: {e}", file=sys.stderr)


# 醒来轮契约（§0.3：注入只给知觉材料，行为契约在系统提示里一次性写死）。
# 〔〕外=会发到 TA 手机上的消息、〔〕内=心里活动只留档——投递分流在
# wake_sdk.split_musings，这里的字必须和那边的机械口径一字不差地对上。
def _wake_contract() -> str:
    u = config.user_name()
    return (f"【有时你会自己醒来：没有{u}的新消息，只有一段〔〕包着的知觉（时间、间隔、"
            f"见闻）。那不是{u}在找你，是你自己的时间——想干什么干什么，不用非得说话。"
            f"这种醒来的时候，你写在〔〕外的话会作为消息发到{u}手机上；只想自己待着，"
            f"就把心里活动整段用〔〕包起来（包着的不会发出去），没什么可说就安安静静"
            f"待着，别硬找话。】")


def _wake_gate(handle: session_mgr.LoopHandle):
    """PreToolUse 门：与审批无关的三件事（PLAN_native §1.4）。hook 先于权限
    判定跑；hook 放过（{}）的写类因为不进 allowed_tools 会落 CLI 的 ask，
    路由到 can_use_tool（_permit_gate）弹卡——两层是组合，不是二选一。

    1. 路径闸：Read/Grep/Glob 和 Edit/Write 都过（限仓根+黑名单）。Bash 没有
       路径参数，闸对它放空——机主看得到命令原文，拍板本身就是闸。
    2. wake 禁用面照 PR12：turn_kind=wake 查 wake_tools。
    3. game 泵门：mcp__game__* 只在泵开着时放行；泵没开一律拒——任务引擎
       互斥由此顺带成立（拿不到锁就开不了泵）。
    聊天轮/巩固轮 → 其余全放行，行为与没挂 hook 一字不差。"""
    import pipeline

    def _deny(reason: str) -> dict:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}

    async def gate(hook_input, tool_use_id, ctx) -> dict:
        tool = (hook_input or {}).get("tool_name") or ""
        tool_input = (hook_input or {}).get("tool_input") or {}
        if tool in pipeline.READONLY_BUILTINS:
            # 只读常驻（PR14-b）：任何轮来源都能看一眼（§2 核实纪律），
            # 但过路径闸（限根目录+黑名单，§5.3 安全面）——先于醒来禁用面，
            # 只读工具不在 wake 挂载表里，不挡在这儿 wake 轮就全拒了。
            why = pipeline.readonly_path_guard(tool_input, handle.char_id)
            return {} if why is None else _deny(why)
        if tool in pipeline.WRITE_BUILTINS:
            # 写类（§1.4）：hook 只过路径闸，别的判断一概不做——放过就落 ask
            # → can_use_tool 弹卡原地挂起，批不批在机主指头上（§1.2）。
            why = pipeline.readonly_path_guard(tool_input, handle.char_id)
            return {} if why is None else _deny(why)
        if tool.startswith("mcp__game__"):
            if handle.meta.get("game_pump"):
                return {}
            return _deny("游戏这会儿不在手边——想玩的话先用 game_start 把游戏拿过来。")
        if handle.meta.get("turn_kind") != "wake":
            return {}
        if tool in (handle.meta.get("wake_tools") or set()):
            return {}
        return _deny("这会儿是你自己醒着的时间，这个工具不在手边（醒来那条路不挂它）"
                     "——想用的话留到聊天或上机的时候。")
    return gate


def _permit_gate(handle: session_mgr.LoopHandle):
    """can_use_tool（PLAN_native §1.2）：写类四件不进 allowed_tools，CLI 判
    ask 路由到这儿——那次调用原地挂起，permits 弹卡给机主。批了返回 Allow，
    工具原地执行、同一轮继续；拒了/超时返回 Deny(理由)，模型接着说话。
    安全面（路径闸/wake 面/泵门）在 PreToolUse 门里，先于这儿跑；这儿只管
    「机主批不批」一件事。超时分轮来源（§1.3）。"""
    async def can_use(tool: str, tool_input: dict, ctx) -> "PermissionResultAllow | PermissionResultDeny":
        import permits
        u2 = config.user_name()
        if tool == "AskUserQuestion":
            # 问答卡（PLAN_chatui §3.5/U4）：卡本体在 assistant 事件流过
            # translate_events 时已推给 app（sse "question"），这儿只管挂起等答案，
            # 答案用 updated_input 回填（机制 09-01 探针实证，见 questions.py 卷首）。
            import questions
            got = await questions.ask(
                handle.char_id,
                questions=(tool_input or {}).get("questions") or [],
                question_id=getattr(ctx, "tool_use_id", None))
            if got is None:
                mins = max(1, questions.TIMEOUT_SEC // 60)
                return PermissionResultDeny(
                    message=f"问题递到{u2}手机上，等了约 {mins} 分钟没等到回答——"
                            "先按你自己的判断来，TA 回头看到会说的。")
            answers, note = got
            if answers is None:
                return PermissionResultDeny(
                    message=f"{u2}这会儿没答" + (f"：{note}" if note else "。"))
            return PermissionResultAllow(
                updated_input={**(tool_input or {}), "answers": answers})
        turn = handle.meta.get("turn_kind") or "chat"
        timeout = permits.timeout_for(turn)
        verdict = await permits.ask(
            handle.char_id, tool=tool,
            summary=_tool_summary(tool, tool_input or {}),
            title=(getattr(ctx, "title", None) or ""),
            permit_id=getattr(ctx, "tool_use_id", None),
            timeout=timeout)
        u = config.user_name()
        if verdict is None:
            mins = max(1, int(timeout) // 60)
            return PermissionResultDeny(
                message=f"卡递到{u}手机上，等了约 {mins} 分钟没等到拍板——"
                        "这一下先没做。想做的话过阵子再试就好。")
        allow, reason = verdict
        if allow:
            return PermissionResultAllow()
        return PermissionResultDeny(
            message=f"{u}没批这一下" + (f"：{reason}" if reason else "。"))
    return can_use


# 游戏节奏骨架（设计稿三：TICK_SYSTEM 的条件式变体，常驻聊天系统提示——没有
# 进出场换 client，骨架只能一次性写死；措辞挂「游戏在手边时」条件，平时不沾）。
# 机制事实在小抄里（game_notes_read），这里只写游戏无关的运行前提。
GAME_RHYTHM_SYSTEM = """

【游戏在手边时的节奏（game_start 之后才算拿到；平时这些工具不在手边）】
拿着游戏的时候，你的时间按「短轮」走：每轮做一小步，说完就停——停不是结束，
画面留在原地，轮会自己续上。你会收到一个「·」：那不是任何人说话，当作你自己
回过神来、目光落回屏幕。{user}和世界的消息随时可能插进来代替「·」，插进来就先回应人。
- 常态轮：**double tap 起手**（点位照小抄）→ 看返回截图：完整文字 → 直接点评这句，
  收轮；画面在动/半截字 → game_watch 等到终态再点评，收轮。
- 首轮、导航轮（菜单里找路、没有上一轮点评可依赖）：look 起手，看清再动。
- watch 的终态只有两种：**完整文字** → 点评收轮；**章节目录** → 结算轮——先
  game_chapter_write 把这一场写成章节志、game_progress_write 更新进度，再决定
  读下一章还是 game_end 放下游戏。
- look 到非预期画面（弹窗/异常/不认识的界面）：停手，看清楚再动，拿不准问{user}。
- 每轮只做自己这一步，说完就停，别在一轮里连读半章——节奏是你的朋友。
- 读得久了，更早的画面会在记忆里淡去——自然的事；文字和你说过的话一直都在。
  值得留住的，用笔记本和 hold 留。
"""


def _game_capable(char_id: str) -> bool:
    """这个角色的聊天 session 挂不挂游戏（schema 并集常驻，taste ④）：
    STORY_ENGINE=unified 且 game 模式开着且游戏资源归他。挂载开关/泵状态只进门
    （_wake_gate），不动 schema——中途拨开关不用换 client。"""
    if config.STORY_ENGINE != "unified" or not config.GAME_MODE_ENABLED:
        return False
    try:
        import plugins
        return plugins.owner_of("tmux") == char_id
    except Exception:
        return False


def _shot_sink(handle: session_mgr.LoopHandle):
    """game 截图 → 事件账本（重铸「近 K 张图回填」的材料）。泵没开不落
    （不该发生：泵没开 game_* 被门拒），失败不抛不影响轮。"""
    def sink(jpg: bytes) -> None:
        pump = handle.meta.get("game_pump") or {}
        if pump.get("seg_id"):
            import activity_log
            activity_log.save_shot(pump["seg_id"], jpg)
    return sink


def build_options(char_id: str, catalog: Optional[list] = None,
                  handle: Optional[session_mgr.LoopHandle] = None) -> ClaudeAgentOptions:
    """口径对齐 pipeline.base_claude_args（chat 路）：人设+菜单+叮嘱进系统提示，
    Ombre http 直传 dict，插件/宠物/skills 的 mcp.json 解析合并，白名单+strict。
    与 -p 的差别只有结构：稳定段从每轮 prompt 挪进 session 系统提示（付一次）。
    handle＝泵的把手（PR12）：给了才挂醒来禁用面的 PreToolUse 门（按 meta.turn_kind
    判轮）；测试/工具脚本不传，拿到的 options 和从前一样。"""
    import pipeline
    if os.environ.get("ANTHROPIC_API_KEY"):
        # -p 那条路每次 pop 掉 key；SDK 子进程继承我们的 env，没法逐调用摘——
        # 有 key 就拒开，绝不静默改成 API 计费（订阅纪律）。
        raise RuntimeError("环境里有 ANTHROPIC_API_KEY：SDK 聊天路会走 API 计费，拒开")

    parts = [pipeline.rendered_persona(char_id).read_text("utf-8")]
    mb = pipeline.tool_menu_block("chat", char_id)
    if mb:
        parts.append(mb)
    parts += [pipeline.pronoun_hint(), pipeline._chat_next_hint(), _wake_contract()]
    sb = pipeline.sticker_block(catalog)
    if sb:
        parts.append(sb)
    # 禁空头承诺那条二选一（§14.2，09-04 从每轮注入挪来）。**排在最尾**是有意的：
    # 它是离新消息最近的一句，也是被淹掉就等于没有的那一句（one_turn_hint 的复盘）。
    parts.append(pipeline.one_turn_hint("chat_session"))
    system = "\n\n".join(p for p in parts if p)

    servers: dict = {}
    tools: list[str] = []
    if pipeline.ombre_alive(char_id):
        import characters
        oc = characters.ombre_conf(char_id)
        server: dict = {"type": "http", "url": oc["mcp_url"]}
        if oc["mcp_token"]:
            server["headers"] = {"Authorization": f"Bearer {oc['mcp_token']}"}
        servers["ombre-brain"] = server
        tools += pipeline.OMBRE_TOOLS
    import plugins
    plug_cfg, plug_tools = plugins.mounted("chat", char_id)
    _merge_mcp_file(servers, str(pipeline._basics_mcp_config(char_id)))
    tools += pipeline.BASICS_MCP_TOOLS       # 无条件：钟 + 取公开资料
    if plug_cfg:
        _merge_mcp_file(servers, plug_cfg)
        tools += plug_tools
    if pipeline._pet_mcp_mounted(char_id):
        _merge_mcp_file(servers, str(pipeline._pet_mcp_config(char_id)))
        tools += pipeline.PET_MCP_TOOLS
    if pipeline._skills_mounted("chat", char_id):
        import skills
        _merge_mcp_file(servers, str(skills.mcp_config(char_id)))
        tools += skills.SKILLS_MCP_TOOLS
    if handle is not None and _game_capable(char_id):
        # game 并入意识流（设计稿三）：进程内 game MCP 常驻 + 节奏骨架进系统提示。
        # 工具能不能用由 _wake_gate 按泵状态判，schema 恒在（换 client 只在重铸）。
        import game_loop
        servers["game"] = game_loop.build_game_server(
            handle, unified=True, shot_sink=_shot_sink(handle))
        game_tools = [f"mcp__game__{t}" for t in game_loop.GAME_TOOL_NAMES]
        tools += game_tools
        system += GAME_RHYTHM_SYSTEM.replace("{user}", config.user_name())

    if handle is not None and (config.READONLY_TOOLS_ENABLED
                               or config.WRITE_TOOLS_ENABLED):
        # 文件工具挂载（PLAN_native §1）：只读直放（进 allowed_tools，常驻所有
        # 轮次）；写类四件 schema 也挂、但**不进 allowed_tools**——每次调用被
        # CLI 判 ask，路由到 can_use_tool 弹卡原地挂起。安全面在 PreToolUse
        # 闸里，handle=None（没门）就一概不挂。两个闸分开拨：schema token
        # 成本逐段实测。
        segs: list[str] = []
        if config.READONLY_TOOLS_ENABLED:
            tools += sorted(pipeline.READONLY_BUILTINS)
            segs.append("你手边常驻一套只读的文件工具（Read/Grep/Glob），看得到 "
                        f"cassette 仓（{pipeline.CODE_ROOT}）的代码和资料，"
                        f"还有{config.user_name()}的 dossier 仓（{pipeline.DOSSIER_ROOT}）"
                        "——想核实什么随手翻，别背结论。凭据（.env）和别的角色的 "
                        "state 房间不在范围里。")
        if config.WRITE_TOOLS_ENABLED:
            tools += sorted(pipeline.WRITE_BUILTINS)
            segs.append(f"改东西的工具（Edit/Write/Bash）也在手边。每次动手，"
                        f"那一下会先弹到{config.user_name()}手机上等TA批：批了"
                        "就原地继续做；拒了或没等到，你会收到一句原因，接着"
                        "说你的就好。")
        else:
            segs.append("改文件的工具这会儿不在手边。")
        system += "\n\n【" + "".join(segs) + "】"
        if config.WRITE_TOOLS_ENABLED:
            system += _discipline_block(char_id)

    if handle is not None and config.QUESTION_CARDS_ENABLED:
        # 问答卡（PLAN_chatui §3.5/U4）：AskUserQuestion 挂载但不进 allowed_tools
        # → 每次调用落 ask → can_use_tool 挂起，app 弹卡机主作答（updated_input
        # 回填）。wake 轮被 PreToolUse 门挡着（不在 wake_tools），问不了。
        tools = tools + ["AskUserQuestion"]
        system += (f"\n\n【拿不准要{config.user_name()}拍板的事，可以用 "
                   "AskUserQuestion 出一张选择卡：卡会弹到TA手机上，TA选完你"
                   "同轮就拿到答案；等一阵没等到就先按自己的判断来。】")

    env = {}
    if tools and pipeline.tool_search_on("chat"):
        tools = tools + [pipeline.TOOL_SEARCH_TOOL]
        env["ENABLE_TOOL_SEARCH"] = "true"

    return ClaudeAgentOptions(
        system_prompt=system,
        model=config.MODEL,
        # 隔离文件系统 settings（PLAN_native §10 待验③实证 09-01）：默认 None=
        # 全加载，机主 ~/.claude/settings.json 会整份漏进 TA 的会话（swift-lsp
        # 插件实测在场）；机主哪天点一次「always allow」写进 permissions.allow，
        # can_use_tool 就被静默遮蔽——弹卡形同虚设。空表把用户/项目/local 三层
        # 全关掉，会话吃的只有这里显式给的东西。
        setting_sources=[],
        cwd=pipeline.neutral_cwd(),
        mcp_servers=servers,
        strict_mcp_config=True,
        tools=tools,
        # 写类四件和 AskUserQuestion 不进直放面（PLAN_native §1.1 / chatui U4）：
        # 挂载归 tools（schema 在），放行走 ask → can_use_tool（一命令一卡/
        # 一问一卡，机主原地拍板）。
        allowed_tools=[t for t in tools
                       if t not in pipeline.WRITE_BUILTINS
                       and t != "AskUserQuestion"],
        can_use_tool=(_permit_gate(handle)
                      if handle is not None and (config.WRITE_TOOLS_ENABLED
                                                 or config.QUESTION_CARDS_ENABLED)
                      else None),
        env=env,
        include_partial_messages=True,   # 逐字增量：text 事件靠它
        # max_turns 不设：-p 路从来没限过，聊天轮的工具链长度由模型自己收
        hooks=({"PreToolUse": [HookMatcher(hooks=[_wake_gate(handle)])]}
               if handle is not None else None),
    )


# ---------- 注入正文（易变段；稳定段在系统提示里） ----------

def build_injection(messages, char_id: Optional[str],
                    extra_hints: Optional[list[str]] = None) -> str:
    """一轮的包装文本：时间感 + 手上有什么 + 新消息。口径抄 build_prompt 的易变段——
    历史不在这儿（历史活在 transcript 里，这正是整个迁移的意义）。

    **注入瘦身（PLAN_native §14.2，09-04）**：留下的三样——几点、隔了多久、
    手上有什么——**全是知觉材料**，没有一句是「关于他的说明」。规矩（禁空头承诺
    那条二选一）挪进了系统提示付一次，用法归工具 schema——原来那段
    `one_turn_hint("chat_session")` 是**和系统提示重复的第二份**，每轮白付。
    这正是下面 :1605 那条纪律要的形状，别往回加。

    时间头用 `stamp_str` 不用 `now_str`（不带时段词）：和 `wake_sdk.wake_injection`
    的抬头、和 `_stamp_times` 重铸时补的锚行**天然同形**，他才认得出是同一种东西。"""
    import pipeline
    last = messages[-1]
    lines = [h for h in (extra_hints or []) if h]
    if lines:
        lines.append("")   # 有叮嘱才空一行；没有就别拿空行开头
    lines.append(f"【{pipeline.stamp_str(int(time.time()))}】")
    gap = pipeline.gap_before_last(messages)
    if gap:
        lines.append(f"【距离上一条消息，过了 {gap}】")
    pending = pipeline.pending_todo_block(char_id)
    if pending:
        lines.append(pending)
    lines.append("")
    lines.append("【回下面这条。按这句的份量和情绪回：随口就随口，别硬凑长，"
                 "一句话或一个词也可以。】")
    lines.append(f"{config.user_name()}：{last.text}")
    return "\n".join(lines)


# ---------- SDK 消息 → CLI stream-json dict（sse.translate_events 的口粮） ----------

def _assistant_dict(msg: AssistantMessage) -> dict:
    blocks = []
    for b in msg.content:
        if isinstance(b, TextBlock):
            blocks.append({"type": "text", "text": b.text})
        elif isinstance(b, ToolUseBlock):
            blocks.append({"type": "tool_use", "id": b.id, "name": b.name,
                           "input": b.input})
    return {"type": "assistant", "message": {"content": blocks}}


def _user_dict(msg: UserMessage) -> dict:
    blocks = []
    content = msg.content if isinstance(msg.content, list) else []
    for b in content:
        if isinstance(b, ToolResultBlock):
            blocks.append({"type": "tool_result", "tool_use_id": b.tool_use_id,
                           "content": b.content, "is_error": b.is_error})
    return {"type": "user", "message": {"content": blocks}}


def _capture_capsules(char_id: str, text: str, turn: str = "chat") -> None:
    """收场白 capsule（PLAN_native §4：结论跨重铸过桥的唯一结构化通道）。
    他气泡里「◆ 结论 ← 出处」打头的行落进**行为账**——证据原文在账里永存，
    事后查证走账，不注回上下文。（旧家是 code 段的事件账；code 无场之后
    事件账没有段地址，按角色一本的行为账就是它的家——PLAN_native §4 记档。）"""
    if "◆" not in (text or ""):
        return
    import activity_log
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("◆"):
            activity_log.append_act(char_id, turn, "capsule",
                                    line.lstrip("◆").strip())


# ---------- 执行层 tool 落账（S3 补线②：§5.3 四类表一、三类的确定性载体） ----------

# MCP 工具的对象字段（不列工具表：新插件的动作自动认得出）。顺序=从「人一眼
# 认得出是哪件事」到「至少是个地址」——信认 subject、网页认 title、浏览器认 url。
_OBJ_KEYS = ("subject", "title", "url", "to", "query", "pattern",
             "path", "page_id", "uid", "id", "text", "content")


def _tool_summary(name: str, inp: dict) -> str:
    """事件账/行为账里的对象摘要：常用工具取关键字段（读取侧聚合要认它，行为
    清单要让人一眼认出是哪封信），其余压成一行 json。截断交给写入侧
    （append_event 的 EVENT_TEXT_CAP＝第一类「原文级」的量纲、append_act 200）。"""
    try:
        if name in ("Read", "Edit", "Write", "NotebookEdit"):
            return str(inp.get("file_path") or inp.get("notebook_path") or "")
        if name in ("Grep", "Glob"):
            pat, path = str(inp.get("pattern") or ""), str(inp.get("path") or "")
            return f"{pat}（{path}）" if path else pat
        if name == "Bash":
            return str(inp.get("command") or "")
        if name.endswith("__next_wake"):
            import pipeline   # 模块级没 import（循环依赖）；漏了会被下面的 except 吞掉
            # 闹钟（§14.1）：_OBJ_KEYS 对 {action, minutes, todo} 一个都匹配不上，
            # 不特判就掉进 json.dumps 兜底。行为账上这一行是**单槽位覆盖的留痕**
            # （规避规则 5：被顶掉的旧钟不能凭空消失），得读得懂。
            act = str(inp.get("action") or "read").strip().lower()
            if act != "set":
                return act
            mins = pipeline.clamp_next_minutes(inp.get("minutes"))
            if mins is None:
                return "set（分钟数写歪了）"
            at = int(time.time()) + mins * 60
            return "set " + pipeline.alarm_slot_str(at, inp.get("todo") or "")
        for k in _OBJ_KEYS:
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return json.dumps(inp, ensure_ascii=False)
    except Exception:
        return ""


# 回执摘要上限：这是**回执**不是内容——够看清「成没成」，不搬返回体。
RET_CAP = 300


def _result_text(content) -> str:
    """tool_result 的回执正文（`ToolResultBlock.content` 三形都收：str /
    block list / None）。非文本块（图片等）跳过——要的是那句话，不是载荷。"""
    try:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [str(b.get("text") or "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            return "\n".join(p for p in parts if p).strip()
    except Exception:
        pass
    return ""


class _ToolTrace:
    """一轮消息流里 tool_use 配上 tool_result → 落两本账（时间+轮来源+工具+
    对象摘要+成败+回执）。确定性落账，不靠他自觉——重铸后 transcript 里的工具块
    物理无法复原（forge 断言拒 tool 块），这两本账是干活留痕唯一的家。
    轮死在半路、配不上对的不落（账丢一条不影响轮）。每轮一个实例，不跨轮攒状态。

    - **事件账**（折叠段）：要有地址，没开段就不落——段只在 game 场开
      （code 无场，PLAN_native：写类留痕走行为账）。
    - **行为账**（按角色一本）：每轮都落，判线 pipeline.acts_worthy。
      08-31 事故修：聊天轮从来不开段，而「说寄了信、其实一次工具都没调」那类
      失约恰恰全发生在聊天轮（那天 21:24/21:26 连编两轮）。原来行为账唯一的
      写入点在 wake_sdk 的 stored 镜像里，聊天轮零落账＝空白不构成反证，他
      下一轮会把假信当既成事实往下算。搬到执行层之后，两种轮一个口径。"""

    def __init__(self, handle: session_mgr.LoopHandle):
        self.handle = handle
        self.pend: dict = {}

    def use(self, block_id: str, name: str, inp: Optional[dict]) -> None:
        # 原始 input 也留着：闹钟留底要铸回**他真调过的那次调用**，
        # 拿 summary 那句人话反推等于重写历史（§14.4「照实铸」）。
        self.pend[block_id] = (name, _tool_summary(name, inp or {}), inp or {})

    def result(self, block_id: str, is_error, content=None) -> None:
        name, summary, inp = self.pend.pop(block_id, (None, "", {}))
        if not name:
            return
        import activity_log
        import pipeline
        # 成败两道口径，别混（§7.2 / §7.2.1 复盘，眠眠 09-01 12:16 拍板）：
        #
        # `ok` 吃**结构判据**——①MCP 的 is_error ②返回体 {"ok": false}
        # （codemode 那类插件的口径）。读的是专门表示成败的字段，不是猜。
        # **③ 那道文本婉拒名单不吃**：它给的是「像失败」不是「是失败」，
        # 账本认错会被下一轮当既成事实往下算，容错代价跟灰字提示差一个量级。
        #
        # `ret` 存回执原话——因为 ①② 也兜不住业务层的软拒绝：09-01 11:10
        # 实例，一封信被配额挡回来（回执「今天已经寄了 3 封了。明天再来。」），
        # is_error 是 false、返回体不是 json，两道结构判据一道都不命中，
        # 账上照样 ok=true。**「成没成」最后只有原话答得了**，别指望 ok。
        # 只读工具不存 ret——它们的返回是内容不是回执（读类在折叠框里本来就
        # 聚合成「翻看过」一行），存了只占地方。
        text = _result_text(content)
        ok = not pipeline.tool_result_error_structural(bool(is_error), text)
        ret = "" if name in pipeline.READONLY_BUILTINS else text[:RET_CAP]
        turn = self.handle.meta.get("turn_kind") or "chat"
        seg = (self.handle.meta.get("game_pump") or {}).get("seg_id")
        if seg:
            activity_log.append_event(
                seg, "tool", name=name, text=summary, ok=ok, ret=ret,
                ext=pipeline.external_tool(name),
                ro=name in pipeline.READONLY_BUILTINS, turn=turn)
        if pipeline.acts_worthy(name):
            activity_log.append_act(self.handle.char_id, turn, name, summary,
                                    ok=ok, ret=ret)
        # 闹钟留底（§14.4 补铸）：只留**改过钟的那几次**——read 没改动世界，
        # 铸回去只是噪音。失败的也留：他试过这件事是真的，回执会说没成。
        if (name.endswith("__next_wake")
                and str(inp.get("action") or "read").strip().lower() != "read"):
            state_store.append_alarm_call(
                {"ts": int(time.time()), "id": block_id, "name": name,
                 "input": inp, "ret": text[:RET_CAP], "ok": ok},
                self.handle.char_id)


async def _turn_events(client, handle: session_mgr.LoopHandle,
                       flags: dict) -> AsyncIterator[dict]:
    """吃一轮（到 ResultMessage 止），产出 translate_events 认识的事件 dict。
    空闲超时/流断都产 __idle_timeout__ 哨兵（translate 会以 error+done 收尾），
    并在 flags 里留痕给泵决定 session 生死。"""
    trace = _ToolTrace(handle)
    agen = client.receive_messages().__aiter__()
    while True:
        try:
            msg = await asyncio.wait_for(agen.__anext__(),
                                         timeout=config.CLAUDE_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            import permits
            import questions
            if permits.waiting(handle.char_id) or questions.waiting(handle.char_id):
                # 审批/问答挂起不算空闲（PLAN_native §1.2）：卡在机主手上，
                # 流静着是正常的。两边各自有超时兜底，这儿等结果就好。
                continue
            flags["timeout"] = True
            print("[chat_loop] 轮内空闲超时（session 按死处理）", file=sys.stderr)
            yield {"type": "__idle_timeout__"}
            return
        except StopAsyncIteration:
            flags["eof"] = True
            print("[chat_loop] 引擎消息流断了（session 按死处理）", file=sys.stderr)
            yield {"type": "__idle_timeout__"}
            return
        if isinstance(msg, StreamEvent):
            yield {"type": "stream_event", "event": msg.event}
        elif isinstance(msg, AssistantMessage):
            handle.touch()
            if getattr(msg, "usage", None):
                handle.meta["ctx_est"] = _ctx_from_usage(msg.usage)
            for b in msg.content:
                if isinstance(b, ToolUseBlock):
                    trace.use(b.id, b.name, b.input)
            yield _assistant_dict(msg)
        elif isinstance(msg, UserMessage):
            for b in (msg.content if isinstance(msg.content, list) else []):
                if isinstance(b, ToolResultBlock):
                    trace.result(b.tool_use_id, b.is_error, b.content)
            yield _user_dict(msg)
        elif isinstance(msg, ResultMessage):
            _note_usage(handle.char_id, msg,
                        handle.meta.get("turn_kind") or "chat",
                        bool(handle.meta.get("game_pump")))
            if msg.is_error:
                flags["error_subtype"] = msg.subtype or "?"
                # 错误正文必须落日志（2026-09-12 体检）：CLI 在 API 报错时发的是
                # subtype=success + is_error=true，原因全在 result 字符串里；之前只
                # 打 subtype，09-07 起每天 2-6 次「醒来轮没走完」查不出是 529 还是别的。
                print(f"[chat_loop] 轮结果报错（{handle.char_id}，subtype={msg.subtype}，"
                      f"api_error_status={msg.api_error_status}）："
                      f"{str(msg.result or '')[:300]!r}", file=sys.stderr)
            yield {"type": "result", "result": msg.result,
                   "is_error": msg.is_error, "subtype": msg.subtype,
                   "api_error_status": msg.api_error_status}
            return


def _ctx_from_usage(usage) -> int:
    """一次请求的真实上下文占用＝这条 prompt 的全部 input（命中缓存的 + 新写
    缓存的 + 没走缓存的）＋ 它产出的 output（下一条 prompt 里就有它）。

    ⚠️ 只许读 **AssistantMessage.usage（逐请求）**——`ResultMessage.usage` 是
    整轮求和，多请求的轮里 cache_read 会被加好几遍（08-31 实测：一个 5 请求
    的轮报 cr=472,009，当时真实上下文才 97,002）。

    为什么不再用 estimate_tokens 累加对话文本（08-31 事故复盘）：旧口径只数
    「铸进去的历史 + 每轮注入 + 每轮回复」，**系统提示、工具 schema（约 3 万）、
    工具的入参和返回、thinking 一概不数**。实测那晚 21:24 那轮，旧口径 22,891、
    真实 100,139——差 4.4 倍，于是软阈 100k / 硬阈 150k 事实上永远够不着，
    「聊天正文绝不退 harness auto-compact」那条防线是虚的。"""
    u = usage or {}
    return sum(int(u.get(k) or 0) for k in
               ("input_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens", "output_tokens"))


def _usage_scene(kind: str, in_game: bool) -> str:
    scene = SCENE if kind == "chat" else f"{SCENE}/{kind}"
    return scene + "/game" if in_game else scene


def _note_usage(char_id: str, msg: ResultMessage, kind: str = "chat",
                in_game: bool = False) -> None:
    """usage 落账（PLAN_sdk §10 S2 设计稿二：手机端 usage 面板的数据源）。
    append-only jsonl 按天分文件；写失败只记日志，绝不影响聊天轮。
    kind＝轮来源（chat/wake）、in_game＝泵状态（设计稿三：kind 与记账解耦——
    「他玩游戏花的」按泵标，面板上和陪聊/醒来分得开）。"""
    try:
        d = state_store.STATE_DIR / "usage"
        d.mkdir(exist_ok=True)
        rec = {"ts": int(time.time()), "char": char_id,
               "scene": _usage_scene(kind, in_game),
               "subtype": msg.subtype, "usage": msg.usage or {}}
        day = time.strftime("%Y%m%d")
        with open(d / f"usage-{day}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[chat_loop] usage 落账失败: {e}", file=sys.stderr)


# ---------- 账 ----------

def _ledger_path(char_id: str):
    return LEDGER_DIR / f"{char_id}.json"


def _persist_ledger(char_id: str, sid: Optional[str], ledger: list[dict]) -> None:
    """观测用落盘（真相永远是权威消息库；重启后一律视同账亡重铸，不从这儿恢复）。"""
    try:
        LEDGER_DIR.mkdir(exist_ok=True)
        tmp = _ledger_path(char_id).with_suffix(".tmp")
        tmp.write_text(json.dumps({"sid": sid, "ledger": ledger},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(_ledger_path(char_id))
    except Exception as e:
        print(f"[chat_loop] 账落盘失败: {e}", file=sys.stderr)


# ---------- 活动框（§4 规则二：回流点评带上「你当时在读剧情」的框架）----------

def _fold_trace_lines(iv: dict) -> str:
    """折叠段的经历摘要（S3 补线③）：从这一场的事件账 derive「亲手做过什么」，
    附进折叠框——不再只有「细节淡下去了」。写类逐条（§5.3 第一类原文级，
    天然稀疏，帽 12 防边界，带回执原话）、读类聚合一行事实（第三类：留事实
    不留内容）。
    确定性：同事件账同字节。事件账过了保留窗（30 天）→ 空串，折叠框回到
    光框，与从前一致。game 段的点按被 ext 判线挡在外面（第四类试错），
    这里真正的客户是 code 段——工具不上屏不进消息库，重铸后只有这本账。"""
    import activity_log
    seg = activity_log.interval_seg_id(
        iv.get("char") or "", iv.get("scene") or "", int(iv.get("start") or 0))
    all_evs = activity_log.read_events(seg)
    caps = [e for e in all_evs if e.get("kind") == "capsule"]
    evs = [e for e in all_evs if e.get("kind") == "tool" and e.get("ext")]
    if not evs and not caps:
        return ""

    def _hm(ts) -> str:
        return time.strftime("%H:%M", time.localtime(int(ts or 0)))

    def _short(name: str) -> str:
        return (name or "?").rsplit("__", 1)[-1]

    acts = [e for e in evs if not e.get("ro")]
    reads = [e for e in evs if e.get("ro")]
    lines: list[str] = []
    # capsule 先行（§5.3 第二类：结论带指针，他自己写的收场白）——帽 20 防边界
    for e in caps[:20]:
        t = (e.get("text") or "").strip()
        if t:
            lines.append(f"◆ {t}")
    for e in acts[:12]:
        t = (e.get("text") or "").strip().replace("\n", " ")[:80]
        fail = "" if e.get("ok", True) else "（没成）"
        # 回执（§7.2）：ok 只证明协议层没报错，办没办成看这句原话。老账行
        # 没有 ret（09-01 之前落的）→ 缺键取空，那行与从前一字不差。
        ret = (e.get("ret") or "").strip().replace("\n", " ")[:40]
        lines.append(f"[{_hm(e.get('ts'))}] {_short(e.get('name'))}"
                     + (f"：{t}" if t else "") + fail
                     + (f" → {ret}" if ret else ""))
    if len(acts) > 12:
        lines.append(f"……还有 {len(acts) - 12} 件")
    if reads:
        objs: list[str] = []
        for e in reads:
            t = (e.get("text") or "").strip().replace("\n", " ")[:40]
            if t and t not in objs:
                objs.append(t)
        if objs:
            head = "、".join(objs[:8])
            tail = f" 等共 {len(objs)} 处" if len(objs) > 8 else ""
            lines.append(f"翻看过：{head}{tail}")
    return "\n".join(lines)


def _stamp_times(history: list[dict],
                 char_id: Optional[str] = None) -> list[dict]:
    """给注入侧（TA 说的话）每一条打上时间戳。渲染规则，和 _frame_activities 同一层：
    **只改铸造输入、不进账、不改权威原文**。

    2026-09-02 事故：14:07 那轮 Cassius 开口就说「昨天我猜滚动窗口」——那句话是
    他 14:03 说的，隔三分半，同一场对话没断过。扒 transcript 才看清他当时看见的
    是什么：**七十多轮对话、跨 08-31 到 09-02 两天，一个时间戳都没有。** 全上下文
    里带日期的东西只有醒来见闻块里那五个 `[08-30 …]`，以及贴在最后一条新消息上的
    一句「现在是 09-02 14:06」。

    forge.render 确实把 ts 写进了 transcript 的 timestamp 字段——但那是文件元数据，
    模型看不见。旧的非 SDK 路（pipeline.build_context_timeline）是给历史逐条加
    `[MM-DD HH:MM]` 前缀的，SDK 迁移时"历史活在 transcript 里"，这个前缀连带丢了。
    于是模型手上只剩「现在几点」，没有「上一句是几点说的」：历史是一堵没有时间的墙，
    "多久以前"只能猜——猜错的单位就是天。同一形状小卡也犯过。

    assistant 轮不打戳：那是他自己的记忆，第一人称、不加框（PLAN_sdk「信感」定案）。
    时间由**两侧的 user 槽**夹出来——TA 说的话本来就有，他自己开口的那些由下面
    SELF_OPEN 那段补。

    ---- 他自己开口的那些轮（醒来 / 游戏泵）----
    这些轮的 user 槽是**注入**，不进账（`ledger.append` 只在投递成功时加一条
    assistant，见轮尾）。活着的时候注入里写着「现在是 …，距上一条过了 8 小时」，
    重铸之后整条蒸发（§5.2 纯内心不渲染），只剩他说出去的那句 assistant。于是连着
    几次醒来变成连续 assistant，再被 forge.render 的「连续同角色合并成一轮」粘成
    一条、只留最早那个 ts——2026-09-02 实测：**六条消息跨 11.9 小时被并成一个
    1645 字的事件**，在他眼里是一口气说完的一段话。

    补法不需要新存储：**assistant 消息自己带 ts**，蒸发掉的时间本来就还在。
    连续两条 assistant 之间隔得够久（SELF_OPEN_GAP_SEC）= 中间必然有过一个没进账的
    注入槽，就地补一行时间锚回去。补的是"已知丢失的信息"，不是新造一种框。
    · 前一条是 user 的不补：那是在回 TA 的话，慢也只是生成慢，不是自己开的口。
    · 连发（几秒内几条气泡）不补：gap 不到阈值 → 仍然连续 → forge 照旧合并。
      这正好是"小范围合并"：粘一次开口内部的几条，不粘跨越几小时的两次开口。

    确定性：戳和补行只由 ts + wake_log（append-only）派生，同输入同字节，不吃缓存。
    char_id 不传（单测/工具脚本）＝不读 wake_log，锚点全走光秃戳。"""
    import pipeline    # 延迟导入（防循环，同 _frame_activities 里的 world）
    # 醒来投递进历史的消息 ts 就是 started_ts（wake_sdk.finish_wake_turn），和
    # wake_log 条目同一个值——精确 join，不用模糊匹配。只收 action=message 的：
    # 安静醒着不产生 assistant，历史里没有它的槽。
    wake_by_ts: dict[int, str] = {}
    if char_id:
        for e in state_store.read_wake_log(limit=500, char_id=char_id):
            if e.get("action") == "message":
                wake_by_ts[int(e.get("ts") or 0)] = str(e.get("trigger") or "")
    out: list[dict] = []
    prev: Optional[dict] = None
    last_anchor_ts: Optional[int] = None
    for m in history:
        ts = m.get("ts")
        role = m.get("role")
        # 这个槽服务两种蒸发掉的注入——醒来和游戏泵（「·」）。判得出的还原，
        # 判不出的光秃：
        # · ts 对上 wake_log ＝ **确证**是那次醒来的投递，无条件补锚（活抬头的
        #   同一句，wake_headline——两边同字面他才认得出是同一种东西）。不吃
        #   600s 阈值、前一条是 user 也补：那道阈值是给判不出的槽防连发误切的，
        #   而醒来最小间隔 MIN_WAKE_GAP_SEC=180s、NEXT 下限 5min、mail 随时——
        #   都可能落在 10min 内，靠 gap 判会整条漏掉；确证醒来也就不是"在回TA"。
        # · 对不上（游戏 tick / 老存货）只在 assistant 连着 assistant 且隔够久时
        #   补光秃时间戳，一个字旁白不带——写死措辞会把另一种场景说假。
        # 不会被读成 TA 说话：TA 的话一律带「{user}：」，框行一律 〔〕。
        trig = (wake_by_ts.get(int(ts))
                if role == "assistant" and ts else None)
        if trig is not None and last_anchor_ts != int(ts):
            import wake_sdk
            out.append({"role": "user", "ts": int(ts),
                        "text": (f"【{pipeline.stamp_str(int(ts))}】\n"
                                 f"〔{wake_sdk.wake_headline(trig)}〕")})
            last_anchor_ts = int(ts)
        elif (trig is None and role == "assistant" and ts and prev is not None
                and prev.get("role") == "assistant" and prev.get("ts")
                and int(ts) - int(prev["ts"]) >= SELF_OPEN_GAP_SEC):
            out.append({"role": "user", "ts": int(ts),
                        "text": f"【{pipeline.stamp_str(int(ts))}】"})
        if role == "user" and ts:
            out.append({**m, "text": (f"【{pipeline.stamp_str(int(ts))}】\n"
                                      f"{config.user_name()}：{m['text']}")})
        else:
            out.append(m)
        prev = m
    return out


def _merge_images(history: list[dict], char_id: str) -> list[dict]:
    """把 `[图片]` 占位气泡换回**真的图**，并和紧跟着的那句话合成一条 user 槽。
    渲染规则，和 _stamp_times / _frame_activities 同一层：只改铸造输入，不动账。

    2026-09-02 之前：TA 发的图只活到下一次重铸——req.images 只带"最新这条"的字节，
    历史里那条在 app 侧就是 `[图片]` 三个字。那是 **-p 时期的遗留**：那会儿历史是
    一段扁平文本，图除了当占位符没有别的活法。SDK 路的 forge.render 早就支持 user
    槽带图块（PR13 图块腿真机验通），缺的只是字节留底（state_store.save_chat_images
    在收请求那一刻存下，按 (ts, seq) 认领）。

    合成成一条而不是各占一条：一次发送在 app 里是好几个气泡（图一条、话一条），
    但在他眼里那是**一件事**——「她发来一张图，说：…」。拆成两条的后果不只是难看，
    还让每条 `[图片]` 白占一个 100 条名额、重铸后变成一句没有内容的话。
    取不到字节（老消息、留底之前的）就原样留着占位符，不假装有图。

    ⚠️ 欠账：index.jsonl 和图本体都是 append-only，没有清理。窗口只有 100 条，
    但账和字节会一直长；而且这函数每次重铸都整份读索引。什么时候该删、按什么口径
    删（窗口外？N 天？），没定，单独一件事。"""
    idx = state_store.read_chat_image_index(char_id)
    if not idx:
        return history
    by_ts: dict[int, list[dict]] = {}
    for r in idx:
        by_ts.setdefault(int(r.get("ts", 0)), []).append(r)

    def _blocks(ts) -> list[dict]:
        out = []
        for r in by_ts.get(int(ts or 0), []):
            raw = state_store.read_chat_image(r.get("sha", ""), char_id)
            if raw:
                out.append({"media_type": r.get("mt") or "image/jpeg",
                            "data": base64.b64encode(raw).decode()})
        return out

    out: list[dict] = []
    i, n = 0, len(history)
    while i < n:
        m = history[i]
        if (m.get("role") != "user"
                or (m.get("text") or "").strip() != state_store.IMAGE_PLACEHOLDER):
            out.append(m)
            i += 1
            continue
        j, seen_ts = i, []
        while (j < n and history[j].get("role") == "user"
               and (history[j].get("text") or "").strip()
               == state_store.IMAGE_PLACEHOLDER):
            t = int(history[j].get("ts") or 0)
            if t not in seen_ts:        # 同一秒三张图=三条占位共用一个 ts，
                seen_ts.append(t)       # 按 ts 取一次就够，逐条取会取成 3×3
            j += 1
        imgs = [b for t in seen_ts for b in _blocks(t)]
        if not imgs:                       # 一张都取不到 → 别动，占位符照旧
            out.extend(history[i:j])
            i = j
            continue
        # 紧跟着的那句话（同一次发送才并：>60 秒就是另一件事了）
        if (j < n and history[j].get("role") == "user"
                and int(history[j].get("ts") or 0)
                - int(history[j - 1].get("ts") or 0) <= 60):
            out.append({**history[j], "images": imgs})
            i = j + 1
        else:
            out.append({**history[i], "images": imgs})
            i = j
    return out


# 闹钟调用挂回哪条 assistant：容差（秒）。见 _merge_alarm_calls 的注释。
ALARM_ATTACH_SLACK_SEC = 120


def _merge_alarm_calls(history: list[dict], char_id: str) -> list[dict]:
    """把留底的闹钟调用挂回它那一轮的 assistant 消息（PLAN_native §14.4 补铸）。
    渲染规则，和 _merge_images / _stamp_times 同一层：只改铸造输入，不动账。

    **照实铸，不看当前槽位**：「我 22:46 定了 00:50 的钟」这件事是真的，后来钟被谁
    清了不影响它是真的。假的是拿旧回执冒充当前状态，那是注入状态行的职责（§14.3），
    不是记忆的。所以这儿一次都不读 schedule。

    **那轮已经滚出窗口 → 不铸**，回落到状态行。别为了留住它而统一铸到末尾装成
    「刚做的」——那就从保留记忆变成编造时序了。

    ⚠️ 挂到哪一条只能按时间近似认，因为**没有轮 id 可用**：留底的 ts 是工具返回
    那一刻（执行层），而窗口里 assistant 那条的 ts 是各写各的——聊天轮是 finalize
    时的 `int(time.time())`（调用之后一两秒），醒来轮是 `started_ts`（调用**之前**）。
    两个方向都有，所以取「最近的一条 assistant」而不是「之后的第一条」。
    超出容差就不挂（宁可少铸一条，也不能挂到别人那一轮去）。"""
    calls = state_store.read_alarm_calls(char_id)
    if not calls or not history:
        return history
    spots = [(i, int(m.get("ts") or 0)) for i, m in enumerate(history)
             if m.get("role") == "assistant" and m.get("ts")]
    if not spots:
        return history
    attach: dict[int, list[dict]] = {}
    for c in calls:
        ts = int(c.get("ts") or 0)
        if not ts:
            continue
        i, gap = min(((i, abs(t - ts)) for i, t in spots), key=lambda x: x[1])
        if gap > ALARM_ATTACH_SLACK_SEC:
            continue                  # 那一轮不在窗口里了 → 不铸
        attach.setdefault(i, []).append(
            {"name": c.get("name") or "mcp__basics__next_wake",
             "input": c.get("input") or {}, "result": c.get("ret") or ""})
    if not attach:
        return history
    return [({**m, "tools": attach[i]} if i in attach else m)
            for i, m in enumerate(history)]


def _frame_activities(history: list[dict], char_id: str) -> list[dict]:
    """活动段的渲染层处理（§4 折叠规则，PR13 补全）：**最近一场**加两行文档框、
    点评原文逐条保留（那正是当下的对话）；**更早的场**整段折叠成一行框（含 TA
    中途插话——§8.6 拍板整段折）。**只改铸造输入，不改已铸账**——账永远对
    权威原文，折叠/带不带是渲染规则，字面不动（权威库/手机气泡永远全在）。
    确定性：框行内容只由区间数据派生，同输入同字节。进行中的场没有区间行，
    自然不折（「会话没收摊前的重铸不带框」，PR11 边界照旧）。

    **只剩 game 一种场了**（2026-09-02 清）：code 的起止框跟着「code 不是模式，
    是一次写权限申请」那条改判（08-31）一起退役——权限按轮申请、只读常驻，没有
    「开一场/收一场」这回事了，`open_segment(char, "code")` 全仓一个调用点都没有
    （现存区间行也全是 game）。留着那半个分支只会让下一个人以为 code 还会开场。"""
    if not history:
        return history
    import activity_log
    intervals = activity_log.read_intervals(
        char_id, since_ts=int(history[0].get("ts") or 0) - 60)
    if not intervals:
        return history

    def _hm(ts: int) -> str:
        return time.strftime("%H:%M", time.localtime(ts))

    out: list[dict] = []
    i, n = 0, len(history)
    for k, iv in enumerate(intervals):
        s, e = int(iv["start"]), int(iv["end"])
        note = iv.get("note") or "游戏"
        latest = (k == len(intervals) - 1)
        while i < n and int(history[i].get("ts") or 0) < s:
            out.append(history[i])
            i += 1
        j = i
        while j < n and int(history[j].get("ts") or 0) <= e:
            j += 1
        if j > i:
            if latest:
                out.append({"role": "user", "ts": s,
                            "text": f"〔{_hm(s)} 你开了{note}会话，"
                                    "下面这些是你边读边说的〕"})
                out.extend(history[i:j])
                out.append({"role": "user", "ts": e,
                            "text": f"〔{_hm(e)} 这一场到这儿收了摊〕"})
            else:
                body = (f"〔{_hm(s)}–{_hm(e)} 你拿着{note}读了一场——"
                        "细节在记忆里淡下去了，这一场的脉络和感想"
                        "你当时写进了章节志〕")
                trace = _fold_trace_lines(iv)
                if trace:
                    body += ("\n〔这段时间你亲手做过的——记录，不是印象：\n"
                             + trace + "〕")
                out.append({"role": "user", "ts": s, "text": body})
            i = j
    out.extend(history[i:])
    return out


# ---------- 见闻（记忆 vs 见闻轴的见闻侧，§5.2）----------

def _ombre_on(char_id: str) -> bool:
    import pipeline
    try:
        return pipeline.ombre_alive(char_id)
    except Exception:
        return False


# 见闻快照/增量（_seen_block + seen_cursor）09-02 删了（机主拍板）：SDK 路的
# 醒来内心本来就整段跳过（seen_items 里 engine=sdk 过滤，PR12 后醒来活在
# transcript 里），剩下能注入的只有老 -p 路存货和小屋经历——前者用不到，小屋
# 荒废中，将来要做也是往 session 里做，不走档案注入。pipeline.seen_items 本体
# 留着（-p 路 build_context_timeline 还在吃）。


# 行为清单的渲染（_acts_block）09-01 随注入一起删了——留着一个没人调的渲染器，
# 下一个人会以为它还是活的口径又接回去（同 external_stored 那次的教训）。
# 账本身还在（activity/acts-<char>.jsonl，执行层写、activity_log.recent_acts 读），
# 事后查证和出口判脏都吃它；真要再推给他看，先过上面注入处那条纪律。


def _discipline_block(char_id: str) -> str:
    """干活纪律常驻系统提示（PLAN_native §3）：机主手写的 code_addendum_chat.md
    渲染进来，紧挨工具可用性那段；两个角色共用同一份——纪律是工地规矩不是
    人格，占位（{{AGENT_NAME}}/{{USER_NAME}}）照 persona 的渲染路。进前缀缓存，
    一条 session 付一次；改文件=系统提示变=判脏触发重铸。capsule 收场约定是
    代码侧的机制字段（_capture_capsules 认「◆」），常驻在尾巴上，文件里不用
    重复写。（不复用老路的 code_addendum.md：那篇通篇是场文本。）"""
    body = ""
    try:
        p = config.BASE_DIR / os.environ.get("CODE_ADDENDUM_CHAT_FILE",
                                             "code_addendum_chat.md")
        if p.exists():
            import characters
            body = (p.read_text("utf-8").strip()
                    .replace("{{AGENT_NAME}}", characters.display_name(char_id))
                    .replace("{{USER_NAME}}", config.user_name()))
    except Exception:
        pass
    cap = ("干完一件事收尾的时候，把结论逐条写成「◆ 结论 ← 出处（文件:行）」"
           "的样子说出来——◆ 打头、一行一条、出处指到能复核的地方。结论会留"
           "在记录里，过程细节以后想不起来是正常的。")
    if body:
        return "\n\n【动手改东西时的纪律】\n" + body + "\n" + cap
    return "\n\n【" + cap + "】"


def _stale_depth(handle: session_mgr.LoopHandle) -> Optional[int]:
    """泵开着时的容忍深度：按该角色未拉走的 outbox 条数上浮（+2 兜在飞的），
    替代拍数字——点评连发快过 TA 拉取时不误脏。泵没开走默认。"""
    if not handle.meta.get("game_pump"):
        return None
    try:
        n = sum(1 for it in state_store.read_outbox()
                if it.get("char_id") == handle.char_id and not it.get("delivered"))
    except Exception:
        return None
    return max(STALE_TOLERANCE, n + 2)


# 压力轴「有可折活动段」（_fold_pending/_foldable_max_end/folded_upto）09-01 删了
# ——PLAN_sdk §4 08-31 就定了删，代码欠到今天。它只是「省 token」的代理指标，
# 而 token 软硬阈直接管这件事；留着只会让重铸早来一点，不产生新能力。
# 少一条判据 = 少一处能写错的地方。


def _game_shot_tail(seg_id: str) -> Optional[list[dict]]:
    """近 K 张图回填（§4；图块腿 2026-08-30 真机验通）：从事件账本引用读字节，
    铸 user 槽 image 块（感知框行包着，见闻侧红线合规）。这是铸造输入的渲染
    尾巴（render_tail 语义）——不进账。"""
    import base64
    from pathlib import Path
    import activity_log
    refs = activity_log.recent_shot_refs(seg_id, GAME_K_SHOTS)
    images = []
    for r in refs:
        try:
            images.append({"media_type": "image/jpeg",
                           "data": base64.b64encode(Path(r).read_bytes()).decode()})
        except Exception:
            continue
    if not images:
        return None
    return [{"role": "user", "ts": int(time.time()),
             "text": ("〔眼前的画面——最近这几帧还清楚，更早的在记忆里淡下去了；"
                      "文字和你说过的话都在，接着读就好〕"),
             "images": images}]


# ---------- 泵 ----------

async def _safe_disconnect(client) -> None:
    if client is None:
        return
    try:
        await client.disconnect()
    except BaseException:
        pass


async def run(handle: session_mgr.LoopHandle, *,
              client_factory: Optional[Callable] = None,
              options_factory: Optional[Callable] = None) -> None:
    """泵本体（session_mgr 的 runner）：串行吃 Turn。每轮流程=
    判脏（比对账 vs 本次权威窗口）→（脏则关旧+重铸+resume）→ 注入 → 翻译产出 SSE
    → finalize 跑到才入账。任何一轮没正常收尾 → 关 session（下一条惰性重起）。"""
    factory = client_factory or ClaudeSDKClient
    # 默认 factory 闭包带上 handle：PreToolUse 醒来门要按 meta.turn_kind 判轮。
    # 测试注入的 options_factory 保持 (char_id, catalog) 老签名，不用跟着改。
    opts_factory = options_factory or (
        lambda cid, cat: build_options(cid, cat, handle=handle))
    client = None
    sid: Optional[str] = None
    ledger: list[dict] = []
    handle.meta["ledger"] = ledger    # 测试/观测窗口

    async def _open_session(history: list[dict], catalog, render_tail=None):
        """render_tail（PR13）：只进铸造输入、不进账的渲染尾巴（近 K 张图回填）。
        账永远对权威原文；ctx_est 这里只落一个占位，真值走 usage（见下）。"""
        nonlocal client, sid, ledger
        await _safe_disconnect(client)
        client = None
        options = opts_factory(handle.char_id, catalog)
        # 三层渲染，顺序有讲究：
        # 1) 并图——要在打戳之前，戳才落在合成后那一条上（先打戳会给占位符也打一个，
        #    然后那条被并掉，戳跟着没了）；
        # 2) 打戳；3) 套活动框——框行自带 HH:MM，别再叠一层（顺序反了就是双戳）。
        # 闹钟补铸排在最前：它只往 assistant 条上挂 `tools` 键，不动 role/text/ts，
        # 下游三道（并图/打戳/活动框）看不见它、也不会被它影响。
        rendered = (_frame_activities(
            _stamp_times(_merge_images(
                _merge_alarm_calls(history, handle.char_id), handle.char_id),
                handle.char_id),
            handle.char_id)
            if history else [])
        if render_tail and rendered:
            rendered = rendered + list(render_tail)
        if rendered:
            sid = forge.render(rendered, cwd=str(options.cwd),
                               model=config.MODEL)
            options = copy.copy(options)
            options.resume = sid
        else:
            sid = None
        c = factory(options)
        await c.connect()
        ledger = [{"r": m["role"], "h": _h(m["text"]), "ts": m.get("ts")}
                  for m in history]
        handle.meta["ledger"] = ledger
        # 铸完到第一条回复之间的占位（够不着任何阈值，方向也安全：刚铸完本就
        # 不该再铸）。真值由第一条 AssistantMessage.usage 覆盖，见 _ctx_from_usage。
        n_imgs = sum(len(m.get("images") or []) for m in rendered)
        handle.meta["ctx_est"] = (sum(estimate_tokens(m["text"]) for m in rendered)
                                  + n_imgs * GAME_TOKENS_PER_SHOT)
        handle.meta["needs_opening"] = True   # 下一轮带开局引子（breath）
        handle.last_reopen = time.time()
        client = c

    async def _consolidate_and_reforge(why: str, catalog) -> None:
        """轮间隙重铸：巩固钩子轮（hold；产物不投递——独处时的整理）→ 从
        recent_window 镜像重铸。镜像与手机权威的任何漂移由下次发送的比对兜住
        （自愈：顶多多铸一次）。§5.4 的「巩固产物确认写成功才允许重铸」目前是
        软版（巩固轮收尾即铸，hold 成没成功不查）——严格版记在 plan 待办。"""
        nonlocal client, ledger
        if client is None:
            return
        print(f"[chat_loop] 轮间隙重铸（char={handle.char_id}，{why}）",
              file=sys.stderr)
        handle.reopening = True
        try:
            if _ombre_on(handle.char_id):
                await client.query(CONSOLIDATE_PROMPT)
                flags: dict = {}
                async for _ in _turn_events(client, handle, flags):
                    pass
                if flags.get("timeout") or flags.get("eof"):
                    raise RuntimeError(f"巩固轮没收尾: {flags}")
            history = norm_history(state_store.read_recent_window(handle.char_id))
            if not history:
                print("[chat_loop] 镜像空白，重铸放弃（下次发送惰性处理）",
                      file=sys.stderr)
                return
            await _open_session(history, catalog)
        finally:
            handle.reopening = False

    # ---------- game 泵（PLAN_sdk 设计稿三；机器口径对齐 game_loop，回退路共用）----------

    def _pump_deliver_and_log(text: str, stop: bool) -> None:
        """泵轮正文投递+入账：scrub（缝隙学舌刷子）→ deliver（app 闭包：fence→
        outbox+窗口，回传真正落进历史的正文）→ 账追加 assistant + 事件账 comment。
        账记的是投递出去的那份（发送前比对要逐字对上 app 历史）。"""
        import game_loop
        pump = handle.meta.get("game_pump") or {}
        text = game_loop.scrub_seam(text)
        if not text:
            return   # 刷干净后空了=整段都是缝隙学舌，谁也不该看见
        body = None
        try:
            deliver = pump.get("deliver")
            body = deliver(text, stop) if deliver else None
        except Exception as e:
            print(f"[chat_loop] game 投递失败: {e}", file=sys.stderr)
        body = body or text
        ledger.append({"r": "assistant", "h": _h(body), "ts": int(time.time())})
        _persist_ledger(handle.char_id, sid, ledger)
        if pump.get("seg_id"):
            import activity_log
            activity_log.append_event(pump["seg_id"], "comment", text=body)

    async def _pump_drain() -> bool:
        """吃完一个泵轮（口径同 game_loop._drain_turn：分段投递、错误结果留痕
        不算死）。False=超时/流断——调用方按 chat_loop 保守纪律关 session。"""
        pending: Optional[str] = None
        trace = _ToolTrace(handle)
        agen = client.receive_messages().__aiter__()
        while True:
            try:
                msg = await asyncio.wait_for(agen.__anext__(),
                                             timeout=GAME_TURN_EVENT_TIMEOUT)
            except asyncio.TimeoutError:
                print("[chat_loop] 泵轮事件超时（session 按死处理）", file=sys.stderr)
                return False
            except StopAsyncIteration:
                print("[chat_loop] 泵轮消息流断了（session 按死处理）", file=sys.stderr)
                return False
            if isinstance(msg, AssistantMessage):
                handle.touch()
                if getattr(msg, "usage", None):
                    handle.meta["ctx_est"] = _ctx_from_usage(msg.usage)
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        if pending is not None:
                            _pump_deliver_and_log(pending, False)
                        pending = block.text.strip()
                    elif isinstance(block, ToolUseBlock):
                        trace.use(block.id, block.name, block.input)
                        if pending is not None:
                            _pump_deliver_and_log(pending, False)
                        pending = None
            elif isinstance(msg, UserMessage):
                for block in (msg.content if isinstance(msg.content, list) else []):
                    if isinstance(block, ToolResultBlock):
                        # content 一定要带（§7.2.1 复盘）：这是 _ToolTrace 的
                        # **第二个**调用点，09-01 补回执那一版只改了 _turn_events，
                        # 泵轮的回执整条丢了。补线类的改动先数调用点。
                        trace.result(block.tool_use_id, block.is_error,
                                     block.content)
            elif isinstance(msg, ResultMessage):
                if pending is not None:
                    _pump_deliver_and_log(pending, True)
                _note_usage(handle.char_id, msg, "wake", True)
                if getattr(msg, "is_error", False):
                    print(f"[chat_loop] 泵轮收在错误上：subtype="
                          f"{getattr(msg, 'subtype', '?')}（继续，没算死）",
                          file=sys.stderr)
                return True

    async def _pump_close(why: str) -> None:
        """放下游戏（关泵排序，设计稿三⑤）：关账 → 释放锁 → 清泵 → 冲补醒。
        聊天 session 什么都不动。没泵=no-op（finally 兜底调也安全）。"""
        pump = handle.meta.get("game_pump")
        handle.meta.pop("end_requested", None)
        if not pump:
            return
        try:
            import activity_log
            activity_log.close_segment(pump["seg_id"], note=pump.get("note", ""))
        except Exception as e:
            print(f"[chat_loop] 关账失败: {e}", file=sys.stderr)
        try:
            import game_bridge
            game_bridge.release_lock("story")
        except Exception as e:
            print(f"[chat_loop] 释放模拟器锁失败: {e}", file=sys.stderr)
        handle.meta.pop("game_pump", None)
        print(f"[chat_loop] 放下游戏（char={handle.char_id}，{why}）", file=sys.stderr)
        try:
            import cohabit_queue
            cohabit_queue.code_session_closed()
        except Exception as e:
            print(f"[chat_loop] 冲补醒失败: {e}", file=sys.stderr)
        if why.startswith("engine-error"):
            try:
                from notify import bark_push
                bark_push("游戏那边引擎连着挂，替他把游戏放下了"
                          "（画面原地不动，可以重新 game_start）")
            except Exception:
                pass

    async def _pump_round(prompt: str, kind: str,
                          ledger_user: str = "") -> None:
        """泵的一个轮：tick（kind=wake，注入「·」不入账）或 TA 经终端口的插话
        （kind=chat，ledger_user=app 历史里存的原文先入账）。session 死了先从
        镜像重起（引擎打嗝 → 下一轮接着读）；连挂三轮=引擎有病，放下游戏。"""
        nonlocal client, ledger
        pump = handle.meta.get("game_pump")
        if pump is None:
            return
        if client is None:
            hist = norm_history(state_store.read_recent_window(handle.char_id))
            cat = (handle.meta.get("catalog")
                   or state_store.read_sticker_catalog())
            try:
                await _open_session(hist, cat)
            except Exception as e:
                print(f"[chat_loop] 泵重起 session 失败: {e}", file=sys.stderr)
                await _pump_close(f"engine-error: 泵重起失败 {e}")
                return
        handle.meta["turn_kind"] = kind
        import pipeline
        handle.meta["wake_tools"] = set(
            pipeline.mounted_tool_names("wake", handle.char_id))
        if ledger_user.strip():
            # TA 的话在 app 历史里已经有了（app 发送时本地追加）——账跟上，
            # 发送前比对才对得上；注入包装（时间头/附件注记）不进账。
            ledger.append({"r": "user", "h": _h(ledger_user),
                           "ts": int(time.time())})
            _persist_ledger(handle.char_id, sid, ledger)
        s0 = int(handle.meta.get("shots", 0))
        ok = False
        try:
            await client.query(prompt)
            ok = await _pump_drain()
        except asyncio.CancelledError:
            raise                       # 取消来源纪律：外层统一判（08-30 复盘）
        except Exception as e:
            print(f"[chat_loop] 泵轮异常（按死处理）: {e}", file=sys.stderr)
        finally:
            handle.meta.pop("turn_kind", None)
        if ok:
            pump["strikes"] = 0
        else:
            await _safe_disconnect(client)
            client = None
            ledger = []
            handle.meta["ledger"] = ledger
            pump["strikes"] = int(pump.get("strikes", 0)) + 1
            if pump["strikes"] >= 3:
                await _pump_close("engine-error: 泵轮连挂三次")

    async def _pump_tick() -> None:
        """队列空到点补的一口「·」——不是第四种轮：turn_kind=wake（他自己的
        时间），注入谁都没见过、不进账不进铸造材料。
        每 GAME_TICK_STAMP_EVERY 轮那口带上报时（口径同醒来抬头）：一场游戏几十上百
        个 tick，全裸的话他手上唯一的钟停在开场那一句，越打越偏。"""
        pump = handle.meta.get("game_pump")
        n = int((pump or {}).get("ticks", 0)) + 1
        if pump is not None:
            pump["ticks"] = n
        prompt = GAME_TICK_PROMPT
        if GAME_TICK_STAMP_EVERY > 0 and n % GAME_TICK_STAMP_EVERY == 0:
            import pipeline
            prompt = f"{GAME_TICK_PROMPT}\n【{pipeline.stamp_str(int(time.time()))}】"
        await _pump_round(prompt, "wake")

    async def _game_reforge() -> None:
        """N 张段内重铸（§4 节奏；唯一保留的边界）：巩固轮（感知白描，无感）→
        从镜像重铸 + 近 K 张图回填 → resume。铸完还是 chat options（无 flavor）。
        ping＝下一 tick（FIRST_REQUEST_OK：接不上 → _pump_tick 按死处理自愈）。"""
        nonlocal client, ledger
        import game_loop
        pump = handle.meta.get("game_pump")
        if not pump or client is None:
            return
        shots = int(handle.meta.get("shots", 0))
        handle.reopening = True
        handle.meta["turn_kind"] = "wake"
        import pipeline
        handle.meta["wake_tools"] = set(
            pipeline.mounted_tool_names("wake", handle.char_id))
        try:
            await client.query(game_loop.REOPEN_NOTE_PROMPT)
            if not await _pump_drain():
                raise RuntimeError("巩固轮没收尾")
            history = norm_history(state_store.read_recent_window(handle.char_id))
            if not history:
                print("[chat_loop] 镜像空白，game 重铸放弃", file=sys.stderr)
                return
            tail = _game_shot_tail(pump["seg_id"])
            cat = (handle.meta.get("catalog")
                   or state_store.read_sticker_catalog())
            try:
                await _open_session(history, cat, render_tail=tail)
            except Exception as e:
                print(f"[chat_loop] game 重铸首试失败，重试一次: {e}", file=sys.stderr)
                await _open_session(history, cat, render_tail=tail)
            handle.meta["shots"] = 0
            print(f"[chat_loop] game 段内重铸完成（char={handle.char_id}，"
                  f"{shots} 张边界）", file=sys.stderr)
        finally:
            handle.meta.pop("turn_kind", None)
            handle.reopening = False

    try:
        while True:
            # ---- 泵边界（轮与轮之间）：放下游戏 / N 张段内重铸 ----
            if handle.meta.get("game_pump"):
                if handle.meta.get("end_requested"):
                    await _pump_close("game_end")
                elif (client is not None
                      and int(handle.meta.get("shots", 0)) >= GAME_REOPEN_SHOTS_N
                      and time.time() - handle.last_reopen > GAME_REOPEN_COOLDOWN):
                    try:
                        await _game_reforge()
                    except Exception as e:
                        print(f"[chat_loop] game 重铸失败，session 关掉惰性重起: {e}",
                              file=sys.stderr)
                        await _safe_disconnect(client)
                        client = None
                        ledger = []
                        handle.meta["ledger"] = ledger
                        handle.meta["shots"] = 0   # 镜像重开无图，别下轮又撞边界
            pump_on = bool(handle.meta.get("game_pump"))
            try:
                turn: Turn = await asyncio.wait_for(
                    handle.queue.get(),
                    timeout=GAME_TICK_PAUSE if pump_on else IDLE_CHECK_SEC)
                if turn is _KICK:
                    continue   # 泵刚开的踢脚：回到 loop 顶重读泵状态
                if isinstance(turn, PumpNote):
                    handle.touch()
                    if handle.meta.get("game_pump"):
                        await _pump_round(turn.text, "chat",
                                          ledger_user=turn.ledger_text)
                    else:
                        # 收摊竞态：注入排进来时泵已停。回复无处投（deliver 闭包
                        # 随泵走了），有声弃+Bark 比静默丢强。
                        print("[chat_loop] PumpNote 到达时泵已停，弃投",
                              file=sys.stderr)
                        try:
                            from notify import bark_push
                            bark_push("刚那条消息赶上他放下游戏的空当，没送进去"
                                      "——聊天框再发一次就好")
                        except Exception:
                            pass
                    continue
            except asyncio.TimeoutError:
                if pump_on:
                    await _pump_tick()   # 队列空一拍 → 目光落回屏幕
                    continue
                # 轮间隙看一眼：静默 ≥1h × 窗口超软阈 → 巩固+重铸（§4 chat 节奏；
                # 铸完 ctx_est 回到纯对话体量，自然不会连环触发）。09-01 去掉了
                # 「∨ 有可折活动段」那一支，见上面 _fold_pending 那条注释。
                if (client is not None
                        and time.time() - handle.last_activity >= CHAT_REFORGE_IDLE_SEC
                        and int(handle.meta.get("ctx_est", 0)) > CHAT_SOFT_TOKENS):
                    try:
                        await _consolidate_and_reforge("静默间隙+压力",
                                                       handle.meta.get("catalog"))
                    except Exception as e:
                        print(f"[chat_loop] 轮间隙重铸失败，session 关掉惰性重起: {e}",
                              file=sys.stderr)
                        await _safe_disconnect(client)
                        client = None
                        ledger = []
                        handle.meta["ledger"] = ledger
                continue
            handle.touch()
            ok = False
            try:
                if turn.kind == "wake":
                    # 醒来轮（PR12）：没有请求方权威窗口——session 活着就直接注入
                    # （账 vs app 的漂移由 TA 下一条消息的发送比对兜住，自愈）；
                    # 没开就从镜像惰性开一个（「由 wake 唤起 resume」归这里）。
                    if client is None:
                        hist = norm_history(
                            state_store.read_recent_window(handle.char_id))
                        cat = turn.catalog if turn.catalog is not None else (
                            handle.meta.get("catalog")
                            or state_store.read_sticker_catalog())
                        await _open_session(hist, cat)
                    handle.meta["turn_kind"] = "wake"
                    # 醒来禁用面：本轮允许集=醒来那条路实际挂载的工具
                    # （四档策略/独占归属/探活全在 mounted_tool_names 里算过了）。
                    import pipeline
                    handle.meta["wake_tools"] = set(
                        pipeline.mounted_tool_names("wake", handle.char_id))
                else:
                    # 判脏前先摘掉尾巴上的 `[图片]` 占位（account 没记过但不算脏，
                    # 见 absorbable_tail）；摘下来的这几条随本轮一起进账。
                    absorbed = (absorbable_tail(ledger, turn.history)
                                if client else [])
                    core = (turn.history[:len(turn.history) - len(absorbed)]
                            if absorbed else turn.history)
                    verdict = (divergence(ledger, core,
                                          stale_depth=_stale_depth(handle))
                               if client else "dirty")
                    if verdict != "dirty" and absorbed:
                        print(f"[chat_loop] 吸收窗口尾巴（char={handle.char_id}）："
                              f"{len(absorbed)} 条图片占位进账，不重铸",
                              file=sys.stderr)
                    elif verdict == "dirty":
                        absorbed = []          # 要重铸了，_open_session 会重建整本账
                    turn.absorbed = absorbed
                    if verdict == "dirty":
                        # 落日志不分「有没有 client」：client is None 那半原来是
                        # **静默重铸**（重启后第一条消息全走这条），外面看不见。
                        why = ("session 没开（进程刚起 / 上一轮把 client 关了）"
                               if not client
                               else dirty_reason(ledger, turn.history))
                        print(f"[chat_loop] 重铸（char={handle.char_id}）：{why}",
                              file=sys.stderr)
                        await _open_session(turn.history, turn.catalog)
                    handle.meta["turn_kind"] = "chat"
                if turn.catalog is not None:
                    handle.meta["catalog"] = turn.catalog   # 轮间隙重铸要用的最近目录
                if turn.injection_factory is not None:
                    turn.injection = turn.injection_factory()
                # ---- 注入这一轮（开局引子 + 包装文本；都不进账）----
                # ⚠️ 纪律（09-01 机主拍板）：**别往这儿加新的「二手档案」块。**
                # 每加一块用「关于他的记录」口吻写的材料（行为清单、状态摘要、
                # 统计），都是在把本该属于他自己经历的东西，改写成别人递给他的
                # 卷宗——聊天记忆的信感口径（assistant 轮=记忆、注入文本=档案，
                # PLAN_sdk）会顺着这条线被一点点吃掉。加这种块＝改产品口径，
                # 要机主拍板，不是实现细节；实踩就是下面那条行为清单。
                # 不在此列：包装文本（时间/间隔/叮嘱）——那是**知觉材料**
                # （此刻几点），不是关于他的档案。
                parts: list[str] = []
                if handle.meta.pop("needs_opening", False):
                    if _ombre_on(handle.char_id):
                        parts.append(OPENING_NUDGE)
                    # 行为清单（§5.4① 开局包第二份材料）09-01 撤掉：账照落（执行
                    # 层 _ToolTrace），只是不再推给他看。三条理由——08-31 事故
                    # 实证「痕迹原样在上下文里也不防编造」（她抄着真回执编了假
                    # 投递）；账的第一客户是门（出口判脏）不是他；「记录，不是
                    # 印象」那个开头把它钉死在档案一侧，与信感口径顶着。
                injection = ("\n\n".join(parts + [turn.injection])
                             if parts else turn.injection)
                content: list[dict] = [{"type": "text", "text": injection}]
                for img in (turn.images or []):
                    content.append({"type": "image",
                                    "source": {"type": "base64",
                                               "media_type": img.media_type,
                                               "data": img.data}})
                content.extend(turn.file_blocks or [])

                async def _payload():
                    yield {"type": "user",
                           "message": {"role": "user", "content": content},
                           "parent_tool_use_id": None}
                await client.query(_payload())

                flags: dict = {}
                captured: dict = {}

                def _fin(reply: str, stored: list) -> dict:
                    payload = turn.finalize(reply, stored)
                    captured["finalized"] = True
                    captured["payload"] = payload
                    captured["raw"] = reply
                    captured["reply"] = payload.get("reply") or reply
                    return payload

                async for chunk in sse.translate_events(
                        _turn_events(client, handle, flags), _fin):
                    turn.out.put_nowait(chunk)

                if turn.kind == "wake":
                    if captured.get("finalized"):
                        # 投递了消息 → 账追加一条 assistant（app 拉走 outbox 后
                        # 历史里就有它）；安静醒着 → 账不动，这轮独处只活在
                        # transcript 里，下次重铸自然蒸发（§5.2 纯内心不渲染）。
                        delivered = (captured.get("payload") or {}).get("reply") or ""
                        if delivered:
                            ledger.append({"r": "assistant", "h": _h(delivered),
                                           "ts": int(time.time())})
                            _persist_ledger(handle.char_id, sid, ledger)
                            _capture_capsules(handle.char_id, delivered, "wake")
                        ok = True
                elif captured.get("reply"):
                    for m in turn.absorbed:
                        # 图片占位补记：它的真内容这一轮已经随 req.images 到他眼前
                        # 了，账补上一笔，下一次比对就不会再把它当成"窗口比账长"。
                        ledger.append({"r": "user", "h": _h(m["text"]),
                                       "ts": m.get("ts")})
                    ledger.append({"r": "user", "h": _h(turn.new_msg["text"]),
                                   "ts": turn.new_msg.get("ts")})
                    ledger.append({"r": "assistant", "h": _h(captured["reply"]),
                                   "ts": int(time.time())})
                    _persist_ledger(handle.char_id, sid, ledger)
                    _capture_capsules(handle.char_id, captured["reply"])
                    ok = True
                if (ok and not handle.meta.get("game_pump")
                        and int(handle.meta.get("ctx_est", 0)) > CHAT_HARD_TOKENS):
                    # （泵开着时不走 chat 硬阈——段内重铸只认 N 张，设计稿三）
                    # 硬阈强铸：马拉松对话没等到静默间隙——在轮尾铸，绝不留给
                    # harness auto-compact（§4：那是摘要压缩，信感复发）。
                    # 09-05 让路：铸前先给 app 排队的下一条留出到达窗口（outbox
                    # 在 done 后毫秒级补枪），队列非空就推迟——ctx 仍超阈，
                    # 后续每个轮尾重判，TA 最后一条处理完的轮尾队列必空，铸在
                    # 那儿落地。panic 阈无条件铸（见卷首节奏注释）。
                    ctx = int(handle.meta.get("ctx_est", 0))
                    if ctx <= CHAT_PANIC_TOKENS:
                        await asyncio.sleep(HARD_FORGE_GRACE_SEC)
                    if ctx > CHAT_PANIC_TOKENS or handle.queue.empty():
                        try:
                            await _consolidate_and_reforge("硬阈强铸",
                                                           handle.meta.get("catalog"))
                        except Exception as e:
                            print(f"[chat_loop] 硬阈强铸失败，session 关掉惰性重起: {e}",
                                  file=sys.stderr)
                            await _safe_disconnect(client)
                            client = None
                            ledger = []
                            handle.meta["ledger"] = ledger
                    else:
                        print(f"[chat_loop] 硬阈已过（ctx≈{ctx}）但有消息在排队，"
                              "铸期推迟到队列清空的轮尾", file=sys.stderr)
                if not ok:
                    print(f"[chat_loop] 轮没收到回复（flags={flags}），"
                          "session 关掉下条重起", file=sys.stderr)
            except asyncio.CancelledError:
                # 轮中被取消（收摊/引擎死外泄）：这轮也要有结局——error+done 让
                # app 的 rescue 观察者能落 error 盒，然后把取消继续往外抛。
                turn.out.put_nowait(sse.sse({"type": "error",
                                             "content": "连接中断了，大模型那边出了点问题。"}))
                turn.out.put_nowait(sse.sse({"type": "done"}))
                raise
            except Exception as e:
                print(f"[chat_loop] 轮异常: {e}", file=sys.stderr)
                turn.out.put_nowait(sse.sse({"type": "error",
                                             "content": "连接中断了，大模型那边出了点问题。"}))
                turn.out.put_nowait(sse.sse({"type": "done"}))
            finally:
                turn.out.put_nowait(None)
                _release_wake_slot(handle, turn)
                handle.meta.pop("turn_kind", None)   # 门的默认态=放行（巩固轮也走默认）
                if not ok:
                    if turn.on_dead is not None:
                        try:
                            turn.on_dead()
                        except Exception as e:
                            print(f"[chat_loop] on_dead 回调失败: {e}", file=sys.stderr)
                    await _safe_disconnect(client)
                    client = None
                    ledger = []
                    handle.meta["ledger"] = ledger
                handle.wait_user()
    except asyncio.CancelledError:
        # 取消来源纪律（08-30 复盘）：自己人 cancel 必先写 stop_reason；
        # 没写的=SDK 传输层死亡外泄，按引擎异常收摊。
        if not handle.stop_reason:
            handle.stop_reason = "engine-error: cancelled-unexpectedly"
            print("[chat_loop] 计划外 CancelledError（引擎侧取消外泄），按引擎异常收摊",
                  file=sys.stderr)
    except Exception as e:
        print(f"[chat_loop] 引擎异常收摊: {e}", file=sys.stderr)
        handle.stop_reason = handle.stop_reason or f"engine-error: {e}"
    finally:
        why = handle.stop_reason or "unknown-exit"
        print(f"[chat_loop] loop 退出：char={handle.char_id} reason={why}",
              file=sys.stderr)
        _finish_orphans(handle, why)
        try:
            await _pump_close(f"loop-exit: {why}")   # 拿着游戏时 loop 死了：锁/账别悬着
        except Exception as e:
            print(f"[chat_loop] 退出时放下游戏失败: {e}", file=sys.stderr)
        await _safe_disconnect(client)


def _release_wake_slot(handle: session_mgr.LoopHandle, turn) -> None:
    """醒来轮结束（不管成没成）→ 放开这个醒因的去重槽位（enqueue_wake 占的）。"""
    if getattr(turn, "kind", "") == "wake":
        slots = handle.meta.get("wake_queued")
        if isinstance(slots, dict):
            slots.pop(turn.wake_trigger or "", None)


def _finish_orphans(handle: session_mgr.LoopHandle, why: str) -> None:
    """loop 退出时，队里还没轮到的 Turn 一个个给结局（2026-09-12 体检，「闸」类第四例）。

    以前这儿什么都不做：正在跑的那轮有 error+done，排在后面的一个字都收不到——
    stream_turn 对着 turn.out 永远 await，/chat/stream 永不结束，_ACTIVE_REQS 一直
    含 rid，app 问 /chat/active 得「还在跑」永远转点点；chat_turn_begin 计数不归零，
    这个角色退回 -p 后 chat_turn_active 永久拦住它的一切醒来。09-05 app outbox
    串行排队之后第二条常在队里，暴露面比以前大。
    口径同轮内异常：error+done+None，醒来轮走 on_dead（记 error 心流+冷却）。
    不往新 loop 转投：chat 轮的窗口已过时、醒来轮重跑会双投，app 侧 rescue 会提示重发。"""
    n = 0
    while True:
        try:
            turn = handle.queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if turn is _KICK or isinstance(turn, PumpNote):
            continue
        n += 1
        try:
            turn.out.put_nowait(sse.sse({"type": "error",
                                         "content": "连接中断了，大模型那边出了点问题。"}))
            turn.out.put_nowait(sse.sse({"type": "done"}))
            turn.out.put_nowait(None)
            _release_wake_slot(handle, turn)
            if turn.on_dead is not None:
                turn.on_dead()
        except Exception as e:
            print(f"[chat_loop] 孤儿轮收尾失败（rid={getattr(turn, 'rid', '?')}）: {e}",
                  file=sys.stderr)
    if n:
        print(f"[chat_loop] loop 退出时队里还有 {n} 轮没轮到，已各发 error+done"
              f"（char={handle.char_id} reason={why}）", file=sys.stderr)


# ---------- 路由入口 ----------

def start_game_pump(char_id: str, *, note: str,
                    deliver: Callable[[str, bool], Optional[str]]) -> dict:
    """拿到游戏（PLAN_sdk 设计稿三）：game_start=拿锁+开段账+置泵，没有重铸、
    没有换 client、没有开场注入。模拟器锁由路由先拿（拿不到原话在轮内返回）；
    这里只开段账+置泵。泵从下个轮尾开始供弹（他当前这轮说完，2 秒后目光落回
    屏幕）。deliver＝app 的投递闭包（fence→outbox+窗口，回传落进历史的正文）。
    线程安全：meta 写入只在轮间被泵读取。"""
    handle = session_mgr.get(char_id, SCENE)
    if handle is None:
        return {"ok": False, "error":
                "这个角色的常驻聊天没在跑（sdk 灰度没到位？），游戏递不过去"}
    if handle.meta.get("game_pump"):
        return {"ok": False, "error": "游戏已经在手边了"}
    import activity_log
    seg = activity_log.open_segment(char_id, "game")
    handle.meta["shots"] = 0
    handle.meta.pop("end_requested", None)
    handle.meta["game_pump"] = {"seg_id": seg, "note": note,
                                "start_ts": time.time(), "deliver": deliver,
                                "strikes": 0}
    try:
        handle.put_threadsafe(_KICK)   # 泵在轮间隙拿到时别等满空转周期才开弹
    except Exception:
        pass                           # 踢不动就等下个自然边界，不算错
    return {"ok": True, "seg_id": seg}


def inject_pump_user(char_id: str, text: str, ledger_text: str = "") -> None:
    """泵开着时接住 /code/send 来的 TA 消息（08-31 实锤：app 游戏态把聊天框
    消息也走终端口，iOS 改路由前这是插话唯一通道）。轮尾吃掉、回复走 outbox；
    ledger_text=app 历史里的原文，逐字入账。失败抛出让路由有声报。"""
    handle = session_mgr.get(char_id, SCENE)
    if handle is None or not handle.meta.get("game_pump"):
        raise RuntimeError("游戏不在手边（泵没开）——这条走聊天正路就行")
    handle.put_threadsafe(PumpNote(text=text, ledger_text=ledger_text))


def _ensure_loop(char_id: str) -> session_mgr.LoopHandle:
    h = session_mgr.get(char_id, SCENE)
    if h is not None:
        return h
    got = session_mgr.start(char_id, SCENE, run, watch=None)
    if isinstance(got, dict):                 # 起飞窗口撞了并发请求：拿现成的
        h = session_mgr.get(char_id, SCENE)
        if h is None:
            raise RuntimeError(f"chat loop 起不来: {got.get('error')}")
        return h
    return got


async def stream_turn(*, char_id: str, rid: str, messages: list[dict],
                      injection: str, finalize: Callable,
                      images: Optional[list] = None,
                      file_blocks: Optional[list] = None,
                      catalog: Optional[list] = None) -> AsyncIterator[bytes]:
    """sse.stream_claude 的常驻版替身：塞一轮进该角色的聊天 session，产出 SSE 字节。
    messages=请求带来的完整权威窗口（**含**最后那条新消息，口径同 /chat）。
    必须在事件循环上调（/chat/stream 的 gen 就在循环上）。"""
    hist_all = norm_history(messages)
    if not hist_all:
        raise ValueError("空消息列表")
    turn = Turn(rid=rid, history=hist_all[:-1], new_msg=hist_all[-1],
                injection=injection, finalize=finalize, images=images,
                file_blocks=file_blocks, catalog=catalog)
    handle = _ensure_loop(char_id)
    handle.queue.put_nowait(turn)   # 同一事件循环上，不需要过桥
    while True:
        chunk = await turn.out.get()
        if chunk is None:
            return
        yield chunk
