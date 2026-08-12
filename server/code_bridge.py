"""
Code 模式桥：管一个 tmux 会话里常驻的交互式 claude——起停、发话、抓画面、透传按键。

和聊天那条路的区别：聊天是每条消息起一个一次性 `claude -p`（无状态、只挂白名单 MCP
工具）；code 模式是一个**活着的**交互式会话，手上有整台电脑（Bash/Write/Edit）。
所以它默认关（config.CODE_MODE_ENABLED），权限弹窗一律保留——绝不 bypass。

纯 tmux 操作，不 import pipeline/app：上下文文本由调用方渲染好传进来，避免循环 import。

几处非搬不可的细节（都是实战踩出来的，改之前先想清楚）：
- 会话活着的判据用「pane 的 shell 还有没有子进程」（pgrep -P），不是 has-session：
  claude 崩了/退了会留一个空 shell 的 tmux 会话，只判 has-session 会把消息糊到 shell
  提示符上，还会让"会话占用中"永久挡住重新切入。
- 发消息走 tmux buffer + `paste-buffer -p`（bracketed paste）。**没有 -p，缓冲区里的
  \\n 就是回车**：消息带的时间戳前缀会被单独提交成一条，多行消息的后几行整个丢掉。
  末尾那个 send-keys Enter 才是唯一的提交动作。
- paste 之前先 C-u 清输入行：终端页盲按残留的半截字会拼进下一条消息开头。
- 起会话的命令里 `--append-system-prompt` 必须排在最后一个 flag 位。--mcp-config /
  --add-dir 都是 variadic（吃多个值），谁排最后就会把后面那个位置参数（context 展开
  的几万字）当成又一个值吞掉 → ENAMETOOLONG，会话根本起不来。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import config

BASE = Path(__file__).resolve().parent
CODE_DIR = BASE / "state" / "code_mode"
CONTEXT_PATH = CODE_DIR / "context.md"
SYSTEM_PATH = CODE_DIR / "system.md"          # 人设 + code 守则，每次起会话现拼
HOOK_SETTINGS_PATH = CODE_DIR / "hooks.settings.json"
HOOK_SCRIPT = BASE / "hooks" / "code_segments.py"
UPLOAD_DIR = CODE_DIR / "uploads"             # app 随消息发来的图片落这儿，给他 Read

# 会话画面宽高。宽度保持 80——手机上等宽字体一行放不下 80 列已经要横滑了，再宽只会更难读。
#
# ⚠️ 高度必须开大：交互式 claude 的 TUI 跑在 **alternate screen** 里，滚出可视区的内容
# tmux 一行都不往 scrollback 写（实测 history_size 恒为 0，`capture -S -2000` 抓到的
# 和当前屏一模一样）。所以"能往回看多少"完全等于 pane 有多高——40 行的时候手机上只能
# 看到最后一两轮，前面的全被顶掉了。
PANE_HEIGHT = 240
PANE_WIDTH = 80


def _find_tmux() -> str:
    """tmux 的路径。which 找不到就试几个常见位置（后端多半是 launchd/nohup 起的，
    PATH 比登录 shell 干净得多，homebrew 的路径常常不在里面）。"""
    found = shutil.which("tmux")
    if found:
        return found
    for p in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux",
              os.path.expanduser("~/.local/bin/tmux"), "/usr/bin/tmux"):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return "tmux"   # 交给 PATH，失败时 _tmux() 会返回带错的 CompletedProcess


TMUX = _find_tmux()
SESSION = config.CODE_SESSION

# ---------- 档案（profile）----------
# 一张桌子两种坐法：code=写代码（内置工具全开、cwd 在代码仓）；game=剧情会话（工具白名单
# 收窄到纯 game/记忆 MCP，cwd 是一块**故意留空的干净地板**——放在代码仓底下会继承那儿的
# CLAUDE.md，TA 一睁眼就串台）。同一时刻只有一个会话活着（start 先杀光所有档案）：
# 只有一个 TA，「一边写代码一边打游戏」在这儿是物理保证不是自觉。
#
# ⚠️ game 的 cwd 第一次用之前，机主要手动在那个目录里跑一次 claude 把「信任此文件夹」
# 按掉——那个确认框会吃掉启动命令行尾部的东西，会话起来了但 TA 不知道自己为什么在这儿。
GAME_CWD = os.path.expanduser(os.environ.get("GAME_STORY_CWD", "~/cassette-game"))

PROFILES = {
    "code": {"session": config.CODE_SESSION},
    "game": {"session": os.environ.get("GAME_SESSION", "").strip() or "cassette-game",
             "cwd": GAME_CWD},
}

# 当前活着的是哪个档案：**刻意落盘不放内存**——后端重启不该让「TA 在哪儿、玩了多久」归零
# （看守和 wake 避让都靠它）。
SESSION_STATE_PATH = CODE_DIR / "session.json"


def _read_session_state() -> dict:
    try:
        return json.loads(SESSION_STATE_PATH.read_text("utf-8"))
    except Exception:
        return {}


def _write_session_state(d: dict) -> None:
    _write_atomic(SESSION_STATE_PATH, json.dumps(d, ensure_ascii=False, indent=2))


def active_profile() -> str:
    p = _read_session_state().get("profile")
    return p if p in PROFILES else "code"


def _session() -> str:
    """当前档案的 tmux 会话名。所有会话操作（send/capture/keys/杀）都打到它头上——
    /code/* 那排路由因此对两个档案通用，终端页和弹窗按钮不用写第二份。"""
    return PROFILES[active_profile()]["session"]


def session_started_at() -> int:
    return int(_read_session_state().get("started_at") or 0)


class CodeModeOff(Exception):
    """对应模式没在 .env 里打开。路由层转成 503。"""


def require_enabled(profile: Optional[str] = None) -> None:
    p = profile or active_profile()
    if p == "game":
        if not config.GAME_MODE_ENABLED:
            raise CodeModeOff("game 模式没开：在 server/.env 里设 GAME_MODE_ENABLED=1 再重启后端")
    elif not config.CODE_MODE_ENABLED:
        raise CodeModeOff("Code 模式没开：在 server/.env 里设 CODE_MODE_ENABLED=1 再重启后端")


def _tmux(*args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([TMUX, *args], capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return subprocess.CompletedProcess(args=[TMUX, *args], returncode=127, stdout="", stderr=str(e))


def tmux_available() -> bool:
    return _tmux("-V", timeout=5).returncode == 0


# ---------- 工作目录 ----------
def resolve_cwd(cwd: Optional[str]) -> str:
    """定这次会话起在哪个目录。app 可以按次指定（多项目），但必须落在白名单根目录之下——
    手机端能指定任意目录起一个有 Bash 权限的会话是不能接受的。
    realpath 之后判前缀：`..` 和软链都会在这一步被摊平，绕不过去。"""
    default = os.path.realpath(os.path.expanduser(config.CODE_CWD))
    if not (cwd or "").strip():
        return default
    want = os.path.realpath(os.path.expanduser(cwd.strip()))
    roots = [os.path.realpath(os.path.expanduser(r)) for r in config.CODE_CWD_ALLOW] or [default]
    for root in roots:
        if want == root or want.startswith(root.rstrip("/") + "/"):
            return want
    raise PermissionError(
        "这个目录不在允许范围里（在 .env 的 CODE_CWD_ALLOW 里加它所在的根目录）")


# ---------- 会话状态 ----------
def session_alive() -> bool:
    """会话在 **且里面 claude 真的在跑**。
    只判 has-session 不够：claude 崩了/退了会留一个空 shell 的 tmux 会话，那样切入会被
    「会话占用中」永久挡住，send 还会把消息糊到 shell 提示符上。判据用"pane 的 shell 还
    有没有子进程"——比 pane_current_command 稳（claude 跑 Bash 工具时前台命令名会变，
    子进程一直在）。"""
    if _tmux("has-session", "-t", _session()).returncode != 0:
        return False
    r = _tmux("list-panes", "-t", _session(), "-F", "#{pane_pid}")
    pid = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not pid:
        return False
    try:
        return subprocess.run(["pgrep", "-P", pid[0]], capture_output=True,
                              text=True, timeout=5).returncode == 0
    except Exception:
        return True      # 判不出来就当活着，别误杀正在干活的会话


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(text, "utf-8")
    tmp.replace(path)


def _build_system(profile: str = "code") -> str:
    """人设 + 守则，拼成本次会话的 --append-system-prompt。
    顺序有意：守则在后——它要压得住人设里"说话简短"之类的调子（简短是说话风格，
    不是干活深度）。人设里的 {{AGENT_NAME}}/{{USER_NAME}} 占位符照聊天那边一起渲染。

    game 档案再往后接插件带的出厂纪律（story_discipline.md）：主仓 addendum 管通用守则，
    游戏专属的机制事实/坐标小抄跟着插件发版走，改纪律不用动主仓。

    多角色：人设取**会话归属角色**的（codemode/game-story 的 owner）——
    会话是独占资源，谁的资源谁的人设。"""
    import characters
    import plugins
    cid = plugins.owner_of("game-story" if profile == "game" else "codemode")
    files = [characters.persona_path(cid)]
    if profile == "game":
        files.append(config.game_addendum_path())
        files.append(BASE / "plugins" / "game-story" / "prompts" / "story_discipline.md")
    else:
        files.append(config.code_addendum_path())
    parts = []
    for p in files:
        try:
            parts.append(p.read_text("utf-8"))
        except Exception:
            pass
    text = "\n\n".join(parts)
    return (text.replace("{{AGENT_NAME}}", characters.display_name(cid))
                .replace("{{USER_NAME}}", config.user_name()))


def _hook_settings() -> Optional[Path]:
    """把逐段上报的 hook 写成一份**只给这个会话**的 settings（claude --settings）。
    这样不用去动用户全局的 ~/.claude/settings.json——那是别人的机器配置，我们没资格
    往里塞东西，塞了还会对他所有的 claude 会话生效。"""
    if not HOOK_SCRIPT.is_file():
        return None
    cmd = f'"{sys.executable}" "{HOOK_SCRIPT}"'
    entry = [{"matcher": "", "hooks": [{"type": "command", "command": cmd}]}]
    import json
    payload = json.dumps({"hooks": {"Stop": entry, "PostToolUse": entry}},
                         ensure_ascii=False, indent=2)
    _write_atomic(HOOK_SETTINGS_PATH, payload)
    return HOOK_SETTINGS_PATH


def _shq(s: str) -> str:
    """单引号包裹给 shell（send-keys 是把整行文本敲进 shell 的，路径带空格就散架）。"""
    return "'" + str(s).replace("'", "'\\''") + "'"


def _wait_mcp_ready(hint: str = "game_session_mcp", timeout: int = 30) -> bool:
    """等 MCP stdio 子进程在进程表里出现（= claude 起完、工具注册上了）。

    game 档案的第一条上下文必须等这个再 paste：MCP 是异步连的，走命令行位置参数会让
    第一轮请求里**一个工具都没有**——TA 只能把想调的工具按 JSON 写成文本吐出来然后
    end_turn，整个会话坐死（实机踩过一整晚）。code 档案有内置工具兜着，不用等。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = subprocess.run(["pgrep", "-f", hint], capture_output=True,
                               text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                time.sleep(1.0)   # 进程在了再给注册留一拍
                return True
        except Exception:
            pass
        time.sleep(0.8)
    return False


def start(context_text: str, auth_key: str, cwd: Optional[str] = None,
          mcp_configs: Optional[list] = None, profile: str = "code",
          tools: Optional[list] = None) -> dict:
    """杀旧起新 + 注入上下文（每次切入都是干净会话，无漂移、确定性）。

    退出模式时会话会被杀掉（app 那边调 /code/stop）——这样「会话活着 = 模式开着」是条
    干净的不变量，app 回前台就靠它对齐、也靠它发现 TA 自己切了进来。代价是正在跑的活会
    被停掉，所以 app 在退出前会探一下 is_busy()、正干着活就先问一句。

    profile 见 PROFILES。tools 给了就 --strict-mcp-config + 双写白名单（game 档案用：
    会话手上只有列出来的 MCP 工具，Bash/Write/Edit 物理上碰不到）。
    上下文注入分两条路：code 走命令行位置参数（一起送，简单）；game 起裸会话等
    _wait_mcp_ready 再 paste（原因见那儿）。"""
    require_enabled(profile)
    if not tmux_available():
        return {"ok": False, "error": "找不到 tmux（brew install tmux）"}
    prof = PROFILES[profile]
    if profile == "game":
        workdir = prof["cwd"]
        if not os.path.isdir(workdir):
            return {"ok": False, "error":
                    f"游戏会话的工作目录不存在：{workdir}——mkdir 之后先手动在里面跑一次 "
                    "claude 把「信任此文件夹」按掉（那个确认框会吃掉启动参数）"}
    else:
        try:
            workdir = resolve_cwd(cwd)
        except PermissionError as e:
            return {"ok": False, "error": str(e)}
        if not os.path.isdir(workdir):
            return {"ok": False, "error": f"工作目录不存在：{workdir}"}

    for p in PROFILES.values():
        _tmux("kill-session", "-t", p["session"])
    session = prof["session"]
    # context / system 写文件再由 shell 展开：几万字的历史用 send-keys 直塞必然撕裂。
    _write_atomic(CONTEXT_PATH, context_text)
    _write_atomic(SYSTEM_PATH, _build_system(profile))
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # 上报地址/密钥用 new-session -e 传（hook 由 claude 起，env 一路继承下来）。
    # ⚠️ 别改回 send-keys 敲 `export ...`：那等于把密钥打进一个交互式 shell，zsh 会把它
    # 原样写进 ~/.zsh_history。-e 是直接给会话环境，不经过 shell。
    r = _tmux("new-session", "-d", "-s", session, "-c", workdir,
              "-x", str(PANE_WIDTH), "-y", str(PANE_HEIGHT),
              "-e", f"CASSETTE_BACKEND_URL={config.BACKEND_URL}",
              "-e", f"CASSETTE_AUTH_KEY={auth_key}")
    if r.returncode != 0:
        return {"ok": False, "error": f"tmux new-session 失败：{r.stderr[:200]}"}

    time.sleep(0.5)   # 等 shell 起来，不然这条命令会敲在半个提示符上
    cmd = f"claude --model {config.MODEL}"
    for c in (mcp_configs or []):
        cmd += f" --mcp-config {_shq(c)}"
    settings = _hook_settings()
    if settings:
        cmd += f" --settings {_shq(settings)}"
    if profile == "code":
        # 图片落在 state/code_uploads/，工作目录多半不包含它 → 显式给访问权。
        cmd += f" --add-dir {_shq(UPLOAD_DIR)}"
    if tools:
        tl = ",".join(tools)
        cmd += f" --strict-mcp-config --tools {_shq(tl)} --allowedTools {_shq(tl)}"
    # ⚠️ 结尾必须是只吃一个值的 --append-system-prompt，位置参数（context）才落得对位。
    cmd += f' --append-system-prompt "$(cat {_shq(SYSTEM_PATH)})"'
    if profile == "code":
        cmd += f' "$(cat {_shq(CONTEXT_PATH)})"'
    _tmux("send-keys", "-t", session, cmd, "Enter")
    _write_session_state({"profile": profile, "session": session,
                          "started_at": int(time.time())})
    if profile == "game":
        ready = _wait_mcp_ready()
        r2 = send(context_text)
        if not r2.get("ok"):
            # 注入失败的会话是个「人在但不知道自己为什么在」的半成品，留着还会让下一次
            # game_start 被「会话占用中」挡死——直接杀掉，让 TA 干净地重试。
            _tmux("kill-session", "-t", session)
            return {"ok": False, "error": f"上下文注入失败：{r2.get('error', '')}"}
        if not ready:
            return {"ok": True, "session": session, "cwd": workdir,
                    "warn": "MCP 迟迟没就绪，上下文已注入但第一轮可能缺工具"}
    return {"ok": True, "session": session, "cwd": workdir}


# ---------- 确认弹窗 ----------
# 有确认框在等按键的时候 paste 文本会被它吃掉——消息开头是数字就等于替 TA 按了选项，
# 所以 send 必须先挡下来；同时把选项抠给 app 渲染成按钮。
#
# ⚠️ **判据只能是结构，不能是「画面里出现了某个字样」。** 实锤过一次：我在聊天里解释
# 这段代码，正文里写了 `Esc to cancel`、下面跟着「1. 2. 3.」的编号列表，整段回显进画面
# → 面板长出三颗假按钮、怎么点都不消（正文不会自己走），/code/send 的护栏又认定"有弹窗
# 在等"，把手机上发的每一条都挡了回去。人被锁在外面，只能杀掉整个会话才脱身。
#
# 真弹窗长这样（Claude Code v2.1 实抓，Write 和 Bash 两种一模一样）：
#
#     ────────────────────────────────────────────  ← 和输入框共用的那道横线
#      Bash command
#        curl -sI https://example.com | head -1
#      Do you want to proceed?
#      ❯ 1. Yes
#        2. Yes, and don't ask again for: curl -sI https://example.com
#        3. No
#     （空行）
#      Esc to cancel · Tab to amend · ctrl+e to explain   ← 页脚，画面**最后一行**
#
# 关键在于弹窗在场时**输入框和底部状态栏整个不画**——正文伪装不了这一点。于是三道闸：
#   ① 页脚落在画面最后几行里，且它下面没有输入框的横线（正文里的同样字样下面一定还有
#      输入框和状态栏，落不到最后）
#   ② 页脚是一排 `·` 隔开的短按键提示，其中一段形如 "Esc to xxx"（正文是长句，段长超标）
#   ③ 选项紧贴页脚往上连成一片，编号必须从 1 连续数上来（正文里的编号列表凑不齐这个）
#
# ⚠️ 还有一种**不画页脚**的变体（WebFetch 权限框实抓，08-10 真机漏过一次——弹窗没被
# 认出来，发进去的消息被它吃掉，结尾那个 Enter 正好按在 ❯ 1. Yes 上，等于替 TA 放行）：
#
#     ────────────────────────────────────────────
#      Fetch
#        url: "https://example.com", prompt: "…"
#        Claude wants to fetch content from example.com
#     （空行）
#      Do you want to allow Claude to fetch this content?
#      ❯ 1. Yes
#        2. Yes, and don't ask again for example.com
#        3. No, and tell Claude what to do differently (esc)   ← 画面**最后一行**
#
# esc 提示折进了 3 号选项自己的尾巴，整屏以选项块收尾。这个变体的闸：
#   ④ 选项块就是画面**最后**几行（正文里的编号列表下面一定还有输入框，落不到底；
#      输入框自己的内容也够不着底——它下面还有一道横线兜着）
#   ⑤ 块里必须有 TUI 画的 ❯ 选择光标（require_cursor；有页脚兜底时不苛求这个，
#      别把老路收紧）
#   ⑥ 编号照旧必须 1..N 连续
# 以后 CC 换了弹窗样式，照着上面的办法抓一份真画面再改，别凭印象加字样。

# 选项行：前面可能有个 ❯ 光标（单捕获出来，闸 ⑤ 要用），也容忍框线（旧版本画过框）。
# 形如 " ❯ 1. Yes"
_OPTION_RE = re.compile(r"^[\s│|]*([❯›>])?\s*(\d+)[.)]\s+(.+?)\s*[│|]?\s*$")
# 续行（选项文字太长被折到下一行）：没有编号、缩进较深、不是框线。
_CONT_RE = re.compile(r"^[\s│|]{3,}(\S.*?)\s*[│|]?\s*$")
# 页脚里的按键提示段，形如 "Esc to cancel" / "esc to exit"
_FOOTER_SEG_RE = re.compile(r"^(?:esc|escape)\s+to\s+\w+", re.I)
# 选项尾巴上的按键注解，按钮上不用显示
_KEYHINT_RE = re.compile(r"\s*\((?:esc|enter|tab|shift\+tab|ctrl\+\w+)\)\s*$", re.I)

_FOOTER_SEG_MAX = 40    # 按键提示都很短。超过这个长度的是正文，不是页脚
_FOOTER_LOOKBACK = 3    # 页脚在最后几行有字的里面找
# 下面两个上限**只是防跑飞的兜底，不是判据**——收不全的下场不是少个按钮，而是编号凑不齐
# 1..N（闸 ③/⑥）→ 整个弹窗当没看见 → 面板一颗按钮都不出。宁可放宽：真正挡正文的是闸
# ①②④⑤ 和 1..N，扫描到非选项非续行的行本来就会立刻停。
# 实机事故（08-10 14:39）：`Yes, and don't ask again for:` 后面跟的是一条带 3 个 header
# 和整坨 JSON 的 curl，2 号选项在 pane 里折了 4 行 —— 老的 _CONT_MAX=3 在这儿断掉，只收到
# 3 号，编号对不上 → dialog_options 返空。手机上弹窗认得出（页脚在，send 被挡）但按钮不出，
# 人被卡在「发不了消息、也点不了确认」的死角里。
_OPTION_SCAN_MAX = 80   # 选项块整块最多往上扫这么多行
_CONT_MAX = 12          # 一个选项最多折这么多行
_LABEL_MAX = 100        # 按钮上的文案上限：app 那排按钮是定高的，太长会把面板撑变形


def _screen(text: Optional[str] = None) -> list:
    """画面，末尾空行掐掉——所以最后一个元素就是屏幕上最后一行有字的。

    掐末尾是必须的：弹窗在场时状态栏不画，pane 底下会空出几行，不掐就找不到页脚。"""
    if text is None:
        r = _tmux("capture-pane", "-t", _session(), "-p")
        text = r.stdout if r.returncode == 0 else ""
    return (text or "").rstrip().splitlines()


def _is_rule(line: str) -> bool:
    """输入框上下那道横线。"""
    s = line.strip()
    return bool(s) and set(s) == {"─"}


def _footer_idx(lines: list) -> int:
    """页脚那一行的下标；不是弹窗返回 -1。见上面那段注释里的闸 ① ②。"""
    seen = 0
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if _is_rule(line):
            return -1           # 撞上输入框的横线 = 底下是输入框，不是弹窗
        if not line.strip():
            continue
        seen += 1
        if seen > _FOOTER_LOOKBACK:
            return -1
        segs = [s.strip() for s in line.split("·")]
        if any(len(s) > _FOOTER_SEG_MAX for s in segs):
            continue
        if any(_FOOTER_SEG_RE.match(s) for s in segs):
            return i
    return -1


def _parse_options(lines: list, footer: int, require_cursor: bool = False) -> list:
    """从 footer 往上收选项（无页脚的变体传 len(lines)，即从画面最底往上收）。

    **往上走**是要害：选项块永远紧贴页脚/画面底部，正文在更上面；从上往下扫就会先撞见
    正文里的编号列表，还会把它当成 1 号按钮——屏幕上写着一句正文，按下去执行的却是 Yes。

    require_cursor（闸 ⑤）：块里必须见到 ❯ 选择光标才算数——无页脚变体没有闸 ①②
    兜底，全靠它挡正文里恰好落到扫描区的编号列表。"""
    opts: list = []
    pending: list = []          # 折行的后半截，等上面那个编号行来认领
    saw_cursor = False
    i = footer - 1
    while i >= 0 and footer - i <= _OPTION_SCAN_MAX:
        line = lines[i]
        if not line.strip():
            if opts or pending:
                break           # 选项块和上面正文之间那道空行 = 到头了
            i -= 1
            continue            # 页脚和选项之间那道空行，跨过去
        m = _OPTION_RE.match(line)
        if m:
            saw_cursor = saw_cursor or bool(m.group(1))
            label = m.group(3).strip().rstrip("│|").strip()
            if pending:
                label = " ".join([label, *reversed(pending)]).strip()
                pending = []
            # 按键注解在整句拼完之后才去——它常常落在折行的后半截上
            label = _KEYHINT_RE.sub("", label).strip()
            if not label:
                break
            if len(label) > _LABEL_MAX:
                label = label[:_LABEL_MAX - 1].rstrip() + "…"
            opts.append({"key": m.group(2), "label": label})
            if m.group(2) == "1":
                break           # 数到 1 就是块顶，再往上是问题行和正文
            i -= 1
            continue
        c = _CONT_RE.match(line)
        if c and len(pending) < _CONT_MAX:
            pending.append(c.group(1).strip())
            i -= 1
            continue
        break
    opts.reverse()
    if require_cursor and not saw_cursor:
        return []
    # 闸 ③/⑥：编号必须正好是 1..N。对不上说明收到的是正文，不是选项块。
    if not opts or [o["key"] for o in opts] != [str(n) for n in range(1, len(opts) + 1)]:
        return []
    return opts[:9]


def dialog_pending() -> bool:
    """有没有确认框正等着按键。send 的护栏靠它——**两种形态都得认**：
    只认页脚的话，无页脚的 Fetch 弹窗会放 send 过去，粘贴的文字被弹窗吃掉、
    结尾的 Enter 直接按在 ❯ 1. Yes 上（08-10 实锤，弹窗被无声放行）。"""
    if not session_alive():
        return False
    lines = _screen()
    if _footer_idx(lines) >= 0:
        return True
    return bool(_parse_options(lines, len(lines), require_cursor=True))


def dialog_options(screen_text: Optional[str] = None) -> list:
    """把弹窗里的选项抠出来 → [{key, label}]，给 app 渲染按钮。

    以前 app 那排按钮是写死的「1 允许 / 2 总允许 / 3 拒绝」——描述不准，而且选项经常
    不止三个（选文件、选方案的面板能有四五个）。文案一律取自弹窗原文，有几个渲染几个。"""
    lines = _screen(screen_text)
    footer = _footer_idx(lines)
    if footer >= 0:
        return _parse_options(lines, footer)
    # 无页脚变体：从画面最底往上收，闸 ④⑤⑥ 把关（见模块顶部那段注释）。
    return _parse_options(lines, len(lines), require_cursor=True)


# ---------- 发消息 / 按键 / 抓画面 ----------
def _input_box() -> Optional[list]:
    """输入框那一格的内容（TUI 里最后两条横线之间那块）。认不出来就返回 None。"""
    r = _tmux("capture-pane", "-t", _session(), "-p")
    if r.returncode != 0:
        return None
    lines = [ln.rstrip() for ln in r.stdout.splitlines()]
    seps = [i for i, ln in enumerate(lines) if ln.strip() and set(ln.strip()) == {"─"}]
    if len(seps) < 2:
        return None
    return lines[seps[-2] + 1: seps[-1]]


def _clear_input(max_rounds: int = 12) -> None:
    """把输入框清干净再发消息。

    ⚠️ `C-u` 一次只删**一个视觉行**。空输入框按一下 ↑ 会把上一条消息整个调回来——注入的
    上下文有一万多字、三百多个视觉行，一次 C-u 只削掉最后一行，剩下的全变成下一条消息的
    前缀（实锤：76 字的消息被顶成 13470 字，前面 13394 字是上一轮的注入上下文，既烧 token
    又跟真正的指令抢注意力）。
    Esc 清不掉（实测无效）；C-c 能一下清干净，但它会打断正在跑的活，绝不能用在发消息前。
    所以只能一行行删——一次 send-keys 带 40 个 C-u，几轮就够，中间拿画面判断有没有清完。"""
    prev = None
    unknown = 0
    for _ in range(max_rounds):
        box = _input_box()
        if box is not None and not "".join(ln.lstrip("❯ ").strip() for ln in box):
            return                      # 干净了
        if box is None:
            # 认不出输入框（TUI 换了边框样式、画面正好在重绘）：清两轮就收手。
            # 认不出来时下面那个"画面没变就停"的判断永远为假，不设这个闸就会空转满
            # max_rounds 轮、白敲几百个 C-u，每发一条消息多等一秒。
            unknown += 1
            if unknown > 2:
                return
        cur = "\n".join(box) if box is not None else None
        if cur is not None and cur == prev:
            # 一轮 C-u 下去画面纹丝不动 = 没东西可删了，别死磕。
            # 最常见的就是这种：CC 会在空输入框里放一句自己生成的建议回复（ghost text），
            # 抓屏看着和真内容一模一样，但它不是真内容，C-u 删不动，也不会被带进消息里。
            return
        prev = cur
        _tmux("send-keys", "-t", _session(), *(["C-u"] * 40))
        time.sleep(0.05)


def send(text: str) -> dict:
    """发一条消息进会话。多行/含引号的文本走 tmux buffer 粘贴，send-keys 直塞会撕裂。"""
    require_enabled()
    if not session_alive():
        return {"ok": False, "error": "会话不在（先切一次 Code 模式）"}
    if dialog_pending():
        return {"ok": False, "dialog": True,
                "error": "TA 正停在一个确认弹窗上，这条会被弹窗吃掉——先在终端里按掉，再发"}
    _clear_input()
    # ⚠️ 必须 load-buffer（从文件读），不能 set-buffer（文本当命令参数）：tmux 命令走
    # server socket 有 ~16KB 上限，聊天短消息没事，game 档案注入的首条上下文（场景+
    # 百条历史）几万字，set-buffer 直接失败 → paste 报「no buffer」（08-12 实锤）。
    tmp = CODE_DIR / f".paste.{os.getpid()}.{uuid.uuid4().hex[:6]}.txt"
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, "utf-8")
        r0 = _tmux("load-buffer", "-b", "cassette-code", str(tmp))
    finally:
        tmp.unlink(missing_ok=True)
    if r0.returncode != 0:
        return {"ok": False, "error": f"load-buffer 失败：{r0.stderr[:200]}"}
    # -p = bracketed paste。没有它，缓冲区里的换行就是回车，整条消息会被切成好几条提交。
    r = _tmux("paste-buffer", "-b", "cassette-code", "-p", "-d", "-t", _session())
    if r.returncode != 0:
        return {"ok": False, "error": f"paste 失败：{r.stderr[:200]}"}
    time.sleep(0.2)
    # 入口查过一次还得再查：TA 正跑着活的话，弹窗可能恰好在 paste 前后冒出来——这时
    # 按下 Enter 就是替 TA 选中 ❯ 停着的那项（多半是 Yes，等于无声放行）。不按的话：
    # 弹窗冒在 paste 前，文字已被弹窗吃了，重发就行；冒在 paste 后，文字还留在输入框里，
    # 下次 send 的 _clear_input 会收拾掉。两种都比替 TA 按掉权限强。
    if dialog_pending():
        return {"ok": False, "dialog": True,
                "error": "刚要提交时弹窗冒了出来，这条没发出去——先按掉弹窗再发一次"}
    _tmux("send-keys", "-t", _session(), "Enter")   # 这才是唯一的提交动作
    return {"ok": True}


# 有名字的按键原样传给 tmux，其余当字面文本敲进去（-l）。
_NAMED_KEYS = {"Enter", "Escape", "Tab", "Up", "Down", "Left", "Right", "Space",
               "BSpace", "C-c", "C-d", "C-u", "C-r", "S-Tab", "PageUp", "PageDown"}


def send_keys(keys: str) -> dict:
    """终端面板的按键透传：弹窗选项的数字、回车、Esc、Ctrl-C 都走这儿。"""
    require_enabled()
    if not session_alive():
        return {"ok": False, "error": "会话不在"}
    if keys in _NAMED_KEYS:
        r = _tmux("send-keys", "-t", _session(), keys)
    else:
        r = _tmux("send-keys", "-t", _session(), "-l", keys)
    return {"ok": r.returncode == 0}


def capture(lines: int = PANE_HEIGHT) -> dict:
    """抓会话画面给终端面板显示。

    能往回看多少完全取决于 pane 有多高（见 PANE_HEIGHT 那段：alternate screen 里
    tmux 的 scrollback 永远是空的），所以这里直接抓整个 pane、按需要截尾部 N 行。

    **只抓一次**：画面和弹窗选项都从这一份里解析。以前分两次抓（还各带一次探活），
    面板 1.2 秒轮一回就是五个子进程，而且两次抓的还是两个不同时刻的画面。"""
    require_enabled()
    r = _tmux("capture-pane", "-t", _session(), "-p")
    if r.returncode != 0 or not session_alive():
        return {"ok": False, "alive": False, "content": "", "dialog": []}
    lines = max(20, min(int(lines or PANE_HEIGHT), PANE_HEIGHT))
    raw = r.stdout.splitlines()
    # pane 开得比内容高，上下都会剩一堆空行：尾部的留着会让最新一行沉在空白里，
    # 顶部的留着则是一大片黑。两头都掐掉，只留真正有字的那一段。
    while raw and not raw[0].strip():
        raw.pop(0)
    while raw and not raw[-1].strip():
        raw.pop()
    # 中间那些空行也要收：pane 开到 240 行，而 CC 的 TUI 把输入框画在**最底下**，
    # 正文和输入框之间就空出一长条（实测过一次是连续 29 行）——在手机上就是正文和
    # 输入框中间一大块纯黑，看着像画面裂了。连续 3 行以上压成 2 行；正常输出里
    # 连着三行空行本来就少见，压掉也只是少一点行距，不会丢字。
    squeezed: list[str] = []
    blanks = 0
    for line in raw:
        if line.strip():
            blanks = 0
            squeezed.append(line)
        else:
            blanks += 1
            if blanks <= 2:
                squeezed.append(line)
    raw = squeezed
    # 弹窗给的是**整屏**，别先截尾 N 行喂它：弹窗在场时状态栏不画、pane 底下空出几行，
    # 按行数截出来的那段可能整段是空白，页脚根本不在里面。
    return {"ok": True, "alive": True, "content": "\n".join(raw[-lines:]),
            "dialog": dialog_options(r.stdout)}


def is_busy() -> bool:
    """TA 此刻是不是正在跑一个活。

    判据是「画面还在动」：隔 0.8 秒抓两帧整屏，不一样就是在跑。
    ⚠️ 别只盯底部那几行——实测生成期间输入框和状态栏是**完全静止**的，动的是正文区
    （字一个个往外冒、工具调用一条条加）。停下来之后整屏定格，两帧一模一样。"""
    if not session_alive():
        return False

    def frame() -> str:
        return _tmux("capture-pane", "-t", _session(), "-p").stdout or ""

    first = frame()
    time.sleep(0.8)
    return frame() != first


def stop() -> dict:
    # 杀光所有档案的会话，不只当前的：session.json 万一和现实脱节（手动开过、状态文件
    # 写坏），照名单全杀才能保住「stop 之后必然没有会话」的不变量。
    for p in PROFILES.values():
        _tmux("kill-session", "-t", p["session"])
    return {"ok": True, "alive": session_alive()}


# ---------- 随消息带的图片 ----------
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
        "image/gif": "gif", "image/heic": "heic"}


UPLOAD_KEEP_DAYS = 30    # 收进来的图片/文件留这么久；会话里可能过一阵还要回头看，别删太狠
UPLOAD_KEEP_MAX = 200    # 再加个条数帽，防一天里传几百张把盘吃掉


def _prune_uploads() -> None:
    """清掉过期的上传件。每次存新文件时顺手做，不另起定时器。"""
    try:
        files = sorted((p for p in UPLOAD_DIR.iterdir() if p.is_file()),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    cutoff = time.time() - UPLOAD_KEEP_DAYS * 86400
    for i, p in enumerate(files):
        try:
            if i >= UPLOAD_KEEP_MAX or p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        except OSError:
            pass


def save_uploads(items: list) -> list:
    """app 发来的图片落到 state/code_mode/uploads/，返回绝对路径——消息里带上路径让 TA
    自己 Read（起会话时 --add-dir 过这个目录，够得着且不弹权限）。"""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _prune_uploads()
    stamp = time.strftime("%m%d-%H%M%S")
    paths = []
    for i, (data, media_type) in enumerate(items):
        ext = _EXT.get((media_type or "").lower(), "png")
        p = UPLOAD_DIR / f"upl-{stamp}-{uuid.uuid4().hex[:4]}-{i}.{ext}"
        p.write_bytes(data)
        paths.append(str(p))
    return paths


# 文件名里只留这些：其余一律换成下划线。名字是 app 那头传来的，直接拿去拼路径不行。
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._一-鿿-]")


def _safe_name(name: str) -> str:
    """把原始文件名收拾成一个安全的短名字（只取 basename，防 ../ 和绝对路径）。"""
    base = os.path.basename((name or "").strip()) or "file"
    base = _SAFE_NAME_RE.sub("_", base).lstrip(".") or "file"
    if len(base) <= 60:
        return base
    # 太长要截，但**扩展名得留着**——TA 是照着路径自己去读的，.py 还是 .log 一眼可辨。
    stem, dot, ext = base.rpartition(".")
    return f"{stem[:50]}.{ext}" if dot and len(ext) <= 10 else base[:60]


def save_files(items: list) -> list:
    """app 随消息发来的文件落到同一个 uploads 目录，返回绝对路径。

    和聊天那条路不一样：聊天把文件转成 document block 塞进 prompt，这边直接落盘给路径——
    那个会话手上有 Read/Grep，自己看比塞进上下文更自然，大文件也不占 token。
    保留原文件名（清洗过）方便 TA 认得出是什么，前面加时间戳防重名互相覆盖。"""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _prune_uploads()
    stamp = time.strftime("%m%d-%H%M%S")
    paths = []
    for i, (data, name) in enumerate(items):
        p = UPLOAD_DIR / f"{stamp}-{uuid.uuid4().hex[:4]}-{_safe_name(name)}"
        p.write_bytes(data)
        paths.append(str(p))
    return paths
