"""三方争议裁决：居民—物业—居委会之间的规则层

## 这个模块存在的理由

"居民说没修好、物业说修好了"这类争执，社区每天都在处理，但绝大多数系统
只做到"工单办结"就结束了——因为一旦引入"谁对谁错"，就要有人负责、要有依据、
要能追溯。**回避争议恰恰是很多政务系统做不下去的原因**。

三方治理的核心不是"多一个角色"，而是**多一个中立的裁决环节**，且这个环节必须：
1. 有明确的**入口**（居民能表示不服，而不是打电话骂人）；
2. 有明确的**结论**（责任在谁，而不是"已沟通"）；
3. 有明确的**依据**（合同条款/维修资金办法，而不是和稀泥）；
4. 有明确的**追溯**（谁在什么时候判的，不可事后改）。

## 两条铁律

**回避原则**：物业是争议当事方，永远不能裁决涉及自己的工单。
`can_judge()` 里明写 role 判断，权限点 `order.verdict` 也刻意不发给物业——
双保险，因为只靠权限点配置，管理员一次误勾就把规则破了。

**依据必填**：没有依据的裁决等于偏袒。`judge()` 里 basis 少于 8 字直接拒绝，
不接受"协调处理""已沟通"这类空话——它既不能说服居民，也无法在街道复核时自证。
"""
from __future__ import annotations

from datetime import datetime

from app.extensions import db
from app import evidence, orderflow
from app.models import AuditLog, ResidentTicket, WorkItem, WorkOrder

# 争议类型：居民不服时选，居委裁决时可修正
DISPUTE_TYPES = {
    "duty": "责任归属（该不该物业管）",
    "fee": "费用承担（这笔钱谁出）",
    "quality": "质量不达标（干得不合格）",
    "delay": "超期未办（拖太久）",
}

# 裁决结论 → (动作, 中文, 一句话解释)
#
# 前三个都会 `uphold`（维持办结），只有 property_fault 会 `reject`（退回重办）。
# 这个比例是刻意的：真实社区里绝大多数"不满意"最后查明是居民对责任范围的理解偏差，
# 真正需要物业返工的只是一部分。如果裁决里"驳回"占压倒多数，
# 说明要么派单派错了，要么物业确实在糊弄——两种情况都是值得预警的信号。
VERDICTS = {
    "property_fault": ("reject", "物业责任 · 退回重办", "属于物业服务合同约定范围，应返工"),
    "owner_duty": ("uphold", "业主自用部分 · 物业无责", "专有部分维修由业主自行承担"),
    "public_duty": ("uphold", "公共部位 · 已尽责", "公共部位维修费用由公共收益或维修资金列支"),
    "escalate": ("uphold", "超出社区权限 · 上报街道", "需上级部门或专业单位处置，已移交"),
}


def can_judge(user, order=None) -> tuple[bool, str]:
    """能不能裁这张单。返回 (可否, 理由)

    理由是要给人看的：不给理由的 403 只会让人觉得"系统坏了"。
    """
    if not user:
        return False, "未登录"
    if getattr(user, "role", "") == "property":
        # 回避原则。即使管理员误把 order.verdict 勾给了物业，这里也会拦住。
        return False, "物业是争议当事方，不能裁决自己的工单（回避原则）"
    if user.role not in ("community", "admin"):
        return False, "裁决权归社区居委会"
    if order is not None:
        if order.status != "done":
            return False, "只有已办结的工单才可裁决"
        if order.confirm_status != "disputed":
            return False, "居民尚未表示不服，无需裁决"
        # 防重复裁决。判据不是"verdict 非空"，而是**本轮**是否已裁：
        # reject 之后 confirm_status 会被清空、verdict 却保留（那是历史结论，
        # 用于追溯），物业重办、居民再次不服时会重写 confirm_at。
        # 所以比时间戳就能自动区分"同一轮被提交两次"与"第二轮争议"。
        #
        # 不做这个校验的后果真实发生过：种子脚本重复执行，同一张单在同一秒
        # 被判了两次 uphold，日志里两条记录、verdict 被后者覆盖，
        # 事后复盘看到"判两次"却说不清哪次作数。
        if (order.verdict or "") and _judged_this_round(order):
            return False, ("该工单本轮争议已裁决，不重复受理；"
                          "如需改判，须居民重新提出异议或提请街道复核")
    return True, ""


