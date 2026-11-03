"""报表接口：生成 / 列表 / 详情 / 版本历史 / 回滚

详情不返回 `item_rows`（导出用的明细快照，最多 300 行）：
页面只要汇总，把几百行明细塞进 JSON 只会让"打开报表"变慢。
导出走 `/reports/<id>/export/xlsx`，由服务端读快照排版。

版本历史与回滚见 `app/report/revisions.py`：`report` 一行一期（归档件唯一），
被替掉的每一版整份存在 `report_revision`（只追加）。两件事因此同时成立——
列表里没有两份同月报表，历史上每一版又都还在。
"""
import json
from flask import Blueprint, request
from app.api import ok, fail
from app.auth import current_user, perm_required
from app.models import Report
from app.agents.report import ReportAgent
from app.report.revisions import history, rollback

bp = Blueprint("reports", __name__)
agent = ReportAgent()

# 只给导出用的字段，接口响应里剔除
EXPORT_ONLY = ("item_rows",)


def public(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in EXPORT_ONLY}


@bp.post("/generate")
@perm_required("report.generate")
def generate():
    body = request.get_json(silent=True) or {}
    period = (body.get("period") or "").strip()
    u = current_user()
    # actor 传进去是为了让历史版本记得住"谁把它盖了"，
    # 审计里的 actor=system 只能说明"报表 Agent 干的"，指不到人。
    data = agent.run({"period": period or None,
                      "org": u.org if u else "",
                      "actor": u.name if u else ""})
    return ok(public(data))


@bp.get("")
@perm_required("report.view")
def list_reports():
    reps = Report.query.order_by(Report.period.desc(), Report.generated_at.desc()).limit(30).all()
    return ok([{
        "id": r.id, "period": r.period, "line": r.line,
        "version": int(r.version or 1),
        "rollback_from": int(r.rollback_from) if r.rollback_from else None,
        "generated_at": r.generated_at.strftime("%Y-%m-%d %H:%M"),
        "summary": json.loads(r.data_json).get("summary", ""),
    } for r in reps])


@bp.get("/<int:rid>")
@perm_required("report.view")
def detail(rid):
    r = Report.query.get_or_404(rid)
    data = json.loads(r.data_json)
    data["report_id"] = r.id
    data["version"] = int(r.version or 1)
    data["rollback_from"] = int(r.rollback_from) if r.rollback_from else None
    data["generated_at"] = r.generated_at.strftime("%Y-%m-%d %H:%M")
    return ok(public(data))


@bp.get("/<int:rid>/versions")
@perm_required("report.view")
def versions(rid):
    """当前版 + 全部历史版，各带摘要前 60 字，便于肉眼比对差在哪。"""
    r = Report.query.get_or_404(rid)
    return ok(history(r))


@bp.post("/<int:rid>/rollback")
@perm_required("report.generate")
def do_rollback(rid):
    """回滚到第 N 版；不传 version 即回上一版。

    写操作，所以挂 `report.generate` 而不是 `report.view`——
    街道能读能导出，但改归档件必须由有生成权的人来做。
    """
    r = Report.query.get_or_404(rid)
    body = request.get_json(silent=True) or {}
    raw = body.get("version")
    if raw in (None, ""):
        target = int(r.version or 1) - 1
    else:
        try:
            target = int(raw)
        except (TypeError, ValueError):
            return fail("version 必须是整数")
    u = current_user()
    done, msg = rollback(r, target, actor=u.name if u else "")
    if not done:
        return fail(msg)
    data = json.loads(r.data_json)
    data.update({
        "report_id": r.id,
        "version": int(r.version or 1),
        "rollback_from": int(r.rollback_from) if r.rollback_from else None,
        "generated_at": r.generated_at.strftime("%Y-%m-%d %H:%M"),
        "msg": msg,
    })
    return ok(public(data))
