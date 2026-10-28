# Plan：工具挂载改延迟 + 固定菜单 + 开场探活 + 重铸留用法

> 2026-10-29 机主与 Claude Code 会话定稿。**设计文档，业务代码一行没动**；
> 只新增了四个探针工具（§8），数字全部出自它们的实测。
> 起点是「系统提示里的游戏节奏每轮都占 token」，顺藤摸出来：工具 schema 才是大头
> （每轮固定 ~36k），菜单与挂载在 session 内永不对账，Ombre 开场没连上整条
> session 失忆。
> 同日补：与 PLAN_interject、PLAN_gamechat 的交界见 §11；§9 是三份合排的施工顺序。

---

## 0. 一句话

**sdk 聊天 session 开工具延迟；Ombre / basics 常驻，其余延迟；每个角色一份固定菜单；
开 session 时对 MCP 探活并把缺席如实告诉他；重铸时把最近用过的工具用法铸回去。**

| | 现在 | 改后 |
|---|---|---|
| MCP schema | 全量常驻，~36.1k tok / 请求 | Ombre+basics 常驻 ~8.2k，其余按需取 |
| 每请求固定开销（cass，Opus 估） | ~54.8k | ~26.5k（18.3k 全延迟基线 + Ombre 7.4k + basics 0.8k） |
| 软阈 100k 里留给对话的 | ~45k | ~73k |
| 菜单 | 按开 session 那刻的挂载过滤渲染 | 每角色固定一份，按配置渲染，不随运行时状态变 |
| MCP 没连上 | 静默缺席整条 session，没人知道 | 开场探活 + 重连一次 + 注入告知 + 门拒同话 |
| 重铸后 | 全量 schema 天然都在 | 最近用过的工具用法铸回 transcript，没铸上的他自己再取一次 |
| 游戏节奏 | 常驻系统提示 | game_start 返回值带上 + 重开后首轮补注 |
| 收尾格式「◆」 | 常驻系统提示 | 删 |

## 1. 实测依据（10-29，agent-sdk 0.2.148 / **SDK 自带 CLI 2.1.251**）

> ⚠️ 两个 CLI：SDK 的 `_find_cli` 先找包里自带的 `_bundled/claude`（2.1.251，`_cli_version.py`），
> 找不到才用 PATH 上的 `claude`（2.1.310）。所以 sdk 聊天 session 和本稿全部探针跑的都是 **2.1.251**；
> `-p` 老路（wake.py / cohabit / forge_regress 的 CLI 腿）跑的是 2.1.310。下文 `alwaysLoad` 的说明原文
> 是从 2.1.310 二进制里 strings 出来的，行为实测在 2.1.251 上——两版都有这个字段。

### 1.1 体量（`mcp_probe.py`，cass 真实系统提示，`get_context_usage`）

| | 全量 | 延迟 |
|---|---|---|
| 系统提示 | 13,626 | 13,626 |
| 内置工具 | 5,041 | 4,650（+426 延迟） |
| MCP 工具 | 36,099 | 0（36,100 延迟） |
| **合计** | **54,774** | **18,284** |

按 server：galatea 11,330（26 个）、browser 7,843、ombre-brain 7,449、jobhunt 2,369、
game 2,298、notion 1,692、prs 914、basics 756、mail 701、webpage 480、skills 191。

### 1.2 开场状态（`mcp_probe.py`）

- `connect()` ~1.4s 返回；首拍 10/12 已 connected。
- **galatea 返回后还 pending 2.4–2.6s**（两次一致）；game（进程内）+0.2s 才出现。
- 定下来之后每个 server 实给工具与预期**全部对上**（这次 Ombre 在线，掉线情形未测）。

### 1.3 toggle / 名单（`mcp_probe.py --ask`）

- stdio/http server：toggle off → `disabled`、名单里消失、**模型答 NONE**；on → 回来、
  模型报得出全名。延迟模式下名单是活的。
- **进程内 SDK server（game）不吃 toggle**：off 静默空操作（仍 connected、14 个都在），
  on 抛 `SDK servers should be handled in print.ts`。
