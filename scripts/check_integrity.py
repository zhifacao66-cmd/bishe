"""数据层自洽检查（只读，不改库）

代码逻辑对了，不代表**库里已有的数据**也自洽——早先的缺陷可能已经写进了
状态字段、检索文本、报表版本号。这里按"同一事实两处存放"的地方逐项比对。

运行：python scripts/check_integrity.py（只读，检查的是副本）
"""
import os
import pathlib
import shutil
import sys
import tempfile

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# 用副本检查：`_ticket_payload` 会顺手把单据状态写回（它本来就是"读的时候
# 顺手对齐状态"），直接在演示库上跑等于用检查动作改了被检查的对象。
# 副本放在系统临时目录：放 data/ 会跟着项目一起被同步/打包，还得自己清理。
SRC = BASE / "data" / "community.db"
DB = pathlib.Path(tempfile.mkdtemp(prefix="integrity_")) / "community.db"
for suf in ("", "-wal", "-shm"):
    s_ = pathlib.Path(str(SRC) + suf)
    if s_.exists():
        shutil.copy2(s_, str(DB) + suf)
os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                                  # noqa: E402
from app.extensions import db                               # noqa: E402
from app.models import (FdeRecord, FieldSecurity, Ledger, Notice,  # noqa: E402
                        Report, ReportRevision, ResidentTicket, Task,
                        WorkItem, WorkOrder, WorkOrderLog)
from app.search import content_text                         # noqa: E402

app = create_app("dev")
issues = []


def report(label, bad, sample=""):
    flag = "OK  " if not bad else "FAIL"
    print(f"  [{flag}] {label}" + (f"  {sample}" if bad else ""))
    if bad:
        issues.append(label)


