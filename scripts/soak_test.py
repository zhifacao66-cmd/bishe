# -*- coding: utf-8 -*-
"""长稳测试（Soak Test）

并发压测回答「能扛多少」，长稳回答「会不会越跑越差」。

典型失效模式（只看单次请求发现不了）：
1. 内存泄漏：SQLAlchemy session / 线程局部对象没释放，RSS 单调上升直到 OOM
2. 线程/连接泄漏：每个请求新建连接未归还，线程数持续增长
3. WAL 失控：SQLite 的 -wal 文件只增不减，磁盘写满
4. 延迟漂移：模型上下文缓存膨胀、索引失效、表变大后查询变慢，P95 缓慢抬升
5. 质量退化：长时间运行后模型降级率上升（排队/超时变多）

做法：固定并发持续打混合负载，周期采样资源与统计窗口指标，
用最小二乘拟合 RSS 斜率判断「真泄漏」还是「正常缓存波动」。

用法
    python scripts/soak_test.py --minutes 20 --interval 30 --concurrency 2
    python scripts/soak_test.py --minutes 2 --interval 10          # 快速自检

输出
    data/soak_report.json   全量时间序列与结论
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import psutil                                              # noqa: E402

from bench_concurrency import (                            # noqa: E402
    Bench, CORPUS, db_counts, db_size_kb, pct, start_server,
)

# 长稳负载配比：以轻请求为主（拉高请求次数才能暴露慢泄漏），少量真实 FDE 保活模型链路
# 注意：所有接口都必须在 wang（社区工作者）的权限集内，否则 403 会被误算成系统错误。
# 曾经踩过：放了 /api/audit（audit.view 只有街道有），导致错误率恒为 10%，看着像不稳定。
MIX = [
    ("GET", "/api/health/ping", 30),
    ("GET", "/api/fde/stats", 15),
    ("GET", "/api/orders?limit=20", 15),
    ("GET", "/api/ledgers?limit=20", 10),
    ("GET", "/api/reports", 10),
    ("POST", "/api/health/dbwrite", 15),
    ("FULL", "", 5),
]


def gpu_snapshot():
    """显存/利用率/温度（无 NVIDIA 卡时返回空）"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return {}
        vals = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
        return {"gpu_mem_mb": float(vals[0]), "gpu_total_mb": float(vals[1]),
                "gpu_util": float(vals[2]), "gpu_temp": float(vals[3])}
    except Exception:                                      # noqa: BLE001
        return {}


def ollama_rss_mb():
    """Ollama 侧 RSS：模型服务自己的内存泄漏同样会拖垮整机"""
    total = 0.0
    try:
        for p in psutil.process_iter(["name", "memory_info"]):
            if "ollama" in (p.info["name"] or "").lower():
                total += p.info["memory_info"].rss / 1024 / 1024
    except Exception:                                      # noqa: BLE001
        pass
    return round(total, 1)


def slope_per_hour(xs_min, ys):
    """最小二乘斜率：输入分钟，输出每小时增量"""
    n = len(xs_min)
    if n < 3:
        return 0.0
    mx, my = sum(xs_min) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs_min)
    if den == 0:
        return 0.0
    k = sum((x - mx) * (y - my) for x, y in zip(xs_min, ys)) / den
    return k * 60.0          # 每分钟 → 每小时


def _get(url, timeout=10):
    """绕过系统代理（压测机常挂代理，127.0.0.1 不应经过它）"""
    return requests.get(url, timeout=timeout, proxies={"http": None, "https": None})


