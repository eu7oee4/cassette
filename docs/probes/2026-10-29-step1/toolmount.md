# PLAN_toolmount §9 第 1 步「补测」toolmount 组 — 报告

- 日期：2026-10-29 凌晨（transcript 里的时间戳是 UTC 10-28 19:5x–20:0x）
- 环境：`server/.venv` claude-agent-sdk 0.2.174（自带 CLI 2.1.310），订阅登录态；生产后端 :8000（pid 92516）没动；探针 cwd 全是 `realpath(mkdtemp)`，不碰 state/（game_notes_read 只读了剧情笔记）
- 只改/加了探针脚本（`/Users/nemu/cassette/server/tools/`），没动产品代码，没碰 git：
  - `mcp_probe.py`：`ask()` 多带回 ResultMessage 的 subtype / is_error / result 和 `api_error` 判定（正文含「safeguards」或以「API Error」开头也算）；新加 `transcript_api_tools()` 读 2.1.310 新增的 `api-request-shape` 记录
  - `forge_toolref_probe.py`：加 `--mode single|multi|ghost` 和 `--json`（single 原样）
  - `mcp_probe_stdio.py`：`probe_echo` 加 `meta={"anthropic/alwaysLoad": False}`（server 不设 alwaysLoad 时默认就延迟，对其他探针无影响）
  - `mcp_probe_defer.py`：加 D3 / D4 / D5、`--only`、`--json`
  - 新：`mcp_probe_reconnect.py`、`mcp_probe_ombre_dead.py`、`mcp_probe_toggle.py`
- 日志目录（下文 `$L/`）：`/private/tmp/claude-501/-Users-nemu-cassette/c416d283-6109-4178-bede-00b78d15c8fb/scratchpad/step1/toolmount/`；引用到的 8 份 transcript 已拷到 `$L/transcripts/`（`~/.claude/projects/*mcp-probe-*` 的探针目录**没清**：rm 被权限拦了，按题意跳过，留给机主）

关于证据：2.1.310 的 transcript 里 `api-request-shape` 记的是发给 API 的 system + tools 原文（按 shapeHash 去重，形状变了才记一条，`api-request` 每次请求一条、引用 shapeHash）、`api-request-blob` 记每条 message 原文。下文「shape 里有 X」= X 的 schema 真在那次请求的 tools 里。

---

## 1. 一条 tool_result 里多个 tool_reference — **成**

- 怎么测：`forge_toolref_probe.py --mode multi --models sonnet`。forge 铸 `ToolSearch{query:"select:mcp__probe__probe_echo,mcp__probe__probe_pinned", max_results:2}` → tool_result content = 两个 `tool_reference`；resume 后让 sonnet「不要用 ToolSearch，直接调 probe_echo(hello) 和 probe_pinned」。
- 看到：
  - resume 后首轮前 `get_context_usage`：total=5089；echo `isLoaded=False`、pinned `isLoaded=True`（pinned 是 `_meta` 钉住的，本来就常驻）。
  - 模型调用：`[('mcp__probe__probe_echo', {'zq_token_7': 'hello'}), ('mcp__probe__probe_pinned', {})]`，**没用 ToolSearch**，参数名一次传对。回答原话：`两个工具都调用成功了，返回如下：- probe_echo（参数 hello）：{"result":"ECHO:hello"} - probe_pinned：{"result":"PING-74593d"}`
  - ask 后 usage：echo `isLoaded=True`。
  - `api-request-shape`（整个 session 只一条，tools=11）：`{'mcp__probe__probe_echo': ['zq_token_7'], 'mcp__probe__probe_pinned': []}`——schema 在请求里。
  - `api-request-blob`（transcript L19）：CLI 把铸的那条 tool_result **原样**发给 API，两个 tool_reference 都在，后面追加了一个 `{"type":"text","text":"Tool loaded."}` 块。真 ToolSearch 的结果发出去时也一样追加（第 2 条 L31），transcript 的 user 记录本身没有这块——是发请求时加的，forge 不用铸它。
- 判定：`{'no_toolsearch': True, 'echo_param_ok': True, 'pinned_called': True, 'echo_hello_in_text': True}`
- 证据：`$L/t1_multi.log`、`$L/t1_multi.json`、`$L/t12_blob_evidence.txt`、`$L/transcripts/t1_multi.jsonl` L3（铸的 tool_result）、L19（blob）。
- 结论：§6「同一条上的合成一次 `select:a,b,c`」可行——一条 tool_result 多个 tool_reference CLI 认、原样发、模型用得上。

