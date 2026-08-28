# 一期 Plan：多角色化 + Cass 搬家 + 醒来预算

> 2026-08-12 定稿。目标：cassette 从「恰好一个 TA」变成「N 个角色可切换」，把 Cass（mianmian）
> 作为第二个角色搬进来，并给醒来加 token 预算。二期（同居世界，见 PLAN_cohabit.md）、
> 三期（工作群）另立 plan，但本期的数据结构要给它们留座位（会话模型、senderID、channel）。

## 已拍板的决策

- **Cass 搬家，不双活**：mianmian app 退役（两边各持权威聊天记录 + 双 wake 调度器，双活必分叉）。
- **persona 一个字不改**（1–96 行：人设/关系/风格/摩擦/形态/安全词/NSFW 规则原样搬）。
  「技术环境」起的工具描述按此口径处理：**Playroom、轮盘（Ruota della Fortuna）、团团三段删掉**
  （mianmian 独有）；**Ombre 使用习惯三行 + 「时间感」保留**（cassette 有同一套记忆工具、同样注入时间）。
  搬家后工具能力全靠 cassette 现有机制给（memory_block、插件 addendum），不写回 persona。
- **插件商店开关 per 角色分开算**。
- **角色不绑死 Claude——留通用引擎缝**：char.json 加 `engine` 字段，缺省 `claude-code`
  （现状：claude -p 子进程 + MCP 全家桶）；预留 `openai-compat`（`{base_url, api_key_env,
  model}`，OpenAI 兼容协议吃下 DeepSeek/Kimi/Ollama/OpenRouter…，mianmian workgroup 的
  DeepSeek worker 已验证此路）。M1 只做接口缝——聊天 call_claude/流式、醒来 run_claude_wake
  三个调用点收拢成 engine 分发——不实现第二引擎（等首个非 Claude 住户入住再写）。
  能力分层：醒来四段协议/表情标记/房间文本协议通用；MCP 插件与 Ombre MCP 工具是
  claude-code 专属，通用引擎的记忆走「后端代调 Ombre」（待议）。API 引擎按 token 计费，
  per 角色预算对它们更要紧。
- **Ombre 路线**：Cass 先从 ~2.7 升到 2.13.1（与 cassette 对齐，照 mianmian 侧
  `server/PLAN_ombre_upgrade.md` 执行，那份 plan 的数据兼容性已在副本实测过）；
  以后有空两边一起上最新（写此文时上游 2.17.4）。2.13.1→2.17.4 无数据迁移级变更，
  唯一 BREAKING 是 2.15 砍了 SSE MCP 传输——两边都走 streamable-http，不受影响。
- **Claude Desktop 与 Cass 的脑子正式分家**：Desktop 久不用了，升级日把 Desktop 配置里的
  ombre MCP 挂载摘掉。不摘的话它仍指着同一份 luvclaude，哪天打开就是旧代码写生产桶；
  摘了就再没有「两边版本要同步」的约束。
- 二期待议（本期不做）：群聊里 Cass 与 TA 互相的世界观认知设定。

## 版本地图（2026-08-12 实况）

| 实例 | 版本 | 备注 |
|---|---|---|
| Cass `ombre-brain`(18001) | 官方 `p0luz/ombre-brain:2.13.1`（2026-08-15 升级，compose 在 `~/Documents/ombre-cass-run/`） | 290 桶，存 `~/Documents/luvclaude`；回滚：`ombre-brain:rollback-20260815` + `luvclaude_backup_20260815.tar.gz` |
| cassette `ombre-cassette`(18002) | 官方 `p0luz/ombre-brain:2.13.1` | |
| 上游最新 | 2.17.4 | git tag 停在 v2.7.6，版本看 CHANGELOG/VERSION |
| ~~`ombre-upgrade-test`(18003)~~ | 已清（2026-08-15） | 容器和 `~/Documents/_ombre_upgrade_test` 都删了 |

## M0 · 醒来预算（独立可发版，最先做）

背景：现有三道用户闸（每天条数 / 最小间隔 / 刚聊过静默）全在 `wake.try_push`——模型跑完一整轮
才拦，只拦推送不省 token（`wake.py:284` 注释原话「闸门只拦推送不拦思考」）。醒来前只有
`MIN_WAKE_GAP_SEC`、code 避让、静默期拦「随机醒」三道隐形闸；**scheduled（他自己定的 NEXT）
不受任何预算约束**。

