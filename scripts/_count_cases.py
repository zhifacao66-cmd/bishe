"""串行单跑全部回归套，抓每套实际"通过 N 项"。

批跑不可信（多套共用 community.db 快照会撞 WAL），所以必须一个个起进程。
本项目三种输出格式并存，逐种匹配并保留候选行，避免"正则没命中就记 -1"。
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
# 子进程 stdout 是管道，Windows 默认 cp936；脚本里打 ✅ / ❌ 会 UnicodeEncodeError
# 直接 sec 退（rc=1、300ms、零输出），看起来像"测试挂了"，其实是编码。
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
LOGDIR = ROOT / "data" / "_case_out"
LOGDIR.mkdir(parents=True, exist_ok=True)
PATS = [
    re.compile(r"结果：\s*(\d+)\s*通过"),        # 结果：40 通过 / 0 失败
    re.compile(r"PASS\s+(\d+)\s*/\s*(\d+)"),      # 结果：PASS 32 / 32
    re.compile(r"通过\s*(\d+)\s*项"),              # 通过 52 项，失败 0 项
    re.compile(r"(\d+)\s*项通过"),
]

# 宿主有"单次调用内批量删除"保护（阈值 50）：每套会删自己的 -wal/-shm/db，
# 28 套一起跑必被拦在中途。所以支持按区间分段跑，每段独立一次调用。
lo, hi = 0, 999
if len(sys.argv) > 1 and ":" in sys.argv[1]:
    lo, hi = (int(x) for x in sys.argv[1].split(":"))

rows = []
for p in sorted((ROOT / "scripts").glob("smoke_*.py"))[lo:hi]:
    t0 = time.time()
    try:
        r = subprocess.run([PY, str(p)], cwd=str(ROOT), capture_output=True, env=ENV,
                           timeout=600, text=True, encoding="utf-8", errors="replace")
        out = (r.stdout or "") + "\n" + (r.stderr or "")
        code = r.returncode
    except subprocess.TimeoutExpired:
        out, code = "TIMEOUT", -9
    ms = int((time.time() - t0) * 1000)
    (LOGDIR / (p.stem + ".log")).write_text(out, encoding="utf-8", errors="replace")
    passed, total = -1, -1
    for pat in PATS:
        hits = pat.findall(out)
        if hits:
            last = hits[-1]
            passed = int(last[0] if isinstance(last, tuple) else last)
            if isinstance(last, tuple) and len(last) > 1:
                total = int(last[1])
            break
    fails = re.findall(r"(\d+)\s*(?:项)?失败", out)
    rows.append({"suite": p.stem, "pass": passed, "total": total,
                 "fail": int(fails[-1]) if fails else -1, "rc": code, "ms": ms})
    print(f"{p.stem:28s} pass={passed:4d} total={total:4d} rc={code} {ms}ms", flush=True)

ok = [r for r in rows if r["rc"] == 0 and r["pass"] >= 0]
res = {"suites_total": len(rows), "suites_green": len(ok),
       "items_total": sum(r["pass"] for r in ok),
       "unmatched": [r["suite"] for r in rows if r["pass"] < 0],
       "suites_failed": [r["suite"] for r in rows if r["rc"] != 0],
       "rows": rows}
_out = ROOT / "data" / f"_cases_{lo}_{hi}.json"
_out.write_text(json.dumps(res, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
print("=" * 70)
print(json.dumps({k: v for k, v in res.items() if k != "rows"}, ensure_ascii=False))
