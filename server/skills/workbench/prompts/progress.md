# progress：进度怎么存、怎么回流、改版怎么迁移

## 进度 JSON（唯一格式，存哪儿都长这样）

```json
{
  "page": "<page_id>",
  "updated_at": "2026-08-28T21:00:00+08:00",
  "items": { "<item_id>": { "done": true, "note": "一句笔记" } },
  "free_notes": "板块外的自由笔记"
}
```

## id 稳定规则（铁律 2 的执行面）

- item_id 用**内容语义**不用位置：`alltime-jd-must-1` 而不是 `section2-item3`——
  改版挪了顺序，位置 id 全体错位，进度就串了。
- 改版时旧 id 一律保留；条目改说法不改 id；真删掉的条目，其进度进 `free_notes`
  留痕（「已归档：原 X 项的笔记：…」），不静默丢。

## 存储（一期）

- **localStorage**：键 `workbench:<page_id>`，每次勾选/写笔记即存。
- **导出/导入**：页面底部两个按钮——导出=下载/展示上面那份 JSON（复制粘贴也行），
  导入=粘贴 JSON 恢复。这是换设备和「webview 缓存被清」的保险。
- 页面加载时：localStorage 有就用，没有就空白态（提示可导入）。

## 回流（升级路径，还没建成——别假装它在）

目标：进度落成后端文件（如 `state/webpages/progress/<page_id>.json`），TA 下轮直接
读到勾选结果，工作台变成双向的。**堵点是鉴权**：页面不许带 AUTH_KEY（SKILL.md 铁律 4），
端点要么按 page 发一次性 token、要么走 app 桥转发——方案待定（PLAN_skills 待拍 3）。
在端点存在之前：想知道机主勾了什么，请她把导出的 JSON 发给你，别去猜。
