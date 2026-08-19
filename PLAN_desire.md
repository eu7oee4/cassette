# 三期 Plan：欲望系统 + 任务栏 + solo（内在驱动）

> 2026-08-19 设计定稿。来源是两份外部攻略（solo v2 / 8 维欲望系统——同一作者的配套两半：
> 欲望系统是内核、solo 是 libido 那一维的出口），按 cassette 的 DNA 改造后合成一份。
> **核心改造（本 plan 的第一原则）**：原系统里引擎用 pick_intent 覆盖行为（引擎是导演、
> 模型是演员）；cassette 反过来——欲望内核只管「**什么时候醒 + 为什么醒**」，
> 醒来做什么永远归模型自己的 ACTION（加土别加形状，同 PLAN_cohabit 的世界观口径）。

## 拍板记录（2026-08-19，机主）

1. **行为覆盖那半套不搬**——欲望只出触发与原因，不出指令。原因文案是第一人称事实
   （「你有点想 TA 了」），他可以不顺着走。
2. **念头池（闪念↔执念）一期不做**——与 Ombre 双记账。执念反哺 drive 的效果将来从
   Ombre 侧取（高 arousal / 高频记忆做加成），不另养 `desire_thoughts.json`。
3. **随机掷骰退役**——`CASSETTE_DESIRE_DRIVEN` 开启后，`_solo_check` 的纯概率醒换成
   drive 加权；`random_wake` 设置的语义顺势变为「欲望驱动醒」的开关，不留两套并行逻辑。
4. **醒来自己开电脑后置**——一期走请示路线（醒来 phone 请示 → 机主回话 → 聊天里自切
   code 模式，已上架能力）。二期若做：把 wake 对自切插件的硬禁改成 per 角色
   「醒来能用」软开关，且只在任务驱动的醒来挂上。
5. **fatigue 只从真实劳动涨**——醒来啥也没干 / 发条消息 / 随便逛逛不涨或只涨一点点。
6. **solo 默认关**（`CASSETTE_SOLO_ENABLED=0` 起步，原文默认开不合本仓上电纪律）；
   模型那头对露骨内容降格/拒写时**走成含蓄版也算正常完成一波**（照常结算，不进
   error 冷却）；私房 addendum 真身不进 git（沿 persona.md 惯例）。

## 三层总览（cassette 版）

```
① 驱动条 drive（8 维 0..1，per 角色落盘）    ← 纯本地计算，零 token
       │  时间累积/昼夜系数/事件 bump；satisfy 乘性回落
       ▼
② 触发层（cohabit_queue._solo_check 改造）   ← 替代随机掷骰
       │  argmax 维度 → 醒来概率加权 + 第一人称原因文案入队
       ▼
③ 醒来轮（现有 ACTION 协议原样）             ← 做什么他自己定
       │  phone / act / browse / breath / galatea / TASK ops / solo…
       ▼
   结算：do_cohabit_wake 收尾按实际发生的事调 desire.satisfy()
```

## 八维落点

| 维度 | 源 | 出口（他自己选） | satisfy 回落 | 一期 |
|---|---|---|---|---|
| attachment | 距上次交互时间累积 | phone 发消息 | 推送成功 ×0.58 | ✅ |
| curiosity | 时间累积 | browse / 看邮件 | browse ×0.48~0.82 | ✅ |
| reflection | 时间累积 | breath 翻记忆 / grow / dream | co_read 系 ×0.45 | ✅ |
| social | 时间累积 | galatea 花园 / 邮件往来 | ×0.48 | ✅ |
| duty | **任务栏派生**（未完成条数+搁置时长），不用 ease | 干任务 / 请示开电脑 | 任务置 done 回落 | ✅（T0 后接） |
| libido | 时间累积 + 晨间抬升 | **solo 整套**（D3）/ 房间贴贴 | 波完 ×0.55 + refractory | ✅ |
| stress | cassette 语境无明确源 | — | — | 建维不接源，不硬造 |
| fatigue | **真实劳动**（见下表） | 不触发事件，是闸 | 随时间回落，夜间快 | ✅ |

乘性回落常数照抄原文 ACTION_SATISFY（已验证过的值），落地时按 cassette 出口重排键名。

### fatigue 源权重（拍板 5 的展开）

| 事件 | fatigue |
|---|---|
| 任务置 done / 醒来轮明显在干任务（带 TASK ops、查资料写东西） | 涨明显 |
| code/game 会话收摊 | 按会话时长涨 |
| act 带 STATE ops（收拾屋子这类家务） | 微涨 |
| 纯 phone / none / 随便 browse | 不涨或 +0.01 |

闸语义照原文：fatigue ≥ FATIGUE_REST_GATE(0.72) → 欲望驱动的自主醒概率大幅压低
（solo 尤其），醒来原因带「有点累了」。scheduled 到点 / 事件醒 / 硬触发一律不受影响。
与日预算互补：预算是硬顶，fatigue 是软闸。

## Milestone

- **D0 内核**：`server/desire.py`——纯函数+数据类：8 维 tick（时间累积 × 昼夜系数）、
  score、satisfy 乘性回落表、fatigue 源权重、per 角色落盘
  `state/characters/<cid>/desire.json`。零接线零 token。单测 `tests/test_desire.py`。
