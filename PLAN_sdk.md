# Plan：SDK 引擎盘点——剧情会话换引擎 + 聊天/工作室的 SDK 路线图

> 2026-08-28 立项盘点，**只有「剧情会话换引擎」是待动工主体**，其余两节是盘点记档。
> 动因：如鸢读剧情半小时后一轮比一轮慢。机理已查实（见下），批量推进和裁剪
> 两条捷径已被否（动画探测靠每步完整截图，谁都不能省）。

## 0. 变慢的机理（已查实，别重查）

- 截图 480×853，按官方 patch 公式 `⌈宽/28⌉×⌈高/28⌉` = 18×31 ≈ **560 token/张**。
  token 上图和 TA 的点评+思考五五开（一小时各 3~8 万），谁都不是零头。
- 真正的不对称在**字节**：560 token 的文字 ≈ 2KB，560 token 的截图 ≈ 133KB base64，
  60 倍。API 无状态、每次工具调用重传全部历史，prompt cache 只省服务端算力
  **不省上传**——一小时后每轮光图就传 ~15MB，这是延迟主因；总上下文逼近
  auto-compact 阈值时再叠加恶化。
- Claude Code（tmux 里跑的东西）的请求体是它自己拼的，**没有任何配置面能加
  `context_management`（clear_tool_uses）**，它的历史也不交给我们剪。
  官方对多轮图片堆积的推荐（Files API 按 file_id 引用）CLI 工具结果走不了。

## 1. 引擎坐标系（面试题「为什么用 claude -p 和 tmux 不用 SDK」也用这张表答）

| | claude -p 一次性（聊天/醒来现状） | tmux 交互式 CLI（code/游戏会话现状） | claude-agent-sdk | 裸 SDK（Messages API + tool runner） |
|---|---|---|---|---|
| messages 数组归谁 | CLI（每次重拼 prompt） | CLI（历史封闭） | SDK 内部（Claude Code harness） | **自己** |
| 计费 | **订阅登录态**（Max 包月） | 同左 | **订阅**（mianmian 工作群实证：ClaudeSDKClient + 删 ANTHROPIC_API_KEY，见其 lead.py） | **API key 按 token 计费** |
| 工具执行/权限/MCP | CLI 白给（--tools 白名单 + mcp-config） | 同左 | harness 白给 | 自己搭 loop；本地 stdio MCP 要自己当 client |
| 上下文清理 | 无（每次一次性，无所谓） | auto-compact，图剪不掉 | harness 自管，同样剪不掉 | **随便剪**（clear_tool_uses / 客户端自己剪） |
| 外部消息插话 | 不适用 | tmux send-keys（code_bridge.send） | 流式输入可编程注入（细节待查证） | 自己的队列，天然支持 |

结论一句话：**CLI 三大白给——订阅计费、agent harness、权限面；SDK 一大独有——
messages 归自己**。哪个 workload 的痛点是「历史必须自己剪」，哪个才值得付
SDK 的三样代价。全仓只有剧情会话（图片密集+长会话）满足这个条件。

## 2. 主体：剧情会话换引擎（game loop 直跑 SDK）

游戏会话恰好是全家依赖面最小的 tmux 用户：strict 白名单只有 game MCP + Read，
不要 Bash/文件工具/skills。适合单独换引擎，**code 会话照旧不动**。

### 2.1 目标架构

