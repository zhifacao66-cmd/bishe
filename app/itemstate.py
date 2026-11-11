"""业务项的进度判据：全站唯一一处。

同一个事项"办到哪一步了"，过去在两个地方各算一遍：

  · 居民端 `portal._ticket_payload`：工单都办结、或已归档进台账 → 已办结
  · 首录台看板 `fde._today_rows`：只看工单 → 民政/综治这类**只写台账、
    不产生工单**的线永远停在"待派发"

于是同一件事在居民端是"已办结"、在社区工作台是"待派发"。这不是文案差异，
是两个页面在陈述互相矛盾的事实——看的人只会相信那个说"没人管"的。

判据收在这里，文案各自映射：内部看"已完成/办理中/已派单/待派发"，
居民看"已办结/处理中/已受理"。

`WorkItem.status` 不参与判断：全站没有任何一处写过它（创建时默认 pending，
之后没人动），拿它当依据等于把"已完成"写成一个永远不成立的分支。
"""
from datetime import datetime

from app.extensions import db
from app.models import Ledger, ResidentTicket, WorkItem, WorkOrder

# 内部（首录台/工单中心）用：文案 + 徽标类名
STAGE_STAFF = {
    "done": ("已完成", "b-green"),
    "doing": ("办理中", "b-orange"),
    "open": ("已派单", "b-blue"),
    "pending": ("待派发", "b-gray"),
}

# 居民端用：居民只关心"办没办完"，已派单与待派发都叫"已受理"
STAGE_RESIDENT = {
    "done": "已办结",
    "doing": "处理中",
    "open": "已受理",
    "pending": "已受理",
}


def stage(item, orders=None, filed=None) -> str:
    """返回 done / doing / open / pending 四态之一。

    `orders`、`filed` 可由调用方预先批量取好传进来，避免列表页 N+1 查询：
    首录台一次要判十几条，逐条查工单+台账就是几十次往返。
    """
    if orders is None:
        orders = WorkOrder.query.filter_by(work_item_id=item.id).all()
    if filed is None:
        filed = Ledger.query.filter_by(work_item_id=item.id).first() is not None

    states = [(o.status or "open") for o in orders]
    # 台账没有"状态"，有行就是已归档。居民端的"已办结"必须认它：
    # 民政/综治只写台账，只按工单判会让这些事项永远到不了办结。
    if filed:
        states.append("filed")

    if "doing" in states:
        return "doing"
    if states and all(s in ("done", "filed") for s in states):
        return "done"
    if "open" in states:
        return "open"
    return "pending"


def _ticket_overall(fde_id: int) -> str:
    """一整张居民诉求的进度：全部办结 / 有推进 / 未开始。

    与 `portal._ticket_payload` 的展示口径同源，避免"页面显示已办结、
    库里还是已受理"这类分析口径漂移。
    """
    items = WorkItem.query.filter_by(fde_id=fde_id).order_by(WorkItem.id.asc()).all()
    if not items:
        return "open"
    item_ids = [it.id for it in items]
    orders_map: dict[int, list] = {}
    for o in WorkOrder.query.filter(WorkOrder.work_item_id.in_(item_ids)).all():
        orders_map.setdefault(o.work_item_id, []).append(o)
    filed_ids = {wid for (wid,) in db.session.query(Ledger.work_item_id)
                 .filter(Ledger.work_item_id.in_(item_ids)).distinct().all()}
    stages = [stage(it, orders=orders_map.get(it.id, []), filed=it.id in filed_ids)
              for it in items]
    if all(s == "done" for s in stages):
        return "done"
    if any(s not in ("open", "pending") for s in stages):
        return "doing"
    return "open"


def sync_ticket_status(fde_id: int) -> int:
    """把居民单状态回写到 `resident_ticket.status`，返回实际变化的条数。

    只在**写路径**调用（建单、居民表态、工单流转），渲染函数不再 commit：
    GET 有副作用会让预取/重试/爬虫都产生写，还会和 SQLite 的写锁抢。
    """
    if not fde_id:
        return 0
    tickets = ResidentTicket.query.filter_by(fde_id=fde_id).all()
    if not tickets:
        return 0
    overall = _ticket_overall(fde_id)
    changed = 0
    for t in tickets:
        if (t.status or "") != overall:
            t.status = overall
            t.updated_at = datetime.now()
            changed += 1
    return changed
