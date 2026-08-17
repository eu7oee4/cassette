# 三期 Plan：团团入住（宠物系统）

> 2026-08-16 方案定稿（同日两轮讨论拍板完毕）。前提：二期大房子一期已上电（PLAN_cohabit C4
> 观察期）。团团从 mianmian-app **搬家**过来（不分身：那边退役，状态与日志迁移当记忆）。
> mianmian 侧团团系统的完整调查（数据模型/引擎/MCP/美术管线）见该仓 server/PLAN_pet.md，
> 本文只记 cassette 侧怎么接。

## 定位一句话

**团团 = 世界层的第三类实体（pet ≠ character），配一个独立的廉价猫引擎
（DeepSeek 迷你协议），它的动作落进房间事件流从而物理化地参与整个小屋**——
薅醒不再需要 poke 标记，猫走到你房间挠门就是事件醒来。

## 已拍板的决策（2026-08-16 机主）

- **搬家不分身**：mianmian 侧退役，`state/pet/state.json` + `petlog.jsonl` 迁移过来当记忆。
  两个后端不通，同一只猫两份人生不成立。
- **实体身份：pet 是第三类实体，不是 character**。`entity_ids() = user + characters + pets`；
  猫有位置、可被 carry、可当事件 actor、出现在「这里有谁」，但 characters 的全部管线
  （手机线程/persona 引擎/wake 预算/通讯录）都不认识它——硬塞进 characters 会到处漏。
- **猫洞设定**：团团不受门禁管（`can_enter` 对 pet 豁免）。信息安全**不靠门禁靠注入**：
  猫的上下文只含当前房间 + 照料记录，不含它在别处看到的对话——带不走就转述不了，
  通道结构性封死（小卡「会走路的信息通道」之问的正式答案）。残余泄漏只剩
  「团团刚从谁房里跑出来」这种位置信息，认了，是现实感不是漏洞。
- **引擎继续 DeepSeek**（OpenAI 兼容，~3.5s 一次；接线走 env/config，不进库硬编码）。
  不占 `WAKE_EXEC_LOCK`（那是 claude -p 的锁），猫有自己的轻量 worker。
- **角色关系不加文案**：char 记忆里已有团团，只加一套 pet 工具说明进工具菜单，
  模型自己判断怎么相处。
- **拉屎引擎驱动**（mianmian 教训：铲屎形同摆设，因为从没让模型拉屎——
  「记得提醒模型」和「忘记提醒」是同一条路）：见下「需求系统」。
- **自动喂食器/饮水机在猫房**：猫粮/水永不缺。挑食+不爱喝水人设照旧
  （猫粮不饿不吃、罐罐吃饱自停、冻干/猫条无上限但心情最多 70、水最多 70——
  口径从 mianmian BUTTON_SPEC 移植）。饿了概率行为=随便找人讨罐罐/自己去吃猫粮。
- **猫的活动范围 = 注册房间白名单**：不去 away；hallway 整个从世界删除（见 P-1）。
- **抱起**：`world.move` 的 carry 政策放开到猫（机制早预埋好了，cohabit.py CARRY
  解析处只需加一个合法目标）。床/姿态**不建子地点**：猫的姿态走地点状态快照
  （「团团蜷在沙发上睡着了」），猫引擎在移动/睡眠切换时自动维护、猫走了就 remove
  ——正好是「现在这里有什么」的快照语义。
- **猫事件不算外部输入**：不重置 char 的连发计数（否则猫是无限续杯器，N=4 被打穿）；
  char 对猫事件的醒来概率调低；猫自己也有连发上限。半夜跑酷吵醒人是低概率功能。
- **自唤醒回环三层拆**（char 逗猫 → 猫必醒 → 猫反应落事件 → 又唤 char 的振荡器）：
  ① 猫的反应直接在 MCP 工具结果里同轮返回给发起者；② 猫反应事件入队时**排除发起者**
  （作者排除的姊妹规则）；③ 猫事件不重置连发（上条）。三层都要。
- **互动要让在场其他人看见**：投喂落系统提示「谁 给团团喂了 罐罐/猫条/冻干」；
  互动由工具替 char 落一条事件（「Cassius 摸了摸团团的头」），工具说明注明
  「动作已替你落进房间，别在 MOTION 里重复」——防双写。
- **开源仓口径**：通用宠物系统代码进库；团团实例（persona、sprite、petlog/state、
  DeepSeek 接线）是数据不进库（gitignore），与 characters 同构
  （import_cass.py 删团团章节的先例延续）。

