"""报表版本归档与回滚

「一期一份」与「能回滚」表面上冲突：只留一行，旧内容必然被覆盖掉。
解决方式是把两件事拆开——

  · `report`        一行一期，列表/导出/编号都只看它（归档件唯一）
  · `report_revision` 只追加，存被替掉的每一版整份快照（历史不丢）

两条硬规则：

1. **覆盖前先归档**。改 `report` 行之前，必须先把旧内容塞进 `report_revision`。
   顺序反了，旧内容就物理消失，回滚无从谈起——而这一步不会报错，只会静默丢数据。

2. **回滚 = 用旧内容生成新一版，版本号继续往上走**。
   不允许把 version 改回旧号。版本号是归档件的唯一标识（"2026-10 第 3 版"），
   一旦允许重号，这个标识就不再指向唯一一份内容，导出件上的编号也就没意义了。
   回滚后 `rollback_from` 记着"本版抄的是第几版"，可追溯但不混淆。
"""
from datetime import datetime

from app.agents.base import mask_sensitive
from app.extensions import db
from app.models import AuditLog, Report, ReportRevision

REASON_CN = {
    "regenerate": "重新生成",
    "rollback": "回滚前留档",
}


def archive(rep: Report, reason: str, actor: str = "") -> ReportRevision | None:
    """把 rep 的当前内容存进历史表。**必须在覆盖它之前调用。**"""
    if rep is None or not rep.data_json:
        return None
    rv = ReportRevision(
        report_id=rep.id,
        period=rep.period,
        line=rep.line,
        version=int(rep.version or 1),
        data_json=rep.data_json,
        generated_at=rep.generated_at,
        reason=reason if reason in REASON_CN else "regenerate",
        actor=(actor or "")[:32],
    )
    db.session.add(rv)
    return rv


def history(rep: Report) -> list[dict]:
    """当前版 + 已归档的历史版，按版本号降序。

    给页面用：让人一眼看出"这份月报被改过几次、每次改的是什么"。
    摘要取 `summary` 前 60 字——版本之间的差别通常在数字上，
    一两个字就能看出这版和那版说的是不是同一件事。
    """
    import json

    rows = (ReportRevision.query
            .filter_by(report_id=rep.id)
            .order_by(ReportRevision.version.desc())
            .all())
    out = [{
        "version": int(rep.version or 1),
        "generated_at": rep.generated_at.strftime("%Y-%m-%d %H:%M") if rep.generated_at else "",
        "archived_at": "",
        "reason": "",
        "current": True,
        "rollback_from": int(rep.rollback_from) if rep.rollback_from else None,
        "summary": (json.loads(rep.data_json).get("summary", "")[:60]
                    if rep.data_json else ""),
    }]
    for r in rows:
        try:
            s = json.loads(r.data_json).get("summary", "")[:60]
        except (ValueError, TypeError, AttributeError):
            s = ""
        out.append({
            "version": int(r.version or 1),
            "generated_at": r.generated_at.strftime("%Y-%m-%d %H:%M") if r.generated_at else "",
            "archived_at": r.archived_at.strftime("%Y-%m-%d %H:%M") if r.archived_at else "",
            "reason": REASON_CN.get(r.reason, r.reason or ""),
            "current": False,
            "rollback_from": None,
            "summary": s,
        })
    out.sort(key=lambda x: -x["version"])
    return out


def rollback(rep: Report, target: int, actor: str = "") -> tuple[bool, str]:
    """回滚到第 target 版：抄它的内容，生成新的一版。

    返回 (成功, 给用户看的说明)。
    """
    cur = int(rep.version or 1)
    if target == cur:
        return False, f"第 {target} 版就是当前版本，无需回滚"

    # 可用目标 = 已归档的版本号（当前版不算）。范围外和"号码对但没快照"
    # （早期版本落在 audit_log 里、没进历史表）都要给出同一句提示：
    # 只说"不存在"等于让人猜到底能回哪一版。
    avail = [r.version for r in
             (ReportRevision.query.filter_by(report_id=rep.id)
              .order_by(ReportRevision.version).all())]
    if target < 1 or target > cur or target not in avail:
        tip = (f"（可回滚：第 {'、'.join(str(v) for v in avail)} 版）" if avail
               else "（这份报表还没有任何历史版本）")
        return False, f"回滚目标第 {target} 版不可用，当前是第 {cur} 版{tip}"

    rv = (ReportRevision.query
          .filter_by(report_id=rep.id, version=target)
          .order_by(ReportRevision.id.desc()).first())
    if not rv:
        return False, f"找不到第 {target} 版的快照"

    # 先归档当前版，再覆盖。反过来的话，回滚本身就成了又一次"不可回滚的覆盖"。
    archive(rep, "rollback", actor)

    rep.data_json = rv.data_json
    rep.version = cur + 1
    rep.generated_at = datetime.now()
    rep.rollback_from = target

    db.session.add(AuditLog(
        actor=(actor or "system")[:32], agent="report", action="rollback_report",
        input_digest=mask_sensitive(f"{rep.period} 第 {cur} 版 → 回滚到第 {target} 版")[:250],
        output_digest=mask_sensitive(
            f"已生成第 {cur + 1} 版（内容同第 {target} 版）")[:250],
    ))
    db.session.commit()
    return True, f"已回滚：现在是第 {cur + 1} 版，内容等同第 {target} 版"
