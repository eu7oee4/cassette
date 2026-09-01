# Plan：SDK 原生形状——一条会话，只读直放，写类弹卡

> 2026-09-01 晚定稿（机主与本轮对话逐条拍板），**全部未动工**。
> 这份 plan 独立成立，不需要读 PLAN_sdk / PLAN_proposals 的任何旧节。
>
> **写给施工者的三条规矩**：
> 1. 正文（§0–§7）只写新形状。它就是完整的设计，不是对旧版的补丁。
> 2. 旧世界只出现在两个地方：§8 改判登记（哪些旧定稿被推翻）、§9 拆迁清单（哪些代码要删）。
> 3. 施工时撞见正文没提的旧机制，**默认它属于拆迁清单**——别补回来、别写兼容层、
>    别在新代码里留「这里我们不做 X」的注释。新形状里不存在的东西，就让它不存在。
>    拿不准的列出来问，别自作主张往回接。

---

## 0. 一句话

模型收到一条 user 消息，决定调手上的工具：**只读的直接放行，动东西的弹卡给机主**——
批了，那次调用原地继续；拒了，模型这轮接着说话。
没有模式、没有场、没有归属、没有状态机、没有转场。

---

## 1. 权限：全走 SDK 原生（claude-agent-sdk 0.2.148 实读）

### 1.1 直放面 = `allowed_tools`

进白名单的工具自动放行，这是 SDK 自带的。名单内容：

- `READONLY_BUILTINS`（Read / Grep / Glob）——常驻所有轮次，想核实什么随手翻。
- 所有挂载的 MCP 面（插件 / 宠物 / skills / Ombre / game），照 `build_options` 现有拼法。

**写类四件（Edit / Write / NotebookEdit / Bash）不进这份名单。** 不进名单的调用，
CLI 权限规则判成 `ask`，路由到下面的回调（`types.py:2113`）。

### 1.2 审批面 = `can_use_tool`

```
写类调用发生
  → CLI 判 ask → can_use_tool(tool, input, ctx) 被调，这次调用原地挂起
  → 后端登记一张待批单（内存 asyncio.Future，不落盘）
      + Bark 推送 + app 卡片（§6）
  → 机主在卡上拍板 → REST /permits/decide → future 置值
      批准 → 返回 PermissionResultAllow → 工具原地执行，同一轮继续跑
      拒绝 → 返回 PermissionResultDeny(message=理由) → 模型收到理由，这轮接着说话
  → 超时 → Deny("等了 N 分钟没人批")，同上
```

要点：

- **被批的就是挂起的那次调用本身。** 参数在 `input` 里原样摆着，机主看到的
  就是要跑的那条东西。批准即执行，不存在「批完再重发」这个环节。
- `ctx` 自带 `title` / `display_name` / `description`（SDK 生成的整句提示文案，
  `types.py:225-233`）——卡片文案直接用它当底稿；`tool_use_id` 当单号。
- 待批单是**内存态**。后端重启即作废（回调随进程一起消失），模型下次要做再调一次
  ——这是特性：裁决没有持久状态可管，也就没有过期 / 悬单 / 状态迁移要维护。
- `PermissionResultDeny(interrupt=True)` 可以直接掐断这一轮，留给「机主想立刻叫停」
  的场景，常规拒绝不用。

### 1.3 超时：回调里自己实现

`asyncio.wait_for(future, timeout)`，到点自动 Deny。时长分轮来源配置：
聊天轮机主大概率在场，可以给长些；wake 轮机主多半不在，快拒，
模型这轮放下、以后想做再申请。具体秒数 §10 待拍。

### 1.4 PreToolUse hook 留下的三件事

hook 先于权限判定跑，继续管三件与审批无关的事：

1. **路径闸**：Read/Grep/Glob 和 Edit/Write 都过（限仓根 + 黑名单：凭据、
   别的角色的 state 房间）。Bash 没有路径参数，闸对它放空——机主看得到命令原文，
   拍板本身就是闸。
2. **wake 禁用面**：wake 轮只放 `wake_tools` 表里的（现行口径不动）。
3. **game 泵门**：`mcp__game__*` 只在泵开着时放行（现行口径不动）。

hook deny 的调用到不了审批；hook 放过的写类（返回 `{}`）落进 `ask` → 回调。
写类在 hook 里**不做任何别的判断**。

### 1.5 安全根

批准只能从后端 REST 来（app 卡片调）。邮件 / 论坛 / 网页里的注入文本够得到模型、
够不到那个 REST——外面的话最多怂恿他发起调用，弹出来的卡还是在机主指头底下。

### 1.6 可选二期：「这阵都放行」

