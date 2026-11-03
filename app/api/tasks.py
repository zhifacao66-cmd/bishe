"""任务看板接口：街道下发任务 → 社区/物业承接 → 确认完成

权限：
  task.view    查看任务（三方都有，但只能看到派给自己的）
  task.manage  下发 / 关闭任务（仅街道）

数据隔离：社区/物业只能看到 assignee_role 与本人角色一致的任务，
街道作为下发方可看全部——与工单"物业只看本公司"是同一套最小必要原则。
"""
from datetime import datetime

from flask import Blueprint, request
from sqlalchemy import func

from app.api import ok, fail
from app.auth import ROLE_NAMES, ROLE_PERMS, current_user, perm_required
from app.extensions import db
from app.models import AuditLog, Task

bp = Blueprint("tasks", __name__)

DEADLINE_FMT = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d")


def _can(role: str, perm: str) -> bool:
    return perm in ROLE_PERMS.get(role or "", set())


def _parse_deadline(raw: str):
    raw = (raw or "").strip()
    for fmt in DEADLINE_FMT:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _audit(action, detail):
    u = current_user()
    db.session.add(AuditLog(actor=u.name if u else "系统", agent="task",
                            action=action, output_digest=detail[:250]))
    db.session.commit()


def _brief(t: Task, now: datetime) -> dict:
    overdue = bool(t.status != "done" and t.deadline and now > t.deadline)
    left_h = None
    if t.deadline and t.status != "done":
        left_h = round((t.deadline - now).total_seconds() / 3600, 1)
    return {
        "id": t.id,
        "title": t.title,
        "assignee_role": t.assignee_role,
        "assignee_role_name": ROLE_NAMES.get(t.assignee_role, t.assignee_role),
        "assignee_org": t.assignee_org or "",
        "detail": t.detail or "",
        "deadline": t.deadline.strftime("%Y-%m-%d %H:%M") if t.deadline else "",
        "status": t.status,
        "creator": t.creator or "",
        "created_at": t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "",
        "closed_at": t.closed_at.strftime("%Y-%m-%d %H:%M") if t.closed_at else "",
        "overdue": overdue,
        "overdue_hours": round((now - t.deadline).total_seconds() / 3600, 1) if overdue else 0,
        "left_hours": left_h,
    }


LIST_LIMIT = 100


def _scoped(base=None):
    """按身份收窄可见任务（列表 / 统计 / 办结校验共用这一处）

    非街道角色只给派给自己的任务。单位为空的任务是"发给全体的"，
    所以 `assignee_org == u.org` 之外还要放行 NULL 与空串——
    漏了这两条，老数据（补列前建的）会从受派方列表里凭空消失。
    """
    q = Task.query if base is None else base
    u = current_user()
    # 白名单而不是"除街道外一律收窄"：受派方只有 community / property 这两个角色。
    # 早先写成 `u.role != "street"`，admin 也被收窄成 assignee_role=="admin"——
    # 而任务里根本不存在这个受派角色，于是**管理员登录后工单页永远是空的**，
    # 统计也全 0（受派方物业反而能看到自己的 5 条）。admin 持有全量权限
    # （auth.ROLE_PERMS["admin"]=set(PERMS)），列表却看不到任何一条，自相矛盾。
    # 这段判断现已收敛到 app/policy.py:scope_query，与工单/证据链同源。
    from app.policy import scope_query
    q, _ = scope_query("task", q, u)
    return q


def _status_col():
    """状态列，NULL 当 open。

    老库补列时 status 可能是 NULL，直接写 `!= 'done'` 在 SQL 里对 NULL 求值
    是 NULL（不是 True），那批任务会既不算待办也不算已完成，统计总和比实际少。
    """
    return func.coalesce(Task.status, "open")


@bp.get("")
@perm_required("task.view")
def list_tasks():
    u = current_user()
    # desc 让 "open" 排在 "done" 之前（字符序 o > d），待办永远在顶部
    tasks = (_scoped().order_by(Task.status.desc(), Task.deadline.asc())
             .limit(LIST_LIMIT).all())
    now = datetime.now()
    rows = [_brief(t, now) for t in tasks]

    # 统计必须走服务端聚合，**不能数上面这 100 行**。
    # 明细有 limit，数出来的是"这一屏有多少"；任务超过 100 条时，
    # 卡片会把 140 条写成 100 条，而且超期数也会跟着少报——
    # 街道据此判断"这个社区还剩几件没办"，数字错了比没功能更糟。
    # 统计与明细共用 _scoped()，两边看到的是同一批任务。
    base = _scoped()
    by_status = dict(base.with_entities(_status_col(), func.count(Task.id))
                     .group_by(_status_col()).all())
    total = sum(by_status.values())
    done = by_status.get("done", 0)
    overdue = base.filter(_status_col() != "done",
                          Task.deadline.isnot(None),
                          Task.deadline < now).count()

    return ok({
        "rows": rows,
        "can_manage": _can(u.role, "task.manage") if u else False,
        "summary": {
            "open": total - done,
            "overdue": overdue,
            "done": done,
            "total": total,
            "shown": len(rows),
            "truncated": total > len(rows),
        },
    })


