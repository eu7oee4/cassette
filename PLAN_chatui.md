# Plan：聊天界面 UI 重做——一套色 token，一套圆角，一套卡

> 2026-09-01 下午立稿。机主逐条口述、本轮实读代码核对，**全部未动工**。
>
> **这份 plan 管什么**：聊天页里看得见的一切——配色 token、气泡、卡片、
> 小字提醒、媒体、系统消息、流式的分裂方式、卡片选完之后的收起。
>
> **不管什么**：权限卡的**后端通路**（那是 [PLAN_native](PLAN_native.md) §1/§6，
> 这里只管它长什么样）；小屋/房间的配色（`HousePalette` 是独立的一套暖色世界，
> 不随系统深浅走，这次不动它）。
>
> **写给施工者的三条规矩**（照抄 PLAN_native 卷首，同样成立）：
> 1. 正文只写新形状，它就是完整设计，不是对现状的补丁。
> 2. 现状只出现在 §7（差距表）和 §10（实读记录）两处。
> 3. 撞见正文没提的旧样式，默认它属于要改的那一堆——别在新代码里留
>    「这里我们保持原样，因为…」的注释墓碑。

---

## 0. 一句话

**AI 说的话进灰气泡，机主说的话进染色气泡，要机主动手的东西进染色卡片，
其余一切（媒体、文件、网页）不染色，做过什么用灰色小字说。**

三条总纲：

1. **染色只有两个地方**：机主的气泡、需要交互的卡片。AI 气泡统一灰底——
   气泡和卡片都染色太花，而机主天然没有卡片，两边正好各占一样。
2. **圆角只有一个数**：对准气泡圆角，全局一致（头像是圆的，天然不参与）。
3. **小字和系统消息直接坐在背景上**，不进任何容器——所以它们的颜色必须
   在深浅两种背景、以及任何自定义背景图上都读得清。

---

## 1. 色彩 token

### 1.1 机主给的命名 → 代码里的落点

机主口述的名字保留在左列（改 md 时照这个说话），右列是代码里建议的形状。

| 机主命名 | 代码建议 | 说明 |
|---|---|---|
| `bg_light` / `bg_dark` | `ChatPalette.bg` | 浅/深各一份，深浅模式切换时整套 palette 换 |
| 背景图 | `ChatPalette.bgImage` + `bgImageDim` | 浅深各可自定义一张；`bgImageDim` 是蒙版透明度滑块（见 §1.3） |
| `text_light`（默认白） | `textOnDark` | ⚠️ 命名反直觉：`text_light` 是**浅色的字**，用在深底上 |
| `text_dark`（默认黑） | `textOnLight` | 同上，深色的字用在浅底上 |
| `theme` | `ChatPalette.theme` | 主题色。**保留但暂时无处可用**——见 §1.4 |
| `theme_light` / `theme_dark` | `theme.light` / `theme.dark` | 同一色相的浅身/深身 |
| `theme_logo` | `theme.logo` | 机主的符号，**待定**（§8） |
| `char1`, `char1_light`, `char1_dark`, `char1_bullet` | `CharPalette { base, light, dark, bullet }` | 按 charID 查表，见 §1.2 |

代码形状：

```swift
struct CharPalette {
    let base: Color      // 人物色本体（头像描边、名字）
    let light: Color     // 浅身：深色模式下的气泡/卡片底
    let dark: Color      // 深身：浅色模式下的气泡/卡片底
    let bullet: Color    // 列表符号等气泡内小装饰（用途待定，见 §8）
    let logo: String     // 小字前缀符号
}
```

### 1.2 染色的唯一口径（背下来，全局只有这一条）

> **浅色模式** → 用人物色的 `dark` 当底 + `text_light`（白字）
> **深色模式** → 用人物色的 `light` 当底 + `text_dark`（黑字）

适用面：**机主的气泡** + **染色卡片**（permit 卡 / 问答卡）。
其余一律不套这条：AI 气泡是灰底，文件卡/网页卡是白底，图片表情不染。

### 1.3 背景图与对比度

- 浅色模式、深色模式各可自定义一张背景图。
- 配一个**蒙版透明度滑块**（`bgImageDim`）：背景图上盖一层 `bg` 色的半透明层，
  滑到底＝纯色背景。
- 为什么必须有：气泡和卡片本来就全不透明，图只在缝隙里露；但**小字提醒和
  系统消息是直接坐在背景上的**，字色固定灰，遇到高对比度的照片会直接读不出来。
  蒙版是唯一一个滑块就能救全场的做法。

### 1.4 主题色和机主人物色是两个色，不合并（09-01 拍板）

代码里现在这是两个不相干的值：

- `Color.theme` = `ThemePalette.plumTint.accent` = `#352526`（深梅，几乎是黑）
- `IdentityColor.color(for: "user")` = `#B86166`（玫瑰）——小屋里机主的身份色

**拍板：不合并，两个都留。**

- **这份 plan 里凡是要用色的地方，一律用机主人物色**（机主气泡染色、链接色、
  默认头像、按钮/角标那些现在写着 `Color.theme` 的位置）。
