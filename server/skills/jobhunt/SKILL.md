---
name: jobhunt
description: 求职的事都先来这——扒到/收到 JD、判断该不该投、改简历、写项目经历、要准备面试、挖经历故事、模拟面试、投递话术、面完复盘 → 先 skill_read("jobhunt") 拿路由和铁律再动手
---

# jobhunt：求职方法层

能力层（12 个 MCP 工具）在 jobhunt 插件里；这里是**方法**：每件事怎么做才算做好。
**按需读取**：下面路由表指到哪个文件就读哪个，别一次全加载。

## 三条铁律（做任何一件之前都生效）

1. **不杜撰。** 经历、职责、数字全部来自机主真实提供。可以引导、追问、帮她把模糊的
   说清楚，**绝不替她编 Action 和 Result**。缺的写 `待补`，不留空也不编。
   数字写库前回问一遍：「我记成 ___，对吗？」外部验证类条目（star / 奖项 / 专利）写进
   任何交付物前必须点开原始出处确认署名是她（09-01 drift-bottles 事故）。
2. **先查库，再开工。** 任何题进来先读 `state/jobhunt/stories/_index.md` 和 `jd_list` 看有没有能
   复用的；几个岗有一大半准备是重复的（自我介绍、项目怎么讲、高频追问），
   不许一岗重做一遍。
3. **证据分级：`unknown` 永远不许变成一个看着合理的值。** 打分、写手册、比较岗位前
   先读 `frameworks/evidence-ranking.md`——那条规则治的是已经翻过的车。

## 路由表

| 场景 | 读哪个 | 状态 |
|---|---|---|
| 扒到一条岗 / 「这个该不该投」 | `prompts/jd-decode.md` | 可用（草稿） |
| 准备手册前 / 「这家什么来头」 | `prompts/company-intel.md` | 可用（草稿） |
| 决定投了，出定向简历 | `prompts/resume-tailor.md` | 可用（草稿） |
| 「项目经历没亮点」「毕设怎么写」「这个项目怎么包装」 | `prompts/project-writeup.md` | 可用（草稿） |
| 【主菜】要面试了，出面试手册 | `prompts/interview-manual.md` | 可用（草稿） |
| 手册里发现「这题没素材」／刚踩完一件真事想拿去讲 | `prompts/story-mining.md` | 可用（草稿） |
| 手册备完，要练 | `prompts/mock-interview.md` | 可用（草稿） |
| 岗位状态卡住（没回音 / 等排期 / 探活 / 找内推） | `prompts/outreach.md` | 可用（草稿） |
| 真面完了，当天复盘 | `prompts/interview-debrief.md` | 可用（草稿） |

流水线顺序：解码 → 定向简历 → 情报 → 手册 → 挖故事 → 模拟 → 真面 → 复盘 → 下一轮手册。
每篇都是「草稿版，跑完第一个岗后回来修」：**跑真岗时踩到的坑记回文件里**，别只记在聊天里。

## 框架（查表用，不是流程）

- `frameworks/evidence-ranking.md` 四档证据 + 硬 cap
- `frameworks/jd-signals.md` 国内 JD 词典 + 隐藏信号 + 红旗
- `frameworks/competency-tags.md` 十二个能力标签 + 题型反查
- `frameworks/star-car.md` STAR/CAR/SOAR + 黄金配比 + 一稿多用

## 数据层（机主的真实资产，全在 `state/jobhunt/`，不进 git）

- 个人档案 / 简历：`profile_get`、`resume_list/read/save/render`
- 岗位库和打分：`jd_save/list/read/score`（打分必须走 evidence-ranking）
- 投递台账：`applications_list`；投递信：`email_draft`（出不了门，落草稿箱等机主确认）
- 故事库 `stories/`、公司情报 `intel/`、手册 `manuals/`：约定见各流程文件
- 真题库 `questions.md`（面后复盘攒，模拟面试第一题源）、撤回声明 `retracted.md`
  （扛不住追问的说法，硬闸）、会话记录 `sessions/`（模拟和真面都记，记的是她实际说的）

## 成品去向

面试手册的成品形态是**网页工作台**（可勾选、可笔记、进度能存）——内容做完后
`skill_read("workbench")` 走交付层，别自己现编一个网页。

来源与许可见 `SOURCES.md`。
