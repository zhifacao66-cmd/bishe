"""工单流转：状态迁移 + 留痕的唯一入口

派单由 `DispatchAgent` 触发，接单/转派/办结由 `app/api/orders.py` 触发，
四处都调这里，动作名与状态迁移在 `ACTIONS` 里定死。

**为什么不让各处自己写 `o.status = "doing"`**
因为那样"改状态"和"留痕"是两件事，迟早有人只做前一件——更新了状态、
日志里却查不到是谁推的，追责链在这里断掉，而且断了不会报错。
现在 `push()` 内部同时改状态和写一条 `work_order_log`，想漏都漏不掉。

状态机：open --accept--> doing --finish--> done
               transfer 只换人，状态仍是 doing
               done --reject--> doing     居委会判定办得不合格，退回重办
               done --uphold--> done      居委会判定物业无责，维持办结（状态不变）

**reject / uphold 为什么放在这里而不是单开一张"裁决表"**
裁决的结果本来就要改变工单状态（驳回必须能重办，否则物业没有入口），
而"改状态必须走 push()"是本项目的红线。如果另开一张表记裁决、
再在旁边补一句 `o.status = "doing"`，就等于自己破了这条红线。
所以裁决作为一个动作进状态机，裁决理由走 `detail`，结论走 `order.verdict`。
"""
from datetime import datetime

from app.extensions import db
from app.models import WorkOrderLog

# action -> (目标状态, 中文说明)
ACTIONS = {
    "dispatch": ("open", "派单"),
    "accept": ("doing", "接单"),
    "transfer": ("doing", "转派"),
    "finish": ("done", "办结"),
    "reject": ("doing", "驳回重办"),
    "uphold": ("done", "维持办结"),
}

# 每个动作允许的**起始**状态。None = 新单尚未落库（dispatch 时 order 刚建出来）。
#
# 为什么放在这里而不是只靠 API 前置校验：那四个端点确实各自校验了状态，
# 但校验写在调用方，就等于"每个新调用点都要重新记得校验一次"。将来加个
# 批量办结、或某个 Agent 想直接推进状态，漏写一句就出现 done→done 的
# 重复办结，而流转记录本身看不出异常（它只是如实记录了两次办结）。
# 状态机这么小的东西，应该只有一处定义。
ALLOWED_FROM = {
    "dispatch": {None, "open"},
    "accept": {"open"},
    "transfer": {"doing"},
    "finish": {"doing"},
    # 只有办结的单才有"驳回"可言；open 状态居民都还没见过结果，谈不上不服
    "reject": {"done"},
    # uphold 是"不改状态的一次判定"：居委审了、认定物业无责、维持 done。
    # from == to 在这里是合法的，但不能因此放宽到任何状态——
    # 写死 done，才不会被误用到 doing 的单上变成"维持一个还没办完的结果"。
    "uphold": {"done"},
}

LABEL = {k: v[1] for k, v in ACTIONS.items()}


def push(order, action, actor="", actor_id=None, actor_org="", detail="", at=None,
         extra=None):
    """记一步流转，并把工单推到该动作的目标状态。

    调用方只需在调用前改好 `assignee` 之类"这一步特有"的字段；
    状态由这里统一改，`from_status` 取改之前的值，所以事件链能对上。
    不 commit —— 交给调用方和自己那批写入一起提交，避免半截事务。

    `at` 只给造演示数据用：真实操作一律走默认的"此刻"，
    否则时间线可以被随手写歪，那就不是留痕了。

    起始状态不在 `ALLOWED_FROM` 里时抛 ValueError —— 这是编程错误
    （调用顺序错），不是用户操作错误，所以不降级、不静默修状态。

    **同时落一条证据链**：留痕有两种读者——内部人要查"这单走过哪几步"
    （work_order_log，带中文姓名和单位），争议方要证明"这条记录没被改过"
    （evidence_chain，带哈希）。两件事在同一处完成，才不会出现
    "状态变了、日志写了、但链上漏了"这种只在一半情况下成立的留痕。
    """
    if action not in ACTIONS:
        raise ValueError(f"未知工单动作：{action}")
    cur = order.status or None
    if cur not in ALLOWED_FROM[action]:
        raise ValueError(
            f"工单 {order.order_no} 当前状态 {cur or '未落库'} 不允许「{LABEL[action]}」"
            f"（允许的起始状态：{'、'.join(sorted(x or '未落库' for x in ALLOWED_FROM[action]))}）")
    to_status, _ = ACTIONS[action]
    if order.id is None:
        # 派单时工单对象刚建出来还没落库，而链记录要写 target_id。
        # flush 只发 SQL 不出事务，id 就有了；不 flush 会写成 target_id=None，
        # 而该列不允许为空，报错还发生在事务中途。
        db.session.flush()
    log = WorkOrderLog(
        order_id=order.id,
        order_no=order.order_no,
        action=action,
        from_status=order.status,
        to_status=to_status,
        actor=(actor or "")[:32],
        actor_id=actor_id,
        actor_org=(actor_org or "")[:64],
        detail=(detail or "")[:2000],
        created_at=at or datetime.now(),
    )
    order.status = to_status
    db.session.add(log)

    # 工单状态变了，关联居民诉求的缓存状态也要跟着变。
    # 放在 push() 里而不是各 API：这是全站唯一的状态变更入口，
    # 新增动作（批量办结、裁决）不会再漏一次回写。没有关联居民单时是一次空查询。
    if order.work_item_id:
        from app.itemstate import sync_ticket_status
        from app.models import WorkItem
        wi = db.session.get(WorkItem, order.work_item_id)
        if wi and wi.fde_id:
            sync_ticket_status(wi.fde_id)

    from app import evidence
    payload = {
        "order_no": order.order_no,
        "action": action,
        "from": log.from_status,
        "to": to_status,
        "detail": (detail or "")[:180],
    }
    if extra:
        payload.update(extra)
    evidence.append("order", order.id, action, payload, actor=actor, at=log.created_at)
    return log


def events(order_id):
    """按时间正序取某张单的全部流转（追责按事情发生的顺序读，不按最新在前）"""
    rows = (WorkOrderLog.query
            .filter(WorkOrderLog.order_id == order_id)
            .order_by(WorkOrderLog.created_at.asc(), WorkOrderLog.id.asc())
            .all())
    return [{
        "action": r.action,
        "label": LABEL.get(r.action, r.action),
        "from_status": r.from_status or "",
        "to_status": r.to_status or "",
        "actor": r.actor or "",
        "actor_id": r.actor_id,
        "actor_org": r.actor_org or "",
        "detail": r.detail or "",
        "at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
    } for r in rows]
