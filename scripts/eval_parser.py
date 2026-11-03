"""拆解 Agent 效果评测脚本。

用途：用标注好的社区语料跑一遍 ParserAgent，量化三项指标，产出可直接写进论文的表格。

指标定义：
- 项数准确率：预测业务项数量 == 标注数量 的样本占比（衡量"该拆几条"）
- 类型准确率：按「(type) 多重集合」计算的 micro-F1（衡量"拆成什么事"）
- 去向准确率：类型匹配上的业务项，标注的业务线是否被全部命中（召回口径；
  多送一条线不算错，因为真实业务允许"一案多线"同时流转）
- 平均耗时：单条端到端耗时，反映模型可用性

用法：
    python scripts/eval_parser.py              # 跑全部用例
    python scripts/eval_parser.py --verbose    # 打印每条预测明细
    python scripts/eval_parser.py --case 3     # 只跑第 3 条
"""
import argparse
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# ---------------------------------------------------------------------------
# 标注集：43 条真实风格的社区口语记录
# 覆盖：单事项 / 一句话多事项 / 跨业务线 / 口语指代与省略 / 方言化表达
# 标注依据：docs/03-评测与消融实验.md 中的类型判定规则
# ---------------------------------------------------------------------------
CASES = [
    # ---------- A 报修（设施设备损坏）----------
    {"text": "7栋电梯异响，要检修，联系我13900001111",
     "expect": [{"type": "报修", "lines": {"物业"}}]},
    {"text": "2栋屋顶漏水，居民反映房间墙面发霉",
     "expect": [{"type": "报修", "lines": {"物业"}}]},
    {"text": "4栋2单元电梯困人了，已经联系物业",
     "expect": [{"type": "报修", "lines": {"物业"}}]},
    {"text": "8栋2单元电梯坏了，老人下不了楼，麻烦尽快修",
     "expect": [{"type": "报修", "lines": {"物业"}}]},
    {"text": "14栋302住户反映楼上漏水，已经三天了",
     "expect": [{"type": "报修", "lines": {"物业"}}]},
    {"text": "小区东门门禁刷卡失灵了，进出都不方便",
     "expect": [{"type": "报修", "lines": {"物业"}}]},
    {"text": "3栋楼道声控灯全灭了",
     "expect": [{"type": "报修", "lines": {"物业"}}]},
    {"text": "5栋旁边绿化带的水管爆了，水一直在流",
     "expect": [{"type": "报修", "lines": {"物业"}}]},

    # ---------- B 人口变动 ----------
    {"text": "2栋2单元202室新租户李某，电话13800001234",
     "expect": [{"type": "人口变动", "lines": {"综治"}}]},
    {"text": "2栋2单元202室租户张某补办居住登记",
     "expect": [{"type": "人口变动", "lines": {"综治"}}]},
    {"text": "12栋302搬来一户新住户，需要登记人口信息",
     "expect": [{"type": "人口变动", "lines": {"综治"}}]},
    {"text": "王阿姨家儿子从外地回来同住，要更新居住信息",
     "expect": [{"type": "人口变动", "lines": {"综治"}}]},
    {"text": "9栋503租户退租搬走了，需要注销登记",
     "expect": [{"type": "人口变动", "lines": {"综治"}}]},

    # ---------- C 走访 ----------
    {"text": "今天走访8栋独居老人王奶奶，她血压偏高，腿脚也不太方便",
     "expect": [{"type": "走访", "lines": {"民政", "卫健"}}]},
    {"text": "上午去看望了3栋的残疾户陈大哥，送了米面油",
     "expect": [{"type": "走访", "lines": {"民政"}}]},
    {"text": "15栋低保户刘阿姨家孩子考上大学，上门了解助学需求",
     "expect": [{"type": "走访", "lines": {"民政"}}]},
    {"text": "今天去7栋高龄老人周爷爷家探视，老人一个人住，需要定期关注",
     "expect": [{"type": "走访", "lines": {"民政"}}]},
    {"text": "走访2栋精神障碍患者家属，了解服药情况",
     "expect": [{"type": "走访", "lines": {"民政", "卫健"}}]},

    # ---------- D 调解 ----------
    {"text": "5栋1单元两户因为空调外机噪音吵起来了，需要调解",
     "expect": [{"type": "调解", "lines": {"综治"}}]},
    {"text": "6栋两家因为停车位争执不下，闹到社区来了",
     "expect": [{"type": "调解", "lines": {"综治"}}]},
    {"text": "3栋楼上楼下因漏水赔偿问题产生矛盾，希望社区调解",
     "expect": [{"type": "调解", "lines": {"综治"}}]},
    {"text": "11栋住户投诉隔壁装修噪音扰民，双方情绪激动",
     "expect": [{"type": "调解", "lines": {"综治"}}]},

    # ---------- E 咨询 ----------
    {"text": "低保户刘阿姨来问今年取暖补贴什么时候发",
     "expect": [{"type": "咨询", "lines": {"民政"}}]},
    {"text": "有居民来咨询新生儿医保怎么办理",
     "expect": [{"type": "咨询", "lines": {"卫健"}}]},
    {"text": "4栋租户问居住证办理需要哪些材料",
     "expect": [{"type": "咨询", "lines": {"综治"}}]},
    {"text": "居民来问老旧小区加装电梯的申请流程",
     "expect": [{"type": "咨询", "lines": {"街道"}}]},

    # ---------- F 隐患排查 ----------
    {"text": "6栋地下室有群租，住了十几个人，消防通道也被堵了",
     "expect": [{"type": "隐患排查", "lines": {"综治"}}]},
    {"text": "10栋架空层有人私拉电线给电动车充电，很危险",
     "expect": [{"type": "隐患排查", "lines": {"综治"}}]},
    {"text": "7栋2单元消防栓前面堆了杂物，通道被占",
     "expect": [{"type": "隐患排查", "lines": {"综治"}}]},
    {"text": "12栋楼道里堆放了很多纸箱杂物，存在火灾隐患",
     "expect": [{"type": "隐患排查", "lines": {"综治"}}]},

    # ---------- G 行政事务 ----------
    {"text": "街道通知下周三上午九点开消防工作会，要带上本月检查台账",
     "expect": [{"type": "其他", "lines": {"街道", "综治"}}]},
    {"text": "明天上午街道要来检查文明创建工作，需要准备材料",
     "expect": [{"type": "其他", "lines": {"街道"}}]},
    {"text": "区里要求本周五前上报独居老人名单",
     "expect": [{"type": "其他", "lines": {"街道", "民政"}}]},
    {"text": "月底前要把本月矛盾纠纷排查表报到街道",
     "expect": [{"type": "其他", "lines": {"街道", "综治"}}]},

    # ---------- H 一句话多事项（含并列/追加表述）----------
    {"text": "1栋楼下垃圾桶满了好几天没人清运，另外3栋的健身器材也坏了",
     "expect": [{"type": "报修", "lines": {"物业"}},
                {"type": "报修", "lines": {"物业"}}]},
    {"text": "12栋302新搬来一对小夫妻，男的是外地户口，要办居住证；顺便说楼道声控灯不亮了",
     "expect": [{"type": "人口变动", "lines": {"综治"}},
                {"type": "报修", "lines": {"物业"}}]},
    {"text": "2栋2单元202室新租户李某电话13800001234，另外7栋电梯异响要检修，顺便说楼道灯也不亮了",
     "expect": [{"type": "人口变动", "lines": {"综治"}},
                {"type": "报修", "lines": {"物业"}},
                {"type": "报修", "lines": {"物业"}}]},
    {"text": "今天去看望9栋独居的张大爷，他血压有点高，家里下水道还堵了",
     "expect": [{"type": "走访", "lines": {"民政", "卫健"}},
                {"type": "报修", "lines": {"物业"}}]},
    {"text": "5栋有两户因为噪音吵起来，同时10栋架空层有人私拉电线充电",
     "expect": [{"type": "调解", "lines": {"综治"}},
                {"type": "隐患排查", "lines": {"综治"}}]},
    {"text": "街道要我们上报本月消防台账，另外6栋地下室群租问题还没处理完",
     "expect": [{"type": "其他", "lines": {"街道", "综治"}},
                {"type": "隐患排查", "lines": {"综治"}}]},

    # ---------- I 口语化 / 指代 / 省略（难点样本）----------
    {"text": "老张家那口子又犯病了，得上门看看",
     "expect": [{"type": "走访", "lines": {"民政", "卫健"}}]},
    {"text": "王姐说楼上那户半夜老响，吵得睡不着",
     "expect": [{"type": "调解", "lines": {"综治"}}]},
    {"text": "3号楼2单元那个电梯又不行了",
     "expect": [{"type": "报修", "lines": {"物业"}}]},
]