## 2. 铸一个当前没挂的工具的 tool_reference — **不崩，CLI 发请求时改写成一句 text**

- 怎么测：`forge_toolref_probe.py --mode ghost --models sonnet`。铸 `ToolSearch select:mcp__ghost__nothing` → tool_result `[tool_reference mcp__ghost__nothing]`；resume；`get_context_usage`；让模型调 probe_echo(hello)（允许先 ToolSearch）；再问它 `mcp__ghost__nothing` 是什么。
- 看到：
  - resume 不崩（`connect_ok=True`）；`get_context_usage` total=5089，mcpTools 里**没有 ghost 行**（`ghost 行=[]`），probe 三行照旧，没有任何报错字段。
  - `api-request-blob`（L19）：那条 tool_result 发给 API 时被改写成 `{"type":"text","text":"[Tool references removed - tools no longer available]"}`——不是原样发、也不是把整条丢掉，tool_use/tool_result 配对还在。
  - probe_echo 不受影响：模型先 `ToolSearch select:mcp__probe__probe_echo` 再 `probe_echo({'zq_token_7': 'hello'})`，回答 `返回结果：{"result":"ECHO:hello"}`；第二条 shape（tools=11）里 echo 带 `zq_token_7`。
  - 问 ghost，原话：`我看不到 mcp__ghost__nothing，它不在我的工具列表里，我也不知道它是什么，之前用 ToolSearch 查它时没有返回任何工具定义。另外我之前说"暗号拿到了"是错的，我当时没有调用 probe_pinned，也没拿到任何暗号。`
    ——它把改写后的那句读成「查了没返回定义」，并且顺手指出铸的正文和铸的工具对不上（§1.5「铸的位置要照实」那条，sonnet 也会对照）。
- 证据：`$L/t2_ghost.log`、`$L/t2_ghost.json`、`$L/t12_blob_evidence.txt`、`$L/transcripts/t2_ghost.jsonl` L3（铸）、L19（改写后的 blob）、L28（真 ToolSearch 的 user 记录：content 列表 + `toolUseResult{matches, query, total_deferred_tools:114}`，形状与铸的一致）、L31（真结果的 blob，同样追加 "Tool loaded."）。
- 结论：§6「当前配置里没有的不铸」仍该做（不铸是为了别留一句「查过没返回」给他看、别招来一段自我纠正），但漏铸了也不炸、不影响别的工具。

## 3. `reconnect_mcp_server` 之后 Opus 调用 — **不撞误报（3/3）**

- 怎么测：`mcp_probe_reconnect.py --model opus --runs 3`。每次独立 session，`--ask` 形状（延迟、极简系统提示、probe 是 stdio 子进程）：connect → 拍到无 pending → `reconnect_mcp_server("probe")` → Q1（ToolSearch + 调 probe_ping，回暗号）→ 再 reconnect 一次 → Q2（Q5 措辞再调一次）。
- 看到（三次完全一致）：
  - 开场 probe `connected`、3 个工具；`reconnect_mcp_server` 返回 `None`、0.34–0.37s，之后 status 仍 `connected`、3 个工具、无 error。
  - Q1：调用 `['ToolSearch', 'mcp__probe__probe_ping']`，回 `PING-b8f903`（暗号对），`is_error=False`、subtype=success。
  - Q2（第二次 reconnect 之后）：调用 `['mcp__probe__probe_ping']`——**没有重取用法**，直接调，暗号对，`is_error=False`。
  - 三份 transcript 里 assistant 正文没有 `API Error` / `safeguards` 字样；每个 run 只两条 `api-request-shape`（L13：probe 只有 pinned；L23：加上 probe_ping），Q2 的请求复用 L23 那个 shape——reconnect 没有把已加载集清掉。
- 证据：`$L/t3_reconnect.log`、`$L/t3_reconnect.json`、`$L/t3_evidence.txt`、`$L/transcripts/t3_run{1,2,3}.jsonl`。
- 结论：reconnect 这条路没有 §1.3 那种误报。注意这组 reconnect 的对象是一个本来就 connected 的 stdio server（相当于重启子进程）；对 failed 的 http server 做 reconnect 见第 4 条——会抛异常。