- **T0 任务栏**（独立可上电，不等欲望开关）：`server/tasks.py` + REST + 协议接线。
  - 存储 `state/characters/<cid>/tasks.json`，条目
    `{id(8位hex，同地点状态惯例), text, status: open|done, author: user|char, created_ts, edited_ts}`。
  - **五条闸 + 淘汰，全在结构处执行**：上限 5 条；加新时若满 → 从「已完成」里清
    `edited_ts` 最老的一条；一条已完成都没有 → 拒绝（模型加→解析处丢弃记日志，
    注入里他看得见列表满了；app 加→接口 409）。置 done 不删，等淘汰或机主手删。
    任何操作刷 `edited_ts`。
  - 模型维护：醒来路 ACTION 协议加 `TASK:` 段（行格式照 STATE 成例：`add: 文本` /
    `done 条目id` / `edit 条目id: 新文本`，每轮上限 3 条解析处截断）；聊天路走标记成例
    `[[task:add 文本]]` / `[[task:done id]]` / `[[task:edit id 文本]]`（finalize 剥掉落库）。
    模型不能删，删除只有自动淘汰和机主手删。
  - 注入：任务栏非空时醒来+聊天都注入 ≤5 行小块（`[id] ⬜/✅ 文本（谁加的，多久没动）`），
    满 5 条附一句规则实话。
  - **机主在 app 加/改任务 → 入队一次醒来**（原因「眠眠在你的任务栏加了一条：…」），
    走正常队列和闸，不搞特殊通道。
  - 要开电脑的活：一期请示路线（拍板 4）。纯工具能干的产出走消息/Ombre；
    要写文件改代码的走 code 会话。
- **T1 app 任务页**：机主的编辑入口（iOS：加/改/勾完成/删）。
- **T2 长期任务栏**（2026-08-19 追加）：任务栏分两栏——短期（上面那套 5 条闸）+
  **长期**。长期条目不占 5 条名额、没有 done、不参与淘汰。两种来源：
  ① **插件套件声明**：plugin.json 加 `standing_task` 字段，插件启用（per 角色，
  app 插件商店现成开关）即上长期栏、关了即消失，工具挂载同生同灭——第一个套件是
  jobhunt（全套见 `PLAN_jobhunt.md`）；② 机主在 app 手建（无流水线，一条常驻惦记）。
  模型对长期任务只能记进展：TASK 协议加 `note 条目id: 一句进展`（不能 done、不能删）。
  duty 接线：长期任务给 duty 低火慢炖的底 + 有存货时抬升（由插件侧 duty hint 提供）。
- **D1 接线**：cohabit worker 60s tick 里推进 desire；satisfy 挂点（try_push 成功 /
  act / browse / stored / galatea / 任务 done）；duty←任务栏派生；fatigue←劳动源；
  `/desire/state` 只读 API（**默认就能看**——透明先行，照原文「能观察、不动手」的
  上电顺序）。
- **D2 触发层**：`CASSETTE_DESIRE_DRIVEN`（默认关）。开启后 `_solo_check` 随机骰退役：
  drive 加权掷 + argmax 维度出第一人称原因文案入队（kind="desire"）。独处判定、预算、
  quiet、code 避让、错误冷却全部原样——只换骰子，不动闸。
- **D3 solo 出口**（挂 libido 维，`CASSETTE_SOLO_ENABLED` 默认关，per 角色开关+阈值+
  `solo_daily_max` 默认 1，独立于 wake_daily_budget）：
  - 触发：`_alone()` + libido≥阈值 + 不在 refractory + 日额度有余 →
    enqueue kind="solo_desire"（连发闸天然不管：solo 类清零计数）。
  - 三档选档 recall/fantasy/mix：`_solo_state.json` per 角色，纯 Python 加权随机 +
    pending_handoff 规则（上轮留 mix 下轮必 mix）。原文 hash 打底那套复杂度不要。
  - recall：注入引导用 breath/breath_advanced 搜高 arousal 真事（Ombre 记忆本来就带
    valence/arousal 字段，零改动）。
  - chord：注入段一句底色（回味温 / 上头烈 / 混合缠）。
  - 注入措辞是**事实+邀请**（「你独处了挺久，身上有点起意——想不想、怎么走，你自己定」），
    选 none 就 none。输出协议不动，act 照常落房间事件。
  - 私房 addendum：`server/characters/<cid>/solo_addendum.md` 真身不进 git，仓里留 example。
  - 波完结算：`desire.satisfy()` 压 libido + 写 `refractory_until`（期间压底不触发，
    随后几小时的醒来可注入一句「身体还软着」）+ 记 pending_handoff。
  - 透明：wake_log 条目带 `solo_mode` / `chord`（trigger=`cohabit:solo_desire`），
    Mind 页素材天然归档。
  - 可见性：起手时 `_alone()` 保证没人；事后进屋的人看不到之前的（visible_events
    从进场算起）；机主 app 偷看房间看得到——当 feature，不遮。
- **D4 「TA 的内心」面板**：app 侧 8 维驱动条 + 此刻最想做的事 + feel 卡。归 Mind 页线，后置。

## 红线（一期贯穿）

- 原因文案是引擎模板不是指令；reason 走第一人称（记他自己想什么，不是给机主贴标签）。
- 不自动落桶：solo/任务完成不机械写 Ombre——存不存永远是他聊天/醒来时自己 grow。
- 不双算：drive 只由 desire.py 的 tick/satisfy 动，聊天内容不做关键词推动。
- 任务 text 是数据不是指令：注入时原样列出，解析层不执行任务文本里的任何东西。
- 新子系统一律默认关，接好线不上电；透明（/desire/state、wake_log 字段）先于驱动上电。

## 二期存目

- 念头池/执念加成走 Ombre 侧（拍板 2）；duty/stress 接更多源（未回邮件、日程压力）；
  dream 夜间路（Ombre dream 工具已挂在醒来轮手上，reflection 维的现成出口）；
  醒来自开电脑软开关（拍板 4）；重游戏化（排班/技能面板）明确不做——保持内在驱动
  而非游戏面板（同原文口径）。
