# -*- coding: utf-8 -*-
"""并发压测：真实 HTTP 全链路 + 落库一致性校验

与 eval_parser.py / eval_e2e.py 的区别：
- 前面两个测「对不对」，这里测「扛不扛得住」——同一套 FDE 入口，用递增并发去打。

测什么
1. 延迟分位：P50/P90/P95/P99（均值会掩盖长尾，基层用户对卡顿最敏感的是 P95）
2. 吞吐：QPS 与「并发-吞吐」曲线，判断是否已饱和
3. 质量衰减：并发升高时 llm_used=False（降级到规则）的比例——这是本系统特有的指标，
   模型排队超时会走规则兜底，接口仍然 200，但拆解质量下降，必须单独统计
4. 错误分类：5xx / 4xx / 业务码 / 连接超时 / database is locked
5. 落库一致性：成功 N 次，库里必须正好多 N 条 FDE 记录，业务项/工单增量必须等于
   各响应求和——并发下最容易出现「响应成功但没落库」或「重复落库」，只测延迟发现不了

两种负载
- full ：POST /api/fde 全链路（脱敏→拆解→派单→填报→汇总），瓶颈在 GPU
- light：GET 健康检查/统计/工单列表，纯框架+SQLite，用来证明「框架本身不是瓶颈」

用法
    # 连已在跑的服务
    python scripts/bench_concurrency.py --levels 1,2,4,8 --requests 8

    # 自起独立实例（独立库/端口，不污染演示数据）
    python scripts/bench_concurrency.py --spawn --port 5056 --db data/bench.db --tag baseline

    # 模拟「未加固」基线：关 WAL、放大模型并发
    python scripts/bench_concurrency.py --spawn --port 5056 --db data/bench_base.db \
        --env SQLITE_WAL=0 --env SQLITE_BUSY_TIMEOUT=5000 --env LLM_MAX_CONCURRENCY=64 \
        --tag baseline
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlparse

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------------------
# 语料：真实社区口吻，1~3 个业务项，覆盖物业/政务多条线
# ---------------------------------------------------------------------------
CORPUS = [
    "2栋2单元202室新租户李某，电话13800001234，这个月搬进来的",
    "7栋3单元电梯有异响，麻烦物业尽快检修，联系电话13900001111",
    "2栋2单元202室租户李某要补办居住登记",
    "今天走访7栋独居老人王奶奶，她血压偏高，家里下水道也堵了",
    "10栋架空层有人私拉电线给电动车充电，另外6栋地下室群租问题还没处理",
    "12栋302新搬来一对小夫妻，男的是外地户口要办居住证；顺便说楼道声控灯不亮了",
    "5栋2单元低保户张阿姨来问这个月低保金为什么还没到账",
    "9栋门禁坏了，另外3栋楼道灯也坏了，都需要修",
    "8栋2单元电梯坏了，老人下不了楼，麻烦尽快修",
    "街道通知明天上午九点开会，要求上报本月消防检查台账",
    "2栋楼下垃圾桶满了好几天没人清运，另外健身器材也坏了",
    "4栋1单元楼道堆放杂物影响逃生，居民反映过好几次了",
]

TABLES = ["fde_record", "work_item", "work_order", "ledger", "audit_log", "field_security"]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def pct(vals, p):
    """线性插值分位（与 numpy 默认一致，样本少时比 nearest 稳）"""
    if not vals:
        return 0.0
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def db_counts(path: str) -> dict:
    """只读连接数表行数；WAL 模式下可与写进程并发读"""
    import sqlite3
    out = {}
    if not path or not os.path.exists(path):
        return out
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        cur = conn.cursor()
        for t in TABLES:
            try:
                out[t] = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:                                # noqa: BLE001
                out[t] = -1
        conn.close()
    except Exception as e:                                   # noqa: BLE001
        out["error"] = str(e)[:120]
    return out


def db_size_kb(path: str) -> float:
    if not path or not os.path.exists(path):
        return 0.0
    total = os.path.getsize(path)
    for suf in ("-wal", "-shm"):                             # 长稳要看 WAL 是否失控膨胀
        p = path + suf
        if os.path.exists(p):
            total += os.path.getsize(p)
    return round(total / 1024, 1)


# ---------------------------------------------------------------------------
# 压测器
# ---------------------------------------------------------------------------
class Bench:
    def __init__(self, base: str, user: str, pwd: str, db_path: str, timeout: int = 180):
        self.base = base.rstrip("/")
        self.user, self.pwd = user, pwd
        self.db_path = db_path
        self.timeout = timeout
        self._local = threading.local()

    # ---- 每个线程独立 Session：避免共享 cookie jar 的内部锁成为假瓶颈 ----
    def session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.trust_env = False          # 不走系统代理（本机 127.0.0.1 更稳）
            # 不复用 keep-alive 连接：werkzeug 开发服务器会回收空闲长连接，
            # 客户端再次复用时会拿到已被服务端关闭的 socket（RemoteDisconnected），
            # 表现为「并发越高 conn_error 越多」的假故障，掩盖真实指标。
            s.headers.update({"Connection": "close"})
            r = s.post(f"{self.base}/login",
                       data={"username": self.user, "password": self.pwd},
                       timeout=15, allow_redirects=False)
            if r.status_code not in (301, 302, 303):
                raise RuntimeError(f"登录失败 HTTP {r.status_code}: {r.text[:120]}")
            self._local.s = s
        return s

    def login_once(self):
        self.session()

    # ---- 单次请求 ----
    def call_full(self, text: str) -> dict:
        t0 = time.perf_counter()
        rec = {"ok": False, "status": 0, "code": None, "err": "",
               "items": 0, "orders": 0, "llm_used": None}
        try:
            r = self.session().post(f"{self.base}/api/fde", json={"raw_text": text},
                                    timeout=self.timeout)
            rec["status"] = r.status_code
            rec["lat"] = time.perf_counter() - t0
            body = r.json()
            rec["code"] = body.get("code")
            if r.status_code == 200 and body.get("code") == 0:
                d = body["data"]
                rec["ok"] = True
                rec["items"] = len(d.get("items") or [])
                rec["orders"] = len(d.get("orders") or [])
                rec["llm_used"] = bool(d.get("llm_used"))
            else:
                rec["err"] = self._classify(r.status_code, body)
        except requests.Timeout:
            rec["lat"] = time.perf_counter() - t0
            rec["err"] = "client_timeout"
            rec["status"] = -1
        except Exception as e:                               # noqa: BLE001
            rec["lat"] = time.perf_counter() - t0
            rec["err"] = "conn_error"
            rec["detail"] = str(e)[:100]
            rec["status"] = -1
        rec.setdefault("lat", time.perf_counter() - t0)
        return rec

    def call_light(self, spec) -> dict:
        method, path = spec if isinstance(spec, tuple) else ("GET", spec)
        t0 = time.perf_counter()
        rec = {"ok": False, "status": 0, "code": None, "err": ""}
        try:
            s = self.session()
            if method == "POST":
                r = s.post(f"{self.base}{path}", json={}, timeout=self.timeout)
            else:
                r = s.get(f"{self.base}{path}", timeout=self.timeout)
            rec["status"] = r.status_code
            body = r.json()
            rec["code"] = body.get("code")
            rec["ok"] = r.status_code == 200 and body.get("code") == 0
            if not rec["ok"]:
                rec["err"] = self._classify(r.status_code, body)
        except requests.Timeout:
            rec["err"] = "client_timeout"
            rec["status"] = -1
        except Exception as e:                               # noqa: BLE001
            rec["err"] = "conn_error"
            rec["detail"] = str(e)[:100]
            rec["status"] = -1
        rec["lat"] = time.perf_counter() - t0
        return rec

    @staticmethod
    def _classify(status: int, body: dict) -> str:
        if status >= 500:
            return f"http_{status}"
        if status == 429:
            return "http_429"
        msg = str(body.get("msg", ""))
        if "locked" in msg.lower():
            return "db_locked"
        if status in (401, 403):
            return f"http_{status}"
        return f"biz_{body.get('code')}"

    # ---- 单档并发 ----
    def run_level(self, level: int, total: int, mode: str, ex: ThreadPoolExecutor) -> dict:
        tasks = []
        for i in range(total):
            if mode == "full":
                tasks.append(("full", CORPUS[i % len(CORPUS)]))
            else:
                # 4 读 1 写：读写混合更接近真实负载，写探针用来暴露 SQLite 写锁竞争
                specs = [
                    ("GET", "/api/health/ping"),
                    ("GET", "/api/fde/stats"),
                    ("GET", "/api/orders?limit=20"),
                    ("GET", "/api/ledgers?limit=20"),
                    ("POST", "/api/health/dbwrite"),
                ]
                tasks.append(("light", specs[i % len(specs)]))
        results = []
        lock = threading.Lock()

        def work(t):
            r = self.call_full(t[1]) if t[0] == "full" else self.call_light(t[1])
            r["path"] = t[1] if isinstance(t[1], str) else t[1][1]
            with lock:
                results.append(r)

        # 分批提交：每批恰好 level 个同时在飞，保证并发度严格等于档位。
        # executor 在外部创建并复用，线程内的登录会话因此只建立一次（PBKDF2 很贵，
        # 若每档重建线程池，每个线程首次登录的 ~100ms 会污染首批延迟分位）。
        t0 = time.perf_counter()
        for i in range(0, len(tasks), level):
            batch = tasks[i:i + level]
            futs = [ex.submit(work, t) for t in batch]
            for f in futs:
                f.result()
        wall = time.perf_counter() - t0

        lats = [r["lat"] for r in results]
        ok = [r for r in results if r["ok"]]
        errs = {}
        details = {}
        for r in results:
            if not r["ok"]:
                errs[r["err"] or "unknown"] = errs.get(r["err"] or "unknown", 0) + 1
                d = (r.get("detail") or "")[:110]
                if d:
                    details[d] = details.get(d, 0) + 1
        fallback = sum(1 for r in ok if r.get("llm_used") is False)

        # 按接口拆分，避免「一条慢查询/一次登录」被误读成整体长尾
        per_path = {}
        for r in results:
            k = r["path"]
            per_path.setdefault(k, []).append(r)
        endpoints = {}
        for k, rs in per_path.items():
            ls = [x["lat"] for x in rs]
            endpoints[k] = {
                "n": len(rs),
                "p50_ms": round(pct(ls, 50) * 1000, 1),
                "p95_ms": round(pct(ls, 95) * 1000, 1),
                "max_ms": round(max(ls) * 1000, 1),
                "err": sum(1 for x in rs if not x["ok"]),
            }

        return {
            "level": level,
            "requests": len(tasks),
            "ok": len(ok),
            "err": len(results) - len(ok),
            "err_rate": round((len(results) - len(ok)) / len(results) * 100, 2),
            "qps": round(len(ok) / wall, 2) if wall > 0 else 0.0,
            "wall_s": round(wall, 2),
            "lat_min": round(min(lats), 3),
            "lat_mean": round(statistics.mean(lats), 3),
            "lat_p50": round(pct(lats, 50), 3),
            "lat_p90": round(pct(lats, 90), 3),
            "lat_p95": round(pct(lats, 95), 3),
            "lat_p99": round(pct(lats, 99), 3),
            "lat_max": round(max(lats), 3),
            "llm_used": sum(1 for r in ok if r.get("llm_used") is True),
            "fallback": fallback,
            "fallback_rate": round(fallback / len(ok) * 100, 2) if ok else 0.0,
            "items_sum": sum(r.get("items", 0) for r in ok),
            "orders_sum": sum(r.get("orders", 0) for r in ok),
            "errors": errs,
            "error_details": details,
            "endpoints": endpoints,
        }


# ---------------------------------------------------------------------------
# 独立实例
# ---------------------------------------------------------------------------
def start_server(port: int, db_path: str, env_extra: dict, tag: str):
    url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update({
        "APP_PORT": str(port), "APP_TEST": "1", "FLASK_ENV": "dev",
        "DATABASE_URL": "sqlite:///" + db_path.replace("\\", "/"),
    })
    env.update(env_extra or {})
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    log_path = os.path.join(BASE_DIR, "logs", f"bench_{tag}.log")
    log = open(log_path, "ab")
    print(f"  · 启动被测实例 :{port}  db={os.path.basename(db_path)}  "
          f"env={ {k: v for k, v in (env_extra or {}).items()} }")
    proc = subprocess.Popen([sys.executable, "run.py"], cwd=BASE_DIR, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 120
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"被测实例启动失败，详见 {log_path}")
        try:
            r = requests.get(f"{url}/api/health/ping", timeout=2, proxies={"http": None})
            if r.status_code == 200:
                return proc, url, log_path
        except Exception:                                    # noqa: BLE001
            pass
        time.sleep(0.6)
    proc.terminate()
    raise RuntimeError(f"被测实例 120s 未就绪，详见 {log_path}")


def parse_env(pairs):
    out = {}
    for kv in pairs or []:
        if "=" not in kv:
            raise SystemExit(f"--env 需要 K=V 格式: {kv}")
        k, v = kv.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------------------
# 打印
# ---------------------------------------------------------------------------
def print_table(rows, mode):
    unit = "ms" if mode == "light" else "s"
    mul = 1000 if mode == "light" else 1
    print(f"\n  {'并发':>4} {'请求':>4} {'成功':>4} {'失败':>4} {'降级':>4} "
          f"{'QPS':>7} {'P50':>8} {'P90':>8} {'P95':>8} {'P99':>8} {'最大':>8}")
    print("  " + "-" * 82)
    for r in rows:
        print(f"  {r['level']:>4} {r['requests']:>4} {r['ok']:>4} {r['err']:>4} "
              f"{r['fallback']:>4} {r['qps']:>7.2f} "
              f"{r['lat_p50']*mul:>7.0f}{unit} {r['lat_p90']*mul:>7.0f}{unit} "
              f"{r['lat_p95']*mul:>7.0f}{unit} {r['lat_p99']*mul:>7.0f}{unit} "
              f"{r['lat_max']*mul:>7.0f}{unit}")
    all_err = {}
    for r in rows:
        for k, v in r["errors"].items():
            all_err[k] = all_err.get(k, 0) + v
    if all_err:
        print("  错误分布:", ", ".join(f"{k}={v}" for k, v in all_err.items()))
        det = {}
        for r in rows:
            for k, v in (r.get("error_details") or {}).items():
                det[k] = det.get(k, 0) + v
        for k, v in sorted(det.items(), key=lambda x: -x[1])[:3]:
            print(f"    ×{v}  {k}")
    if mode == "light" and rows:
        r = rows[-1]
        print(f"\n  接口明细（并发 {r['level']}，按 P95 降序）:")
        print(f"    {'接口':<32}{'n':>4}{'P50':>9}{'P95':>9}{'max':>9}{'err':>5}")
        for k, v in sorted(r["endpoints"].items(), key=lambda x: -x[1]["p95_ms"]):
            print(f"    {k:<32}{v['n']:>4}{v['p50_ms']:>8.1f}ms{v['p95_ms']:>8.1f}ms"
                  f"{v['max_ms']:>8.1f}ms{v['err']:>5}")


def main():
    ap = argparse.ArgumentParser(description="FDE 全链路并发压测")
    ap.add_argument("--base", default="http://127.0.0.1:5000")
    ap.add_argument("--levels", default="1,2,4,8")
    ap.add_argument("--requests", type=int, default=8, help="full 模式每档总请求数")
    ap.add_argument("--light-requests", type=int, default=40, help="light 模式每档总请求数")
    ap.add_argument("--modes", default="full,light")
    ap.add_argument("--user", default="wang")
    ap.add_argument("--pwd", default="123456")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--db", default="", help="数据库文件路径，用于落库一致性校验")
    ap.add_argument("--spawn", action="store_true", help="自起独立实例")
    ap.add_argument("--port", type=int, default=5056)
    ap.add_argument("--env", action="append", help="覆盖环境变量 K=V，可重复")
    ap.add_argument("--tag", default="bench")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    env_extra = parse_env(args.env)
    db_path = args.db if args.db else os.path.join(BASE_DIR, "data", "community.db")
    if db_path and not os.path.isabs(db_path):
        db_path = os.path.join(BASE_DIR, db_path)

    proc = None
    base = args.base
    if args.spawn:
        PROC_DB = db_path
        if os.path.exists(PROC_DB):
            for suf in ("", "-wal", "-shm"):     # 每次从干净库开始，计数才可比
                try:
                    os.remove(PROC_DB + suf)
                except OSError:
                    pass
        proc, base, log_path = start_server(args.port, PROC_DB, env_extra, args.tag)
    else:
        PROC_DB = db_path

    report = {
        "tag": args.tag, "base": base, "started_at": datetime.now().isoformat(timespec="seconds"),
        "levels": levels, "requests_per_level": args.requests,
        "env_overrides": env_extra, "modes": {},
    }
    try:
        b = Bench(base, args.user, args.pwd, PROC_DB, args.timeout)
        b.login_once()          # 健康详情要登录态（匿名只给概要），先建会话再取
        h = b.s.get(f"{base}/api/health", timeout=10)
        report["health"] = h.json().get("data", {}) if h.ok else {}
        print("=" * 88)
        print(f"并发压测  tag={args.tag}  target={base}")
        print(f"模型={report['health'].get('llm', {}).get('model')}  "
              f"引擎={report['health'].get('db', {}).get('engine')}  "
              f"闸门并发={report['health'].get('llm', {}).get('gate', {}).get('limit')}")
        print("=" * 88)

        if args.warmup > 0 and "full" in modes:
            print(f"[预热] 连续 {args.warmup} 次请求（模型加载/首次 CUDA kernel 不计入统计）...")
            for _ in range(args.warmup):
                r = b.call_full(CORPUS[0])
                print(f"       warmup: ok={r['ok']} {r['lat']:.2f}s llm_used={r.get('llm_used')}")

        # 线程池全局复用：每个线程只登录一次，压测期间不再付 PBKDF2 的代价
        max_level = max(levels)
        ex = ThreadPoolExecutor(max_workers=max_level, thread_name_prefix="bench")
        try:
            bar = threading.Barrier(max_level)

            def _warm_thread(_i):
                try:
                    bar.wait(timeout=60)
                except Exception:                            # noqa: BLE001
                    pass
                b.login_once()

            list(ex.map(_warm_thread, range(max_level)))
            print(f"[会话] 已为 {max_level} 个工作线程建立登录会话")

            for mode in modes:
                print(f"\n{'='*88}\n【{'FDE 全链路' if mode == 'full' else '轻接口（框架+DB）'}】\n{'='*88}")
                before = db_counts(PROC_DB)
                rows = []
                for lv in levels:
                    total = args.requests if mode == "full" else args.light_requests
                    row = b.run_level(lv, total, mode, ex)
                    rows.append(row)
                    print(f"  ▸ 并发 {lv:>2} 完成：{row['ok']}/{row['requests']} 成功，"
                          f"QPS {row['qps']:.2f}，P95 {row['lat_p95']*1000:.0f}ms")
                after = db_counts(PROC_DB)

                consistency = {}
                if mode == "full" and before and after and "error" not in before:
                    ok_sum = sum(r["ok"] for r in rows)
                    item_sum = sum(r["items_sum"] for r in rows)
                    order_sum = sum(r["orders_sum"] for r in rows)
                    d = {t: after.get(t, 0) - before.get(t, 0) for t in TABLES}
                    consistency = {
                        "ok_requests": ok_sum,
                        "fde_delta": d["fde_record"],
                        "fde_match": d["fde_record"] == ok_sum,
                        "item_delta": d["work_item"], "item_expect": item_sum,
                        "item_match": d["work_item"] == item_sum,
                        "order_delta": d["work_order"], "order_expect": order_sum,
                        "order_match": d["work_order"] == order_sum,
                        "ledger_delta": d["ledger"],
                        "audit_delta": d["audit_log"],
                        "lost_writes": ok_sum - d["fde_record"],
                    }
                    print(f"\n  [落库一致性] 成功请求 {ok_sum} / FDE 增量 {d['fde_record']} → "
                          f"{'✅ 一致' if consistency['fde_match'] else '❌ 不一致'}"
                          f"   业务项 {d['work_item']}/{item_sum}"
                          f"   工单 {d['work_order']}/{order_sum}"
                          f"   台账 {d['ledger']}   审计 {d['audit_log']}")

                report["modes"][mode] = {"rows": rows, "consistency": consistency,
                                         "db_before": before, "db_after": after}
                print_table(rows, mode)
        finally:
            ex.shutdown(wait=True)

        h2 = requests.get(f"{base}/api/health", timeout=10, proxies={"http": None, "https": None})
        report["health_after"] = h2.json().get("data", {}) if h2.ok else {}
        report["finished_at"] = datetime.now().isoformat(timespec="seconds")
        report["db_size_kb"] = db_size_kb(PROC_DB)

        out = args.out or os.path.join(BASE_DIR, "data", f"conc_{args.tag}.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n报告已写入 {out}")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:                                # noqa: BLE001
                proc.kill()


if __name__ == "__main__":
    main()
