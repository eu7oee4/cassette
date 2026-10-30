"""cassette 后端配置：全部从环境变量读（见 .env.example），个人配置一律不进仓。"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
# 读 .env 之前的环境快照：app 上按「重启后端」时新进程用它起（PLAN_prs §6.2）。
# load_dotenv 不覆盖已有变量——新进程要是继承这个进程的 env，改过的 .env 重启了也不生效。
BOOT_ENV = dict(os.environ)
load_dotenv(BASE_DIR / ".env")

# X-Auth 共享密钥：app 与后端各持一份。不配则后端拒绝所有请求（fail closed）。
AUTH_KEY = os.environ.get("CASSETTE_AUTH_KEY", "")

# claude CLI（需已安装并登录；凭据走 CLI 登录态，后端不碰 API key）
MODEL = os.environ.get("CLAUDE_MODEL", "opus")
# 默认 300→900（2026-08-10）：浏览器插件上架后一轮可能真的在网上逛——超时上限跟着
# 能力面长，纯聊 300 够、带浏览器就不够了。sse 那边这个值是空闲超时（每行之间），
# 非流式/wake 是总时长，取同一个够用的上限。
CLAUDE_TIMEOUT_SEC = int(os.environ.get("CLAUDE_TIMEOUT_SEC", "900"))
STREAM_PING_SEC = 25   # 流式心跳间隔：工具调用期没有正文可发，定期 ping 撑住连接

# 人设文件：--system-prompt-file 完整替换默认系统提示词（实时读取，改人设不用重启）。
# 没有 persona.md 就退回 example——克隆下来零配置也能跑。
PERSONA_PATH = BASE_DIR / os.environ.get("PERSONA_FILE", "persona.md")
if not PERSONA_PATH.exists():
    PERSONA_PATH = BASE_DIR / "persona.example.md"

# 时区（时间感知注入用）
APP_TZ = ZoneInfo(os.environ.get("APP_TZ", "Asia/Shanghai"))

# 名字：app 首次启动引导用户起好、存后端 settings，这里的 env 值只是兜底默认。
# 用函数不用常量——用户随时可在 app 里改名，每次拼 prompt 现读才能即时生效。
USER_NAME_DEFAULT = os.environ.get("USER_DISPLAY_NAME", "user")
AGENT_NAME_DEFAULT = os.environ.get("AGENT_NAME", "cassette")


def user_name() -> str:
    import state_store
    return (state_store.load_settings().get("user_name") or "").strip() or USER_NAME_DEFAULT


def agent_name() -> str:
    import state_store
    return (state_store.load_settings().get("agent_name") or "").strip() or AGENT_NAME_DEFAULT


def user_pronoun() -> str:
    """提到用户时的人称代词（她/他/TA，用户在设置里选）。模型默认会瞎猜性别，必须显式给。"""
    import state_store
    p = (state_store.load_settings().get("user_pronoun") or "").strip()
    return p if p in ("她", "他", "TA") else "TA"

# ---------- Code 模式（tmux 里一个常驻的交互式 claude）----------
# ⚠️ 这道门和本仓其它地方的安全姿态**不一样**：聊天/醒来只挂白名单 MCP 工具，内置的
# Bash/Write/Edit 永远不开；code 模式恰恰相反——那个会话手上有整台电脑。所以默认关，
# 要用得自己在 .env 里拧开，并读一遍 README 的 Code mode 一节。权限弹窗照旧保留
# （绝不 --dangerously-skip-permissions），在 app 的内联终端里按。
CODE_MODE_ENABLED = os.environ.get("CODE_MODE_ENABLED", "0") == "1"

# 会话默认工作目录 = 那个 claude 的地盘：Grep/Glob 不带 path 时搜这儿、相对路径从这儿算、
# 只有这个目录的 .claude/settings.local.json 权限白名单才生效。留空 = 用本仓根目录。
CODE_CWD = os.environ.get("CODE_CWD", "").strip() or str(BASE_DIR.parent)

# app 每次切入可以指定别的工作目录（多项目），但必须落在这些根目录之下——手机端能指定
# 任意目录起一个有 Bash 权限的会话是不能接受的。逗号分隔；留空 = 只认 CODE_CWD 及其子目录。
CODE_CWD_ALLOW = [p.strip() for p in os.environ.get("CODE_CWD_ALLOW", "").split(",") if p.strip()]

# tmux 会话名（和你自己手开的会话重名会被杀，改这个避开）
CODE_SESSION = os.environ.get("CODE_SESSION", "cassette-code").strip() or "cassette-code"

# code 会话的人设追加档：交互式 claude 只能 --append-system-prompt（不能像聊天那样
# --system-prompt-file 整个替换——替换掉它就没有工具用法说明了，活也就干不成）。
# 人设 + 这份守则一起追加在默认提示词后面。
# 用函数不用常量：import 时定死的话，先跑起后端、后写 code_addendum.md 得重启才认。
_CODE_ADDENDUM_FILE = os.environ.get("CODE_ADDENDUM_FILE", "code_addendum.md")


def code_addendum_path() -> Path:
    p = BASE_DIR / _CODE_ADDENDUM_FILE
    return p if p.exists() else BASE_DIR / "code_addendum.example.md"


# game 剧情会话的守则文件（通用守则；游戏专属纪律在 game-story 插件的 prompts/ 里）。
_GAME_ADDENDUM_FILE = os.environ.get("GAME_ADDENDUM_FILE", "game_addendum.md")


def game_addendum_path() -> Path:
    p = BASE_DIR / _GAME_ADDENDUM_FILE
    return p if p.exists() else BASE_DIR / "game_addendum.example.md"

# 后端自己的地址：给 MCP 插件（如 codemode 自切）回调用。插件是独立进程，够不着这里的
# uvicorn 命令行参数，只能靠这个约定。改端口跑记得同步改这里。
BACKEND_URL = os.environ.get("CASSETTE_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")

# ---------- Game 模式（MuMu 模拟器 + MaaFramework 任务引擎，game_bridge.py）----------
# 默认关：要 mac 上装着 MuMu、模拟器切到 720x1280@320、跑过 tools/fetch_maayuan.py。
# 开通步骤见 README 的 Game mode 一节。没开时 /game/* 一律 503，插件工具有声报错。
GAME_MODE_ENABLED = os.environ.get("GAME_MODE_ENABLED", "0") == "1"

# 剧情会话引擎（PLAN_sdk S1/PR7 → S2/PR13）：
#   sdk（默认）=agent-sdk 独立 loop；tmux=最老路回退；
#   unified=game 并入聊天意识流（设计稿三：泵不是会话）——只对 sdk 聊天灰度
#   角色生效，其余角色自动退 sdk 独立 loop。真机验收过再拨 unified 当默认。
# 新路跑稳两周后旧路和 game-story 插件的会话侧 MCP 一起退役。
STORY_ENGINE = (os.environ.get("STORY_ENGINE", "sdk").strip() or "sdk")

# 只读工具常驻（PLAN_sdk §5.3 PR14-b）：Read/Grep/Glob 挂进 sdk 聊天 session 的
# 所有轮次（§2 核实纪律：结论过桥的时候来源也过得去）。默认关——schema token
# 成本必须真机实测（PLAN_cache：上下文最大稀释源是工具 schema）再拨闸。
READONLY_TOOLS_ENABLED = os.environ.get("READONLY_TOOLS", "0") == "1"

# 写类工具挂载闸（PLAN_native §1）：Edit/Write/NotebookEdit/Bash 的 schema
# 常驻进 sdk 聊天 session，但不进 allowed_tools——每次调用被 CLI 判 ask，
# 路由到 can_use_tool 弹卡原地挂起（permits.py），机主批的就是那次调用本身。
# 拨开=写类 schema 挂载+审批通路生效。默认关，两段拨闸：先 READONLY_TOOLS
# 实测 schema token，稳了再拨这个（上电硬前置：iOS 权限卡先做出来，§6）。
WRITE_TOOLS_ENABLED = os.environ.get("WRITE_TOOLS", "0") == "1"

# 醒来改代码只提 PR（PLAN_prs）：拨开=PR 工具（prs_mcp）挂进 sdk 聊天 session、
# 本角色工作副本（.worktrees/<char>/）里的写类直接放行不弹卡。前提是 WRITE_TOOLS
# 也开着（在副本里干活用的就是 Edit/Write）。默认关。（10-29 起醒来轮也弹卡，
# 「关着时 wake 轮写类照样拒」那条退役——关着只是副本不放行、照样弹卡。）
WAKE_PRS_ENABLED = os.environ.get("WAKE_PRS", "0") == "1"

# 派活提 PR（PLAN_delegate §2 / §8 第 2 条，10-30）：他挑模型写说明、后端起一次性 session 去改。
# 白名单（fable 是别名 → claude-fable-5-1）；日上限按发起的轮分两档，先这么定、跑一周看实际再调。
DELEGATE_MODELS = os.environ.get("DELEGATE_MODELS", "haiku,sonnet,opus,claude-fable-5-1")
DELEGATE_DAILY = int(os.environ.get("DELEGATE_DAILY", "5"))
DELEGATE_DAILY_WAKE = int(os.environ.get("DELEGATE_DAILY_WAKE", "2"))
# 灰度（§7 第 5 步）：逗号分隔的角色 id，只这些角色能派；空 = 都能。先只给 cass 开一周。
DELEGATE_CHARS = {s.strip() for s in os.environ.get("DELEGATE_CHARS", "").split(",") if s.strip()}

# 工具延迟 + 常驻（PLAN_toolmount §2，10-30 第 8 步）：sdk 聊天 session 挂 ToolSearch，
# 工具用法（schema）按名字取、常驻名单（pipeline.ALWAYS_LOAD_SERVERS）照旧全量。
# 同一把闸也管「轮间隙 toggle」那个口子（§5.2 / §10）：game 平时 toggle off、插件在 app 里
# 开关走 toggle、关着的插件也挂上随即 toggle off。
#   "0" / 空（默认）= 关：一切照旧全量，回退一行；"1" = 全角色；"cass,default" = 只这些角色。
# 只管常驻 session 路（context="chat_session"）；-p 老路（wake.py / cohabit）口径照旧
# （pipeline.TOOL_SEARCH_CONTEXTS）。先给 cass 开几天看 ToolSearch 频率。
CHAT_TOOL_DEFER = os.environ.get("CHAT_TOOL_DEFER", "0").strip()


def chat_tool_defer(char_id) -> bool:
    """这个角色的常驻聊天 session 开不开工具延迟（含轮间隙 toggle 口子）。"""
    v = CHAT_TOOL_DEFER
    if v in ("", "0", "false", "off"):
        return False
    if v in ("1", "true", "on", "all"):
        return True
    return bool(char_id) and char_id in {s.strip() for s in v.split(",") if s.strip()}


# 轮内插话（PLAN_interject §4 / P2–P4）：TA 在他一轮没说完时发的话，直接写进在飞的聊天
# session（CLI 在下一个工具边界吸收；没有边界就自己另起一轮）。"0" / 空 = 关：一切照旧排队；
# "1" = 全角色；"cass" = 只这些角色（灰度先开一个，看一周重铸 / rescue 次数）。
# 同时要 app 新包在请求头声明支持（X-Interject），两边都要有，旧包一行不变。
INTERJECT = os.environ.get("INTERJECT", "0").strip()


def interject_on(char_id) -> bool:
    v = INTERJECT
    if v in ("", "0", "false", "off"):
        return False
    if v in ("1", "true", "on", "all"):
        return True
    return bool(char_id) and char_id in {s.strip() for s in v.split(",") if s.strip()}


# 问答卡（PLAN_chatui §3.5/U4）：AskUserQuestion 挂进 sdk 聊天 session（不进
# allowed_tools → 每问一卡，can_use_tool 挂起等机主作答，updated_input 回填）。
# 默认关，上电硬前置：iOS 问答卡先做出来——卡没人答只会白等一阵超时拒。
QUESTION_CARDS_ENABLED = os.environ.get("QUESTION_CARDS", "0") == "1"

# 聊天引擎（PLAN_sdk S2/PR10）：按角色灰度。
#   "p"（默认）=一次性 claude -p（今天的生产）；"sdk"=全角色走常驻 session；
#   "sdk:<id1>,<id2>"=只这些角色走 sdk，其余 p——先切提信感的那个角色，
#   他既是受益人也是验收人（§10 S2 PR10）。
CHAT_ENGINE = (os.environ.get("CHAT_ENGINE", "p").strip() or "p")


def chat_engine(char_id: str) -> str:
    """这个角色这轮聊天走哪个引擎（"p" / "sdk"）。char_id 必须是已解析的规范 id。"""
    if CHAT_ENGINE == "sdk":
        return "sdk"
    if CHAT_ENGINE.startswith("sdk:"):
        allow = {s.strip() for s in CHAT_ENGINE[4:].split(",") if s.strip()}
        return "sdk" if char_id in allow else "p"
    return "p"

# 可选：Bark 推送（断连补投/主动消息时通知手机；不配则静默跳过）
BARK_URL = os.environ.get("BARK_URL", "").strip()
BARK_ICON = os.environ.get("BARK_ICON", "").strip()   # 通知图标（公网可访问的图片 URL），空=Bark 默认

# wake 调度器（主动性）：总开关 + tick 间隔（每 tick 只做本地预判，不叫模型）
PROACTIVE_ENABLED = os.environ.get("PROACTIVE_ENABLED", "1") != "0"
WAKE_TICK_SEC = int(os.environ.get("WAKE_TICK_SEC", "300"))

# 同居世界（大房子，PLAN_cohabit）总开关：默认关——C2 把触发/队列全部接好但不上电，
# 事件流接到真角色上是 C4 的事（开 = 事件/独处醒来生效 + 聊天可附带 [[move:]]）。
COHABIT_ENABLED = os.environ.get("CASSETTE_COHABIT_ENABLED", "0") == "1"
# 连发上限 N（泛化定义见 PLAN）：无新外部输入时连续系统触发醒来的上限，入队处拦。
COHABIT_CHAIN_N = int(os.environ.get("CASSETTE_COHABIT_CHAIN_N", "4"))

# 可选：长期记忆 Ombre-Brain（P0luz 的开源项目 https://github.com/P0luz/Ombre-Brain ，
# 自部署服务，只对接不 vendor）。没跑 Ombre / 中途挂了 → 自动退回纯聊天，永不因记忆层断掉。
OMBRE_ENABLED = os.environ.get("OMBRE_ENABLED", "1") != "0"
OMBRE_MCP_URL = os.environ.get("OMBRE_MCP_URL", "http://localhost:18001/mcp")
# Ombre 的 MCP 静态密钥（Ombre 配 mcp_auth_mode: token 时用；留空=对方免鉴权）
OMBRE_MCP_TOKEN = os.environ.get("OMBRE_MCP_TOKEN", "").strip()
# Ombre 的 REST 面（记忆页数据源）：默认从 MCP URL 推；Dashboard 密码换 cookie。
OMBRE_REST_URL = os.environ.get("OMBRE_REST_URL", "").strip() or OMBRE_MCP_URL.rsplit("/mcp", 1)[0]
OMBRE_DASHBOARD_PASSWORD = os.environ.get("OMBRE_DASHBOARD_PASSWORD", "").strip()
