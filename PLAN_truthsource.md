# PLAN_truthsource：换真相源——重铸从旧 session 读回，手机历史降级成恢复源

> 2026-10-29 立稿，**未动工**。起因：机主问「如果重铸和判脏都不按手机历史呢？直接从旧 session 重铸？」
> 这件事 PLAN_native §14.4 登记过（「换真相源」「保留量 token 闸」两条，「下一个 plan 级，只登记不动工」），
> PLAN_sdk §2.5 把反面（「绝不读回磁盘」）当承重墙。本稿把两边的账合在一处，给一个不必全有全无的形状。
>
> 标注口径：**【实证】** = 探针 / 日志 / transcript 里看到的；**【读码】** = 读代码推的；**【待验】** = 还没证据。
>
> **开工时机（机主 10-29 拍板）**：先单独立稿，合排（PLAN_toolmount §9）照走；等合排灰度跑出「对不上」频率
> 再决定开不开工。

---

## 0. 一句话

今天的真相源是手机消息库，Mac 上的 transcript 是只写不读的派生物，每次重铸都从手机重派生。
本稿要换的是：**日常重铸读回活着的 transcript 过滤重链，手机历史只在读回失败 / 文件不在 / TA 说重来时当恢复源；
判脏从「每轮比对两份渲染」改成「app 发删改事件」。** 自愈不丢，只是从「每次重铸」挪到「读回失败时」。

## 1. 今天的形状【读码】

| 处 | 现在 |
|---|---|
| 真相源 | 手机 app 本地历史，每轮 `/chat(/stream)` 全量带 `req.messages`（~100 条） |
| 后端镜像 | `state/<角色>/recent_window.json`（≤300 条，每轮覆盖写，给 wake / 泵 / 重启后的重铸用） |
| transcript | `~/.claude/projects/<slug>/<sid>.jsonl`，`forge.render` 只写不读（forge.py:10 自己写明「没有读回接口」） |
| 判脏 | `chat_loop.divergence`：账（本 session 铸过/追加过的 (role, hash, ts)）vs 本次窗口尾部切片；10-29 第 3 步起比对前按 ts 排序、账按气泡记 |
| 重铸入口 | `_open_session(history, catalog)`，history 永远是窗口（请求带的或镜像读的）；四道渲染（闹钟补铸 / 并图 / 打戳 / 活动框）全是在**从别处把 transcript 本来就有的东西捞回来** |
| 触发重铸的路 | ① 判脏；② 进程刚起 client None；③ token 软/硬/PANIC 阈（`_consolidate_and_reforge`）；④ game N 张段内（`_game_reforge`）；⑤ 泵/醒来 client None |
| app 删改 | `/window/sync` 只盖镜像，不生成；下次发送比对出分歧 → 重铸，模型自然忘掉被删的话 |

**一个承重的事实**：五条重铸路里，只有 ① 和 app 删改真的需要手机历史。②③④⑤ 发生时**没有任何东西脏**，
它们重铸是为了换 session / 换系统提示 / 压 token，从手机重派生在这几条路上纯粹是「因为只有这一条路」。

## 2. 换过去能拿掉什么

- **整个判脏这一类问题消失。** 多段回复、tick 交错、同秒竞态、钟偏差、`[图片]` 占位吸收、STALE_TOLERANCE、
  ts 排序，全是在比对同一件事的两份渲染。session 自己是真相，就没有东西可比。第 3 步（账按气泡记）退成恢复路的校验。
- **「丢的少」直接成立。** transcript 里天然有工具调用、图、注入的时间头、被吸收成 system 的插话。
  今天每留一样都要从别处捞一次（图 `chat_images`、闹钟 `alarm_calls`、活动 `activity_log`，下一个工具再捞一次）。
  §14.4 原话：「要真做到丢的少，早晚得从一本完整的账铸」。PLAN_toolmount §6「重铸留用法」在读回下不用另做——
  用法就在 transcript 的 tool_result 里。
- **重启不再需要重铸。** 现在进程一起，每个角色第一条消息必铸一次（「session 没开」那条日志）。
  按角色持久化 sid，起来直接 `resume=`，零成本接上。这一条**单独就能上**，不依赖其余。
