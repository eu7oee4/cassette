"""session_mgr：常驻 agent-sdk loop 的生命周期层（PLAN_sdk S1/PR4）。

一个注册表管所有常驻会话，键 = (char_id, scene)。game 先用，chat/code 迁移时复用。
只管生命周期（注册/查询/收摊/看守），不 import 任何业务模块——loop 里跑什么、
收摊要清什么锁，全由调用方以回调/runner 的形式带进来。

串台防御（[[cassette-charswitch-bug-class]]，在这一处执行）：
- **没有可变的全局「当前」**：查询必须带 (char_id, scene)，注册表不提供
  「当前活着的那个」这种便利入口——便利入口就是下一次串台的入口。
- 归属在 start() 那一刻钉死在 handle 上，活着期间不变；转移资源不改已存在的
  handle（半截会话跟人走的教训，口径同 code_bridge.session_char）。
- 独占组（exclusive_group）：同组只允许一个 loop 活着（"computer" 组=game/code
  互斥——「一边写代码一边打游戏」是物理不存在）。撞上是**拒绝不是杀**：杀正在
  跑的活是调用方的决定，不该是注册表的副作用。

看守内建（原 app._game_watchdog 三件事按 handle 配置搬进来）：
- idle_stop_sec 无活动 → on_idle_stop（收摊回调由调用方给：释放锁/推 Bark）。
- 停着等回话超 wait_nudge_sec → on_wait_nudge（每次等待只提醒一次）。
- handle.reopening 打着 marker 时看守全停手（滚动重开进行中≠idle，别抢收）；
  reopen_cooldown_sec 里不允许再次自动重开（防连环重开）。
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

_LOOP: Optional[asyncio.AbstractEventLoop] = None    # lifespan 里喂一次
_registry: dict[tuple[str, str], "LoopHandle"] = {}
_reg_lock = threading.Lock()


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """后端 lifespan 启动时喂进来。路由跑在线程池里，起 task/塞队列都要靠它过桥。"""
    global _LOOP
    _LOOP = loop


@dataclass
class WatchPolicy:
    idle_stop_sec: float = 20 * 60
    wait_nudge_sec: float = 5 * 60
    tick_sec: float = 60.0
    reopen_cooldown_sec: float = 5 * 60
    # 回调都收 handle。on_idle_stop 只在 loop 还活着时调，调完 mgr 会 cancel task。
    on_idle_stop: Optional[Callable[["LoopHandle"], None]] = None
    on_wait_nudge: Optional[Callable[["LoopHandle"], None]] = None


@dataclass
class LoopHandle:
    char_id: str
    scene: str
    exclusive_group: Optional[str] = None
    watch: Optional[WatchPolicy] = None
    started_at: float = field(default_factory=time.time)
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    meta: dict = field(default_factory=dict)         # loop 自用（截图计数等）
    task: Optional[asyncio.Task] = None
    last_activity: float = field(default_factory=time.time)
    awaiting_user_since: Optional[float] = None      # 停下来等 TA 回话的时刻
    reopening: bool = False                          # 滚动重开 marker：看守停手
    last_reopen: float = 0.0
    stop_reason: Optional[str] = None
    _nudged: bool = field(default=False, repr=False)

    def alive(self) -> bool:
        # task 还是 None = 注册了、还没在事件循环上起飞——也算活着：不然两个请求
        # 打在起飞窗口里，独占组的检查会同时放行（双开）。起飞失败的由 _boot 的
        # finally 摘牌，不会永远占着。
        return self.task is None or not self.task.done()

    def touch(self) -> None:
        """有动静（模型说话/调工具/TA 消息进来）。"""
        self.last_activity = time.time()
        self.awaiting_user_since = None
        self._nudged = False

    def wait_user(self) -> None:
        """这一轮说完、停下来等 TA。等待期间不算 idle 归零——idle 判的是
        last_activity，等回话 20 分钟没人理照样该收摊（口径同旧看守：哈希包含
        双方，「没变」=两边都没动）。"""
        if self.awaiting_user_since is None:
            self.awaiting_user_since = time.time()

    def put_threadsafe(self, item) -> None:
        """从任意线程往 loop 的队列塞消息（路由线程 → 事件循环）。"""
        if _LOOP is None:
            raise RuntimeError("session_mgr 还没 set_event_loop")
        _LOOP.call_soon_threadsafe(self.queue.put_nowait, item)

    def request_stop(self, reason: str) -> None:
        """线程安全收摊。loop 的 runner 在 finally 里自己清（断连/释放锁）。"""
        self.stop_reason = self.stop_reason or reason
        if _LOOP is not None and self.task is not None:
            _LOOP.call_soon_threadsafe(self.task.cancel)


def start(char_id: str, scene: str,
          runner: Callable[[LoopHandle], Awaitable[None]], *,
          exclusive_group: Optional[str] = None,
          watch: Optional[WatchPolicy] = None,
          meta: Optional[dict] = None) -> LoopHandle | dict:
    """注册并起一个常驻 loop。撞键/撞独占组返回 {"ok": False, "error": ...}
    （谁挡的路说清楚），成功返回 handle。线程安全：路由线程直接调。"""
    handle = LoopHandle(char_id=char_id, scene=scene,
                        exclusive_group=exclusive_group, watch=watch,
                        meta=dict(meta or {}))
    with _reg_lock:
        cur = _registry.get((char_id, scene))
        if cur is not None and cur.alive():
            return {"ok": False, "error": f"{char_id} 的 {scene} 会话已经开着"}
        if exclusive_group:
            for h in _registry.values():
                if h.exclusive_group == exclusive_group and h.alive():
                    return {"ok": False, "error":
                            f"独占组 {exclusive_group} 被占着（{h.char_id} 的 {h.scene}）——"
                            "先收摊再切，杀不杀正在跑的活由调用方决定"}
        _registry[(char_id, scene)] = handle

    async def _boot() -> None:
        handle.task = asyncio.current_task()
        watch_task = (asyncio.create_task(_watchdog(handle))
                      if handle.watch else None)
        try:
            if handle.stop_reason:      # 起飞前就被 request_stop 了（cancel 没得取消）
                return
            await runner(handle)
        finally:
            if watch_task:
                watch_task.cancel()
            with _reg_lock:
                if _registry.get((char_id, scene)) is handle:
                    del _registry[(char_id, scene)]

    if _LOOP is None:
        raise RuntimeError("session_mgr 还没 set_event_loop")
    running = None
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        pass
    if running is _LOOP:
        asyncio.ensure_future(_boot())
    else:
        asyncio.run_coroutine_threadsafe(_boot(), _LOOP)
    # task 由 _boot 第一行钉上；起飞前 alive() 短暂为 False，get() 已经能查到。
    return handle


def get(char_id: str, scene: str) -> Optional[LoopHandle]:
    with _reg_lock:
        h = _registry.get((char_id, scene))
    return h if (h and h.alive()) else None


def group_alive(group: str) -> Optional[LoopHandle]:
    """独占组里现在谁活着（没有返回 None）。这是唯一的"横向"查询，且必须带组名
    ——不提供无参的「当前会话」。"""
    with _reg_lock:
        for h in _registry.values():
            if h.exclusive_group == group and h.alive():
                return h
    return None


def stop(char_id: str, scene: str, reason: str = "manual") -> bool:
    h = get(char_id, scene)
    if h is None:
        return False
    h.request_stop(reason)
    return True


async def _watchdog(handle: LoopHandle) -> None:
    w = handle.watch
    assert w is not None
    while True:
        await asyncio.sleep(w.tick_sec)
        if handle.reopening:
            continue                      # 滚动重开进行中：不算 idle，不 nudge
        now = time.time()
        if now - handle.last_activity > w.idle_stop_sec:
            if w.on_idle_stop:
                try:
                    w.on_idle_stop(handle)
                except Exception:
                    pass
            handle.stop_reason = handle.stop_reason or "idle"
            if handle.task:
                handle.task.cancel()
            return
        if (handle.awaiting_user_since is not None and not handle._nudged
                and now - handle.awaiting_user_since > w.wait_nudge_sec):
            handle._nudged = True         # 每次等待只提醒一次，TA 回话后 touch 重置
            if w.on_wait_nudge:
                try:
                    w.on_wait_nudge(handle)
                except Exception:
                    pass
