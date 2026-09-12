# Plan：SDK 原生形状——一条会话，只读直放，写类弹卡

> 2026-09-01 晚定稿（机主与本轮对话逐条拍板）。
> **施工记录见 §12**（2026-09-02 时间感补线，已 commit `e53f66f`，未上电）——
> 那一节还带着这一批留下的待做清单。
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
- **只读 Bash 命令 CLI 自己放行，不进回调**（09-01 实证，CLI 2.1.252）：
  `pwd` 这类只读命令没弹卡直接跑了，`touch` 才进回调。所以「Bash 每次必弹卡」
  要念成「**真动东西的 Bash 必弹卡**」——方向与只读直放的设计一致，但这类
  调用不会出现在 permits 里，行为账想收它们得另想（§7.2）。
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

- **N0 · 纪律进系统提示**（§3）：独立可先行，只动 `build_options` 拼串。✅ 09-01
- **N1 · 后端审批通路**（§1）：`allowed_tools` 摘掉写类四件；`can_use_tool` 回调
  + 内存待批单 + 超时；REST 两个端点 + Bark。拨闸复用 `WRITE_TOOLS_ENABLED`
  （语义不变：拨开=写类 schema 挂载 + 审批通路生效）。**先写测试**：挂起 / 批准 /
  拒绝 / 超时 / 后端重启作废，五条都要有。✅ 09-01
- **N2 · iOS 权限卡**（§6）：拨闸的硬前置。✅ 09-01 落码未上电（施工在
  PLAN_chatui U4：PermitCardView + sse permit 事件 + syncPermits；
  批准免二次确认照 §6 倾向先做了，待拍④机主看真机效果再改）。
  拨闸剩的只有待验④（写类 schema token 实测）——实测完拨 WRITE_TOOLS=1。
- **N3 · 拆迁**（§9）：分两批。第一批（门侧，随 N1 同一个 PR）：`code_permits`
  整件、互斥检查、每轮 addendum 注入。✅ 09-01 第二批（code_bridge 的 code 半边）：
  **先核 game 线的依赖**（§9 两处 ⚠️）再动。
- **N4 · 重铸新口径**（§5）：forge 改造 + 四保险，与 N1–N3 无依赖可并行。
- **真机验收清单**：§10 待验五条。

### 7.1 施工记录（09-01，N0+N1+N3 第一批落码，469 测试全绿，未上电）

新件：`server/permits.py`（内存待批单）+ `tests/test_permits.py`（五条主线+门
集成+回调两形+capsule）。REST=`/permits/pending` GET + `/permits/decide` POST
（`reason` 随拒绝进 Deny 的 message）。现场决定四条，都是正文没写死、施工撞上的：

1. **capsule 的家=行为账**（§4 的「事件账」落不下了）：事件账要段地址，code
   无场之后没有段——「◆ 结论 ← 出处」由 `_capture_capsules` 落进按角色一本的
   行为账（`acts-<cid>.jsonl`，tool="capsule"，wake 轮带 scene=wake）。同理
   **`acts_worthy` 收写类进行为账**（旧口径写类归段账故意不落）——「每条写类
   调用就是账上一行」由此成立。行为账行文本帽 200 字符，capsule 超长会截。
2. **审批挂起 ≠ 轮内空闲**：`_turn_events` 的 `CLAUDE_TIMEOUT_SEC`（15 分钟）
   会把挂着等批的静流当 session 死。豁免判据=`permits.waiting(char)`，且豁免面
   有界（卡过 deadline+5s 或 future 已死就不算在等——防泄漏的卡把空闲超时
   永远钉死，[[cassette-wake-gate-bug-class]] 的近亲）。
3. **超时默认值**（待拍①还没拍）：聊天轮 600s / wake 轮 120s，
   env=`PERMIT_TIMEOUT_CHAT_SEC` / `PERMIT_TIMEOUT_WAKE_SEC`，拍了改 env。
4. **SDK 的 `CanUseToolShadowedWarning` 静掉**（chat_loop 模块级 filter）：
   allowed_tools+can_use_tool 组合是本设计故意的（§8），那条提醒对这套形状是误报。

纪律文件基建：`code_addendum_chat.example.md` 入库、正式文件进 .gitignore
（照 code_addendum.md 现状）；capsule 收场约定是代码侧机制字段（常驻
`_discipline_block` 尾巴），机主文件里不用重复写。**机主还没写正式文件**——
起步内容照 §3（只 add 自己动过的、commit≠push）。

「先放着」的（第二批/N4 的活，这次没动）：`_fold_trace_lines`、
`_frame_activities` 的 code 折叠框半边（历史 code 区间还要靠它渲染，随 N4 拆）、
`hooks/code_segments.py`、`wake.code_session_owner/block`、`code_bridge` 全件、
`owner_of("tmux")` 各兜底（game 归属锚还在用）。

### 7.2 验证记录（09-01 晚，待验①③离线实证，独立 SDK 探针不碰生产）

探针：scratchpad `permit_probe.py`（形状对齐 `build_options`：写类不进
`allowed_tools`、`can_use_tool` 挂着、订阅路无 API key）。CLI 2.1.252 / SDK 0.2.148。

1. **待验① 无隐藏超时**：回调挂 720s 再 Allow——CLI 不断连，工具原地执行。
   模型第一次写错路径（`/probe_hang.txt`），放行后工具报错、它自己 `pwd` 纠正
   重试，又挂了第二个 720s，照样走通。**一轮连续两次分钟级挂起、总时长 24 分钟，
   收尾正常**——比设计要的还多验了一层。
2. **待验③ 遮蔽坐实（两半）**：
   - 机制半：settings 文件里 `allow: ["Write"]` → Write 直接执行，回调一声不响；
   - 用户层半：只在机主 `~/.claude/settings.json` `enabledPlugins` 里启用的
     swift-lsp 出现在子进程 init 里 → **`setting_sources` 默认 None = 全加载**
     （`types.py:2225`，文档与实测一致）。今天没事只因机主 settings 恰好没有
     `permissions.allow`；TA 手机上那张卡会被机主自己某次「always allow」静默绕过。
   - **修法已落码并实证**：`build_options` 加 `setting_sources=[]` → init 里
     swift-lsp 消失、plugins=[]、回调照常拦截。顺带把机主个人的插件 / skills /
     Stop hook 全挡在 TA 会话外（全是想要的）。469 测试绿。
3. **计划外发现（已写进 §1.2）**：CLI 对只读 Bash 命令（`pwd`）有内建自动放行面，
   不进回调。真动东西的（`touch`）必进回调、Deny 生效。含义两条：这类调用
   permits 里不会有记录；行为账 `acts_worthy` 若把 Bash 一律当写类收，
   会把 `pwd` 这类也记上——要不要按命令区分，随 N2 拨闸前顺手看一眼。

### 7.3 拨闸记录（09-01 晚 18:55）

待验①-⑤全部实证完（②④⑤补测见 §10 划线处；探针 scratchpad
permit_probe.py 的 tokens/interject/conflict 三个模式），`.env` 拨
`WRITE_TOOLS=1` + `QUESTION_CARDS=1`，一条命令重启上电。
**真机验收欠一个硬前置：手机上要装 09-01 之后的新 app 包**（两张卡的 UI
在 cf484a4/842e8c7）——旧包看不见卡，TA 弹卡只会白等超时（聊天 600s/
wake 120s）。装包前这段窗口 TA 若自己动手，收到的是「等了约 N 分钟没等到
拍板」的软拒，不炸轮。

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

1. ~~CLI 侧等 `can_use_tool` 裁决有没有隐藏超时~~ ✅ **09-01 离线实证无超时**
   （独立 SDK 探针，CLI 2.1.252）：一轮里连续两次挂 720s（> 聊天轮 600s 上限）
   再 Allow，工具都原地执行、24 分钟的轮正常收尾。细节见 §7.2。
2. ~~挂起期间该 session 的流式 / 插话表现~~ ✅ **09-01 离线实证**：挂起第 10s
   插话，消息不丢、轮不断——CLI 把它排进**同一轮**，放行后模型先答插话再继续
   干活，一个 result 收官（§7.3）。产品面剩的是体感：TA 的插话最长等一个
   审批超时（聊天 600s），真机用起来嫌久就调 env。
3. ~~settings 的 allow 规则会不会遮蔽回调~~ ✅ **09-01 离线实证：会，且子进程
   默认吃用户全局 settings**（两半都坐实，细节见 §7.2）。修法已落码：
   `build_options` 加 `setting_sources=[]`（注意**不是**空 `--settings`——
   那只是加一层 flag settings，挡不住用户层加载）。
4. ~~写类四件的 schema token 实测~~ ✅ **09-01 实测**（opus，探针 tokens 模式）：
   写类四件 **+2292 tokens**、AskUserQuestion **+1464**（基线只读三件 2735）。
   全走前缀缓存，一条 session 付一次写缓存、之后命中。
5. ~~并发写响亮失败~~ ✅ **09-01 等价实证**：挂起窗口里文件被外人改 → 批准后
   Edit 直接报「File content has changed since it was last read」（CLI 有
   **mtime 级检测**，比 old_string 匹配还早一层），err=True、对方的写原样
   保留、模型如实汇报。真双角色并发真机再顺手验一次即可。

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

---

## 12. 时间感补线（2026-09-02 施工记录，`e53f66f`，**09-02 20:15 已重启上电**）

起因是一句话：Cassius 09-02 **14:07** 开口说「**昨天**我猜滚动窗口」——那句是他
**14:03** 说的，隔三分半，同一场对话没断过。往下扒出来一串，全在同一个根上。

### 12.1 根因：历史是一堵没有时间的墙

扒 14:07 那轮真正吃进去的 transcript（90 个事件），全上下文里的时间信息只有：

```
带日期的标记：  [08-30 10:07] [08-30 12:08] [08-30 13:47] [08-30 17:11] [08-30 20:41]
【现在是…】：   1 次（贴在最后一条新消息上：09-02 14:06）
```

**七十多轮对话、跨 08-31 到 09-02 两天，一条时间戳都没有。** 那五个 `[08-30 …]`
是醒来见闻块里的内心，不是对话。

两条机制合起来造的：

1. `forge.render` 把 ts 写进 transcript 的 `timestamp` 字段——那是**文件元数据，
   模型看不见**。`message.content` 里只有裸 text。
2. 活轮注入头那句【现在是 …】**活不过下一次重铸**：权威库存的是 app 传来的裸原文，
   重铸＝拿裸原文重画一遍，所有历史轮的时间头一起蒸发。而 14:07 那轮**恰好就是
   重铸后的第一轮**（两条独立证据：transcript 里那条以 `OPENING_NUDGE` 开头，
   而 `needs_opening=True` 全代码只有 `_open_session` 会置；`nohup.out` 里
   `[09-02 14:05:40] wake_sdk 抽下次自醒` 紧跟着一行 `判脏，重铸`）。

