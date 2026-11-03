"""长尾风险预警：把注意力从"平均工单"挪到"最坏的那几件"

## 为什么这个模块比统计报表更重要

平均工单时长、平均办结率这类数字，在社区治理里几乎不影响任何人的工作：
一张单拖 3 天还是 5 天，居民一般不会闹；一张单拖 40 天、报了 3 次、
转了 4 手，就是信访和 12345 的源头。
**真正的成本集中在尾部，而平均值永远看不见尾部。**

所以这个模块只干一件事：把尾部挑出来，并按"可能激化的程度"排序。

## 三个信号

**反复投诉**——同一个手机号（或同一地址）多次报同一类事。
一次是偶发，两次是没解决，三次是"上次那个答复我不认"。
系统里重复报事不是数据冗余，是**上一轮处理失败的证据**。

**踢皮球**——转派 ≥2 次，或转派跨了单位。
关键是"跨单位"：物业内部换个人修，是正常工作分配；
从物业转到居委再转回物业，是责任边界在打转。
`actor_org` 变化次数才是皮球的核心指标，单纯 transfer 次数会误伤。

**长期未结**——超过 30 天还没办结。这条单列，因为它的性质变了：
已经不是"效率问题"，而是"矛盾积累"。

## 风险分的定位：排序工具，不是概率

`score()` 输出 0-100，**不是"87% 会激化"**。它的唯一用途是决定
"这 20 件里先看哪 5 件"。所以：

- 权重是**拍的经验值**，不是训练出来的。写清楚比装作有依据更可信。
- `explain()` 必须能把分拆开。一个说不清来历的分数，社区不敢照着它行动。
- 分数不跨批次比较。今天 80 分和上个月 80 分不一定同义（规则变过）。
  要追踪单件风险的变化，看 `items` 里的绝对值，不看分数。
"""
from datetime import datetime, timedelta

from app.extensions import db
from app.models import ResidentTicket, WorkItem, WorkOrder, WorkOrderLog

# 30 天。与 app/process.py:LONG_PENDING_DAYS 同一个数，但**刻意分两处定义**：
# 流程模块用它做"超长时间"分类，风险模块用它算分。将来如果制度把红线
# 改成 20 天，两处的处理方式可能不同（一个改分类、一个改权重），
# 强行合成一个常量反而会绑住手脚。这里用注释保证改一处时能看到另一处。
LONG_PENDING_DAYS = 30

# 风险权重。**经验值，不是学习出来的**——这句话要留在代码里，
# 因为答辩一定会问"权重怎么定的"。
#   反复报事每次 +18：第三次报事已经是很强的信号，54 分起步
#   跨单位转派每次 +22：皮球是最伤信任的，权重给最高
#   同单位转派每次 +8：正常轮换，象征性计分
#   超期 1 倍 +15 / 3 倍再 +20：先超期后严重超期，递增
#   长期未结 +25：性质质变
#   被驳回 +20：说明办过一次没办对
#   居民不服未裁 +18：仲裁缺位，最直接的不满来源
W_REPEAT = 18
W_CROSS_ORG_TRANSFER = 22
W_SAME_ORG_TRANSFER = 8
W_OVER_SLA = 15
W_OVER_SLA_3X = 20
W_LONG_PENDING = 25
W_REJECTED = 20
W_DISPUTED_UNJUDGED = 18

# 分数封顶。超过 100 没有意义——用户只会看到一堆 100 分并列，排序失效。
SCORE_CAP = 100


def _hours(a, b):
    if not a or not b:
        return None
    d = (b - a).total_seconds() / 3600.0
    return None if d < 0 else round(d, 2)


def _norm_phone(p):
    """手机号归一，只为比对。不脱敏（这一步在内存里，不落库、不出接口）。

    带空格、带横线、+86 前缀的同一个号，会被当成两个人——
    这正是"反复投诉"最难发现的原因：居民换个写法重报一次，
    系统就看不见它和上一条是同一个人。
    """
    s = "".join(ch for ch in (p or "") if ch.isdigit())
    if s.startswith("86") and len(s) == 13:
        s = s[2:]
    return s