## 状态模型（从 mianmian pet_store 移植 + 改造）

- 保留：`satiety / hydration / energy / mood`（0–100，mood 不被动衰减）、
  `litter`（1 干净 / 2 有屎 / 3 满了）、`asleep`、惰性衰减（无定时任务，读时按流逝现算，
  睡着暂停衰减改回精力、到 90 自醒）、`petlog.jsonl`（actor 加入 cassette 的实体 id 体系）。
- 新增：`poop_pressure`（隐藏憋屎压力值，见需求系统）。
- 摘除：`poke`（物理化薅醒取代——猫想找人自己走过去）。
- **位置不进 pet state**：位置权威仍是 `state/world.json`（团团是普通 entity），
  不搞两份真相。

## 需求系统（引擎驱动，模型只管风味）

原则：**生存性行为不靠模型自觉**。压力值/地板是引擎的，拉不拉/吃不吃的「时机和样子」
才是模型的。

- **憋屎**：`poop_pressure` 随时间涨、吃东西加速涨（惰性计算，与衰减同构）。
  到阈值 → 触发自主醒，原因注入「你想拉屎了」→ 模型自己走到猫房解决（拉屎的
  确定性覆盖：litter+1，**只在猫房生效**）。压力过硬上限 → 引擎强制执行。
- **砂盆满了猫拒绝用** → 压力继续涨 → 触发原因升级成「憋得难受，去找人铲屎」
  → 猫去挠门喵喵叫 = 落房间事件 = 事件醒来接管。**物理化薅醒从需求系统自己长出来**，
  不单独设计。
- **饿/渴**：satiety/hydration 低过阈值 → 自主醒原因「你饿了/渴了」→ 模型选：
  讨罐罐（找人）或自己去猫房吃猫粮/喝水。**satiety 硬地板**：低过底线引擎强制吃猫粮
  ——自动喂食器的兜底语义就是「不靠模型自觉也饿不死」。
- **困**：energy 低 → 找地方睡（睡哪由模型定，姿态进地点状态快照）。

## 猫引擎（迷你醒来）

- 注入：四值+砂盆+睡醒 ／ **当前房间**的在场者与最近事件（别处的一概不给）／
  petlog 最近 N 条（照料连续性）／ 触发原因。
- 输出协议（JSON，`response_format=json_object` 同 mianmian）：
  `{reply: "*动作白描* 喵", action_tag: 固定集合, stats: 四值新绝对值, sleep,
  move: 房间id|null}`。**没有说话字段**——猫物理上不会转述人话（信息通道的
  结构性堵法之二）；pet persona 写死「听不懂复杂内容，只对语气和熟词反应」。
  数值权归模型 + 少数确定性覆盖（拉屎/铲屎/睡眠切换/硬地板），口径同 mianmian。
- 触发三路：
  1. **直接互动必醒**（投喂/互动/被抱），重置猫自己的连发计数；
  2. **所在房间新事件概率醒**（作者排除 + 发起者排除；概率调低）；
  3. **自主 tick**（需求驱动，见上；无需求时低频随机溜达/换睡姿）。
- DeepSeek 可以不听 char 的话（「让团团去找 Cass」只是原样转给引擎，去不去它定）。

## char 的 MCP 工具（pet MCP，挂 wake 与 chat，服务端在场校验）

1. **任何地点碰到团团**（char 与猫同地点才可用）：
   - `pet_state` 查看四值；
   - `pet_feed` 投喂（罐罐/猫条/冻干三选一）→ 落系统提示「谁 给团团喂了 X」；
   - `pet_interact` 互动（自由文本，原样给 DeepSeek）→ 工具替 char 落动作事件。
   - 投喂/互动猫必醒，迷你协议的反应**在工具结果里同轮返回**。
2. **只有在猫房**：`pet_scoop` 铲屎（litter 置 1，落系统事件；**不要求猫在场**——
   铲的是盆不是猫；猫在场则概率反应）。

用户侧入口（P3 一并做）：RoomPage 团团在场时出按钮组（喂/撸/铲），走同一套端点、
同一套在场校验——比 mianmian 更「真」一格：铲屎得人走到猫房。

## Milestones

