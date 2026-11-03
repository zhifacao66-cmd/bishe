# -*- coding: utf-8 -*-
"""端到端业务指标评测（E2E）

与 eval_parser.py（只评拆解模型）不同，这里走**真实 HTTP 全链路**：

    登录 → POST /api/fde → 保密审查 → 拆解 → 派单 → 填报
         → 物业接单/反馈 → 台账归档 → 报表生成

统计的是业务后果，不是模型分数：漏派单率、漏建档率、重复建档率、
库内明文残留、工单闭环率、端到端耗时、减负结构性口径。

设计要点
1. 跑在独立库 data/eval.db，每次运行重建，绝不污染演示数据
2. 期望单据由标注 items 推导（含"物业"→工单，其余线→台账），
   避免人工标注与业务规则自相矛盾
3. 匹配用"关键词命中 + 贪心最大匹配"，不做字符串全等（LLM 摘要措辞会变）

用法：
    python scripts/eval_e2e.py            # 全量 24 条
    python scripts/eval_e2e.py --limit 5  # 调试用
"""
import argparse
import json
import os
import re
import statistics
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE, "data", "eval.db")
# 必须在 import app 之前设置，config 在导入时读取
os.environ["DATABASE_URL"] = "sqlite:///" + DB_PATH.replace("\\", "/")
sys.path.insert(0, BASE)