def _norm_addr(a):
    """地址归一：去掉空白与常见修饰前缀，只留"楼栋门牌"这类可比部分。

    归一化只做最保守的两件事（去空白、去"社区/小区"前缀），
    不做同义词替换——"3栋201"和"三栋201"在这里仍然算两个地址。
    宁可少合并（漏报一次），不要错合并（把两户人家的事算成一件）。
    """
    s = "".join((a or "").split())
    for pre in ("幸福社区", "幸福小区", "本社区", "小区"):
        if s.startswith(pre):
            s = s[len(pre):]
    return s


def repeat_complainants(min_times=2):
    """反复投诉的人。按手机号分组，同时给出每条诉求的时间线。

    `min_times=2` 就报，不等到 3 次——第二次报同一件事，已经说明
    第一次的处理没让居民接受。等他报第三次才干预，就已经晚了。
    """
    tiles = ResidentTicket.query.order_by(ResidentTicket.created_at).all()
    buckets = {}
    for t in tiles:
        key = _norm_phone(t.phone) or _norm_addr(t.address)
        if not key:
            continue
        buckets.setdefault(key, []).append(t)

    out = []
    for key, group in buckets.items():
        if len(group) < min_times:
            continue
        # 是否"同一件事"：地址归一后相同，或原文有较长公共前缀。
        # 这里用最松的判据（同地址即算），因为漏报比误报代价大得多，
        # 而页面会同时显示每个人报的具体内容和时间，人工一眼能分辨。
        same_addr = len({_norm_addr(t.address) for t in group}) == 1
        first = min(t.created_at for t in group)
        last = max(t.created_at for t in group)
        span = _hours(first, last)
        out.append({
            "key": key,
            "phone_masked": _mask_phone(group[0].phone),
            "name": group[0].name or "",
            "address": group[0].address or "",
            "times": len(group),
            "same_address": same_addr,
            "first_at": first.strftime("%Y-%m-%d") if first else "",
            "last_at": last.strftime("%Y-%m-%d") if last else "",
            "span_days": round((span or 0) / 24, 1),
            "unresolved": sum(1 for t in group if (t.status or "") != "done"),
            "lines": sorted({x for t in group for x in (t.line_summary or "").split("/") if x}),
            "tickets": [{
                "id": t.id, "code": t.code,
                "text": (t.raw_text or "")[:120],
                "status": t.status or "",
                "at": t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "",
            } for t in group],
        })
    out.sort(key=lambda x: (-x["times"], -x["span_days"]))
    return out


def _mask_phone(p):
    """展示用打码。接口出的是打码值——预警台是内部页面，
    但"内部"不等于"可以到处放明文手机号"，脱敏口径要一致。"""
    s = _norm_phone(p)
    return s[:3] + "****" + s[-4:] if len(s) == 11 else (s[:3] + "***" if s else "")


def ping_pong(min_transfers=2, order_ids=None):
    """踢皮球的单：转派多次，重点是**跨单位**。

    跨单位转派的判定：相邻两次转派的 `actor_org` 不同。
    只数 transfer 次数会误伤——维修工请假、交接班都会转派，
    那是一个单位内部的工作分配，不是推诿。
    """
    q = WorkOrderLog.query
    if order_ids is not None:
        if not order_ids:
            return []
        q = q.filter(WorkOrderLog.order_id.in_(list(order_ids)))
    logs = q.order_by(WorkOrderLog.order_id, WorkOrderLog.created_at,
                      WorkOrderLog.id).all()
    by_order = {}
    for r in logs:
        by_order.setdefault(r.order_id, []).append(r)

    out = []
    for oid, ls in by_order.items():
        trans = [x for x in ls if x.action == "transfer"]
        if len(trans) < 1:
            continue
        orgs = [t.actor_org or "" for t in trans]
        cross = sum(1 for a, b in zip(orgs, orgs[1:]) if a and b and a != b)
        if len(trans) < min_transfers and not cross:
            continue
        o = db.session.get(WorkOrder, oid)
        if not o:
            continue
        out.append({
            "id": o.id, "order_no": o.order_no,
            "content": (o.content or "")[:80],
            "status": o.status or "",
            "transfers": len(trans),
            "cross_org": cross,
            "orgs": sorted({x for x in orgs if x}),
            "chain": [{
                "at": x.created_at.strftime("%Y-%m-%d %H:%M") if x.created_at else "",
                "from_org": x.actor_org or "",
                "detail": (x.detail or "")[:60],
            } for x in trans],
            "reject_count": sum(1 for x in ls if x.action == "reject"),
            "age_days": round((_hours(o.created_at, datetime.now()) or 0) / 24, 1),
        })
    out.sort(key=lambda x: (-x["cross_org"] * 100 - x["transfers"]))
    return out