- ToolSearch 取过一次，同一份 transcript 里第二次直接调，不再取。
- ⚠️ toggle off→on 之后再调：Sonnet 先重取一次再调（成）；**Opus 三次都返回
  `API Error: Opus 5's safeguards flagged this message`**（换过措辞照旧，前四轮正常）。
  原因不明 → **生产不用 toggle**（§5.3）。

### 1.4 按 server / 按工具控制延迟（`mcp_probe_defer.py`）

- MCP 配置 `alwaysLoad: true` → 整个 server 常驻：http（ombre）、stdio（basics）、
  **进程内 sdk（game）都生效**（SDK 把除 instance 外的键原样传给 CLI）。
- 单工具 `_meta["anthropic/alwaysLoad"] = true` → 只钉那一个（stdio/http 可用；
  SDK 的 `@tool` 只生成 maxResultSizeChars 的 _meta，进程内工具做不了单工具级）。
- **延迟开不开看 ToolSearch 挂没挂，不看环境变量**（SDK 路 / 2.1.251 实测）：不设 `ENABLE_TOOL_SEARCH`、只挂
  ToolSearch → 全部 MCP 延迟。与 `pipeline.tool_search_on` docstring 的说法相反（§7）。
- 「默认全量只延迟几个」的写法 = 挂 ToolSearch + 要常驻的 server 设 `alwaysLoad: true`；
  单给某个 server 设 `false` 做不到。
- ⚠️ CLI 里这段说明带 `@internal` 标记——**不是公开契约**，升级可能变（§8 探针兜）。

### 1.5 重铸能不能留住用法（`forge_toolref_probe.py`）

目标工具 `probe_echo(zq_token_7)`——参数名猜不到，第一次就传对 = schema 真在眼前。

| | Sonnet | Opus |
|---|---|---|
| 对照：resume 不带 tool_reference | 猜成 `{"hello":…}`，校验报错 | 拒调：「只有名字没有 schema」 |
| 铸进 `ToolSearch → tool_result[tool_reference]` | **一次传对，成** | **一次传对，成** |

- transcript 里取用法的形状：assistant `tool_use{name: ToolSearch, input: {query:
  "select:X", max_results: 1}}` → user `tool_result{content: [{type: tool_reference,
  tool_name: X}]}`。CLI resume 时从 transcript 恢复已加载集，伪造的也认。
- 没取用法的延迟工具，模型**只有名字没有参数**；无参工具瞎调能成，有参的必错——
  所以兜底「他自己再取一次」是真兜底（Opus 实测会先去取，不硬猜）。
- ⚠️ resume 后、首轮之前 `get_context_usage` 不可信（totalTokens=1、isLoaded 全 False）。
- 铸的位置要照实：测试里 ToolSearch 挂在一句编的话上，Opus 当场指出「那时我只加载了
  schema，并没有真正调用过」——他会对照上下文。

## 2. 延迟 + 常驻

- `build_options`：sdk 聊天 session 挂 ToolSearch（进 tools 和 allowed_tools）。
  环境变量 `ENABLE_TOOL_SEARCH=true` 照给（显式，别依赖 CLI 默认）。
- 常驻名单写在一处（`pipeline.ALWAYS_LOAD_SERVERS = {"ombre-brain", "basics"}`），
  `_merge_mcp_file` / Ombre dict 组装时给这些 server 的配置加 `alwaysLoad: True`。
  - Ombre：几乎每轮都用（开场浮记忆、hold），7.4k 值得常驻。
  - basics：now / fetch / next_wake，0.8k，高频。
  - **game 不常驻**（机主拍板）：只对 cass 有用、平时用不上；游戏泵靠 §6 铸回用法。
- 总闸：`CHAT_TOOL_DEFER=1`（.env，默认先关），关 = 一切照旧全量，回退一行。
- `-p` 老路（wake.py / cohabit）不动：`TOOL_SEARCH_CONTEXTS` 口径照旧。

## 3. 固定菜单

