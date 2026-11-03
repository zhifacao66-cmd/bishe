"""物业工单接口：列表 / 详情 / 接单 / 转派 / 反馈完成（闭环）

权限：查看需 order.view；接单、转派、办结需 order.handle（仅物业角色）

**每一次状态变更都必须走 `orderflow.push()`**，它同时改状态和写流转记录。
这里不出现裸的 `o.status = "..."`——那样会出现"状态变了但没人知道是谁推的"，
追责链断掉且不报错，是最难查的一类问题。
"""
from datetime import datetime

from flask import Blueprint, request
from sqlalchemy import func, or_

from app.api import ok, fail
from app.auth import current_user, perm_required
from app.extensions import db
from app.models import WorkOrder, WorkOrderLog, User, AuditLog
from app.search import like_all
from app import orderflow

bp = Blueprint("orders", __name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _int_arg(name, default, cap):
    """非法/非正参数回默认值再夹上限（`size=0` 是"没填"，不是"只要一条"）"""
    try:
        v = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    if v <= 0:
        return default
    return min(v, cap)


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


def _hours(a, b):
    """两个时刻相差小时数，保留一位小数；缺任一端返回 None（不拿 0 冒充"秒办"）"""
    if not a or not b:
        return None
    return round((b - a).total_seconds() / 3600.0, 1)


def _scoped(q=None):
    """按身份收窄可见工单

    「列表」「统计」「超期告警」三处必须共用这一段的过滤，
    否则会出现"卡片说 28 件待接单，列表里只有 12 件"这种数字打架。
    数据隔离只有一处实现，才不会漏。

    实现已收敛到 `app/policy.py:scope_query`——以前这段 filter 和
    `tasks._scoped` / `evidence` / `docs._visible` 各写一份，
    改规则要记得改四处，漏一处就是越权。
    """
    from app.policy import scope_query
    q = q or WorkOrder.query
    q, _ = scope_query("order", q, current_user())
    return q


def _audit(action, detail):
    """写审计。**不 commit**：与调用方那一批写入共用一个事务，
    否则会出现"审计已经落库、业务改动回滚了"的鬼记录。"""
    u = current_user()
    db.session.add(AuditLog(actor=u.name if u else "物业端", agent="work_order",
                            action=action, output_digest=detail[:250]))


def _load_order(oid):
    """按 id 取单，并做**归属校验**。

    列表接口已按 property_co 过滤，但写操作如果只按 id 直取就会绕过那层过滤，
    形成水平越权（IDOR）：知道单号（工单号是 `W+日期+序号`，可枚举）就能替
    别家物业接单、办结，甚至写入别家的处理反馈。
    数据隔离必须同时落在「读」和「写」两侧，只做读侧等于没做。

    返回 (order, error_response)；error_response 非空时直接返回给调用方。
    """
    o = db.session.get(WorkOrder, oid)
    if not o:
        return None, (fail("工单不存在", code=404), 404)
    u = current_user()
    if u and u.role == "property" and u.org and o.property_co != u.org:
        db.session.add(AuditLog(
            actor=u.name, agent="auth", action="deny",
            input_digest=f"物业跨主体操作工单 {o.order_no}"[:250],
            output_digest="已拒绝：工单不属于本单位",
        ))
        db.session.commit()
        return None, (fail("该工单不属于本单位，无权操作", code=403), 403)
    return o, None


def _find_handler(o, name):
    """把"姓名"解析成本单位在职的物业账号。

    转派对象必须是真实账号，不能是自由文本——否则填错一个字，
    单子就转给了一个不存在的人，之后既没人认领也查不到（任务受派单位
    踩过同一个坑，见 docs/17）。
    """
    name = (name or "").strip()[:32]
    if not name:
        return None
    # 单位为空时 `User.org == None` 会变成 `IS NULL`，把 org 没填的账号
    # 全匹配进来——那等于绕过了"同单位"这条限制，必须先在门口挡住。
    if not (o.property_co or "").strip():
        return None
    return (User.query
            .filter(User.role == "property", User.status == "active",
                    User.org == o.property_co,
                    or_(User.name == name, User.username == name))
            .first())


def _item(o, reject_map=None):
    """单条工单的对外形状

    `reject_map` 是批量算好的「工单 id → 被驳回次数」。列表页面 50 条，
    逐条 `count()` 就是 50 次查询，压测里会直接体现在 P95 上——
    而它本来一条 group by 就能算完。
    """
    from app.agents.supervise import SuperviseAgent
    from app.arbitration import DISPUTE_TYPES, VERDICTS
    return {
        "id": o.id, "order_no": o.order_no, "content": o.content,
        "property_co": o.property_co, "sla_hours": o.sla_hours,
        "sla_level": o.sla_level or "", "status": o.status, "feedback": o.feedback,
        "assignee": o.assignee or "", "assigned_at": _fmt(o.assigned_at),
        "done_at": _fmt(o.done_at),
        # 处理时长只用真实时刻算；老单没有接单时间就留空，
        # 不能拿 created_at 顶上去——那算的是"从派单到办结"的总时长，
        # 跟"维修工干了多久"是两个指标。
        "handle_hours": _hours(o.assigned_at, o.done_at),
        "overdue": SuperviseAgent.is_overdue(o),
        "created_at": o.created_at.strftime("%Y-%m-%d %H:%M"),
        # 争议与裁决。字段透出给前端，但**结论文案服务端算**：
        # 让前端拿代码去查中文表，两边迟早不同步（新增一种裁决时页面会显示空白）。
        "confirm_status": o.confirm_status or "",
        "confirm_status_text": {"ok": "居民已确认", "disputed": "居民不服"}.get(
            o.confirm_status or "", "待居民确认"),
        "confirm_note": o.confirm_note or "",
        "confirm_at": _fmt(o.confirm_at),
        "dispute_type": o.dispute_type or "",
        "dispute_type_text": DISPUTE_TYPES.get(o.dispute_type or "", ""),
        "verdict": o.verdict or "",
        "verdict_text": VERDICTS[o.verdict][1] if o.verdict in VERDICTS else "",
        "verdict_basis": o.verdict_basis or "",
        "verdict_by": o.verdict_by or "",
        "verdict_at": _fmt(o.verdict_at),
        # 拒一次 = 踢一次皮球。这个数从日志现查，不在工单上冗余存计数：
        # 冗余计数一旦和日志对不上，追责时就不知道该信哪个。
        "reject_count": (reject_map.get(o.id, 0) if reject_map is not None
                         else WorkOrderLog.query.filter_by(
                             order_id=o.id, action="reject").count()),
    }


def _reject_map(order_ids):
    """批量算「工单 id → 被驳回次数」"""
    if not order_ids:
        return {}
    rows = (db.session.query(WorkOrderLog.order_id, func.count(WorkOrderLog.id))
            .filter(WorkOrderLog.action == "reject",
                    WorkOrderLog.order_id.in_(order_ids))
            .group_by(WorkOrderLog.order_id).all())
    return {oid: n for oid, n in rows}


@bp.get("")
@perm_required("order.view")
def list_orders():
    from app.agents.supervise import SuperviseAgent
    base = _scoped()
    limit = _int_arg("limit", DEFAULT_LIMIT, MAX_LIMIT)
    page = _int_arg("page", 1, 1000)
    status = (request.args.get("status") or "").strip()
    kw = request.args.get("q") or ""      # 长度与切词交给 search.terms 处理

    # 筛选只收窄明细（matched），不动家底（total/open/doing/done/overdue）——
    # 卡片上一律是"整本账有多少"，数字不会因为输了个关键词就变小。
    q = base
    if status in ("open", "doing", "done"):
        q = q.filter(WorkOrder.status == status)
    if kw:
        # 通配符转义与多词 AND 统一在 app/search.py（为什么不用 FTS5 那里也写了）
        cond = like_all([WorkOrder.order_no, WorkOrder.content,
                         WorkOrder.property_co, WorkOrder.assignee], kw)
        if cond is not None:
            q = q.filter(cond)             # assignee 可为空，NULL 不参与 OR 命中

    matched = q.count()
    orders = (q.order_by(WorkOrder.created_at.desc())
              .offset((page - 1) * limit).limit(limit).all())

    # 统计口径与明细同源：都在上面那个"已按单位隔离"的 base 上数。
    # 前端 `list.filter(...).length` 数的是被 limit 截断后的那几十条，
    # 演示库 57 条工单、页面只给 50 条，"已完成"就少报 7 件。
    counts = dict(base.with_entities(WorkOrder.status, func.count(WorkOrder.id))
                  .group_by(WorkOrder.status).all())
    total = sum(counts.values())
    # 超期是"未办结且已过时限"。判定必须复用 SuperviseAgent.is_overdue，
    # 不能在这里另写一套时间比较——两套阈值迟早不一样，告警条和卡片就会打架。
    # 只看未办结的，量级远小于全表。
    overdue = sum(1 for o in base.filter(WorkOrder.status != "done").all()
                  if SuperviseAgent.is_overdue(o))
    rm = _reject_map([o.id for o in orders])

    return ok({
        "items": [_item(o, rm) for o in orders],
        "counts": {
            "total": total,
            "open": counts.get("open", 0),
            "doing": counts.get("doing", 0),
            "done": counts.get("done", 0),
            "overdue": overdue,
            # 居民不服的单数：它和超期不是一回事——超期是"慢了"，
            # 不服是"办了但不认"。卡片要分开，否则居委会分不清该催谁。
            "disputed": base.filter(WorkOrder.confirm_status == "disputed").count(),
            "matched": matched,
            "page": page, "size": limit,
            "pages": max(1, (matched + limit - 1) // limit),
            "shown": len(orders),
            "truncated": matched > len(orders),
        },
        "filter": {"status": status, "q": kw},
    })


@bp.get("/arbitration")
@perm_required("order.verdict")
def arbitration_board():
    """居委会的仲裁台：待裁清单 + 概览

    为什么单独一个入口而不是在工单列表上加个筛选：
    仲裁台要回答的是"哪几件在等我判"，而工单列表回答的是"有多少活"。
    两个视角混在一个页面上，居委会每次都得先想"我该用哪个筛选"。
    """
    from app import arbitration
    scope = (request.args.get("scope") or "pending").strip()
    if scope == "judged":
        # 已裁决：居民仍是不服状态，但结论已下。留着是因为——
        # 居民随时可能补材料要求复议，这些单不能从眼前消失。
        rows = (WorkOrder.query
                .filter(WorkOrder.confirm_status == "disputed",
                        WorkOrder.verdict.isnot(None), WorkOrder.verdict != "")
                .order_by(WorkOrder.verdict_at.desc()).limit(100).all())
    else:
        rows = arbitration.pending().limit(100).all()
    rm = _reject_map([o.id for o in rows])
    return ok({
        "items": [_item(o, rm) for o in rows],
        "stats": arbitration.stats(),
        "scope": scope,
        # 选项由服务端下发，前端不再自己维护一份中文表
        "dispute_types": arbitration.DISPUTE_TYPES,
        "verdicts": {k: {"label": v[1], "hint": v[2], "action": v[0]}
                     for k, v in arbitration.VERDICTS.items()},
    })


@bp.post("/<int:oid>/judge")
@perm_required("order.verdict")
def judge(oid):
    """裁决一张有争议的工单

    两档错误必须分开返回，否则监控会把"没权限"和"填错了"算成一类：
      · 403：不是仲裁方（物业碰自己的单、角色不对）
      · 400：结论未知、依据太短、单子状态不对
    """
    o, err = _load_order(oid)
    if err:
        return err
    from app import arbitration
    u = current_user()
    okc, why = arbitration.can_judge(u, o)
    if not okc:
        return fail(why), 403

    d = request.get_json(silent=True) or {}
    okj, msg = arbitration.judge(
        o, d.get("verdict") or "", d.get("basis") or "", u,
        note=d.get("note") or "", dispute_type=d.get("dispute_type") or "")
    if not okj:
        return fail(msg), 400
    db.session.commit()
    return ok({"id": o.id, "status": o.status, "verdict": o.verdict,
               "verdict_text": msg, "confirm_status": o.confirm_status,
               "reject_count": WorkOrderLog.query.filter_by(
                   order_id=o.id, action="reject").count()})


@bp.get("/handlers")
@perm_required("order.handle")
def handlers():
    """可转派的人：本单位在职的物业账号

    只回姓名和账号名，不回手机号——选人不需要联系方式。
    仍按项目约定包 `{items, counts}`，不吐裸数组：前端拿到的形状
    全站一致，才不会有人写 `.map` 有人写 `.items.map`。
    """
    u = current_user()
    q = User.query.filter(User.role == "property", User.status == "active")
    if u and u.role == "property" and u.org:
        q = q.filter(User.org == u.org)
    rows = q.order_by(User.id).all()
    return ok({
        "items": [{"name": x.name, "username": x.username, "org": x.org}
                  for x in rows],
        "counts": {"total": len(rows)},
    })


@bp.get("/<int:oid>")
@perm_required("order.view")
def order_detail(oid):
    """工单详情 + 完整流转记录（只读，供追责与对账）"""
    o, err = _load_order(oid)
    if err:
        return err
    d = _item(o)
    d["events"] = orderflow.events(o.id)
    d["event_count"] = len(d["events"])
    return ok(d)


@bp.post("/<int:oid>/accept")
@perm_required("order.handle")
def accept(oid):
    o, err = _load_order(oid)
    if err:
        return err
    if o.status != "open":
        return fail(f"工单当前状态为 {o.status}，不能接单")
    u = current_user()
    # 处理人取会话身份，不读前端传值：处理人是追责依据，
    # 允许前端指定等于允许冒名（首录的 creator 也是同一个道理）。
    o.assignee = (u.name or "")[:32]
    o.assignee_id = u.id
    o.assigned_at = datetime.now()
    orderflow.push(o, "accept", actor=o.assignee, actor_id=u.id, actor_org=u.org or "",
                   detail=f"接单处理（{o.sla_level or '一般'} / {o.sla_hours}h）")
    _audit("accept", f"工单{o.order_no} 由 {o.assignee} 接单")
    db.session.commit()
    return ok({"id": o.id, "status": o.status, "assignee": o.assignee,
               "assigned_at": _fmt(o.assigned_at)})


@bp.post("/<int:oid>/transfer")
@perm_required("order.handle")
def transfer(oid):
    """转派：把处理中的单子交给本单位另一个人（交接班 / 请假 / 换工种）

    这是"处理人"这个字段真正要解决的事：光记"接单时是谁"，
    一旦换手就再也对不上。转派改 assignee，同时把"从谁转给谁、为什么"
    写进流转记录，原处理人不会被覆盖掉。
    """
    o, err = _load_order(oid)
    if err:
        return err
    if o.status != "doing":
        return fail(f"工单当前状态为 {o.status}，只能转派处理中的工单")
    data = request.get_json(silent=True) or {}
    to_name = (data.get("assignee") or "").strip()
    note = (data.get("note") or "").strip()[:200]
    if not to_name:
        return fail("请选择转派给谁")
    target = _find_handler(o, to_name)
    if not target:
        names = [x.name for x in User.query.filter(
            User.role == "property", User.status == "active",
            User.org == o.property_co).all()]
        return fail("转派对象必须是本单位在职的处理人"
                    + (f"（可选：{'、'.join(names)}）" if names else "，但本单位暂无其他在职账号"))
    if target.id == o.assignee_id:
        return fail(f"工单当前就由 {target.name} 处理，无需转派")
    u = current_user()
    prev = o.assignee or "未指派"
    o.assignee = target.name[:32]
    o.assignee_id = target.id
    # 转派不改接单时间：assigned_at 记的是"这单什么时候开始被处理"，
    # 换个人不会让处理时长从零开始——否则换个手 SLA 就自动复活了。
    orderflow.push(o, "transfer", actor=(u.name if u else ""), actor_id=(u.id if u else None),
                   actor_org=(u.org if u else ""),
                   detail=f"{prev} → {target.name}" + (f"：{note}" if note else ""))
    _audit("transfer", f"工单{o.order_no} 由 {prev} 转派给 {target.name}")
    db.session.commit()
    return ok({"id": o.id, "status": o.status, "assignee": o.assignee,
               "assigned_at": _fmt(o.assigned_at)})


@bp.post("/<int:oid>/finish")
@perm_required("order.handle")
def finish(oid):
    o, err = _load_order(oid)
    if err:
        return err
    if o.status != "doing":
        return fail(f"工单当前状态为 {o.status}，不能完成")
    feedback = ((request.get_json(silent=True) or {}).get("feedback") or "").strip()
    if not feedback:
        return fail("请填写处理反馈")
    u = current_user()
    o.feedback = feedback[:1000]
    o.done_at = datetime.now()
    # 交接班时单子可能由别人办结：assignee 不覆盖（"谁接的单"是事实），
    # 办结人记在流转记录里。只有老单从没记过处理人时才补上，避免空着。
    if not o.assignee and u:
        o.assignee = (u.name or "")[:32]
        o.assignee_id = u.id
    orderflow.push(o, "finish", actor=(u.name if u else ""), actor_id=(u.id if u else None),
                   actor_org=(u.org if u else ""), detail=o.feedback)
    _audit("finish", f"工单{o.order_no} 完成（{u.name if u else ''}）：{feedback[:60]}")
    # F5：台账Agent 补全流程记录
    from app.agents.fill import archive_order_feedback
    lg = archive_order_feedback(o)
    db.session.commit()
    return ok({"id": o.id, "status": o.status, "feedback": o.feedback,
               "assignee": o.assignee, "done_at": _fmt(o.done_at),
               "handle_hours": _hours(o.assigned_at, o.done_at),
               "ledger_id": lg.id})