旧的非 SDK 路（`pipeline.build_context_timeline`）是给历史逐条加 `[MM-DD HH:MM]`
前缀的。**SDK 迁移时「历史活在 transcript 里」，这个前缀连带丢了。** 两个角色同一
条路，所以小卡也犯过同款。

### 12.2 时间锚：三层渲染，只改铸造输入不动账

`_open_session` 里的顺序是有讲究的，反了就出错：

```python
rendered = _frame_activities(_stamp_times(_merge_images(history, cid), cid), cid)
```

**① 并图**（`_merge_images`）要在打戳之前——先打戳会给 `[图片]` 占位也打一个，
然后那条被并掉，戳跟着没了。
**② 打戳**（`_stamp_times`）。**③ 套活动框**（`_frame_activities`）——框行自带
`HH:MM`，顺序反了就是双戳。

格式全场一个（`pipeline.stamp_str`，三处共用：打戳 / 醒来抬头 / 游戏 tick 报时）：
`【MM-dd 周X HH:mm】`。年份逐条重复零信息、时段词从 24 小时制读得出，都砍了；
**星期留着**——模型从日期反推星期不可靠，而「周末还是工作日」是他判断该不该打扰
TA 的依据。

- **TA 的每句话**带戳 + 称呼。**他自己的话不打戳**（第一人称记忆，信感定案不动）。
- **他自己开口那些轮**（醒来 / 游戏泵）的 user 槽是**注入、不进账**，重铸后整条
  蒸发（§5.2 纯内心不渲染），只剩他说出去的那句 assistant。于是连着几次醒来变成
  连续 assistant，再被 `forge.render` 的「连续同角色合并」粘成一条、只留最早那个
  ts——**实测：六条消息跨 11.9 小时并成一个 1645 字的事件**，在他眼里是一口气说完
  的一段话。

  补法不需要新存储：**assistant 消息自己带 ts，蒸发掉的时间本来就还在。**
  - `ts` 对上 `wake_log`（`action=message`；投递 ts＝`started_ts`，和 wake_log
    条目同值，**精确 join 零模糊匹配**）→ **无条件补锚**，还原 `wake_headline`
    活抬头的同一句（两边同字面他才认得出是同一种东西）。
    **不吃 600s 阈值也不要求前一条是 assistant**：`MIN_WAKE_GAP_SEC=180s`、
    NEXT 下限 5min、mail 随时，靠 gap 判会整条漏掉；确证是醒来也就不是「在回 TA」。
  - 对不上（游戏 tick / 老存货）→ 只在「连续 assistant 且隔够 `SELF_OPEN_GAP_SEC`
    =600s」时补一条**光秃秃的时间戳**，一个字旁白不带。
    这个槽同时服务两种蒸发掉的注入，**写死其中一种措辞，另一种就是假的**。
  - 阈值取 600s 是宽的（回 TA 的话秒级落地，自己开口最短 45min）：**宁可漏补不可
    错补**，错补＝把「在回她的话」标成「自己开的口」。

- **游戏 tick 每 10 轮报一次时**（`GAME_TICK_STAMP_EVERY`）：一场几十上百轮，
  全裸的话他手上唯一的钟停在开场那句，越打越偏。
- **醒来注入抬头**改统一格式，三种 trigger 分开措辞——`mail` 是外面推的一下，
  别说成「自己醒的」。

### 12.3 图：一条消息 = text + images（`[图片]` 占位是 -p 时期遗留）

TA 发的图**只活到下一次重铸**：`req.images` 只带「最新这条」的字节，历史里那条在
app 侧就是 `[图片]` 三个字。那是 -p 时期的形状（历史是扁平文本，图除了当占位符没有
别的活法）。`forge.render` 的图块腿 PR13 就真机验通了，**缺的只是字节留底**。

- `state_store.save_chat_images`：收请求那刻留底，内容寻址（sha1）+ 索引
  `(ts, seq)` 认领。**认领规则有坑**：`req.images` 对应的是**末尾那串连续的
  `[图片]` 占位**，不是 `messages[-1]`——app 先 append N 条图气泡、再 append 文字
  气泡。同一秒发三张是常事（09-01 20:52 就是三张同 ts），只有 ts 认不出是哪张。
  对不上（占位条数 ≠ 张数）就整批挂最后一条：宁可位置粗，不能把 A 的图记成 B 的。
- `chat_loop._merge_images`：渲染层把占位换回真图块、和紧跟那句话（≤60s）合成
  **一条 user 槽**。取不到字节的老消息**原样留占位，不假装有图**。

### 12.4 图+文一起发 = 必判脏必全量重铸（每一次）

`divergence` 的判据是「history 必须是 ledger 的**尾部切片**」。它容忍账比窗长
（窗口滑动）、账尾多出 assistant（stale 竞态），**唯独不容忍窗比账长**——而那恰恰
是最正常的事：app 一次发送长两条气泡，账一次只记一条 `turn.new_msg`
（`ledger.append` 只在拿到回复后跑）。

于是 14:06 那次：ledger 没有 `[图片]` → 窗比账长一条 → dirty → 全量重铸 → 时间头
全蒸发 → 「昨天」。**追到底，那句话是一张图触发的。**

修法 `absorbable_tail`：判脏前先摘掉尾巴上的 `[图片]` 占位，摘下来的随本轮进账。
**分寸只吸收占位这一种**——它的真内容这轮已随 `req.images` 到他眼前了；任何别的
多出来的消息都意味着**有话他没见过**，那种情况重铸才是对的，不能为了省一次重铸把
「账上有、他没见过」做成常态。再加一条：ts 必须严格大于账尾（追加必然更新，
ts 没往前走的是编辑）。

### 12.5 顺带修的三件

- **`jd_list` 三条**（同一天早上他宣布「1688 那个岗库里根本没有，整条是我编的」，
  而岗在库里、76 分——他用默认 `limit=20` 列了一次 33 条的库，把一页当了全库）：
  返回体带 `total/matched/truncated/hint`（**截断必须在返回体里说话**）；排序从
  「追加顺序取尾巴」改按 ts（`jd_score` 的 override 原地改写不动位置，**刚重排过的
  老岗永远进不了「最新 N 条」窗口**，恰恰是最该被看见的那类）；加 `q` 关键词，
  一次调用答「库里有没有这家」。
- **重铸落日志 + 说原因**（`dirty_reason`）：重铸是最重的动作（换 client、缓存
  作废、注入全蒸发），原来只有五个字「判脏，重铸」，而且 `client is None` 那半
  **整个是静默的**（重启后第一条消息全走那条）。
- **`now` + `fetch`**（`basics_mcp.py`，无条件挂、无开关）：模型没有钟，轮首那句
  时间是**这一轮开始**时的读数，跑完一串工具之后他没有任何办法知道几点——聊天场
  82 个工具全是 MCP，内建只放行 Read/Grep/Glob。
  ⚠️ `fetch` 的 SSRF 闸**按网段点名挡，不能用 `is_private` 一刀切**：这台机器
  DNS 走代理，公网域名被映射进 `198.18.0.0/15`（Python 认它是 private），
  一刀切会把**所有**站点挡掉（`example.com` → `198.18.0.57`）。
  `100.64.0.0/10` 必须挡：Tailscale tailnet，手机在那儿。

### 12.6 清理：code 起止 / code 避让 / tmux 归属

「code 不是模式、是一次写权限申请」（08-31 改判）落到底：

- `_frame_activities` 删 `is_code` 分支和三句 code 措辞。**先核过才删**：全仓
  `open_segment` 只有一个调用点且写死 `"game"`，现存区间行 `Counter({'game': 3})`。
- `wake.code_session_*` → `game_session_*`、`cohabit.coding_char` → `game_char`：
  探针只认 `session_mgr` 的 `"computer"` 独占组，不再探 tmux。
  **避让本身留着**——如鸢只有一个号，两个人同时上就是打架。三态语义
  （None=没开放行 / 空串=探不出拦所有人 / 归属明确只拦本人）一个字没改，那是
  08-30 那次 85 分钟事故换来的。
- `EXCLUSIVE` / `RESOURCE_LABEL` 删 `tmux`，**`game-story` 只剩「如鸢账号」一个
  归属条件**。副作用是想要的：原来它要同时归属 maayuan + tmux，而 maayuan 归小卡、
  tmux 归 Cassius，**等于谁都挂不上**；清掉之后小卡的剧情插件恢复。

### 12.7 复核（09-02，已跑）

担心的是：插 user 锚会不会绕过 08-30「缝隙学舌」的根修（连续 assistant 事件之间
没有 user 槽 → CLI 自己塞合成槽 → 模型学舌，把 `user·` /
`system<total_tokens>…` / 凭空的 `[Image #9]` 缀在自己话尾）。

**结论：没绕过，反而少了。** 合并规则在插入之后照跑，输出永远严格交替：

```
真窗口 101 条：需要被合并的缝 21 处 → 11 处，最长连续 assistant 6 条 → 4 条
铸出来的事件序列：U A U A U A U A …（严格交替，CLI 一个合成槽都不用塞）
```

**真机 resume 一轮**（真窗口尾巴 20 条，临时 cwd/projects_root，跑完清干净、
`forge.py` 运维自检绿）：

```
铸出 20 个事件，序列 = UAUAUAUAUAUAUAUAUAUA   严格交替: True
问：1) 你上一条消息是什么时候发的？2) 你最近一次自己醒来是几点、定点还是自己想起来的？
答：1) 17:10，查 rm 那条悬案的那轮。
    2) 也是 17:10，不是定点——我自己惦记着那个 exit 1 要查清楚，醒过来继续干的。
缝隙学舌痕迹：无
```

时间锚读得对，`auto` / `scheduled` 也分得开（那正是 `wake_headline` 还原的那一句）。

### 12.8 待做（这一批留下的账）