### 3.1 口径

- 每个角色一份 `characters/<id>/tool_menu.md`（已有），**按配置渲染、不按运行时状态
  过滤**：「配置」= 这个角色装了、归他、开着的插件 + 配了的 Ombre + 宠物 / skills / PR
  开关 + 内置工具开关。Ombre 此刻连不连得上、某个 stdio 起没起来，**不影响菜单**——
  运行时缺席走 §5 的注入告知。
- 好处：同一份配置永远渲染出同一份系统提示，前缀缓存最稳；运行时状态与系统提示解耦。
- 渲染器改动（`pipeline.tool_menu_block`）：
  - 名字表从「本轮实际挂载」换成「这个角色配置上的全集」（`configured_tool_names(char_id)`
    ——各家常量 + 插件 manifest 的工具清单 + 内置工具开关），不探活。
  - needs 语义不变（全在才渲染）——配置关掉的能力照旧不提。
  - **场景注记自动补**：块里有工具不在 `mounted_tool_names("wake")` 里（NO_WAKE_PLUGINS /
    WAKE_TOOL_EXCLUDE / 写类醒来禁用面），渲染时在块尾自动加一句「醒着的时候这几样用
    不了：…」。作者不手写场景，手写会和门的口径漂。
  - 头一句随延迟开关换：开着就说「用法默认没加载，先 ToolSearch select: 取」（现成逻辑）。
  - `_warn_uncovered` 保留，并加反向检查：菜单提到、但配置里没有的工具 → 日志喊。
- 维护纪律（写进菜单文件顶部注释）：**上新插件 / 改工具名必须同步改菜单**；日志那两条
  喊话是提醒，不是过滤。

### 3.2 新增菜单块（草稿在会话里给过，定稿时照抄）

- `## 翻代码和资料 <!-- needs: Read, Grep, Glob -->`：只读范围（cassette 仓 / dossier 仓），
  .env 不在范围，state/ 只看自己房间 + jobhunt / workbench / webpages。
- `## 动手改东西 <!-- needs: Edit, Write, NotebookEdit, Bash -->`：一动一卡、卡前说人话、
  只在 TA 跟你说话时能用。
- `## 改代码：提一个 PR`（改写现有块）：开头加「你自己醒着时想改代码只有这条路；
  聊天时改动大也走这条」；「命令跑不了」改成「git 的事全交给这几个工具」（chat 轮
  Bash 是能弹卡的，原话不准）。
- `## 拿不准的事让 TA 选 <!-- needs: AskUserQuestion -->`：要停下来等答案才用卡，
  随口问就直接问；只在 TA 跟你说话时能用。

内置工具（Read 等）原本不在 `mounted_tool_names` 里、needs 写它们永远不渲染——§3.1 的
「配置全集」把它们按 READONLY_TOOLS / WRITE_TOOLS / QUESTION_CARDS 三个开关并进去。
**不要**把它们塞进 `mounted_tool_names`：那张表兼做醒来允许集（wake_tools），
AskUserQuestion 进去就在醒来轮放行了。

### 3.3 系统提示瘦身（同批）

- 删 `chat_loop.py` 里拼文件工具段 + `_write_paths_text` + 问答卡那段 → 进菜单（§3.2）。
- 删 `_discipline_block` 调用（`code_addendum_chat.md` 不存在，整段就剩「◆ 结论 ← 出处」
  一句）。`_capture_capsules` 一并删：它写的行为账 `acts-<cid>.jsonl` 已无读者
  （`recent_acts` 零调用、`_acts_block` 09-01 已删、`_fold_trace_lines` 读的是段事件账）。
  存量 203 行留在账里不动。
- 删 `system += GAME_RHYTHM_SYSTEM`，改 §4。
- 做完之后 `one_turn_hint` 重新落回系统提示最末——它注释里「排在最尾是有意的」
  现在是假的（后面还挂着 ~2.5k），这批顺手修好。

## 4. 游戏节奏：拿起游戏时注入

