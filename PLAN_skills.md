# Plan：skill 基建 + 两套 skill 的分层

> 2026-08-28 方案定稿。管三件事：①skill **机制**怎么建（自建渐进披露，不走 CLI 原生）；
> ②两套 skill（jobhunt 域方法层 / workbench 通用交付层）怎么摆才不混；③施工顺序。
>
> 上游：`PLAN_jobhunt_skills.md` 是 jobhunt 的**内容**规划（流程/框架/铁律，全部保留），
> 但它的「落法待拍 1/2」「M5 手机侧挂载」被本 plan 接管改判，见第五节。
> 小屋开关（PLAN_house_switch）与本案**零耦合**，见第三节末。

---

## 一、实测记录（2026-08-27/28，CLI 2.1.247，姿态复刻 pipeline）

原生 Skill 机制的探底（`--system-prompt-file` + 精确白名单，haiku，量 input_tokens）：

| 姿态 | 结果 | tokens |
|---|---|---|
| `--tools ""`（现状纯聊天） | skill 完全不可见 | 284 |
| `--tools Skill --allowedTools Skill` | **15 个全进上下文**：机主的 grill-me/impeccable + CLI 内置 13 个 | 3,164 |
| + `--setting-sources project` | 用户级挡掉，内置 13 个还在 | 2,902 |
| + `--disallowedTools "Skill(x)"` | **只挡执行不挡清单**（同 `--tools` 摘 MCP schema 的失效模式） | 2,895 |
| + env `CLAUDE_CODE_DISABLE_BUNDLED_SKILLS=1` | **内置全藏**，只剩项目 skill | 1,452 |
| 触发测试（项目 skill 正常触发） | 能用：自动调 Skill、按流程执行 | 3 轮 6,095 |

- 嵌套子 skill 不被发现 / 项目级 skill 认：PLAN_jobhunt_skills 第一节已实测，不重复。
- **MCP 工具在「内置工具全开、无 --tools 白名单」的会话里可用 per-session settings 预批准**
  （code 模式的关节，08-28 A/B 实测）：`--settings` 里
  `"permissions": {"allow": ["mcp__skills__skill_read"]}` → 直接执行成功、内置工具一个不少；
  不加 → 卡权限。这就是 code 路挂 skill 的钥匙（`tools` 参数不能用：一给就整体
  strict 白名单，Bash/Write 全被摘）。

## 二、选型：自建「skill 库 + 菜单索引 + skill_read」，不走 CLI 原生

原生路线靠 env 开关能把污染压到只剩项目 skill（+~1,200/轮），**可用但不选**：

1. `CLAUDE_CODE_DISABLE_BUNDLED_SKILLS` 是**没写进 --help 的开关**，版本一升说没就没——
   缓存断点那次（CLI 多打一个块就全线 400）已经教过一次这种依赖的脆法。
2. 没有角色/场景维度：per-char、chat/wake 分挂、`when:` 过滤全接不上；chat/wake 共用
   `state/claude_cwd` 一个 cwd，per-char 项目 skill 得另拆 cwd，越搭越歪。
3. 清单注入的位置/格式 CLI 说了算，进不了我们的缓存稳定段，动作式触发纪律也套不上。
4. `--allowedTools Skill` 是整族放行，收窄到 `Skill(名字)` 又造「看得见调不了」的坑。
5. 恒空 cwd 的 @-mention 防线得开缝。

自建 = 把已验证过的渐进披露（菜单列名字、按需取正文）复制到 skill 上，三个场景同一套。
原生路线 + env 开关**记档作 fallback**：哪天自建路线有硬伤，它是退路。

## 三、机制设计

```
server/skills/                    ← skill 库（tracked，方法论是代码）
  <skill>/SKILL.md                  frontmatter: name / description / 可选 when: chat|wake|code
  <skill>/prompts|frameworks/*.md   子文件，按需读
server/characters/<id>/skills/    ← 角色覆盖：同名 skill 整目录覆盖，其余共享（union）
```

- **`skills.py`（独立模块，不依赖 pipeline）**：渲染索引 + 读文件。pipeline 和 code_bridge
  各自 import——绕开「code_bridge 不 import pipeline」的防环红线。热读，改文件不用重启。
- **`skills_mcp.py`（主仓内置 MCP，照 `pet_mcp.py` 成例，不走插件商店）**：唯一工具
  `skill_read(name, path="")`——不传 path 读 SKILL.md（路由表），传 path 读子文件。
  **嵌套限制在这套里不存在**（那是 CLI 原生的坑）。只读；路径 resolve 后必须落在
  skills 根目录下（防 `../` 逃逸）；白名单照旧，安全姿态不变。
- **索引**：从 frontmatter 自动渲染「名字 + 一句话 + 触发时机」。
  条目必须是**动作式触发**（「收到 X → 先 `skill_read('y')` 再动手」）——08-13 菜单
  实锤过条件式标题的死法：TA 不觉得自己「想不起来」，就永远不去翻。