`PermissionResultAllow.updated_permissions` 支持 session 级追加放行规则——卡片上
可以放第三个按钮，机主选了就这条 session 后续同类调用免弹。**一期不做**（§10），
机制在这儿记一笔：真做也是同一扇门上的一个按钮，不是另一扇门。

---

## 2. 并发：两个角色同时干活

谁都能申请写。两边同时想动手，机主手机上是两张**署名的**卡——批谁、先批谁、
批不批，都在机主指头上：**机主本人就是串行化点**，每一次写都过一次人手。

剩下的冲突面两块自愈、一块靠纪律：

- **同文件并发 Edit**：`old_string` 精确匹配，文件被对方改过就当场失败报错，
  模型重读重试。响亮失败，不会悄悄写坏。
- **git 同时操作**：`index.lock` 撞上也是响亮失败。
- **交错暂存**（A `git add -A` 把 B 改了一半的文件卷进自己的 commit）是唯一
  安静的坑，用纪律堵：**提交只 add 自己动过的文件，永远不用 `-A`**（写进 §3 的纪律文件）。

---

## 3. 干活纪律：一份文件，进系统提示

- **文件**：`server/code_addendum_chat.md`（机主手写，仓里 gitignore 与否照现状）。
- **位置**：`build_options` 拼系统提示时渲染进去，紧挨着工具可用性那段文字。
  两个角色**共用同一份**（纪律是工地规矩不是人格；角色差异 persona 已经管了），
  `{{AGENT_NAME}}` / `{{USER_NAME}}` 占位照 persona 的渲染路走。
- **付费形状**：进前缀缓存，一条 session 付一次。改文件=系统提示变=判脏触发重铸，
  纪律不常改，可接受。
- **起步内容**（机主的文件，这里只列本 plan 新增该写的）：
  - 提交只 add 自己动过的文件，不用 `-A`（§2）。
  - capsule 收场约定：干完一件事，把结论写成「◆ 结论 ← 出处」（§4）。

---

## 4. 留痕：账归账，上下文归上下文

- **行为账**：`_ToolTrace` 照现状逐条落（事件账 + 行为账，`ret` 回执列已在）。
  每条写类调用就是账上一行，一行自带机主批准这个事实（没批的根本执行不到）。
- **capsule**：他干完一件事自己写「◆ 结论 ← 出处」进事件账（`chat_loop.py:472`，
  这是结论跨重铸过桥的**唯一结构化通道**，§5）。
- 账是给门、给事后查证、给 Stop hook 那条防线用的；**不注回上下文**。

---

## 5. 重铸：结论过桥，证据不过桥

参考架构（施工 N4 动手前先去读一遍原文）：

> **https://github.com/dankefox/swap-tutorial** （「精炼续窗」——四层架构、
> 过滤逻辑、生产四保险都出自它，本节只摘了对得上号的部分）

它的四层与本仓现有件一一对应：

| swap-tutorial | cassette 现有件 |
|---|---|
| Durable 层（长期身份/关系/事实） | 消息库（真相源）+ persona + Ombre 记忆 |
| Startup 层（当前状态合成） | 系统提示（persona+菜单+纪律）+ 开局见闻快照（`_seen_block` since_ts=0） |
| Bridge 层（干净近尾巴） | 重铸出的 JSONL |
| Evidence 层（旧账冷存档） | 事件账 / 行为账 / 旧 transcript 归档 |

重铸规则五条：

1. **边界外的 `tool_use` / `tool_result` 成对丢弃。** 他的 text 原样过桥——
   他说过的话本来就带着他的结论。forge 伪造 JSONL 无校验（真机已验），
   成对丢即可，别留半截。
2. **近尾巴 N 轮逐字保留，含工具块**——干到一半的活不失忆。N 待拍（§10），
   教程的 ~12 轮 / ~48k token 作参考起点。尾巴边界切在轮界上。
3. **结构化结论走 capsule 进事件账**（§4），证据原文在账和归档里永存，
   事后查证走账，不占上下文。
4. **触发与判脏照旧**：主动铸=token / 图片两轴各软硬档；判脏=发送前比对。
5. **只在轮间铸。** 审批挂起时轮还开着，所以「挂起时不铸」自动成立，不用加规则。

「他声称做过但工具块被丢了」不归重铸管——那是行为账 + Stop hook 防线的事，
账在 Evidence 层不丢。

**forge 操作纪律四保险**（抄教程的生产建议，forge 今天只有判脏没有回滚）：
铸前真备份（`cp -a`，避硬链）、dry-run 验源、原子写、last-good 回滚。

---

## 6. iOS：一套卡