- `_game_story_start_unified` 的返回 `hint` 带上节奏全文（去掉「game_start 之后才算
  拿到」那句条件，开头改「游戏在手边了，这段时间你的时间按短轮走……」）。调它的那一轮
  当场看到，第一个「·」之前就懂规矩。
- 重铸会冲掉它（工具返回不进账）：`_open_session` 里若 `handle.meta["game_pump"]` 在，
  置 `pump["rhythm_due"] = True`；下一个**新起的轮**（tick / 机主在游戏中发来的消息 / 醒来，
  哪个先到算哪个）在注入文字前补节奏，发出后清旗。机主的消息现在走终端口成 `PumpNote` 轮，
  PLAN_gamechat 之后是普通 chat 轮——两种都是新起的一轮，补在注入里、不进账。
  用一个 helper 包「新起一轮」的 `client.query` 入口，别各处手写。
- ⚠️ **在飞插话（PLAN_interject 的 `Interject`）一律不加任何前缀**——插话靠原文跟 transcript 里
  `queued_command` 配对，前面多一个字就配不上，会被误判成「没吸收」（§11）。§5.1 的缺席告知同此。

### 4.1 泵开着时的重开统一收口

泵开着时 session 重开有四条路：泵的 N 张段内重铸（`_game_reforge`）、引擎打嗝重开
（`_pump_round` 里 `client is None`）、chat 路判脏重铸、§5.2 配置变了的重铸。现在只有第一条带
近 K 张图回填（`render_tail`），其余三条什么游戏上下文都不带。PLAN_gamechat 之后 chat 路判脏
在游戏里会常态化，这个缺口必须先补。

- `_open_session` 自己判 `handle.meta["game_pump"]`：在就**一律**带上三样——
  ① 近 K 张图回填（`_game_shot_tail` 挪进来，`_game_reforge` 不再单独传）；
  ② `rhythm_due` 置位（§4）；③ 取过的用法铸回（§6，做好后接上）。
- 不管哪条路触发的重开，带的东西都一样——「某条路漏带一样」这类问题从结构上消失。
- game_end 后 transcript 里残留那段到下次重铸为止——措辞本就是「拿着游戏的时候」，不清。

## 5. 开 session 探活

### 5.1 开场

`_open_session` 里 `connect()` 之后：

1. `get_mcp_status()` 每 0.2s 拍一次直到没有 `pending`，超时 10s（galatea 实测 ~2.6s）。
2. `failed` 的逐个 `reconnect_mcp_server` 一次，再拍一次。
3. 用 status 里每个 server 的 `tools` 与配置全集（§3.1）对账，缺的落
   `handle.meta["absent_tools"]`（按 server 分组 + 错误原话），打日志；缺 Ombre 推 Bark。
4. 缺席告知：下一轮注入前缀加一句知觉白描（「〔记忆那边这会儿连不上〕」这类，按 server
   一句，文案表写在一处）；`_wake_gate` 对 absent 工具直接 deny 同一句话——不让他调到
   CLI 那层去撞一个不在的工具。

### 5.2 中途

- 现有 60s 轮间隙看守里加一拍 `get_mcp_status()`（只在空闲时，不在轮中）：
  - connected → failed：`reconnect_mcp_server`；还不行就进 absent + 下一轮告知。
  - absent 里的恢复了（重连成功）：出 absent + 下一轮告知「连上了」。
- 插件在 app 里开 / 关：**不 toggle**，给该角色 session 打「配置变了」标记，下一个轮间隙
  走一次巩固 + 重铸（新配置 → 新菜单 → 新挂载，口径一次对齐）。
  - 动手条件：没有在飞的轮、PLAN_interject 的 `pending` 为空（它规定 pending 非空不铸）。
  - **泵开着时推迟到 game_end**，别在一局中间换 client。

### 5.3 为什么不用 toggle

§1.3：进程内 server 不吃；stdio/http 能用但 toggle 回来后 Opus 那一轮稳定撞安全误报，
原因不明。`reconnect_mcp_server` 是否有同样问题**未测**——§9 第 1 条探针先补。