- 新增设置 `wake_daily_budget`（每天最多醒 N 次）。闸位在 `wake.maybe_wake()`、起模型之前：
  从 `read_wake_log(limit=1000)` 尾部数今天 `source=="wake"` 条数（`push_block` 同款数法）。
- 拦 scheduled + probability（自发醒）。硬触发（邮件白名单、到点提醒）豁免——「到点必须说」
  是契约；但计数照记，Mind 页可见。
- 预算耗尽时 `next_wake_at` 原地待命不清不改（复用 code 避让的「return 不写盘」模式），
  日切后第一个 tick 兑现。
- prompt 如实告知：预算见底时注入提示句（仿 push_block），让他自己决定 NEXT 定不定、定多远。
  不告知 = 他定了 NEXT 却不兑现，是骗他。
- 设置页加一行 UI，与现有三闸并列，文案区分「说话预算」vs「醒来预算」。
- **预算按角色分，不共用**：M0 上线时只有一个角色，设置先挂全局 settings.json；
  M1 随其它 wake 策略一并移入角色的设置。**实施时改了落点**（2026-08-14 订正）：
  没进 char.json，进的是 `state/characters/<id>/settings.json`——app 的设置页本来就在
  读写这份，进 char.json 就得再开一条写路径。char.json 只留接线（engine/ombre/display_name），
  会被人改的策略留在 settings。每角色独立预算、各数各的 wake_log。
  不设全局总闸——总量 = 各角色预算之和，要收紧就分别调低（全局总闸有「先醒先得」
  抢额度的问题，不做）；全局只留「醒来并发 = 1」的锁。

## M1 · 后端角色化（全程向后兼容）

**角色注册表**
- `server/characters/<char_id>/`：`persona.md` + `char.json`
  （display_name、engine、ombre_url、wake 设置、独占资源绑定）。可考虑 characters/ 独立 git 仓
  （沿用 mianmian prompts 仓的版本管理习惯）。
- 新模块 `server/characters.py` 替掉 import 时解析的 `config.PERSONA_PATH` 单例；
  `config.agent_name()` → char.display_name。现有 TA 启动时自动收编为默认角色（就地迁移旧布局）；
  **不带 char_id 的请求一律落默认角色**——迁移期 App 一行不改也不坏。
- `user_name` / `user_pronoun` 留全局（用户只有一个）；`agent_name` + wake 策略入 char.json。

**state 命名空间化**（对照单例清单逐个改）
- `state/characters/<id>/{wake_log.jsonl, recent_window.json, schedule.json, browse_log.jsonl,
  persona_rendered.md}`；`state_store.py` 读写函数全部加 char_id 参数。
- `outbox.json` 保持全局一份，每条加 `char_id`（App 路由用）；`/pending/ack` 不变。
- 插件启用状态 per-char（`state/characters/<id>/plugins_enabled.json`）；
  MCP config 按（角色 × context）渲染，文件名带角色前缀防互踩。
- **独占资源**（2026-08-14 订正，原文按插件分归属是错的）：归属的核心是「这东西只有一份，
  所以同一时刻只能给一个人用」。归属记在**资源**上不记在插件上——一个插件可能吃好几样
  （game-story 既要游戏账号又要会话），几个插件可能吃同一样（两个 game 插件共用一个账号，
  按插件分开记的话两个角色会同时上手同一个号）。表在 `plugins.EXCLUSIVE`（插件 → 资源列表），
  归属存 `state/plugin_owners.json`（**不是 char.json**：它是「这样东西归谁」，不是角色属性），
  改归属走 `POST /plugins/owner` 或手编该文件。插件挂不挂 = 它吃的每一样资源都归你。
  一期不做共享/占用轮转——归谁、别人就开不了。
  - 真的只有一份：`maayuan`（机主的《如鸢》账号，**不是"MuMu 只有一台"**——模拟器能开
    多实例，卡住的是账号）、`beacon`（一装置一卡）。
  - **暂时**只有一份（是我们自己限的，将来每人一份，做成之后就从 EXCLUSIVE 里删掉）：
    ~~`mailbox`~~ ✅ 2026-08-15 做掉了（一人一个信箱，接线 characters.mail_conf）；
    ~~`chrome`~~ ✅ 2026-08-24 做掉了（一人一个浏览器：characters.browser_conf +
    keeper 全线收 char_id + PLUGIN_GATE 没接线不挂载；当年写的「mounted 不给插件传
    env」那半句早已不成立）；`tmux` ← 留到三期工作群（每人一台"自己的 MacBook"）。

