# PLAN_interject §5 P0 探针补测 a–e 报告（2026-10-29）

环境：`server/.venv` claude-agent-sdk 0.2.174（自带 CLI 2.1.310），订阅登录态，cwd=`server/tools`。
只改了 `server/tools/interject_probe.py`（加 `verify` / `image` / `toollist` 三个模式和 `--model` / `--hooks` / `--watch` / `--logdir` 四个开关），没碰产品代码、没碰 git。
本目录（`/private/tmp/claude-501/-Users-nemu-cassette/c416d283-6109-4178-bede-00b78d15c8fb/scratchpad/step1/interject/`）里：

| 文件 | 内容 |
|---|---|
| `fb98bcbc_tool_haiku_1028.jsonl` | 昨晚 tool 模式 transcript 原件副本 |
| `a_verify_haiku.log` + `verify_claude-haiku-5-5_7e5717b5-….jsonl` | a 腿 |
| `a_verify_haiku_run0_premerged.log` + `verify_…_78b78083-….jsonl` | a 腿第一次跑（插话撞到首条之前，意外收获，见 §f-7） |
| `b_image_haiku.log` + `image_…_3e8f34ec-….jsonl` | b 腿 |
| `b2_toollist_haiku.log` + `toollist_…_68f2fb77-….jsonl` | b 腿补充：纯文字但 content 是列表 |
| `c_text_hooks_haiku.log` / `c_tool_hooks_haiku.log` / `c_image_hooks_haiku.log` + 对应 jsonl | c 腿 |
| `e_text_opus.log` / `e_tool_opus.log` / `e_tool2_opus.log` + `*_opus_*.jsonl` | e 腿 |

日志里 `S:` 前缀 = SDK 流里到达的单调时刻，`T:` 前缀 = 该行首次在 transcript 文件里被读到的单调时刻（20ms 轮询），`HOOK` = hook 回调时刻。

---

## a. 被吸收的插话渲染给模型的原文

**结论：模型看到的是一条 `role=system` 的消息（不是 user），正文是固定英文包装 + 插话原文，没有任何时间；插话原文是一字不改嵌在里面的。模型自己复述得一字不差，但把它当成「用户发的」。**

证据（昨晚 `fb98bcbc…jsonl`，今天 `verify_…7e5717b5…jsonl` 同形，行号一致）：

- 第 27 行 `attachment type=queued_command`，`parentUuid` = 第 25 行那条 tool_result user 的 uuid，`renderedRole:"system"`，`rendered[0].content` 全文：
  ```
  <system-reminder>
  The user sent a new message while you were working:
  补充：橘子换成葡萄

  This is how Claude Code surfaces messages the user sends mid-turn — within the running turn, often alongside the next tool result, rather than as a separate conversation turn. Address the message above as you continue this turn.
  </system-reminder>
  ```
  注意这行的 `timestamp` 和 `attachment.timestamp` 都是 **enqueue 时刻**（19:39:35.755），不是落盘时刻（它在文件里排在 19:39:51.59 的 tool_result 后面）。
- 第 30 行 `queue-operation remove reason=absorbed_mid_turn`，`content` = 原文，`commandUuid` = 第 27 行的 `source_uuid`。
- 第 33 行 `api-request-blob`，**`message.role = "system"`**，`content[0].type=text`，正文开头就是上面那段（**不带 `<system-reminder>` 标签**），紧接着在**同一个 text 块里**拼了 `The following tools just became available…` 工具清单（claude.ai 的 Docs/Notion 连接器延迟挂上来的 `deferred_tools_delta`，跟探针环境有关，生产不一定有），后面 75 个 `tool_addition` 块。没有任何时间戳、日期。
- 第 34 行 `api-request`，`tail` 的第三个 hash = 第 33 行的 hash，证明这条 system 消息就是本次请求 messages 尾部的最后一条。

两条插话（opus tool2，`tool2_opus_ce7c1004…jsonl` 第 32 行）：两段包装**各自完整**、顺序拼在同一个 system text 块里（「The user sent a new message…补充一…Address the message above…\n\nThe user sent a new message…补充二…」），然后才是工具清单。