def _judged_this_round(order) -> bool:
    """本轮争议是否已有结论。

    有 confirm_at 就比时间：裁决时刻不早于居民表态时刻 = 已经裁过。
    confirm_at 缺失（老数据）时退回"verdict 非空即已裁"——
    宁可多拦一次（让居委会去核查），也不要让结论被静默覆盖。
    """
    if not (order.verdict or ""):
        return False
    if not order.confirm_at or not order.verdict_at:
        return True
    return order.verdict_at >= order.confirm_at


def judge(order: WorkOrder, verdict: str, basis: str, user, note: str = "",
          dispute_type: str = ""):
    """执行裁决：写结论 + 推状态机 + 落链 + 写审计。**不 commit**

    返回 (ok, msg)。
    """
    ok, why = can_judge(user, order)
    if not ok:
        return False, why
    if verdict not in VERDICTS:
        return False, f"未知裁决结论：{verdict}"
    basis = (basis or "").strip()
    if len(basis) < 8:
        return False, "请写明裁决依据（不少于 8 字，如「物业服务合同第 3 条约定公共照明由物业维护」）"
    if dispute_type and dispute_type not in DISPUTE_TYPES:
        return False, f"未知争议类型：{dispute_type}"

    action, label, _ = VERDICTS[verdict]
    order.verdict = verdict
    order.verdict_basis = basis[:256]
    order.verdict_by = (user.name or "")[:32]
    order.verdict_at = datetime.now()
    if dispute_type:
        order.dispute_type = dispute_type

    # 状态与留痕走同一个入口（红线：禁止裸改 status）。
    # 裁决结论通过 extra 并入同一条链记录，不再单独 append 一次——
    # 两次 append 会让"同一次裁决"在链上占两个序号，事后核对时
    # 看到的是两条记录，很难判断是"判了两次"还是"一次判了记两笔"。
    orderflow.push(order, action, actor=(user.name or ""), actor_id=getattr(user, "id", None),
                   actor_org=getattr(user, "org", "") or "",
                   detail=f"{label}｜依据：{basis[:180]}" + (f"｜{note}" if note else ""),
                   extra={"verdict": verdict, "verdict_label": label,
                          "basis": basis[:180], "judge": order.verdict_by})

    if action == "reject":
        # 退回重办后，上一轮的"已办结"不再作数：确认状态必须清空，
        # 否则居民端会一边显示"处理中"、一边保留"已确认"，自相矛盾。
        # done_at 保留 —— 它记录的是"上一次办结发生在何时"，是事实，不该抹掉。
        order.confirm_status = ""
        order.confirm_at = None
        order.confirm_note = ""
    else:
        # 维持办结：居民的"不服"已被正式回应，落地为"已裁决"。
        # 这里不把 confirm_status 改成 ok —— 居民本人并没有确认解决，
        # 替他改口等于伪造居民意愿。
        order.confirm_note = (order.confirm_note or "")[:300]

    db.session.add(AuditLog(
        actor=order.verdict_by, agent="arbitration", action=f"verdict:{verdict}",
        input_digest=f"工单{order.order_no}",
        output_digest=f"{label}｜{basis[:120]}"[:250]))
    return True, label