- **`theme` token 保留在 palette 里，暂时没地方用到也行。** 别为了「让它有用」
  硬塞进某个元素——空着比乱用强，以后想起来要用再接。

施工含义：现有代码里 `Color.theme` 的每一处引用都要过一遍，判断它是「机主的身份」
（→ 换成机主人物色）还是「产品的主题」（→ 留着 `theme`）。大部分是前者。

---

## 2. 角色标识

| 角色 | 人物色 | logo |
|---|---|---|
| 小卡（`default`） | 不变（雾蓝 `#7FA8C9`） | 🐾 **用 SVG 画一个实色的**，不用系统 emoji |
| Cassius（`cass`） | 不变（琥珀 `#C9A15B`） | ✦ |
| 机主（`user`） | 玫瑰 `#B86166`（现有 `IdentityColor("user")`，**不是** `theme`） | 待定（§8） |

- 🐾 要自己画 SVG 的原因：emoji 是彩色位图，跟不上人物色，也跟不上灰字色。
  SVG 单色路径可以直接染成当前该用的颜色。iOS 侧落成 asset（`template` 渲染模式）
  或 `Path`，两者都行。
- **头像 ≠ logo**，两套东西别混：头像是可换的照片（`ProfileStore.avatar`，圆形 34pt），
  logo 是小字提醒前面那个符号。

---

## 3. 聊天流里的每一种东西

### 3.1 气泡（对白）

- 流式逐字长出来。
- `\n` = 段内换行（现有 `hardenLineBreaks` 已经在做）。
- `\n\n` = **换一个气泡**（新口径，见 §4）。
- **AI 气泡：统一灰底**，跟是哪个角色无关。
- **机主气泡：染色**，按 §1.2 那条口径。
- 气泡内 markdown 照旧（链接/行内码/列表/引用/表格都留在气泡里）。
  - **链接色 = 机主人物色**（不是 `theme`，见 §1.4）
  - **行内代码底色 = 深灰/炭黑，固定不变**（不随深浅模式、不随人物色）

> 附带的好处，记一笔：因为 AI 气泡回到灰底，「气泡内 markdown 要在六种底色上
> 各验一遍」这件活自动消失了。只剩染色卡片内部的文字要验。

### 3.2 `*斜体*`（roleplay 动作）

- 单独渲染成一块，**不放在气泡里**。
- **UI 没想好** → 待定（§8）。这一条不定，§3.1 的段落切分要先绕开它。

### 3.3 代码块

- **按原来的方式渲染**：深色卡（atom-one-dark `#282c34`）+ 顶栏语言标签 + 一键复制
  + 横向滚动 + Highlightr 语法高亮。脱出气泡，宽度取气泡列宽，靠发送者那侧。
- 唯一要跟着改的是圆角（现在 10，要对齐到气泡圆角，见 §6）。

### 3.4 小字提醒

**形状**：`{char_logo} {内容}`，固定灰色，直接坐在背景上，**靠左**（跟系统消息的
居中区分开——小字提醒是某个角色做的事，系统消息是 app 自己说的话）。

**颜色**：固定灰色，只保证深浅两种背景上都看得清（背景图靠 §1.3 的蒙版兜底）。

**动效**：**模型还在生成、但下一句还没跟上来时闪烁**——柔和的呼吸感，隐隐灭灭，
不要粗暴地闪。参考 http://orbs.jakubantalik.com 的质感。
（如果自定义 logo 前缀实现不了，就直接用那个 orbs 的形状当指示。）

**包括这三类**：

1. **「生成中」**——取代现在的三个跳点。
   `🐾 cassette 正在思考…` / `✦ Cassius 正在思考…`
2. **所有「工具调用」**——如 `🐾 mcp_mail_read`（举例）。
   ⚠️ 这条不只是 UI，牵动后端口径，见 §7.2。
3. **「next_wake」**——如 `✦ Cassius 决定下次21:13醒来`。

**唯一的例外：游戏工具族不上屏**（09-01 拍板）。
`tap` / `look` 那一族（`mcp__game__*`，游戏进行中的操作）**一条小字都不发**——
它们太密，每两个气泡之间夹一串，聊天流会被淹掉。**要做后面再补**（怎么补见 §8）。

- 游戏轮里的静默不是死寂：「正在思考…」那条呼吸小字（本节第 1 条）照常在。
- ⚠️ 边界要划准：例外指的是**游戏进行中的操作工具**。
  `task_run`（派引擎跑日常，机主要核对任务清单）和 `game_start`（控制信号）
  不属于这一族，照旧出小字。施工时按工具名前缀判，别按插件名一刀切。

### 3.5 卡片（要机主动手的）

- **染色**，按 §1.2 那条口径。
- **二选一的按钮横着排；两个以上的选项按钮竖着列。**
- **选完之后收起来**，不占聊天空间——收成什么见 §5。
- 包括：
  1. **permit 请求卡**：选项描述**照搬 SDK 原生的**（`ctx.title` / `display_name` /
     `description`，见 PLAN_native §6）。
  2. **问答选项卡**，两个已知来源：
     - 场景 1：SDK 原生的问问题选选项功能
     - 场景 2：之前装的 grill-me skill
     - 可能还有别的场景 → 卡片做成通用形状，别绑死在某一个来源上。

