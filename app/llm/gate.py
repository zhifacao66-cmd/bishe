"""本地模型并发闸门（显存感知的排队器）

为什么需要：
Ollama 在单卡上对同一模型基本是串行推理（8GB 显存下并行度=1~2）。若应用层不限流，
N 个并发请求会一起打到 Ollama，各自在 HTTP 连接上干等，先到的请求可能因为
排在队尾的请求占满队列而触发 60s 超时；而且排队过程不可观测、不可控。

做法：
在**应用进程内**放一个信号量，把排队前移到业务侧：
- 拿到槽位才真正调用模型，显存占用恒定；
- 排队超过 LLM_QUEUE_TIMEOUT 主动放弃 → 抛 LLMUnavailable → Agent 走规则降级，
  请求仍然返回 200（可用性优先），只是 llm_used=False；
- 排队时长、并发峰值、拒绝数全部计入快照，供 /api/health 与压测脚本观测。

边界说明：进程内信号量只在单进程（dev server / 单 worker）严格成立。
生产多 worker/多机部署时需换成 Redis 令牌桶或独立推理队列（见 docs/07 第六节）。
"""
import threading
import time
from contextlib import contextmanager


class QueueTimeout(Exception):
    """排队超时：不视为故障，由调用方降级处理"""


class LLMGate:
    def __init__(self):
        self._sem: threading.BoundedSemaphore | None = None
        self._cfg_lock = threading.Lock()
        self._stat_lock = threading.Lock()
        self.limit = 0
        self.queue_timeout = 0
        self.max_hold = 0.0
        # —— 观测指标 ——
        self.inflight = 0
        self.waiting = 0
        self.served = 0
        self.rejected = 0
        self.stale = 0                # 被判定僵死并强制回收的槽位数
        self.wait_sum = 0.0
        self.wait_max = 0.0
        self.peak_inflight = 0
        # 活跃持有者：token -> 开始时刻（monotonic）。用 dict 而不是单个时间戳，
        # 因为并发上限将来可能调到 2，只记"最近一个"会算错年龄。
        self._active: dict = {}
        self._seq = 0
        self._stale_tokens: set = set()

    def configure(self, limit: int, queue_timeout: float, max_hold: float = 0.0):
        limit = max(1, int(limit or 1))
        self.max_hold = float(max_hold or 0)
        if self._sem is not None and limit == self.limit:
            self.queue_timeout = float(queue_timeout)
            return
        with self._cfg_lock:
            if self._sem is None or limit != self.limit:
                self._sem = threading.BoundedSemaphore(limit)
                self.limit = limit
            self.queue_timeout = float(queue_timeout)

    def reclaim_stale(self) -> int:
        """回收僵死的槽位，返回回收个数。

        真实事故：线上一次首录的模型调用卡在网络层，`requests` 的 timeout
        并没有兜住它（**timeout 不覆盖 DNS 解析**，也不覆盖某些中间设备
        只发 SYN-ACK 不回数据的半开连接）。于是 inflight 永久停在 1。
        因为并发上限也是 1，之后**所有**请求都在这里排 40 秒队然后降级——
        表现出来就是"模型忽然全挂了"，而模型本身好好的，重启一下又好了。

        这类故障不能只靠人重启：占位必须能自愈。
        """
        if self.max_hold <= 0:
            return 0
        now = time.monotonic()
        reclaimed = 0
        with self._stat_lock:
            for tok, start in list(self._active.items()):
                if now - start < self.max_hold:
                    continue
                self._active.pop(tok, None)
                self._stale_tokens.add(tok)
                self.stale += 1
                reclaimed += 1
                if self.inflight > 0:
                    self.inflight -= 1
        for _ in range(reclaimed):
            try:
                self._sem.release()
            except (ValueError, AttributeError):
                pass                  # 已经回到初始额度，多放一次会溢出，忽略
        return reclaimed

    @property
    def hold_age(self) -> float:
        """最老活跃槽位已持有多久（秒）。没人在用时为 0。"""
        with self._stat_lock:
            if not self._active:
                return 0.0
            return time.monotonic() - min(self._active.values())

    @contextmanager
    def slot(self, timeout: float | None = None):
        """取一个模型槽位。

        `timeout` 是**单次请求**的排队上限；不传才用全局 LLM_QUEUE_TIMEOUT。
        公网匿名入口会传更短的值：居民不值得为一个规则兜底也能办的事等 40 秒。
        """
        sem = self._sem
        if sem is None:                       # 未 configure 时退化为不限流
            yield
            return
        effective = self.queue_timeout if timeout is None else max(0.0, float(timeout))
        t0 = time.perf_counter()
        with self._stat_lock:
            self.waiting += 1
        acquired = sem.acquire(timeout=effective)
        wait = time.perf_counter() - t0
        with self._stat_lock:
            self.waiting -= 1
            self.wait_sum += wait
            self.wait_max = max(self.wait_max, wait)
            if not acquired:
                self.rejected += 1
        if not acquired:
            # 排队超时前先看一眼：是不是前面那个根本没在推理，只是卡在网络上。
            # 有僵死就回收再来一次，避免"一次网络卡顿 = 整个模型通道停摆"。
            if self.reclaim_stale():
                acquired = sem.acquire(timeout=1.0)
            if not acquired:
                raise QueueTimeout(
                    f"本地模型排队超时（>{effective:.0f}s，并发上限 {self.limit}）"
                )
        with self._stat_lock:
            self._seq += 1
            tok = self._seq
            self._active[tok] = time.monotonic()
            self.inflight += 1
            self.served += 1
            self.peak_inflight = max(self.peak_inflight, self.inflight)
        try:
            yield
        finally:
            with self._stat_lock:
                self._active.pop(tok, None)
                stale = tok in self._stale_tokens
                self._stale_tokens.discard(tok)
                if not stale and self.inflight > 0:
                    self.inflight -= 1       # 僵死时已在 reclaim 里减过，别减两次
            try:
                sem.release()
            except ValueError:
                pass                         # 同上：僵死回收后额度已补，再放会溢出

    def snapshot(self) -> dict:
        with self._stat_lock:
            avg = self.wait_sum / self.served if self.served else 0.0
            return {
                "limit": self.limit,
                "queue_timeout": self.queue_timeout,
                "max_hold": self.max_hold,
                "inflight": self.inflight,
                "waiting": self.waiting,
                "served": self.served,
                "rejected": self.rejected,
                "stale": self.stale,
                "hold_age_s": round(time.monotonic() - min(self._active.values()), 1)
                              if self._active else 0.0,
                "peak_inflight": self.peak_inflight,
                "wait_avg_ms": round(avg * 1000, 1),
                "wait_max_ms": round(self.wait_max * 1000, 1),
            }

    def reset(self):
        with self._stat_lock:
            self.inflight = 0
            self.waiting = 0
            self.served = 0
            self.rejected = 0
            self.stale = 0
            self.wait_sum = 0.0
            self.wait_max = 0.0
            self.peak_inflight = 0
            self._active.clear()
            self._stale_tokens.clear()


gate = LLMGate()