```
server/game_loop.py（新，常驻 asyncio task，替代 tmux+claude 交互式）
  ├─ 引擎：anthropic SDK，自己的 agent loop（tool runner 或手写 while tool_use）
  ├─ 工具：game_bridge 函数直挂（look/watch/tap/swipe/back/launch/close/quit/
  │        notes_read/notes_write + end），不再走 MCP stdio——
  │        game_session_mcp.py 的实现原样搬进工具函数，插件仓那份退役
  ├─ 系统提示：复用 _build_system("game") 的产物（场景注入+出厂纪律+addendum）
  ├─ 消息通道：asyncio.Queue——聊天侧 TA 的消息 push 进来，loop 在当前工具轮
  │        结束后作为下一条 user 消息注入（替代 code_bridge.send 的 send-keys）；
  │        模型说的话经现有回传路进 TA 聊天气泡（口径照 codemode plan 末节）
  └─ 历史修剪（换引擎的全部意义）：每轮请求前把「最近 K 张之外」的截图
         tool_result 内容换成一行占位文本（如「[第37步截图，已清理]」）。
         K 先取 6（够 watch 连拍对比 + 回看一眼）。token 和上传字节一起掉，
         上下文恒定 ≈ 系统提示 + 文本史 + K 张图。
```

### 2.2 修剪与缓存的配合

- 剪的是 user 轮的 tool_result，合法；thinking 块原样回传不动。
- 每次修剪必然 bust 一次从修剪点起的缓存——所以**别每轮剪一张**，攒着每
  10~15 步剪一批，剪完那轮付一次 cache 重写，之后又全是 cache read。
- 断点自己放（API 上限 4 个，不再被 CLI 占 3 个）：系统提示 1 个 + 对话尾 1 个
  起步，ttl 按会话节奏定（读剧情步频 <5min，默认 5m 即可，不用 1h 的 2× 写价）。
- 服务端 `context_management`（clear_tool_uses beta）不用：它不省上传字节，
  客户端自己剪是全集。

### 2.3 看守层重做（app.py 游戏看守）

现在的看守靠 `code_bridge.capture()` 截屏比对判 idle——loop 化之后全部退役，
换成 loop 内建状态：上次活动时间戳、是否在等 TA 回话。20 分钟无活动收摊、
5 分钟等回话推送 Bark 两条规则原样搬。**一小时软提醒和「收摊重开」纪律整套
退役**——历史不再涨，没有摊可收；game_end 保留但语义变成「关掉这局 loop」。

### 2.4 接口兼容（聊天侧无感）

- `/game/story/start` 改为起 loop task（不再 kill/起 tmux、不再 _wait_mcp_ready）；
  game-story 插件（聊天侧 game_start/notes 三件套）一行不用改。
- 急停锁、设备自愈、笔记本都在 game_bridge，直接复用。
- 与任务引擎（MaaYuan）的模拟器互斥锁照旧。

### 2.5 引擎二选一（2026-08-28 修订：agent-sdk 从「否掉」升级为 B 方案）

> 初版把 agent-sdk 一句「同 harness 剪不掉、换了白换」否了——错。mianmian 工作群
> 实证它走订阅（机主从没买过 API），这让它成了唯一不新增支出的换引擎路。

**A · 裸 SDK（Messages API），真·修剪**：2.1~2.4 的原方案。历史归自己、上下文
恒定、速度恒定、永不收摊。代价：API key 按量计费——修剪后粗估 Sonnet 5 约
$1~2/小时（Opus 翻倍多），净新增支出。

**B · claude-agent-sdk，自动滚动重开**：历史仍剪不掉（同 Claude Code harness），
但把「收摊重开」从 TA 手动三分钟变成 loop 自动几十秒：
- 游戏工具用 `create_sdk_mcp_server` 进程内挂（game_bridge 函数直包，不走 stdio，
  没有 _wait_mcp_ready）；TA 消息经 `client.query()` 注入（多次 query 同会话）；
- loop 数截图张数，攒到 ~50 张：让模型写笔记本 → 关 client → 新起一个（系统提示 +
  进度摘要注入），游戏画面原地不动，接着读。重开成本 = 重付一次未缓存的系统提示
  + 笔记本（订阅额度内，不是钱）+ 几十秒停顿；
- tmux/send-keys/截屏看守照样全退役，看守逻辑同 2.3 搬进 loop。