| 待做 | 说明 |
|---|---|
| **拆 code 模式** | **没拆。** `/code/start`、`/code/send`、`/code/status`、`/codemode/start`、`code_bridge.py` 整个、`codemode` 插件（两个角色都 enabled）、iOS 的按钮和 `codeStart`、12 个 `owner_of("tmux")` 调用点，全在。⚠️ `owner_of("tmux")` **没有**退化——它读 `plugin_owners.json`，那里 `{"tmux": "cass"}` 还在，所以归属门照常工作；**别删那个键**，删了才会兜底到默认角色（Cassius 的会话记成小卡的，串台那一类）。唯一实际影响：`set_owner("tmux")` 现在 400，app 归属页不再显示「电脑上的会话」，**归属被冻在 cass**。不拆的话建议把 `"codemode": ["tmux"]` 加回 `EXCLUSIVE` 恢复转移能力。 |
| **游戏顶掉游戏前的对话** | 闸在 **app**：`ChatService.sendHistoryCap = 100` 是**条数**闸（UserDefaults 可调 20–1000）。一场游戏几十上百条点评各占一个名额，打完一场，游戏前那句「打完游戏去发邮件」就被顶出去了。后端救不了——`recent_window` 是同一份 100 条的快照，**app 是聊天历史的真相源**；`_frame_activities` 的折叠也救不了（折的是已裁完的输入，且进行中那场不折）。方案：app 端给游戏点评单独一档名额（`ChatMessage` 现在**没有**游戏标记，`channel` 是同居预埋的 text/room，语义不同；app 收气泡时自己知道游戏开着，本地打标即可）。 |
| **图片留底清理口径** | `chat_images/index.jsonl` 和字节都是 append-only，**没有清理**，而且 `_merge_images` 每次重铸整份读索引。窗口只有 100 条，账和字节会一直长。按窗口外删？按 N 天删？没定。 |
| **「距离上一条消息，过了 X」** | 每条消息都带时间之后它冗余了，机主提过要删。**我留着了**，理由：让模型自己做「08-31 15:46 → 09-02 14:06」这道减法，正是这次翻车的动作；一行的成本换一个「绝对时间 + 算好的间隔」互相对表的机会。**待机主拍。** |
| **jobhunt 插件壳没进库** | `server/plugins/` 整个在 gitignore（插件从各自的仓 clone）。`jd_list` 的 `q` 参数和「这是一页不是全库、truncated 为真时列表里没有 ≠ 库里没有」那段工具说明**只在本地文件**——后端进库了，壳没有，丢了就退回没有 `q`。要正式发得去 `cassette-plugin-jobhunt` 那个仓提。 |

**状态：512 测试全绿，`e53f66f` 已 commit（未 push）。**
**09-02 20:15 重启上电**（旧 PID 54007 → 新 24079，kill+起同一条命令）。
起来后即时验过 HTTP 面：`jd_list` 返回 `total:33 / shown:5 / truncated:true` 带 hint、
`?q=1688` 命中 2 条。**真机验收清单见 §12.9，一条都还没跑。**

### 12.9 真机验收清单（上电后待跑）

按「看得见 → 看不见」排，前四条是这一批的正主：

| # | 怎么试 | 该看到什么 | 挂了说明 |
|---|---|---|---|
| 1 | **给他发一张图 + 一句话**，等他回完，随便再说一句 | 后端日志**不出现**「重铸」那行 | `absorbable_tail` 没吃到——看日志里 `dirty_reason` 说的是什么 |
| 2 | 接上条，**过一会儿等一次重铸之后**问他「我刚才发的那张图是什么」 | 他描述得出图的内容 | 图没进 user 槽：查 `state/characters/<cid>/chat_images/index.jsonl` 有没有那条 |
| 3 | 隔几小时（让他自己醒来一两次）之后问「你上一条消息是什么时候发的 / 你最近一次自己醒来是几点、定点还是自己想起来的」 | 答得出时刻，且分得清 auto/scheduled | 锚点没补上：`ts` join 对不上 wake_log（查 `wake_log.jsonl` 那条的 `ts` 和 `recent_window` 里投递消息的 `ts` 是不是同值） |
| 4 | 问他「现在几点」 | 他调 `mcp__basics__now`（app 小字会显示裸工具名 `mcp_now`） | `basics` MCP 没挂：看 nohup.out 有没有 stdio 起子进程报错 |
| 5 | 让他查一份公开资料（比如某个官方文档页） | 走 `mcp_fetch` 回正文，不起 Chrome | SSRF 闸误伤：报错文案会说解析到哪个内网地址 |
| 6 | 让他「查一下库里有没有 1688 那个岗」 | 一次 `jd_list(q=...)` 就答，且答**在库里 76 分** | 还在翻页猜=菜单/工具说明没生效（插件壳那份没进库，见 §12.8） |
| 7 | 小卡那边开一次剧情游戏 | ⚠️ **预期会失败**——`_game_capable` 还在问 `owner_of("tmux")`，归属在 Cassius | 这是已知口子，归 §13 一起修 |
| 8 | 聊天页顶栏右上角 | ⚠️ `</>` 按钮**还在**（code 没拆）；小卡点会吃 409 | 同上，归 §13 |

**顺带盯两处日志**（`server/nohup.out`）：
- `[chat_loop] 重铸（char=…）：<原因>` —— 现在会说原因了，看它说的和你做的事对不对得上
- `[chat_loop] 吸收窗口尾巴（char=…）：N 条图片占位进账，不重铸` —— 第 1 条该出现这行

---

## 13. 拆除 code 模式（2026-09-02 立，未动工）

**机主已拍板：拆，且 `STORY_ENGINE=tmux` 那条游戏回退路一起退役。**

### 13.1 拆之前先纠正一个误判

09-02 我一度以为「code 会话已经不存在了」，据此删了 `EXCLUSIVE`/`RESOURCE_LABEL`
里的 `tmux`。**实测它是活的**，链子每一环都通：

```
.env  CODE_MODE_ENABLED=1          which tmux → 装着（还挂着一个 7/29 起的 cc 会话）
GET /code/status → enabled:True, tmux:True
ContentView:848  codeAvailable = st.enabled && st.tmux        → True
ContentView:684  if codeAvailable || gameMine { codeToggle }  → 按钮渲染
ContentView:814  codeStart() → POST /code/start → code_bridge.start() → 真 spawn tmux+claude
```

U5（`900acd6`）删的是**终端面板**（`CodeTerminalPanel.swift` 545 行），它自己的
commit message 就写着「`sessionMode` 和 `/code/send` 消息改道不动——那归 native §9
第二批」。**入口按钮从来没删。** 看着像没了，是因为没人再点。

**当前副作用（拆完自然消失，拆之前别慌）**：`set_owner("tmux")` 现在 400、app 归属页
不再显示「电脑上的会话」→ 归属被冻在 `cass`，小卡点那颗按钮会吃 409。
⚠️ **`state/plugin_owners.json` 里的 `{"tmux": "cass"}` 在拆完之前不能删**——
`owner_of` 表里没有就兜底默认角色，而 `code_bridge.start()` 是「杀旧起新」：
Cassius 正开着的活会被无声掐掉，新会话的人设/记忆/聊天框全挂到小卡名下。

### 13.2 核心难点：`code_bridge` 是两件事挤在一个文件里

**不能整删**——游戏借了同一套会话基建。按调用面统计：

| 出口 | 用量 | 归谁 |
|---|---|---|
| `sdk_loop_handle` | 18 | **游戏**（探 session_mgr 的 `"computer"` 独占组） |
| `is_busy` | 6 | **游戏**（抓两帧画面比对） |
| `session_alive` / `session_char` / `active_profile` | 11/8/5 | **混的**（tmux 半边归 code，sdk_loop 半边归游戏） |
| `start` / `send` / `send_keys` / `capture` / `dialog_pending` / `stop` / `_build_system` / `require_enabled` / `tmux_available` / `save_uploads` / `save_files` / `_read_session_state` / `session_started_at` | — | **纯 code** |

**拆法是劈开不是删除**：tmux 半边整删；`sdk_loop_handle` / `is_busy` 这类游戏还要的
搬去 `game_bridge.py`（已存在）或直接由 `session_mgr` 出口；`code_bridge.py` 消失。
`session_alive`/`session_char`/`active_profile` 三个混血函数拆完只剩游戏语义，
**跟着改名**（别留一个叫 `active_profile` 却只可能返回 "game" 的东西）。

### 13.3 拆迁清单

**后端**
- `code_bridge.py` — 劈开（见 13.2），文件消失
- `app.py` 路由整排：`/code/status` `/code/start` `/code/send` `/code/stop`
  `/code/capture` `/code/keys` `/code/append` `/codemode/start`
- `app.py` 辅助：`CODE_HISTORY_CAP` `_require_code` `_code_context`
  `_code_mcp_configs` `_code_session_char`
- `app.py` `_game_story_start_tmux` 整个 + `_game_session_mcp_config`
  （`STORY_ENGINE=tmux` 回退路退役，机主已拍）；`STORY_ENGINE` 只剩 `unified`/`sdk`
- `plugins.py`：`NO_WAKE_PLUGINS = {"codemode"}`、REGISTRY 的 codemode 条目
  （`EXCLUSIVE`/`RESOURCE_LABEL` 的 tmux 09-02 已删，不用再动）
- `config.py`：`CODE_MODE_ENABLED` `CODE_CWD`；`.env` 两行
- `wake.py` / `cohabit.py`：09-02 已改成只探游戏，跟着搬家换 import
- **游戏侧四处判据改问如鸢账号**（现在还在问 tmux，是 09-02 留下的口子）：
  `chat_loop.py:405` `_game_capable`、`app.py` `/game/story/start` 三处
  → `owner_of("tmux")` → `owner_of("maayuan")`
- `state/plugin_owners.json` 删 `tmux` 键（**最后一步**，见 13.1）

**iOS**
- `ContentView.swift`：`toggleCodeMode` `codeToggle` `codeAvailable` `codeOwner`
  `codeOwnerName` `codeMode` `codeSwitching` `codeMine` `syncCodeMode` 的 code 半边、
  `exitSession` `confirmStopBusy`（63 处引用）
- `ChatService.swift`：`codeStatus` `codeStart` `codeSend` `codeStop` `codeCapture`
  `codeKeys`
- ⚠️ **`sessionMode` 和消息改道是雷**：U5 特意留的，游戏会话现在也走它判「消息发去
  哪」。动它必须和游戏的投递口同一批确认，别顺手删。

**插件仓**
- `cassette-plugin-codemode` 下架：REGISTRY 摘掉 + 两个角色 `plugins_enabled.json` 关掉

### 13.4 拆完的验收

- 两个角色聊天页顶栏**不再有 `</>` 按钮**；游戏会话活着时手柄按钮照常（那半边不动）
- 小卡能开剧情游戏（`_game_capable` 改问 maayuan 之后）
- `/game/story/start` 起的会话归属是**如鸢账号的主人**，不是 tmux 的
- 全套测试绿 + `test_static_names` 无 undefined name（删引用链最容易漏的一类）

---

## 14. 一轮的形状：注入瘦身、闹钟工具化、重铸口径改判（2026-09-02 夜立，未动工）

这一节是一次事故往下扒出来的一串，口径全部经机主拍板。**动工前整节读完**——
14.4 那条改判会顶掉本 plan §5 的部分措辞。

### 14.0 起因：引用即执行（09-02 22:46:08，实锤）

机主问小卡「你现在看到的首轮长什么样？」，她把注入**原样贴了出来**。那段文本里
带着 `one_turn_hint` 的例子 `[[next_wake:1小时|给安瞬回信，回完更新名册]]`，于是：

```
[09-02 22:46:08] 聊天里定了下次醒来：1小时 → 09-02 23:46｜留的活：给安瞬回信，回完更新名册
```