### 3.6 图片

- **不染色**，搬现有的。
- **堆叠卡**（连续 ≥2 张同发送者收成一摞）搬现有的。
- **方形圆角裁剪**（现在就是 160×160 `scaledToFill` + 圆角，保留）。
- 圆角对齐到气泡圆角（§6）。

### 3.7 表情包

- **搬现有的**（自适应比例，不裁方——透明底/长条表情裁方会毁）。
- **后期优化目标：透明底 + GIF**。不在这一期。

### 3.8 文件卡 / 网页卡

- **都白底**（现在就是 `systemBackground` 实底 + 细描边，保留）。
- **文件名一行**，过长压缩，**但保留后缀**：

  ```
  📄 文件一份资… .pdf
  ```

- 圆角统一，**靠发送者那侧**。
- 实现要点：中间截断（`.truncationMode(.middle)` + `.lineLimit(1)`）。
  现在是 `lineLimit(2)` 且后缀另起一行显示，要改。

### 3.9 系统消息

- **居中灰字**，带破折号包裹：

  ```
  ——游戏急停：已叫停——
  ```

- 无 logo、无头像、无气泡。它是 app 说的话，不属于任何角色——这也是它跟
  §3.4 小字提醒的分界（后者靠左带 logo）。

---

## 4. 流式：前一个气泡定型，新气泡长出来

### 4.1 能不能做到——能，而且这条路现在就在跑

实读结论（不是推断，行号见 §10）：

**「当前气泡定稿保留、下一段另起一个新气泡」这个机制已经完整实现并在生产跑着**，
只是触发条件是**工具调用切段**，不是 `\n\n`：

```
后端 sse.py:126    新的 text 内容块开始 → yield {"type":"text_break"}
app  ChatService   text_break → StreamEvent.textBreak
app  ContentView   .textBreak → 当前气泡 editText 落盘定稿 → streamingId = nil
                   → 下一个 .text 片段到达时新建一条 message，气泡从下面长出来
```

所以问题不是「能不能」，是「按 `\n\n` 切的那一刀切在哪一层」。

### 4.2 三条路，推荐第三条

**路 A · 流式时切成多条 message**（在 `.text(chunk)` 分支里检测 `\n\n`，
复用现成的定稿+另起代码）

- 优点：跟 `text_break` 完全同构，改动集中在一处。
- 代价三条，都不轻：
  1. 必须同时把 `sawBreak` 置真，否则 `finalizeStreamedReply` 会走单段路、
     拿 `resp.reply`（全文）覆盖最后一个气泡 = **前面的话重复一遍**。
  2. 一轮变成多条 assistant message → 发回后端的历史条数膨胀，
     `sendHistoryCap`（默认 100 条）能装的轮数变少。
  3. 老历史和断连补投（`insertProactive` 整段存一条）不走这条路，
     形状对不上——同一段话「当时看到的」和「重进 app 看到的」不一样。

**路 B · 后端按 `\n\n` 发 text_break**

- 不推荐。`sse.py` 里 `raw_segments` 正是用 `"\n\n".join` 拼回全文的，
  按 `\n\n` 切会和这套拼接逻辑打架；而且要和 `MarkerStreamFilter` 的缓冲交互。

**路 C · 只在渲染层切，消息模型一行不动 ← 推荐**

- 一条 message 照旧存整段原文；`MessageRow` 把它渲染成**多个气泡**。
- 定稿态：`messageSegments` 从「所有散文块合成一个气泡」改成
  **每个顶层块各自一个气泡**（代码块照旧脱出成卡）。
- 流式态：`streamingBubble` 也切——用便宜的 fence-aware 字符串切分（见 4.3），
  每段一个纯 Text 气泡，**呼吸点只挂最后一段**。
- 优点，条条都在解路 A 的代价：
  1. `sawBreak` / `finalizeStreamedReply` 一行不用动。
  2. 历史条数不变，`sendHistoryCap` 不受影响。
  3. **老历史和断连补投自动跟着一致**，不用单独补。
  4. 流式期间万一切错（见 4.3），定稿时块级解析会自动纠正，**错误不落盘**。
  5. 消息在存储层仍是模型写出来的那一整段——**重铸「经历逐字保留」的口径不被破坏**。
- 代价：一条消息内的多个气泡在长按删除/编辑时是一个整体（删一个＝删整条）。
  ——这跟现在「气泡+代码块」多段消息的行为一致，不是新问题。

### 4.3 三个必须写清的坑