def long_pending(days=LONG_PENDING_DAYS, order_ids=None):
    """超过 N 天还没办结的单。"""
    cutoff = datetime.now() - timedelta(days=days)
    q = (WorkOrder.query
         .filter(WorkOrder.status != "done",
                 WorkOrder.created_at < cutoff))
    if order_ids is not None:
        if not order_ids:
            return []
        q = q.filter(WorkOrder.id.in_(list(order_ids)))
    rows = q.order_by(WorkOrder.created_at.asc()).all()
    out = []
    for o in rows:
        age = _hours(o.created_at, datetime.now())
        out.append({
            "id": o.id, "order_no": o.order_no,
            "content": (o.content or "")[:80],
            "status": o.status or "", "property_co": o.property_co or "",
            "assignee": o.assignee or "",
            "created_at": o.created_at.strftime("%Y-%m-%d") if o.created_at else "",
            "age_days": round((age or 0) / 24, 1),
            "over_ratio": round((age or 0) / (o.sla_hours or 24), 2),
        })
    return out


def explain(order, transfers=None, reject_count=None, repeat_times=0):
    """把一张单的风险分拆成"分项 + 分数 + 理由"。

    拆开是本模块最重要的设计：社区要拿这张表去跟物业谈，
    必须能说"这 40 分是三次转派给的，不是我随口定的"。
    """
    now = datetime.now()
    trans = transfers or []
    cross = 0
    orgs = [t.actor_org or "" for t in trans]
    for a, b in zip(orgs, orgs[1:]):
        if a and b and a != b:
            cross += 1
    same = max(0, len(trans) - cross)
    age_h = _hours(order.created_at, now) or 0
    ratio = (age_h / (order.sla_hours or 24)) if order.sla_hours else 0
    rj = reject_count or 0
    is_open = (order.status or "") != "done"

    items = []
    if repeat_times >= 2:
        items.append(("反复报事", W_REPEAT * min(repeat_times - 1, 3),
                      f"同一手机号/地址累计报事 {repeat_times} 次"))
    if cross:
        items.append(("跨单位转派", W_CROSS_ORG_TRANSFER * cross,
                      f"在 {len({x for x in orgs if x})} 个单位之间转了 {cross} 次"))
    if same:
        items.append(("同单位转派", W_SAME_ORG_TRANSFER * same,
                      f"单位内部转派 {same} 次（正常轮换，计分较低）"))
    if ratio >= 3:
        items.append(("严重超期", W_OVER_SLA + W_OVER_SLA_3X,
                      f"已耗时 {age_h:.1f}h，是 SLA({order.sla_hours}h) 的 {ratio:.1f} 倍"))
    elif ratio >= 1:
        items.append(("超期", W_OVER_SLA,
                      f"已耗时 {age_h:.1f}h，超过 SLA({order.sla_hours}h)"))
    if is_open and age_h > LONG_PENDING_DAYS * 24:
        items.append(("长期未结", W_LONG_PENDING,
                      f"已挂 {age_h / 24:.1f} 天仍未办结"))
    if rj:
        items.append(("被驳回重办", W_REJECTED * min(rj, 2),
                      f"居委判定物业责任，退回 {rj} 次"))
    if (order.confirm_status == "disputed") and not (order.verdict or ""):
        items.append(("居民不服待裁", W_DISPUTED_UNJUDGED,
                      "居民已表态不服，尚无裁决结论"))

    raw = sum(x[1] for x in items)
    score = min(SCORE_CAP, raw)
    return {
        "score": score,
        "raw": raw,
        # 封顶了要标出来：不然两个都是 100 分的单会被认为"同样严重"，
        # 而它们的原始分可能是 100 和 160。
        "capped": raw > SCORE_CAP,
        "items": [{"name": n, "score": s, "reason": r} for n, s, r in items],
        "level": ("高危" if score >= 60 else "关注" if score >= 35 else "正常"),
    }


