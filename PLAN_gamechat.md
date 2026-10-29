# Plan：游戏态的聊天框改走 /chat/stream

> 2026-10-29 机主与 Claude Code 会话立稿。**10-29 深夜第 1、2 步已落码（未重启、app 未装真机），施工记录在 §5 末。**起点是对 PLAN_toolmount / PLAN_interject
> 交界的复查：游戏态里机主的消息走的是 tmux 时代留下的「终端口」`/code/send`，
> 而 PR13 之后游戏早就骑在聊天 session 上了——这条岔路只剩历史原因。
> 依赖：PLAN_interject P1（账按气泡记）、PLAN_toolmount §4.1（泵开着时的重开统一收口）。
>
> 标注口径同 PLAN_interject：**【实证】/【读码】/【待验】**。

---

## 0. 一句话

**游戏泵（unified）开着时，聊天框照常走 `/chat/stream`：回复流式上屏、图片直接进消息、
插话（PLAN_interject）在游戏轮里也能用；`/code/send` + `PumpNote` 留作旧包兼容，跑稳后退役。**

## 1. 现在怎么走（【读码】）

- **app**：`sessionMode = codeMine || gameMine`（`ContentView.swift:127`）。游戏会话活着且归当前角色
  （`gameSessionActive`，来自 `/code/status` 的 `profile=game`，或聊天回复里的 `game_started`）
  → 聊天框发送走 `chatService.codeSend` → `POST /code/send`（`ContentView.swift:977`）。
- **后端** `/code/send`（`app.py:1780` 前后）：
  - 图片 / 文件落盘，正文后面附「〔随消息发来 N 张图，用 Read 看：路径〕」；
  - 加时间头「〔现在是 …〕」；
  - 泵（`is_pump`）→ `chat_loop.inject_pump_user` → `PumpNote` 进队列，**当前短轮结束后**
    当一轮吃掉，回复走 outbox，账记 `ledger_text`（app 历史里的原文）；
  - 独立 game loop（`STORY_ENGINE=sdk` 回退）→ `put_threadsafe`；tmux 回退 → `code_bridge.send`；
  - 最后 `_code_window_append("user", …)` 记窗口。
- 名字由来：tmux 版 Code 模式时这条口是往终端里 `send-keys`，注释里叫「终端口」。

## 2. 改走 /chat/stream 后后端会怎样

### 2.1 已经现成的（【读码】）

- 泵与聊天同一条 session、同一个队列；chat `Turn` 进来先被处理，泵只在队列空时补「·」
  （`chat_loop.py` 主循环 `wait_for(..., timeout=GAME_TICK_PAUSE)`）——行为同 `PumpNote`，只是回复流式。
- 判脏容忍 outbox 里没拉走的泵点评：`_stale_depth` 按该角色未拉走条数上浮（泵专用）。
- app 拉 outbox 按 ts 插位（`ChatStore.insertIntoCurrent`）——**但顺序不保证与账一致**，见 §2.2 第 3 条。
- `_wake_gate`：泵开着时 chat 轮放行 `mcp__game__*`——机主说「点左边那个」他这一轮就能点。
- chat 硬阈强铸在泵开着时本就跳过（`not handle.meta.get("game_pump")`）。
- 图片走消息里的 image 块（不再落盘让他 Read），时间感走聊天注入，比终端口好。

### 2.2 会坏的（必须先修）

1. **多段回复判脏**（PLAN_interject §3）：游戏里他回你几乎都是「说一句、点一下、看图、再说」，
   app 多个气泡、账一条拼接回复 → 下一条消息判脏、整体重铸。终端口那条路回复走 outbox 整轮投递，
   碰不到；改走流式就会天天碰。→ **前置：PLAN_interject P1（账按气泡记）。**
2. **chat 路重铸不带游戏的东西**：判脏走 `_open_session(turn.history)`，没有近 K 张图回填
   （`render_tail` 只在 `_game_reforge` 里传）、没有节奏补注、没有取过的用法。现在只在偶尔判脏时
   缺，改路由后会常态化。→ **前置：PLAN_toolmount §4.1（泵开着时的重开统一收进 `_open_session`）。**