**wake 调度多角色**
- 仍是一个 `scheduler_loop`，每 tick 遍历角色：各自 next_wake_at / 概率 / 预算。
- 全局「醒来并发 = 1」锁：同 tick 两角色都想醒就排队，防两个 claude -p 叠着烧。
- `chat_turn_active` 互斥改 per-char（Cass 在聊天不拦 TA 醒来）。

**API**
- `ChatRequest` 加 `char_id`；`/mind` `/settings` `/plugins/*` 加角色参数；
  `/memories/*` 按 char.ombre_url 转发（遗忘/归档删除两个按钮已上线且兼容 2.13.1，无需再动）；
  `/pending` 条目带 `char_id`。
- code/game 会话一期**保持全局唯一**（三期再泛化），session.json 记归属角色，
  会话话语进 outbox 时带 char_id。

## M2 · iOS 会话列表 + 多角色消息模型

- `MessageSender` 枚举 → `senderID: String`；Codable 解码给旧值默认映射
  （`me`→`"me"`，`other`→默认角色 id），旧 JSON 无痛读入。
- 二期预埋：`ChatMessage` 加 `channel` 字段（默认 `text`）——同居世界（PLAN_cohabit.md）
  的「真实交互 / 手机 text」两类对话靠它区分，现在加一行省以后迁移。
- 聊天记录按会话分文件 `Documents/conversations/<conv_id>/chat_history.json`；
  首启把现有整份迁成默认角色私聊，失败保留原文件（沿用 `.broken` 思路）。
- 导航（实施时改了主意）：**不做微信式列表根，做会话切换器**——顶栏标题可点进会话列表
  （+ 抽屉「会话」入口），选中即切、返回聊天。理由：二期同居世界的「房子视图」才是终局
  根视图（手机=聊天 UI 降级成悬浮层），现在把根改成微信列表是注定要拆的中间态。
  抽屉页面按归属拆：Mind / 记忆 / 设置 per-角色（App 统一漏斗 authedRequest 自动带
  当前角色），插件商店跟当前角色走。
- 头像 `Profile/other.png` → `Profile/<charID>.png`；`syncPending()` 按 char_id 路由 + 未读角标。
- ⚠️ 新增 Swift 文件注意 pbxproj 是 skip-worktree 的，提交按老规矩摘 hunk。

## M3-0 · Cass 的 Ombre 升级（搬家紧前执行）—— ✅ 2026-08-15 完成

> 实况：官方镜像跑在 `~/Documents/ombre-cass-run/`（容器名沿用 ombre-brain，只绑 127.0.0.1，
> 挂进 ombre-cassette-run_default 网络借 ombre-ollama 做 bge-m3 embedding，290 桶全量索引完；
> 顺手修掉了 2.7 时代 embedding 404）。MCP 开静态 token，mianmian 后端改为动态渲染
> `server/state/ombre.mcp.json`（git 里的静态文件废弃）+ `_ombre_mcp_rpc` 带 Bearer 头。
> md5 全量比对 0 丢失；首启衰减归档 6 个刚跌破 0.3 的桶（只改 type 字段，可从 archive/ 恢复）。

照 `~/mianmian-app/server/PLAN_ombre_upgrade.md`，结合搬家前提有四个调整：

1. **§三②（改 mianmian main_v2.py 写路径）跳过**：升级后老镜像的自有 trace 端点消失，
   mianmian 记忆页改/删会 404——但 Cass 马上搬走，补丁写了就扔。把升级排在搬家紧前，
   过渡期接受 mianmian 记忆页只读；聊天/醒来的记忆读写走 MCP 不受影响。
2. **容器直接用官方 `p0luz/ombre-brain:2.13.1` 镜像**（与 cassette 一致），
   不再从本地 clone 构建。本地 clone 不 pull 到 main。
3. **§三④（Claude Desktop）改为摘挂载**：从 Desktop 配置删掉 ombre 那段，正式分家（见决策）。
4. **收尾**：`docker rm -f ombre-upgrade-test`、清 `~/Documents/_ombre_upgrade_test`；
   备份/回滚 tag/回归清单照原 plan §五。

## M3 · 搬家

服务端（一次性脚本 `server/tools/import_cass.py`）：
1. persona：1–96 行原样 + 保留段落（Ombre 习惯、时间感）→ `characters/cass/persona.md`；
   Playroom / 轮盘 / 团团段删除。