- **`_stamp_times` 退役。** §14.5 说得很清楚：活会话里时间一直在，重铸才丢时间。读回就不丢了。
- **插话的形状保住。** interject 的插话在 transcript 里是 `queued_command` → system 壳；今天重铸会把它改写成普通 user 轮，
  读回保留他当时真看到的形状。

## 3. 要付什么

1. **自愈换位置。** PLAN_sdk §2.5：铸错、CLI 自己改写、云同步盖回、依赖投毒，今天下一次重铸从手机重派生就全盖掉；
   读回之后坏账会被继承。本稿的答案是 §4 第 1 条——恢复源保留，坏账在「读回校验不过 / 首次请求失败」时被盖掉，
   不再是「每次」。**§2.5 那条红线要改口**，不是删。
2. **transcript 格式是 CLI 的，没有版本承诺。** `forge.render` 只写它的一个子集，读回要吞 CLI 吐的全部。
   PLAN_sdk §2.4 评 Codex Compass 教程时说得准：噪音回合切割、dropping 状态机、坏行处理，那一半并发症全是读回型才有的。
   换了就一个不少地接过来。09-03 还撞见 transcript 的 mtime 被不明来源顶过一次（PLAN_native §14.4），那份文件不只有我们在动。
3. **app 删改要换一条路。** 现在靠比对发现；读回之后得改成显式事件：app 说「删了 / 改了哪条」（账里有 hash 可认），
   后端过滤重铸。/window/sync 这个口子已经在，形状要变。
4. **异常收尾要截尾。** 轮没正常收尾时 transcript 里可能躺着 TA 没看到的半条回复。今天的规矩「关 session 下次从手机铸」
   等于自动擦掉；读回就得自己截到最后一条投递成功的边界（outbox / 账知道这个边界）。
5. **我们自己的注入要会认。** tick 的「·」、`GAME_TICK_PROMPT`、开局引子、节奏补注、时间头，今天「不进账、重铸自然脱落」。
   读回之后要么按原文认出来滤掉，要么留着吃 token。认的办法只许**整串精确匹配我们自己发过的字**，
   不许写标记解析器——不然又是 [[cassette-mention-is-use-bug-class]]。
6. **保留量要自己按 token 收口。** 今天手机窗口的 100 条天然是闸；读回没有闸，§14.4 第 2 条那个 token 闸从「早晚」变成「必须」。
7. **一致性承诺变软。** 今天的设计性质是「模型记得的 = 手机上有的」（实际已经软了：醒来、泵点评、注入都不在手机上）。
   读回后两边可以长期不一样，比如 app 重装、历史清空，模型还记得。这多半是好事，但要说出来。

## 4. 折中形状（建议）

1. **手机历史降级成恢复源，不是每轮真相。** 只用在三处：session 文件不在 / 读回校验不过；重铸后首次 API 请求失败（ping 纪律）；
   TA 明确说「重来」。这就是 PLAN_sdk §2.4 记的 dankefox 两档：普通 Forge 筛近期上下文，恢复 Forge 从持久库重建。
2. **日常重铸改读回。** §1 的 ②③④⑤ 四条路本来就发生在没脏的时候，正是「丢的少」最要紧的地方。
   读回 → 过滤（截尾 / 滤注入 / token 闸）→ 过 forge 现有的 `_assert_native_block_types` / `_assert_tool_pairing` → 铸新文件 → resume →
   首次请求不成功就退到第 1 条。
3. **判脏改事件。** 每轮比对停掉；/window/sync 改成带 diff 的删改事件，触发过滤重铸。第 3 步那套比对留着当恢复路开 session 后的校验，
   不再是主路。

## 5. 读回器的范围【实证 10-29，cass 当前活 session】

一份真实 transcript（`12a455b2…jsonl`，128 行）里有什么：

| | 数 |
|---|---|
| 顶层 `type` | `user` 63 / `assistant` 63 / `atis-latch` 1 / `mode` 1 |
| 块类型 | `text` 60+60 / `tool_use` 3 / `tool_result` 3 / `image` 4 / **`thinking` 0** |
| 顶层键 | `atis cwd effort entrypoint gitBranch isSidechain message mode parentUuid permissionMode promptId promptSource requestId sessionId timestamp type userType uuid version` |

