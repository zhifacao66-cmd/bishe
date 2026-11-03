"""进程内滑动窗口限流（零依赖）

只做一件事：`allow(key, limit, window)` 数时间窗内的调用次数。
放这里而不是各模块各写一份，是因为"限流口径"一旦不统一，就会出现
同一个入口在 A 处限 20 次、B 处限 120 次，看起来都限了、实际按宽松的走。

已知边界（写清楚，别装作没有）：
  · **单进程内存**。多 worker 部署时每个进程各限一份，等于限额×worker 数。
    本项目当前是单进程跑 Flask/gunicorn，够用；真要上多进程得换 Redis。
  · 进程重启后计数清零。所以"锁定"这种必须扛重启的能力不能建在这上面，
    见 `app/api/portal.py` 里按审计表算的查询码锁定。
  · 键的选取比实现更重要：公网入口用 IP 只是兜底（代理池可绕过），
    真正管用的是按手机号、按账号这种攻击者换不掉的维度。
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_BUCKET: dict[str, list[float]] = {}
_CLEAN_AT = {"ts": 0.0}


def allow(key: str, limit: int, window: float) -> bool:
    """窗口内计数未达上限则记一次并返回 True，否则 False。"""
    now = time.time()
    with _LOCK:
        arr = [t for t in _BUCKET.get(key, []) if now - t < window]
        if len(arr) >= limit:
            _BUCKET[key] = arr
            return False
        arr.append(now)
        _BUCKET[key] = arr
        # 每 5 分钟清一次过期键：键里带手机号/IP，不清会一直长
        if now - _CLEAN_AT["ts"] > 300:
            for k in [k for k, v in _BUCKET.items() if not v or now - v[-1] > 3600]:
                _BUCKET.pop(k, None)
            _CLEAN_AT["ts"] = now
        return True


def reset():
    """测试用：清空所有计数（各 smoke 脚本之间不能互相污染）"""
    with _LOCK:
        _BUCKET.clear()
