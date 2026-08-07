"""cassette 后端配置：全部从环境变量读（见 .env.example），个人配置一律不进仓。"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# X-Auth 共享密钥：app 与后端各持一份。不配则后端拒绝所有请求（fail closed）。
AUTH_KEY = os.environ.get("CASSETTE_AUTH_KEY", "")

# claude CLI（需已安装并登录；凭据走 CLI 登录态，后端不碰 API key）
MODEL = os.environ.get("CLAUDE_MODEL", "opus")
CLAUDE_TIMEOUT_SEC = int(os.environ.get("CLAUDE_TIMEOUT_SEC", "300"))
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

# 后端自己的地址：给 MCP 插件（如 codemode 自切）回调用。插件是独立进程，够不着这里的
# uvicorn 命令行参数，只能靠这个约定。改端口跑记得同步改这里。
BACKEND_URL = os.environ.get("CASSETTE_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")

# 可选：Bark 推送（断连补投/主动消息时通知手机；不配则静默跳过）
BARK_URL = os.environ.get("BARK_URL", "").strip()
BARK_ICON = os.environ.get("BARK_ICON", "").strip()   # 通知图标（公网可访问的图片 URL），空=Bark 默认

# wake 调度器（主动性）：总开关 + tick 间隔（每 tick 只做本地预判，不叫模型）
PROACTIVE_ENABLED = os.environ.get("PROACTIVE_ENABLED", "1") != "0"
WAKE_TICK_SEC = int(os.environ.get("WAKE_TICK_SEC", "300"))

# 可选：长期记忆 Ombre-Brain（P0luz 的开源项目 https://github.com/P0luz/Ombre-Brain ，
# 自部署服务，只对接不 vendor）。没跑 Ombre / 中途挂了 → 自动退回纯聊天，永不因记忆层断掉。
OMBRE_ENABLED = os.environ.get("OMBRE_ENABLED", "1") != "0"
OMBRE_MCP_URL = os.environ.get("OMBRE_MCP_URL", "http://localhost:18001/mcp")
# Ombre 的 MCP 静态密钥（Ombre 配 mcp_auth_mode: token 时用；留空=对方免鉴权）
OMBRE_MCP_TOKEN = os.environ.get("OMBRE_MCP_TOKEN", "").strip()
# Ombre 的 REST 面（记忆页数据源）：默认从 MCP URL 推；Dashboard 密码换 cookie。
OMBRE_REST_URL = os.environ.get("OMBRE_REST_URL", "").strip() or OMBRE_MCP_URL.rsplit("/mcp", 1)[0]
OMBRE_DASHBOARD_PASSWORD = os.environ.get("OMBRE_DASHBOARD_PASSWORD", "").strip()
