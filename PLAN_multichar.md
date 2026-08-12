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
| Cass `ombre-brain`(18001) | ~2.7 本地镜像 + 3 自有补丁 | 247 桶，存 `~/Documents/luvclaude` |
| cassette `ombre-cassette`(18002) | 官方 `p0luz/ombre-brain:2.13.1` | |
| 上游最新 | 2.17.4 | git tag 停在 v2.7.6，版本看 CHANGELOG/VERSION |
| `ombre-upgrade-test`(18003) | 2.13.1 测试残留 | 升级日 `docker rm -f` + 清 `~/Documents/_ombre_upgrade_test` |

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
  M1 随其它 wake 策略一并移入 char.json，每角色独立预算、各数各的 wake_log。
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
- **独占资源**：game（一台 MuMu）、mail（一个邮箱）、beacon（一装置一卡）声明 `exclusive: true`，
  同一时刻只绑一个角色（绑定写 char.json）。一期不做共享——游戏归谁、别人就开不了。

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

## M3-0 · Cass 的 Ombre 升级（搬家紧前执行）

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

退役切换日 checklist：
- [ ] mianmian 后端 `launchctl unload com.mianmian.backend`（至少停 wake）
- [ ] **Ombre 18001 容器保持常驻**（已是 cassette 的依赖）
- [ ] Desktop ombre 挂载已摘（M3-0 做过则勾掉）
- [ ] mianmian app 手机上留作只读或删除，随意

## 顺序与风险

顺序：M0（当天可上）→ M1 → M2（M1/M2 可并行，靠默认角色兼容层解耦）→
M3-0 → M3 演练（先导测试角色验格式）→ 正式搬家 + 退役。

盯着的点：
- 两角色各聊各的 = 并发 claude -p，订阅限流没数据，先跑着观察。
- Cass persona 里没提但 mianmian 世界观里有的东西（比如他「切 code 模式读代码」的纪律
  引用的是 mianmian 的 code_start）——搬后 cassette 的 codemode 插件同名能力在，口径基本无缝，
  但演练时确认一遍他调的工具名都存在。