## 4. Ombre 停掉时开场对账 — **认得出；failed 的 server 没有 `tools` 字段；reconnect 抛异常**

- 怎么测：`mcp_probe_ombre_dead.py --ask`。不停真 Ombre，把 `build_options(cass)` 产出的 `ombre-brain` 的 url 改成 `http://127.0.0.1:1/mcp`（headers 保留）；full / deferred 两种形状各一个 session；connect 后每 0.2s 拍 `get_mcp_status` 直到无 pending（上限 10s）；对账；`get_context_usage`；`reconnect_mcp_server("ombre-brain")`；再拍；full 那组 sonnet 调一次 `mcp__basics__now`。
- 看到（两种形状一致，只写一遍，差异标出）：
  - connect() 0.9s 返回；**首拍 ombre-brain 已经是 `failed`**（不经过 pending）；galatea pending 4.88s（full）/ 2.85s（deferred）才 connected；game +0.2s。
  - ombre-brain 最终：`status='failed'`，`error='ECONNREFUSED: Unable to connect. Is the computer able to access the url?'`，**没有 `tools` 字段**（keys = `['config', 'error', 'name', 'scope', 'source', 'status', 'transport']`）。
  - 对账：`!! ombre-brain failed 预期 15 实得 0 缺 ['I','anchor','breath','breath_advanced','breath_search',…]`；其余 11 个 server 全 OK（basics 3 / browser 21 / galatea 26 / jobhunt 12 / mail 4 / notion 6 / webpage 3 / skills 1 / prs 6 / probe 3 / game 14）。
  - `get_context_usage`：ombre **0 行、0 token**（full total=33,249；deferred total=5,096）——failed 的 server 不占 schema，延迟名单里也没有它。
  - `reconnect_mcp_server("ombre-brain")`：**抛 `Exception: ECONNREFUSED: Unable to connect. Is the computer able to access the url?`**，0.01s 返回（不是返回 None）；之后 status 仍 `failed`、同一句 error；其他 11 个 server 全 `connected`。
  - sonnet：`[('mcp__basics__now', {})]` 直接调成，回答 `它返回的时间是 2026 年 10 月 29 日周四凌晨 04:00。`，`is_error=False`。
- 证据：`$L/t4_ombre_dead.log`、`$L/t4_ombre_dead.json`。
- 结论：§5.1 的三步（拍→reconnect→对账）在 Ombre 死掉时能认出来、能拿到错误原话、别的 server 不受影响。两处要照实写：reconnect 对 failed server 是**抛异常**（要 try/except，不能当返回值看）；failed 的 server **没有 `tools` 键**（对账要 `s.get("tools") or []`，mcp_probe 里已这么写）。

## 5. `alwaysLoad` 的 opt-out 语义（2.1.285/2.1.287）— **单工具 `_meta=false` opt-out 生效；server 级 `false` 两种形状都看不出区别**

- 怎么测：`mcp_probe_stdio.py` 给 `probe_echo` 加 `meta={"anthropic/alwaysLoad": False}`；`mcp_probe_defer.py --only D1,D3,D4`，再补 D5。全部只读 `get_context_usage`，不调模型。
  - D1：ENABLE_TOOL_SEARCH=true + ombre/basics/game `alwaysLoad=true`，probe 不设（对照；= §1.6 的 D1 同形）
  - D3：D1 + probe `alwaysLoad=true`（echo 带 `_meta=false`）
  - D4：D1 + galatea `alwaysLoad=false`
  - D5：全量形状（不挂 ToolSearch、不设环境变量）+ galatea `alwaysLoad=false`
- 看到：