3. **tick 点评与用户消息的 ts 交错**（10-29 晚复审补，【读码】）：`ChatStore.swift:324` 按 timestamp 插入；
   用户气泡 ts 是手机发送时刻 t0，消息排队期间一个 tick 点评以 t1 > t0 投递，app 里是 [U, tick]，账里是
   [tick, U]（PumpNote 路 `chat_loop.py:1689` 用户条在轮头追加、点评在投递时追加；chat 路 `:1933-1937`
   轮尾追加），下一条必判脏。**今天 PumpNote 路就有**，改走 chat 路一样有，形状混在 PLAN_interject §3 那批
   「第 0 条对不上」里。修法二选一：① 账里用户条的 ts 用 app 给的 ts，比对前两边都按 ts 稳定排序
   （`divergence` 比的是序列，排序要同口径）；② 泵收到 chat Turn 时把 outbox 里**未拉走**的点评 ts 抬到
   max(ts, 用户 ts + 1)。本稿默认 ①，和 P1 同批做。→ ~~前置：PLAN_interject P1 核对时把这类分开数。~~ **10-29 第 3 步已按 ① 落码**：
   `chat_loop.ts_sorted` 比对前两边稳定排序（同 ts 保持原序、没 ts 沿用前一条，只排副本账不动）；PumpNote 带 `ts`
   （/code/send 收到的时刻）入账；chat 路回复气泡的 ts 不早于 TA 那条。残余：点评投递与 TA 消息落在**同一秒**时两边
   原序仍可能相反（账 [tick, U]、app [U, tick]），会多铸一次，灰度时看频率再定要不要上 ②。

## 3. 改动

### 3.1 后端

- `/code/status` 加字段 `engine`：`"pump"`（unified 泵，骑在聊天 session 上）/ `"loop"`（独立 SDK loop）/
  `"tmux"`。只有 `pump` 时 `/chat/stream` 才够得着游戏——独立 loop 和 tmux 回退路仍然只能走终端口。
- `/chat/stream` 不用改：泵开着时 chat 轮本来就进同一条 session。
- `/code/send` 的泵分支（`inject_pump_user` / `PumpNote`）**保留**，给旧包用；加一行日志
  「旧包仍在走终端口」，用来判断什么时候能退。
- 兼容不需要能力头：旧后端没有 `engine` 字段 → 新包读不到就照旧走终端口；旧包照走 `/code/send`，
  后端那条一直留着（§3.3 退役前）。两个方向都自然退化。

### 3.2 app

- 路由拆开：`sessionMode` 只看 `codeMine`；游戏态（`gameMine && engine == "pump"`）发送走正常的
  `/chat/stream` 那套（流式、`pumpOutbox` 串行、快照、rescue 全复用）。
  `engine != "pump"`（回退路）照旧走 `codeSend`。
  - `engine` 只从 `/code/status` 轮询来（sessionMode 下 3s 一次，`ContentView.swift:218`），而
    `gameSessionActive` 能由聊天回复的 `game_started` 立刻置真（`:1321`）、那时 engine 还是空——
    **game_started 到手时先拉一次 `/code/status` 再路由**，别让头一两条消息按「空 → 走 codeSend」。
    新后端有 PumpNote 兜着不出事，§3.3 退役之后那条就是 409。
- ⏸ 的显示条件不动（`gamePauseVisible`，`ContentView.swift:116`，仍看 `gameMine`），只是不再决定消息
  走哪条路。（终端面板已退役，`:918`、`:1320` 两处注释是旧话，顺手改。）
- chat 路的注入（`build_injection`，`chat_loop.py:698-706`）会给每条游戏中消息带待办块、PR 事件行、
  「距离上一条过了多久」；`_pump_round` 现在都不带。本稿默认**照带**（那是知觉材料，不是档案），玩一局
  看会不会把他的注意力从画面上拉走，再定要不要泵开着时只留时间头。【待拍 4】