- **权限卡**：卡上是 `ctx.title` 那句整话 + 参数原文（Bash 显示命令原文，
  Edit 显示文件和改动），两个按钮：批准（二次确认可免——挂起有超时兜底，
  误触的代价是一次可撤销的写）/ 拒绝（可附一句理由，进 Deny 的 message）。
- **入口**：聊天页最新位置插卡 + Bark 推送。挂起是分钟级的，不需要独立管理页
  ——超时自动拒，没有「悬着的单」要收拾。
- **REST**：`/permits/pending` GET（app 回前台对齐用）、`/permits/decide` POST。
- **上电硬前置**：卡没做出来之前，挂起审批没人能批，只会超时拒。Bark 只能提醒
  不能拍板。所以后端通路（§7 N1）可以先行合入，**拨闸要等卡**。
- proposals（对外动作那套，另一份 plan）将来复用同一张卡的形状，此处不展开。

---

## 7. 施工顺序与拨闸

- **N0 · 纪律进系统提示**（§3）：独立可先行，只动 `build_options` 拼串。
- **N1 · 后端审批通路**（§1）：`allowed_tools` 摘掉写类四件；`can_use_tool` 回调
  + 内存待批单 + 超时；REST 两个端点 + Bark。拨闸复用 `WRITE_TOOLS_ENABLED`
  （语义不变：拨开=写类 schema 挂载 + 审批通路生效）。**先写测试**：挂起 / 批准 /
  拒绝 / 超时 / 后端重启作废，五条都要有。
- **N2 · iOS 权限卡**（§6）：拨闸的硬前置。
- **N3 · 拆迁**（§9）：分两批。第一批（门侧，随 N1 同一个 PR）：`code_permits`
  整件、互斥检查、每轮 addendum 注入。第二批（code_bridge 的 code 半边）：
  **先核 game 线的依赖**（§9 两处 ⚠️）再动。
- **N4 · 重铸新口径**（§5）：forge 改造 + 四保险，与 N1–N3 无依赖可并行。
- **真机验收清单**：§10 待验五条。

---

## 8. 改判登记（本 plan 推翻/顶替了哪些旧定稿）

施工中看到旧文档写着下面这些，以本 plan 为准，别被带回去：

| 旧定稿 | 出处 | 现口径 |
|---|---|---|
| code 写权限走 `code_permits` 带外门，一场一批、收摊即失效 | PLAN_sdk PR14-c/d | 一命令一卡，SDK 原生挂起裁决，无场无收摊（§1） |
| 「code_permits 和 proposals 两张门别合并」 | PLAN_proposals §6 | code 门整个退役；proposals 收窄回对外动作（发信/发帖类），不接管代码 |
| 电脑互斥：tmux 归属角色之外连申请都不递 | PLAN_sdk PR14-d | 退役。谁都能申请，机主是串行化点（§2） |
| 「走 hook 不走 can_use_tool——allowed_tools 会遮蔽回调」 | chat_loop.py:201 注释 | 两者组合用：遮蔽只影响**进了**白名单的工具，写类不进名单就不被遮蔽（§1.4） |
| 「Bash 命令级细分是走不稳的路」 | PLAN_sdk PR14-c | 不再需要细分：机主批的就是那条命令原文本身（§1.2） |
| git push 要不要单独一张 proposal（未决） | PLAN_proposals §8.2 | 自动消解：push 天然一卡一批 |
| addendum 一场一注 / 批下来那轮注入干活纪律 | PLAN_sdk PR14-d | 纪律常驻系统提示（§3） |
| 活动段折叠 / `_fold_trace_lines` 折叠框 | PLAN_sdk PR11 | 退役。证据不过桥，重铸边界外工具块成对丢（§5） |
| code 会话转场口径「只有结论过桥、证据不过桥」 | PLAN_codemode 末节 | 升格为重铸总则（§5）；转场本身消失，口径反而更纯 |
| 老 code 路的场文本 `code_addendum.md`、措辞补丁、「聊天里说的判断多半没验证过」类提示 | code_bridge / app.py / chat_loop 散布 | 全删不留替代：证据就在他自己的历史里，查过的带着工具块，模型自己分辨得出 |
| Stop hook =「说了没做」的主防线（设计另立待做） | PLAN_proposals §0/§10 | **后置**（机主 09-01 晚拍板）：native 落地后先观察犯错频率再议——那类病是旧架构（证据关在别的会话里、落账半残）养出来的，原生生态下先看它还犯不犯 |
| 行为账/行为清单注回上下文 | PLAN_proposals §5 曾设同款注入位 | 不做（§4 口径：账照落，不注回上下文）。proposals 自己的待办纸条归那份 plan，与本表无关 |

