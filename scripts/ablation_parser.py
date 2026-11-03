"""拆解 Agent 消融实验（Ablation Study）。

目的：量化每个工程手段对最终效果的贡献，回答"这些设计到底值不值"。

对比配置：
    rules  纯规则桩（无 LLM，关键词匹配）—— 基线
    json   LLM + format:"json"（无 schema、无重试）—— 最朴素的接法
    schema LLM + JSON Schema 约束解码（无重试）
    full   LLM + JSON Schema + 预分句校验重试 —— 系统实际方案
    think  full + 开启 Qwen3 思考链（只在子集上跑，测代价）

用法：
    python scripts/ablation_parser.py                 # 跑全部配置
    python scripts/ablation_parser.py --only full     # 只跑某个配置
    python scripts/ablation_parser.py --no-think      # 跳过思考链对比
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.eval_parser import CASES, _f1, score   # noqa: E402

# 思考链对比只跑子集：6 条多事项 + 4 条单事项，足够反映趋势且控制耗时
THINK_SUBSET = [1, 9, 14, 27, 35, 36, 37, 38, 39, 40]


def build_agent(name):
    from app.agents.parser import ParserAgent
    if name == "rules":
        return _RulesAgent()
    if name == "json":
        return ParserAgent(use_schema=False, use_retry=False, audit=False)
    if name == "schema":
        return ParserAgent(use_schema=True, use_retry=False, audit=False)
    if name == "full":
        return ParserAgent(use_schema=True, use_retry=True, audit=False)
    if name == "think":
        return ParserAgent(use_schema=True, use_retry=True, think=True, audit=False)
    raise ValueError(f"未知配置: {name}")


class _RulesAgent:
    """纯规则基线：完全不调用模型"""
    name = "rules"

    def run(self, payload):
        from app.agents.parser import ParserAgent
        return {"items": ParserAgent(audit=False)._fallback(payload["raw_text"]),
                "source": "rules"}


def run_config(name, cases_idx=None):
    from app import create_app
    app = create_app("dev")
    agent = build_agent(name)
    idx = cases_idx or list(range(1, len(CASES) + 1))
    rows = []
    with app.app_context():
        for i in idx:
            case = CASES[i - 1]
            t0 = time.time()
            out = agent.run({"raw_text": case["text"]})
            cost = time.time() - t0
            s = score(out["items"], case["expect"])
            rows.append({"no": i, "cost": cost, "source": out["source"], **s})

    n = len(rows)
    p, r, f1 = _f1([(x["pc"], x["ec"]) for x in rows])
    return {
        "config": name, "samples": n,
        "n_acc": sum(x["n_ok"] for x in rows) / n,
        "type_precision": p, "type_recall": r, "type_f1": f1,
        "line_acc": sum(x["line_hit"] for x in rows) / max(sum(x["line_total"] for x in rows), 1),
        "avg_seconds": sum(x["cost"] for x in rows) / n,
        "fallback": sum(1 for x in rows if x["source"] == "fallback"),
        "rows": rows,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只跑指定配置")
    ap.add_argument("--no-think", action="store_true")
    a = ap.parse_args()

    configs = ["rules", "json", "schema", "full"]
    if not a.no_think:
        configs.append("think")
    if a.only:
        configs = [a.only]

    results = []
    for cfg in configs:
        idx = THINK_SUBSET if cfg == "think" else None
        print(f"\n>>> 运行配置 [{cfg}]" + (f"（子集 {len(idx)} 条）" if idx else f"（全量 {len(CASES)} 条）"))
        res = run_config(cfg, idx)
        results.append(res)
        print(f"    数量准确率 {res['n_acc']:.1%} | 类型F1 {res['type_f1']:.1%} "
              f"| 去向 {res['line_acc']:.1%} | {res['avg_seconds']:.2f}s/条"
              + (f" | 降级 {res['fallback']}" if res["fallback"] else ""))

    # ---- 汇总表 ----
    label = {"rules": "纯规则（基线）", "json": "LLM + format:json",
             "schema": "LLM + Schema", "full": "Schema + 预分句重试",
             "think": "上述 + 思考链"}
    print("\n" + "=" * 88)
    print(f"{'配置':<24}{'样本':>5}{'数量准确率':>11}{'类型F1':>9}{'去向准确率':>11}{'耗时/条':>10}{'降级':>6}")
    print("-" * 88)
    for r in results:
        print(f"{label[r['config']]:<22}{r['samples']:>5}{r['n_acc']:>10.1%}"
              f"{r['type_f1']:>10.1%}{r['line_acc']:>11.1%}{r['avg_seconds']:>9.2f}s{r['fallback']:>6}")
    print("=" * 88)

    # ---- 逐条对比：json vs full（看哪些样本被修复）----
    by_cfg = {r["config"]: r for r in results}
    if "json" in by_cfg and "full" in by_cfg:
        j = {x["no"]: x for x in by_cfg["json"]["rows"]}
        f = {x["no"]: x for x in by_cfg["full"]["rows"]}
        fixed = [n for n in j if not j[n]["n_ok"] and f.get(n, {}).get("n_ok")]
        broke = [n for n in j if j[n]["n_ok"] and n in f and not f[n]["n_ok"]]
        print(f"\nSchema+重试 修复的样本: {len(fixed)} 条 {fixed}")
        print(f"Schema+重试 变差的样本: {len(broke)} 条 {broke}")
        for n in fixed:
            print(f"  #{n} {CASES[n-1]['text'][:44]}")

    # ---- 落盘 ----
    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cases": len(CASES),
        "think_subset": THINK_SUBSET,
        "results": [{k: v for k, v in r.items() if k != "rows"} for r in results],
        "details": {r["config"]: [{"no": x["no"], "n_ok": x["n_ok"],
                                   "cost": round(x["cost"], 3),
                                   "pred_types": dict(x["pc"]),
                                   "exp_types": dict(x["ec"])} for x in r["rows"]]
                    for r in results},
    }
    path = os.path.join(os.path.dirname(__file__), "..", "data", "ablation_report.json")
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=2)
    print(f"\n结果已写入 {os.path.normpath(path)}")


if __name__ == "__main__":
    main()