2. `wake_log.jsonl`（~546KB）拷入 Cass state 命名空间，Mind 页历史无缝续上。
3. `char.json` 写 `ombre_url: http://localhost:18001/mcp`——记忆一个字节不动，跟人走。
4. 贴纸库一期全局共用。

聊天记录（唯一要动 mianmian 的地方）：
- mianmian `Info.plist` 加 `UIFileSharingEnabled`，重装一次，Finder 把 Documents 整个拖出
  （含 ChatImages / ChatFiles / Stickers）。
- cassette 侧导入器：解析 chat_history.json，kind 映射（forumAction 等降级为文本占位）；
  **媒体文件按文件名重挂**——`.image(URL)` 若存绝对沙盒路径，跨 App 必断，导入前先验证存储格式。

退役切换日 checklist（2026-08-15 晚执行完毕，M3 完成）：
- [x] mianmian 后端已停（`bootout` + `disable` 防重启复活；outbox 清零后才断电，
      com.claude.wake-mianmian 定点唤醒 agent 一并停了）
- [x] **Ombre 18001 容器保持常驻**（已是 cassette 的依赖）
- [x] Desktop ombre 挂载已摘（M3-0 2026-08-15 做掉，备份 `claude_desktop_config.json.bak-ombre-20260815`）
- [x] mianmian app 手机上留作只读
- 实录：聊天 5474 条 + 头像 + 媒体并入（default 1496 条无损；35 个已删贴纸灰占位）；
  wake_log 722 条续上；⚠️ Finder 拖同名文件夹是**替换不是合并**，必须导出→Mac 合并→删→回填。

## 顺序与风险

顺序：M0（当天可上）→ M1 → M2（M1/M2 可并行，靠默认角色兼容层解耦）→
M3-0 → M3 演练（先导测试角色验格式）→ 正式搬家 + 退役。

盯着的点：
- 两角色各聊各的 = 并发 claude -p，订阅限流没数据，先跑着观察。
- Cass persona 里没提但 mianmian 世界观里有的东西（比如他「切 code 模式读代码」的纪律
  引用的是 mianmian 的 code_start）——搬后 cassette 的 codemode 插件同名能力在，口径基本无缝，
  但演练时确认一遍他调的工具名都存在。

---

# 复盘：角色串台这一类（2026-08-28）

起因是 `f85608f`（default 的整份设置被写进 cass，Cassius 显示成 cassette）。修完之后
按新的 `postmortem` skill 走了一遍四步，结论记在这里——**这不是一个 bug，是多角色化
带进来的一整类**。

## 1. 根因：四个条件同时成立

1. 设置回写是**延迟**的（改动防抖 600ms 才 POST）；
2. 请求上的 `?char=` 在**发请求那一刻**才去读全局 `CurrentCharacter`；
3. `ProactiveSettingsStore` 只有一份 settings，身上不带「这是谁的」；
4. 设置页右上角就挂着切人按钮，`.id(currentCharID)` 一变整页重建，而防抖 Task 寄在
   `@State` 上 —— 重建**不会**取消它。

于是两个窗口会把 A 的整份设置贴上 B 的标签落地：「改一下 → 600ms 内切人」，
以及「切人后 GET 还没回、旧值还留在 store 里」（后者是一次网络往返，比 600ms 宽）。

帮凶：本地无缓存时的兜底把 `agentName` 写死成 `"cassette"`——**给身份编了个默认值**，
把「缺身份」变成了「错身份」。

## 2. 归类

> **延迟写入 + 可变的全局「当前」= 迟早写到别人头上。**

抽象条件：动作从发起到落地中间有异步间隔；身份不在数据身上、要现去问一个全局；
这个全局在间隔里能被改。少任何一条都不出事。

对照后端就清楚了：`server/` 那边 `char_id` 显式一路传参、醒来队列**按 cid 做 key**
（`cohabit_queue._pending: dict[cid → 原因清单]`，身份靠存放位置表达，不是靠现问）、
MCP 子进程认的是启动时就钉死的 `CASSETTE_CHAR_ID`。**同样是「环境里的身份」，
进程级常量安全，可变全局致命。**

## 3. 规避规则（迁移到任何多账号 / 多租户 / 多角色的东西上）

1. **身份跟着值走，不跟着「现在」走**——每份带角色维度的数据自己带 charID。
2. **异步动作在发起那一刻把 (值, 身份) 成对快照**，执行时只认快照。
3. **异步响应落地前校验身份没变**，变了就丢弃——否则旧响应会覆盖新主人的值。
4. **切身份是个事务**：切之前 flush 未落地的写、切之后重建视图、重载缓存。三件都要做，
   漏一件就是一次串台。