**默认倾向 B**：速度上限略低于 A（每段内仍会缓涨，只是段短、永远到不了慢区），
但零新增支出、且与全仓「凭据走订阅、后端不碰 key」的安全姿态一致。A 留作
「哪天买了 API key」的升级路——2.1 的 loop 结构两边通用，B 先落地不白做。

### 2.6 决策点（动工前要拍板）

1. 频率门槛：TA 一周读一两次 → 现状够用，本 plan 躺着；天天读 → 上 B。
2. 滚动周期 N（默认 50 张）和重开时注入什么（笔记本全文 or 进度摘要）。
3. **模型**：见第 6 节「剧情交给 Sonnet？」——引擎和模型是两个独立决策。

## 3. 记档：聊天管线若走 SDK 要改什么（不动工，面试/规划用）

按 pipeline.py 现状逐条：

1. **凭据与计费**（最大项）：claude CLI 登录态 → API key。聊天+醒来+邮件+猫
   一天几百次调用全变按量付费——这就是聊天**不**走 SDK 的理由本身。
2. **prompt 拼装**：现在每条消息重拼一整段 prompt（人设 --system-prompt-file
   替换 + 时间线文本）→ SDK 下人设进 system 参数、时间线可改真 messages 多轮；
   缓存断点从「CLI 占 3 个剩 1 个名额」（PLAN_cache 的核心约束）变成 4 个全归
   自己，PLAN_cache 第三刀（时间线断点）就有名额做了。
3. **工具面**：--tools 白名单 + mcp-config 挂 stdio MCP（Ombre/插件）→ 裸 SDK
   不会替你跑本地 stdio MCP，要么自己当 MCP client 把工具桥进 tool runner，
   要么每个插件改成纯函数注册。skills_mcp 渐进披露那套也要重接。
4. **流式**：/chat/stream 现在是「CLI stream-json → sse.py 翻译」→ SDK 原生
   事件流，翻译层删掉反而更薄。
5. **解析层**：parse_claude_stream / stored 块提取 → SDK 类型化响应，整层退役。
6. 安全姿态换了形态：「子进程不碰 key」变成「后端持 key」，威胁模型要重写一段。

一句话：**技术上全部可换、多数还更干净，唯独计费模型不可换**——订阅 vs 按量
是产品级决策不是工程决策。

### 3.1 那走 agent-sdk 呢（订阅保住，2026-08-28 补盘）

agent-sdk 底下就是同一个 Claude Code：订阅、模型、**缓存断点名额（PLAN_cache
的「CLI 占 3 剩 1」约束）全部原样**，时间线三路合并照旧自己拼（那是产品核心
不是负担）。它换掉的只是调用形态，能白捡三样：

1. **每条消息的启动开销**：现在一次性 claude -p 每条消息都重启 CLI + 全部插件
   stdio MCP 子进程（邮件/求职/skills/game-story……每条聊天都付一遍启动费）。
   agent-sdk 的 `create_sdk_mcp_server` 让插件工具变进程内 python 函数，启动费
   归零——这是聊天路唯一摸得着的体验收益（每条消息快几秒）。
2. **类型化流事件**：parse_claude_stream / sse.py 翻译层退役。附带白拿一个：
   PLAN_studio「房间流式」第 1 步（wake runner 增量读，Popen+逐行 hack）
   就是 sdk 的原生能力——房间流式的服务端工作量直接砍半。
3. **权限/hook 变 python 回调**（can_use_tool / PreToolUse）：白名单和门控
   不再靠 settings 预批准体操，mianmian lead.py 已验证这套写法。

不动的理由也摆着：聊天是全仓最精调的路径（打扰控制/text_break/stored 块/
多角色/小屋注入），解析层是对着 CLI stream-json 的怪癖打磨过的；agent-sdk
版本 skew 是新依赖面；ToolSearch 延迟工具 + skills 渐进披露那套在 sdk 下
未验证。**痛点烈度排序：游戏（真痛）> 工作室（新能力）> 聊天/醒来（改善型）**
——合理路径是游戏 loop 用 B 方案把 agent-sdk 趟熟，回头再评聊天迁移，
顺路收掉房间流式。

