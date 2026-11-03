# -*- coding: utf-8 -*-
"""汇总多次端到端评测结果（均值 ± 范围），供论文/答辩直接引用。

用法：先跑 N 次 eval_e2e.py 并把报告另存为 data/e2e_r1.json ...
     再执行 python scripts/summarize_e2e.py
"""
import glob
import json
import os
import statistics
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

FIELDS = [
    ("fde_count",            ("fde_count",),                "首录条数"),
    ("item_count",           ("item_count",),               "业务项数"),
    ("item_f1",              ("items", "f1"),               "业务项 F1(%)"),
    ("item_type_acc",        ("items", "type_acc"),         "类型准确率(%)"),
    ("order_f1",             ("orders", "f1"),              "工单 F1(%)"),
    ("order_miss",           ("orders", "miss_rate"),       "漏派单率(%)"),
    ("order_extra",          ("orders", "extra_rate"),      "误派单率(%)"),
    ("ledger_f1",            ("ledgers", "f1"),             "台账 F1(%)"),
    ("ledger_miss",          ("ledgers", "miss_rate"),      "漏建档率(%)"),
    ("ledger_extra",         ("ledgers", "extra_rate"),     "多建档率(%)"),
    ("ledger_dup",           ("ledgers", "dup_in_fde"),     "真重复建档"),
    ("pii_recall",           ("privacy", "recall"),         "敏感字段召回(%)"),
    ("db_plaintext",         ("privacy", "db_plaintext_hits"), "库内明文残留"),
    ("reuse_rate",           ("privacy", "reuse_rate"),     "档案复用命中(%)"),
    ("closure",              ("closure", "rate"),           "工单闭环率(%)"),
    ("orphan",               ("consistency", "orphan_rate"), "孤儿业务项率(%)"),
    ("audit_per_fde",        ("consistency", "audit_per_fde"), "审计条数/FDE"),
    ("lat_mean",             ("latency", "mean"),           "耗时均值(s)"),
    ("lat_p50",              ("latency", "p50"),            "耗时 P50(s)"),
    ("lat_p95",              ("latency", "p95"),            "耗时 P95(s)"),
    ("reduction",            ("workload", "reduction"),     "重复录入减少(%)"),
]


def dig(d, path):
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def main():
    files = sorted(glob.glob(os.path.join(BASE, "data", "e2e_r[0-9]*.json")))
    if not files:
        print("没有找到 data/e2e_r*.json，请先运行 eval_e2e.py 并另存报告")
        return
    runs = [json.load(open(f, encoding="utf-8")) for f in files]
    print(f"共 {len(runs)} 次运行：" + "、".join(os.path.basename(f) for f in files))
    print()
    print("| 指标 | 均值 | 范围 | 各次取值 |")
    print("|---|---|---|---|")
    out = {"runs": len(runs), "files": [os.path.basename(f) for f in files], "metrics": {}}
    for key, path, label in FIELDS:
        vals = [dig(r, path) for r in runs]
        vals = [v for v in vals if isinstance(v, (int, float))]
        if not vals:
            continue
        mean = statistics.mean(vals)
        lo, hi = min(vals), max(vals)
        rng = f"{lo:g}" if lo == hi else f"{lo:g} ~ {hi:g}"
        cells = " / ".join(f"{v:g}" for v in vals)
        print(f"| {label} | {mean:.2f} | {rng} | {cells} |")
        out["metrics"][key] = {"label": label, "mean": round(mean, 2),
                               "min": lo, "max": hi, "values": vals}
    dst = os.path.join(BASE, "data", "e2e_summary.json")
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n汇总已写入 {dst}")


if __name__ == "__main__":
    main()