- 编辑 / 重新生成：终端口时代游戏态整个禁用（「那些轮次有真实副作用」）。走 chat 路后照 chat 的规矩，
  但**泵开着时**仍禁用（重放 = 再点一遍游戏）。【待拍 1】
- 发送中被游戏点评插队：outbox 点评按 ts 插位，正在流式的气泡不受影响【待验，模拟器】。

### 3.3 退役（新包跑稳两周后）

- 前提：新包已经做了「game_started 先拉 status」（§3.2），不然那几秒的 codeSend 没人接。
- 删 `inject_pump_user`、`PumpNote`（及主循环、`_finish_orphans` 里对它的分支）、`/code/send` 的泵分支、
  游戏那条 `_code_window_append`。
- `/code/send` 本身留给 Code 模式 tmux 回退路，那条路的退役归 PLAN_native。

## 4. 测试

- 单测：泵开着时一个带工具的 chat 轮（罐头流：文字 → game_tap → 文字），P1 之后下一条不判脏。
- 单测：泵开着时判脏重铸 → `_open_session` 产出的铸造输入带近 K 张图、`rhythm_due` 置位（PLAN_toolmount §4.1）。
- 真机：一局里连发三条（含一张图）；锁屏中途回来 rescue；回退路（`STORY_ENGINE=sdk`）仍走终端口。
- 模拟器只做编译验证，**别连真后端**（10-13 模拟器吞消息事故，PLAN_interject §5 P3 同条）。

## 5. 施工顺序

整体顺序见 PLAN_toolmount §9（三份计划合并排的）。本计划自己的步骤：

1. ~~后端 `/code/status` 加 `engine`~~ **已落码（10-29）**。
2. ~~前置两项到位后（PLAN_interject P1、PLAN_toolmount §4.1），app 改路由。~~ **已落码（10-29，模拟器编译过、未装真机）**。
3. 旧包兼容期观察终端口日志；两周后做 §3.3 退役。

**10-29 施工记录**：
- 后端：`code_status` 返回 `engine`（`sdk_loop_handle()` 有且 `is_pump` → `pump`；有但不是泵 → `loop`；没有 → `tmux`），
  老字段一个没动；`/code/send` 泵分支加一行 `[code_send] 旧包仍在走终端口（char=…）`，§3.3 退役看它。
  `tests/test_code_status.py` 两条（三种 engine + 老字段还在）。
- app：`CodeStatus.engine: String?`；`ContentView` 新增 `gameEngine`（`syncCodeMode` 里从 status 取）和派生值
  `terminalRoute = codeMine || (gameMine && gameEngine != "pump")`，**只有 `send()` 的改道判断换成它**；
  `sessionMode` 保留原义（会话活着且是我的）给顶栏 / 3s 轮询 / 编辑禁用——待拍 1 按默认「泵开着时仍禁用编辑」落的。
  `game_started` 到手时 `Task { await syncCodeMode() }` 先拉一次 status 再路由。两处「终端面板」旧注释顺手改。
- §4 第一条单测不用另写：`test_multi_segment_reply_ledgers_per_bubble` 在 `GamePumpLoopTest` 里被继承、泵开着时也跑；
  第二条是 PLAN_toolmount 第 4 步的 `test_dirty_reforge_in_game_carries_same_as_segment_reforge`。
- 待拍 3（游戏中消息不另加「〔现在是 …〕」）、待拍 4（chat 注入照带）都按本稿默认落的，玩一局再看。

## 6. 待拍

1. 游戏态的编辑 / 重新生成：泵开着时禁用（本稿默认），还是只禁用带游戏工具调用的那几轮？
2. 回退路（`STORY_ENGINE=sdk` 独立 loop / tmux）要不要一起退役，让游戏只有 unified 一条路？
   退了的话 §3.1 的 `engine` 字段和 app 的分支都能省掉。
3. 游戏中发的消息要不要像终端口那样额外加「〔现在是 …〕」——chat 注入已有时间头，本稿默认不加。
4. chat 注入的待办块 / PR 事件行 / 间隔行，泵开着时照带还是只留时间头（§3.2）——本稿默认照带，玩一局再看。