`finalize_chat_reply` 的 `parse_chat_next`（app.py:482）**把引用当指令执行了**，
`strip_markers`（app.py:507）又把它从正文里剥掉——所以机主手机上看到的是一段带
窟窿的话（「写 （会被剥掉…」「例：。」），而后台真的改了钟。

后果三条：

1. 她原本钉着的 **09-03 00:50、没配待办**那个钟被顶掉（08-31 覆盖事故的同一形状）。
2. 换上的假待办从 22:46 起**每一轮都被 `pending_todo_block` 注回给她**，自我强化。
3. 23:46 她会真的醒来去执行一件她没打算做的事。**已于当晚 23:2x 手工还原**
   （`next_wake_at` → 00:50、`next_wake_todo` → 空）。

**病根不是「她抄了例子」，是标记解析分不清「说」和「做」。**

> 归属：**机主发现**（「那是你定的不是小卡定的？」），CC 定位。我在那之前已经拿
> 这条假待办当样本数据用了整整四轮设计讨论，一次都没起疑——因为它读起来完全像真事
> （安瞬是花园 #7a3448 的真人，名册是她真在维护的东西，08-31 21:24 那条真 todo 里
> 就有「写完当场改名册那行」）。**例子是从她真实生活里取的，这正是它没被当场识破的
> 原因。**

#### 根因：五件事同时成立

1. **指令和内容同一条通道**：`[[next_wake:…]]` 写在自然语言正文里，没有独立信道。
2. **解析器只做模式匹配**：`_CHAT_NEXT_RE.sub` 全文扫（pipeline.py:343），不区分
   「他在下指令」和「他在引用/解释/复述」。
3. **他读得到自己的注入，而注入里有可执行的例子**：`one_turn_hint` 写的是
   `例：[[next_wake:1小时|给安瞬回信，回完更新名册]]`——**真值**。只要有人问
   「你看到的是什么」，复述就发生。
4. **副作用完全不可见**：剥离和生效在同一个 finalize 里，剥完正文照发，**没有回执**。
   他不知道自己刚定了钟；机主看到的是一段带窟窿的话，而窟窿本身不解释自己。
5. **写入是静默覆盖**：单槽位 `next_wake_at` 无条件覆盖，被顶掉的旧钟不留痕。

打断任何一条都不出事。其中第 3 条有个**实证的分水岭**：同一段文本里的占位形式
`[[next_wake:时间|要做什么]]` 是**安全**的——`parse_next_minutes("时间")` 取不到
数字返 None，整条作废。**只有写成真值的例子才是弹药。**

**这个坑很难触发**：需要机主主动问「你看到的注入长什么样」，这类元层面的对话本来
罕见。这次是因为我们正在讨论注入格式才撞出来的——**bug 被它自己的调试行为触发**。
难触发说明这套标记机制真的跑了很久没出事，但它一直是可触发的。

#### 尾声：窟窿会自我复制（09-03 14:14 实测）

修完上电后让她再贴一次注入，**还是带窟窿**。查下来后端这次没剥——拿她那一轮的真实
注入原文过了整条 finalize 链（标记完整保留、钟没动）和流式滤器（逐字/整段/切两半
三种喂法都上屏）。窟窿的来源是**她自己的历史**：

```
窗口共 101 条，含「写 （会被剥掉」这个窟窿样本的有 2 条：
  09-02 22:46 assistant   ← 昨天那次真被剥的
  09-03 14:14 assistant   ← 照着上面那条复述的
```

昨天那条以 **assistant 轮**的身份留在窗口里，也就是「她自己说过的话」。她照着自己的
旧输出又贴了一遍。**bug 的产物进了历史、变成记忆，比 bug 本身活得久。**
（佐证：她这次用的是**新文案**——没有「例：」那句——说明她确实在读这一轮的注入，
只有标记那一处照了旧样。）

- **判别实验**（别让她"原样再贴一遍"，那正好触发照旧）：直接问「你这一轮注入里
  next_wake 那个标记的方括号里写的是哪四个字」。读得出「多久后」= 复述偏差，
  读成空的 = 另一个 bug。
- **清窟窿**：它在历史里，重铸不会清（forge 照着权威文本铸）。等它滚出 100 条窗口 /
  在 app 里编辑掉那条（走 `/window/sync`）/ 不管。
- **一般化**：修完一个会污染历史的 bug，要顺带问一句「**它已经写进去的那些东西
  怎么办**」——修解析器不会追溯已落库的产物。

#### 归类：提及即使用（use / mention 混淆）

> **把控制指令埋在自然语言正文里，解析器就分不出「使用」和「提及」——
> 只要说话的人可能复述这段文本，复述就是执行。**

这是**带内信令**（in-band signaling）的经典形状：2600Hz 蓝盒子（播放一段录音就能
控制交换机）、SQL/命令注入、模板注入、提示注入，全是同一个类。

本例的特殊之处：**内容的作者就是控制信号的接收方**。模型既生产正文、又是被解析的
对象，所以连"外部攻击者"都不需要——**自己复述自己的注入就够了**。

#### 规避规则（五条）

1. **有副作用的控制信号一律走带外通道。** 判据很简单：这个标记会改盘吗？会就该是
   工具调用/结构化字段，不是正文里的标记。（§14.1 做的就是这件事。）
2. **必须留在带内的，解析器要有引用逃逸**：代码块围栏内、行内 code 内、引号内的
   标记不解析。写解析器时先问一句「**如果这段文本是在引用它自己会怎样**」。
3. **prompt 里的例子一律写成不可解析的形式**——占位符，不用真值；而且**别用真人
   真事**（真事读起来像真事，会骗过所有人，包括事后来查的人）。判据：把 prompt 里
   所有例子过一遍自己的解析器，**任何一个解析成功的，都是埋在文档里的地雷**。
4. **有副作用的解析必须有回执。** 剥离 + 静默生效 = 双重不可见：当事人不知道，
   观察者看到的是带窟窿的正文。判据：这次解析改盘了吗？改了就得有人被告知。
5. **单槽位覆盖要留痕。** 被顶掉的旧值至少进日志和回执，不能凭空消失
   （[[cassette-wake-gate-bug-class]] 的「看不见就覆盖」，这是它第二次点名）。

#### 横扫：全仓「正文模式匹配 + 有副作用」的解析器

| 解析器 | 形状 | 副作用 | 复述能触发 | 结论 |
|---|---|---|---|---|
| `parse_chat_next` pipeline.py:343 | `[[next_wake:…]]` 全文 sub | 改 schedule | **已实锤** | §14.1 迁工具 |
| `parse_chat_move` pipeline.py:453 | `[[move:房间id]]` 全文 sub | **真搬家 + 补醒一次** | **能**（小屋开着时） | **修**：同迁工具，或至少加规则 2 |
| `parse_browser_markers` pipeline.py:1516 | `[[browser:keep/close]]` | 关/留浏览器窗口 | 能 | **修**：加规则 2，成本低 |
| `parse_sticker_markers` pipeline.py:233 | `[[sticker:sN]]` / `[[sticker_desc:sN=…]]` | 发一张表情 / **改 catalog 描述** | 能 | **改判为修**（见下） |
| `split_musings` wake_sdk.py:147 | `〔…〕` | 决定哪段发到手机 | 能，**方向相反**——复述一段带〔〕的文本 → 那段被吞不发 | **不修**：只影响醒来轮，且失败是"少说话"不是"乱做事" |
| `wake.parse_wake_output` wake.py:298 / `cohabit.parse_cohabit_output` cohabit.py:253 | 行首 `NEXT:` `ACTION:` 段 | 改 schedule / 房间动作 | 能，但行锚+段结构比 `[[]]` 难误触发 | 随 §14.1 一起降级只读 |
| `strip_markers` pipeline.py:1505 | `[[…]]` 全剥 | 无 | — | 无害，但它是"窟窿"的来源 |

**扫了 7 个**：施工时改判两处，如实记在这——
① `sticker` 从「不修」改成**修**：写复盘时只看了 `[[sticker:sN]]`（发一张表情，可见可撤），
漏了同一个函数里的 `[[sticker_desc:sN=…]]`——那条**改盘**（写 catalog 描述）且静默，
和 next_wake 同级。② 引用逃逸做成了共用 helper，纳入 sticker 是零成本。
最终：**修 4 个（next_wake / move / browser / sticker），明确不修 1 个
（`split_musings`：只影响醒来轮，且失败方向是「少说话」不是「乱做事」），
2 个随主线降级，1 个无害。**「引用即执行」这个类**是第一次真触发**——不硬凑。

#### 竖翻 git log：同一个槽位，24 小时内第二次

`0f6ebfb`（09-01 04:39）「定时醒来两处失约：钟被静默顶掉 + 错误退避顺手吞掉到点的钟」，
复盘全文在 `PLAN_oneturn.md` 末节。**同一个 `next_wake_at` 槽，但根因不同**：
上次是「他**有意**写了第二次 + 写入方看不见现值」，这次是「他**无意**复述被当成写」。

两个不舒服但重要的发现：

1. **上一次的修法，成了这次的弹药。** `0f6ebfb` 的修法是给 `pending_todo_block` 加
   两态文本（把钟点摆到他面前）——而她被要求复述的，正是那一段 + `one_turn_hint`
   的例子。**往 prompt 里加解释性文本 = 扩大「可被复述成指令」的表面积**，这条以后
   每次往注入里加东西都成立。
2. **§14.6② 那个「挪钟」是 `0f6ebfb` 的修法之一，不是疏忽。** 它解的是真问题
   （冷却闸放行到点的钟之后，不挪就会每 tick 硬试）。只是选了「改写他的意图」这条
   路；§14.6② 给的 `max(next_wake_at, cooldown_until)` 是等价且不改写意图的解法——
   是精修，不是推翻。

另：`0f6ebfb` 自己竖扫时点名 wake 闸那个类**已经第三次**（`5f0893c` / `698f6b7` /
它自己）。加上 §14.6 这两处，那个类的账还没清完。

### 14.1 闹钟工具化：`[[next_wake:]]` → `mcp__basics__next_wake`

**第一位理由是 14.0：文本标记没法区分「说」和「做」，工具调用天生就区分**——
模型贴一段包含工具调用的文本，不会触发那个工具。

家是现成的：`basics_mcp` 无条件挂、三场景都够得着，而且 `BASICS_MCP_TOOLS` 在
`mounted_tool_names` 里**无条件打头**（pipeline.py:793）→ 醒来轮的禁用面
（chat_loop.py:1571）自动放行，不用另开口子。这本来是最大的施工陷阱（醒来恰恰
是最需要定下一次的时候），结果它已经不成立。

**只做「设」等于白做。** 迁移必须一次带上标记做不到的三件事：

