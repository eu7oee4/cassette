# UI.md：cassette 视觉规范（用色 / 深色模式 / 现状盘点）

> UI 专用文档。以后改颜色、圆角、间距，先在这里定规则，再动代码。
> `PLAN_chatui.md` §1–§2 是 09-01 定下的旧用色口径，**有一部分已经和代码对不上**（见 §0.3）。
> 新规则定下来以后以本文为准，PLAN_chatui 那边只留历史。
>
> 本文所有行号是 2026-10-08 实读的，基于 10-08 的聊天页简洁化改动（未提交）。

---

## 0. 现在代码里有哪些色（`ios/cassette/Theme.swift`）

### 0.1 Token 一览

| Token | 值 | 现在拿来干什么 |
|---|---|---|
| `ChatPalette.bg` | 浅色 `#FFFFFF`；深色 `systemGroupedBackground`（≈纯黑） | 聊天区底色、背景图上的蒙版色 |
| `ChatPalette.bubbleAI` | 浅深**都是** `#E9E9EB` | TA 的气泡 |
| `ChatPalette.textOnDark` / `textOnLight` | 白 / 黑 | 只经由 `ChatTint.text` 间接用到 |
| `ChatPalette.theme` | `#352526`（深梅） | **一处都没用**（09-01 拍板「保留但别硬塞」） |
| `CharPalette.base` | 每个人物一色，见 0.2 | 头像描边、名字、PR 小圆点、默认头像 |
| `CharPalette.light` / `.dark` | 每个人物一套，见 0.2 | `.dark` 在 10-08 之后**没人用了**；`.light` 只在深色模式的我方气泡/卡片上用 |
| `CharPalette.bullet` | 和 base 同值 | **一处都没用**（PLAN_chatui §8 待定） |
| `CharPalette.logo` / `logoSymbol` | ✦ / 🐾 | 小字提醒前缀（跟灰字走，不吃颜色） |
| `ChatTint.fill` | 浅色 → **黑**（10-08 改）；深色 → 人物色 `.light` | 我方气泡、权限卡、问答卡、我发的文件/网页卡 |
| `ChatTint.text` | 浅色 → 白；深色 → 黑 | 上面这些东西上的字 |
| `Color.userAccent` | = 机主人物色 base `#AC7F5E` | **全 app 的 `.tint`** + 其他页面一大堆按钮/角标（见 §2） |
| `Color.chatInk` / `onChatInk` | `label` / `systemBackground`（浅色黑/白，深色白/黑） | 10-08 新加：聊天页的功能色 |
| `Color.noteGray` | `#828282`，不随深浅变 | 小字提醒、系统消息、居中时间、在场状态行 |
| `HousePalette`（`Color.house`） | 现用 `oatMilk`（另有 `honeyCaramel`、`roseAsh`） | 小屋/房间页整套，**不跟系统深浅色走** |
| 代码块底 | `#292B36`（atom-one-dark），写死在 `MarkdownMessageView.swift:127` | 代码块卡片 |

### 0.2 人物色表（`IdentityColor.palette(for:)`）

| 人物 | base | light | dark | 备注 |
|---|---|---|---|---|
| 机主 `user` | `#AC7F5E` | `#FDEBEB` | `#8C6E6E` | base 同时就是 `userAccent` |
| 小卡 `default` | `#7FA8C9` | `#B2CBDE` | `#303030` | |
| Cassius `cass` | `#CB4C09` | `#F0FCD9` | `#420217` | light 是浅绿，和 base 琥珀不是一个色相 |
| 其他角色 | 按 id 哈希出色相，固定饱和度/亮度 | | | 新角色没配时的兜底 |

### 0.3 文档和代码对不上的地方

- PLAN_chatui §1.4 / §2 写的机主色 `#B86166`、Cassius `#C9A15B`，代码里实际是 `#AC7F5E`、`#CB4C09`。
- PLAN_chatui §1.4 写 `Color.theme = ThemePalette.plumTint.accent`，代码里已经没有 `ThemePalette` 和 `Color.theme`，只剩 `ChatPalette.theme` 这个没人用的字段。
- PLAN_chatui §1.2 「浅色用人物色 dark 当底」10-08 已改成黑底。

---

## 1. 深色模式问题（先记录，暂不修）

按严重程度排。全是读代码推出来的，**没有在深色模式下实际截图验证过**。