交叉验证（`a_verify_haiku.log` 39.213s 那条 Assistant TEXT，haiku）：模型把包装连同插话逐字复述，与第 33 行 blob 的 text 完全一致。它自答：① 「以 user 消息的形式出现」（**错**，实际 role=system——模型分不出 role，只看文字）；② 发送者「你本人」；③ 「没有带时间戳或日期」（对）；④ 「和工具列表不在同一段里」（**错**，实际在同一个 text 块里；模型按语义分段）。

**回答 §4.2 的问题：**
- 要不要自己加时间头：CLI 不加，模型看到的插话没有任何时间。cassette 正常轮的注入是带时间锚的（§14 时间锚统一），插话想保持一致就得把时间头写进原文。
- 要不要自己加人称包装：CLI 已经包了一层「The user sent a new message while you were working」，再包一层第二人称会双重包装；但这层包装是英文、system 口吻，跟 cassette 的「信感」格式不一致——这是待拍，不是本腿能定的。
- 配对用的原文是哪一段：`queue-operation remove` 的 `content` 和 `queued_command.prompt` 都是**传给 `client.query()` 的原字符串**（含时间头的话一起算），配对用它；它跟 blob 里的包装无关。

## b. 插话带图片

**结论：带图的插话照样 mid-turn 吸收，图完整到模型眼前（haiku 两次都答「纯红色」），但渲染形状变了——变成 `role=user` 消息的尾巴，不再是 system 消息。**

证据（`image_…3e8f34ec…jsonl`）：
- 第 27 行 `queued_command`：`prompt` 是**整个 content 列表**（text 块 + image 块，base64 原样），`rendered: null`、没有 `renderedRole`。
- 第 30 行 `remove absorbed_mid_turn`，`content` 只有文字部分 `"新消息里这张图是什么颜色？"`（**图块不在配对内容里**，只看 text 块拼接）。
- 第 33 行 `api-request-blob` **`role=user`**：`[tool_result, tool_result, text, image]`——两条工具结果之后挂一个 text 块（内容是 **带 `<system-reminder>` 标签** 的同一段包装 + 插话文字）再挂 image 块（`image/png`，176 字节 base64 = 原图）。
- 第 34 行另一条 blob `role=system` 只装工具清单（deferred tools delta），和插话分开了。
- 第 35 行 `api-request` 的 `tail` 同时含这两个 hash。
- 流里：`b_image_haiku.log` 23.554s 模型答「图片是纯红色（接近 #FF0000）的实心方块」；`c_image_hooks_haiku.log` 22.874s 同。

补充（`toollist_…68f2fb77…jsonl`）：content 是**只有 text 块的列表**时，仍走 system 路（第 27 行 `renderedRole=system`、第 33 行 blob `role=system`）。所以决定 role 的是**有没有非文字块**，不是 str/list 形状。

对 §0「带图片的插话 v1 照旧排队」：技术上不需要排队，吸收、配对（按文字）、送图都通。只是两种 role 形状不同，重铸时都变普通 user 轮，无影响。

## c. UserPromptSubmit hook 能不能替代读 transcript

**结论：能，而且比读 transcript 更早、更干净。hook 对两种结局都触发（每次 query 一次），靠 `prompt_id` 分辨：被吸收 → `prompt_id` 等于在飞那轮的 prompt_id；另起一轮 → 新 prompt_id。触发时机在工具边界，先于任何 transcript 行落盘。**

| 模式 | hook 次数 | hook#2 时刻 / 位置 | hook#2 `prompt_id` |
|---|---|---|---|
| text（haiku，2 个 result） | 2 | 9.990s：result#1 (9.983) 之后、init#2 (9.992) 之前、新 user 行落盘 (10.106) 之前 | `2e448a71`≠ 首轮 `fda8cea5`（**新**） |
| tool（haiku，1 个 result） | 2 | 19.981s：流里 tool_result#2 (19.978) **之后 3ms**；transcript 的 `queued_command`/`remove`/`api-request` 在 20.088–20.091 才落盘 | `0d41e663` **= 首轮**（同一个） |
| image（haiku，1 个 result） | 2 | 20.171s：tool_result#2 (20.169) 后 2ms | `c8945179` = 首轮 |