1. **代码围栏里的空行不许切。** 流式期间还没解析 markdown，盲切 `\n\n` 会把
   带空行的代码块劈成两半。切分器要跟踪 ``` 的奇偶，围栏内不切。
2. **列表/表格不许被劈开。** 这正是定稿态要按**顶层块**切、而不是按字符串
   `\n\n` 切的原因：markdown 的松散列表（项之间有空行）源码里全是 `\n\n`，
   但它是一个块。按块切天然等价于「`\n\n` 换气泡」，且不会误伤。
3. **`*斜体*` 块（§3.2）还没定 UI。** 它一旦要脱出气泡，就成了 `messageSegments`
   的第三种 segment。切分器的形状要给它留位置，别写死成「气泡/代码卡」二选一。

---

## 5. 卡片选完之后收起来

四条，都按机主原话：

1. **permit 请求卡**
   - **放行后** → 直接变成小字提醒「命令调用」
   - **拒绝后** → 变成小字提醒「生成中」
2. **问答选项卡**
   - 连续问答时：一张选完，这张变成小字提醒
     `眠眠选择了「什么什么…」`（**只保留所选选项的前几个字**），
     然后**自动浮现下一张**。
   - **可以带自定义答案的输入框**（功能上和 Claude 原生一样，UI 要我们自己做）。
3. **超时未选择** → 整张卡**置灰**，**左上角标「已超时」**。
4. 收起后的小字提醒走 §3.4 那套（logo 前缀 + 固定灰）。

---

## 6. 圆角与几何

**一个数**：气泡圆角（现在是 18，`.continuous`）。下面这些全部对齐到它——

| 元素 | 现在 | 改成 |
|---|---|---|
| 气泡 | 18 continuous | 18（基准，不动） |
| 代码块卡 | 10 | 18 |
| 图片 / 图片堆叠 | 16 | 18 |
| 表情包 | 16 | 18 |
| 文件卡 / 网页卡 | 16 | 18 |
| permit 卡 / 问答卡 | 新建 | 18 |
| 卡片里的按钮 | 新建 | 待定（§8）——按钮通常要比容器小一号才不显笨 |
| 输入栏 | 待核 | 18 |
| 头像 | 圆形 | 圆形（不参与） |

---

## 7. 跟现状的差距

### 7.1 三档工作量

**① 搬现有的（几乎不用动）**
图片 + 堆叠卡、表情包、代码块渲染、系统消息居中灰字、文件卡/网页卡的白底质感、
`text_break` 那条定稿/另起的流式通路。

**② 改现有的**
- `ThemePalette` 只有两个字段（`accent` / `bubbleMe`），要扩成 §1.1 那套完整 token，
  并且**要有浅深两份**——现在全靠系统语义色（`systemGray5` / `systemGroupedBackground`）
  自动跟随，没有任何显式的深浅分支。
- `messageSegments`：散文块合并 → 每块一个气泡（§4.2 路 C）。
- `streamingBubble`：单个纯 Text → fence-aware 多段（§4.3）。
- 文件卡：文件名两行 + 后缀另起一行 → 一行中间截断保留后缀（§3.8）。
- 全局圆角（§6）。
- markdown theme：链接色改主题色、行内码底色固定炭黑（现在是 `systemGray4`，
  会跟着系统深浅漂）。
- 机主气泡：`Color.bubbleMe`（30% 透明的浅紫）→ §1.2 的实底染色 + 反色字。

**③ 零行新建**
- 深浅两套 palette 的显式切换（现在完全没有 `colorScheme` 分支）。
- **自定义背景图 + 蒙版透明度**（现在全仓零行）。
- **permit 卡 / 问答卡**（iOS 全仓无 `permit`，PLAN_native §10 已核）。
- 🐾 的 SVG 资源。
- 小字提醒的 logo 前缀 + 呼吸动效。

### 7.2 ⚠️ 「所有工具调用都出小字」不是 UI 改动

现在灰字**只对写类工具的白名单**发（`pipeline._stored_from_tool_use`）：
hold/grow/trace/I/webpage_write/code_start/task_run/game_start/mail_send。
读类（breath、mail_read、Read/Grep/Glob…）**一条都不发**，注释里写死了
「只记写操作，breath 等读操作不算产物」。

所以 §3.4 第 2 条要成立，得改后端下发口径。两件事要先想清楚：

1. **量**。最密的那一族（游戏 `tap`/`look`）已经按 §3.4 那条例外整个不上屏了，
   所以这条不再是拦路的。剩下的是 PLAN_native 落地后常驻直放的 Read/Grep/Glob——
   一轮翻十个文件就是十条小字。
   **聚合因此是优化项，不是前置项**：想做的话，现有的「浏览了 N 个网页 ›」灰字
   就是现成的聚合形态（点开展开明细），直接照抄这个形状。→ 规则待定（§8）。

   ⚠️ **更正一句我自己写错的**：初稿这里写着「breath 每轮开场必调」——**错的**，
   而且这个口径 08-31 就跟着 PLAN_sdk §5.2「breath 瘦身」改了：常驻 session 之后
   breath **只在会话刚开、或者刚重铸完那一轮**做。我是从 `tool_menu.example.md`
   读来的，而那份正是没改干净的两处之一（见下）。
2. **失败态会掉一层信息**。现在的文案是人话且带原因（「想寄一封邮件，但没成：
   配额满了」）；换成裸工具名 `🐾 mcp_mail_send` 之后，「没成」和它的原因往哪儿放。
   ——这条正好接上今天上午刚落的 `ret` 那一列（`ok` 只证明协议层没报错，
   办没办成看回执原话，见 `41a4e82` / `80f877d`）。小字提醒是 `ret` 的第一个
   合适的读者。

→ **09-01 已拍板**，结论在 §8.C：裸名 / ret 追加原因 / 读类跟聚合绑下期
（所以后端下发面这期没扩，只是条目多带了 `name`）。

### 7.2.1 顺手挖出来的：breath 口径有两处没改干净（09-01 实读）

08-31 那次「breath 瘦身」（只在刚开/刚重铸那一轮做）没改全。**这跟 UI 无关，
但既然撞上了就记在这儿，别让它继续骗下一个读到的人**：

**2026-09-01 20:4x 已全部改完**（眠眠让改的，五处）。下表留作记录：

| 位置 | 原话 | 处置 |
|---|---|---|
| `characters/cass/persona.md:104` | 「每次开场先 breath 让记忆浮现」 | 改成「接上断口的那一轮…会收到一句提醒」 |
| `tool_menu.example.md:54` | 「**每轮开场先 breath 一次**——这是习惯动作」 | 整段换成两个角色 tool_menu 的现行口径 |
| `tool_menu.example.md:38`、`characters/{default,cass}/tool_menu.md:38` | 体例警告：「breath / hold 这种**每轮都该做**的事」 | 改「**不由意图触发**的事…正文里把触发条件写死」 |
| `tool_menu.example.md:70`、`characters/{default,cass}/tool_menu.md:72` | hold 块：「**跟浮记忆一样**是习惯动作」 | 改「这是**习惯动作**」 |

**08-31 那次为什么没改干净——形状值得记：**

- 改的人（我）只改了「## 开口之前，先浮一次记忆」**那一块的正文**，没搜同一份文件里
  **别处提到 breath 的地方**。于是两个角色的 tool_menu 里，第 72 行「跟浮记忆一样是
  习惯动作」往上翻 16 行就是第 56 行「不用每轮再浮一遍」——**同一份菜单里自己打自己**，
  而且那句话还把已经作废的口径当锚点，用来解释 hold 该怎么做。
- 第 38 行更隐蔽：改的人**意识到了** breath 不该在那句里（example 是「breath / hold」，
  角色版已经删成「hold」），但**留下了「每轮都该做」这个说法**——半个动作，
  正是「改到一半」的标准形状。
- **`Grep` 工具搜不到角色目录**（`characters/` 在 gitignore 里，Grep 默认跳过），
  只有 `Bash grep` 看得见。08-31 那次八成就是这么漏的。**以后扫 persona/菜单一律走 Bash。**

→ 这跟今天下午 `_ToolTrace` 两个调用点是同一类：**同一件事在多处有副本，改了主的没改从的。**
- 真正的口径在 `chat_loop.py:118` 的 `OPENING_NUDGE`，由 `needs_opening` 触发
  （`chat_loop.py:992`，重铸后置真）——**机制早就是对的，是文本没跟上**。

### 7.3 终端面板：可以退役，但前提是游戏的工具调用先合并（09-01 改判）

面板现在的挂载条件是 `sessionMode = codeMine || gameMine`——**游戏剧情会话
共用同一块终端**（`ContentView.swift:110`、`:365`，后端 `/code/*` 打的是
「当前活着的会话」）。

但共用不等于必需：**游戏的消息本来也上屏**，走的是同一条气泡流。所以面板整块
可以退役——code 那半边被 permit 卡取代，game 那半边本来就有气泡。

**没有前置了**（09-01）：游戏工具族整个不上屏（§3.4 例外），
原本担心的「气泡之间夹一串 `tap`/`look`」不会发生。摘面板不必等聚合。

摘的时候连带要走的：`bottomOverlayHeight` 那套让位逻辑、`TerminalHeightKey` /
`TerminalRatioKey` 两个 PreferenceKey、`chatAreaHeight` 的量高。
（按卷首第 3 条：这些全属拆迁，别留兼容层。）

---

## 8. 待定（机主直接在这一节改）

**A. 色**
- [ ] `theme_logo` = 机主的符号是什么（小卡🐾、Cassius✦、机主＝？）
- [ ] `char_bullet` 具体用在哪——列表符号？气泡内强调？还是别的
- [ ] 三个角色的 `light` / `dark` 两身具体色值（现在只有 `base` 一个色）
- [ ] 小字提醒那个「固定灰」的具体值（深浅两种背景都要读得清 → 大概率是
      中性灰 + 一点点不透明度，不是 `.secondary`）
- [ ] `bgImageDim` 的默认值；浅深两种模式各自一张背景图还是共用一张

**B. 形**
- [ ] `*斜体*` roleplay 块的 UI（§3.2，唯一一个完全没形状的元素）
- [ ] 卡片里按钮的圆角（跟 18 还是小一号）
- [ ] 小字提醒靠左还是居中（本 plan 按靠左写，理由见 §3.9；机主没明说）

**C. 口径**
- [x] §7.2 那整条 **✅ 09-01 拍板**：
      1. **文案=全裸工具名**（`🐾 mcp_mail_send`），现有写类人话文案换掉，零维护；
      2. **失败=读 `ret` 判断，同一条小字后追加原因短句**（「，没成：{回执摘要}」）；
      3. **读类=显示但跟聚合绑一个包**——聚合这期不做（见下），所以**这期读类
         不下发，后端维持只发写类白名单**；读类+聚合下期一起上。
- [x] **聚合规则 ✅ 09-01 拍板：这期不做**，先单条上屏真机跑几天看实际密度，
      规则（按什么合、文案长什么样）到时候再定。
- [ ] **游戏小字以后要补的时候，补成什么**（§3.4 例外，「要做后面再补」）：
      是聚合成一条（「操作了 8 次 ›」），还是只报有意义的节点（进了哪个界面、
      拿到什么），还是干脆走另一条通路（不进聊天流，进 Mind 页 / 游戏笔记本）
- [x] 流式分裂走路 C（渲染层切）✅ U2 已按路 C 落地（09-01）

**D. 本 plan 没覆盖、但改配色一定会撞到的**
- [ ] 发送中 / 发送失败 / 待送达补投 三种状态在新配色下长什么样
- [ ] 未读角标 + 「回到最新」胶囊
- [ ] 长按菜单（编辑/删除/重新生成）在染色气泡上的高亮
- [ ] 输入栏、附件条、表情面板的配色（本 plan 只写了聊天流）

---

## 9. 建议的施工顺序

1. **U0 · 色 token 落地**：`ChatPalette` + `CharPalette` + 深浅两份 + 解掉 §1.4 冲突。
   不改任何布局，只把现有硬编码色换成 token。**改完外观应该几乎没变化**——
   这是这一步做对了的判据。✅ 09-01（`Color.theme` 全 app 换 `Color.userAccent`
   =机主玫瑰，这是 §1.4 的既定改动、不算破判据；ThemePalette/bubbleMe 退役）
2. **U1 · 染色口径上身**：机主气泡染色 + AI 气泡回灰 + 圆角统一。✅ 09-01
   （§1.2 口径进 `ChatTint`（Theme.swift）一处；三角色 light/dark 第一版色值
   =base 掺 40% 白/25% 黑，看效果再调：玫瑰 D4A0A3/8A494D、雾蓝 B2CBDE/5F7E97、
   琥珀 DFC79D/977944；行内码炭黑 2E2F33+浅字 E6E6E6；染色底上链接=反色字靠
   下划线区分；圆角 16/10/20→18 含输入栏；模拟器编译过，真机没看）
3. **U2 · 流式分裂**（§4，路 C）。独立，可跟 U1 并行。✅ 09-01
   （定稿态 `messageSegments` 每顶层块一气泡+无块级快路按空行切；流式态
   `streamingSegments` 便宜切分进 MessageSegments.swift，```/~~~ 围栏内不切，
   呼吸点只挂最后一段；松散列表流式里会暂时劈开、定稿块级解析自动纠正不落盘；
   模拟器编译过，真机没看）
4. **U3 · 小字提醒**：logo 前缀 + 呼吸动效 + 三类内容接线。✅ 09-01
   （口径见 §8.C 拍板：裸名/ret 追加原因/读类和聚合绑下期）。施工记录：
   - **后端**：`pipeline.bare_tool_name`（`mcp__server__tool`→`mcp_tool`，server 段
     是挂载编号掐掉；内置名照原样）；stored 条目带 `name` 字段（`tool` 标签不动——
     mail_draft 改判/心流日志/网页卡反查都认它）；sse `memory` 事件带 `name`；
     `StoredItem` 补字段；`next_wake_note` 措辞改「决定下次 {时间} 醒来（{原话}）」
     （名字 app 端接）。测试 `test_tool_notes.py` 锁形，全套 490 绿。
   - **iOS**：memoryNote 从 SystemMessageRow 分出来成 `NoteRow`（靠左 +
     `CharLogo` 前缀 + `Color.noteGray` 固定灰 8A8A8E，§8.A 具体值待拍）；
     小卡 logo 用 SF Symbol `pawprint.fill`（emoji 🐾 是彩色位图染不上灰，
     CharPalette 加 `logoSymbol` 字段）、Cassius=✦ 文本字符；系统消息照 §3.9
     补破折号包裹；三跳点气泡退役 → `ThinkingNoteRow` 呼吸小字
     （`{logo} {名字} 正在思考…`，opacity 0.3↔0.95 缓 1.5s 往复）；
     `.memory` 事件走 `toolNoteText`（裸名；失败「，没成：{ret 原因}」；gametask
     带任务清单可核对；mail_draft 带「等你过目」；老后端 name 空回落人话文案）；
     next_wake 小字=`{名字} 决定下次…`；「选了」「批了」senderID 归 user
     （机主动作非角色动作，机主符号待定 §8 先无 logo）。模拟器编译过，真机没看。
5. **U4 · 卡片**：先做问答卡（有 grill-me 这个现成场景可以自测），
   再做 permit 卡。**permit 卡是 PLAN_native 拨闸的硬前置**（那份 §7 N2）。
   **问答卡半边 ✅ 09-01 全链路落码未上电**：
   - 机制 09-01 探针实证：AskUserQuestion 挂载不进 allowed_tools → can_use_tool
     挂起，`PermissionResultAllow(updated_input=原input+answers)` 回填，模型同轮
     拿到 `"问题"="答案"` 的 tool_result；自定义答案=任意字符串当 label，CLI 不校验。
   - 后端：`questions.py`（内存单，形状抄 permits）+ `_permit_gate` 问答分支 +
     sse `question` 事件（assistant 事件里 tool_use 一现身就推卡，单号=tool_use_id）
     + REST `/questions/pending`、`/questions/decide` + Bark + 轮内空闲豁免并入
     （permits.waiting **or** questions.waiting）。12 条新测试，全套 481 绿。
   - iOS：`QuestionCardView`（染色走 §1.2 用角色人物色；二选一横排/多项竖列；
     multiSelect 勾选+「就这些」；自定义答案输入框；超时置灰+「已超时」角标+收起；
     按钮圆角先按 12＝小一号，§8 待定）钉在输入栏上方；答完出小字
     `选了「前14字…」`（机主名字没进 app 文案，要改再说）+ 队列自动浮现下一题；
     回前台/轮询 syncQuestions 对齐（游戏泵轮里弹的卡靠这条补）；换角色清队重拉。
   - **拨闸**：`.env` 加 `QUESTION_CARDS=1` + 重启后端（默认关；灰度独立于
     WRITE_TOOLS，问答卡可以先上）。自测场景：让他用 AskUserQuestion 问任意选择题。
   - **permit 卡半边也 ✅ 09-01 全链路落码未上电**（=native N2，拨写闸的硬前置
     由此解除）：sse `permit` 事件（写类 tool_use 现身即推卡，带参数原文——
     Bash 命令全文/Edit 文件和改动，运输帽 600–2000 字符）+ iOS
     `PermitCardView`（同问答卡骨架：染色/超时置灰/收起；批准免二次确认，
     拒绝可附一句理由）+ `/permits/*` 现成 REST + syncPermits 轮询对齐。
     批准后小字 `批了 {tool}：{摘要}`（U3 成形时已换：senderID 归 user 无 logo）；
     拒绝不出小字（「生成中」呼吸本来就亮着）。
     ⚠️ 幽灵卡：路径闸拒的调用推了卡却到不了 permits——靠轮询收走+409 有声，
     罕见路，实时性换这个代价（sse._permit_events 注释里记了）。
     上电顺序照 native：写类 schema token 真机实测后拨 WRITE_TOOLS=1。
6. **U5 · 摘掉终端面板**（§7.3）。前置只有 U4 的 permit 卡（code 那半边要有人接），
   game 那半边靠气泡流本来就够。✅ 09-01（CodeTerminalPanel.swift 整删；
   挂载 overlay、bottomOverlayHeight 让位、TerminalHeightKey/RatioKey、
   chatAreaHeight 量高、terminalExpanded 全摘；**sessionMode 和 /code/send
   消息改道没动**——那是 native §9 第二批的活，拆它要和 app 投递口同一批；
   模拟器编译过）
7. **U6 · 背景图 + 蒙版**。完全独立，随时可插。✅ 09-01
   （ChatBackgroundStore：浅深各一张落 Documents/ChatBackground、统一转 JPEG
   缩到 2000pt；蒙版=bg 色盖层，滑到底纯色，**默认 0.4 是第一版**（§8.A 待定项
   顺手拍了，看效果再调）；设置入口在「设置」页新「聊天背景」段（PhotosPicker
   选图/换图/清除+浓度滑块）；渲染在 ContentView.chatBackground（色+图+蒙版，
   只管聊天页）；纯本机偏好不进后端；模拟器编译过）
8. **U7 · 文件卡文件名一行**、图片/表情圆角这些零碎。

`U0` 之前不要动别的——所有后面几步都要用到 token，先散着改会改两遍。

### 9.1 U3 上电后剩余清单（09-01 晚，e3c3a0f 已 commit + 后端已重启）

1. **真机验收（最大欠账）**：U0–U3 全部只过了模拟器。要机主装新 app 包逐样看：
   三角色 light/dark 色值、染色气泡/反色字、流式切气泡、背景图+蒙版 0.4、
   两张卡收起小字、**U3 的呼吸小字/爪印 logo/固定灰 8A8A8E**。旧包连不炸但
   看不见任何新样式。
2. **U7 零碎**：文件卡文件名一行中间截断保留后缀（§3.8）；图片/表情圆角
   U1 已对齐过，核一眼没漏的就行。
3. **§8 待拍**（都在上面各小节，机主直接改 md）：A 机主符号/char_bullet 用途/
   色值调优/小字灰具体值/背景图浅深共用与否；B `*斜体*` 块 UI（唯一没形状的）/
   卡片按钮圆角（12 第一版）/小字靠左要不要改；C 读类+聚合下期那个包的聚合
   规则、游戏小字以后补成什么；D 发送三态/未读角标/长按高亮/输入栏配色。
4. **breath 口径两处没改干净**（§7.2.1，跟 UI 无关但挂在这别丢）：
   `characters/cass/persona.md:104`（活的，和他自己 tool_menu 打架）+
   `tool_menu.example.md:54`（模板）。

---

## 10. 实读记录（2026-09-01 下午，本轮亲自读的，带行号）

**流式那条链**
- `server/sse.py:118-126` — 新 text 内容块开始 → `yield {"type":"text_break"}`；
  段与段之间用 `"\n\n".join` 拼回 `sealed`。
- `ios/cassette/ChatService.swift:254` — `text_break` → `StreamEvent.textBreak`。
- `ios/cassette/ContentView.swift:1044-1058` — `.textBreak`：当前气泡 `editText`
  定稿落盘 → `streamingId = nil` → 重新亮「正在输入」。
- `ios/cassette/ContentView.swift:1116-1145` — `finalizeStreamedReply`：
  `sawBreak` 决定最后一个气泡用流出的原文定稿、还是用 `resp.reply` 全文覆盖。
  **这就是路 A 那第一条代价的出处。**

**渲染**
- `ios/cassette/MessageSegments.swift:39-64` — 只有 `CodeBlock` 脱出成 `.card`，
  其余顶层块全部 `"\n\n".join` 合成**一个** `.bubble`。§4.2 路 C 要动的就是这里。
- `ios/cassette/ChatView.swift:594-600` — 流式走 `streamingBubble`（纯 Text），
  定稿走 `textBody`（markdown）。
- `ios/cassette/ChatView.swift:678-692` — `streamingBubble`：单个 Text + 呼吸点，
  圆角 18，底色 `isMe ? Color.bubbleMe : Color(.systemGray5)`。
- `ios/cassette/ChatView.swift:694-712` — `segmentView`：气泡同款配色/圆角；
  代码块卡走 `MarkdownMessageView` 自带的深色底。
- `ios/cassette/MarkdownMessageView.swift:41-80` — markdown theme：
  链接 `.accentColor`、行内码底 `systemGray4`、`*斜体*` 是 `primary.opacity(0.5)`、
  代码块 `#282c34` 圆角 10。

**媒体与卡**
- `ios/cassette/ChatView.swift:349-372` — `displayRows`：连续 ≥2 张同发送者图片
  收成 `imageStack`。
- `ios/cassette/ChatView.swift:601-618` — 表情自适应比例（圆角 16）、
  图片 160×160 填充裁切（圆角 16）。
- `ios/cassette/ChatView.swift:619-666` — 文件卡 / 网页卡：`systemBackground` 实底
  + `systemGray4` 0.5pt 描边 + 圆角 16；文件名 `lineLimit(2)`，后缀另起一行。
- `ios/cassette/ChatView.swift:493-501` — `SystemMessageRow`：居中 caption
  `.secondary`。`memoryNote` 和 `system` 现在**共用这一个样式**（`:56`），
  新设计要把它们分成两档（居中无 logo / 靠左带 logo）。

**色**
- `ios/cassette/Theme.swift:5-9` — `ThemePalette` 只有 `accent` + `bubbleMe` 两个字段。
- `ios/cassette/Theme.swift:86-96` — `IdentityColor`：user 玫瑰 `#B86166`、
  default 雾蓝 `#7FA8C9`、cass 琥珀 `#C9A15B`，未知 id 按哈希出稳定色。
- `ios/cassette/Theme.swift:101` — `Color.current = ThemePalette.plumTint`（`#352526`）。
  **和上面的 user 玫瑰是两个值** → §1.4。
- 全仓 grep：`背景图` / `backgroundImage` / `chatBackground` **零行**；
  `colorScheme` / `preferredColorScheme` **零行**（深浅全靠系统语义色）。

**后端口径**
- `server/pipeline.py` `_stored_from_tool_use` — 灰字只对写类白名单发，
  注释原话：「只记『写』操作，breath 等读操作不算产物」。→ §7.2。
- `server/pipeline.py` `StoredCollector.on_user` — 成败等 `tool_result` 才定案；
  `mail` 落草稿箱会改判成 `mail_draft`（「等你过目」不能说成「寄出了」）。

**终端面板**
- `ios/cassette/ContentView.swift:110` — `sessionMode = codeMine || gameMine`；
  `:363-369` — 面板 overlay 挂载条件是 `sessionMode`，注释写明「游戏剧情会话
  共用这块终端」。→ §7.3：共用属实，但游戏消息本来也上屏，所以面板仍可退役，
  前置是小字聚合。

**breath 口径（§7.2.1，顺手挖的，跟 UI 无关）—— 2026-09-01 20:4x 已改完，五处**
- 已改：`characters/cass/persona.md:104`、`tool_menu.example.md` 三处、
  `characters/{default,cass}/tool_menu.md` 各两处。全仓残留扫描已空。
- **生效时机**：`characters.py` 的 docstring 写明 persona/菜单是**热读**（不缓存），
  但它们在 SDK session 建立时进 system prompt——所以**新会话/重铸后**的那一轮才吃到
  新文案，当时活着的会话里仍是旧的。不需要重启后端。
- `server/characters.py:85-91` / `:96-110`、`server/config.py:24-26` — 回落链：
  `characters/<id>/*.md` → 默认角色退 `server/persona.md`（**不存在**）/
  `server/tool_menu.md`（**不存在**）→ 退 `*.example.md`。
  所以小卡的 persona 实际读的是 `persona.example.md`（干净）。
- `server/chat_loop.py:118` `OPENING_NUDGE` + `:992` `needs_opening = True`
  （重铸后置真）——**机制早就是对的口径，是人设/菜单文本没跟上。**