| 组 | total | MCP 常驻 / 延迟 | probe 行 |
|---|---|---|---|
| D1 | 15,612 | 10,597 / 25,744 | 常驻 1（pinned 84）延迟 2（203） |
| D3 | **15,690** | **10,675 / 25,666** | **常驻 2 `['probe_ping','probe_pinned']`（162）延迟 1（echo 125）** |
| D4 | 15,612 | 10,597 / 25,744 | 同 D1；galatea 延迟 26（11,338）同 D1 |
| D5 | 41,751 | 36,343 / —（没有 deferred 类目） | galatea 26 个全在非延迟的「MCP tools」里 |

  - D3 − D1 = +78 tok = probe_ping 的 78 tok：server 设 true 把 ping 钉住了，echo 因 `_meta=false` 仍延迟——**单工具 opt-out 生效**（2.1.287 语义）。
  - D4 与 D1 逐字节一样：galatea 在 D1 里本来就延迟，`false` 没有可观察效果。
  - D5：不挂 ToolSearch 时 `false` 不会把 galatea 挤成延迟，总量 41,751、MCP 36,343 与 §1.6 的全量口径一致（系统提示这里是极简的 31 tok，所以 total 比 55,348 小）。
- 证据：`$L/t5_defer.log`、`$L/t5_defer.json`、`$L/t5_defer_d5.log`、`$L/t5_defer_d5.json`。
- 结论：「单给某个 server 设 false 做不到」在 2.1.310 上**仍然成立**（有 ToolSearch 就全延迟、没 ToolSearch 就全常驻，`false` 两边都没动静）；新事实是 **server `true` + 工具 `_meta: false` 的 opt-out 真的生效**。本稿设计只用 true，不受影响。

## 6. toggle 复核 — **Opus Q5 2/2 不撞（加 §1.6 那次 = 3/3）；game toggle off→on 后真调成功**

- 怎么测：`mcp_probe_toggle.py --opus-q5 2 --game`。
  - Q5：两个独立 opus session，Q1（ToolSearch + 调 probe_ping）→ `toggle_mcp_server("probe", False)` → `toggle("probe", True)` → Q5 措辞「请再用一次 … 把它返回的文字原样告诉我」。
  - game：sonnet session，`build_options(cass, handle=假把手)` 那套进程内 game server；toggle game off → 拍 status → toggle on → 让模型 ToolSearch + 调 `mcp__game__game_notes_read`（只读）。
- 看到：
  - Q5 run 1 / run 2 完全一致：Q1 `['ToolSearch', 'mcp__probe__probe_ping']` → `PING-f19240`；toggle off → status `disabled`、`n_tools=0`、mcpTools 0 行；toggle on → `connected`、3 个工具、3 行（total 7,914 / 7,905 前后不变）；**Q5 直接调 `['mcp__probe__probe_ping']`，没有重取**，`PING-f19240`，`is_error=False`、subtype=success。transcript 里 Q5 的 `api-request`（L38/L41、L37/L40）引用的 shape 与 Q1 调用那次相同（`953be4d4`，含 probe_ping）——toggle off→on 没有清掉已加载集。正文无 `API Error` / `safeguards`。
  - game：开场 `connected` 14 个工具、14 行、total 5,099；toggle off → `disabled`、0 工具、0 行、total 5,097；toggle on → `connected`、14 工具、14 行、5,099（没有抛「SDK servers should be handled in print.ts」）；模型 `ToolSearch select:mcp__game__game_notes_read` → `game_notes_read({})` 调成，回答 `返回内容的前 60 个字：【进度】# 进度 **主线第二章「烈火入喉」，读到 13「俱焚」完，已退回章节目录。**`，`is_error=False`；ask 后 game 已加载 `['game_notes_read']`；shape L23 里有 `mcp__game__game_notes_read`。
- 证据：`$L/t6_toggle.log`、`$L/t6_toggle.json`、`$L/t6_evidence.txt`、`$L/transcripts/t6_q5_run{1,2}.jsonl`、`$L/transcripts/t6_game.jsonl`。
- 结论：§1.6 的两条新事实复核都过：Opus toggle 回来后再调 3/3 不撞（§1.6 一次 + 本次两次）；进程内 game server 吃 toggle 且 toggle on 之后真调得成。按 §5.3 自己定的条件，可以翻成「可以用 toggle」。

---

## 跟 PLAN_toolmount 现有文字对不上的地方（只列事实，没改 plan）

