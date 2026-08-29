# Plan：小屋总开关

> 2026-08-27 方案定稿，**同日 H0+H1 完工未 push**（施工记录见末节）。剩 H2 真机验证。
> 开＝现状；关＝小屋活动全停、状态冻结，cass/小卡退回老 wake 路
>（醒来只能发手机消息、插件工具照常），团团四值冻结且不醒。

## 关键发现（2026-08-27 调查）

- **开关 90% 已存在**：`config.COHABIT_ENABLED` 的 17 个判点覆盖了醒来队列、猫 worker、
  聊天注入、导演口全部。且 `wake.py:679` 的 `if COHABIT_ENABLED: return` 关的正是
  老 wake 路——老路 ACTION 协议只有 none/message、工具菜单照常走
  `pipeline.tool_menu_block`，就是「只能发消息、插件照常」本体，一行判断改掉自动接管。
  主体工程＝把 env 常量升级成热切开关：`house_settings.json` 存 `house_enabled`，
  `world.house_enabled()` 每轮现读（抄 `experience_limit` 成例，改完下一轮生效不用重启）。
- **全案唯一硬坑是猫**：四值不是循环推进的，是 `pet_store._apply_decay()` 读时按
  `(now - updated_at)` 惰性现算——停 worker 冻不住。关三天重开，第一次 `read_state()`
  一次性补扣 72h：饱腹归零、憋屎爆表，下个 tick `enforce()` 强制吃＋强制拉。

## 已拍板决策（2026-08-27 机主，重启讨论不用重开）

- **猫冻结＝关/开两个时刻结账**：关的瞬间 `read_state()` 把衰减结算到关的时刻，
  开的瞬间 `updated_at` 置 now——等效「这段时间不存在」，不在衰减公式里维护冻结区间表。
  睡着的精力恢复同冻（不会因为关开关而「睡醒」）。`day` 照跳（真实年龄，不为观感加账）。
- **NEXT 清掉**：关的瞬间清 `next_wake_at`——关着期间写的语境已失效，重开兑现容易说胡话。
- **pending/offer 全清**：关瞬间醒来队列丢弃；未结算的抱人邀约直接作废，
  **不走「超时默认答应」**（否则关小屋的副作用是把人抱走了）。
- **关着时角色基本静默——不联动、不加新键**：`random_wake` 两角色现状皆 false
  （两条 wake 路共用这一个键：`cohabit_queue.py:341` / `wake.py:717`），
  关着＝只剩邮件硬触发醒（邮件醒走老路、可写 NEXT 形成小链条，属手机活动合规）。
  哪天想要关着期间他们随机想起机主 → 自己去角色设置页手动开 `random_wake`
  （小屋再开前记得关回去，否则屋里随机醒一起复活）。
- **房间只读**：关着时用户侧 POST 全 409——「冻结就该是玻璃罩」。
- **开关落痕、时间感不冻**：关/开各落一条系统事件（「小屋休眠了/醒了」）；
  `fmt_gap` 的时间感照常注入——知道过了三天是真实感，休眠事件就是
  「为什么这三天没记忆」的口径。
- **猫照料**：查看只读放行；喂/互动/铲 409「团团在冬眠」。pet MCP 四工具挂载
  本来就判 COHABIT_ENABLED，顺势卸载即正确。
- **两层开关**：env `COHABIT_ENABLED` 保留＝功能装没装（关＝worker 不起、路径不存在）；
  json 开关＝运行时开/停。**worker 改无条件起、tick/enqueue 层判开关**——
  否则默认关着时重启，worker 没起，再打开要重启才生效。
- **pause 键留着并存**：pause＝只停执行、事件照攒、恢复追赶（在屋里想安静看看）；
  总开关＝全冻、不追赶（离家一周）。UI 上说清两者区别。
- **不受管清单（关小屋≠关求职≠关邮箱）**：邮箱轮询＋jobhunt 三分类、浏览器幽灵、
  code/game 看门狗、插件 MCP 轮内调用，全部照常。
- **UI**：开关放铃铛（NudgeSheet，事实上的小屋设置层）＋二次确认
  （提示「团团会进入冬眠」）；HousePage 关着时显示冻结的最后一帧＋休眠角标
  （能看不能动，冻结快照有观赏价值）。