## 6. 重铸留用法

- **记账**：每轮从流里认两类事件，按角色落 `state/characters/<id>/tool_touch.json`
  （`{全名: 最后接触 ts}`）：ToolSearch 的 tool_result 里的 tool_reference（取到）、
  任何 `tool_use`（用到）。**记「最后接触」不记「哪天取的」**——游戏工具取在 game_start
  后、之后一直在用，按取的时刻挂，一局玩久了那轮滚出窗口，之后每次重铸都得重取。
  - 记账挂在读流的地方，不挂在 `Turn` 收尾：PLAN_interject 里插话没被吸收时，CLI 会在同一次
    读流里自己再起一轮，那一轮的工具接触也要记上。
- **铸回**：新函数 `_merge_tool_loads`，与 `_merge_alarm_calls` 同层、同在
  `_open_session`（`chat_loop.py:1486`）里调——段内重铸 / 引擎打嗝重开 / 漂移重铸三条路
  一次覆盖。
  - 按最后接触排序取前 N，**预算按 token 封顶**（暂定 8k，用 `get_context_usage` 的
    逐工具 tokens 算）；常驻 server 的不铸；当前配置里没有的不铸。
  - 挂到**时间不早于这次接触的第一条 assistant**（容差同闹钟），同一条上的合成一次
    `select:a,b,c`；那轮已滚出窗口 → 不铸（照实铸、不编时序）。
    不用「最近的一条」：PLAN_interject P1 之后一轮拆成多段、中间可能夹着插话，挑最近的会挂到
    插话之后那段上，铸出来变成「先听到插话、才取的用法」。forge 把工具铸在那条正文**前面**，
    所以正确的锚是「接触之后的第一段」。前提是 P1 给每段打准 ts。
    （`_merge_alarm_calls` 有同一个隐患，一并改。）
- **forge 改动**：tools 槽的 `result` 允许 content 列表（现在一律 `str()`）；digest 照
  json.dumps 走，确定性不变；带 toolUseResult 字段照真 transcript 补。
- **兜底**：没铸上的他自己 ToolSearch 再取——菜单头那句 + CLI 的延迟名单都在，
  Opus 实测缺 schema 时会先取不硬猜。代价一跳。
- 游戏中自然全是游戏工具（2.3k 全进预算），平时是最近聊天用过的那几样。

## 7. 顺手修（注释 / 文档与代码对不上的）

- `pipeline.tool_search_on` docstring：「只挂 ToolSearch 不给环境变量 = 延迟静默不生效」
  ——SDK 路（自带 2.1.251）下相反（§1.4）。`-p` 路（2.1.310）没测，改注释前先用探针补一腿。
- `chat_loop.py:1380`：「改文件=系统提示变=判脏触发重铸」——判脏只比对话历史，不比系统
  提示。§5.2 的「配置变了」标记落地后改写成真话。
- `chat_loop.py:536`：「排在最尾是有意的」——§3.3 做完自然成立，核一眼。
- README：插件「开关即时生效」在 sdk 路不成立（§5.2 落地后才成立）；架构 / Code 模式 /
  醒来四段协议几节整体过时，另起一批改。

## 8. 探针工具（留作维护工具，`server/tools/`）

| 文件 | 测什么 | 调模型 |
|---|---|---|
| `mcp_probe.py` | 开场 pending 时间线、挂载对账、各部分 token、toggle 名单变化 | `--ask` 时 |
| `mcp_probe_stdio.py` | 上面几个用的 stdio 探针 server（probe_ping / probe_pinned / probe_echo） | — |
| `mcp_probe_defer.py` | alwaysLoad 按 server / 按工具、ToolSearch 是延迟开关 | `--ask` 时 |
| `forge_toolref_probe.py` | 铸进 tool_reference 后 resume 能否一次传对参数 | `--ask` 时 |