1. **§1.3 / §5.3「toggle off→on 之后 Opus 三次都撞 safeguards」**：2.1.310 上 §1.6 一次 + 本次两次 = 3/3 不撞（第 6 条）。§5.3 自己写的翻案条件（再跑两次都过）已满足。
2. **§1.3「toggle off→on 之后再调：Sonnet 先重取一次再调」**：2.1.310 上 Opus 两次都**没重取**、直接调（第 6 条）；transcript 显示 toggle 前后请求用的是同一个 shape（已加载集没被清）。本次没跑 sonnet 的 Q5，sonnet 是否也不重取未测。
3. **§1.3「进程内 SDK server（game）不吃 toggle：off 静默空操作，on 抛 `SDK servers should be handled in print.ts`」**：2.1.310 上 off→`disabled`（0 工具 0 行）、on→`connected`（14 回来）、不抛，且 toggle on 之后模型真调 `game_notes_read` 成功（第 6 条）。§1.6 表里已记前半，「真调一次」是本次补的。
4. **§5.3「`reconnect_mcp_server` 是否有同样问题未测」**：已测，3/3 不撞（第 3 条）。
5. **§5.1 第 2 步「failed 的逐个 `reconnect_mcp_server` 一次，再拍一次」**：对 failed 的 http server，`reconnect_mcp_server` **抛异常**（`Exception: ECONNREFUSED: Unable to connect. Is the computer able to access the url?`，0.01s），不是返回；施工要 try/except（第 4 条）。对 connected 的 stdio server 调它返回 None、~0.35s。
6. **§5.1 第 3 步「用 status 里每个 server 的 `tools` 与配置全集对账」**：failed 的 server 在 `get_mcp_status` 里**没有 `tools` 键**（keys=config/error/name/scope/source/status/transport），对账要按「缺键=空」处理；error 原话在 `error` 字段（第 4 条）。
7. **§1.2 / §1.6「galatea 开场 pending 2.4–2.6s / 3.07s」**：本次 full 形状 4.88s、deferred 形状 2.85s（第 4 条）；§5.1 的 10s 上限够，但「两次一致 3.07s」不是稳定值。另外 ombre 死端口时 ombre-brain **首拍就是 failed**，不经过 pending，拍到无 pending 的等待时间全是 galatea 的。
8. **§1.4「⚠️ 上一条『单给某个 server 设 false 做不到』只对 2.1.251 成立」**：2.1.310 上也成立——D4（D1 + galatea false）与 D1 逐字节一样、D5（全量 + galatea false）galatea 36,343 里照常驻（第 5 条）。§1.6「D2 两版都测不出」那句是对的，§1.4 那条「只对 2.1.251 成立」的限定可以去掉。新事实：server `alwaysLoad: true` + 工具 `_meta anthropic/alwaysLoad: false` 的 **opt-out 生效**（plan 里只写了 true 钉单工具，没写 false 剔单工具）。
9. **§1.5「resume 后、首轮之前 `get_context_usage` 不可信（totalTokens=1、isLoaded 全 False）」**：2.1.310 上 resume 后首轮前 totalTokens=5,089（不是 1）、`_meta` 钉住的 pinned `isLoaded=True`；只有铸进 tool_reference 的那个（echo）在首轮前仍报 `isLoaded=False`（第 1 / 2 条）。「不可信」对 isLoaded 仍成立，对 totalTokens 不再是 1。
10. **§6「forge 改动：tools 槽的 result 允许 content 列表」**：补两条实测形状——(a) CLI 发请求时会在 tool_reference 列表后**自己追加** `{"type":"text","text":"Tool loaded."}`，transcript 记录里没有、forge 不该铸；(b) 一条 tool_result 多个 tool_reference 原样发出、模型用得上（第 1 条）；(c) 引用了当前没挂的工具时 CLI 改写成 `[Tool references removed - tools no longer available]`，不崩（第 2 条）。真记录的 `toolUseResult.total_deferred_tools` 是实数（114），铸的写 0 没影响。
11. **§8 探针表**：少了本次新加的 `mcp_probe_reconnect.py`、`mcp_probe_ombre_dead.py`、`mcp_probe_toggle.py`，以及 `forge_toolref_probe.py --mode multi|ghost`、`mcp_probe_defer.py` D3/D4/D5。
12. 顺带一条 plan 没写的事实：**failed 的 server 在 `get_context_usage` 里 0 行 0 token**（full / deferred 都是），所以 §6「常驻 server 的不铸；当前配置里没有的不铸」之外，Ombre 掉线时它的工具也不在延迟名单里——铸回它的 tool_reference 会走第 2 条那条改写路。