hook_input 原文（`c_tool_hooks_haiku.log` 末尾）：
```
{"session_id": "cf92b571-…", "transcript_path": "/Users/nemu/.claude/projects/-Users-nemu-cassette-server-tools/cf92b571-….jsonl",
 "cwd": "/Users/nemu/cassette/server/tools", "prompt_id": "0d41e663-cf08-4b00-b04a-838888ed6bb9",
 "permission_mode": "default", "hook_event_name": "UserPromptSubmit", "prompt": "补充：橘子换成葡萄"}
```
- `prompt` 是**纯文字**：带图的插话 hook 里 `prompt` 只有文字块（`"新消息里这张图是什么颜色？"`），图不在 hook_input 里（`c_image_hooks_haiku.log`）。
- 首条 query 也触发一次（hook#1，0.89s，给出在飞轮的 prompt_id）——引擎在轮开头就拿得到「这轮的 prompt_id」，后面每个 hook 只需比对。
- hook 没有 `parentUuid`/tool_result uuid，不直接告诉你「挂在哪个 tool_result 后面」；但它在流里的 tool_result 之后 2–3ms、下一条 AssistantMessage 之前（≥1.8s）到达，引擎按「最近一条 tool_result」归位即可。
- 没验：hook 里 `prompt` 配对是否也受「提及即使用」一类影响——它是原文，跟 transcript 一样需要 rid 标记。

## d. transcript 写盘时机

**结论：引擎在流里 tool_result 到达那一刻去读 transcript，读不到 `queued_command`——连那条 tool_result 自己的 user 行都还没落盘。所有相关行（tool_result user 行、queued_command、remove、api-request-blob、api-request）在流事件之后约 105–120ms 一起落盘（同一个 20ms 轮询窗口内全到）。到下一条 AssistantMessage 时（≥1.8s 后）早就齐了。**

五次采样（单调秒；「T 落盘」=五行里第一行到最后一行的首见区间）：

| 运行 | S:tool_result | T 落盘 | 差 | 下一条 S:assistant | 余量 |
|---|---|---|---|---|---|
| verify haiku | 19.819 | 19.925–19.927 | +106~108ms | 26.681 | 6.75s |
| image haiku | 20.663 | 20.775–20.777 | +112~114ms | 22.786 | 2.0s |
| tool haiku（hooks） | 19.978 | 20.088–20.091 | +110~113ms | 22.907 | 2.8s |
| tool opus | 14.013 | 14.132–14.135 | +119~122ms | 18.127 | 4.0s |
| tool2 opus | 11.312 | 11.425–11.428 | +113~116ms | 13.283 | 1.85s |

CLI 自己打的 timestamp（文件内，`fb98bcbc` 昨晚那份 24/25/30/34 行）：tool_result user 行 51.591 → remove 51.609 → api-request 51.624，内部间隔 18+15ms；今天四次分别 11/13、8/11、18/16 ms——CLI 是先写 user 行再写 queued_command/remove 再写 request，但落盘是攒在一起刷的（看不出先后差），落盘整体比流晚 ~110ms。没有插话的工具边界也一样（例如 verify 的 tool_result#3：34.811 → 34.924 落盘，+113ms）。

另：`queued_command` 行的 `timestamp` 字段 = enqueue 时刻（比它前后的行都早十几秒），**不能拿它当落盘/吸收时刻**；吸收时刻看 `remove` 行的 timestamp。

对 §4.2「流里每来一条 tool_result 就查 transcript」：直接查会读早；要么等到下一条 AssistantMessage 再查（余量 ≥1.8s），要么轮询到那条 tool_result 的 uuid 出现在文件里再往后看，要么——按 c 腿——直接用 hook，不读文件。

## e. 用 opus 重跑 text / tool / tool2

**结论：结局数、吸收点、transcript 形状与 haiku 一致；唯一差别是 opus 工具调用是一次一个（串行），haiku 是一批两个（并行），所以「已发出的工具照跑、下一步才改口」那句在 opus 下表现为「第一个查完就改口，橘子压根没查」。**