- **P-1 hallway 摘除** ✅（2026-08-17，前置清理，独立 commit，与猫无关先行）：
  代码足迹已查清（2026-08-16）——`world.py`（常量/docstring/move 特判/
  `_default_location` 兜底改**客厅**）、`cohabit.py` 两处分支（:89 走廊文案 / :350
  act 无房间守卫改只剩 away）、`cohabit_queue.py:213` 聊天提示、`app.py:845` 注释、
  `tests/test_world.py`+`test_cohabit.py` 三个用例改写、iOS `HouseModels.swift:11` 注释。
  当前 world.json 没人站走廊，删除干净。**away 保留**（出门/回家开关在用），
  只从猫的可去范围摘除。
- **P0 世界层泛化** ✅（2026-08-17）：`server/pets.py` 注册表（`server/pets/<id>/pet.json`
  与 characters 同构，目录 gitignore）；猫房补种进房间注册表（只补缺不覆盖，
  type=bedroom+owner → 默认位置顺路成立）；`entity_ids()`/`entity_name` 认猫；
  `can_enter` 猫洞豁免；CARRY 放开到猫（抱猫不走邀约，直接 move+carry）；
  醒来提示词有猫才提猫；事件醒来队列不认猫（enqueue 会炸的路从源头排除）；
  `_alone` 猫不算「别人」；`_append_experience` 跳过猫（信息通道的结构性封堵）；
  用户 `/world/move` 加 `carry` 参数（只放行宠物）。**团团实例未注册**——
  没脑子的猫不该先出现在屋里，注册随 P1 搬家一起。
- **P1 猫引擎** ✅（2026-08-17）：`pet_store.py`（多宠物 `state/pets/<pid>/`、poke 摘除、
  poop_pressure 惰性涨 + 吃饭加速、needs() 需求推导、姿态锚点）+ `pet_engine.py`
  （接线 pet.json engine 段回落 DEEPSEEK_* env；注入=状态/需求/**当前房间**/petlog/
  触发，别处对话结构性不进；迷你协议 reply/action/stats/sleep/move/pose/log 无说话
  字段；确定性覆盖=拉屎限猫房+砂盆满拒用；enforce() 生存硬地板走真实物理）+
  迁移脚本 `tools/import_tuantuan.py`（persona/state/petlog/.env 键，created_at 保留）。
  引擎面 interact()/wake()/enforce() 建好未接线——触发是 P2、工具是 P3。
  **迁移脚本未执行**：等 P2/P3 齐了上电时跑（先搬=屋里出现一只没脑子的猫，
  且 mianmian 侧还活着会状态分裂）。测试 115 全绿（新增 13）。
- **P2 触发与闸** ✅（2026-08-17）：`pet_queue.py`——猫 worker（app lifespan 起独立
  daemon，没宠物空转，注册+重启即上线）；事件概率醒（作者排除+PET_EVENT_PROB=0.25+
  间隔 180s+连发 N=3，interact=外部输入清零，需求/溜达=自主醒反清零同 solo 口径）；
  需求 tick（60s：enforce 硬地板先行且不叠模型醒、同类需求 30 分钟不重复催、
  没需求 IDLE_PROB=0.02 溜达）；错误冷却 10 分钟。char 侧三道门在
  cohabit_queue._on_room_event：猫动静唤 char 概率 PET_TO_CHAR_PROB=0.5、
  **发起者排除**（interaction_guard：工具结果已同轮给过反应）、连发不重置
  （external_input 只有用户路由调，结构性成立）。引擎级 _ENGINE_LOCK 串行
  interact/tick。测试 125 全绿（新增 10）。
- **P3 照料入口**：pet MCP 四工具 + 工具菜单说明；用户侧 RoomPage 按钮组。
- **P4 iOS 渲染**：sprite 管线搬运（美术是 mianmian 私产 → gitignore 实例数据）；
  HousePage/RoomPage 里团团的展示与走动；聊天页 overlay 要不要保留待议
  （cassette 聊天已是悬浮层，overlay 叠 overlay 的形态待真机看）。
- **P5 搬家收尾（mianmian 仓的活，单独尾巴）**：poke 标记/聊天 overlay/pet MCP 挂载退役。

## 待议

- [ ] 聊天页猫 overlay 保留与否（P4 真机看）。
- [ ] PetView 式全屏页要不要（还是房间视图就够）。
- [ ] 心情手滑条保留与否；手滑要不要进 petlog（mianmian 遗留待议）。
- [ ] 乱拉（憋过头在猫房外出事故）要不要——一期不做，压力硬上限=强制在猫房解决。
- [ ] pet_persona 重写（mianmian 侧自注草稿待眠眠重写，搬家时一并）。
- [ ] 猫房放几层、房间名（注册表可手编，默认值 P0 定）。