- **thinking 不在**（这份里一个没有，和 PLAN_native §14.4 探针的结论一致：多数时候没东西可带）。读回器**永远丢 thinking**，
  整块删——签名块只许整删或原样保留，无中间态（PLAN_sdk §2.4 ①）。
- **`atis-latch` / `mode` 这类 CLI 私有条目**：读回器只认 `user` / `assistant` 两种，其余整行跳过；
  坏行（JSON 解不开）记数、跳过，超过阈值整份放弃走恢复源。
- **tool_use / tool_result 配对**：沿用 `forge._assert_tool_pairing`；截尾时若切在一对中间，整对丢。
- **image 块**：原样保留，受 token 闸（图按 `GAME_TOKENS_PER_SHOT` 计）；游戏段内重铸的「近 K 张」规则变成
  「保留最后 K 个 image 块、更早的替换成感知框行」——和今天 `_game_shot_tail` 的语义一致，只是来源换了。
- **注入识别**：我们发出去的每一轮 `client.query(...)` 字串，发之前记在一本「注入账」里（整串 + ts）；读回时 user 块文本
  **整串相等**才算注入。时间头 `【MM-dd 周X HH:mm】\n` 形状的前缀允许剥掉再比。不做任何别的模式匹配。
- **截尾边界**：从文件尾往前找最后一个「账里有的 assistant 气泡」，之后的全丢。
- **跨版本**：读回器钉当前 CLI（2.1.310）的形状，`forge_regress` 加一条「读一份真 transcript → 过滤 → 铸 → resume → 问原文」的腿，
  升 CLI 必跑（并进 PLAN_toolmount §8 清单）。

## 6. 与其他 plan 的关系

- **PLAN_sdk §2.5 红线改口**：「forge 绝不读回磁盘」→「**恢复路**绝不读回磁盘；日常路读回但必过校验 + ping，失败即退恢复路」。
- **PLAN_native §14.4** 两条「还没做的更大的」归本稿；§14.5 `_stamp_times` 在读回下退役。
- **PLAN_toolmount §6 重铸留用法**：读回下自然成立，§9 第 9 步可以省掉大半（只剩「恢复路」那一半要留用法）。
- **PLAN_interject P1 账按气泡记**：退成恢复路校验；§4.3 的「同秒竞态」不用修。
- **PLAN_gamechat §2.2 ②**：不用做。
- **PLAN_proposals / code_permits**：不受影响（行为账是另一本，和真相源无关）。

## 7. 施工顺序（粗，开工时再细）

a. **读回器 + 校验 + 探针**（离线，只动 forge 和 tools/forge_regress）：读真 transcript → 过滤 → 铸 → resume → 问原文。
   不碰 chat_loop。全绿才往下。
b. **sid 持久化 + 重启直接 resume**：收益最大、改动最小的一步，可以单独上电。账也一起持久化（`_persist_ledger` 已在）。
c. **日常重铸改读回**（§1 ②③④⑤）+ **token 闸**同批。恢复路保留原样。
d. **判脏改事件**：app 侧 /window/sync 带 diff；后端过滤重铸。要发 app 包。
e. **删每轮比对**，第 3 步那套留作恢复路校验。

## 8. 待拍

- 开工时机：合排灰度后看「对不上」频率（已拍：先不动）。
- 删改事件的形状：按 hash 认还是 app 给消息 id（app 现在的 `ChatMessage` 有没有稳定 id，要查）。
- 注入留不留：滤掉省 token，留着「他当时看到的」更完整。倾向滤 tick 和引子、留时间头。
- token 闸数值：沿用 `CHAT_SOFT/HARD/PANIC_TOKENS` 还是另立「读回保留量」。
- CLI 升版策略：读回器钉版，升版必跑 forge_regress 新腿；能不能接受「升版当天读回失败全走恢复路」。

## 9. 不做

- 不读回 thinking。
- 不做 transcript 编辑器（改字 / 插条）——真要改历史走恢复路从手机铸。
- 不跨角色读（slug 撞目录 = 串台的文件系统版，PLAN_sdk §2.5 ④；读回前核 `sessionId` 和 cwd）。
