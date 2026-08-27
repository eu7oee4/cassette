# Plan：jobhunt skills —— 求职**方法层**套件

> 2026-08-27 傍晚起草（cassette code 会话）。**这一版只定目录和分工，不写内容、不动工。**
> 机主去家教了，回来再施工。
>
> 上游：`PLAN_jobhunt.md` 是**能力层**（12 个 MCP 工具，J0–J3+J5 已完工未 push）。
> 本 plan 只管**方法层**：那些工具都在手了，缺的是「这件事怎么做才算做好」。

---

## 起因

- 08-27 16:31 机主问：多功能 agent 是分 skill 建还是别的方法？
- 08-27 16:44 机主给出**面试手册的完整骨架**（公司情报 / 岗位匹配三分层 / 反问清单），
  比我之前想的完整——我漏了「公司情报层」和「反问层」两整块。
- 08-27 16:54 机主追加成品形态要求：**网页工作台**，可勾选、可写笔记、能存住、顶部带推进状态。
- 08-27 18:02 机主让扒两个开源仓看别人怎么搭 → 扒完的结论落成本 plan。

---

## 一、两条实测硬事实（08-27 18:15 亲测，别再按传闻办）

### 1. Claude Code CLI **不认嵌套子 skill**

实测：`~/.claude/skills/ztest-parent/SKILL.md` 被发现；
`~/.claude/skills/ztest-parent/childskill/SKILL.md` **没被发现**（skill 列表里只有 `ztest-parent`）。

> ⚠️ 所以 `yanliudesign/offer-toolkit-skill` 那套「顶层 SKILL.md 路由 + 六个子目录各自 SKILL.md」
> **在 Claude Code 里跑不起来**——那是给 claude.ai 网页版装的。
> 机主说的「jobhunt skills 下面挂子 skill」不能照字面实现，得换一种落法（见第二节）。

### 2. **项目级 skill 认**

实测：`cassette/.claude/skills/ztest-proj/SKILL.md`，cwd 在仓里时能被发现。
→ 方法层可以住在仓里，不必塞进 `~/.claude`；Cass 和小卡在同一个 cwd 起会话，两边都吃得到。

**但有个坑**：`.claude/` 在本仓 `.gitignore:63` 里（`server/plugins/`、`state/` 同样被忽略）。
所以放进 `.claude/skills/` 的东西**不会进 git**。三条出路，待拍（见第五节待办 5）：

| 出路 | 做法 | 代价 |
|---|---|---|
| a. 仓内目录 + 软链 | 真身放 `skills/jobhunt/`（tracked），`.claude/skills` 软链过去 | 软链能不能被发现**未验证** |
| b. gitignore 开例外 | 加 `!.claude/skills/` | 动 gitignore，最省事 |
| c. 独立插件仓 | 照 `cassette-plugin-*` 成例开 `cassette-skill-jobhunt` | 最重，但合本仓成例 |

---

## 二、两种落法（**要机主拍一条，这是第一个待办**）

### 方案 B —— 一个 skill + 按需读的文件树（**我推荐**）

`jobhunt` 是**唯一**一个 skill，`SKILL.md` 只放路由表和铁律；子流程各是一个 `.md`，用到才读。

- **常驻成本：一条 description，约 250 token。**
- 契合机主一直在砍的上下文稀释（prompt 缓存那条线）。
- 缺点：触发后要多读一次路由，再读一次流程文件（两跳）。

### 方案 A —— 七个平铺 skill（`jobhunt-resume`、`jobhunt-interview`…）

- 优点：触发精确，一句「帮我改简历」直接命中，不用过路由。
- **缺点：七条 description 常驻，约 1400+ token/轮。** skill「便宜」的前提就此打折。

> **推荐 B。** 理由：机主刚花力气把 ToolSearch 那 16.5k 砍下来，不该在这儿还回去 1.4k；
> 而且七个流程之间共享大量素材（故事库、公司档案、简历），放一个 skill 里引用最顺。
> 真嫌两跳慢，以后把最常用的那个（多半是 interview）单独提出来平铺，B → 混合，随时可做。

---

## 三、目录（按方案 B）