def _f1(pairs):
    """基于 (predict, expect) 多重计数的 micro-F1"""
    pred_all, exp_all = Counter(), Counter()
    for p, e in pairs:
        for k in p:
            pred_all[k] += p[k]
        for k in e:
            exp_all[k] += e[k]
    tp = sum(min(pred_all[k], exp_all[k]) for k in set(pred_all) | set(exp_all))
    precision = tp / max(sum(pred_all.values()), 1)
    recall = tp / max(sum(exp_all.values()), 1)
    f1 = 2 * precision * recall / max(precision + recall, 1e-9)
    return precision, recall, f1


def score(pred, exp):
    """单条打分，返回 {n_ok, pc, ec, line_hit, line_total}"""
    pc = Counter(p["type"] for p in pred)
    ec = Counter(e["type"] for e in exp)
    line_hit, line_total = 0, 0
    used = set()
    for e in exp:
        for j, p in enumerate(pred):
            if j in used or p["type"] != e["type"]:
                continue
            used.add(j)
            line_total += 1
            # 召回口径：标注的业务线被全部命中即算对
            line_hit += set(e["lines"]).issubset(set(p["target_lines"]))
            break
    if line_total < len(exp):
        line_total = len(exp)
    return {"n_ok": len(pred) == len(exp), "pc": pc, "ec": ec,
            "line_hit": line_hit, "line_total": line_total}