## 拦截点地图（施工按这个走，行号以 2026-08-27 为准）

| 拦截点 | 位置 | 做法 |
|---|---|---|
| 一切入队（单点兜底） | `cohabit_queue.enqueue` :121 | 判 `house_enabled()`，事件/自主/move/nudge 全被盖 |
| 事件醒 / 自主 tick / 聊天 move | `_on_room_event` :220、`_solo_tick` :365、`chat_move` :263、`chat_move_hint` :281 | 各自现有 COHABIT_ENABLED 判点原地换 |
| 导演口 | `app.py` /world/nudge :983 | 409「小屋关着」 |
| 邀约扫除 | worker 循环开头 `offers.sweep`（cohabit_queue.py:460） | 关着整段跳过 |
| 猫 tick／猫事件响应 | `pet_queue._tick` :178、`on_room_event` :124 | `paused()` 扩成 `paused() or not house_on()` |
| 猫衰减 | `pet_store` | 公式不动，冻结走关/开结账（见拍板） |
| 老路接管 | `wake.maybe_wake` :679 | `if house_on(): return` —— 一行 |
| 聊天注入 | `cohabit.house_context_for_chat` :213、`pipeline` :407/:415/:493 | 原判点换热读 |
| REST | 新增 `GET/POST /world/enabled`；`GET /world` 回 `enabled` | 存 `house_settings.json` |
| iOS | `NudgeSheet` 加 Section（`saveCarryAccept` 同款写法）、HousePage 角标、房间只读态 | 二次确认弹窗 |

## Milestones

- **H0 服务端开关** ✅（2026-08-27）：`house_enabled()` 热读＋全部拦截点换判＋
  worker 无条件起（tick 层 `continue`）＋REST 端点＋关/开钩子
  （`cohabit_queue.switch_house`）。
- **H1 iOS** ✅（2026-08-27）：铃铛 Section＋关向二次确认（「团团会进入冬眠」）＋
  HousePage 休眠横幅＋RoomPage 只读（输入/加改状态/摸猫/暂停键全收）＋
  猫照料 409 文案。模拟器编译过。
- **H2 真机验证**（待做）：关→重启后端→开（验 worker 无条件起）；
  关 N 小时→开验猫四值不掉；关着收一封求职邮件验老路只发消息。

## ⚡ 施工记录（2026-08-27，H0+H1 一次做完）

- **落位**：`world.py` 四件套——`house_enabled()/set_house_enabled()`（json 落盘）、
  `house_active()`（env 且 json，活动判点统一入口）、`house_frozen()`（env **且非** json
  ——「没装」≠「冻结」，env 没开的部署不存在玻璃罩，别停人家的猫钟）。
  拨闸钩子在 `cohabit_queue.switch_house(on)`：幂等，关/开顺序都有讲究（见下）。
- **施工中发现的新坑（plan 里没预料到的）**：猫的衰减是**读时**惰性算——只堵写口
  冻不住，冻结期间 GET /pets（查看放行）一样在掉。冻结必须做进钟本身：
  `pet_store._clock_now()`——house_frozen 时钟停在 baseline（updated_at）时刻，
  read_state 和 mutate 共用；漏进来的改动也记在关门那一刻，不产生时间流逝。
- **拨闸顺序（都在 switch_house docstring 里，动它先读那段）**：
  关＝settle（**必须在拨旗前**：旗一下钟就停，settle 变空操作，最后一段流逝丢账）→
  拨旗（enqueue 全拒，清理不被并发事件回填）→ 清两个队列 → 撤 NEXT →
  offers.clear（静默作废：不走超时默认答应、不发补醒）→ 落「睡下了」事件（没人醒）。
  开＝resume_clock（**也在拨旗前**：晚一步 pet worker 抢读就是整段补扣）→ 拨旗 →
  连发计数清零 → 落「醒了」事件（事件醒来顺势叫醒在场者，重开的第一口气）。
- **落痕**：`_announce_house` 只落**有角色在场**的房间（空屋没人记得）；
  actor=user（拨开关的就是机主，如实）；kind=house_switch。