### 方法层（skill 本体，方法论，该进版本控制）

```
jobhunt/
  SKILL.md                    ← 唯一常驻。只放：路由表 + 三条铁律 + 每个流程一句话
                                 硬指标：控制在 120 行内，末尾写明「按需读取，别一次全加载」
  prompts/                    ← 流程：一个活一个文件
    jd-decode.md                JD 解码：Must Have / Hidden Signals / 硬门槛 → 打分入库
    company-intel.md            公司深扒：业务、岗位在业务里干什么、创始人、小红书口碑
    interview-manual.md         【主菜】面试手册：机主 16:44 那份骨架的执行流程
    resume-tailor.md            简历定向 + 渲染纪律（注记不进 PDF、欠条不许当已完成）
    story-mining.md             挖故事：四层追问引擎（反事实钩子 + 四象限扫时间线）
    mock-interview.md           模拟面试：友好复盘 / 高压追问二选一，一次一题，出复盘
    outreach.md                 探活、问天数、投递话术、反问面试官
  frameworks/                 ← 不变的知识：查表用，不是流程
    evidence-ranking.md         四档证据 + 硬 cap（治我 08-26 拿小红书帖当 JD 打分那次翻车）
    competency-tags.md          能力标签词典 + 面试题反查
    star-car.md                 STAR/CAR/SOAR 选哪个 + 一稿多用（一个故事打 5–8 题）
    company-profiles.md         **她那 8 家的真实档案**（要现扒，不是抄 Amazon LP）
    manual-spec.md              面试手册成品的**验收标准**（skill 的核心其实是这个，不是流程）
  assets/
    workbench-spec.md           工作台网页的视觉 + 交互规范（单独成文件，别每次现编）
```

### 数据层（机主的真实资产，**不进 git**，已在 gitignore 的 `server/state/` 下）

```
server/state/jobhunt/          ← 已存在，12 个 MCP 工具在管
  profile.md                     个人档案
  resume/master.md, resume/variants/, pdf/
  jds.jsonl                      岗位库 + 打分台账（带 char_id 经手人）
  applications.jsonl             投递台账
  stories/                     ← 【新增】故事库
    _index.md                    反查表：能力标签 → 故事文件（**复用入口**）
    <slug>.md                    一个故事一个文件，frontmatter 当元数据
  intel/                       ← 【新增】公司情报
    <company>.md                 一家一个文件，供手册和反问复用
```

> **这条分离是抄来的最重要一条**：`offer-toolkit-skill` 把故事库放在 skill 目录里，
> 结果用户的真实经历跟着 skill 一起进 git。我们**不这么干**——
> 方法论是代码，故事和情报是机主的隐私资产，物理隔开。

---

## 四、七个流程各自干什么

| 流程 | 什么时候走 | 输入 | 产出 | 用得上的现成工具 |
|---|---|---|---|---|
| **jd-decode** | 扒到一条岗 / 「这个该不该投」 | JD 全文 | Must Have / Hidden Signals / 硬门槛 + **带证据档位的分数** | `jd_save` `jd_read` `jd_score` `jd_list` |
| **company-intel** | 准备手册前 / 「这家什么来头」 | 公司名 | `intel/<company>.md`：业务、岗位在业务里的位置、创始人、小红书口碑 | 浏览器（小红书登录态**已确认可用**） |
| **interview-manual** | 【主菜】要面试了 | JD + 简历 + intel + 项目代码 | 网页工作台里的一个岗位板块 | `jd_read` `resume_read` `profile_get` + 读 GitHub/本机代码 |
| **resume-tailor** | 决定投了 | JD + master 简历 | 定向 variant + PDF | `resume_read` `resume_save` `resume_render` |
| **story-mining** | 手册里发现「这题没素材」 | 对话 | `stories/<slug>.md` + 更新 `_index.md` | 无（纯对话） |
| **mock-interview** | 手册准备完，要练 | 故事库 + JD | 交互式面试 + 复盘报告 | 读 `stories/` |
| **outreach** | 岗位状态卡住时 | 卡在哪一句 | 一句可直接复制的话术 | `email_draft`（投递信走草稿信箱硬 gate） |