| # | 问题 | 位置 | 严重度 |
|---|---|---|---|
| D1 | **TA 的气泡读不清**：`bubbleAI` 深色也是浅灰 `#E9E9EB`，但字色走 `.primary`，深色下是白字 → 浅灰底白字。流式气泡、定稿后的 Markdown 气泡都这样 | `Theme.swift:23`（`ChatPalette.dark.bubbleAI`）；`ChatView.swift` `streamingBubble` 的 `bubbleText ?? .primary`；`MarkdownMessageView.swift:44` `base = textColor ?? .primary` | 🔴 基本不能用 |
| D2 | **气泡里的行内码颜色反了**：行内码固定 `.primary` 字 + `systemGray4` 底。深色下是白字配深灰块，坐在浅灰的 TA 气泡里、或浅粉的我方气泡里，像一块补丁 | `MarkdownMessageView.swift:61-66` | 🟠 |
| D3 | **我方气泡/卡片深浅模式不是一套语言**：浅色已经统一黑底白字；深色还是走人物色 `.light`（机主是浅粉 `#FDEBEB`，权限卡/问答卡按角色是浅蓝或浅绿），黑字 | `Theme.swift` `ChatTint.fill` | 🟠 等用色规则定了一起改 |
| D4 | **斜体旁白在 TA 气泡里更淡**：`*动作*` 用 `base.opacity(0.5)`，D1 修好之前是白字半透明压在浅灰上，几乎看不见 | `MarkdownMessageView.swift:55-58` | 🟠（随 D1 一起） |
| D5 | **跳转高亮是一道白光**：从聊天记录跳过来，整行闪 `Color.white.opacity(0.85)`，深色下刺眼，而且会把白字气泡冲掉 | `ChatView.swift` `.background { if flashRowId == row.id { Color.white... } }` | 🟡 |
| D6 | **TA 的文件/网页卡**：底是 `systemBackground`（深色下纯黑），坐在 `systemGroupedBackground`（也接近黑）上，只靠 0.5pt 的 `systemGray4` 描边分开，卡片轮廓很弱 | `ChatView.swift` `attachmentCard` | 🟡 |
| D7 | **长按菜单**是写死的深灰 `#1C1C1E` 浮条，深色背景上轮廓弱；气泡压暗用的是 `gray 28%`，深色下几乎看不出 | `ChatView.swift` `BubbleMenuBar` / `BubbleMenuModifier` | 🟡 |
| D8 | `ChatPalette.dark` 里 `textOnDark` / `textOnLight` / `theme` 跟浅色那份一模一样，「深浅各一份」只有 `bg` 真的不同 | `Theme.swift:21-27`（`ChatPalette.dark`） | ⚪ 结构问题，改规则时一起理 |
| D9 | 10-08 新加的 `chatInk` 在深色下是白色：发送按钮变成白底黑箭头，顶栏的 Code / 急停开关「开着」时是白胶囊。逻辑上没错，但没看过实际效果 | `Theme.swift` `chatInk`；`InputBar.swift`；`ContentView.swift` 顶栏 | ⚪ 待看 |
| D10 | 小屋（`Color.house`）固定是浅色奶咖系，系统切深色时它不变。这是 09 月有意设计的（「独立的暖色世界」），但从深色聊天页切到小屋会很晃眼 | `HousePage.swift:5` 注释 | ⚪ 设计取舍，不一定算 bug |

不受影响、深色下没问题的：代码块（写死的深色底）、`noteGray` 小字（`#828282` 在黑底上对比度约 5:1）、输入框（`systemGray6`）、图片/表情。

---

## 2. 人物色 / 主题色用到的所有地方

「性质」一列是我的判断，改规则时最关键的就是这一列：
- **身份**：这个颜色在回答「这是谁」，换成别的人物就该换色。
- **功能**：这个颜色只是「这东西能点 / 被选中 / 很重要」，跟是谁无关，现在用人物色只是因为 09-01 拍过「凡要用色的地方一律用机主人物色」。

### 2.1 全局

| 位置 | 用的什么 | 干什么 | 性质 |
|---|---|---|---|
| `cassetteApp.swift:8` | `.tint(Color.userAccent)` | **全 app 默认控件色**：所有没显式指定颜色的 Button、Toggle、Picker、链接、`.borderedProminent`、导航返回键、`ProgressView` 都是棕色 | 功能 |
| `DraftsPage.swift:217, 272`；`MemoryPage.swift:222, 229`；`MindPage.swift:75` | `.tint(.red)` | 因为全局 tint 把删除按钮的默认红也盖成了棕色，这 5 处是手动压回红色的补丁 | （副作用） |
| `MarkdownMessageView.swift:51` | `.accentColor`（= 全局 tint） | **TA 气泡里的链接色**（我方气泡里链接跟字色走） | 功能 |

