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