@bp.post("")
@perm_required("task.manage")
def create_task():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    role = (data.get("assignee_role") or "").strip()
    if not title:
        return fail("任务标题不能为空")
    if len(title) > 100:
        return fail("任务标题过长（≤100 字）")
    if role not in ("community", "property"):
        return fail("受派角色只能是 community 或 property")
    ddl = _parse_deadline(data.get("deadline"))
    if not ddl:
        return fail("截止时间格式应为 2026-10-25 或 2026-10-25 18:00")
    if ddl <= datetime.now():
        return fail("截止时间必须晚于当前时间")

    # 受派单位必须是该系统里真实存在的组织（取自同角色账号的 org）。
    # 早先这里是自由文本，填错一个字任务就发给了一个不存在的单位——
    # 列表按 org 隔离，受派方永远看不到，任务成了没人认领的孤儿。
    from app.models import User
    org = (data.get("assignee_org") or "").strip()
    known = {u.org for u in User.query.filter(User.role == role,
                                              User.org.isnot(None),
                                              User.org != "").all()}
    # `known` 为空时不能放行：那说明这个角色一个在职账号都没有，
    # 指定任何单位都发不到人手上，任务会变成谁也看不见的孤儿。
    if org and org not in known:
        return fail(f"受派单位「{org}」不存在，可选：{'、'.join(sorted(known))}"
                    if known else
                    f"{ROLE_NAMES.get(role, role)}暂无在职账号，请先建号再下发任务")

    u = current_user()
    t = Task(title=title, assignee_role=role,
             assignee_org=org,
             detail=(data.get("detail") or "").strip()[:500],
             deadline=ddl, creator=u.name if u else "")
    db.session.add(t)
    db.session.commit()
    _audit("task_create", f"下发任务「{title}」→ {ROLE_NAMES.get(role, role)}，"
                          f"截止 {ddl:%Y-%m-%d %H:%M}")
    return ok(_brief(t, datetime.now()))


@bp.post("/<int:tid>/done")
@perm_required("task.view")
def finish_task(tid):
    """受派方确认完成；街道也可代为关闭（任务作废/已线下办结）"""
    t = Task.query.get_or_404(tid)
    u = current_user()
    # 单位隔离必须同时落在读和写两侧。
    # 列表接口按 assignee_org 过滤了，这里如果只比角色，社区 A 的账号
    # 就能凭 id 办结社区 B 的任务（任务 id 是自增的，可枚举）——
    # 状态被改掉、留痕写着"受派方反馈"，而真正的受派方根本没办。
    # 工单侧踩过同一个坑（见 orders._load_order），这里按同一口径补上。
    same_unit = (not t.assignee_org) or bool(u and u.org and u.org == t.assignee_org)
    is_assignee = bool(u and u.role == t.assignee_role and same_unit)
    can_manage = bool(u and _can(u.role, "task.manage"))
    if not (is_assignee or can_manage):
        if u and u.role == t.assignee_role and not same_unit:
            # 跨单位操作要留痕：单看日志是"一次被拒"，连着看就是越权探测
            db.session.add(AuditLog(
                actor=u.name, agent="auth", action="deny",
                input_digest=f"跨单位办结任务 {t.id}（受派 {t.assignee_org}）"[:250],
                output_digest="已拒绝：任务不属于本单位"))
            db.session.commit()
        # 状态码必须和响应体一致：这里早先只写了 `fail(..., code=403)`，
        # Flask 会用默认 200 返回，于是「越权被拒」在 HTTP 层看起来是一次成功。
        # 后果很实在：按状态码统计的监控/巡检会把拒绝算成放行，
        # 线上核验脚本也因此误判过"越权全部通过"（body 里才是真的）。
        return fail("只能完成派给本角色的任务，或由街道关闭", code=403), 403
    if t.status == "done":
        return fail("任务已完成，无需重复操作")
    t.status = "done"
    t.closed_at = datetime.now()
    db.session.commit()
    _audit("task_done", f"任务「{t.title}」已完成"
                        f"{'（街道关闭）' if can_manage and not is_assignee else '（受派方反馈）'}")
    return ok(_brief(t, datetime.now()))