def resident_confirm(ticket: ResidentTicket, ok: bool, note: str = "",
                     dispute_type: str = "", at=None):
    """居民表态：解决 / 不服。**不 commit**。返回 (ok, msg, 影响单数)

    只作用于该诉求下**已办结**的工单。还在处理中的单不参与表态——
    居民不能对一件还没干完的事说"没解决"，那会让工单被误判进争议池，
    白白占用居委会的裁决精力。

    `at` 与 `orderflow.push()` 同义：**只给造演示数据用**。
    真实操作一律"此刻"，否则"什么时候表态的"可以被随手写歪，
    而这条时间恰恰是判断"是否已经裁过"的依据（见 can_judge）。
    """
    if not ticket:
        return False, "查询码无效", 0
    orders = []
    if ticket.fde_id:
        items = WorkItem.query.filter_by(fde_id=ticket.fde_id).all()
        ids = [it.id for it in items]
        if ids:
            orders = (WorkOrder.query.filter(WorkOrder.work_item_id.in_(ids),
                                             WorkOrder.status == "done").all())
    if not orders:
        return False, "这件事还没有办结的环节，暂不能表态", 0

    note = (note or "").strip()
    if not ok and len(note) < 4:
        return False, "请说明哪里没解决（不少于 4 个字），居委会据此核实", 0
    if not ok and dispute_type and dispute_type not in DISPUTE_TYPES:
        return False, f"未知争议类型：{dispute_type}", 0

    now = at or datetime.now()
    for o in orders:
        o.confirm_status = "ok" if ok else "disputed"
        o.confirm_at = now
        o.confirm_note = note[:512]
        if not ok and dispute_type:
            o.dispute_type = dispute_type
        evidence.append("order", o.id, "confirm" if ok else "dispute", {
            "order_no": o.order_no,
            "result": "ok" if ok else "disputed",
            "note": note[:180],
            "dispute_type": dispute_type,
        }, actor=f"居民(尾号{ticket.phone[-4:]})", at=now)

    db.session.add(AuditLog(
        actor=f"居民(尾号{ticket.phone[-4:]})", agent="arbitration",
        action="confirm" if ok else "dispute",
        input_digest=f"诉求#{ticket.id}",
        output_digest=("已确认解决" if ok else f"表示不服：{note[:60]}")[:250]))
    return True, ("已确认解决" if ok else "已提交，居委会将核实"), len(orders)


def pending():
    """待裁决清单：居民表示不服、尚未有裁决结论的单

    这是居委会的"仲裁台"入口。为什么不是"所有 disputed 单"：
    已经判过的单（verdict 非空）仍留在 disputed 状态——因为居民从未
    改口说"解决了"，把他说成已解决是不诚实的。所以列表要按 verdict 区分
    "待裁"和"已裁待复"。
    """
    return (WorkOrder.query
            .filter(WorkOrder.confirm_status == "disputed", _null_verdict())
            .order_by(WorkOrder.confirm_at.asc()))


def _null_verdict():
    """`verdict` 为空串或 NULL 都算"未裁决"

    SQL 里 `verdict != ''` 对 NULL 求值为 NULL，会把老库里的行整批筛掉——
    这是本项目反复踩的同一个坑（见 docs/26「NULL 不等于空串」）。
    """
    from sqlalchemy import or_
    return or_(WorkOrder.verdict.is_(None), WorkOrder.verdict == "")


def stats():
    """仲裁概览：待裁、已裁、驳回率

    驳回率是"三方治理"最有信息量的一个数：它同时反映物业履约质量
    和居委会的裁决尺度。但样本少时不要当趋势讲（docs/06 的口径要求）。

    每个数都**单独 count**，不用"总数减已裁"倒推待裁——
    倒推的错误不会报错，只会在某个中间状态对不上时静默偏移
    （docs/14 的教训：数字与明细必须各自同源可查）。
    """
    from sqlalchemy import func
    def cnt(*conds):
        return db.session.query(func.count(WorkOrder.id)).filter(*conds).scalar() or 0

    done_total = cnt(WorkOrder.status == "done")
    disputed = cnt(WorkOrder.confirm_status == "disputed")
    judged = cnt(WorkOrder.verdict.isnot(None), WorkOrder.verdict != "")
    rejected = cnt(WorkOrder.verdict == "property_fault")
    waiting = cnt(WorkOrder.confirm_status == "disputed", _null_verdict())
    return {
        "done_total": done_total,
        "disputed": disputed,
        "pending": waiting,
        "judged": judged,
        "rejected": rejected,
        # 分母用"已裁决"而不是"已办结"：驳回率问的是"判了的里面有多少判物业输"，
        # 拿全部办结单做分母会把大量从未起争议的单算进去，比值低到没有意义。
        "reject_rate": round(rejected * 100.0 / judged, 1) if judged else None,
    }