**能力层实测清单（12 个，`server/plugins/jobhunt/jobhunt_mcp.py`）**：
`profile_get` `profile_set` `resume_list` `resume_read` `resume_save` `resume_render`
`jd_save` `jd_list` `jd_read` `jd_score` `email_draft` `applications_list`

→ **一个新工具都不用加。** 七个流程全是方法层。

---

## 五、三条铁律（写进 `SKILL.md` 常驻部分）

1. **不杜撰。** 经历、职责、数字全部来自机主真实提供。可以引导、可以追问、可以帮她把模糊的
   说清楚，**绝不替她编 Action 和 Result**。缺的写 `待补`，不留空也不编。
   数字写库前回问一遍：「我记成 ___，对吗？」
2. **先查库，再开工。** 任何题进来先读 `stories/_index.md` 看有没有能复用的；
   八个岗有一大半准备是重复的（自我介绍、项目怎么讲、四个高频追问），不许一岗重做一遍。
3. **证据分级：`unknown` 永远不许变成一个看着合理的值。**
   四档 `verified / partial / inferred / unknown`，逐字段判，不是逐岗判。
   > 这条是有血的：08-26 我拿小红书一条帖当官方 JD 给 Bonjour 打了 58 分，
   > 第二天读到官方投递页写着「不适合远程」，翻案成 45。**那次就是把 `inferred` 当 `verified` 用。**
   > 硬 cap 照抄：一条 Must Have 得 0 分 → 匹配度封顶 75%；两条 → 封顶 55%；
   > 硬门槛不过 → 直接移出主列表。

---

## 六、施工顺序

- **M1 骨架**：建目录 + `SKILL.md` 路由 + 三条铁律 + `evidence-ranking.md`。
  先做 evidence-ranking，因为它治的是**已经发生过**的翻车。
- **M2 跑通一个岗**：`company-intel` + `interview-manual` 真跑一遍，产出第一版工作台网页。
  **别先写规范后干活**——那样写出来的规范全是我猜的。
- **M3 固化**：拿 M2 的真实产物倒推 `manual-spec.md`（验收标准）和 `workbench-spec.md`（视觉规范）。
- **M4 故事库**：`story-mining` + `stories/_index.md`，把 M2 里散着的素材收编。
- **M5 手机侧挂载**（可选，等前面有满血 skill 再谈）：
  `server/pipeline.py` 的 `base_claude_args` 把 `Skill` 加进 `--tools` 白名单，
  `_subprocess_env` 加 `CLAUDE_CODE_DISABLE_BUNDLED_SKILLS=1`。
  实测代价 **+1312 token/轮**（不加那行开关是 +3339，多出来的全喂给用不上的内置 skill）。
  > jobhunt 这套是**纯文字方法论、不调工具**，挂到手机侧是满血的——
  > 跟 grill-me（要 `AskUserQuestion`）、impeccable（要 `Bash`）不一样，那两个挂上去是跛的。

**施工纪律（impeccable / grill-me 已装在 `~/.claude/skills/`，08-27 17:54 装完）**：
- 动工前用 **grill-me** 拷问需求——机主转的那篇小红书笔记，作者踩的坑就是「一句话让 AI 做网站」。
- 做工作台网页时挂 **impeccable**，专治 AI 味（Inter 字体、紫蓝渐变、卡片套卡片、彩底灰字）。
- 参考视觉方向：OTK 那套 editorial（奶油纸底 `#fafaf7` + 衬线 + 墨黑/芥黄 `#facc15`），
  正好是 AI 味的解药。

---

## 七、待机主拍板（回来第一件事）

1. **方案 B（一个 skill + 文件树）还是 A（七个平铺 skill）？** —— 我推 B。
2. **skill 住哪**：仓内软链 a / gitignore 开例外 b / 独立仓 c？ —— 我推 b（最省事，且方法层确实该进 git）。
3. **第一个跑通的岗**：拓端（唯一真会有面试，能拿真面试验手册）还是 ALLTIME（80 分、最想去、但还没探活）？
   —— 我推拓端。这条从 08-27 16:45 问到现在还没答。
