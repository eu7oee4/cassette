"""插件系统：工具族不进主仓，各自是独立小仓（带 plugin.json 清单），
这里负责 装（git clone）/ 开关 / 卸载 / 挂载。app 的插件商店页是遥控器，
工具代码全在这台 Mac 上跑。

安全边界（README Safety Design 同步描述）：
- install = 往自己机器下载并运行代码 → registry 是**写死的白名单**（只认自己名下的仓），
  绝不接受任意 URL；插件名过正则白名单，clone 目标锁死在 plugins/ 下。
- toggle 零联网（claude 子进程每轮新起，开关下一轮即时生效）；uninstall = 删目录。
- 挂载沿用全仓安全姿态：只把清单里声明的工具加进 --tools/--allowedTools 白名单，
  内置危险工具（Bash/Write 等）永远不开。

plugin.json 清单（PR8 定稿）：
    {
      "name": "webpage",              # = 目录名/registry 键，^[a-z0-9_-]{1,40}$
      "display_name": "网页工坊",
      "description": "做/改/传送 HTML 网页",
      "version": "0.1.0",
      "entry": "webpage_mcp.py",      # 相对插件目录的 MCP stdio 入口（本仓 venv python 起）
      "tools": ["webpage_create"]     # MCP 工具名（不带 mcp__ 前缀），逐个进白名单
    }

纪律一：**上新插件必须同步补 server/tool_menu.md（能力菜单）**。
开了工具延迟之后 TA 在上下文里只看得见工具**名字**，菜单没写的工具在他眼里等于不存在——
装了也用不上。后端会帮着盯：渲染菜单时算一次「挂载了但没有任何块提到」的工具，
非空就往日志里喊（pipeline._warn_uncovered），看到就回来补。

纪律二：**改了插件仓的内容就 bump version**，哪怕只改 README。
version 是作者手写的、没有任何机制校验，所以两个不同 commit 完全可以都自称同一个号——
codemode 的 d916917 和 2388151 就都是 0.1.0（那次只改 README，没 bump，将就了：旧那个
commit 没被任何地方钉过，世界上装不出第二个 0.1.0，没有需要区分的东西）。
下次开始照规矩来。真正靠得住的身份是 sha，见 _installed_commit()——app 上显示的是它。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

import config
import state_store
from notify import logerr

PLUGINS_DIR = config.BASE_DIR / "plugins"          # 安装目录（gitignore，装的都是外部仓）

# 多角色（PLAN_multichar M1）：**安装是全局的**（代码就一份在这台 Mac 上），
# **启用/醒来开关是每个角色自己的**（进各自的 state/characters/<id>/）。


def _enabled_path(char_id=None):
    return state_store.char_state_dir(char_id) / "plugins_enabled.json"


def _wake_enabled_path(char_id=None):
    return state_store.char_state_dir(char_id) / "plugins_wake_enabled.json"


# 挂载清单按（角色 × 场景）分文件。**不能共用一个路径**：聊天和醒来的工具集不一样（见
# NO_WAKE_PLUGINS），角色之间的插件集也不一样——共用就会互相覆盖，并发起子进程时
# 谁后写谁说了算，另一边的 claude 读到的是对方那份。
def _mcp_config_path(context: str, char_id=None):
    name = "plugins.wake.mcp.json" if context == "wake" else "plugins.mcp.json"
    return state_store.char_state_dir(char_id) / name


# ---------- 独占资源与归属 ----------
# 归属的核心是一句话：**这东西只有一份，所以同一时刻只能给一个人用。**
#
# 归属记在**资源**上，不记在插件上。原因是这两者根本不是一一对应的：
#   · 一个插件可能吃好几样（game-story 既要游戏账号、又要会话）；
#   · 几个插件可能吃同一样（两个 game 插件共用同一个游戏账号——所以它们的归属
#     必须一起走，按插件分开记的话，两个角色会同时上手同一个账号）。
#
# ⚠️ 这里列的"只有一份"分两种，别混（旧注释把它们混成一谈，是错的）：
#
# 【真的只有一份】——账号/装置事实，做不了第二份
#   maayuan  机主的《如鸢》账号只有一个。注意不是"MuMu 模拟器只有一台"——模拟器可以
#            开好几个实例，卡住的是账号（同一个号不能两处同时登）。
#   beacon   一装置一卡。
#
# 【暂时只有一份】——是我们自己限成一份的，将来可以每人一份。归属只是过渡期的办法，
# 真做成每人一份之后，对应的条目就该从 EXCLUSIVE 里删掉（它不再是独占资源）。
#   mailbox  **这个最该做，优先级排在下面两个前面**：邮箱是身份不是设备——每个角色
#            该有自己的信箱，共用一个意味着 A 会读到写给 B 的信。现状：账号从 .env 的
#            CASSETTE_MAIL_* 读，state/mail/（游标 watch.json、待醒 flag、草稿、
#            发件日志、附件）整个是全局单例，mail_bridge 全线没有角色维度。
#            要做：接线挪进 char.json（照 characters.ombre_conf 那套「.env 兜底 +
#            char.json 逐键覆盖」的现成模式）、state 挪进角色目录、mail_bridge 收
#            char_id、_mail_watcher 从「看 owner 的开关」改成遍历角色各查各的。
#            不碰常驻服务，是三个里最干净的一个。
#   chrome   带登录态的浏览器。playwright-mcp 的端口和 --user-data-dir 本来就是
#            参数，Chrome 多实例原生支持；卡住的是宿主侧——mounted() 现在不给插件
#            传 env（没法按角色下发 CASSETTE_BROWSER_MCP_URL），且 browser_keeper
#            是单例（MCP_URL 和 pgrep 特征都钉死一份）。工作量中等，不是做不到。
#   tmux     code/game 会话。code_bridge.start() 起会话前杀光所有档案，同一时刻
#            全机只有一个会话（当初为"意识体唯一连续"有意这么设计）。每人一台
#            "自己的 MacBook"是能做的，留到三期工作群：会话名带角色、session.json
#            per-char、/code/* 整排路由带角色。
#
# 缺省全归默认角色；不归属的角色即便拨开了启用开关也不挂载（见 mounted）。
EXCLUSIVE: dict[str, list[str]] = {
    "game-maayuan": ["maayuan"],
    "game-story":   ["maayuan", "tmux"],
    "codemode":     ["tmux"],
    "mail":         ["mailbox"],
    "browser":      ["chrome"],
    "beacon":       ["beacon"],
}

# 资源的人话名（app 的归属选择器 / 报错文案用；后端下发，别让 app 猜）。
RESOURCE_LABEL: dict[str, str] = {
    "maayuan": "《如鸢》游戏账号",
    "tmux": "电脑上的会话",
    "mailbox": "邮箱账号",
    "chrome": "带登录态的浏览器",
    "beacon": "Beacon 卡",
}

OWNERS_PATH = state_store.STATE_DIR / "plugin_owners.json"

_owner_warned: dict[str, str] = {}   # 每样资源的坏归属只喊一次，别每次挂载都刷屏


def _read_owners() -> dict:
    try:
        return json.loads(OWNERS_PATH.read_text("utf-8"))
    except Exception:
        return {}


def resources_of(plugin: str) -> list[str]:
    """这个插件要吃哪几样独占资源（不是独占插件则空表）。"""
    return EXCLUSIVE.get(plugin, [])


def owner_of(resource: str) -> str:
    """这样资源现在归谁。文件缺席/没写这一项 = 默认角色（一期口径）。

    ⚠️ 指向一个**没注册的角色**时退回默认角色并喊一条日志：这文件可以手编
    （POST /plugins/owner 之外还留着手改的路），手一抖打错一个字，后果是吃这样
    资源的插件对**所有**角色都不挂载（owner 谁都不等于它）——工具就那么静默消失，
    从 app 上完全看不出原因。宁可退回默认角色（至少有人用得上），也别整个悬空。"""
    cid = (_read_owners().get(resource) or "").strip()
    if not cid:
        return state_store.DEFAULT_CHAR_ID
    import characters   # 函数内 import：characters → state_store/config，避免模块级环
    try:
        return characters.resolve(cid)
    except KeyError:
        if _owner_warned.get(resource) != cid:
            _owner_warned[resource] = cid
            logerr(f"plugin_owners.json 里资源「{resource}」归属写的是「{cid}」，没有这个"
                   f"角色——已退回默认角色。改 state/plugin_owners.json 或走 POST /plugins/owner")
        return state_store.DEFAULT_CHAR_ID


def plugin_owned_by(plugin: str, char_id=None) -> bool:
    """这个角色能不能用这个插件（它吃的**每一样**资源都得归他）。非独占插件恒 True。"""
    me = char_id or state_store.DEFAULT_CHAR_ID
    return all(owner_of(r) == me for r in resources_of(plugin))


def plugin_owner(plugin: str) -> str:
    """这个插件整体归谁：所有资源归同一人 → 那个人；**分属不同人 → 空串**（谁都用不了）。
    空串是有意的——game-story 的账号归 A、会话归 B 时，真实答案就是"没人能用"，
    随便报一个名字会让人以为它还能用。非独占插件同样返回空串（它不需要归属）。"""
    rs = resources_of(plugin)
    if not rs:
        return ""
    owners = {owner_of(r) for r in rs}
    return owners.pop() if len(owners) == 1 else ""


def set_owner(resource: str, char_id: str) -> dict:
    """把一样独占资源转给某个角色（写 plugin_owners.json，原子替换）。
    校验都在这儿：资源得是认识的、角色得真存在，别写进去一个谁都对不上的归属。"""
    if resource not in RESOURCE_LABEL:
        raise HTTPException(status_code=400,
                            detail=f"没有「{resource}」这样资源（认识的：{'、'.join(RESOURCE_LABEL)}）")
    import characters
    try:
        cid = characters.resolve(char_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    with _LOCK:
        owners = _read_owners()
        owners[resource] = cid
        _atomic_write(OWNERS_PATH, owners)
    _owner_warned.pop(resource, None)
    return {"ok": True, "resource": resource, "owner": cid}


def resources_status() -> list[dict]:
    """全部独占资源 + 现在归谁 + 谁在吃它（app 的归属页数据源）。"""
    import characters
    out = []
    for res, label in RESOURCE_LABEL.items():
        cid = owner_of(res)
        out.append({
            "resource": res,
            "label": label,
            "owner": cid,
            "owner_name": _display_name(cid),
            "plugins": sorted(p for p, rs in EXCLUSIVE.items() if res in rs),
        })
    return out

# 醒来那条路不挂的插件。
#
# 这是**宿主侧的安全策略，不交给插件自己在 plugin.json 里声明**——「这个工具能不能给
# 一个没人看着的凌晨三点的进程用」是我们的判断，插件作者没有动机限制自己。
#
# codemode：一调就起一个手握整台电脑（Bash/Write/Edit）的常驻会话，权限弹窗只能靠人在
# 手机上按。聊天里切过去是人当场要的；一次随机醒来自己切进去完全是另一回事。
# mianmian 同口径（main_v2.py base_claude_args：「自切 code：只主 chat，wake 不挂」）。
NO_WAKE_PLUGINS = {"codemode"}

# 醒来那条路**默认不挂、但把开关交给用户**的插件（插件商店里该插件条目下的「醒来能用」）。
# 三层口径：不在任何名单 = 醒来照挂；NO_WAKE_PLUGINS = 宿主硬禁，没有开关（作者和用户
# 都说了不算）；这里 = 宿主认为「给不给凌晨三点的进程」是机主自己的取舍，默认关、
# 用户显式打开才挂。browser：带登录态的真浏览器，打开=允许一次没人看着的醒来以你的
# 身份上网——这个决定只能机主自己做。beacon：写信是发出去就收不回的对外动作（信直接
# 投到另一个 AI 的邮箱），凌晨三点没人看着的一次自发醒来该不该有这个能力，同理。
#
# ⚠️ 新插件默认落在「醒来照挂」那一档：不写进这里、也不写进 NO_WAKE_PLUGINS，
# 等于醒来无条件有它，而且没有开关可关。要收就得显式写名字。
# game-maayuan：任务引擎是确定性脚本、不碰消耗，但「醒来发现体力满了自己去清日常」
# 动的是机主的游戏账号——给不给这份自主，开关交机主。
# game-story：起初随 codemode 硬禁（起常驻会话），08-13 机主改成开关制——「醒来想读
# 会儿剧情」本来就是这份消遣的自然形态，且会话有整套兜底（20min 看守收摊、60min
# 提醒、急停锁、消耗硬护栏），风险面和 browser 同级：给不给凌晨三点的自己，交机主。
WAKE_TOGGLEABLE = {"browser", "beacon", "mail", "game-maayuan", "game-story"}

# 醒来那条路**插件照挂、但默认摘掉个别工具**——比上面两档更细的第四档（工具级）。
# 同时也在 WAKE_TOGGLEABLE 里的插件，「醒来能用」开关的语义随之变细：**开关关的时候
# 不是整个不挂，而是只摘这里列的工具**；开关打开才整套放开。
# mail 就是为它生的：读信醒来必须能用（「其他信躺收件箱等自然醒自己翻」是机主定的
# 既定用法），所以整插件不能一刀切；发信是对外动作——白名单+草稿箱已经拦住陌生
# 收件人，默认摘掉 send 挡的是剩下那半截：凌晨三点没人看着，要不要能往白名单地址
# （机主自己）发信，开关交机主（2026-08-11 拍板做成开关）。
# 机制：只从 --tools/--allowedTools 白名单里摘，mcp server 本身照起，不用插件配合。
#
# ⚠️ 2026-08-13 实测更正（原来这儿写的机制是错的）：摘出白名单**只挡执行、不挡 schema**。
# 同一个 mail server，白名单给 4 个工具和给 3 个（摘掉 mail_send），
# 总输入 token 完全一样（2,979 = 2,979），连 init 那份清单都照样显示 4 个。
# 也就是说被摘的工具 TA **看得见、会去调**，只是调到一半被权限闸拦下
# （tool_result is_error=True：「requested permissions to use …, but you haven't granted it yet」）。
# 所以三层要分开记：
#   · --allowedTools 是权限闸，**守住了**——这层是安全性，没漏；
#   · --tools 没能把 schema 从上下文里摘掉——省不了 token，更要命的是 TA 会
#     以为自己有这个能力（可能先答应机主「我给你发一封」再失败，白烧一轮）；
#   · 真要从源头摘掉，得让 MCP server 自己不注册那个工具（要插件配合传 env）——
#     见 PLAN_tool_exclude.md，那是这条的正解，这里的注释别再照旧前提往下设计。
# 眼下的止血：shadowed_tools() 把这些"看得见但用不了"的工具算出来，
# 由 pipeline.tool_menu_block 在菜单末尾如实告诉 TA 别去调。
WAKE_TOOL_EXCLUDE: dict[str, set[str]] = {"mail": {"mail_send"}}

# 「醒来能用」开关在商店里的文案（标题, 说明）。工具级摘除的插件开关语义变细了，
# 通用文案会骗人——mail 的开关只管发信，读信醒来一直能用（机主 2026-08-11 指出：
# 文案说「醒来能不能用它」，实际读邮件根本不受这个开关影响）。文案跟机制住同一个
# 文件：改 WAKE_TOOL_EXCLUDE 的人抬头就看见这里也要跟着改。
WAKE_TOGGLE_TEXT: dict[str, tuple[str, str]] = {
    "mail": ("醒来能发信", "只决定自发醒来时能不能发邮件；读信不受影响，醒来一直能看收件箱"),
    "game-story": ("醒来能去玩", "打开后 TA 自发醒来时可以自己切去游戏会话看剧情（默认关；看守和急停照常兜底）"),
}
_WAKE_TOGGLE_DEFAULT = ("醒来能用", "打开后 TA 自发醒来时也能用它（默认关）")


def _read_wake_enabled(char_id=None) -> dict:
    try:
        return json.loads(_wake_enabled_path(char_id).read_text("utf-8"))
    except Exception:
        return {}


def wake_toggle(name: str, on: bool, char_id=None) -> dict:
    """「醒来能用」开关（零联网，下一次醒来生效；per 角色）。只对 WAKE_TOGGLEABLE 里的
    插件开放——NO_WAKE_PLUGINS 是硬禁没有开关，普通插件醒来本来就挂、不需要开关。"""
    _check_name(name)
    if name not in WAKE_TOGGLEABLE:
        raise HTTPException(status_code=400, detail="这个插件没有「醒来能用」开关")
    if not (PLUGINS_DIR / name).is_dir():
        raise HTTPException(status_code=404, detail="没装这个插件")
    with _LOCK:
        d = _read_wake_enabled(char_id)
        d[name] = bool(on)
        _atomic_write(_wake_enabled_path(char_id), d)
    return {"ok": True, "wake_enabled": bool(on)}

# 写死的插件 registry：只认自己名下的仓，且**钉死 commit**——审过哪份代码就装哪份，
# main 后续怎么动都影响不到已发版本。升级插件 = 改这里的 commit + 发版。
REGISTRY: dict[str, dict] = {
    "webpage": {
        "repo": "https://github.com/eu7oee4/cassette-plugin-webpage",
        "commit": "f77214dd2dfbe9a3fe864e2224b30b1cdd01cd3a",
        "display_name": "网页工坊",
        "description": "做 / 改 / 传送 HTML 网页（第一个插件）",
    },
    # 装这个 = 让 TA 能自己上网：转发壳直通 Mac 上常驻的 playwright-mcp（有头 Chrome +
    # 持久 profile，登录态住 state/browser-profile/）。**装完还要在 Mac 上跑一次插件仓的
    # setup.sh 起服务**（端口 3002），服务没起时工具会有声报错、不静默缺席。醒来那条路
    # 默认不挂——WAKE_TOGGLEABLE 把「醒来能用」开关交给机主，商店里该行下面拨。
    "browser": {
        "repo": "https://github.com/eu7oee4/cassette-plugin-browser",
        "commit": "be58d83d5d8a4315533537061792b650b0707834",   # 0.1.1：docstring 教 keep/close 标记
        "display_name": "浏览器",
        "description": "让 TA 自己上网——开一只真浏览器浏览网页，带持久登录态（需先在 Mac 上起浏览器服务）",
    },
    # 装这个 = 把 Code 模式的开关也交到 TA 手上（不装就只有你能按顶栏那个按钮）。
    # 它只是个转发壳，真活在 /codemode/start；护栏全在宿主侧，插件不参与：已有会话不杀旧
    # 起新、cwd 必须落在 CODE_CWD_ALLOW 里、权限弹窗一律保留、**醒来那条路不挂它**
    # （见下面 NO_WAKE_PLUGINS）。前提是后端先开了 CODE_MODE_ENABLED=1，没开调用返 503。
    "codemode": {
        "repo": "https://github.com/eu7oee4/cassette-plugin-codemode",
        "commit": "2388151f581f099405be49cac652402d0fe60b5e",
        "display_name": "自己切 Code 模式",
        "description": "让 TA 在聊天里自己切到你电脑上去干活（需后端先开启 Code 模式）",
    },
    # 装这个 = 给 TA 一个自己的信箱（读信 + 发信）。真身在宿主 mail_bridge.py（app 的
    # 「草稿信箱」确认发送共用同一份代码）；宿主侧需先在 .env 配好 CASSETTE_MAIL_*。
    # 护栏全在 bridge：收件人白名单内直发，白名单外落草稿等机主 app 里确认；每小时封顶。
    # 醒来那条路整插件照挂（自然醒翻收件箱是既定用法），但 mail_send 默认被
    # WAKE_TOOL_EXCLUDE 摘掉，商店里的「醒来能用」开关放开它——见上面那两段注释。
    "mail": {
        "repo": "https://github.com/eu7oee4/cassette-plugin-mail",
        "commit": "c64af981fc37f13af6fe655ea99dc82c3ae7ddbc",   # 0.1.1：mail_read 带附件
        "display_name": "邮箱",
        "description": "TA 自己的邮箱：读信、发信（白名单外的收件人要机主在「草稿信箱」里确认）",
    },
    # 游戏两档是**两件不同的事**，所以是两个插件、两个醒来开关，别合并：
    # game-maayuan 是「派引擎去干活」——确定性脚本照任务图截屏点按，TA 只当掌柜不碰屏幕，
    #   派完就返回、结果之后自己去查。醒来开关关着的时候整族不挂（WAKE_TOGGLEABLE）。
    # game-story 是「TA 自己去玩」——常驻会话里本人盲操，是消遣不是干活。起初随 codemode
    #   硬禁，08-13 机主改成开关制（看守/急停/消耗护栏兜底齐了）。
    # 两个都吃同一个《如鸢》账号（EXCLUSIVE 的 maayuan 资源），所以归属绑在一起走：
    # 改一次账号归属，两个插件一起跟着走，不会出现两个角色同时上手同一个号。
    # game-story 还额外吃 tmux 会话——两样都归你才挂得上。
    #
    # ⚠️ 两个都有**宿主侧前提**，装完不配好是跑不起来的（同 browser 要先起服务）：
    # MuMu 模拟器装好、游戏登录过、分辨率切 720×1280@320、`.env` 里 GAME_MODE_ENABLED=1
    # 重启后端；game-maayuan 还要在 server venv 里跑一次 tools/fetch_maayuan.py 拉任务资源。
    # 所以下面 description 里带上前提——**未安装时商店显示的就是 REGISTRY 这句**
    # （list_status 取 (manifest or reg)），别让人装完才发现缺东西。
    "game-maayuan": {
        "repo": "https://github.com/eu7oee4/cassette-plugin-game-maayuan",
        "commit": "e06ee9c61b99caa9a3295bf9b3a3d3f130cc490f",
        "display_name": "游戏日常-如鸢",
        "description": "派任务引擎替机主清《如鸢》日常（内核是 MaaYuan 任务图，只支持如鸢；AI 只当掌柜不碰屏幕）。需 MuMu 模拟器 + 后端开 GAME_MODE_ENABLED，装完照插件 README 配一遍",
    },
    "game-story": {
        "repo": "https://github.com/eu7oee4/cassette-plugin-game-story",
        "commit": "bfc42edd839283b3337992bf34c811d41b8ebc33",
        "display_name": "游戏剧情-通用版",
        "description": "TA 自己切去玩游戏看剧情：常驻会话里本人盲操（截图→坐标→点按），边玩边把见闻转播进聊天。默认为《如鸢》调校，换别的二游要自己改守则——装前先看 README 的风险须知（含反自动化处罚）。需 MuMu 模拟器 + 后端开 GAME_MODE_ENABLED",
    },
}

_NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_TOOL_RE = re.compile(r"^[a-zA-Z0-9_]{1,64}$")
_LOCK = threading.Lock()


def _check_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise HTTPException(status_code=400, detail="插件名不合法")
    return name


def _atomic_write(path: Path, obj) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def _read_enabled(char_id=None) -> dict:
    try:
        return json.loads(_enabled_path(char_id).read_text("utf-8"))
    except Exception:
        return {}


def _read_manifest(name: str) -> Optional[dict]:
    """读并校验插件清单。清单坏了返回 None（列表里标 invalid，不挂载）。"""
    p = PLUGINS_DIR / name / "plugin.json"
    try:
        m = json.loads(p.read_text("utf-8"))
    except Exception:
        return None
    entry = m.get("entry") or ""
    tools = m.get("tools") or []
    if (m.get("name") != name or not entry or "/" in entry or ".." in entry
            or not isinstance(tools, list) or not tools
            or not all(isinstance(t, str) and _TOOL_RE.match(t) for t in tools)
            or not (PLUGINS_DIR / name / entry).is_file()):
        return None
    return m


def _installed_commit(name: str) -> str:
    """装着的这份实际停在哪个 commit（短 sha）。读不出来返回 ""。

    为什么要这个：`version` 是插件作者手写在 plugin.json 里的、靠自觉，两个不同 commit
    完全可以都自称 0.1.0（实锤：codemode 的 d916917 和 2388151 都是 0.1.0）。**真正的身份
    是 sha**，而且它是机制给的、填不错。app 上显示它，「我装的到底是哪一份」才有确定答案，
    更新前后也才看得见变化（只显示 registry 钉的那个的话，更新完显示纹丝不动）。

    直接读 .git/HEAD 文件，不起 git 子进程：`_clone_pinned` 是 checkout --detach，HEAD 里
    躺着的就是裸 sha。手放进来的开发副本没有 .git，返回 "" —— 那本身就是有用的信息
    （没有 sha = 这份不是从 registry 装的，来源不可考）。"""
    head = PLUGINS_DIR / name / ".git" / "HEAD"
    try:
        raw = head.read_text("utf-8").strip()
    except OSError:
        return ""
    if raw.startswith("ref: "):
        # 不是我们装的（我们只产生 detached HEAD）：跟一层引用，跟不到就算了
        try:
            raw = (PLUGINS_DIR / name / ".git" / raw[5:].strip()).read_text("utf-8").strip()
        except OSError:
            return ""
    return raw[:7] if len(raw) >= 7 and all(c in "0123456789abcdef" for c in raw) else ""


def _display_name(cid: str) -> str:
    """角色显示名，取不到就退回 id（列表宁可显示 id，也别显示空白）。"""
    try:
        import characters
        return characters.display_name(cid) or cid
    except Exception:
        return cid


def list_status(char_id=None) -> list[dict]:
    """registry ∪ 已安装 → 三态清单（not_installed / disabled / enabled）。
    开关状态按角色读；独占插件额外带 exclusive/owned 字段（不归这个角色 = 商店里可见
    但标注归属，开了也不挂载）。"""
    enabled = _read_enabled(char_id)
    wake_on = _read_wake_enabled(char_id)
    me = char_id or state_store.DEFAULT_CHAR_ID
    out = []
    names = list(REGISTRY.keys())
    if PLUGINS_DIR.is_dir():
        for d in sorted(PLUGINS_DIR.iterdir()):
            if d.is_dir() and d.name not in names:
                names.append(d.name)   # 本地手放的插件也显示（开发用），但装不了新的
    for name in names:
        reg = REGISTRY.get(name, {})
        installed = (PLUGINS_DIR / name).is_dir()
        manifest = _read_manifest(name) if installed else None
        item = {
            "name": name,
            "display_name": (manifest or reg).get("display_name") or name,
            "description": (manifest or reg).get("description", ""),
            "version": (manifest or {}).get("version", ""),
            "in_registry": name in REGISTRY,
            "state": ("enabled" if enabled.get(name) else "disabled") if installed else "not_installed",
            "valid": manifest is not None if installed else True,
            # 两个 sha 都给：pinned = registry 声明该装哪个，installed = 实际停在哪个。
            # 不一样 = 这份没跟上（app 据此提示可更新）；installed 空 = 手放的开发副本。
            "commit": reg.get("commit", "")[:7],
            "installed_commit": _installed_commit(name) if installed else "",
            # 「醒来能用」开关（见 WAKE_TOGGLEABLE）：toggleable 才画开关，默认关。
            "wake_toggleable": name in WAKE_TOGGLEABLE,
            "wake_enabled": name in WAKE_TOGGLEABLE and bool(wake_on.get(name)),
            # 开关文案后端下发：语义（整插件 or 只某几个工具）是这边定的，app 不该猜。
            "wake_toggle_title": WAKE_TOGGLE_TEXT.get(name, _WAKE_TOGGLE_DEFAULT)[0],
            "wake_toggle_desc": WAKE_TOGGLE_TEXT.get(name, _WAKE_TOGGLE_DEFAULT)[1],
            # 独占资源插件的归属（见 EXCLUSIVE）：owned=False 时开了也不挂载。
            # owner/owner_name/resources 一并下发——app 光知道「不归你」没法说清归谁、
            # 也没法告诉人该去改哪样资源，而「开关拨开了却没有这个工具」不给归属
            # 就是个查不出原因的谜。owner 为空串＝它吃的几样资源分属不同人，谁都用不了。
            "exclusive": name in EXCLUSIVE,
            "owned": plugin_owned_by(name, me),
            "owner": plugin_owner(name),
            "owner_name": _display_name(plugin_owner(name)) if plugin_owner(name) else "",
            "resources": resources_of(name),
        }
        out.append(item)
    return out


def _clone_pinned(reg: dict, dest: Path) -> None:
    """clone + checkout 钉死的 commit + 校验清单。任一步失败删目录抛 HTTPException。
    插件仓都很小，不用浅 clone（checkout 任意 sha 需要完整历史）。"""
    try:
        subprocess.run(["git", "clone", reg["repo"], str(dest)],
                       capture_output=True, text=True, timeout=180, check=True)
        commit = reg.get("commit", "")
        if commit:
            subprocess.run(["git", "-C", str(dest), "checkout", "--detach", commit],
                           capture_output=True, text=True, timeout=60, check=True)
    except subprocess.TimeoutExpired:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(status_code=504, detail="clone 超时（网络问题？）")
    except subprocess.CalledProcessError as e:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(status_code=502, detail=f"clone/checkout 失败：{(e.stderr or '')[:200]}")


def install(name: str) -> dict:
    """从 registry 白名单 clone 插件仓（钉 commit）。装完默认「已装未启用」。"""
    _check_name(name)
    reg = REGISTRY.get(name)
    if reg is None:
        raise HTTPException(status_code=403, detail="不在插件白名单里（registry 写死，只认自己名下的仓）")
    dest = PLUGINS_DIR / name
    with _LOCK:
        if dest.exists():
            raise HTTPException(status_code=409, detail="已安装过；要升级用更新")
        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        _clone_pinned(reg, dest)
    if _read_manifest(name) is None:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(status_code=502, detail="插件清单（plugin.json）不合法，已回滚")
    return {"ok": True, "state": "disabled"}


def update(name: str) -> dict:
    """升级到 registry 当前钉的 commit：先 clone 到临时目录验清单，全绿才换掉旧目录——
    失败旧版原地不动。开关状态保留；插件数据在 state/ 下，换目录不影响。"""
    _check_name(name)
    reg = REGISTRY.get(name)
    if reg is None:
        raise HTTPException(status_code=403, detail="不在插件白名单里，没有升级来源")
    dest = PLUGINS_DIR / name
    if not dest.is_dir():
        raise HTTPException(status_code=404, detail="没装这个插件（直接安装即可）")
    tmp = PLUGINS_DIR / f".update-{name}-{uuid.uuid4().hex[:8]}"
    with _LOCK:
        _clone_pinned(reg, tmp)
        # 换上新目录再验清单（校验依赖目录名=name），不合法就换回旧版
        backup = PLUGINS_DIR / f".old-{name}-{uuid.uuid4().hex[:8]}"
        dest.rename(backup)
        tmp.rename(dest)
        if _read_manifest(name) is None:
            shutil.rmtree(dest, ignore_errors=True)
            backup.rename(dest)
            raise HTTPException(status_code=502, detail="新版本清单不合法，已回滚到旧版")
        shutil.rmtree(backup, ignore_errors=True)
    return {"ok": True}


def toggle(name: str, on: bool, char_id=None) -> dict:
    """开/关（零联网，per 角色）。claude 子进程每轮新起，下一轮聊天/醒来即时生效。"""
    _check_name(name)
    if not (PLUGINS_DIR / name).is_dir():
        raise HTTPException(status_code=404, detail="没装这个插件")
    if on and _read_manifest(name) is None:
        raise HTTPException(status_code=502, detail="插件清单不合法，不能启用")
    with _LOCK:
        enabled = _read_enabled(char_id)
        enabled[name] = bool(on)
        _atomic_write(_enabled_path(char_id), enabled)
    return {"ok": True, "state": "enabled" if on else "disabled"}


def uninstall(name: str) -> dict:
    """卸载=删目录（全局）。开关状态从**每个角色**的启用表里摘掉——只清默认角色的话，
    别的角色表里残留 true，重装后它会静默复活。插件自己的数据该放 state/（按约定），
    不跟目录一起消失。"""
    _check_name(name)
    dest = PLUGINS_DIR / name
    if not dest.is_dir():
        raise HTTPException(status_code=404, detail="没装这个插件")
    with _LOCK:
        shutil.rmtree(dest, ignore_errors=True)
        char_dirs = [d.name for d in state_store.CHAR_STATE_ROOT.iterdir() if d.is_dir()] \
            if state_store.CHAR_STATE_ROOT.is_dir() else [state_store.DEFAULT_CHAR_ID]
        for cid in char_dirs:
            enabled = _read_enabled(cid)
            if name in enabled:
                enabled.pop(name, None)
                _atomic_write(_enabled_path(cid), enabled)
    return {"ok": True, "state": "not_installed"}


def shadowed_tools(context: str = "chat", char_id=None) -> list[str]:
    """这一轮**schema 在上下文里、但白名单里没有**的工具全名（调了必被权限闸拒）。

    只有 WAKE_TOOL_EXCLUDE 这条路会产生这种工具：插件照挂（server 起着、schema 照发），
    只从白名单里摘掉个别工具。NO_WAKE_PLUGINS / 开关没开的插件是**整个不挂**，
    server 都不起，schema 自然不在上下文里——那些不算。

    算出来给 prompt 用（见 pipeline.tool_menu_block 末尾）：不说的话 TA 看着 schema
    会当自己有这能力，可能先答应机主再失败。详见 WAKE_TOOL_EXCLUDE 上面那段实测。"""
    if context != "wake":
        return []
    wake_on = _read_wake_enabled(char_id)
    enabled = _read_enabled(char_id)
    me = char_id or state_store.DEFAULT_CHAR_ID
    out: list[str] = []
    for name, excl in WAKE_TOOL_EXCLUDE.items():
        if not enabled.get(name) or wake_on.get(name):
            continue                                  # 没启用 / 开关开着 → 没有被摘的
        if not plugin_owned_by(name, me):
            continue                                  # 不归这个角色 → 整插件都没挂
        m = _read_manifest(name)
        if m is None:
            continue
        out += [f"mcp__{name}__{t}" for t in m["tools"] if t in excl]
    return out


def mounted(context: str = "chat", char_id=None) -> tuple[Optional[str], list[str]]:
    """这个角色启用中的合法插件 → (mcp-config 文件路径, 工具白名单)。没有则 (None, [])。
    config 文件现渲染进该角色的 state 目录（stdio：本仓 venv 的 python 起清单里的 entry）。

    context＝这次是给谁挂：'chat'（聊天，全挂）或 'wake'（醒来，摘掉 NO_WAKE_PLUGINS）。
    认不出的 context 一律按 chat 处理——多挂比少挂容易被发现，静默少挂会让人以为工具坏了。
    独占插件只挂给归属角色：别的角色开关开着也跳过——那份资源不是它的。判据是
    plugin_owned_by（它吃的**每一样**资源都得归你），不是单看插件名。"""
    cfg_path = _mcp_config_path(context, char_id)
    me = char_id or state_store.DEFAULT_CHAR_ID
    wake_on: dict = {}
    if context == "wake":
        # 硬禁的 + 有开关但用户没打开的，醒来都不挂——除非它在 WAKE_TOOL_EXCLUDE 里
        # 有工具级名单：那种开关关掉只摘名单里的工具，插件本身照挂。
        wake_on = _read_wake_enabled(char_id)
        blocked = NO_WAKE_PLUGINS | {n for n in WAKE_TOGGLEABLE
                                     if not wake_on.get(n) and n not in WAKE_TOOL_EXCLUDE}
    else:
        blocked = set()
    enabled = _read_enabled(char_id)
    servers: dict = {}
    tools: list[str] = []
    for name, on in sorted(enabled.items()):
        if not on or name in blocked:
            continue
        if not plugin_owned_by(name, me):
            continue
        m = _read_manifest(name)
        if m is None:
            continue
        servers[name] = {"type": "stdio", "command": sys.executable,
                         "args": [str(PLUGINS_DIR / name / m["entry"])]}
        tools += [f"mcp__{name}__{t}" for t in m["tools"]
                  if context != "wake" or wake_on.get(name)
                  or t not in WAKE_TOOL_EXCLUDE.get(name, ())]
    if not servers:
        return None, []
    payload = json.dumps({"mcpServers": servers}, ensure_ascii=False)
    if not cfg_path.exists() or cfg_path.read_text("utf-8") != payload:
        tmp = cfg_path.with_name(f".{cfg_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(payload, "utf-8")
        tmp.replace(cfg_path)
    return str(cfg_path), tools