with app.app_context():
    print("数据层自洽检查 ·", DB.name)

    # 1. 台账检索文本与正文是否同步（ORM 事件维护，绕过 ORM 的写入会漂）
    stale = []
    for lg in Ledger.query.all():
        want = content_text(lg.content_json) if lg.content_json else None
        if (lg.search_text or None) != (want or None):
            stale.append(lg.id)
    report("ledger.search_text 与 content_json 一致", stale[:8], f"{len(stale)} 条不一致")

    # 2. 工单当前状态与流转链最后一步是否一致
    mismatch = []
    for o in WorkOrder.query.all():
        last = (WorkOrderLog.query.filter_by(order_id=o.id)
                .order_by(WorkOrderLog.created_at.desc(), WorkOrderLog.id.desc())
                .first())
        if last and last.to_status and last.to_status != o.status:
            mismatch.append((o.order_no, o.status, last.to_status))
        if not last:
            mismatch.append((o.order_no, o.status, "无流转记录"))
    report("工单状态与流转链末步一致", mismatch[:5], f"{len(mismatch)} 张不一致")

    # 3. 已办结的工单必须有办结时间（否则"处理时长"永远算不出来）
    bad_done = [o.order_no for o in WorkOrder.query.filter_by(status="done").all()
                if not o.done_at]
    report("已办结工单有 done_at", bad_done[:5], f"{len(bad_done)} 张缺办结时间")

    # 4. 置顶必须带置顶时间（否则排序退化）
    bad_pin = [n.id for n in Notice.query.filter(Notice.pinned.is_(True)).all()
               if not n.pinned_at]
    report("置顶公告有 pinned_at", bad_pin[:5], f"{len(bad_pin)} 条缺置顶时间")

    # 5. 已签收必须有签收人/时间
    bad_recv = [n.id for n in Notice.query.filter_by(recv_status="received").all()
                if not (n.received_at and n.received_by)]
    report("已签收公告有签收人与时间", bad_recv[:5], f"{len(bad_recv)} 条缺")

    # 6. 任务完成必须有 closed_at；未完成的不能有 closed_at
    bad_task = [t.id for t in Task.query.all()
                if (t.status == "done") != bool(t.closed_at)]
    report("任务状态与 closed_at 对应", bad_task[:5], f"{len(bad_task)} 条不一致")

    # 7. 报表版本号与历史表可对上（版本 N 应有 N-1 份历史，回滚另计）
    bad_rep = []
    for r in Report.query.all():
        arch = ReportRevision.query.filter_by(report_id=r.id).count()
        if int(r.version or 1) - 1 != arch and not r.rollback_from:
            bad_rep.append((r.period, r.version, arch))
    report("报表版本号与历史份数匹配", bad_rep[:5], f"{len(bad_rep)} 份不匹配")

    # 8. 居民单状态与业务项推进情况一致
    from app.api.portal import _ticket_payload
    bad_t = []
    for t in ResidentTicket.query.all():
        before = t.status
        want = _ticket_payload(t)["status"]
        if before != want:
            bad_t.append((t.id, before, want))
    report("居民单状态与明细一致", bad_t[:5], f"{len(bad_t)} 单不一致")

    # ---- 以下是"模块之间的链有没有断"：外键指向的对象还在不在 ----
    # 这些行不会让页面报错，只会让某个模块凭空少几条：工单中心还是那个数，
    # 点进去却找不到对应的业务项；报表按业务项汇总，孤儿工单永远不会被统计到。

    wi_ids = {i for (i,) in db.session.query(WorkItem.id).all()}

    # 9. 工单 → 业务项
    orphan_ord = [o.order_no for o in WorkOrder.query.all()
                  if o.work_item_id not in wi_ids]
    report("工单都挂在存在的业务项上", orphan_ord[:5], f"{len(orphan_ord)} 张孤儿工单")

    # 10. 台账 → 业务项
    orphan_lg = [l.id for l in Ledger.query.all() if l.work_item_id not in wi_ids]
    report("台账都挂在存在的业务项上", orphan_lg[:5], f"{len(orphan_lg)} 条孤儿台账")

    # 11. 字段密级 → 业务项（密级没了就等于敏感字段失去管控依据）
    orphan_sec = [f.id for f in FieldSecurity.query.all()
                  if f.work_item_id not in wi_ids]
    report("字段密级都挂在存在的业务项上", orphan_sec[:5], f"{len(orphan_sec)} 条孤儿密级")

    # 12. 居民单 → 首录原文（居民凭查询码查进度，断链就是"查无此单"）
    fde_ids = {i for (i,) in db.session.query(FdeRecord.id).all()}
    orphan_t = [t.id for t in ResidentTicket.query.all()
                if t.fde_id and t.fde_id not in fde_ids]
    report("居民单都能追到首录原文", orphan_t[:5], f"{len(orphan_t)} 单断链")

    # 13. 居民单上的 item_count / line_summary 是**拆解当时的快照**，
    #     与实际拆出的业务项必须对得上：居民端进度条靠它显示"几件事、涉及谁"。
    #     对不上时不会报错，只会少显示一件事或漏一条业务线。
    bad_cnt, bad_line = [], []
    for t in ResidentTicket.query.filter(ResidentTicket.fde_id.isnot(None)).all():
        items = WorkItem.query.filter_by(fde_id=t.fde_id).all()
        if int(t.item_count or 0) != len(items):
            bad_cnt.append((t.id, t.item_count, len(items)))
        want = {l for i in items for l in (i.target_lines or "").split(",") if l}
        got = {x for x in (t.line_summary or "").split("/") if x}
        if want != got:
            bad_line.append((t.id, sorted(got - want), sorted(want - got)))
    report("居民单事项数与实际拆解一致", bad_cnt[:5], f"{len(bad_cnt)} 单对不上")
    report("居民单业务线与实际去向一致", bad_line[:5], f"{len(bad_line)} 单对不上")

    # 14. 台账的业务线必须属于其业务项的去向
    #     填报 Agent 按 target_lines 写台账，写错线会让"民政的件进了综治台账"，
    #     报表按线统计时两边都错，但单看哪一页都正常。
    bad_ln = []
    wi_lines = {i.id: set((i.target_lines or "").split(",")) for i in
                db.session.query(WorkItem.id, WorkItem.target_lines)}
    wi_lines = {k: {x for x in v if x} for k, v in wi_lines.items()}
    for lg in Ledger.query.all():
        allowed = wi_lines.get(lg.work_item_id)
        if allowed is not None and lg.line and lg.line not in allowed:
            bad_ln.append((lg.id, lg.line, sorted(allowed)))
    report("台账业务线属于该事项去向", bad_ln[:5], f"{len(bad_ln)} 条越线")

    # 15. 报表快照的关键字段（导出件只读快照，缺字段会导出成一片 0）
    import json
    bad_rk = []
    for r in Report.query.all():
        try:
            d = json.loads(r.data_json or "{}")
        except ValueError:
            bad_rk.append((r.period, "JSON 解析失败"))
            continue
        miss = [k for k in ("period", "fde_count", "item_count", "line_counts")
                if k not in d]
        if miss:
            bad_rk.append((r.period, ",".join(miss)))
    report("报表快照含关键字段", bad_rk[:5], f"{len(bad_rk)} 份缺字段")

    print()
    if issues:
        print("发现 %d 类不一致（上面已逐项标出）" % len(issues))
    else:
        print("全部自洽：没有发现跨表/跨字段不一致")