### 2.2 聊天页（10-08 已经改过一轮）

| 位置 | 用的什么 | 干什么 | 性质 | 状态 |
|---|---|---|---|---|
| `ChatView.swift` `MessageRow.bubbleFill` | `ChatTint.fill(palette("user"))` | 我方气泡底 | 身份→已改功能 | 浅色已改黑；深色仍是人物色 |
| `PermitCardView.swift:20-22` | `ChatTint.fill(palette(charID))` | 权限卡底（**按角色**取色，不是按机主） | 身份 | 浅色已改黑；深色仍是角色色 |
| `QuestionCardView.swift:28-30` | 同上 | 问答卡底 | 身份 | 同上 |
| `ChatView.swift` `CharLogo` | `palette(senderID).logo/logoSymbol` | 小字前缀 ✦ / 🐾（只取符号，颜色跟灰字走） | 身份 | 不动 |
| `ChatView.swift` `AvatarView` | `userAccent` | 机主**默认头像**（没传照片时的底色）；TA 的默认头像是 `systemGray3` | 身份 | 不动 |
| `ChatView.swift` 浏览灰字展开的网址 | 原 `userAccent` | 网址链接 | 功能 | 10-08 改成 `chatInk 70%` |
| `ContentView.swift` 顶栏猫爪、未读点、Code 开关、急停开关 | 原 `userAccent` | 按钮/开关 | 功能 | 10-08 改成 `chatInk` |
| `ContentView.swift` 顶栏在场状态行 | 原 `userAccent` | 「正在电脑前」这类小字 | 功能 | 10-08 改成 `noteGray` |
| `InputBar.swift` ➕ / 表情 / 发送 / 待发文件图标 | 原 `userAccent` | 输入栏按钮 | 功能 | 10-08 改成灰 / `chatInk` |
| `ChatView.swift` 新消息胶囊、文件/网页卡图标 | 原 `userAccent` | | 功能 | 10-08 改成 `chatInk` / 灰 |

### 2.3 其他页面（还没动过）

| 位置 | 用的什么 | 干什么 | 性质 |
|---|---|---|---|
| `DrawerMenu.swift:78` | `userAccent` | 抽屉菜单每一行的图标 | 功能 |
| `DrawerMenu.swift:89` | `userAccent` 胶囊底 + 白字 | 抽屉菜单行上的数字角标 | 功能 |
| `DrawerMenu.swift:116` | `userAccent 50%` | 未完成页面的占位大图标 | 功能 |
| `ConversationsPage.swift:29` | `userAccent` | 会话列表里的在场状态行（聊天顶栏同款已改灰） | 功能 |
| `ConversationsPage.swift:47` | `userAccent` 胶囊 + 白字 | 未读数角标 | 功能 |
| `ConversationsPage.swift:52` | `userAccent` | 当前会话的 ✓ | 功能 |
| `ConversationsPage.swift:87, 91` | `userAccent 15%` 底 + `userAccent` 爪印 | 角色**默认头像**（没传照片时） | 身份（但用的是**机主**色，不是该角色的色） |
| `HistoryPage.swift:164` | `userAccent` 底 | 窗口条数超限时的数字块 | 功能（警示） |
| `HistoryPage.swift:536` | `userAccent` / `systemGray3` 圆点 | 聊天记录里「谁说的」小圆点：我=棕，TA=灰 | 身份 |
| `HistoryPage.swift:593` | `.tint(userAccent)` | 「保存」按钮（`.borderedProminent`） | 功能 |
| `GamePage.swift:193` | `userAccent` 胶囊 + 白字 | 底部浮出的提示条 | 功能 |
| `GamePage.swift:339` | `userAccent` | 预设单选的选中圈 | 功能 |
| `GamePage.swift:376` | `userAccent` | 任务多选的选中勾 | 功能 |
| `GamePage.swift:391` | `userAccent` | 「有自定义选项」的齿轮 | 功能 |
| `GamePage.swift:426` | `userAccent` | 选项当前值文字 | 功能 |
| `JobhuntPage.swift:166` | `userAccent` / 灰 | 匹配分 ≥70 高亮 | 功能（状态） |
| `JobhuntPage.swift:181, 316` | `userAccent` | 「有回信」「HR 回信」标签 | 功能（状态） |
| `JobhuntPage.swift:271` | `userAccent` / 红 | 「恢复」按钮（对应的「标不投」是红） | 功能 |
| `MemoryPage.swift:171, 174` | `userAccent` 字 + `userAccent 12%` 胶囊底 | 排序切换按钮 | 功能 |
| `MemoryPage.swift:298, 351` | `userAccent` | 置顶 📌 图标 | 功能（状态） |
| `MindPage.swift:171` | `userAccent` | 网址链接 | 功能 |
| `PluginsPage.swift:204` | `userAccent` | 「可更新」小字 | 功能（状态） |
| `PluginsPage.swift:232` | `.tint(userAccent)` | 「安装」按钮 | 功能 |
| `PluginsPage.swift:240, 261` | `.tint(userAccent)` | 启用开关、唤醒开关（Toggle 打开时的颜色） | 功能 |
| `PluginsPage.swift:276` | `.tint(userAccent)` | 「更新」按钮 | 功能 |
| `PluginsPage.swift:413` | `userAccent` | 下拉选择器文字 | 功能 |
| `PRsPage.swift:246` | `userAccent` | 「重启后端」按钮 | 功能 |
| `PRsPage.swift:387, 489` | `IdentityColor.color(for: char)` | PR 时间线 / 详情里「哪个角色提的」小圆点 | 身份 ✅ |
| `WebpagesSheet.swift:60` | `userAccent` | 网页列表行图标 | 功能 |
| `OnboardingView.swift:23` | `userAccent` | 首启引导的磁带大图标 | 功能（品牌） |
| `OnboardingView.swift:73` | `userAccent` | 选头像占位里的 ➕ | 功能 |