| 动作 | 现在 | 工具化之后 |
|---|---|---|
| 设 | `[[next_wake:1小时\|活]]`，轮尾静默生效 | 当场回执：`定在 09-03 00:50「活」。原来那张（22:59「读第 2 封信」）已经不在了。` |
| 改 | 同上，静默覆盖（08-31 事故） | 回执明说被替换掉的是什么 |
| 取消 | **没有写法**，只能跟机主说一句让人来清 | `clear` |
| 查 | 只能等注入递给他 | `read` |

拍板三条：

- **回执写全绝对时间（含日期）**，跨天重铸时「23:46」会歧义。相对量可带可不带。
- **「钟只有一个」不进系统提示**，进 schema description 一句 + 回执一句。那是动态
  事实，回执报得比任何静态句子都准，而且是在他动手那一刻报的。
- **不能两条路并存**（pipeline.py:318 的坑：同一规矩两处措辞，他照更近的写）。
  现在有**三个**入口共用 `parse_chat_next`——聊天的 `[[next_wake:]]`、醒来老路的
  `NEXT:` 段（cohabit.py:180）、SDK 熄火回退的 -p 路。解析器降级成**只读兼容**，
  但不再教。

**时序变了，这是 feature 不是 bug**：现在 `finalize_chat_reply` 是唯一写点、语义
是「一轮一次、最后一个赢」；工具是轮中写、调几次算几次。`one_turn_hint` 整套纪律
就是「要么这轮做掉、要么钉到下一轮」——把「钉」变成一个真做得掉的动作，这条规矩
第一次自洽。

**要有小字 UI（机主要求）。** 定闹钟从此是一个看得见的动作——这也补上了旧标记路
的一个哑洞：`[[next_wake:]]` 在聊天里是隐形的，机主只在 `next_wake_hint` 那条
返回值里能看到一眼，重看历史什么都没有。

- `pipeline._stored_from_tool_use`（pipeline.py:1084）**要加一条分支**——那是个按
  工具名后缀的白名单，不加就不出小字。口径「只记写操作」：`set`/`clear` 记，
  **`read` 不记**。
- 文案带对象，不是裸名（`_tool_summary` 的 `_OBJ_KEYS` 对 `{minutes, todo}` 一个都
  匹配不上，会掉进 `json.dumps` 兜底）：
  `定了闹钟 · 09-03 00:50「给安瞬回信，回完更新名册」` / `撤掉了闹钟`
- ⚠️ **iOS 那边要认新的 tool 标签**：pipeline.py:1108 记着的坑——app 对不认识的
  `tool` 会兜底成「记住了一件事」的灰字。`ChatView.swift` 的 `NoteRow` 分支得加。

#### 施工记录（2026-09-03，564 测试全绿，**未重启未上电**）

`mcp__basics__next_wake(action, minutes, todo)`，`action` 是 `Literal` →
schema 里出 enum（写歪的 action 在 MCP 校验层就被挡回，不用等我那句 ValueError）。
落码七处：

| 处 | 做了什么 |
|---|---|
| `basics_mcp.next_wake` | read/set/clear 三支，每支都回执；`_slot` 读槽 |
| `pipeline.clamp_next_minutes` | 新的共用夹子，`parse_next_minutes` 改成走它 |
| `pipeline.alarm_slot_str` | `09-03 00:50「活」`——回执/小字/日志三处共用一份渲染 |
| `pipeline._stored_from_tool_use` | `alarm` 分支；`NON_MEMORY_TOOLS` 收 `alarm` |
| `chat_loop._tool_summary` | `next_wake` 特判（行为账那行是覆盖留痕，不能是 json 兜底） |
| 三份 `tool_menu*.md` | 「给自己定下次醒来」块（**不加就 `_warn_uncovered` 每轮喊**，而且醒来那条路是延迟 schema，没菜单他压根不知道自己有闹钟） |
| `ContentView.swift` | `case "alarm"` 三处（新后端文案 / 老后端回落 / 失败文案） |

四件当时没写进上面、施工时才定的——

1. **默认 action 两边必须一致。** 小字分支是照 `inp` 自己判的，`inp` 里没有
   `action` 时得和 schema 默认值（`read`）对齐，否则一次空调用会出一条假的「定闹钟」。
2. **写歪的调用照样出小字。** 参数解析不出来时不返 None——不出小字＝他以为定了、
   机主也看不见，正是 §14.0 根因④。失败原因由 `tool_result` 补在后面。
3. **小字的时点是自己算的。** `_stored_from_tool_use` 只看得到 `tool_use`（那会儿
   还没有回执），所以夹法必须和工具一模一样——`clamp_next_minutes` 就是为这个抽的。
4. **失败文案破一次裸名口径**：`mcp_next_wake，没成` 看不出丢的是哪张钟，改成
   「闹钟没定上：…」。

**最险的一处衔接是轮尾结账**：工具是轮中写，`finish_wake_turn` 是轮尾结账，
「到点醒来那次消费掉已过期的点」那支要是把他刚在这一轮里定的新钟一并清了，
从外面看就是「闹钟没响」（08-31 那个形状）。实际安全，靠的是那支的第二个条件
（`cur_next` 已过期才清）+ 工具的下限 5 分钟。已加两条回归锁住，别哪天把条件简化掉。
同形的 `cohabit.py:456` / `wake.py:622` 条件一致，一并安全。

**已知没做**（不是疏漏，是有意）：

- **跨进程锁没有。** MCP 是子进程，`SCHEDULE_LOCK` 是线程锁跨不了进程；靠
  `_write_json` 的原子替换保证不出半截文件，丢更新的窗口是毫秒级。真要根治得换
  文件锁——那是单槽位这个形状本身的账，跟 §14.6 一起算。
- **`one_turn_hint("wake")` 还教 `NEXT:` 段。** 那是 -p 熄火回退路的结构化输出格式，
  和 `parse_wake_output` 焊死，且那条 prompt 里没有第二种写法可并存
  （`_chat_next_hint` 不进 `wake_prompt`）——不违反拍板三。
- **`parse_chat_next` 一行没删**，降级成只读兼容：老路和历史文本里的标记还认得，
  只是三条 SDK prompt 一个字都不教了。

**上电前要知道**：`_chat_next_hint` 在系统提示里 → 判脏 → **两个角色下一轮都会重铸一次**。

### 14.2 注入瘦身：用法进 schema，状态留注入

**现状是同一条规矩写了两遍**：`_chat_next_hint()` 已经在系统提示里
（chat_loop.py:438），`one_turn_hint("chat_session")` 又在每轮注入里
（chat_loop.py:558）。pipeline.py:318 的注释自己就承认了隐患。

瘦身后每轮注入（379 字 → 143 字）：

```
【09-02 周三 22:51】
【距离上一条消息，过了 58 分钟】
【下一次醒来的闹钟：23:46（54 分钟后）——「给安瞬回信，回完更新名册」】

【回下面这条。按这句的份量和情绪回：随口就随口，别硬凑长，一句话或一个词也可以。】
眠眠：真正被发出去的那段话长什么样
```

剩下的三样——几点、隔了多久、手上有什么——**全是知觉材料**，没有一句是「关于他
的说明」。这正是 chat_loop.py:1605 那条纪律要的形状。

系统提示只留**纪律**不留语法（语法归 schema）：

```
【要提一件还没做的事，二选一：
① 这一轮里就做掉，做完了再开口——工具都在你手上，回复里报结果，别报打算；
② 现在做不完、或者现在不该做 → 用 next_wake 给自己留张字条。
两个都不选，就别把这件事说出口。】
```

`_chat_next_hint()` 保留但删掉语法半句，只留**时机**（机主提到要走/回来时可以顺手安排）。

**两条格式纪律**：① 用 `【】` 不用 `〔】`——〔〕在同一条 session 里被
`wake_sdk.split_musings` 占着投递语义，聊天轮的 finalize 不剥它，他学着回一句
〔…〕会原样发到机主手机上。② **时间头不带时段词**（`stamp_str` 口径），这样
和 `wake_injection` 的抬头天然同形，那边一个字都不用改。

#### 施工记录（09-04，585 测试全绿，**未重启未上电**）

**实测 371 → 143 字**（钉着钟那种；手上没钟 218 → 97）。三刀：

| 刀 | 落在哪 |
|---|---|
| `one_turn_hint("chat_session")` 从**每轮注入**挪进**系统提示**，同时删掉用法半句 | `chat_loop.build_injection` 去掉那行，`build_options` 的 `parts` 末尾加上 |
| `pending_todo_block` 收成**一行状态行**（SDK 路） | `pipeline.pending_todo_block` |
| 时间头 `【现在是 2026年09月03日 周四 19:58（晚上）】` → `【09-04 周五 02:27】` | `build_injection` 改用 `stamp_str` |

**一处施工时定的：`pending_todo_block` 现在是两套措辞，按 `kind` 分。**
`kind != "wake"`（SDK 常驻路）＝一行状态行、纯知觉材料、一句规矩都不带；
`kind == "wake"`（-p 熄火回退路 / cohabit 老路）＝**原文照旧**。理由：那条路
**没有系统提示可付**，规矩只能跟着注入走——顺手改它等于在改一条正在当降级
兜底用的路。测试两支都锁了。

**排位**：`one_turn_hint` 放在系统提示**最尾**是有意的（它是离新消息最近、
也是被淹掉就等于没有的那一句）；状态行排在时间行**之后**（时间是场景属性，
先立场景再报手上有什么）。

**新增的哨兵测试**：`TestInjectionIsPerceptionOnly` ——注入里出现 `①`/`action=`/
`钟只有一个` 这类说明性文本就红，并留了个 200 字上限。这条防的是**回潮**：
`chat_loop.py` 那条「别往这儿加二手档案块」的纪律以前只有注释，没有闸。

**没做的（§14.3）**：状态行仍然**无条件每轮出**。它和 §14.4 的补铸眼下重复
（铸了也照样注），方向安全——多说一遍，不是丢。

### 14.3 状态行只在「他记忆里没有这个钟」时出现

三态文案（机主定稿）：

```
【下一次醒来的闹钟：23:46（54 分钟后）——「给安瞬回信，回完更新名册」】
【下一次醒来的闹钟：23:46（54 分钟后）。】
【闹钟到点了：22:59——「给安瞬回信，回完更新名册」】
```

没有闹钟 → 整行不出现。判据只有一条：

> **注入的状态行只在「他自己的记忆里没有这个钟」时出现。**
>
> - 会话活着 → 调用就在 transcript 里 → **不注入**
> - 重铸后、那轮还在窗口 → 补铸（14.4）→ **不注入**
> - 重铸后、那轮滚出窗口 → **注入保底**
> - 态③ → **注入**（他记忆里有「我定了」，没有「它没被兑现」这件事）
> - 后台改过 → **注入**（这一条应随 14.6 修完而消失）