| 模式 | haiku | opus（`--model opus`，生产同款别名） |
|---|---|---|
| text | 2 个 result；第二个 `init`；插话另起一轮（新 promptId） | **2 个**；`text_opus_cc3ab90b…jsonl` 22 行 enqueue → 26 dequeue → 27 新 user 行（promptId `5bec83a0`≠`eb89460e`）；流里 23.322s 第二个 init |
| tool | 1 个；两条 tool_use 并行发出，插话在两个 tool_result 都回来后、第二次请求前被吸收；橘子照查、再补查葡萄 | **1 个**；`tool_opus_d2f615ca…jsonl`：只发一个 tool_use（苹果），24 行 tool_result → 26 行 queued_command（parent=24 行 uuid）→ 28 remove absorbed → 31 blob role=system → 32 api-request；模型说「好，橘子换成葡萄」直接查葡萄，**橘子没查** |
| tool2 | 1 个；两条在同一个边界一起吃进去 | **1 个**；`tool2_opus_ce7c1004…jsonl` 25/26 两行 queued_command 挂同一父（23 行 tool_result）、28/29 两行 remove、32 行一个 system blob 装两段包装；模型「收到：橘子换成葡萄，最后再加查西瓜」依次查葡萄、西瓜 |

流里出现了 `RateLimitEvent` 消息（opus 三次各 1–2 条，haiku 没有），只是告知，没有 429、没有重试、不影响结局数。

## f. 跟 PLAN_interject 现有文字对不上的地方（只列事实）

1. **§1「模型实际看到的渲染原文【待验】」** → 已验：role=system、固定英文包装、无时间；且与同一次请求里别的 system 附件（如工具变更清单）拼在同一个 text 块里。带非文字块（图）时改为 role=user 尾巴（tool_result 之后）。
2. **§1「被吃进当前轮的插话在模型历史里不是一条 user 消息，是一个附件」** → 对一半：纯文字是一条 **system 消息**；带图是**挂在 tool_result 那条 user 消息末尾**的 text+image 块。
3. **§1 表「已经发出去的工具调用照跑（「橘子」照查），下一步才改口」** → 这是 haiku 并行发两个 tool_use 的副产物；opus 串行调用时「橘子」根本没被查。吸收点的定义（「当前这批工具调用全部返回之后、下一次请求模型之前」）本身不变。
4. **§0「带图片 / 文件的插话：v1 照旧排队，等 P0-b 验完再说」** → P0-b 验完：图能吸收、能到模型、配对内容只含文字块。文件块没验（只验了 image）。
5. **§4.2 吸收判定「主证据 = transcript 的 remove absorbed_mid_turn … 时机：流里每来一条 tool_result UserMessage，查 transcript」** → d 腿实测这一刻读不到（晚 ~110ms）；c 腿给出替代：`UserPromptSubmit` hook 对两种结局都触发，`prompt_id` 等于在飞轮的即被吸收，在 tool_result 后 2–3ms 到、先于一切落盘。§4.2 写的「备选信号【待验，P0-c】：是否只对『另起一轮』那种触发」——答案是**不是**，它两种都触发，但可以用 prompt_id 分辨，效果反而更好。
6. **§4.2「插话原文一个字都不加 … 配对按原文配 queued_command」** → 配对字段确认是 `remove.content` / `queued_command.prompt`（str 时）或各 text 块文字（list 时），hook 的 `prompt` 字段也是同一串文字。三处一致。
7. **新事实，plan 里没有**：插话若在 CLI 还没 dequeue 首条时就 enqueue（本次第一跑：client 启动 3.5s，两条 query 几乎同时发），两条会被**拼成一条 user 消息**（`\n` 连接、同一个 promptId、只一个 user 行，`verify_…78b78083…jsonl` 1–5 行），没有 queued_command、没有 absorbed_mid_turn。对 §4.2 路由：「在飞」的判定必须在 CLI 真正 dequeue（流里 init / hook#1 / transcript user 行）之后才算数，否则插话会被当成首条的一部分，既配不上也分不开。
8. **§1「每次 query() 先记一条 enqueue」** → 对；补一点：enqueue 行落盘比 `client.query()` 返回晚 ~75–120ms（`query_sent#2` 3.938 → `T:enqueue#2` 4.015 等）。
9. **§6「升级前必跑清单」** → 2.1.310 多了 `api-request` / `api-request-blob` / `prompt_snapshot` / `deferred_tools_delta` 等行，`queued_command` 多了 `source_uuid` / `delivery_id` / `rendered` / `renderedRole` 字段；探针 `classify()` 已按这些字段写，自检时可一并核。
