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