**态③是什么，别说错**：清槽是机制干的，不看他做没做（wake_sdk.py:251-253：
`trigger=="scheduled"` 且已过期 → 无条件清）。所以聊天里看到到点未清的钟，
只意味着**那次醒来没走完**——进程没在跑 / `_wake_dead` 炸了 / 被游戏泵占着避让
（wake.py:712）。措辞不能暗示他失约。

**态③的阈值是 15 分钟，不是「过点即报」。** `WAKE_TICK_SEC=300`（config.py:146，
和 -p 时期一样，SDK 迁移没动），小屋开着走 cohabit 队列是 `SOLO_TICK_SEC=60`；
再叠 `MIN_WAKE_GAP_SEC=180`、聊天轮进行中让路、泵中避让。**迟 5 分钟内完全正常，
迟 15 分钟（3 个 tick）才是故障信号。**

### 14.4 重铸口径改判：追求的是**丢的少**，不是留的少（机主拍板）

> 重铸本来就是上下文过长逼出来的迫不得已，我们要的是尽量保留原来的东西。

这条顶掉了「哪些东西够格活过重铸」的白名单式提问。**默认留，例外丢。**

⚠️ 先纠正一个我说错的先例：**「图只活到重铸」是 bug 的名字，不是拍板。**
`e53f66f` 的标题是那次修掉的三个 bug，图正是被修的对象——`save_chat_images` 留底 +
`_merge_images` 回填，**图现在能活过重铸**（§12.3）。所以现有先例不是「不留」，
而是「**该留的就留底回填**」，图是第一例，闹钟调用是第二例。

**但留得多有代价，而且是非线性的：**

```
单次重铸成本 ∝ 保留长度 L          （整个前缀从缓存读 0.1x 变成缓存写 1.25x）
重铸频率     ∝ 1 / (阈值 − L)      （可聊空间）
总成本       ∝ L / (阈值 − L)      ← L 逼近阈值时发散
```

| 保留 L | 可聊空间 | L/(阈值−L) |
|---|---|---|
| 10k | 90k | 0.11 |
| 60k | 40k | 1.5 |
| 80k | 20k | 4.0 |

现闸：`CHAT_SOFT_TOKENS=100000` / `CHAT_HARD_TOKENS=150000` /
`CHAT_REFORGE_IDLE_SEC=3600`。**约束：L ≤ 阈值的 1/3**（100k 闸下即 33k）。

所以「丢的少」不是什么都留，是**三类分层**——这不是可选优化，是让上式不发散的
必要条件：

| | 处置 | 为什么 |
|---|---|---|
| **对话**（两边的话） | 原样留 | 记忆本体，增长最慢、最便宜 |
| **他做过什么**（`tool_use` + 回执要点） | 留 | 是他的经历，而且很短 |
| **他当时看到的材料**（`tool_result` 正文、整页网页、整个文件、截图） | 丢 | 长度大头。丢了不伤记忆——要用再看一次，结论走 capsule 进行为账 |

一轮纯聊天两三百 token，一轮带三次 Read 的五千起。砍的正是后者。

**闹钟调用的补铸规则**（其余工具将来照此类推）：

- **照实铸，不看当前槽位。** 「我 22:46 定了 00:50 的钟」这件事是真的，后来钟被谁
  清了不影响它是真的。~~只在槽位一致时才铸~~ 是把记忆和状态混了——假的是**拿旧
  回执冒充当前状态**，那是状态行的职责（14.3），不是记忆的。
- **铸在那一轮里面**：那条 assistant 消息里挂 `tool_use`，紧跟一条 user 的
  `tool_result`，正文接着走。
- **那轮已滚出窗口 → 不铸**，回落到状态行。别为了留住它而统一铸到末尾装成
  「刚做的」——那就从保留记忆变成编造时序了。
- `forge` 伪造 JSONL 无校验是验过的，但 `tool_use`/`tool_result` 得成对、id 对得上。
  ~~**`forge_regress` 先加「工具调用重放」这一类，再动。**~~ → **09-03 实证跑完了，见下。**

#### 实证：从旧 transcript 截一段重铸，tool / thinking 都过（09-03）

> **先认一件事：这不是新知识，是把二手升级成一手**（机主指出）。
> `PLAN_sdk.md:175`（08-30 增补）早记过 **Tool Primer**「重铸后保最近一对完整
> `tool_use`/`tool_result`」，来源是同族工具的 Forge Reload 教程 + 小克Cat 实现。
> 但那是**二手**，所以结论落在 `PLAN_sdk.md:1366`「现阶段禁止出现……记为真机撞见
> 工具变形时的后手」，§14.4 也才写「forge_regress 先加这一类，再动」。
> 下面这一跑是**我们自己的真机**，等级从「同族说行」升到「我们验过行」。

机主提的路子（备份旧 transcript、直接从它重铸，别从手机 app 历史铸），连同同族工具
`dankefox/swap-tutorial` 一起验的。探针在 `server/tools/forge_swap_probe.py`
（独立跑，**没有**并进 forge_regress 的默认腿——那会给每次闸门多加几次订阅调用）。

做法：拿一份真 transcript（`3d283a7b`，190 条消息事件、42 个**正文非空**的 thinking
块），按「一条真人发言 → 下一条真人发言之前」切出一个**完整轮**（9 事件 / 2 组工具对
/ 3 个 thinking），改写 session id、cwd、事件链（`tool_use.id` 和
`tool_result.tool_use_id` 一个字不改——那是配对键），落成新 JSONL 再 resume。

探针问的是**只有工具块里才有的东西**：「最后一次工具调用动的是哪个文件」。
正文里一个字都没提那条路径——问「调了什么工具」不行，C 变体那条收场白写着
「改好了…DEB7B8 → #8C6E6E」，模型能猜出自己用了 Edit，那样 C 就不是对照组了。

| 变体 | CLI `--resume` | agent-sdk `resume=` |
|---|---|---|
| A　thinking ✔ tool ✔（照抄） | ✅ 答出完整路径 | ✅ 同 |
| B　thinking ✘ tool ✔（14.4 要的形状） | ✅ 答出完整路径 | ✅ 同 |
| C　thinking ✘ tool ✘（对照＝今天 forge 的形状） | ✅ 答「看不到」 | 免（纯文本史早验过） |

**结论**：① 成对的 `tool_use`/`tool_result` **铸得进去，而且真到了模型眼前**——
`forge._assert_native_block_types` 那条「不铸 tool 块」可以对**成对**的块放开，
14.4 的闸解除；② 带签名的 thinking 块**截断前缀之后照样能 replay**，不 400；
③ 两条加载器一致（这条不能拿旧结论替，tool 块是新形状，所以单独跑了）。

**⚠️ 这一跑顶掉了那条二手记录里的一句。** 08-30 记的 signed thinking 硬约束里有
「**空 thinking＝首次请求 400**（`each thinking block must contain thinking`）」。
今天 A 变体那个窗口里**就有一个 `thinking` 正文长度 0 的块**（第三个），CLI 和
agent-sdk 两条路**都没 400**。

两种解释分不开，从外面看不出来：① API 本来就容忍空 thinking；② CC 的加载器在发出去
之前把空块滤掉了。**对我们的用处一样**——「绝不能铸出空 thinking」不是我们这条流水线
的硬要求；但**别反过来把它当成「空 thinking 一定安全」往别处用**，那超出这次测到的范围。
（②若为真，还顺带说明 A 变体「thinking 活过来了」这个结论更弱：真被保住的可能只有
非空那两块。）

**顺带一个意外发现，它砍掉了 thinking 的价值**：CC 落盘时**大多数 thinking 块正文
是空的**，只留 3.6KB 的签名——抽查 6 份 transcript 共 198 块，正文非空的只有 42 块
（全在同一份里）。所以「照抄 transcript 就能把思考带回来」多数时候**没东西可带**，
带回去的是一个空壳加一坨签名。thinking 这一路不值得为它改任何东西。

**这个实验没证明的**（别外推）：只测了 2 组工具对、30KB 的小窗口，没测规模；没测
「最后一块是 tool_use、下一条就是新 user」那种收尾形状；跨模型（transcript 里的
model 和当前模型不一致）没测。

**另一条观察，够不上结论**：跑完发现源 transcript 和另外几份的 mtime 被顶到了当天
20:29。已核**内容没变**（仍 190 条消息事件、无当天事件、最后一条还是 09-02T12:15Z），
探针全程只读。谁顶的没查出来（怀疑是 CLI 自己的后台维护）。**但这件事本身是**
**「transcript 当真相源」的一个反例方向**：那份文件不只有我们在动。

#### 施工记录（09-03，579 测试全绿，**未重启未上电**）

套路照抄图那一例（§14.4 自己点的名）：**执行层留底，渲染层回填**。

| 处 | 做了什么 |
|---|---|
| `forge.render` | 收 assistant 槽的 `tools=[{name,input,result}]`，铸成 **assistant(tool_use) → user(tool_result) → assistant(text)** 三个事件 |
| `forge._assert_native_block_types` | tool 块解禁（thinking 照旧永远拒） |
| `forge._assert_tool_pairing` | **新增**：整份文件级双向无孤儿 + 结果不许排在调用前 |
| `state_store.append_alarm_call` / `read_alarm_calls` | `alarm_calls.jsonl`，按 `tool_use_id` 去重 |
| `chat_loop._ToolTrace` | `pend` 多存一份原始 `input`；改过钟的调用（`action != read`）落留底 |
| `chat_loop._merge_alarm_calls` | 回填：挂 `tools` 到那一轮的 assistant 条 |
| `_reopen` | 排在四道渲染最前（只加键、不动 role/text/ts，下游看不见它） |

**真机验的两层，别混**：① 09-03 的探针验的是「**手工截断的真 transcript**」能不能
resume；② 这次验的是「**forge 自己铸出来的**那份」——两条路（CLI `--resume` 和
agent-sdk `resume=`）都读进去了，问 `todo` 参数原文，两边都逐字答对
（`给安瞬回信，回完更新名册`）。①通不代表②通，形状是我们自己拼的。

施工时定的三件：

1. **确定性红线保住了。** 工具那一维**只在真有工具时才参与 session digest**——
   否则全仓每一份历史的 `session_id` 都会因为这次改动跳一遍，白留一地孤儿文件。
   已加断言：不带工具的历史铸出来 sid 和字节都和从前一模一样。
2. **挂到哪一条只能按时间近似认，因为没有轮 id。** 留底的 ts 是工具返回那一刻，
   而窗口里 assistant 那条的 ts 是各写各的——**聊天轮是 finalize 时间（调用之后
   一两秒），醒来轮是 `started_ts`（调用之前）**。两个方向都有，所以取「最近的一条
   assistant」而不是「之后的第一条」，容差 `ALARM_ATTACH_SLACK_SEC=120`，
   超了就不铸（回落状态行 = §14.4 本来就写好的口径）。