def warnings(limit=20, order_ids=None):
    """风险清单：把三类信号合成一张按分数排序的表。

    同一张单可能同时命中"转派多"和"长期未结"，这里必须**合并成一行**，
    不能出现两条。合并口径：以工单为主体（一张单一行），
    反复报事作为附加项挂上去——因为反复报事是按人算的，一张单本身
    "不反复"，但它是反复链条上的一环。
    """
    logq = WorkOrderLog.query
    if order_ids is not None:
        logq = logq.filter(WorkOrderLog.order_id.in_(list(order_ids) or [0]))
    trans_map = {}
    for r in (logq.filter(WorkOrderLog.action == "transfer")
              .order_by(WorkOrderLog.created_at).all()):
        trans_map.setdefault(r.order_id, []).append(r)
    reject_map = dict(
        logq.with_entities(WorkOrderLog.order_id, db.func.count())
        .filter(WorkOrderLog.action == "reject")
        .group_by(WorkOrderLog.order_id).all())
    # 诉求 → 反复次数：按归一手机号
    rep = {x["key"]: x["times"] for x in repeat_complainants()}
    # 工单 → 诉求的链路是 **两跳**：WorkOrder.work_item_id → WorkItem.fde_id
    # → ResidentTicket.fde_id。早先这里直接拿 work_item_id 去 fde 表里查，
    # 两者 id 空间不同，能"查得到"纯属 id 数字巧合，查到的是别人的诉求——
    # 这类错不会报错，只会让反复报事的次数算在不相干的单上。
    item_fde = {wi.id: wi.fde_id for wi in
                WorkItem.query.with_entities(WorkItem.id, WorkItem.fde_id).all()}
    ticket_by_fde = {}
    for t in ResidentTicket.query.all():
        if t.fde_id:
            ticket_by_fde.setdefault(t.fde_id, []).append(t)

    oq = WorkOrder.query
    if order_ids is not None:
        if not order_ids:
            return []
        oq = oq.filter(WorkOrder.id.in_(list(order_ids)))
    rows = []
    for o in oq.all():
        # 这张单所属诉求的报事次数（一个诉求可能拆多张单，只取最大次数，
        # 不能累加——累加会把"同一诉求拆成 3 张单"算成"报了 3 次"）
        times = 0
        for t in ticket_by_fde.get(item_fde.get(o.work_item_id), []) or []:
            times = max(times, rep.get(_norm_phone(t.phone), 0),
                        rep.get(_norm_addr(t.address), 0))
        ex = explain(o, trans_map.get(o.id, []), reject_map.get(o.id, 0), times)
        if not ex["items"]:
            continue
        rows.append({
            "id": o.id, "order_no": o.order_no,
            "content": (o.content or "")[:70],
            "status": o.status or "", "property_co": o.property_co or "",
            "assignee": o.assignee or "",
            "created_at": o.created_at.strftime("%Y-%m-%d") if o.created_at else "",
            **ex,
        })
    rows.sort(key=lambda x: -x["score"])
    return rows[:limit]


def summary(order_ids=None, with_people=True):
    """预警台顶部卡片。

    `with_people=False` 时不统计"反复投诉"——那是按居民手机号聚合的，
    物业不该看到"某户居民报了几次事"，这属于居民画像。
    """
    w = warnings(limit=10_000, order_ids=order_ids)
    rp = repeat_complainants() if with_people else []
    pp = ping_pong(order_ids=order_ids)
    lp = long_pending(order_ids=order_ids)
    return {
        "watched": len(w),
        "high": sum(1 for x in w if x["level"] == "高危"),
        "watch": sum(1 for x in w if x["level"] == "关注"),
        "repeat_people": len(rp),
        "repeat_tickets": sum(x["times"] for x in rp),
        "ping_pong": len(pp),
        "cross_org_orders": sum(1 for x in pp if x["cross_org"] > 0),
        "long_pending": len(lp),
        "tickets": ResidentTicket.query.count() if with_people else 0,
        "orders": (WorkOrder.query.filter(WorkOrder.id.in_(list(order_ids))).count()
                   if order_ids is not None else WorkOrder.query.count()),
        # 样本声明，跟着卡片一起出。预警台最怕"看着像有结论，其实没样本"
        "sample_note": (
            f"样本：{(WorkOrder.query.filter(WorkOrder.id.in_(list(order_ids))).count() if order_ids is not None else WorkOrder.query.count())}"
            f" 张工单 / {ResidentTicket.query.count()} 条居民诉求。"
            "反复投诉与文本热点需要 ≥20 条诉求才具备统计意义，当前仅作为个案线索。"
        ),
    }