**CLI / agent-sdk 升级前必跑清单**（三份计划共用）：`forge_regress.py` + 上表四个 +
PLAN_interject 的 `interject_probe.py`。alwaysLoad 是 `@internal`、tool_reference 恢复、
`queued_command` / `absorbed_mid_turn` 都是 CLI 内部行为，不是公开契约。
SDK 升级会连带换掉它自带的 CLI（现 2.1.251）——PATH 上的 `claude` 升级不影响 sdk 路，只影响 `-p` 路。探针临时目录要 `realpath`
（macOS `/var` → `/private/var`，CLI 按真路径找 transcript，已踩过）；跑完清
`~/.claude/projects/*mcp-probe-*`。

## 9. 施工顺序（三份计划合排：toolmount / interject / gamechat）

1. **补测**（只动探针，可并行）：
   - toolmount：一条 tool_result 多个 tool_reference；铸一个当前没挂的工具的 tool_reference；
     `reconnect_mcp_server` 之后 Opus 调用会不会撞 §1.3 的误报；Ombre 停掉时开场对账认不认得出。
   - interject P0（a–e；e 用 Opus + 自带 CLI 2.1.251 重跑）。
2. **系统提示瘦身**（toolmount §3.3 + §4）：不依赖延迟，可先上。上线后每个角色下一条消息重铸一次。
3. **账按气泡记**（interject P1）：单独上线也能减少现在的重铸；gamechat 和本稿 §6 的锚点都等它。
4. **泵开着时的重开统一收口**（toolmount §4.1）：先接图回填 + 节奏补注，用法铸回等第 8 步接上。
5. **游戏改走 /chat/stream**（PLAN_gamechat）：后端 `/code/status` 加 `engine` → app 改路由 → 观察旧包 → 退役终端口泵分支。
6. **固定菜单**（toolmount §3.1 / §3.2）。
7. **延迟 + 常驻**（toolmount §2），`CHAT_TOOL_DEFER` 默认关，cass 先开跑几天看 ToolSearch 频率。
8. **探活**（toolmount §5）+ **重铸留用法**（toolmount §6）。
9. **插话**（interject P2–P4，含泵开着时的 chat 轮）。
10. **阈值复核**：基线降 ~28k 后 `CHAT_SOFT/HARD/PANIC_TOKENS` 是否下调（量的是含 schema 的
    真实上下文），看一周 usage 再定。
11. 顺手修（§7）。

## 10. 待拍

- 「配置全集」里插件开关算配置（菜单跟着变、触发重铸）还是算运行时（菜单不变、只告知）？
  本稿按「算配置」写。
- §6 的 token 预算 8k、容差沿用闹钟的 120s，是否合适。
- 缺席告知的文案表（每个 server 一句）由机主写还是先给草稿。
- game 的 14 个工具是否要在泵开着时临时常驻——本稿答「不」：进程内 server 不能 toggle、
  换 client 只能重铸，§6 已经够用。

## 11. 与 PLAN_interject / PLAN_gamechat 的交界

| # | 交界 | 规矩 | 落在哪 |
|---|---|---|---|
| 1 | 往 session 里补文字（节奏 §4、缺席告知 §5.1） vs 插话靠原文配对 | 只补在**新起的一轮**的注入里；在飞插话一律不加前缀 | 本稿 §4；interject §4.2 |
| 2 | 铸回用法挂哪条 vs 账按气泡记 | 挂「接触之后的第一段 assistant」，P1 给每段准 ts | 本稿 §6；interject §4.3 |
| 3 | 配置变了的重铸 vs pending 插话不铸 | 只在无在飞轮、pending 空时铸；泵开着推迟到 game_end | 本稿 §5.2；interject §4.2 |
| 4 | 都靠 CLI 内部行为 | 升级前必跑清单共用（§8） | 本稿 §8；interject §6 |
| 5 | 游戏改走 chat 路后判脏常态化 | 先 P1、先 §4.1，再改路由 | PLAN_gamechat §2.2 |

互相帮忙的两处：延迟模式下每次 ToolSearch 都是一个工具边界，插话被当前轮吸收的机会变多；
P1 消掉多段判脏后重铸变少，§6 的压力跟着小。
