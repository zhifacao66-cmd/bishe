"""FDE 首录接口"""
from datetime import datetime, time as dtime, timedelta

from flask import Blueprint, request
from sqlalchemy import func

from app.api import ok, fail
from app.agents.orchestrator import Orchestrator
from app.auth import current_user, perm_required
from app.extensions import db
# 业务项进度的判据统一在 app/itemstate.py：只看工单会漏掉"只写台账"的
# 民政/综治线，出现"居民端已办结、首录台仍待派发"的自相矛盾。
from app.itemstate import STAGE_STAFF, stage as item_stage

bp = Blueprint("fde", __name__)
orch = Orchestrator()

# 业务线顺序与拆解枚举一致（app/agents/parser.py:LINES）。
# 图表顺序写死在这里而不是"按数量降序"：同一业务线在今天和周报截图里
# 停在同一个位置，看图的人不用每次重新找。
LINE_ORDER = ("物业", "综治", "民政", "卫健", "街道")

# 「节省工时」的两个基准（分钟/次）。放在后端而不是模板里：
# 口径要改（比如访谈拿到真实均值）时只改这一处，页面文案与答辩口径同时生效。
MIN_PER_ITEM = 6      # 手工登记一条业务项并分派：填表、喊人、记本子
MIN_PER_REUSE = 10    # 复用历史档案免一次重复采集：翻档、打电话核对

# 业务项进度的判据统一在 app/itemstate.py。
def _by_day(model, start, end):
    """按自然日计数：{ '2026-10-19': 4, ... }

    走服务端 GROUP BY 而不是把行拉回来数——页面上的曲线和明细表用的是
    同一批 created_at，统计与明细不会各算各的。
    created_at 为 NULL 的老数据落不进任何一天（SQL 里 NULL >= x 是 NULL），
    这类行本来也没有"发生了什么时间"，计入某天反而是编数据。
    """
    rows = (db.session.query(func.date(model.created_at), func.count(model.id))
            .filter(model.created_at >= start, model.created_at < end)
            .group_by(func.date(model.created_at)).all())
    return {str(d): int(c) for d, c in rows if d}


def _today_rows(today_start):
    """今日录入的业务项；今天还没录过就退回最近几条，并如实告诉前端。

    返回 (要显示的行, 是否回退, 状态分布)。
    状态分布算的是**今日全部**事项，不是这 8 行：卡片上写"今日 N 项"，
    下面的分布却只数了屏上的 8 条，两处对不上，看一眼就露馅。
    """
    from app.models import Ledger, WorkItem, WorkOrder

    all_items = (WorkItem.query.filter(WorkItem.created_at >= today_start)
                 .order_by(WorkItem.id.desc()).all())
    fallback = False
    if not all_items:
        all_items = WorkItem.query.order_by(WorkItem.id.desc()).limit(6).all()
        fallback = True
    show = all_items[:8]

    # 工单/台账一次性批量取，别在循环里逐条查（8 条就是 8 次往返）。
    # 一条业务项理论上可能有多张工单（转派不产生新单，但历史数据存在），
    # 所以存成列表，判状态时看**全部**单子而不是最后一张。
    ids = [i.id for i in all_items]
    orders, filed = {}, set()
    if ids:
        for o in WorkOrder.query.filter(WorkOrder.work_item_id.in_(ids)).all():
            orders.setdefault(o.work_item_id, []).append(o)
        filed = {wid for (wid,) in db.session.query(Ledger.work_item_id)
                 .filter(Ledger.work_item_id.in_(ids)).distinct().all()}

    summary = {"待派发": 0, "已派单": 0, "办理中": 0, "已完成": 0}
    rows = []
    for it in all_items:
        st = item_stage(it, orders=orders.get(it.id, []), filed=(it.id in filed))
        status, badge = STAGE_STAFF[st]
        summary[status] = summary.get(status, 0) + 1
        if len(rows) < len(show):
            rows.append({
                "id": it.id,
                "subject": it.subject or "",
                "line": (it.target_lines or "").replace(",", "/"),
                "status": status,
                "badge": badge,
                "created_at": it.created_at.strftime("%m-%d %H:%M") if it.created_at else "",
            })
    return rows, fallback, summary, len(all_items)


@bp.post("")
@perm_required("fde.create")
def submit():
    data = request.get_json(silent=True) or {}
    raw = data.get("raw_text")
    # 类型必须先判：前端传数字/对象时 raw_text.strip() 会抛 AttributeError，
    # 被全局处理器兜成一个 500——用户看到"服务内部错误"，实际只是参数传错。
    if not isinstance(raw, str):
        if raw is None:
            return fail("请填写首录内容")
        return fail("首录内容必须是文本")
    raw = raw.strip()
    if not raw:
        return fail("raw_text 不能为空")
    if len(raw) < 5:
        return fail("内容太短（至少 5 个字），请把时间、地点、诉求说清楚")
    if len(raw) > 2000:
        return fail("首录内容过长（≤2000 字），请分条录入或精简后再提交")
    source = (data.get("source") or "text").strip()
    if source not in ("text", "voice", "image"):
        # 来源只用于统计与展示，但枚举外的值会让报表口径多出一类"幽灵来源"
        return fail("来源只能是 text / voice / image")
    # 记录人一律取会话身份，不信任前端传值（防止冒名录入）
    u = current_user()
    result = orch.handle_fde(raw, source=source, creator=u.name if u else "")
    return ok(result)