### 2.4 小屋（独立配色，用的是 `Color.house`，不是人物色）

小屋两个页面一共约 106 处 `Color.house.*`（`HousePage.swift` 46 处，`RoomPage.swift` 60 处），全部走 `HousePalette`，换主题只要改 `Theme.swift` 里 `static let house = ...` 这一行。在小屋里真正用到人物色的只有下面两处：

| 位置 | 用的什么 | 干什么 | 性质 |
|---|---|---|---|
| `HousePage.swift:624` | `IdentityColor.color(for: entityID)` | 房间里头像的 2pt 描边 | 身份 ✅ |
| `RoomPage.swift:492-534` | `IdentityColor.color(for: ev.actor)` | 房间事件流：名字颜色、气泡 26% 底 + 35% 描边 | 身份 ✅ |

### 2.5 小结

- **真正在表达「这是谁」的**只有：头像描边、默认头像、小字 logo、PR 小圆点、聊天记录小圆点、小屋事件流，加上深色模式下的我方气泡和卡片。
- **其余三十多处都是功能色**，现在用棕色只是因为「全局 tint = 机主人物色」。如果规则改成「功能色 = 黑白」，大部分只要改 `cassetteApp.swift:8` 这一行加一个新 token，再把显式写了 `userAccent` 的地方批量换掉。那 5 个 `.tint(.red)` 补丁也可以一起删掉。
- 有两处身份色用错了人：`ConversationsPage.swift:87, 91` 的角色默认头像用的是**机主**色；`HistoryPage.swift:536` 的 TA 圆点用的是灰，不是该角色的色。
- 没在用的字段：`ChatPalette.theme`、`CharPalette.bullet`，以及 10-08 之后的 `CharPalette.dark`。

---

## 3. 改用色规则前要拍板的几件事

（你直接在这一节写答案就行）

1. **功能色用什么？** 全 app 统一黑白（和聊天页一致），还是留一个强调色？如果留，用哪个色？用 `ChatPalette.theme #352526`，还是另选一个？
2. **人物色只留给「身份」吗？** 即只用在头像描边、默认头像、logo、各种「谁说的」圆点和名字上。
3. **深色模式的我方气泡和卡片**：反白（白底黑字），还是深灰（`#2C2C2E` 加白字），还是继续用人物色 `.light`？
4. **权限卡/问答卡要不要再按角色区分颜色？** 浅色已经统一黑色了，多角色同时在线时是否需要一眼看出是谁要权限？可以改成卡片上加一条角色色细边或者头像。
5. **状态色**（匹配分高、有回信、可更新、置顶）：跟功能色走黑白，还是单独给一个状态色？
6. **小屋要不要跟系统深浅色走？** 可以让 `roseAsh`（暖黑）在深色模式下自动接替。
7. **`CharPalette` 的 `light` / `dark` / `bullet` 还要不要？** 如果第 3 条不用人物色，这三个字段可以删，人物色就只剩 `base` 一个值。