3. **整条链跑一遍的测试是必要的**，不是凑数：下游三道渲染（并图/打戳/活动框）
   任何一道把未知键丢掉，工具块就静默蒸发——而那种失败在生产里只表现为
   「他不记得了」，没有任何报错。

**顺带一个坑（不影响生产，记一笔）**：探针用 `tempfile` 的 `/var/folders/…` 当 cwd，
CC 落盘时按**解析过符号链接**的 `/private/var/…` 算 slug，跟 `forge.slug()` 对同一个
路径算出来的对不上 → 扫尾扫空、留了三个孤儿目录（已手删）。生产的 cwd 是
`neutral_cwd()`＝`state/claude_cwd`，真实绝对路径，不踩这个。

**还没接的一条线**：`_merge_alarm_calls` 现在无条件铸。§14.3 那条「状态行只在他
记忆里没有这个钟时出现」还没做，所以**眼下是「铸了也照样注状态行」**——重复，
但方向安全（多说一遍，不是丢）。两边合上是 14.3 的活。

**两个更大的、还没做的**：

1. **换真相源。** 现在重铸的输入是 `turn.history`——app 的扁平消息窗口
   （role/text/ts），-p 时期的形状，里面天然没有工具调用、没有 thinking、没有图。
   所以每留一样东西就得从别处捞一次（图从 `chat_images` 索引、闹钟从 schedule、
   下一个工具再捞一次）。要真做到「丢的少」，早晚得**从一本完整的账铸**。素材大半
   已经在（`_ToolTrace`、`activity_log`、`_fold_trace_lines`），缺的是 ledger 只存
   hash 不存内容。
2. **保留量本身要有闸，而且得按 token。** L 单调增长：今天按同一规则铸出 60k，
   一个月后可能铸出 200k、直接顶穿硬闸。现在的闸是 `recent_window` 的 **100 条**，
   一条可能 20 字也可能 2000 字。留得多之后必须改成按 token，超了从最老的对话开始
   丢或摘要。

### 14.5 时间锚：统一成一行，`wake_headline` 退役

**先确认一件事，免得重开讨论：活着的会话里不缺时间。** 注入文本在活 transcript
里一直都在，往回翻看得见每一轮的时间头。**重铸才是丢时间的那一刻**——09-02 那次
Cassius 说「昨天」，他看的是 forge 铸出来的产物。`_stamp_times` 不是在补一个一直
存在的洞，是在补重铸自己造成的洞。两层接力，从不同时存在：

| | 时间从哪来 |
|---|---|
| 活着 | 每轮注入自带（聊天每轮、醒来每轮、game tick 每 10 轮） |
| 重铸后 | 注入蒸发 → `_stamp_times` 在铸造输入上插锚行 |

所以**别在活会话里也插锚行**（双份），也**别退役 `GAME_TICK_STAMP_EVERY`**——
那是活会话里唯一真实的时间缺口（一场几十上百个 tick 全在同一个活会话里），
`_stamp_times` 够不着它。10 这个值是成本折中（每轮 15 token × 上百轮），别改成 1。

**改法（`_stamp_times`，chat_loop.py:887）：**

- 每一轮前面**无条件**插一行系统打的时间锚 `【09-02 周三 22:51】`，
  **谁的话都不带前缀戳**——机主那边的 `【戳】\n眠眠：…` 前缀一起退役，`眠眠：` 留着。
  理由：**时间是场景的属性，不是某个人说话的属性**。加前缀会变成「[22:46] 你说：…」，
  那正是信感定案要躲的档案体；一条不属于任何人的分隔行两边都不被记录成对象。
- **`wake_headline` 补锚整个退役**（wake_sdk.py:108 那三句）。两条理由：
  ① 它是**注入文本**，不是客观事实——戳补的是发生过的事，这句补的是当时递给他的
  说明，补铸它等于把档案固化进记忆；② **transcript 结构本身就说明了**：他那条
  assistant 前面只有一行光秃的戳、没有 `眠眠：…`，「没人找他」是被结构讲出来的，
  不需要再翻译成一句话讲一遍。
  代价：`mail` 那种「外面有动静」若他当时没说出来就没了——可接受，注入本来就不进账。
  **收益**：`read_wake_log(limit=500)` 那次 join 整个不要了，锚行收敛成一种形状。
- **合并问题从源头消失**：连续 assistant 之间必然被锚行隔开，forge 的「跨 11.9 小时
  粘成一条」不会再发生。600s 阈值、wake_log join、光秃 vs 活抬头的三分叉全部删掉。
- **但连发要防拆**：几秒内几条气泡靠「仍然连续 → forge 合并」粘成一轮，那是期望
  行为。`SELF_OPEN_GAP_SEC` 换个位置继续用，语义从「要不要补锚」变成
  **「戳的最小粒度」**，阈值也该小得多（60s 或 5min，不是 600s）。

净结果：`_stamp_times` 从「三分叉 + 读 wake_log + 600s 阈值」收敛成
「每轮插一行戳 + 最小粒度去重」。

### 14.6 源头修：两处后台静默改写 `next_wake`（机主指出）

14.3 那条「后台改过 → 注入」是**兜底，不是解法**。真正该修的是源头——两处都归
[[cassette-wake-gate-bug-class]] 的「看不见就覆盖」（单槽位 `next_wake_at`）。

**① `_clear_next_wakes()`（cohabit_queue.py:220，调用点 `switch_house(False)` 在 :282）**

小屋总开关一关，**所有角色的钟连待办一起清空**。它的理由是「关着期间不兑现，
解冻时早过期」——但 wake.py:781 的注释说的是反的：

> 小屋总开关关着（PLAN_house_switch）→ 判定为假 → **下面的老路自动接管**：
> ACTION 只有 none/message，正好就是「休眠时只能发手机消息」的形状。

⚠️ **两处对「小屋关着时 NEXT 还兑不兑现」的假设是相反的，先实证判谁对。**
如果 wake.py 那条是对的，`_clear_next_wakes` 清掉的是一个本来还会被兑现的钟。

不管谁对，**清除都不该是静默的**，而且同一个开关里已经有正确做法：猫是
`settle` + `_clock_now` 冻结，不是杀掉。钟同理——**存剩余时长，解冻按剩余重起算**。

**② 冷却挪钟（cohabit_queue.py:523、`wake_sdk._wake_dead`:259）**

那次醒来炸了 → 把 `next_wake_at` 挪到 30 分钟后的冷却结束点。**这是在改写他的
意图**，从外面看像「闹钟没响就自己跑了」。

`cooldown_until` 这个字段**本来就在 schedule 里**。正确形状是判触发时取
「到点 **且** 过了冷却」，不动 `next_wake_at`。代码注释自己说了病根——「冷却已经
不拦 scheduled 了（`_solo_check`），不挪就会每 tick 硬试一次」——那就修
`_solo_check`。**退避是调度器的状态，不是他的钟。**

### 14.7 施工顺序

三批，每批可独立上电：