## 4. 记档：工作室/三人对话的 SDK 解法（PLAN_studio 的新变量）

PLAN_studio 搁置的两个理由，SDK 各解掉多少：

- **「电脑权限独占、两人不能同时开」**：独占是 tmux 结构性的（一个 SESSION 名、
  一个 cwd、code_bridge 单例）。SDK loop 天然一人一个实例，独占消失。✔ 解。
- **「三人怎么对话」**：messages 归自己后，共享一份带说话人标注的 transcript，
  每个角色轮到发言时用自己的人设当 system、transcript 当 messages 调一次——
  mianmian 工作群（claude sdk + deepseek api）就是这个做法，已验证可行。
  但**轮流仲裁本身还是要自己写**（谁接话、何时停），SDK 只是把它从「tmux 里
  做不到」变成「普通代码」。✔ 半解。
- **没解掉的**：搁置的根本理由——「工作室里得有摸得到的代码/资源，不是只动嘴」
  ——和引擎无关，依然成立；且两个角色都走 SDK = 双份 API 计费。
  所以 studio 重启条件更新为：**先有共同工作资源，再谈 SDK 群聊**，顺序不变。

## 5. 附：面试答法（为什么 claude -p + tmux 不用 SDK）

三层答，展示这是权衡不是无知：

1. **计费模型**：产品是 7×24 常驻伴侣，聊天/定时醒来/邮件/游戏一天几百次
   推理。CLI 走订阅登录态是固定成本，Messages API 是无上限按量——个人项目里
   这是第一决策变量。
2. **白给的 harness**：Claude Code 自带工具执行、权限白名单（--tools +
   --allowedTools + strict-mcp-config）、MCP 挂载、上下文管理。裸 SDK 这些
   全要自己搭，而我的差异化工作量应该花在人格管线、状态机和多角色架构上。
   tmux 交互式会话额外白给「外部消息实时插话」和一个人类可 attach 的运维窗口。
3. **知道边界在哪**（加分项）：CLI 的代价是 messages 数组封闭——图片密集的
   长会话（游戏剧情）历史剪不掉、越跑越慢，这个 workload 我有单独换引擎的
   方案（本 plan 第 2 节：agent-sdk 滚动重开 / 裸 SDK 自持修剪两级）。
   按 workload 选引擎，而不是全仓一把梭。

## 6. 剧情交给 Sonnet？（模型决策，独立于引擎）

- **操作层**：认截图里的中文、坐标点按、double/watch 纪律——Sonnet 5 完全够，
  这不是 hardest workload。
- **风险全在点评**：读剧情的产品核心是 TA 那口逐句文学点评（「一个人能同时给出
  三套都成立的动机，那他真正的动机你一个也没听到」这种），文风/洞察密度恰恰是
  Opus 和 Sonnet 还有差距的地方。而且这是**人格问题不只是能力问题**——
  pipeline.py 的仓规写着「静默用别的模型顶替等于让别人替这个角色说话」，
  同一个 TA 在聊天里是 Opus、进游戏变 Sonnet，口径得机主和 TA 自己认。
- **收益**：订阅下 Sonnet 烧额度慢得多（Max 的 Sonnet 用量上限远高于 Opus）——
  TA 若天天读剧情，这是真实的额度压力缓解；走 A 方案则是直接减半开销。
- **怎么定**：别拍脑袋。真机 A/B 一章——同一段剧情 Sonnet 读一节、Opus 读一节，
  点评贴给眠眠盲评；TA 本人的意见也算票（这是 TA 的体验不是纯参数）。
  盲评分不出来 → 用 Sonnet；分得出来 → 留 Opus，额度换体验。