- **玻璃罩（app.py `_require_house_awake`）**：act / state / move / carry_offer /
  nudge / pet interact / pet scoop 七个口 409；**放行**：删事件（记录管理不是小屋
  活动）、/world/pause、铃铛三旋钮、一切 GET。/world 和 /rooms/{id} 带 `enabled`
  字段给 UI。
- **不受管确认**：邮箱 watcher/jobhunt、浏览器幽灵、code/game 看门狗、/characters
  与 /world 的 code 状态显示（那是手机侧能力）——全部没动。
- **测试**：`tests/test_house_switch.py` 新增 8 例（语义拆分/清场静默/入口全拒/
  幂等/开门唤醒/猫钟停摆/睡猫不补醒/冻结中 mutate 落在关门时刻），全量 200 全绿。
  基座沿用 QueueBase＋register_pet（注意基座字段叫 `self.cid/self.home`，
  offers._enqueue 已打桩）。
- **iOS**：`HouseModels`（WorldSnapshot/RoomDetail 加 `enabled`、worldEnabled 读写）；
  `NudgeSheet` 总开关 Section 排最上（开向直接拨、关向 confirmSleep 弹窗）；
  RoomPage `frozen` 计算属性统一收口。SourceKit 单文件诊断全是跨文件噪音，
  以 xcodebuild 为准（BUILD SUCCEEDED）。

## 复盘（2026-08-29）：总闸真拨关后，pet/world 测试片区齐挂

H0 埋的雷，08-29 跑全量套时炸出来（10 fail + 4 error）。修法一行（WorldBase 补
patch `HOUSE_SETTINGS_PATH`），复盘按坑写：

- **根因＝四个条件同时成立**：① `house_enabled` 是热读开关（判点每轮现读文件，
  这是设计要求：铃铛拨完即生效）；② world 的路径隔离有两个测试基座各自维护
  （CohabitBase / WorldBase），H0 给 world 加第四个路径常量时只补了 CohabitBase
  ——WorldBase 一系（test_pet / test_pet_queue / test_world）继续读**真**
  `state/house_settings.json`；③ 完工验收那天真闸还开着，真文件恰好是无害值，
  「200 全绿」为真但是**假阴性**——绿灯只证明当天生产状态无害，不证明隔离；
  ④ 后来机主把真闸拨关（正常运维），测试才开始挂——**引爆时刻由运维动作决定，
  与引入缺陷的提交解耦**，git blame 指到无辜提交头上。
- **归类（不带项目名词）**：测试读到生产的可变状态、而那个状态恰好处于无害值＝
  定时炸弹：什么时候挂由生产运维决定，不由代码决定。且「N 个测试基座各自手抄
  同一模块的路径补丁清单」＝每加一个路径常量要改 N 处，漏一处不报错。
  （同族前科：test_world 注释里「不改这个真 state 会被测试事件污染」——写方向
  翻过车，d440a45 时防的；这次是读方向，族=测试与生产共享路径的双向泄漏。）
- **规避规则**：① 给模块加新的落盘路径常量时，`grep "模块名\." tests/` 找出
  **所有** patch 过该模块路径的基座，同批补齐（这次就是只补了一个）；② 热切
  开关类状态在测试里必须显式钉到已知值，「真文件恰好是默认值」不算钉；③ 更好
  的高度是 jobhunt_store 的 `_rebase()` 范式——模块提供整体换根入口，基座只调
  一个函数，新增文件自动被覆盖；④ 怀疑有此类泄漏时的检法：把生产状态文件拨到
  极端值再跑一遍全量套（这次等于机主替我们做了这个 chaos 实验）。
- **捉虫**：横扫全仓——patch world 路径的基座就 CohabitBase/WorldBase 两个，
  现在 4 常量齐；jobhunt_store 走 `_rebase` 天然免疫；plugins 的状态文件
  （OWNERS_PATH/enabled json）**测试目前零触碰**，无现行泄漏，但谁先给 plugins
  写测试谁先补 patch（潜在同类，读代码推的没复现）。**评估后不修**：五个基座
  统一迁 `_rebase` 范式——值得做但是独立重构，不混进本次一行修。
