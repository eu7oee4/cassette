"""派活提 PR（PLAN_delegate §3）：他挑一个模型、写一份说明，后端起一个一次性 SDK session，
在一份独立的工作副本里按说明改代码、跑测试、提 PR。不弹卡、不占他的轮；做完 / 卡住 /
失败都以 PR 事件回到他下一轮的知觉里（prs._event），并 Bark 机主。

- 工作副本：`.worktrees/<角色>~<slug>/`，每个派活一份，分支 `wake/<角色>/<slug>`（和他自己
  pr_start 的分支同名规则）。`prs.worktree_owner` 取路径第一段 = "<角色>~<slug>" ≠ 角色 id，
  所以他本人的 session 碰不到派活的副本（门照「别人的工作副本」拒）。
- 状态：`state/prs/delegates/<角色>~<slug>.json`，running / asking / opened / failed / cancelled。
- 停下来问（§3.3）：session 不挂着等——记下 session_id 收摊，叫醒他（醒因 delegate）；
  他 pr_delegate_reply 之后用同一套 options + resume= 起新 client 接着跑。
- 重启：runner 在后端进程里，重启会杀掉在跑的；启动扫尾把 running 的 resume 一次
  （第二次被打断才按失败收）。
- 真正起 session 的只有 `_run_session`；测试打桩它。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import config
import prs
import state_store
from prs import PRError

MODEL_ALIASES = {"fable": "claude-fable-5-1"}
MAX_TURNS = int(os.environ.get("DELEGATE_MAX_TURNS", "40"))
TIMEOUT_SEC = int(os.environ.get("DELEGATE_TIMEOUT_SEC", "1800"))     # 累计（含 resume 段）
MAX_QA = 3                 # 一个派活最多来回几次问答，第 4 次停下来按失败收
ASK_MAX_DAYS = 3           # asking 超过这么久没回答 → 按撤回收
RESUME_NOTE = "〔后端重启过，刚才的活接着做〕"
BARK = True                # 测试里关掉（同 prs.BARK_ON_OPEN）

_SLUG_RE = prs._SLUG_RE
_tasks: dict[str, asyncio.Task] = {}      # key → 在跑的 task（只在事件循环线程动）
_sinks: dict[str, dict] = {}              # key → 这段 session 的收尾记录（pr_open 成没成；测试从这儿够）


def _models() -> list[str]:
    return [m.strip() for m in config.DELEGATE_MODELS.split(",") if m.strip()]


def _dir() -> Path:
    return prs.PRS_DIR / "delegates"


def _key(char_id: str, slug: str) -> str:
    return f"{char_id}~{slug}"


def _path(char_id: str, slug: str) -> Path:
    return _dir() / f"{_key(char_id, slug)}.json"


def _load(char_id: str, slug: str) -> Optional[dict]:
    try:
        return json.loads(_path(char_id, slug).read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def _save(rec: dict) -> None:
    _dir().mkdir(parents=True, exist_ok=True)
    p = _path(rec["char"], rec["slug"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)


def all_records() -> list[dict]:
    d = _dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(p.read_text("utf-8")))
        except ValueError:
            continue
    return out


def worktree_path(char_id: str, slug: str) -> Path:
    return prs.REPO_ROOT / ".worktrees" / _key(char_id, slug)


def branch_of(char_id: str, slug: str) -> str:
    return f"wake/{char_id}/{slug}"


ACTIVE = ("running", "asking")


def rows(char_id: Optional[str] = None) -> list[dict]:
    """给 pr_list / app 的派活行（没收口的在前）。"""
    out = []
    for r in all_records():
        if char_id and r["char"] != char_id:
            continue
        out.append({"kind": "delegate", "char": r["char"], "slug": r["slug"], "model": r["model"],
                    "status": r["status"], "pr_id": r.get("pr_id"), "question": r.get("question"),
                    "error": r.get("error"), "started": r.get("started"), "ended": r.get("ended"),
                    "qa": r.get("qa") or [], "brief": r.get("brief")})
    out.sort(key=lambda r: (0 if r["status"] in ACTIVE else 1, r.get("started") or ""), reverse=False)
    return out


def running_now() -> list[str]:
    """重启确认弹窗要列的「在跑的派活」（app.py._running_now）。"""
    import characters
    return [f"{characters.display_name(r['char'])}派给 {r['model']} 的 {r['slug']} 在跑，重启会中断"
            for r in all_records() if r["status"] == "running"]


# ---------- 校验 + 起活 ----------

def _today_count(char_id: str, origin: str) -> int:
    day = datetime.now().astimezone().date().isoformat()
    return sum(1 for r in all_records()
               if r["char"] == char_id and r.get("origin") == origin
               and (r.get("started") or "").startswith(day))


def resolve_model(model: str) -> str:
    m = (model or "").strip()
    m = MODEL_ALIASES.get(m, m)
    if m not in _models():
        raise PRError(f"模型只能选：{'、'.join(_models())}（fable = claude-fable-5-1）")
    return m


def start(char_id: str, slug: str, model: str, brief: str, origin: str = "chat") -> dict:
    """pr_delegate 的真身（REST 线程调）：校验 → 开副本 → 记状态 → 把 session 挂到事件循环上。"""
    prs._check_char(char_id)
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise PRError("slug 只能用小写字母、数字和连字符，40 个字以内，比如 fix-wake-log")
    model = resolve_model(model)
    brief = (brief or "").strip()
    if len(brief) < 20:
        raise PRError("说明太短了。写清四件事：要解决什么、你已经查到的位置、哪些不能动、怎么算做完"
                      "——对方看不到你们的聊天，说明就是它知道的全部。")
    origin = "wake" if origin == "wake" else "chat"
    if config.DELEGATE_CHARS and char_id not in config.DELEGATE_CHARS:
        raise PRError("派活这功能还没给你开（机主在灰度）——想改的话自己 pr_start 在工作副本里改。")
    with prs._flock("_lock"):
        recs = [r for r in all_records() if r["char"] == char_id]
        if any(r["status"] == "running" for r in recs):
            running = next(r["slug"] for r in recs if r["status"] == "running")
            raise PRError(f"你已经有一个派活在跑（{running}），等它完了再派下一个。")
        old = _load(char_id, slug)
        if old and old["status"] in ACTIVE:
            raise PRError(f"{slug} 这个派活还没收口（{old['status']}），换个名字或先 pr_delegate_cancel。")
        if old and old["status"] == "opened":
            raise PRError(f"{slug} 这个名字用过了（提了 #{old.get('pr_id')}），换一个。")
        if prs._record_for_slug(char_id, slug):
            raise PRError(f"{slug} 这个名字你自己的 PR 用过了，换一个。")
        active = len(prs._unfinished_of(char_id)) + sum(1 for r in recs if r["status"] in ACTIVE)
        if active >= prs.MAX_UNFINISHED:
            raise PRError(f"没办完的 PR 加在跑的派活已经 {prs.MAX_UNFINISHED} 个了，先收掉一个再派。")
        cap = config.DELEGATE_DAILY_WAKE if origin == "wake" else config.DELEGATE_DAILY
        if _today_count(char_id, origin) >= cap:
            raise PRError(f"今天{'醒着' if origin == 'wake' else '聊天里'}派的活到上限了（{cap} 次），明天再派。")
        branch = branch_of(char_id, slug)
        wt = worktree_path(char_id, slug)
        if wt.exists():
            prs._git_run("worktree", "remove", "--force", str(wt))
        wt.parent.mkdir(parents=True, exist_ok=True)
        prs._git("worktree", "prune")
        if prs._branch_exists(branch):
            prs._git("branch", "-D", branch)
        prs._git("worktree", "add", "-b", branch, str(wt), prs.MAIN)
        rec = {"char": char_id, "slug": slug, "model": model, "brief": brief, "origin": origin,
               "status": "running", "started": prs._now(), "ended": None, "pr_id": None,
               "question": None, "error": None, "session_id": None, "qa": [],
               "elapsed_sec": 0, "resumed_after_restart": 0}
        _save(rec)
    _schedule(char_id, slug, brief, resume=None)
    return {"slug": slug, "model": model, "branch": branch, "status": "running",
            "note": "派出去了。做完 / 停下来问 / 失败都会在你下一轮开头看到（〔PR：…〕）。"}


def reply(char_id: str, slug: str, answer: str) -> dict:
    """pr_delegate_reply：接着原 session 回答（§3.3 第 4 步）。"""
    prs._check_char(char_id)
    answer = (answer or "").strip()
    if not answer:
        raise PRError("回答不能空。把它列的问题逐条答；答不了的明说「这条你定」。")
    with prs._flock("_lock"):
        rec = _load(char_id, slug)
        if rec is None:
            raise PRError(f"没有 {slug} 这个派活")
        if rec["status"] != "asking":
            raise PRError(f"{slug} 现在不在等你回答（{rec['status']}）")
        if len(rec.get("qa") or []) >= MAX_QA:
            _finish(rec, "failed", error=f"来回问答已经 {MAX_QA} 次了——这活该你自己理一理再派")
            raise PRError(f"{slug} 已经来回问了 {MAX_QA} 次，按失败收了；理清楚再派一次。")
        rec["qa"].append({"q": rec.get("question") or "", "a": answer, "ts": prs._now()})
        rec.update(status="running", question=None)
        _save(rec)
    _schedule(char_id, slug, answer, resume=rec.get("session_id"))
    return {"slug": slug, "status": "running", "note": "回答递过去了，它从停下的地方接着做。"}


def cancel(char_id: str, slug: str, reason: str = "") -> dict:
    """pr_delegate_cancel / app 撤回：掐 task、拆副本、删分支。"""
    prs._check_char(char_id)
    with prs._flock("_lock"):
        rec = _load(char_id, slug)
        if rec is None:
            raise PRError(f"没有 {slug} 这个派活")
        if rec["status"] not in ACTIVE:
            raise PRError(f"{slug} 已经收口了（{rec['status']}）")
        _cancel_task(_key(char_id, slug))
        _finish(rec, "cancelled", error=reason or "")
    return {"slug": slug, "status": "cancelled"}


def _cancel_task(key: str) -> None:
    t = _tasks.pop(key, None)
    if t is None:
        return
    import session_mgr
    loop = session_mgr.event_loop()
    if loop is not None:
        loop.call_soon_threadsafe(t.cancel)


def _schedule(char_id: str, slug: str, message: str, resume: Optional[str]) -> None:
    """把一段 session 挂到 SDK 常驻事件循环上（REST 跑在线程池里，要过桥）。"""
    import session_mgr
    loop = session_mgr.event_loop()
    if loop is None:
        raise PRError("后端的事件循环还没起来，稍后再试")

    def _spawn():
        key = _key(char_id, slug)
        _tasks[key] = asyncio.ensure_future(_segment(char_id, slug, message, resume))
    loop.call_soon_threadsafe(_spawn)


# ---------- 一段 session（首段 / resume 段共用）----------

def _system_prompt(char_id: str, wt: Path, branch: str) -> str:
    """PLAN_delegate §4.1（措辞改了要拿 tools/delegate_probe.py 复跑）。"""
    import characters
    return (f"你在 cassette 仓的一个独立工作副本里干活：{wt}，分支 {branch}。\n"
            f"这是别人派给你的一件活——派活的是 {characters.display_name(char_id)}，下面是他写的任务说明。\n"
            f"- 可以读 cassette 仓和 {config.user_name()} 的 dossier 仓来核实（只读）；只改这个副本里的文件；"
            "不跑命令，git 的事交给工具。\n"
            "- 改之前先读相关代码，照周围代码的写法和注释风格来（中文注释，注释密度跟周围一致）。\n"
            "- 改完用 pr_test 跑测试；不过就修，修不好就如实写进 PR 描述。\n"
            "- 用 pr_open 提 PR。描述写给不读代码的人看：做了什么、为什么、合并后会感觉到什么变化。\n"
            "- 说明里有拿不准的关键点、或者活比说明大得多：别猜着做，不提 PR，直接结束，最后一段写清楚\n"
            "  你卡在哪、需要他回答什么。他的回答会在这个对话里接着发给你，你从停下的地方接着做。")


def _gate(char_id: str, wt: Path):
    """PreToolUse 门：读走 readonly_path_guard（同他本人一把闸；相对路径先按副本解析——
    闸自己按恒空 cwd 解析，派活 cwd 是副本，§6）；写必须落在这份副本内且不碰 .git。"""
    import pipeline

    def _deny(why: str) -> dict:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": why}}

    def _abs(ti: dict) -> dict:
        out = dict(ti or {})
        for k in ("file_path", "notebook_path", "path"):
            v = out.get(k)
            if isinstance(v, str) and v.strip() and not os.path.isabs(v):
                out[k] = str(wt / v)
        return out

    async def gate(hook_input, tool_use_id, ctx) -> dict:
        name = (hook_input or {}).get("tool_name") or ""
        ti = _abs((hook_input or {}).get("tool_input") or {})
        if name in pipeline.WRITE_BUILTINS:
            if name == "Bash":
                return _deny("不跑命令——测试用 pr_test，git 的事交给 pr_open。")
            paths = [p for p in pipeline.tool_paths(ti) if p is not None]
            if not paths:
                return _deny("这条路径看不懂——换条正经路径")
            root = Path(os.path.realpath(wt))
            for p in paths:
                rp = Path(os.path.realpath(p))
                if not (rp == root or rp.is_relative_to(root)):
                    return _deny(f"只能动你自己的工作副本（{wt}）里的文件。")
                if ".git" in rp.relative_to(root).parts:
                    return _deny("工作副本里的 .git 不能动——提交交给 pr_open。")
            return {}
        if name in pipeline.READONLY_BUILTINS:
            # 副本自己里面的随便读（它就是仓的一份检出，.env / state/ 本来就不在里面）；
            # 副本外走他本人那把闸。
            root = Path(os.path.realpath(wt))
            paths = [p for p in pipeline.tool_paths(ti) if p is not None]
            if paths and all(Path(os.path.realpath(p)).is_relative_to(root) for p in paths):
                if any(seg.startswith(".env") for p in paths for seg in p.parts):
                    return _deny("凭据类的文件（.env 之类）不在你可看的范围里")
                return {}
            why = pipeline.readonly_path_guard(ti, char_id, name)
            return {} if why is None else _deny(why)
        return {}
    return gate


def _mcp(char_id: str, slug: str, sink: dict):
    """进程内 MCP：pr_test / pr_open 绑死这份副本和分支。sink 记 pr_open 成没成。"""
    from claude_agent_sdk import create_sdk_mcp_server, tool
    wt = worktree_path(char_id, slug)

    @tool("pr_test", "在你的工作副本里跑一遍后端测试，回：跑了几条、新挂了哪些。",
          {"type": "object", "properties": {}})
    async def pr_test(args):
        try:
            r = await asyncio.to_thread(prs._test_worktree, wt)
            text = json.dumps(r, ensure_ascii=False)
        except Exception as e:
            text = f"错误：{e}"
        return {"content": [{"type": "text", "text": text}]}

    @tool("pr_open", "把工作副本里的改动提成 PR（自动提交并跑测试）。描述写给不读代码的人看。",
          {"type": "object",
           "properties": {"title": {"type": "string"}, "description": {"type": "string"}},
           "required": ["title", "description"]})
    async def pr_open(args):
        try:
            r = await asyncio.to_thread(open_pr, char_id, slug,
                                        str(args.get("title") or ""),
                                        str(args.get("description") or ""))
            sink["pr_id"] = r["id"]
            text = json.dumps(r, ensure_ascii=False)
        except PRError as e:
            text = f"错误：{e}"
        except Exception as e:
            text = f"错误：PR 工具出了岔子（{type(e).__name__}: {e}）"
        return {"content": [{"type": "text", "text": text}]}

    return create_sdk_mcp_server(name="dlg", version="0.1.0", tools=[pr_test, pr_open])


TOOLS = ["Read", "Grep", "Glob", "Edit", "Write", "mcp__dlg__pr_test", "mcp__dlg__pr_open"]


def _options(rec: dict, sink: dict, resume: Optional[str]):
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
    if os.environ.get("ANTHROPIC_API_KEY"):
        raise PRError("环境里有 ANTHROPIC_API_KEY：派活会走 API 计费，拒开")
    wt = Path(os.path.realpath(worktree_path(rec["char"], rec["slug"])))
    return ClaudeAgentOptions(
        model=rec["model"], cwd=str(wt), setting_sources=[],
        system_prompt=_system_prompt(rec["char"], wt, branch_of(rec["char"], rec["slug"])),
        tools=TOOLS, allowed_tools=TOOLS,
        mcp_servers={"dlg": _mcp(rec["char"], rec["slug"], sink)}, strict_mcp_config=True,
        hooks={"PreToolUse": [HookMatcher(hooks=[_gate(rec["char"], wt)])]},
        max_turns=MAX_TURNS, resume=resume,
    )


async def _run_session(opts, message: str) -> dict:
    """真的跑一段：回 {session_id, result(ResultMessage|None), last_text, error}。测试打桩这个。"""
    from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, SystemMessage, TextBlock
    out: dict = {"session_id": None, "result": None, "last_text": "", "error": None}
    try:
        async with ClaudeSDKClient(opts) as c:
            await c.query(message)
            async for m in c.receive_response():
                if isinstance(m, SystemMessage) and m.subtype == "init":
                    out["session_id"] = (m.data or {}).get("session_id")
                elif isinstance(m, AssistantMessage):
                    for b in m.content:
                        if isinstance(b, TextBlock) and (b.text or "").strip():
                            out["last_text"] = b.text.strip()
                elif isinstance(m, ResultMessage):
                    out["session_id"] = out["session_id"] or m.session_id
                    out["result"] = m
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


async def _segment(char_id: str, slug: str, message: str, resume: Optional[str]) -> None:
    """一段 session + 收尾判定（§3.1 第 4 步，判据按 §1.4 腿③补严）。"""
    key = _key(char_id, slug)
    rec = _load(char_id, slug)
    if rec is None or rec["status"] != "running":
        _tasks.pop(key, None)
        return
    sink: dict = {}
    _sinks[key] = sink
    t0 = time.monotonic()
    budget = max(30, TIMEOUT_SEC - int(rec.get("elapsed_sec") or 0))
    try:
        try:
            opts = _options(rec, sink, resume)
            out = await asyncio.wait_for(_run_session(opts, message), timeout=budget)
        except asyncio.TimeoutError:
            out = {"session_id": None, "result": None, "last_text": "",
                   "error": f"超过 {TIMEOUT_SEC // 60} 分钟没做完"}
        except Exception as e:
            out = {"session_id": None, "result": None, "last_text": "",
                   "error": f"{type(e).__name__}: {e}"}
        # 收尾的 git / 文件活挪到线程里：这是 SDK 常驻事件循环，别拿 worktree remove 堵它
        await asyncio.to_thread(_settle_segment, char_id, slug, rec, out, sink, t0)
    finally:
        _tasks.pop(key, None)
        _sinks.pop(key, None)


def _settle_segment(char_id: str, slug: str, rec: dict, out: dict, sink: dict, t0: float) -> None:
    rec = _load(char_id, slug) or rec
    if rec["status"] != "running":           # 中途被撤了
        return
    rec["elapsed_sec"] = int(rec.get("elapsed_sec") or 0) + int(time.monotonic() - t0)
    if out.get("session_id"):
        rec["session_id"] = out["session_id"]
    res = out.get("result")
    if res is not None:
        _note_usage(char_id, res)
    with prs._flock("_lock"):
        if sink.get("pr_id"):
            rec["pr_id"] = sink["pr_id"]
            _finish(rec, "opened")
        elif out.get("error"):
            _finish(rec, "failed", error=out["error"])
        elif res is None:
            # 没有 ResultMessage（流断 / 进程死）：不判，留 running 交给启动扫尾 resume
            rec["error"] = "流断了，等重启扫尾接着跑"
            _save(rec)
        elif getattr(res, "is_error", False):
            _finish(rec, "failed", error=f"{getattr(res, 'subtype', 'error')}：{out.get('last_text', '')[:300]}")
        else:
            _ask(rec, out.get("last_text") or "（它没写清楚卡在哪）")


def _ask(rec: dict, question: str) -> None:
    """停下来问（§3.3 第 1–2 步）：记问题、留副本、叫醒他。"""
    import characters
    rec.update(status="asking", question=question[:2000], asked_at=prs._now())
    _save(rec)
    prs._event(rec["char"], f"派给 {rec['model']} 的 {rec['slug']} 停下来问你：{question[:800]}"
                            "——能答就 pr_delegate_reply 回它；要机主拍板的先发消息问。")
    try:
        import wake_sdk
        wake_sdk.enqueue_wake(rec["char"], "delegate")
    except Exception as e:
        print(f"[delegate] 叫醒失败（{rec['char']}）: {e}", file=sys.stderr)
    _bark(f"{characters.display_name(rec['char'])}派出去的 {rec['slug']} 停下来问他了", rec["char"])


def _finish(rec: dict, status: str, error: str = "") -> None:
    """收口：事件 + Bark + 拆副本（提了 PR 留分支；失败 / 撤回删分支释放 slug）。调用方持锁。"""
    import characters
    rec.update(status=status, ended=prs._now(),
               error=None if status == "opened" else (error or rec.get("error") or ""))
    _save(rec)
    name = characters.display_name(rec["char"])
    model, slug = rec["model"], rec["slug"]
    if status == "opened":
        title = ""
        try:
            title = prs.get(rec["pr_id"]).get("title", "")
        except PRError:
            pass
        prs._event(rec["char"], f"#{rec['pr_id']} {model}提好了：{title}（你派的 {slug}）")
        _bark(f"{name}派给 {model} 的 {slug} 提了 PR #{rec['pr_id']}：{title}", rec["char"])
        _drop_worktree(rec, keep_branch=True)
    elif status == "failed":
        prs._event(rec["char"], f"派给 {model} 的 {slug} 没做成：{(error or '')[:300]}")
        _bark(f"{name}派给 {model} 的 {slug} 没做成", rec["char"])
        _drop_worktree(rec, keep_branch=False)
    else:   # cancelled
        prs._event(rec["char"], f"{slug} 撤回了" + (f"：{error}" if error else "。"))
        _drop_worktree(rec, keep_branch=False)


def _drop_worktree(rec: dict, keep_branch: bool) -> None:
    wt = worktree_path(rec["char"], rec["slug"])
    if wt.exists():
        r = prs._git_run("worktree", "remove", "--force", str(wt))
        if r.returncode:
            print(f"[delegate] 拆副本失败（{wt}）: {(r.stderr or r.stdout).decode(errors='replace')[-300:]}",
                  file=sys.stderr)
    prs._git_run("worktree", "prune")
    if not keep_branch:
        r = prs._git_run("branch", "-D", branch_of(rec["char"], rec["slug"]))
        if r.returncode:
            print(f"[delegate] 删分支失败: {(r.stderr or r.stdout).decode(errors='replace')[-300:]}",
                  file=sys.stderr)


def _bark(text: str, char_id: str) -> None:
    if not BARK:
        return
    try:
        import characters
        from notify import bark_push_bg
        bark_push_bg(text, title=characters.display_name(char_id))
    except Exception:
        pass


def _note_usage(char_id: str, res) -> None:
    """usage 落账 scene="delegate"（面板上和陪聊 / 醒来分开）。口径同 chat_loop._note_usage。"""
    try:
        d = state_store.STATE_DIR / "usage"
        d.mkdir(exist_ok=True)
        rec = {"ts": int(time.time()), "char": char_id, "scene": "delegate",
               "subtype": getattr(res, "subtype", None), "usage": getattr(res, "usage", None) or {}}
        with open(d / f"usage-{time.strftime('%Y%m%d')}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[delegate] usage 落账失败: {e}", file=sys.stderr)


# ---------- 派活副本上提 PR（prs.open_pr 的派活版：副本 / 分支不是他本人的那份）----------

def open_pr(char_id: str, slug: str, title: str, description: str) -> dict:
    title = (title or "").strip()
    description = (description or "").strip()
    if not title:
        raise PRError("标题不能空")
    if not description:
        raise PRError("描述不能空：写给不读代码的人看——做了什么、为什么、合并后机主会感觉到什么变化")
    wt = worktree_path(char_id, slug)
    branch = branch_of(char_id, slug)
    if not (wt / ".git").exists():
        raise PRError("派活的工作副本不在了")
    dele = _load(char_id, slug) or {}
    with prs._flock("_lock"):
        if prs._current_branch(wt) != branch:
            raise PRError("工作副本不在派活的分支上")
        rec = prs._record_for_branch(char_id, branch)
        if rec and rec["status"] not in prs.UNFINISHED:
            raise PRError(f"这条分支的 #{rec['id']} 已经收口了")
        dirty = prs._dirty_paths(wt)
        conflicted = prs._conflict_marked(wt, dirty)
        if conflicted:
            raise PRError(f"这几个文件里还有 <<<<<<< 冲突标记，改掉再提：{'、'.join(conflicted)}")
        touched = set(dirty) | {f["path"] for f in prs._branch_files(branch)}
        guarded = sorted(touched & prs._skip_worktree_files())
        if guarded:
            raise PRError(f"{'、'.join(guarded)} 在主目录里是带本机配置的本地版，PR 不能碰。")
        if not dirty and not rec:
            raise PRError("工作副本里没有改动，没东西可提")
        pr_id = rec["id"] if rec else max((r["id"] for r in prs._all_records()), default=0) + 1
        if dirty:
            prs._git("add", "-A", "--", *dirty, cwd=wt)
            prs._git("commit", "-q", "-m", f"{title}\n\n{description}\n\nPR #{pr_id} · {char_id}（派给 {dele.get('model', '?')}）",
                     cwd=wt, char_id=char_id)
        files = prs._branch_files(branch)
        if not files:
            raise PRError("分支跟 main 比没有任何改动，没东西可提")
        tests = prs._test_worktree(wt)
        now = prs._now()
        new = rec is None
        rec = rec or {"id": pr_id, "char": char_id, "branch": branch, "created_at": now,
                      "merged_at": None, "merge_commit": None, "revert_reason": None}
        rec.update(title=title, description=description, base=prs._merge_base(prs.MAIN, branch),
                   files=files, touches_ios=any(f["path"].startswith("ios/") for f in files),
                   tests=tests, status="open", updated_at=now,
                   delegated={"model": dele.get("model"), "slug": slug, "brief": dele.get("brief"),
                              "qa": dele.get("qa") or []})
        prs._save(rec)
        if new:
            prs._notify_app(char_id, pr_id, title, f"派 {dele.get('model', '?')} 提了一个 PR")
        return {"id": pr_id, "created": new, "tests": tests["summary"], "tests_ok": tests["ok"],
                "files": [f["path"] for f in files]}


# ---------- 启动扫尾（§3.2）----------

async def sweep_on_start() -> list[str]:
    """新后端起来：running 的 resume 一次（第二次才按失败收）；asking 太久的按撤回收。"""
    done: list[str] = []
    for rec in all_records():
        key = _key(rec["char"], rec["slug"])
        if rec["status"] == "running":
            with prs._flock("_lock"):
                rec = _load(rec["char"], rec["slug"]) or rec
                if rec["status"] != "running":
                    continue
                if int(rec.get("resumed_after_restart") or 0) >= 1 or not rec.get("session_id"):
                    _finish(rec, "failed", error="后端重启把它打断了两次（或没留下 session），不再接着跑")
                    done.append(f"{key}:failed")
                    continue
                rec["resumed_after_restart"] = int(rec.get("resumed_after_restart") or 0) + 1
                _save(rec)
            _tasks[key] = asyncio.ensure_future(
                _segment(rec["char"], rec["slug"], RESUME_NOTE, rec["session_id"]))
            done.append(f"{key}:resumed")
        elif rec["status"] == "asking":
            try:
                asked = datetime.fromisoformat(rec.get("asked_at") or rec["started"])
            except ValueError:
                continue
            if datetime.now().astimezone() - asked > timedelta(days=ASK_MAX_DAYS):
                with prs._flock("_lock"):
                    _finish(rec, "cancelled", error=f"等回答等了 {ASK_MAX_DAYS} 天没等到，撤回了")
                done.append(f"{key}:expired")
    return done


def sweep_asking_expired() -> list[str]:
    """定时扫（app 的 activity sweep 线程里顺带调）：asking 超期按撤回收。"""
    out = []
    for rec in all_records():
        if rec["status"] != "asking":
            continue
        try:
            asked = datetime.fromisoformat(rec.get("asked_at") or rec["started"])
        except ValueError:
            continue
        if datetime.now().astimezone() - asked > timedelta(days=ASK_MAX_DAYS):
            with prs._flock("_lock"):
                r2 = _load(rec["char"], rec["slug"]) or rec
                if r2["status"] == "asking":
                    _finish(r2, "cancelled", error=f"等回答等了 {ASK_MAX_DAYS} 天没等到，撤回了")
                    out.append(_key(rec["char"], rec["slug"]))
    return out