def _secure_count():
    """已按二级/三级管控的字段数（field_security 里 level>=2）。

    **必须 join work_item**。2026-10-23 的整体检查查出 15 条孤儿密级：
    一次手工清理（按依赖顺序删 order→ledger→item→fde）漏掉了 field_security，
    剩下的行仍然被 `level >= 2` 数进来，首页"敏感字段管控 46 个"里混着
    15 个已经不存在的业务项 —— 数字虚高，而且拿明细去对永远对不上。
    孤儿由 `scripts/fix_orphans.py` 清理；这里加 join 是为了即使再出现，
    卡片也不会骗人。
    """
    from app.models import FieldSecurity, WorkItem
    # 别写成 `db.session.query(func.count(X)).join(...).count()`：
    # 那样外层 count() 会把这个"已经 SELECT count(*) 的子查询"整体再数一遍行数，
    # 内层没有 GROUP BY ⇒ 永远返回一行 ⇒ 结果恒等于 1（有记录时）或 0。
    # 线上首页真的显示过"敏感字段管控 1 个"，而库里是 43 个。
    return (FieldSecurity.query
            .join(WorkItem, WorkItem.id == FieldSecurity.work_item_id)
            .filter(FieldSecurity.level >= 2).count())


@bp.get("/stats")
@perm_required("fde.view")
def stats():
    from app.models import AuditLog, FdeRecord, WorkItem
    return ok({
        "fde_count": FdeRecord.query.count(),
        "item_count": WorkItem.query.count(),
        "audit_count": AuditLog.query.count(),
        # 已按二级/三级管控的字段数（field_security 表，可查可审计）
        "secure_count": _secure_count(),
    })


@bp.get("/dashboard")
@perm_required("fde.view")
def dashboard():
    """首录台首页的看板数据：统计卡 + 业务线分布 + 处理量趋势 + 今日事项

    为什么单开一个接口而不是模板里塞常量：这三块原本是写死的演示数字
    （12/9/15/5/5、18.5h、五条固定任务），录了十条新事项图表不动一下，
    评委只要问一句"这个 15 是今天的吗"就答不上来。现在全部现查：
      · 柱状图 = 本周 work_item.target_lines 拆分计数
      · 折线   = 本周每天 拆解/派单/台账 三张表的创建数
      · 今日任务 = 今日 work_item + 其工单的流转状态
    一条事项可同时派往多条业务线（target_lines 存 "综治,物业"），
    所以柱状图合计**会大于**事项数——前端 hint 里写明，不让两处对不上。
    """
    from app.models import FdeRecord, Ledger, WorkItem, WorkOrder

    now = datetime.now()
    today = now.date()
    monday = today - timedelta(days=today.weekday())      # 本周一
    week_start = datetime.combine(monday, dtime.min)
    week_end = datetime.combine(today + timedelta(days=1), dtime.min)  # 明天零点，不含

    # —— 统计卡 ——
    items_week = WorkItem.query.filter(WorkItem.created_at >= week_start,
                                       WorkItem.created_at < week_end).count()
    reused_week = Ledger.query.filter(Ledger.reused.is_(True),
                                      Ledger.created_at >= week_start,
                                      Ledger.created_at < week_end).count()
    saved_min = items_week * MIN_PER_ITEM + reused_week * MIN_PER_REUSE

    # —— 业务线分布（本周）——
    # 只取这一列：拆多值字段必须逐行分，但没必要把正文/JSON 一起捞出来
    rows = (db.session.query(WorkItem.target_lines)
            .filter(WorkItem.created_at >= week_start,
                    WorkItem.created_at < week_end).all())
    counts = {k: 0 for k in LINE_ORDER}
    for (raw,) in rows:
        for name in str(raw or "").replace("，", ",").split(","):
            name = name.strip()
            if name in counts:            # 枚举外的历史脏值不计数，也不占位
                counts[name] += 1

    # —— 处理量趋势（本周一 ~ 今天）——
    # 只到今天就停：把周四~周日画成 0 会让曲线末端"看起来在暴跌"
    n_days = today.weekday() + 1
    days = [monday + timedelta(days=i) for i in range(n_days)]
    d_fde = _by_day(WorkItem, week_start, week_end)
    d_ord = _by_day(WorkOrder, week_start, week_end)
    d_led = _by_day(Ledger, week_start, week_end)
    keys = [d.isoformat() for d in days]

    today_rows, fallback, by_status, today_count = _today_rows(
        datetime.combine(today, dtime.min))

    return ok({
        "week": {
            "start": monday.isoformat(),
            "end": today.isoformat(),
            "label": "本周",
        },
        "stats": {
            "fde_count": FdeRecord.query.count(),
            "item_count": WorkItem.query.count(),
            "secure_count": _secure_count(),
            "item_week": items_week,
            "reused_week": reused_week,
            "saved_minutes": saved_min,
            "saved_hours": round(saved_min / 60, 1),
            # 口径随数字一起下发：页面悬浮提示与报表/答辩讲的是同一句话
            "saved_basis": (f"本周拆解 {items_week} 个业务项 × {MIN_PER_ITEM} 分钟"
                            f" + 复用历史档案 {reused_week} 条 × {MIN_PER_REUSE} 分钟"),
        },
        "lines": {"names": list(LINE_ORDER),
                  "counts": [counts[k] for k in LINE_ORDER]},
        "trend": {
            "labels": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][:n_days],
            "dates": keys,
            "parsed": [d_fde.get(k, 0) for k in keys],
            "dispatched": [d_ord.get(k, 0) for k in keys],
            "ledger": [d_led.get(k, 0) for k in keys],
        },
        "today": {
            "fallback": fallback,
            "date": today.isoformat(),
            "count": today_count,
            "by_status": by_status,
            "rows": today_rows,
        },
    })