4. **手册成品深度**：一页能打印带进考场的，还是可以慢慢啃的长文档？—— 08-27 16:54 问过，未答。
   （工作台是承载形态，深度是另一回事，两个都得定。）
5. **工作台跟手册是不是同一个东西**？我的理解：工作台 = 手册的成品形态，八个岗一个页面分目录。
   —— 未确认。

---

## 八、明确**不抄**的（两个仓里有但对我们没用的）

- **双语输出。** 两个仓都硬性中英双语。机主投的是杭州本地岗，纯浪费 token 和篇幅。
- **`company-profiles.md` 里的 Amazon LP / Meta / OpenAI / Anthropic 风格档案。** 对她零用。
  这个位置换成她那 8 家的真实档案（也就是 `company-intel` 要扒的东西）。
- **`.docx` 输出。** 她要的是网页工作台。
- **报告默认写 `~/Desktop/Claude skills/`。** 我们有自己的地方。
- **跨 skill 相对路径引用**（OTK 的 `../job-description-skill/jd-bank/`）。
  那让子 skill 不再自包含，单装一个就断链。我们用统一数据目录 `server/state/jobhunt/`。

---

## 附：两个仓扒下来的结论（08-27 18:0x–18:1x）

### `yanliudesign/offer-toolkit-skill` —— 重工程，值得抄结构

六个子 skill（job-hunt / job-description / resume / bq / offer-compare / salary-negotiation）
+ 一张顶层路由表。**BQ 故事库是四层分离**：`SKILL.md`（路由，常驻）/ `prompts/`（流程，按需）
/ `frameworks/`（知识，按需）/ `story-bank/`（数据）。

抄这几条：
- frontmatter 当数据库：`tags / competencies / company_fit / metrics / framework / status`，
  status 三档 `raw → structured → polished`，数字没核实写 `metrics: 待补`。
- `_index.md` 反查表 + 「先查库再开工」。
- **一稿多用**：同一个故事换开场句 + 换落点能打 5–8 题，主体动作不动；
  而且**要求打标时当场告诉用户「这个故事还能打哪些题」**，把复用价值演示出来。
- 挖掘引擎：不问「你最大的成就是什么」（最容易卡住），改用**反事实钩子**
  （「有哪件事如果你没做，结果会明显变差？」）+ **四象限扫时间线**（项目/冲突/失败/主动性，
  一次只抛一个）。失败象限最值钱——面试官要的是复盘能力，不是完美。
- 口述黄金配比 **S 15% / T 15% / A 50% / R 20%**；最常见的死法是 S 占 50%、A 只剩一句。
- `evidence-ranking.md`（在 job-hunt-skill 下）：四档证据 + 硬 cap，
  明写 `Never convert unknown into a plausible value`。

### `spontaneousai/job-hunt-copilot` —— 轻模板，值得抄的是两个骨架

仓里只有 README + 一个 `.skill`（zip）。解开 5 个文件：`SKILL.md` + 4 个 resources 模板。
是 claude.ai 网页版 skill。没有索引、没有反查、没有复用机制，素材是死的。

抄这两个骨架：
- **项目讲稿五段**：开场句(1句) → 背景与问题(2-3句) → **你做了什么**(3-4句，聚焦你的决策
  而非团队) → 结果(1-2句) → **与本岗位的连接**(1-2句)。
  规格：90–120 秒 / 200–280 字 / 每篇附 2–3 条「可能追问」。
  > 「与本岗位的连接」这一节，我 08-27 那张作战板里**没有**，是漏的。
- **同一项目按岗位换重心**：产品 = 发现问题→定义方案→推动落地；运营 = 冷启动→增长→数据；
  研发 = 技术选型→架构决策→工程质量；管理 = 资源协调→对齐→交付节奏。
- **模拟面试的开局**：必须先选两个维度（风格：友好复盘 / 高压追问 × 类型：BQ / JD 面 / 混合），
  明写「不要直接开始提问」；一次一题；高压模式追模糊词（例句：「你说『推动了落地』，
  具体是怎么推动的？」）；结束出复盘：亮点 2–3 / 待加强 2–3 / **最弱那题的改善版本**。