def evaluate(only_case=None, verbose=False, agent=None):
    from app import create_app

    app = create_app("dev")
    rows = []
    with app.app_context():
        if agent is None:
            from app.agents.parser import ParserAgent
            agent = ParserAgent()
        for i, case in enumerate(CASES, 1):
            if only_case and i != only_case:
                continue
            t0 = time.time()
            out = agent.run({"raw_text": case["text"]})
            cost = time.time() - t0
            pred, exp = out["items"], case["expect"]
            s = score(pred, exp)
            rows.append({
                "no": i, "text": case["text"], "cost": cost, "source": out["source"],
                "n_ok": s["n_ok"], "pred_n": len(pred), "exp_n": len(exp),
                "pc": s["pc"], "ec": s["ec"],
                "line_hit": s["line_hit"], "line_total": s["line_total"],
                "pred": pred,
                "exp": [{"type": e["type"], "lines": sorted(e["lines"])} for e in exp],
            })
            if verbose:
                mark = lambda b: "\033[32m✓\033[0m" if b else "\033[31m✗\033[0m"
                print(f"[{i:02d}] {mark(s['n_ok'])} {cost:5.2f}s {out['source']:8s} {case['text'][:34]}")
                for p in pred:
                    print(f"      → {p['type']:6s} {p['subject'][:26]:28s} {'/'.join(p['target_lines'])}")

    n = len(rows)
    if not n:
        print("没有匹配的用例")
        return None

    precision, recall, f1 = _f1([(r["pc"], r["ec"]) for r in rows])
    n_acc = sum(r["n_ok"] for r in rows) / n
    l_hit = sum(r["line_hit"] for r in rows)
    l_tot = sum(r["line_total"] for r in rows)
    avg = sum(r["cost"] for r in rows) / n
    llm_cnt = sum(1 for r in rows if r["source"] == "llm")

    print("\n" + "=" * 62)
    print("拆解 Agent 评测报告")
    print("=" * 62)
    print(f"样本数              {n}")
    print(f"LLM 实际生效        {llm_cnt}/{n}" + ("  ← 有降级，检查模型服务" if llm_cnt < n else ""))
    print(f"业务项数量准确率    {n_acc:.1%}   （目标 ≥85%）")
    print(f"业务类型 P / R / F1 {precision:.1%} / {recall:.1%} / {f1:.1%}   （目标 F1 ≥85%）")
    print(f"业务线去向准确率    {l_hit}/{l_tot} = {l_hit / max(l_tot, 1):.1%}")
    print(f"平均单条耗时        {avg:.2f}s")
    print("=" * 62)

    bad = [r for r in rows if not r["n_ok"] or r["line_hit"] < r["line_total"]]
    if bad:
        print(f"未完全正确的样本 {len(bad)} 条：")
        for r in bad:
            print(f"  #{r['no']} {r['text'][:34]}  预期 {r['exp_n']} 项 {dict(r['ec'])} / 实得 {r['pred_n']} 项 {dict(r['pc'])}")

    report = {
        "samples": n, "llm_used": llm_cnt,
        "n_acc": round(n_acc, 4), "type_f1": round(f1, 4),
        "type_precision": round(precision, 4), "type_recall": round(recall, 4),
        "line_acc": round(l_hit / max(l_tot, 1), 4), "avg_seconds": round(avg, 3),
        "details": [{"no": r["no"], "text": r["text"], "pred": r["pred"],
                     "expect": r["exp"], "cost": round(r["cost"], 3)} for r in rows],
    }
    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "eval_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"明细已写入 {os.path.normpath(out_path)}")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--case", type=int, default=None)
    a = ap.parse_args()
    evaluate(a.case, a.verbose)
