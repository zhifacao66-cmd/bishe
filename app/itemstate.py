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
from app.models import Ledger, WorkOrder

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
