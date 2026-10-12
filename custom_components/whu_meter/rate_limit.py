"""进程级服务器访问限速器（纯标准库，无 HA / 第三方依赖）。

设计目标
--------
无论"谁"、以"哪条代码路径"、在"什么异常情况下"访问武大水电服务器，
服务器侧观测到的请求间隔都必须 >= ``min_interval``（默认 1.0 秒）。

需要覆盖的全部请求来源（它们最终都取到同一个限速器实例）：

1. 配置流（config_flow）临时创建的 :class:`WhuMeterClient`；
2. 每个 config entry 的协调器（DataUpdateCoordinator）持有的客户端；
3. token 失效后的重新登录与重试；
4. 以及任何将来新增的调用路径 —— 只要经过 ``WhuMeterClient._request``，就自动受控。

（2026-10-12 晚）此前还有一条来源：pyscript 脚本 ``electricity_monitor.py`` 用
``requests`` 直连服务器、绕开本集成的旁路。该脚本连同它的 IPv4 shim 一并删除，
「让旁路共享本集成额度」的那套按文件路径二次装载的逻辑也随之作废，故此处不再提及。

关键实现选择
------------
* **注册表挂在 ``sys`` 模块属性上，而不是模块级全局变量。**
  同一个进程里，本文件完全可能被装载出多份「模块对象」（各自带一套模块级全局），
  例如按文件路径二次加载、或将来被别的集成/脚本复用。``sys`` 是真正唯一的
  进程级对象，把注册表（和它的锁）挂在 ``sys`` 上，才能保证「同一台服务器在
  整个进程里只有一份配额」，而不是每个模块副本各算一份。
* **预约式排队，临界区极短。**
  加锁只用来"算出一个未来的发车时刻"（微秒级），真正的等待在锁外做
  （``asyncio.sleep`` / ``time.sleep``）。这样既能跨协程、也能跨线程严格串行，
  又不会长时间占锁。排队顺序即预约顺序（FIFO），并发调用不会插队。
* **"实际发出时刻"也要满足间隔 —— 这是最容易做错的一点。**
  若只保证"预约时刻彼此间隔 1s"，仍可能违例：前一个请求因事件循环拥塞而
  晚于预约时刻才真正发出，后一个请求按点发出时就会贴得过近。
  因此：① 预约时额外留出 ``RESERVE_GUARD``（20ms）抖动余量；
  ② 真正返回前校验"距上一次**实际**发放是否已够一个间隔"；
  ③ 万一没通过（高负载下仍可能发生），按"不早于上次发放 + 间隔"**重新排期**，
  而不是排到队尾（排到队尾会把间隔拉成 2 秒，实测踩到过）。
* **不使用令牌桶。** 桶会在空闲后允许瞬时突发（比如连发 5 个），而这里要求的是
  硬性"任意 1 秒内最多 1 次"，所以用严格间隔。

有效速率：约 1 / (min_interval + RESERVE_GUARD) ≈ 0.98 次/秒，
始终 <= 1 次/秒（只会更慢，不会更快）。

环境变量（便于离线测试，生产默认值即为要求值）
------------------------------------------------
* ``WHU_METER_MIN_INTERVAL_S``   默认 ``1.0``
* ``WHU_METER_MAX_QUEUE_WAIT_S`` 默认 ``30.0``
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time

_LOGGER = logging.getLogger(__name__)

DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_MAX_QUEUE_WAIT = 30.0

# 预约时给"唤醒抖动"留的余量：让实际发放时刻也能满足间隔，而不必频繁重排。
RESERVE_GUARD = 0.02

_REGISTRY_ATTR = "_whu_meter_rate_limiters"
_REGISTRY_LOCK_ATTR = "_whu_meter_rate_limiters_lock"


class RateLimitExceeded(RuntimeError):
    """排队等待时间超过上限时抛出（说明瞬时并发远超配额）。

    注意：抛出它同样**满足**限速要求——被拒绝的请求根本没有发出去。
    """

    def __init__(self, key: str, wait_s: float, limit_s: float, waiting: int) -> None:
        super().__init__(
            f"服务器访问频率限制：{key} 瞬时请求过多，排队需等待 {wait_s:.1f}s，"
            f"超过上限（限速 {limit_s:.1f}s/次，当前排队 {waiting} 个）"
        )
        self.key = key
        self.wait_s = wait_s
        self.limit_s = limit_s
        self.waiting = waiting


class ServerRateLimiter:
    """把发往某台服务器的请求按固定最小间隔严格串行化。"""

    def __init__(
        self,
        key: str,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        max_queue_wait: float = DEFAULT_MAX_QUEUE_WAIT,
    ) -> None:
        self.key = key
        self.min_interval = float(min_interval)
        self.max_queue_wait = float(max_queue_wait)
        self._lock = threading.Lock()
        self._next_slot = 0.0          # 下一个可预约的发车时刻（monotonic）
        self._last_dispatch = 0.0      # 上一次"真正获准发出"的时刻
        self._created = time.monotonic()
        self.stats: dict[str, float] = {
            "total": 0,        # 累计获准发出的请求数
            "delayed": 0,      # 其中需要等待的请求数
            "rejected": 0,     # 因排队过深被拒绝的请求数
            "requeued": 0,     # 因"实际时刻校验"未通过而重新排期的次数（应≈0）
            "max_wait": 0.0,   # 观测到的最大等待秒数
            "waiting": 0,      # 当前正在等待的请求数
        }

    # ------------------------------------------------------------- internals

    def _reserve(self) -> float:
        """预约一个发车时刻，返回还需等待的秒数（不阻塞）。"""
        now = time.monotonic()
        with self._lock:
            start = self._next_slot if self._next_slot > now else now
            wait = start - now
            if wait > self.max_queue_wait:
                self.stats["rejected"] += 1
                raise RateLimitExceeded(
                    self.key, wait, self.min_interval, int(self.stats["waiting"])
                )
            self._next_slot = start + self.min_interval + RESERVE_GUARD
            self.stats["waiting"] += 1
        return wait

    def _try_grant(self, waited: float) -> bool:
        """校验并登记一次"实际发放"。False 表示需重新排期（不会真的发出）。"""
        now = time.monotonic()
        with self._lock:
            self.stats["waiting"] -= 1
            if now - self._last_dispatch < self.min_interval:
                self.stats["requeued"] += 1
                return False
            self._last_dispatch = now
            # 让后续预约以"实际发放时刻"为基准，避免误差累积
            self._next_slot = max(self._next_slot, now + self.min_interval + RESERVE_GUARD)
            self.stats["total"] += 1
            if waited > 0:
                self.stats["delayed"] += 1
                if waited > self.stats["max_wait"]:
                    self.stats["max_wait"] = waited
        return True

    def _reclaim(self) -> float:
        """把自己重排到"不早于上次发放 + 间隔"（**不排到队尾**，否则间隔会变 2s）。"""
        now = time.monotonic()
        with self._lock:
            target = self._last_dispatch + self.min_interval
            self._next_slot = max(self._next_slot, target + RESERVE_GUARD)
        return max(0.0, target - now)

    def _abandon(self) -> None:
        """等待期间被取消/中断：归还「正在等待」计数。

        已预约的车位**不回收** —— 于是后续请求最多多等一个间隔，方向是"更慢"
        而不是"更快"，不可能因此突破限速。这样处理比"回收车位"简单且更安全。
        """
        with self._lock:
            if self.stats["waiting"] > 0:
                self.stats["waiting"] -= 1

    # ---------------------------------------------------------------- public

    async def acquire(self) -> float:
        """异步获取一次访问许可。返回等待秒数；被拒绝时抛 RateLimitExceeded。

        可被取消：等待中被取消（前端断开、流程被中止、HA 关闭）时，会先把
        计数归还再原样抛出 CancelledError，不会让统计泄漏、也不会误发请求。
        """
        started = time.monotonic()
        while True:
            wait = self._reserve()
            if wait > 0:
                _LOGGER.debug("限速：访问 %s 前等待 %.2fs", self.key, wait)
                try:
                    await asyncio.sleep(wait)
                except asyncio.CancelledError:
                    self._abandon()
                    raise
            waited = time.monotonic() - started
            if self._try_grant(waited):
                return waited
            if waited > self.max_queue_wait:
                with self._lock:
                    self.stats["rejected"] += 1
                raise RateLimitExceeded(
                    self.key, waited, self.min_interval, int(self.stats["waiting"])
                )
            extra = self._reclaim()
            if extra > 0:
                # 此时「等待计数」已由 _try_grant 归还，取消无需额外处理
                await asyncio.sleep(extra)

    def acquire_sync(self) -> float:
        """同步获取一次访问许可（供线程 / executor 中的调用方使用）。"""
        started = time.monotonic()
        while True:
            wait = self._reserve()
            if wait > 0:
                _LOGGER.debug("限速：访问 %s 前等待 %.2fs", self.key, wait)
                try:
                    time.sleep(wait)
                except BaseException:
                    self._abandon()
                    raise
            waited = time.monotonic() - started
            if self._try_grant(waited):
                return waited
            if waited > self.max_queue_wait:
                with self._lock:
                    self.stats["rejected"] += 1
                raise RateLimitExceeded(
                    self.key, waited, self.min_interval, int(self.stats["waiting"])
                )
            extra = self._reclaim()
            if extra > 0:
                time.sleep(extra)

    def snapshot(self) -> dict[str, float | str]:
        with self._lock:
            return {
                "key": self.key,
                "min_interval_s": self.min_interval,
                "reserve_guard_s": RESERVE_GUARD,
                "max_queue_wait_s": self.max_queue_wait,
                "uptime_s": round(time.monotonic() - self._created, 1),
                **self.stats,
            }


# ------------------------------------------------------------------- registry


def _registry_and_lock() -> tuple[dict[str, ServerRateLimiter], threading.Lock]:
    """取进程级注册表与其锁（挂在 sys 上，保证跨模块副本唯一）。"""
    reg = getattr(sys, _REGISTRY_ATTR, None)
    lock = getattr(sys, _REGISTRY_LOCK_ATTR, None)
    if reg is not None and lock is not None:
        return reg, lock
    if lock is None:
        lock = threading.Lock()
        setattr(sys, _REGISTRY_LOCK_ATTR, lock)
    with lock:
        reg = getattr(sys, _REGISTRY_ATTR, None)
        if reg is None:
            reg = {}
            setattr(sys, _REGISTRY_ATTR, reg)
    return reg, lock


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        _LOGGER.warning("环境变量 %s=%r 不是数字，回退为 %s", name, raw, default)
        return default


def get_limiter(
    key: str,
    min_interval: float | None = None,
    max_queue_wait: float | None = None,
) -> ServerRateLimiter:
    """取（或创建）某台服务器的限速器。key 建议用 ``urlparse(url).netloc``。"""
    if min_interval is None:
        min_interval = _env_float("WHU_METER_MIN_INTERVAL_S", DEFAULT_MIN_INTERVAL)
    if max_queue_wait is None:
        max_queue_wait = _env_float("WHU_METER_MAX_QUEUE_WAIT_S", DEFAULT_MAX_QUEUE_WAIT)

    reg, lock = _registry_and_lock()
    with lock:
        limiter = reg.get(key)
        if limiter is None:
            limiter = ServerRateLimiter(key, min_interval, max_queue_wait)
            reg[key] = limiter
            _LOGGER.info(
                "whu_meter 限速器已启用：%s 每 %.1fs 最多 1 次请求（排队上限 %.0fs，已注册）",
                key, min_interval, max_queue_wait,
            )
        return limiter


def describe_all() -> list[dict[str, float | str]]:
    """便于诊断：列出进程内所有限速器的计数。"""
    reg, lock = _registry_and_lock()
    with lock:
        limiters = list(reg.values())
    return [lim.snapshot() for lim in limiters]


def reset_registry() -> None:
    """仅供测试：丢弃全部限速器（生产代码不要调用）。"""
    reg, lock = _registry_and_lock()
    with lock:
        reg.clear()