- **chat/wake**：挂载进 `base_claude_args`（pet MCP 同款三件套：`_skills_mcp_config` /
  `SKILL_TOOLS` / `mounted_tool_names`）；索引由 `tool_menu_block` 追加成一块
  （`needs: skill_read`，渲染纪律自动管住「工具不在场就不提」）。三个调用点
  （`build_prompt` / `wake.wake_prompt`:223 / `cohabit`:153）零新接线。
  chat 索引进稳定段吃缓存（改 skill 文件重建一次，可接受）；wake 的 ToolSearch 延迟
  自动覆盖 skill_read 的 schema。**索引 chat/wake 同一份不做收敛**（08-28 机主拍板）。
- **code**：`start(mcp_configs=[skills那份])` + `_hook_settings` 的 payload 并进
  `permissions.allow`（实测见第一节）；索引渲染成文件插进 `_build_system` 的链条，
  **守则之前**（人设 → skill 索引 → 守则；索引是参考资料、守则要压轴，08-28 机主拍板）。
- **小屋开关零耦合**：两条 wake 路共用同一个挂载漏斗（老路 `wake.py`:223/:325，小屋路
  `cohabit.py`:153/:315 直接调 `run_claude_wake`），开关只切哪条路开火。skill 挂在
  pipeline 层，开关怎么拨都无感。

## 四、两套 skill 的分层（治混乱的核心）

**混乱的根源**：PLAN_jobhunt_skills 把 `workbench-spec.md` 塞在 `jobhunt/assets/` 里，
把「搭工作台」当成了求职的附件。实际上它们是**两层**：

| | jobhunt（域方法层） | workbench（通用交付层） |
|---|---|---|
| 管什么 | 求职这件事怎么做才算做好 | 任何项目的成品怎么变成网页工作台 |
| 产出 | 结构化内容：JD 打分、公司情报、手册数据、故事库 | 可勾选/可笔记/可存进度的网页 |
| 数据 | `server/state/jobhunt/`（隐私，不进 git） | 工作台 HTML + 进度 JSON |
| 换个领域 | 换成别的域 skill（如学习计划） | **原样复用** |

- **接口是数据文件，不是 skill 套 skill**：jobhunt 把内容写进 state，workbench 把内容
  渲染成网页。「jobhunt 工作台」= workbench 吃 jobhunt 产物的第一个实例。
- **需求拷问 / UI 不是独立 skill**，是 workbench 的子文件（`prompts/grill.md` /
  `frameworks/ui-style.md`）。机主 `~/.claude/skills/` 里的 grill-me/impeccable 是
  code 会话私货且带工具依赖（AskUserQuestion / Bash，chat/wake 没有）——文件树版
  要写成**不依赖工具的对话流程**，三个场景才都满血。视觉方向照抄
  PLAN_jobhunt_skills 施工纪律那条：OTK editorial（奶油纸底+衬线+墨黑/芥黄），治 AI 味。
- **交付形态（一期）**：自包含单文件 HTML，进度走「导出/导入 JSON」+ localStorage 双保险，
  由 webpage 插件承载（app 里现成能看）。自包含单文件天然可迁——二期要上
  claude studio / 真托管网站，拿着文件走就行，形态留待拍（见第七节）。
- **进度回流升级路径**（08-28 参考轮的最大收获，见附录）：若 webpage 承载页与后端同源
  （:8000 直出），给后端加一个极小的进度落盘端点（如 `POST /webpages/progress/<page>`），
  页面「保存」直接 POST——进度就变成 **TA 下轮能 `skill_read`/工具读到的文件**，
  勾选和笔记回流进对话闭环，localStorage/导出按钮降级为兜底。一期先验同源，成立就做。

```
server/skills/
  workbench/
    SKILL.md                路由 + 铁律（先拷问再动工；进度文件是用户资产不许重置）
    prompts/grill.md        需求拷问流程（对话式，无工具依赖）
    prompts/build.md        搭/改工作台：读域数据 → 渲染 HTML → webpage 交付
    prompts/progress.md     进度导入/导出约定（JSON schema + 回流合并规则）
    frameworks/ui-style.md  视觉规范（editorial 方向 + AI 味黑名单）
  jobhunt/
    SKILL.md                路由表 + 三条铁律（照 PLAN_jobhunt_skills 第五节，一字不动）
    prompts/…               七个流程（照其第三、四节）
    frameworks/…            evidence-ranking / competency-tags / star-car / manual-spec …
```

## 五、对 PLAN_jobhunt_skills 的改判（其余全部照旧）