class Soak:
    def __init__(self, base, user, pwd, db_path, concurrency, pace, timeout=180):
        self.b = Bench(base, user, pwd, db_path, timeout)

    def _health(self) -> dict:
        """用已登录的会话取完整健康数据（匿名只有概要）"""
        r = self.b.s.get(f"{self.base}/api/health", timeout=10)
        return (r.json() or {}).get("data") or {}
        self.base = base
        self.concurrency = concurrency
        self.pace = pace
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.records = []          # (t_s, ok, lat, err, kind, llm_used)
        self.fde_ok = 0
        self.fde_items = 0
        self.fde_orders = 0
        self.round = 0

    def _pick(self):
        with self.lock:
            i = self.round
            self.round += 1
        # 轮盘法按权重取；用固定轮转更可复现，这里按累加权重取模
        total_w = sum(w for _, _, w in MIX)
        k = i % total_w
        acc = 0
        for m, path, w in MIX:
            acc += w
            if k < acc:
                return m, path
        return MIX[0][0], MIX[0][1]

    def worker(self, wid):
        self.b.login_once()
        while not self.stop.is_set():
            method, path = self._pick()
            if method == "FULL":
                r = self.b.call_full(CORPUS[wid % len(CORPUS)])
                kind = "full"
            else:
                r = self.b.call_light((method, path))
                kind = path
            with self.lock:
                self.records.append({
                    "t": time.time() - self.t0, "ok": r["ok"], "lat": r["lat"],
                    "err": r.get("err", ""), "kind": kind,
                    "llm_used": r.get("llm_used"),
                })
                if kind == "full" and r["ok"]:
                    self.fde_ok += 1
                    self.fde_items += r.get("items", 0)
                    self.fde_orders += r.get("orders", 0)
            if self.pace > 0:
                self.stop.wait(self.pace)

    def window(self, since):
        with self.lock:
            return [r for r in self.records if r["t"] >= since]

    def run(self, minutes, interval, proc):
        self.t0 = time.time()
        before = db_counts(self.b.db_path)      # 先取基线，再放流量，计数才对得上
        threads = [threading.Thread(target=self.worker, args=(i,), daemon=True)
                   for i in range(self.concurrency)]
        for t in threads:
            t.start()

        samples, last_t = [], 0.0
        deadline = self.t0 + minutes * 60
        win_idx = 0

        print(f"\n{'='*96}")
        print(f"长稳测试启动：{minutes} 分钟 / 并发 {self.concurrency} / 采样 {interval}s")
        print(f"{'='*96}")
        print(f"  {'时刻':>6} {'累计':>6} {'RSS':>8} {'线程':>4} {'显存':>8} {'DB':>8} "
              f"{'窗口P50':>8} {'窗口P95':>8} {'错误':>4} {'降级':>4}")
        print("  " + "-" * 90)

        try:
            while time.time() < deadline:
                time.sleep(min(interval, max(0.2, deadline - time.time())))
                if self.stop.is_set():
                    break
                now = time.time() - self.t0
                win = self.window(last_t)
                last_t = now
                # 漂移只看轻请求：full 请求单次 1.6~3s，落在哪个窗口纯属偶然，
                # 会把窗口 P95 拉出 10 倍抖动，看起来像"越跑越慢"。
                win_light = [r["lat"] for r in win if r["kind"] != "full"]
                win_lat = win_light or [r["lat"] for r in win]
                try:
                    # 必须带管理员会话：/api/health 对**匿名**只返回存活状态，
                    # 进程 RSS / 闸门 / 通道这些运维字段不给未登录的人看。
                    h = self._health()
                except Exception as e:                   # noqa: BLE001
                    print(f"  [警告] 采样失败: {str(e)[:80]}")
                    h = {"process": {}, "llm": {"gate": {}}}
                g = gpu_snapshot()
                samp = {
                    "t_min": round(now / 60, 2),
                    "total": len(self.records),
                    "rss_mb": (h.get("process") or {}).get("rss_mb"),
                    "threads": (h.get("process") or {}).get("threads"),
                    "gpu_mem_mb": g.get("gpu_mem_mb"),
                    "gpu_util": g.get("gpu_util"),
                    "gpu_temp": g.get("gpu_temp"),
                    "ollama_rss_mb": ollama_rss_mb(),
                    "db_kb": db_size_kb(self.b.db_path),
                    "wal_kb": round(os.path.getsize(self.b.db_path + "-wal") / 1024, 1)
                    if os.path.exists(self.b.db_path + "-wal") else 0.0,
                    "win_n": len(win),
                    "win_p50_ms": round(pct(win_lat, 50) * 1000, 1),
                    "win_p95_ms": round(pct(win_lat, 95) * 1000, 1),
                    "win_err": sum(1 for r in win if not r["ok"]),
                    "win_fallback": sum(1 for r in win if r.get("llm_used") is False),
                    "gate": (h.get("llm") or {}).get("gate") or {},
                }
                samples.append(samp)
                win_idx += 1
                print(f"  {samp['t_min']:>5.1f}m {samp['total']:>6} {samp['rss_mb']:>7.1f}M "
                      f"{samp['threads']:>4} {str(samp['gpu_mem_mb'] or '-'):>7}M "
                      f"{samp['db_kb']:>7.1f}K {samp['win_p50_ms']:>7.0f}ms "
                      f"{samp['win_p95_ms']:>7.0f}ms {samp['win_err']:>4} {samp['win_fallback']:>4}")
                if proc is not None and proc.poll() is not None:
                    print("  [严重] 被测进程已退出！")
                    break
        finally:
            self.stop.set()
            for t in threads:
                t.join(timeout=70)

        after = db_counts(self.b.db_path)
        return self.summarize(samples, before, after, minutes)

    def summarize(self, samples, before, after, minutes):
        with self.lock:
            recs = list(self.records)
        ok = [r for r in recs if r["ok"]]
        errs = {}
        for r in recs:
            if not r["ok"]:
                errs[r["err"] or "unknown"] = errs.get(r["err"] or "unknown", 0) + 1
        lats = [r["lat"] for r in recs]
        fallback = sum(1 for r in ok if r.get("llm_used") is False)

        # RSS 泄漏判定：拟合斜率 + 首末差双条件，避免把启动期缓存导致的一次性上涨当泄漏
        pts = [(s["t_min"], s["rss_mb"]) for s in samples if s.get("rss_mb")]
        rss_slope = slope_per_hour([p[0] for p in pts], [p[1] for p in pts]) if len(pts) >= 3 else 0.0
        rss_first = pts[0][1] if pts else 0
        rss_last = pts[-1][1] if pts else 0
        threads_first = samples[0].get("threads") if samples else None
        threads_last = samples[-1].get("threads") if samples else None
        wal_max = max([s.get("wal_kb") or 0 for s in samples], default=0)

        # 延迟漂移：后 1/3 与前 1/3 的窗口 P95 中位数对比
        w95 = [s["win_p95_ms"] for s in samples if s.get("win_n")]
        third = max(1, len(w95) // 3)
        p95_head = statistics.median(w95[:third]) if w95 else 0
        p95_tail = statistics.median(w95[-third:]) if w95 else 0
        drift = round((p95_tail - p95_head) / p95_head * 100, 1) if p95_head else 0.0

        consistency = {}
        if before and after and "error" not in before:
            consistency = {
                "fde_ok": self.fde_ok,
                "fde_delta": after.get("fde_record", 0) - before.get("fde_record", 0),
                "match": after.get("fde_record", 0) - before.get("fde_record", 0) == self.fde_ok,
                "item_expected": self.fde_items,
                "item_delta": after.get("work_item", 0) - before.get("work_item", 0),
                "order_expected": self.fde_orders,
                "order_delta": after.get("work_order", 0) - before.get("work_order", 0),
                "audit_delta": after.get("audit_log", 0) - before.get("audit_log", 0),
            }

        return {
            "minutes": minutes,
            "requests": len(recs),
            "ok": len(ok),
            "err": len(recs) - len(ok),
            "err_rate": round((len(recs) - len(ok)) / len(recs) * 100, 2) if recs else 0,
            "qps": round(len(recs) / (minutes * 60), 2),
            "lat_p50": round(pct(lats, 50), 3),
            "lat_p95": round(pct(lats, 95), 3),
            "lat_p99": round(pct(lats, 99), 3),
            "lat_max": round(max(lats), 3) if lats else 0,
            "fallback": fallback,
            "fallback_rate": round(fallback / len(ok) * 100, 2) if ok else 0.0,
            "errors": errs,
            "rss_first_mb": rss_first,
            "rss_last_mb": rss_last,
            "rss_growth_mb": round(rss_last - rss_first, 1),
            "rss_slope_mb_per_h": round(rss_slope, 1),
            "threads_first": threads_first,
            "threads_last": threads_last,
            "wal_max_kb": wal_max,
            "db_growth_kb": round((samples[-1].get("db_kb", 0) - samples[0].get("db_kb", 0))
                                  if samples else 0, 1),
            "db_before": before, "db_after": after,
            "p95_head_ms": p95_head, "p95_tail_ms": p95_tail, "p95_drift_pct": drift,
            "gpu_peak_mb": max([s.get("gpu_mem_mb") or 0 for s in samples], default=0),
            "consistency": consistency,
            "samples": samples,
        }


def verdict(res, args):
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("错误率", res["err_rate"] <= args.err_limit,
        f"{res['err_rate']}%（阈值 {args.err_limit}%）")
    add("内存增长", res["rss_growth_mb"] <= args.rss_limit_mb,
        f"首末差 {res['rss_growth_mb']}MB，斜率 {res['rss_slope_mb_per_h']}MB/h"
        f"（阈值 {args.rss_limit_mb}MB）")
    add("线程数", (res["threads_last"] or 0) - (res["threads_first"] or 0) <= 5,
        f"{res['threads_first']} → {res['threads_last']}")
    add("延迟漂移", abs(res["p95_drift_pct"]) <= args.drift_limit,
        f"{res['p95_head_ms']:.0f}ms → {res['p95_tail_ms']:.0f}ms"
        f"（{res['p95_drift_pct']:+.1f}%，阈值 ±{args.drift_limit}%）")
    add("WAL 体积", res["wal_max_kb"] <= 64 * 1024,
        f"峰值 {res['wal_max_kb'] / 1024:.1f}MB（阈值 64MB）")
    if res["consistency"]:
        c = res["consistency"]
        add("落库一致性", c["match"],
            f"FDE 成功 {c['fde_ok']} / 增量 {c['fde_delta']}；"
            f"业务项 {c['item_delta']}/{c['item_expected']}；"
            f"工单 {c['order_delta']}/{c['order_expected']}")
    return checks


def main():
    ap = argparse.ArgumentParser(description="长稳测试")
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--pace", type=float, default=0.4, help="每个 worker 的请求间隔秒数")
    ap.add_argument("--user", default="wang")
    ap.add_argument("--pwd", default="123456")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--spawn", dest="spawn", action="store_true", default=True)
    ap.add_argument("--no-spawn", dest="spawn", action="store_false")
    ap.add_argument("--port", type=int, default=5058)
    ap.add_argument("--db", default="data/soak.db")
    ap.add_argument("--env", action="append")
    ap.add_argument("--tag", default="soak")
    ap.add_argument("--err-limit", type=float, default=1.0)
    ap.add_argument("--rss-limit-mb", type=float, default=50.0)
    ap.add_argument("--drift-limit", type=float, default=30.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    db_path = args.db if os.path.isabs(args.db) else os.path.join(BASE_DIR, args.db)
    env_extra = {}
    for kv in args.env or []:
        k, v = kv.split("=", 1)
        env_extra[k.strip()] = v.strip()

    proc, base, log_path = None, "http://127.0.0.1:5000", ""
    if args.spawn:
        if os.path.exists(db_path):
            for suf in ("", "-wal", "-shm"):
                try:
                    os.remove(db_path + suf)
                except OSError:
                    pass
        proc, base, log_path = start_server(args.port, db_path, env_extra, args.tag)

    try:
        s = Soak(base, args.user, args.pwd, db_path, args.concurrency,
                 args.pace, args.timeout)
        res = s.run(args.minutes, args.interval, proc)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:                                # noqa: BLE001
                proc.kill()

    checks = verdict(res, args)
    report = {
        "tag": args.tag, "base": base, "finished_at": datetime.now().isoformat(timespec="seconds"),
        "result": res, "checks": checks,
        "passed": all(c["ok"] for c in checks), "log": log_path,
    }
    out = args.out or os.path.join(BASE_DIR, "data", f"{args.tag}_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*96}\n长稳结论\n{'='*96}")
    for c in checks:
        print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']:<8} {c['detail']}")
    print(f"\n  总请求 {res['requests']}（成功 {res['ok']}，错误 {res['err']}），"
          f"平均 {res['qps']} req/s")
    print(f"  延迟 P50 {res['lat_p50']*1000:.0f}ms / P95 {res['lat_p95']*1000:.0f}ms / "
          f"P99 {res['lat_p99']*1000:.0f}ms")
    print(f"  降级 {res['fallback']} 次（{res['fallback_rate']}%），"
          f"显存峰值 {res['gpu_peak_mb']:.0f}MB")
    print(f"  RSS {res['rss_first_mb']:.1f}MB → {res['rss_last_mb']:.1f}MB "
          f"({res['rss_slope_mb_per_h']:+.1f}MB/h)")
    print(f"\n{'✅ 通过：未发现泄漏、漂移或写入丢失' if report['passed'] else '❌ 存在未通过项，见上表'}")
    print(f"报告已写入 {out}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
