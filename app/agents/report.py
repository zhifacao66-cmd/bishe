"""报表Agent：按周期汇总 → 生成街道上报报表（脱敏汇总，不含居民明文）。
v1 模板化摘要；LLM 部署后可升级为自然语言润色。

统计口径按 period（YYYY-MM）**闭开区间**过滤，只统计该自然月内产生的数据。
这一点很重要：报表若统计全表累计值，则 9 月报表与 10 月报表数字完全一样，
"月报"只是换了个标题，街道拿去做环比就失真了。

生成时一次性把数字和明细都写进 data_json（快照），此后无论导出多少次、
隔多久导出，看到的都是同一份数——报表是口径证据，不能每次打开都变。
"""
import json
from calendar import monthrange
from collections import Counter
from datetime import datetime

from app.agents.base import BaseAgent
from app.agents.supervise import SuperviseAgent
from app.extensions import db
from app.models import Report, WorkItem, WorkOrder, Ledger, FdeRecord, AuditLog
from app.report.export import MAX_DETAIL
from app.report.revisions import archive


def _month_range(period: str):
    """'2026-10' → (2026-10-01 00:00, 2026-11-01 00:00)；格式非法则退回当月"""
    try:
        y, m = (int(x) for x in period.split("-")[:2])
        if not 1 <= m <= 12:
            raise ValueError
    except (ValueError, AttributeError):
        now = datetime.now()
        y, m = now.year, now.month
        period = f"{y}-{m:02d}"
    start = datetime(y, m, 1)
    end = datetime(y + (m == 12), (m % 12) + 1, 1)
    return period, start, end


class ReportAgent(BaseAgent):
    name = "report"

    def run(self, payload: dict) -> dict:
        raw_period = payload.get("period") or datetime.now().strftime("%Y-%m")
        period, start, end = _month_range(raw_period)
        prev_period, pstart, _ = _month_range(
            f"{start.year - 1}-12" if start.month == 1 else f"{start.year}-{start.month - 1:02d}"
        )

        # 统一按窗口过滤：本期 / 上期，用于环比
        def scope(t0, t1):
            return dict(
                fde=FdeRecord.query.filter(FdeRecord.created_at >= t0,
                                           FdeRecord.created_at < t1).count(),
                items=WorkItem.query.filter(WorkItem.created_at >= t0,
                                            WorkItem.created_at < t1).all(),
                orders=WorkOrder.query.filter(WorkOrder.created_at >= t0,
                                              WorkOrder.created_at < t1).all(),
                ledgers=Ledger.query.filter(Ledger.created_at >= t0,
                                            Ledger.created_at < t1).all(),
            )

        cur = scope(start, end)
        prev = scope(pstart, start)

        line_cnt = Counter()
        for it in cur["items"]:
            for ln in (it.target_lines or "").split(","):
                if ln:
                    line_cnt[ln] += 1
        type_cnt = Counter(it.type for it in cur["items"] if it.type)
        order_stat = Counter(o.status for o in cur["orders"])
        done = order_stat.get("done", 0)
        total_orders = len(cur["orders"])
        done_rate = round(done / total_orders * 100) if total_orders else 0
        # 与台账页同一个统计源（Ledger.reused 列），不再各自扫 JSON——
        # 两处口径分开算，迟早出现"报表说复用 23 次、台账页说 22 次"
        reuse_cnt = sum(1 for lg in cur["ledgers"] if lg.reused)
        sensitive_cnt = AuditLog.query.filter(
            AuditLog.agent == "security",
            AuditLog.created_at >= start, AuditLog.created_at < end,
        ).count()
        # 超期件数：与督办模块同一判据，避免"督办说 3 件超期、月报说 0 件"
        overdue_cnt = sum(1 for o in cur["orders"] if SuperviseAgent.is_overdue(o))

        # 业务项明细随报表冻结：导出 Excel 的明细页只读这份快照，
        # 不回头查库（否则导出的数字与报表当日上报的数字可能不一致）。
        # 只留非敏感字段，事项摘要本就是派发给物业的那一句。
        rows = sorted(cur["items"], key=lambda x: (x.created_at or datetime.min, x.id))
        detail = [{
            "type": it.type, "subject": it.subject,
            "line": it.target_lines or "",
            "created": it.created_at.strftime("%Y-%m-%d %H:%M") if it.created_at else "",
        } for it in rows[:MAX_DETAIL]]

        def delta(cur_v, prev_v):
            if not prev_v:
                return "上月无数据" if not cur_v else "新增"
            pct = (cur_v - prev_v) / prev_v * 100
            return f"环比{pct:+.0f}%"

        summary = (
            f"{period} 社区治理事项汇总：本期首录 {cur['fde']} 条（{delta(cur['fde'], prev['fde'])}），"
            f"拆解业务项 {len(cur['items'])} 个，覆盖 {len(line_cnt)} 条业务线；"
            f"物业工单 {total_orders} 件（办结 {done} 件，办结率 {done_rate}%"
            + (f"，超期未办结 {overdue_cnt} 件，已列入督办" if overdue_cnt else "")
            + "）；"
            f"复用历史档案 {reuse_cnt} 次，避免重复填报；"
            f"敏感信息脱敏处理 {sensitive_cnt} 批次，全程本地模型处理，无外网传输。"
        )

        data = {
            "period": period,
            "range": [start.strftime("%Y-%m-%d"),
                      f"{start.year}-{start.month:02d}-{monthrange(start.year, start.month)[1]:02d}"],
            "org": (payload.get("org") or "").strip(),
            "fde_count": cur["fde"], "item_count": len(cur["items"]),
            "line_counts": dict(line_cnt), "type_counts": dict(type_cnt),
            "orders": {"total": total_orders, **dict(order_stat)},
            "overdue_count": overdue_cnt,
            "reuse_count": reuse_cnt, "sensitive_count": sensitive_cnt,
            "item_rows": detail,
            "item_rows_truncated": max(0, len(cur["items"]) - MAX_DETAIL),
            "prev": {"period": prev_period, "fde_count": prev["fde"],
                     "item_count": len(prev["items"]),
                     "order_total": len(prev["orders"])},
            "summary": summary,
        }
        # 一期一份：同 (period, line) 已有报表就覆盖升版本，不新增记录。
        # 否则每点一次"生成"就多一份同月报表，列表里 14 条一模一样的 2026-10，
        # 导出件还各带不同编号，归档件就不可信了。
        rep = (Report.query.filter_by(period=period, line="街道")
               .order_by(Report.id.desc()).first())
        regenerated = rep is not None
        actor = (payload.get("actor") or "")[:32]
        if regenerated:
            # 覆盖前先把旧版本整份挪进历史表。这一步在覆盖之后做就没有意义了
            # （旧内容已经物理消失），而且不会报错——静默丢数据最难查。
            archive(rep, "regenerate", actor)
            rep.version = int(rep.version or 1) + 1
            rep.data_json = json.dumps(data, ensure_ascii=False)
            rep.generated_at = datetime.now()
            rep.rollback_from = None      # 重新生成是真·新数据，不再"抄某一版"
        else:
            rep = Report(period=period, line="街道", version=1,
                         data_json=json.dumps(data, ensure_ascii=False))
            db.session.add(rep)
        db.session.flush()
        self._audit("regenerate_report" if regenerated else "generate_report",
                    f"{period} 第 {rep.version} 版", summary[:200])
        db.session.commit()
        data["report_id"] = rep.id
        data["version"] = int(rep.version or 1)
        data["regenerated"] = regenerated
        data["generated_at"] = (rep.generated_at or datetime.now()).strftime("%Y-%m-%d %H:%M")
        return data