| 原条目 | 改判 |
|---|---|
| 待拍 1：方案 A vs B | **基建裁掉了这题**：索引+按需读就是 B 的形状；且 skill_read 带 path，索引条目可直指子文件，两跳变一跳，A 的「触发精确」优势也被动作式索引吃掉。按 B 落。 |
| 待拍 2：skill 住哪（软链/gitignore/独立仓） | **蒸发**：住 `server/skills/`（tracked），`.claude/` 完全不沾。 |
| 待拍 5：工作台=手册？ | **已确认**：工作台是通用承载形态（workbench 层），手册内容是 jobhunt 产物。 |
| M5 手机侧挂载（Skill 白名单 + env） | **被基建取代**。env 开关那条实测复核为真（见第一节），记档作 fallback。 |
| 「嵌套子 skill 不可行故一个 skill+文件树」 | 结论保留，但理由变了：MCP 路线里嵌套限制根本不存在，选 B 是因为它本来就对。 |
| `assets/workbench-spec.md` | 挪进 workbench skill（`frameworks/` 下，M3 倒推时落笔）。 |
| 铁律三条 / 数据分层 / 不抄清单 / 两仓结论 | **全保留，照搬**。 |

## 六、施工顺序

- **S0 基建（chat/wake）**：`skills.py` + `skills_mcp.py` + pipeline 三件套接线 +
  `tool_menu_block` 追加索引块。验收：chat 索引进稳定段且缓存照常命中；wake 延迟下
  `skill_read` 可取可调；空库时索引块不渲染（needs 纪律）；`../` 逃逸被拒。
- **S1 基建（code）**：`start()` 挂 skills MCP + `_hook_settings` 并 permissions.allow +
  addendum 链插索引（守则前）。验收：code 会话里 `skill_read` 免弹窗、Bash/Write 无恙。
- **S2 jobhunt 骨架**（= PLAN_jobhunt_skills M1）：SKILL.md 路由 + 三条铁律 +
  `evidence-ranking.md`。先做它——治的是**已经翻过的车**（08-26 Bonjour 58→45）。
- **S3 workbench 骨架**：SKILL.md + grill.md + ui-style.md + build/progress 草稿。
  草稿即可，**别先写规范后干活**（M2 纪律，对 workbench 同样生效）。
- **S4 合练：跑通一个岗**（= PLAN_jobhunt_skills M2）：`company-intel` +
  `interview-manual` 产内容 → workbench 搭出第一版 jobhunt 工作台。
  这一步天然跨场景：扒情报在 chat（浏览器）、读项目代码和搭网页在 code——
  正是 S0+S1 要铺的路。
- **S5 双向固化**（= M3）：拿 S4 真实产物倒推 `manual-spec.md` + `workbench-spec.md`
  （后者归 workbench）。
- **S6 故事库**（= M4）：story-mining + `stories/_index.md`。

## 七、待机主拍板

1. ~~第一个跑通的岗~~ ✅ 08-28 拍板：**ALLTIME（万物时）**。
2. 手册成品深度：一页带进考场 vs 慢慢啃的长文档？（待拍 4，仍未答）
3. 工作台二期形态：webpage 插件承载到什么时候够用、「claude studio 做成真正的网站」
   具体指什么托管形态？（一期自包含单文件不受影响，可后定）

## 附：workbench 参考轮（08-28 扒仓结论）

### `f-labs-io/agent-html-skills` —— 抄「结果回流」的纪律

17 个 HTML skill（看板/脑图/对比矩阵/测试清单…），交互页的勾选结果**送回给 agent**：
Submit 按钮探 `window.__CLAUDE_SUBMIT_URL__`，有本地监听服务器就 POST JSON，没有就
降级复制到剪贴板。抄这几条：

- **一页只有一个 Submit（保存）动作**——数据流单一，不歧义。我们的对应物就是上面那个
  进度落盘端点（比它的临时监听服务器强：后端常驻，进度落成持久文件）。
- **禁 `innerHTML` 塞变量**（XSS 纪律）——工作台内容全是 TA 生成的文本，照守。
- 每个 skill 带自己的 aesthetic rules，输出才不是「通用 AI 味」——正是 ui-style.md 的定位。
- 剪贴板那条**不抄**：iOS webview 场景不成立，我们的兜底是导出/导入 JSON 文件。

### `hagaybar/html-dashboard` —— 抄「模式库 + INDEX」的结构

`SKILL.md`（工作流）+ `patterns/`（HTML 模式库）+ `INDEX.md`（模式索引，用时选配）：

- **patterns/ + INDEX.md 进 workbench skill**：进度头 / 勾选清单板块 / 笔记卡 /
  对比矩阵各是一个带完整 HTML 骨架的模式文件，build.md 按 INDEX 选配——和我们
  skill_read 的渐进披露同构，且「搭第二个工作台」时模式直接复用。
- **敏感数据不进页面**：它掩 JWT/API key；我们的对应物是工作台 HTML 里不放
  token/密钥/授权码（页面会被 webpage 插件存盘、可能被传送）。
- 它的 localhost 双向实时链路（浏览器↔Claude 在线对话）**不抄**：我们的回流是
  「落盘文件下轮读」，异步、无常驻进程，符合 claude -p 一次性子进程的形状。

### 其他扫到的（记名不深读）

`BehiSecc/awesome-claude-skills` 的 kanban-skill：markdown 文件即卡片 + YAML frontmatter
当状态——和我们「数据层 = state 下的 md/jsonl」完全同构，互相印证，不用改。
