"""周报接口

生成流程里有一条顺序不能颠倒：
    取数（只读）→ 结束事务 → 调模型润色 → 写库
模型调用绝不能放在数据库事务里（docs/26 红线 11）：SQLite 一个写事务
持锁期间调 8B 模型要 2 秒起，并发时别的请求会排队到 busy_timeout 超时。
"""
import json
from datetime import datetime

from flask import Blueprint, request
from sqlalchemy import func

from app.api import fail, ok
from app.auth import current_user, perm_required
from app.extensions import db
from app.models import AuditLog, Weekly
from app.weekly import (apply_polish, collect, current_period, parse_period, period_label,
                        polish, render)

bp = Blueprint("weekly", __name__)


def _audit(action: str, detail: str):
    u = current_user()
    db.session.add(AuditLog(actor=u.name if u else "system", agent="weekly",
                            action=action, input_digest=detail[:250],
                            created_at=datetime.now()))


def _row(w: Weekly, with_body: bool = True) -> dict:
    d = {
        "id": w.id,
        "period": w.period,
        "range": period_label(w.start_date, w.end_date),
        "status": w.status,
        "revision": w.revision or 1,
        "editor": w.editor or "",
        "signed_by": w.signed_by or "",
        "signed_at": w.signed_at.strftime("%Y-%m-%d %H:%M") if w.signed_at else "",
        "created_at": w.created_at.strftime("%Y-%m-%d %H:%M") if w.created_at else "",
    }
    if with_body:
        d["body_md"] = w.body_md or ""
        try:
            d["data"] = json.loads(w.data_json or "{}")
        except (ValueError, TypeError):
            d["data"] = {}
    return d


@bp.get("")
@perm_required("weekly.view")
def index():
    period = (request.args.get("period") or current_period()).strip()
    rows = (Weekly.query.filter_by(period=period)
            .order_by(Weekly.revision.desc()).all())
    history = [{"period": p, "count": n}
               for p, n in (db.session.query(Weekly.period, func.count(Weekly.id))
                            .group_by(Weekly.period)
                            .order_by(Weekly.period.desc()).limit(12).all())]
    return ok({
        "period": period,
        "items": [_row(w, with_body=False) for w in rows],
        "history": history,
    })


@bp.post("/generate")
@perm_required("weekly.manage")
def generate():
    data = request.get_json(silent=True) or {}
    period = (data.get("period") or current_period()).strip()
    start, end = parse_period(period)
    use_llm = bool(data.get("polish"))

    # 已签发的不动：周报是"报出去的东西"，重新生成会把报出去的版本悄悄换掉
    signed = (Weekly.query.filter_by(period=period, status="final")
              .order_by(Weekly.revision.desc()).first())
    if signed and not data.get("force"):
        return fail(f"{period} 的周报已签发（第 {signed.revision} 版），"
                    "如需修改请点「出新修订版」"), 409
    signed_rev = signed.revision if signed else 0

    payload = collect(start, end)
    # 结束只读事务再调模型（红线 11）。rollback 会让上面查到的对象过期，
    # 所以 draft 必须在这之后再取，否则拿到的是失效实例。
    db.session.rollback()

    body = render(payload)
    if use_llm:
        body = apply_polish(body, polish(payload))

    draft = Weekly.query.filter_by(period=period, status="draft").first()
    u = current_user()
    if draft:
        draft.start_date, draft.end_date = start, end
        draft.data_json, draft.body_md = json.dumps(payload, ensure_ascii=False), body
        draft.editor = u.name if u else ""
        w = draft
    else:
        w = Weekly(period=period, start_date=start, end_date=end,
                   data_json=json.dumps(payload, ensure_ascii=False), body_md=body,
                   status="draft", editor=(u.name if u else ""),
                   revision=(signed_rev + 1) if signed_rev else 1)
        db.session.add(w)
    _audit("weekly_generate", f"生成周报 {period}（{'含模型起草' if use_llm else '模板成稿'}）")
    db.session.commit()
    return ok(_row(w))


@bp.get("/<int:wid>")
@perm_required("weekly.view")
def detail(wid):
    w = db.session.get(Weekly, wid)
    if not w:
        return fail("周报不存在"), 404
    return ok(_row(w))


@bp.patch("/<int:wid>")
@perm_required("weekly.manage")
def edit(wid):
    w = db.session.get(Weekly, wid)
    if not w:
        return fail("周报不存在"), 404
    if w.status == "final":
        return fail("已签发的周报不能直接改，请先出新修订版"), 409
    data = request.get_json(silent=True) or {}
    if "body_md" in data:
        body = (data.get("body_md") or "").strip()
        if not body:
            return fail("正文不能为空"), 400
        w.body_md = body
    u = current_user()
    w.editor = u.name if u else w.editor
    _audit("weekly_edit", f"修改周报 {w.period}")
    db.session.commit()
    return ok(_row(w))


@bp.post("/<int:wid>/polish")
@perm_required("weekly.manage")
def re_polish(wid):
    """对已有草稿重新起草「存在问题 / 下周计划」两段"""
    w = db.session.get(Weekly, wid)
    if not w:
        return fail("周报不存在"), 404
    if w.status == "final":
        return fail("已签发的周报不能重新起草"), 409
    try:
        payload = json.loads(w.data_json or "{}")
    except (ValueError, TypeError):
        return fail("取数快照损坏，请重新生成"), 500
    body = w.body_md or ""
    # 先把占位段切掉再拼，否则每点一次就叠一段
    idx = body.find("## 六、存在问题")
    base = body[:idx] if idx >= 0 else body
    extra = polish(payload)
    if not extra.strip():
        return fail("模型不可用，已保留原稿。可手写或稍后重试"), 503
    w.body_md = apply_polish(base + "\n", extra)
    _audit("weekly_polish", f"模型起草周报 {w.period}")
    db.session.commit()
    return ok(_row(w))


@bp.post("/<int:wid>/finalize")
@perm_required("weekly.manage")
def finalize(wid):
    w = db.session.get(Weekly, wid)
    if not w:
        return fail("周报不存在"), 404
    if w.status == "final":
        return fail("已签发"), 409
    if "（请结合本周实际" in (w.body_md or ""):
        return fail("正文还有未填写的占位段（存在问题 / 下周计划），补完再签发"), 409
    u = current_user()
    w.status = "final"
    w.signed_by = (u.name if u else "")[:32]
    w.signed_at = datetime.now()
    _audit("weekly_finalize", f"签发周报 {w.period} 第 {w.revision} 版")
    db.session.commit()
    return ok(_row(w))