5. **别给身份编默认值**。静默兜底会把「缺身份」变成「错身份」——前者会报错，后者不会。
6. **能用结构表达身份就别用字段，能用字段就别用全局**（队列按 cid 分格 > 每条带 char
   字段 > 执行时现问）。

## 4. 捉虫

### 竖着翻 git log：这是第三次，不是第一次

| 时间 | commit | 表现 | 形态 |
|---|---|---|---|
| 08-15 | `8d533c0` | B 的会话里显示 A 的聊天记录 | 读侧：视图私有 @State 不知道自己属于谁，切人不复位 |
| 08-15 | `5a259b2` | 给 B 打的字被塞进 A 的 tmux | 路由侧：全局开关当归属判据；且后端三个端点压根没声明 char 参数 |
| 08-28 | `f85608f` | A 的设置被写进 B，名字被覆盖 | 写侧：延迟动作在落地时才取身份 |

多角色化之前，「当前 = 唯一」这个前提隐含在全代码库；改成多角色后它散落在每一处状态里，
**从来没有人一次性清算过**。这是整份复盘里最值钱的一条。

### 横着扫代码：读代码得出的六处（未在真机复现）

| | 位置 | 判断 | 处置 |
|---|---|---|---|
| A | `ContentView` 头像选图 | 同款：`loadTransferable` 是异步（iCloud 图能下几秒），落地时才问「现在是谁」 | **已修**：发起时钉死 char，`setAvatar(char:)`；只在 char == 当前时刷界面 |
| B | `ContentView.scheduleWindowSync` | 同形状（1s 防抖 + @State Task + 全局身份）。今天不串台纯属侥幸——历史和身份是同一刻读的；实际后果是**A 的删改静默丢失**。而且「排期时快照 messages」这个内容正确性上完全合理的改法会当场把它变成串台 | **已修**：排期时快照 (历史, 角色)；切人前 `flushWindowSync()` |
| C | `draft` / `pendingImages` / `pendingFiles` / `pendingSticker` | 切人不清：给 A 打的字、选的图，切到 B 按发送就发给 B。`8d533c0` 当时明说「输入栏草稿不受影响」，那是从「别弄丢用户打的字」定的，另一面没考虑 | **已修（改成一人一份）**：新增 `DraftStore`，落 `Documents/drafts/<charID>/`，文字防抖 500ms 只重写 meta、附件增删整份重写，切人前存旧的、载入新的 |
| D | `switchCharacter` 里三个 Task 的响应 | `refreshDraftCount` 落地不校验身份，A→B→A 快切时旧响应会后到覆盖。`syncCodeMode` / `charListStore.refresh` **不算**——它们拿的是全机事实（tmux 会话唯一、角色清单全局），不是某个角色的数据 | **已修**：`refreshDraftCount` 加身份校验 |
| E | `ProactiveSettingsStore` 回写失败 | `try?` 吞掉错误 + 清掉 pending：断网时本地已是新值、后端还是旧值，下次对齐把它静默盖回去（「我改的设置自己变回去了」）。不是串台，同属「两份真相没人对账」 | **已修**：失败保留 pending；`refreshFromServer` 先 `await flushPendingNow()`（必须 await，fire-and-forget 会让 GET 抢在 POST 前面） |
| F | 后端 `char_id=None → default` | 同一个模式（静默兜底把「缺身份」变成「错身份」），但后端至今没出过事，且有 `characters.resolve()` 兜着 | **评估后不改**：改动面覆盖全部老调用点，收益不抵风险 |

扫的范围：`ios/cassette/*.swift` 全量 + `server/*.py` 里所有 `char_id` 相关路径。
房间/小屋那一片不在范围内——`/world/*` 的 actor 是 `world.USER_ID`，不带角色维度。

## 5. 待真机验证（机主）

1. 给 A 选一张 iCloud 里的大图，趁下载中切到 B → 头像应落在 A 身上；
2. 在 A 的聊天里删一条消息，1 秒内切到 B → A 的后端窗口应已对齐（看 TA 下次醒来的世界）；
3. 给 A 打半句字 + 选一张图，切到 B（B 应是空的/B 自己那份）再切回 A → 字和图都在；
4. A 里发送一条 → A 的草稿应清空，切走切回还是空的；
5. 杀掉 app 再开 → 草稿还在。