---

## 9. 拆迁清单（行号为 2026-09-01 快照，动手前 grep 复核）

**第一批（随 N1）**：

- `server/code_permits.py` 整文件 + `tests/test_code_permits.py`
- `app.py`：`/code/permit` GET（~1475）、`/code/permit/decide`（~1485）、
  收摊端点（~1496–1501）
- `chat_loop.py` 写类门分支整段（225–253）：互斥检查（234–242）+
  `code_permits.request/active`（243–253）——被 §1 的新路顶替
- `chat_loop.py`：`active_seg`（461–464）、loop 退出 revoke（1442–1444）、
  `_code_addendum_block`（849）及调用点（1326）
- code 段账的开/关（批准开段、收摊关段那对钩子）——留痕改为行为账逐条（§4）

**第二批（先核依赖再动）**：

- `code_bridge.py` 的 **code profile 半边**：start/stop 杀旧起新、context_text
  开场注入、session.json 钉归属、`_build_system` 读 `code_addendum.md`（274）
  - ⚠️ **game profile 半边要先单独核**：story_discipline、`_wait_mcp_ready` 那条路
    是否已被 unified 泵完全顶替，没顶替就留
- `app.py`：`/code/start`、`/code/stop`、`/code/send`
  - ⚠️ **`/code/send` + PumpNote shim（~1598）是 app 游戏态聊天框的现行投递口**
    （08-31 插话被堵的修法），拆它必须和 app 侧改投递口同一批上
- `app.py:1420` 那句提示及同类
- `owner_of("tmux")` 各兜底（app.py / wake.py / cohabit.py / code_bridge.py 十余处）：
  **逐处分类**——服务老 code 路的删，服务 game 归属锚（`_game_capable`,
  chat_loop.py:297）的**留**（模拟器是真独占资源，game 互斥不动）
- `chat_loop.py` `_fold_trace_lines`（~759）及折叠框渲染（随 N4）
- 「电脑上的会话」插件本身**暂不下架**：game 归属锚和未拆的老路还靠它；
  第二批清完、game 锚另立之后再议

---

## 10. 待验 / 待拍

**真机验（拨闸前）**：

1. CLI 侧等 `can_use_tool` 裁决有没有隐藏超时（SDK 侧无；`query.py` 里 60s 那个
   是反方向的初始化请求）。它和 Claude Code 终端等按键是同一条管线，预期无限等，未实证。
2. 挂起期间该 session 的流式 / 插话表现（轮开着，新消息排队多久算可接受）。
3. `~/.claude/settings.json` 的 allow 规则会不会遮蔽回调（`types.py:1861` 明说
   settings 文件的 allow 也遮蔽）——chat session 子进程吃不吃用户全局 settings 要实测，
   必要时给空 `--settings` 隔离。
4. 写类四件的 schema token 实测（拨闸顺序照旧：只读已拨且已实测，写类实测完再拨）。
5. 并发写实测一次：两角色各批一条对同文件的 Edit，确认响亮失败路真响亮。

**待拍（机主）**：

1. 超时时长：聊天轮 / wake 轮各多少分钟。
2. 「这阵都放行」按钮一期不做——确认。
3. 重铸近尾巴 N 取多大（轮数或 token 预算）。
4. 批准按钮要不要二次确认（§6 倾向不要，挂起+可撤销兜着）。

---

## 11. 来源分级

**已核（2026-09-01 本轮实读）**：
- `claude_agent_sdk 0.2.148`：`can_use_tool` 挂起语义、`PermissionResult` 两形、
  `ToolPermissionContext` 的 title/display_name/tool_use_id、`ask` 路由
  （`types.py:113/200-259/2113`）、遮蔽警告与 settings 遮蔽（`types.py:1805-1893`）、
  控制请求超时仅在 SDK→CLI 方向（`_internal/query.py:599-643`）
- `chat_loop.py`：门（200–264）、`build_options` 工具拼装与 segs（368–389）、
  `_code_addendum_block`（849）、capsule（472）、PumpNote（88/1226/1486）
- `code_bridge.py`：`_build_system`（255–294）、start 的转场机器（343–369）
- `.env`：`READONLY_TOOLS=1` 已拨、`WRITE_TOOLS` 未拨（写门从未在生产拦过）
- iOS 全仓无 `permit`（权限 UI 零行，08-31 已核未变）

**网页（未复现）**：swap-tutorial 的四层架构与过滤逻辑（09-01 WebFetch 读的）。

**推断（标着，别当事实用）**：CLI 侧无限等裁决；挂起期间插话排队的具体表现；
game profile 对 code_bridge 的依赖面（第二批拆前必核）。