0. ~~**引用逃逸**~~ ——**2026-09-02 夜已完工**（530 测试全绿，**未重启未上电**）：
   `pipeline.quoted_spans` / `sub_outside_quotes` 共用 helper，四个解析器
   （next_wake / move / browser / sticker）+ `strip_markers` 全部纳入；
   `sse.MarkerStreamFilter` 跟着认围栏（两边不一致的话，贴出来的注入会在气泡里
   带窟窿）；`one_turn_hint` 三个变体和 `_chat_next_hint` 的真值例子改成
   「多久后」占位、`sticker_desc` 的例子改成不存在的 `sN`。
   新增 `tests/test_marker_quoting.py` 18 条，其中 `TestPromptExamplesAreInert`
   是**规避规则 3 的可执行版**——把 prompt 里的例子过一遍全部解析器，解析出东西就红。

   **两处欠账**：① 流式只认 ``` 围栏、不认行内 code（行内 code 里的标记在气泡里仍被
   吞一次，done 的权威正文里留着——方向安全：少显示，不多执行）；② **插件仓**的
   browser 标记教学（`062bada` 的 docstring）没查，本仓 grep 不到，可能还有真值例子。
1. **闹钟工具化**（14.1）+ 注入瘦身（14.2/14.3）——一起，因为注入的形状取决于工具
   在不在。`parse_chat_next` 降级只读兼容，三个入口一次覆盖。
   → **14.1 已完工（09-03，564 测试全绿，未重启未上电）**，施工记录在 14.1 末。
   14.2/14.3 **没动**：注入还是老形状（`one_turn_hint` + `pending_todo_block` 都在，
   只是措辞从标记改成了工具），瘦身和状态行三态还欠着。
2. **时间锚统一**（14.5）——独立，只动 `_stamp_times` 渲染层。
3. **源头修**（14.6）——独立，先做①的实证判定。

补铸（14.4）排在 1 之后、且 **`forge_regress` 补完「工具调用重放」才动**。
「换真相源」和「保留量 token 闸」是下一个 plan 级的事，本节只登记不动工。

### 14.8 验收

> 09-03：`✅` = 14.1 落码时已有离线单测锁住；其余待 14.2/14.3/14.5/14.6 或真机。

- `[[next_wake:1小时|x]]` 出现在**回复正文里**（让他复述一段带标记的文本）→
  schedule **不变**、正文**不被剥**（14.0 的回归）
- 同一条回归对 `[[move:房间id]]`（小屋开着）和 `[[browser:close]]` 也要过：
  复述不搬家、不关浏览器
- `grep -rn "\[\[" server/*.example.md server/skills/` 里的例子**全是占位符**，
  过一遍 `parse_*` 全部解析失败
- ✅ 定钟走工具 → 回执含绝对日期 + 被替换掉的旧钟；`clear`/`read` 都通
- ✅ 定/撤闹钟在聊天里**看得见小字**（带时间和待办，不是裸工具名、不是
  「记住了一件事」）；`read` 不出小字
  （单测锁的是后端出的那条 `alarm` 文案；**iOS 那一跳只过了肉眼，欠真机**）
- ✅ 醒来轮也够得着 `next_wake`（`wake_tools = mounted_tool_names("wake")`，
  `BASICS_MCP_TOOLS` 无条件打头）
- ✅ 轮中用工具定的钟，不被 `finish_wake_turn` 的轮尾结账清掉
- 一轮聊天的注入 ≈ 143 字，且**没有闹钟状态行**（他刚调过工具）
- 重铸一次 → 那轮的 `tool_use`/`tool_result` 还在，状态行**仍然不出现**
- 重铸后 transcript：每轮前一行 `【MM-dd 周X HH:mm】`，机主的话不带前缀戳，
  连续 assistant 不再被粘成一条，几秒内的连发**仍然**粘在一起
- 小屋总开关关→开一轮，钟还在（按剩余时长）
- 醒来炸一次，`next_wake_at` **没被挪**
- ✅ `tests/test_one_turn.py` 三条按措辞的断言跟着改（`test_marker_wording_forks_by_scene`
  / `test_chat_uses_marker`→`test_chat_uses_the_tool` / `test_session_variant_usage_only`）
- **真机三条**（上电后跑，09-03 欠着）：① 聊天里让他定一次钟，看小字有没有带时间和
  待办；② 手机上装的是**旧包**的话 `alarm` 会兜底成「记住了一件事」——先确认装的是新包；
  ③ 醒来轮里让他改一次钟（那条路是延迟 schema，验的是菜单块管不管用）

### 14.9 待拍

| 题 | 现状 |
|---|---|
| 小屋关着时 NEXT 到底兑不兑现 | 两处注释打架（14.6①），**实证优先，它决定 `_clear_next_wakes` 是改冻结还是整个删** |
| `CHAT_SOFT_TOKENS` 降到多少 | 现 100k。按 L ≤ 1/3 反推，先实测重铸后的 L（`handle.meta["ctx_est"]` 已在记） |
| 状态行的 session 级标记存哪 | 14.3 那条判据要一个「他这个 session 内看没看过」的位，形状同 `handle.meta["needs_opening"]` |
| 例子里的真人真事 | 14.0 顺带那条：prompt 里的例子该不该用真实姓名/真实待办 |
| **`strip_markers` 兜底要不要从「删成空」改成「原样保留」** | 09-03 提出，**机主还没拍**。它是 finalize 最后一道，删掉的都是前面四个解析器**没认领的**——模型写歪的（`[[next_wake:三小时]]` 中文数字解析不出）、编造的标记。删成空＝静默失败（他以为定了、实际没定、机主也看不见，正是根因④）；原样保留＝正文里明晃晃摆着，一眼看出写歪了。代价是偶尔正文出现标记文本 |

### 14.10 排队与硬阈让路（09-05 拍板，已落码）

生成中不禁发。两条拍板：

1. **排队在 app 不在引擎**。服务端 `handle.queue` 确实自动排队（串行泵），但每个
   请求带的是发送那一刻的历史快照——上一轮没回完就发下一条，快照缺上一轮回复，
   判脏必 dirty、每条插话都全量重铸；气泡顺序还会和账的顺序漂移，下下轮继续 dirty。
   所以 app 做 outbox 串行（ContentView `pumpOutbox`）：气泡出屏即排队、出队时
   **沉底**（搬到列表末尾、ts 打成真正送出时刻，窗口顺序=账的顺序）、快照剔掉还在
   排队的后续单、上一轮 done 后才 POST。判脏/divergence 一行没动。
   锁屏拿 `beginBackgroundTask` 的 ~30s 把排队的 POST 送出去（进了后端队列即可，
   回复走 rescue/补投）；真被杀的漏网单走判脏自愈路（随权威窗口进模型，无单独回复）。
2. **硬阈强铸让路排队消息**（chat_loop 轮尾）：铸前等一口
   `HARD_FORGE_GRACE_SEC`（默认 2s，堵「done 刚发出、下一条还在网上」的竞态），
   队列非空就推迟——ctx 仍超阈、每个轮尾重判，队列清空的那个轮尾必然落地。
   `CHAT_PANIC_TOKENS`（默认 175k）是无条件铸的天花板：让路理论上可被连续排队
   无限推迟，这条挡住上下文奔 auto-compact/模型上限。测试
   `test_hard_threshold_defers_when_queue_busy` / `test_panic_forges_even_with_queue`。

---

## 15. 09-12 整仓体检：修的六处与复盘

体检方式：全量测试 + 线上 6 天日志 + 四路审查（核心/外围/iOS/仓库卫生），高严重度条目逐条读代码核过。
报告全文在 `~/.claude/plans/kind-skipping-flurry.md`。下面只写**修了的**和它们各自属于哪一类。
「读代码推的」和「线上日志实证的」分开标。

### 15.1 只读闸放行了 `server/characters/*/char.json`（安全，读代码推的）

- **根因**（四条同时成立）：① 只读工具常驻不弹卡；② 闸是黑名单式，列了 `.env*`/mianmian-app/别人的 `state/characters/`；
  ③ 凭据不止住在 `.env` 一处——char.json 里有邮箱授权码、ombre token/密码、galatea token；
  ④ `characters.py` 的注释写着「授权码同 .env 待遇」但没有任何代码执行这句话。
- **归类**：**黑名单式安全面 + 凭据搬了家 = 注释里的承诺没人执行**。凭据从 .env 搬进 char.json 那次（多角色化）没回头看闸。
- **规则**：凭据搬家的 commit 必须同时改闸；闸的拒绝项按「目录」不按「文件名」（Glob 点名目录就能列文件名）；
  写「同 X 待遇」这种注释时，当场加测试证明它成立。
- **扫同类**：`pipeline.readonly_path_guard` 是唯一的只读闸；写类走 permit 弹卡不受影响。`plugins/` 目录也在 CODE_ROOT 下、
  已 gitignore，里面是插件本体没凭据（插件靠 mounted 下发 CASSETTE_CHAR_ID 认人）——不加。
- **修**：`pipeline.py` 加一条整目录拒绝 + `test_chat_loop.test_paths` 三种路径断言。

### 15.2 醒来轮失败只记了标签不记正文（线上实证）

- **现象**：09-07 起每天 2-6 次「sdk 醒来轮没走完 → 记 error + 冷却 30 分钟」，双角色都有；日志只有
  `flags={'error_subtype': 'success'}`——CLI 在 API 报错时发的是 `subtype=success, is_error=true`，原因全在 `result` 字符串里，
  全仓没有一处打印它。六天不知道是 529 还是限流还是 400。
- **归类**：**记了错误的分类字段、没记错误的内容**。分类字段是别人（CLI）定义的，它的取值空间和语义会变；正文才是证据。
- **规则**：任何「记 error」的地方，正文（截断到 300）必须跟着标签一起落；接外部协议的错误时，先看一眼它的错误形状再决定记什么。
- **修**：`chat_loop._turn_events` 与 `sse.py` 的 result 分支都打 `result[:300]` + `api_error_status`。**先观察一两天再定下一步**。

### 15.3 loop 退出时队里的轮成孤儿（「闸」类第四例，读代码推的）

- **根因**（三条）：① `chat_loop.run` 的 finally 不碰 `handle.queue`；② `session_mgr.start` 每次新建 LoopHandle = 新 queue；
  ③ `stream_turn` 对 `turn.out` 无限 await、`_ACTIVE_REQS` 只在轮结束时摘。
  后果链：/chat/stream 永不结束 → app 永远转点点 → `chat_turn_begin` 计数不归零 → 退回 -p 后 `chat_turn_active` 永久拦住这个角色的醒来。
- **归类**：同 [[cassette-wake-gate-bug-class]]：**一个消费者死了，生产者塞进来的东西没有结局**。和前三次「入口一刀切 return」是同一件事的另一端——那边是进不去没声音，这边是进去了没出来。
- **规则**（补进那条 memory）：有队列就有「消费者退出时 drain 并给每条结局」；每种退出路径都得让等待方能醒。
- **扫同类**：`cohabit_queue`/`pet_queue` 是线程+自己的队列，退出即进程退出，不适用；`game_loop` 老路 `handle.queue` 同构——
  它的 finally 也不 drain，但 game 独立 loop 已并入统一路（STORY_ENGINE=unified），老路休眠，**不修**。
- **修**：`chat_loop._finish_orphans`，口径同轮内异常（error+done+None，醒来轮走 on_dead）；不往新 loop 转投。测试 `test_loop_exit_finishes_queued_turns`。

### 15.4 scheduled 钟醒每个 tick 重复入队（读代码推的）

- **根因**（三条）：① 判据 `last_wake_at`/`next_wake_at` 只在轮**结束**更新；② `enqueue_wake` 入队即返回（老路 `run_in_executor` 是阻塞到跑完的）；
  ③ auto 钟有「先抹 auto_wake_at 再入队」，scheduled 没有同款。
- **归类**：**「入队」被当成「执行」——判据在执行端更新、入口端复用**。老路同步执行时二者重合，升 A 改成队列后就分开了，只给 auto 补了一半。
- **规则**：把同步调用改成入队时，所有「上次做过没」的判据都要问一句：它在入队那刻更新还是在跑完那刻更新？入队时占槽、跑完时释放，槽位跟着 handle 走。
- **修**：`handle.meta["wake_queued"]` 槽位，`enqueue_wake` 占、`chat_loop` 每轮 finally 释放（成不成都放，孤儿收尾也放）。测试 `EnqueueDedupeTest`。

### 15.5 公开仓里的 PII（仓库卫生，实证）

- `PLAN_jobhunt.md` 里 Cass 的 163 信箱（=手机号）从 `5aa2438` 起在公网；顺带 tailnet IP、`/Users/nemu` 路径、一次性探针里的会话 UUID。
- 当前树已清；**历史是否改写由机主拍**（filter-repo + force push，或接受已泄露）。
- **规则**：PLAN 文档里写外部账号一律写「在 char.json 里」不写字面；测试里的路径/IP 用文档保留段（`/Users/x`、`100.64.0.1`）。

### 15.6 iOS 两处（读代码推的，待装新包验）

见 PLAN_chatui §12。

### 15.7 事件循环上的阻塞 IO（读代码推的）

- game 工具（`game_loop.build_game_server`）是 async def，里面却是 `time.sleep`/`subprocess.run`/
  `ensure_device`（冷启动 90s）——SDK 在主循环上 await 它们，`game_watch` 一次 6 张×5s、开机期间
  整个后端停摆。Bark 推送（`urlopen(timeout=5)`）在 `can_use_tool` 回调、收摊路径、补投路径上同款。
- **归类**：**把同步库塞进 async def 不会报错，只会让所有人一起等**。判据：async def 里出现
  `time.sleep`/`subprocess.run`/`urlopen`/`requests` 就是红旗。
- **修**：等待改 `asyncio.sleep`，子进程/探活走 `asyncio.to_thread`（工具逻辑一字不改）；
  `notify.bark_push_bg` 后台线程版，事件循环上的 7 处调用点换过去；`wake_sdk` 那处要 ok 值
  记 wake_log，保留同步（每次醒来一发）。
- **扫同类**：`pipeline.ombre_alive`（1.5s 探活，`_open_session` 上）、`browser_keeper.apply_choice`
  的 pgrep（每轮 finalize）——**这次不动**：都 <2s 且有超时，先看 game/Bark 两处改完的效果。
  插件仓 `game_session_mcp.py` 同款（sips 临时文件 + 阻塞）不在本仓。
- 顺手：`_shot_bytes` 的 sips 失败路径临时 PNG 不清（P3）进了 finally；`mail_bridge._imap`
  加 `timeout=30`（「闲置致死」类：半开连接让唯一的 watcher 线程永久卡 recv）+ select 失败
  logout + search 非 OK 打日志。