from app import create_app                      # noqa: E402
from app.extensions import db                   # noqa: E402
from app.models import (                        # noqa: E402
    FdeRecord, WorkItem, WorkOrder, Ledger, Report, AuditLog,
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# --------------------------------------------------------------------------
# 标注集：items 的 lines 决定期望单据
#   lines 含"物业"          → 1 张工单
#   lines 中每个非物业线     → 1 条台账
# kw 为关键字，命中即可匹配（任一命中，取命中最多者）
# --------------------------------------------------------------------------
CASES = [
    {"id": "C01", "text": "2栋2单元202室新租户李某，电话13800001234，这个月搬进来的",
     "items": [{"type": "人口变动", "lines": ["综治"], "kw": ["租户", "李某"]}],
     "pii": ["13800001234"]},

    {"id": "C02", "text": "7栋3单元电梯有异响，麻烦物业尽快检修，联系电话13900001111",
     "items": [{"type": "报修", "lines": ["物业"], "kw": ["电梯"]}],
     "pii": ["13900001111"]},

    {"id": "C03", "text": "2栋2单元202室租户李某要补办居住登记",
     "items": [{"type": "人口变动", "lines": ["综治"], "kw": ["居住登记"]}],
     "reuse": True},

    {"id": "C04", "text": "今天走访7栋独居老人王奶奶，她血压偏高，家里下水道也堵了",
     "items": [{"type": "走访", "lines": ["民政", "卫健"], "kw": ["王奶奶"]},
               {"type": "报修", "lines": ["物业"], "kw": ["下水道"]}]},

    {"id": "C05", "text": "10栋架空层有人私拉电线给电动车充电，另外6栋地下室群租问题还没处理",
     "items": [{"type": "隐患排查", "lines": ["综治"], "kw": ["私拉电线"]},
               {"type": "隐患排查", "lines": ["综治"], "kw": ["群租"]}]},

    {"id": "C06", "text": "12栋302新搬来一对小夫妻，男的是外地户口要办居住证；顺便说楼道声控灯不亮了",
     "items": [{"type": "人口变动", "lines": ["综治"], "kw": ["居住证", "小夫妻"]},
               {"type": "报修", "lines": ["物业"], "kw": ["声控灯"]}]},

    {"id": "C07", "text": "5栋2单元低保户张阿姨来问这个月低保金为什么还没到账",
     "items": [{"type": "咨询", "lines": ["民政"], "kw": ["低保金", "张阿姨"]}]},

    {"id": "C08", "text": "9栋门禁坏了，另外3栋楼道灯也坏了，都需要修",
     "items": [{"type": "报修", "lines": ["物业"], "kw": ["门禁"]},
               {"type": "报修", "lines": ["物业"], "kw": ["楼道灯"]}]},

    {"id": "C09", "text": "8栋2单元电梯坏了，老人下不了楼，麻烦尽快修",
     "items": [{"type": "报修", "lines": ["物业"], "kw": ["电梯"]}]},

    {"id": "C10", "text": "街道通知明天上午九点开会，要求上报本月消防检查台账",
     "items": [{"type": "其他", "lines": ["街道", "综治"], "kw": ["开会", "消防检查"]}]},

    {"id": "C11", "text": "6栋两户居民因为空调外机滴水吵起来了，需要上门调解",
     "items": [{"type": "调解", "lines": ["综治"], "kw": ["空调", "滴水"]}]},

    {"id": "C12", "text": "辖区精神障碍患者刘某最近情绪不稳，家属说药快吃完了",
     "items": [{"type": "走访", "lines": ["卫健"], "kw": ["刘某", "情绪"]}]},

    {"id": "C13", "text": "租户赵某身份证320102199003074512，电话13712345678，8月1日入住5栋601",
     "items": [{"type": "人口变动", "lines": ["综治"], "kw": ["赵某", "入住"]}],
     "pii": ["320102199003074512", "13712345678"]},

    {"id": "C14", "text": "居民来问高龄老人津贴怎么申请，需要准备哪些材料",
     "items": [{"type": "咨询", "lines": ["民政"], "kw": ["高龄", "津贴"]}]},

    {"id": "C15", "text": "2栋屋顶漏水，物业说上周就报过了还没修好",
     "items": [{"type": "报修", "lines": ["物业"], "kw": ["屋顶", "漏水"]}]},

    {"id": "C16", "text": "11栋电梯困人已经救出来了，需要检修；另外11栋消防通道堆了杂物；还有居民反映楼下广场舞噪音太大",
     "items": [{"type": "报修", "lines": ["物业"], "kw": ["电梯"]},
               {"type": "隐患排查", "lines": ["综治"], "kw": ["消防通道"]},
               {"type": "调解", "lines": ["综治"], "kw": ["广场舞", "噪音"]}]},

    {"id": "C17", "text": "低保户李大爷来问临时救助怎么申请",
     "items": [{"type": "咨询", "lines": ["民政"], "kw": ["临时救助", "李大爷"]}]},

    {"id": "C18", "text": "今天给3栋独居老人送餐，老人说膝盖疼得厉害",
     "items": [{"type": "走访", "lines": ["民政", "卫健"], "kw": ["送餐", "膝盖"]}]},

    {"id": "C19", "text": "15栋新开了一家民宿，疑似群租经营，消防设施也不达标",
     "items": [{"type": "隐患排查", "lines": ["综治"], "kw": ["民宿", "群租"]}]},

    {"id": "C20", "text": "居民反映7栋2单元楼道堆了很多纸箱，有火灾隐患",
     "items": [{"type": "隐患排查", "lines": ["综治"], "kw": ["纸箱", "楼道"]}]},

    {"id": "C21", "text": "街道要求各社区本周五前上报独居老人探访台账",
     "items": [{"type": "其他", "lines": ["街道", "民政"], "kw": ["探访台账"]}]},

    {"id": "C22", "text": "5栋601租户赵某要办理居住证签注",
     "items": [{"type": "人口变动", "lines": ["综治"], "kw": ["居住证", "赵某"]}],
     "reuse": True},

    {"id": "C23", "text": "1栋电梯坏了，同时2栋单元门禁也坏了，都要修",
     "items": [{"type": "报修", "lines": ["物业"], "kw": ["电梯"]},
               {"type": "报修", "lines": ["物业"], "kw": ["门禁"]}]},

    {"id": "C24", "text": "6栋3单元402住户反映水压太低，洗澡都成问题，电话13566667777",
     "items": [{"type": "报修", "lines": ["物业"], "kw": ["水压"]}],
     "pii": ["13566667777"]},
]

PLAINTEXT_RE = re.compile(r"1[3-9]\d{9}|\d{17}[\dXx]")


# --------------------------------------------------------------------------
# 匹配：期望 vs 实际，关键词命中 + 贪心
# --------------------------------------------------------------------------
def greedy_match(exp, act, text_of, group_ok=None):
    cand = []
    for i, e in enumerate(exp):
        for j, a in enumerate(act):
            if group_ok and not group_ok(e, a):
                continue
            txt = text_of(a) or ""
            hit = sum(1 for k in e["kw"] if k in txt)
            if hit:
                cand.append((hit, i, j))
    cand.sort(key=lambda x: (-x[0], x[1], x[2]))
    used_e, used_a, pairs = set(), set(), []
    for hit, i, j in cand:
        if i in used_e or j in used_a:
            continue
        used_e.add(i)
        used_a.add(j)
        pairs.append((i, j))
    fn = [e for i, e in enumerate(exp) if i not in used_e]
    fp = [a for j, a in enumerate(act) if j not in used_a]
    return pairs, fn, fp


def prf(tp, fp, fn):
    p = tp / (tp + fp) * 100 if tp + fp else 0.0
    r = tp / (tp + fn) * 100 if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return round(p, 1), round(r, 1), round(f, 1)


def pct(x):
    return f"{x * 100:.1f}%"


# --------------------------------------------------------------------------
# 库内明文残留扫描（保密指标的核心：脱敏是否真的落到库里）
# --------------------------------------------------------------------------
def scan_plaintext():
    """扫描所有可能存文本的表/列，找未脱敏的手机号与身份证。"""
    targets = [
        ("fde_record", "raw_text"),
        ("work_order", "content"),
        ("work_order", "feedback"),
        ("ledger", "content_json"),
        ("report", "data_json"),
        ("audit_log", "input_digest"),
        ("audit_log", "output_digest"),
    ]
    hits = []
    for table, col in targets:
        try:
            rows = db.session.execute(
                db.text(f"SELECT id, {col} AS v FROM {table}")
            ).fetchall()
        except Exception:
            continue
        for rid, v in rows:
            if not v:
                continue
            for m in PLAINTEXT_RE.findall(str(v)):
                hits.append({"table": table, "column": col, "id": rid, "value": m})
    return hits


def main(limit=None):
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    app = create_app("dev")
    with app.app_context():
        db.create_all()

    cases = CASES[:limit] if limit else CASES
    total_items_exp = sum(len(c["items"]) for c in cases)

    rows = []
    order_exp_all, order_act_all = [], []
    ledger_exp_all, ledger_act_all = [], []
    order_tp = order_fp = order_fn = 0
    ledger_tp = ledger_fp = ledger_fn = 0
    item_tp = item_fp = item_fn = 0
    item_type_ok = 0
    pii_total = pii_leaked = 0
    pii_found = pii_missed = 0
    costs = []
    reuse_expected = reuse_hit = 0
    ledger_dup = 0
    extra_from_split = 0    # 多出的台账源于过度拆分（真实误差）
    extra_cross_line = 0    # 多出的台账源于一个事项落多条线（设计允许）
    exp_docs_total = 0      # 人工基线的单据数（按标注算，不用系统实际产出）

    # 登录（社区工作者 + 物业）
    c = app.test_client()
    c.post("/login", data={"username": "wang", "password": "123456"})
    pw = app.test_client()
    pw.post("/login", data={"username": "wuye", "password": "123456"})

    print(f"端到端评测启动：{len(cases)} 条语料 / 期望业务项 {total_items_exp} 个")
    print("-" * 78)

    for idx, case in enumerate(cases, 1):
        t0 = time.perf_counter()
        resp = c.post("/api/fde", json={"raw_text": case["text"]})
        cost = time.perf_counter() - t0
        costs.append(cost)

        if resp.status_code != 200:
            print(f"[{case['id']}] HTTP {resp.status_code} 失败")
            continue
        j = resp.get_json()
        if j.get("code") != 0:
            print(f"[{case['id']}] 业务失败：{j.get('msg')}")
            continue
        d = j["data"]
        items, orders, ledgers = d["items"], d.get("orders", []), d.get("ledgers", [])

        # ---- 业务项匹配 ----
        exp_items = case["items"]
        pairs, fn, fp = greedy_match(
            exp_items, items,
            lambda it: f"{it.get('type','')} {it.get('subject','')}")
        item_tp += len(pairs)
        item_fn += len(fn)
        item_fp += len(fp)
        type_ok = sum(1 for i, jj in pairs if items[jj].get("type") == exp_items[i]["type"])
        item_type_ok += type_ok

        # ---- 期望单据 ----
        exp_orders, exp_ledgers = [], []
        for it in exp_items:
            for ln in it["lines"]:
                if ln == "物业":
                    exp_orders.append({"kw": it["kw"]})
                else:
                    exp_ledgers.append({"line": ln, "kw": it["kw"]})

        op, ofn, ofp = greedy_match(exp_orders, orders, lambda o: o.get("content", ""))
        exp_docs_total += len(exp_orders) + len(exp_ledgers)
        lp, lfn, lfp = greedy_match(
            exp_ledgers, ledgers,
            lambda l: f"{l.get('subject','')} {l.get('line','')}",
            group_ok=lambda e, a: e["line"] == a.get("line"))

        order_tp += len(op); order_fn += len(ofn); order_fp += len(ofp)
        ledger_tp += len(lp); ledger_fn += len(lfn); ledger_fp += len(lfp)

        # ---- 同 FDE 内重复建档（同 line 同 subject 出现两次以上）----
        seen = set()
        for l in ledgers:
            key = (l.get("line"), (l.get("subject") or "").strip())
            if key in seen:
                ledger_dup += 1
            seen.add(key)

        # ---- 多出台账的归因 ----
        # 业务项本身拆多了 → 拆解误差传导（真误差）
        # 业务项数正确但仍多台账 → 一个事项落了多条业务线（设计允许，非重复）
        if len(ledgers) > len(exp_ledgers):
            diff = len(ledgers) - len(exp_ledgers)
            if len(items) > len(exp_items):
                extra_from_split += diff
            else:
                extra_cross_line += diff

        # ---- 复用命中 ----
        if case.get("reuse"):
            reuse_expected += 1
            if any(l.get("reused") for l in ledgers):
                reuse_hit += 1

        # ---- 保密：明文是否还在返回文本里 ----
        masked = d.get("masked_text", "")
        findings = d.get("sensitive_findings", [])
        masked_vals = [f.get("value_masked", "") for f in findings]
        for v in case.get("pii", []):
            pii_total += 1
            if v in masked:
                pii_leaked += 1
            if any(v[:3] in mv and v[-4:] in mv for mv in masked_vals):
                pii_found += 1
            else:
                pii_missed += 1

        # ---- 工单闭环 ----
        closed = 0
        for o in orders:
            if pw.post(f"/api/orders/{o['id']}/accept").get_json().get("code") == 0 \
               and pw.post(f"/api/orders/{o['id']}/finish",
                           json={"feedback": "已现场处理完毕"}).get_json().get("code") == 0:
                closed += 1

        rows.append({
            "id": case["id"], "text": case["text"], "cost": round(cost, 2),
            "items": [f"{i['type']}:{i['subject']}" for i in items],
            "item_lines": [i.get("target_lines", []) for i in items],
            "act_ledger_lines": [l.get("line") for l in ledgers],
            "exp_orders": len(exp_orders), "act_orders": len(orders),
            "exp_ledgers": len(exp_ledgers), "act_ledgers": len(ledgers),
            "order_ok": len(op), "order_miss": len(ofn), "order_extra": len(ofp),
            "ledger_ok": len(lp), "ledger_miss": len(lfn), "ledger_extra": len(lfp),
            "closed": closed,
            "reused": sum(1 for l in ledgers if l.get("reused")),
        })
        flag = "" if (not ofn and not ofp and not lfn and not lfp) else "  <-- 单据有偏差"
        print(f"[{case['id']}] {cost:.2f}s 业务项{len(items)}/{len(exp_items)} "
              f"工单{len(orders)}/{len(exp_orders)} 台账{len(ledgers)}/{len(exp_ledgers)}"
              f"{flag}")

    # ---- 汇总 ----
    with app.app_context():
        fde_n = FdeRecord.query.count()
        item_n = WorkItem.query.count()
        order_n = WorkOrder.query.count()
        ledger_n = Ledger.query.count()
        done_n = WorkOrder.query.filter_by(status="done").count()
        # 复用次数读 Ledger.reused 列（与台账页、报表同一个统计源），
        # 不要再解析 content_json——三处各扫一遍，口径迟早会漂
        reuse_n = Ledger.query.filter(Ledger.reused.is_(True)).count()
        orphan_sql = """
            SELECT COUNT(*) FROM work_item w
            WHERE NOT EXISTS (SELECT 1 FROM work_order o WHERE o.work_item_id = w.id)
              AND NOT EXISTS (SELECT 1 FROM ledger l WHERE l.work_item_id = w.id)
        """
        orphan_n = db.session.execute(db.text(orphan_sql)).scalar() or 0
        plain = scan_plaintext()
        audit_by_action = dict(db.session.execute(db.text(
            "SELECT action, COUNT(*) FROM audit_log GROUP BY action"
        )).fetchall())
        audit_total = AuditLog.query.count()

    # 报表一致性
    rep = c.post("/api/reports/generate", json={"period": "2026-10"}).get_json()
    rep_data = rep.get("data", {}) if rep.get("code") == 0 else {}

    op_, orr_, of_ = prf(order_tp, order_fp, order_fn)
    lp_, lr_, lf_ = prf(ledger_tp, ledger_fp, ledger_fn)
    ip_, ir_, if_ = prf(item_tp, item_fp, item_fn)

    costs_sorted = sorted(costs)
    def pctl(p):
        if not costs_sorted:
            return 0
        k = min(len(costs_sorted) - 1, int(round((len(costs_sorted) - 1) * p)))
        return costs_sorted[k]

    metrics = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cases": len(cases),
        "fde_count": fde_n,
        "item_count": item_n,
        "order_count": order_n,
        "ledger_count": ledger_n,
        "items": {"tp": item_tp, "fp": item_fp, "fn": item_fn,
                  "p": ip_, "r": ir_, "f1": if_,
                  "type_acc": round(item_type_ok / item_tp * 100, 1) if item_tp else 0},
        "orders": {"tp": order_tp, "fp": order_fp, "fn": order_fn,
                   "p": op_, "r": orr_, "f1": of_,
                   "miss_rate": round(order_fn / (order_tp + order_fn) * 100, 1) if (order_tp + order_fn) else 0,
                   "extra_rate": round(order_fp / (order_tp + order_fp) * 100, 1) if (order_tp + order_fp) else 0},
        "ledgers": {"tp": ledger_tp, "fp": ledger_fp, "fn": ledger_fn,
                    "p": lp_, "r": lr_, "f1": lf_,
                    "miss_rate": round(ledger_fn / (ledger_tp + ledger_fn) * 100, 1) if (ledger_tp + ledger_fn) else 0,
                    "extra_rate": round(ledger_fp / (ledger_tp + ledger_fp) * 100, 1) if (ledger_tp + ledger_fp) else 0,
                    "dup_in_fde": ledger_dup,
                    "extra_from_split": extra_from_split,
                    "extra_cross_line": extra_cross_line},
        "privacy": {
            "pii_total": pii_total,
            "recall": round(pii_found / pii_total * 100, 1) if pii_total else 0,
            "leak_in_masked": pii_leaked,
            "db_plaintext_hits": len(plain),
            "db_plaintext_detail": plain[:10],
            "reuse_expected": reuse_expected,
            "reuse_hit": reuse_hit,
            "reuse_rate": round(reuse_hit / reuse_expected * 100, 1) if reuse_expected else 0,
            "reuse_count": reuse_n,
        },
        "closure": {
            "orders": order_n, "done": done_n,
            "rate": round(done_n / order_n * 100, 1) if order_n else 0,
        },
        "consistency": {
            "orphan_items": orphan_n,
            "orphan_rate": round(orphan_n / item_n * 100, 1) if item_n else 0,
            "audit_total": audit_total,
            "audit_per_fde": round(audit_total / fde_n, 1) if fde_n else 0,
            "audit_by_action": audit_by_action,
        },
        "latency": {
            "mean": round(statistics.mean(costs), 2) if costs else 0,
            "p50": round(pctl(0.50), 2),
            "p95": round(pctl(0.95), 2),
            "max": round(max(costs), 2) if costs else 0,
        },
        "report_check": {
            "report_fde": rep_data.get("fde_count"),
            "report_item": rep_data.get("item_count"),
            "report_order": (rep_data.get("orders") or {}).get("total"),
            "match": (rep_data.get("fde_count") == fde_n
                      and rep_data.get("item_count") == item_n
                      and (rep_data.get("orders") or {}).get("total") == order_n),
        },
        "rows": rows,
    }

    # 减负结构性口径（可计算，不含访谈估算）
    # 人工基线用「标注期望单据数」，不用系统实际产出：
    # 否则系统多建的台账也会被算成人工本来要做的事，虚增减负幅度。
    doc_n = order_n + ledger_n
    manual_base = fde_n + exp_docs_total
    metrics["workload"] = {
        "expected_docs": exp_docs_total,
        "actual_docs": doc_n,
        "manual_entries": manual_base,
        "system_entries": fde_n,
        "reduction": round(exp_docs_total / manual_base * 100, 1) if manual_base else 0,
        "reduction_by_actual": round(doc_n / (fde_n + doc_n) * 100, 1) if (fde_n + doc_n) else 0,
        "amplification": round(item_n / fde_n, 2) if fde_n else 0,
        "docs_per_fde": round(doc_n / fde_n, 2) if fde_n else 0,
    }

    out = os.path.join(BASE, "data", "e2e_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)

    print("-" * 78)
    print(f"首录 {fde_n} 条 → 业务项 {item_n} 个 → 工单 {order_n} + 台账 {ledger_n}")
    print(f"业务项  P{ip_} R{ir_} F1 {if_}   类型准确率 {metrics['items']['type_acc']}%")
    print(f"工单    P{op_} R{orr_} F1 {of_}   漏派 {metrics['orders']['miss_rate']}% 误派 {metrics['orders']['extra_rate']}%")
    print(f"台账    P{lp_} R{lr_} F1 {lf_}   漏建 {metrics['ledgers']['miss_rate']}% 多建 {metrics['ledgers']['extra_rate']}%"
          f"（拆解传导 {extra_from_split} / 跨线分派 {extra_cross_line} / 真重复 {ledger_dup}）")
    print(f"保密    召回 {metrics['privacy']['recall']}%  返回文本泄露 {pii_leaked} 处  "
          f"库内明文 {len(plain)} 处")
    print(f"闭环    {done_n}/{order_n} = {metrics['closure']['rate']}%   "
          f"孤儿业务项 {orphan_n} ({metrics['consistency']['orphan_rate']}%)")
    print(f"耗时    均值 {metrics['latency']['mean']}s  P50 {metrics['latency']['p50']}s  "
          f"P95 {metrics['latency']['p95']}s  最大 {metrics['latency']['max']}s")
    print(f"复用    {metrics['privacy']['reuse_hit']}/{reuse_expected} 场景命中，"
          f"库内复用标记 {reuse_n} 条")
    print(f"报表一致：{metrics['report_check']['match']}  "
          f"（报表 {metrics['report_check']['report_fde']}/{metrics['report_check']['report_item']}/"
          f"{metrics['report_check']['report_order']} vs "
          f"实际 {fde_n}/{item_n}/{order_n}）")
    print(f"审计    {audit_total} 条，平均 {metrics['consistency']['audit_per_fde']} 条/FDE")
    print(f"减负口径 人工需录 {metrics['workload']['manual_entries']} 次（首录 {fde_n} + 期望单据 "
          f"{exp_docs_total}）→ 系统 {fde_n} 次，减少 {metrics['workload']['reduction']}%")
    print(f"\n明细已写入 {out}")
    return metrics


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    main(ap.parse_args().limit)
