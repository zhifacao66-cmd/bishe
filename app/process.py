"""流程挖掘：从工单流转日志里算"卡在哪"

数据来源只有一张表 `work_order_log`（orderflow.push 写的，只追加不改），
所以这里做的是**事后分析**，不参与业务决策，也不改任何状态。

## 三个必须先讲清的能力边界

**一、停留时长用相邻日志差，不用工单字段**

`order.assigned_at` 只记"最后一次被接单"的时刻，转派一次就被覆盖。
想算"在这位维修工手上待了多久"，字段法直接失效——而转派恰恰是最该看的环节。
所以一律按 order_id 分组、按时间排序、取相邻两条的差：

    dispatch → accept     响应时长（派出去多久有人接）
    accept   → finish     处理时长（接手后多久办完）
    done     → reject     居民反馈时长（办完多久后不服）
    doing    → transfer   停留时长（在一个人手上压了多久）

**二、分位数要门槛，样本不够就不出**

`P90` 在 n=1 时等于那一条的值，会印成"P90 瓶颈 4.2 小时"，
看起来像个统计指标，实际上只是把一条数据换了个名字。
`MIN_N_FOR_P90 = 5`：低于这个数返回 `None`，页面显示"样本不足"。
`n < 3` 连中位数都不出（三个数以下的中位数没有抗噪意义）。

**三、异常检测走业务规则，不上模型**

用户要的"异常检测"，在这份数据上不能做统计意义上的异常检测：
18 张工单、46 条日志，Isolation Forest 会把这些点判成"全部正常"
或"全部异常"，取决于 contamination 怎么设，而且**无法解释给物业听**。
真到了要跟物业对簿的时候，"模型说这单异常"没有说服力，
"这单超 SLA 4.2 倍、转过 3 次手、被驳回 1 次"才有。

所以 `anomaly()` 是规则引擎，每条命中都给出人话原因。
这条路的好处是：可解释、可复现、可增补、不依赖样本量。
"""
from datetime import datetime, timedelta

from app.extensions import db
from app.models import WorkOrder, WorkOrderLog

# 分位数门槛。低于这个样本量不出 P90 —— 见模块 docstring 第二条。
MIN_N_FOR_P90 = 5
MIN_N_FOR_MEDIAN = 3

# 动作的中文名。**不从 orderflow.LABEL 引**：那里是"操作口径"（驳回重办），
# 这里是"流程视角"（退回），两者未来可能分叉，混用会让改了业务文案的人
# 意外改掉分析报告里的说法。
STAGE_LABEL = {
    "dispatch": "派单",
    "accept": "接单",
    "transfer": "转派",
    "finish": "办结",
    "reject": "退回重办",
    "uphold": "维持办结",
}

# 长尾的时间门槛（小时）。30 天是社区矛盾处理的通用红线，
# 超过这个时间基本已经进入反复投诉或信访阶段。
LONG_PENDING_DAYS = 30


def _hours(a, b):
    """b - a 的小时数，负数或空返回 None。

    负数不是不可能（造数据时 at 写错、或服务器改时间），但出现负数说明
    数据有问题，宁可返回 None 让它从统计里消失，也不能让它把均值拉低。
    """
    if not a or not b:
        return None
    d = (b - a).total_seconds() / 3600.0
    return None if d < 0 else round(d, 2)


def age_hours(o, now=None):
    """这张单已经消耗的时间（小时）。

    **已办结的单必须算到 done_at，不能算到 now。** 这是本模块最容易写错的
    一处，而且错了不报错、只静默产出离谱结论：一张 9 月 18 日创建、
    9 月 18 日当天就办完的单，若拿 now 去减，会得出"已挂 38 天未办结"，
    于是"长期未结"告警里排满了早就办完的单——真实的那几条反而被挤到后面。

    未办结的单仍在消耗时间，用 now。`done_at` 缺失的老单返回 None，
    宁可这块数据从统计里消失，也不猜一个时刻出来。
    """
    if not o.created_at:
        return None
    if (o.status or "") == "done":
        return _hours(o.created_at, o.done_at) if o.done_at else None
    return _hours(o.created_at, now or datetime.now())


def _stat(values):
    """一组数的分布。返回 None 的字段表示"样本不足，不出这个数"。"""
    vs = sorted(v for v in values if v is not None)
    n = len(vs)
    if not n:
        return {"n": 0, "avg": None, "min": None, "max": None,
                "median": None, "p90": None, "sum": None}
    out = {
        "n": n,
        "avg": round(sum(vs) / n, 2),
        "min": vs[0],
        "max": vs[-1],
        "sum": round(sum(vs), 2),
        "median": None,
        "p90": None,
    }
    if n >= MIN_N_FOR_MEDIAN:
        mid = n // 2
        out["median"] = vs[mid] if n % 2 else round((vs[mid - 1] + vs[mid]) / 2, 2)
    if n >= MIN_N_FOR_P90:
        # 最近秩法：取第 ceil(0.9n) 个。不做线性插值——插值会在小样本上
        # 造出一个库里不存在的时长，汇报时说不清"这条 7.3 小时是哪张单"。
        import math
        out["p90"] = vs[min(n - 1, math.ceil(n * 0.9) - 1)]
    return out


def visible_ids(q=None):
    """可见工单的 id 集合。`q` 是已按 ABAC 收窄过的 query（API 层传）。

    为什么分析层也要收窄：流程监控看的是"谁办得慢"。物业登录进来如果
    能看到全社区的工单流转，等于拿到了同行与业委会的对照数据——这不是
    它该有的视角，也不是《个人信息保护法》意义上"最小必要"。
    所以收窄不是可选项，`q=None` 只是给离线脚本（评测、导出）用的后门。
    """
    q = q if q is not None else WorkOrder.query
    return {x for (x,) in q.with_entities(WorkOrder.id).all()}


def _logs_by_order(order_ids=None):
    """一次查出日志并按单分组。**不要逐单查**：

    流程分析天然要遍历所有单，逐单 `query.filter_by(order_id=...)` 在
    数百单时是数百次查询，而这数据本来一条 SQL 就能拿全。
    """
    q = WorkOrderLog.query
    if order_ids is not None:
        if not order_ids:
            return {}
        # `in_()` 传空集合会生成 `IN ()`，SQLite 语法错误；
        # 上面已经先挡了空集合，这里不会有半截 SQL。
        q = q.filter(WorkOrderLog.order_id.in_(list(order_ids)))
    rows = (q.order_by(WorkOrderLog.order_id, WorkOrderLog.created_at,
                       WorkOrderLog.id).all())
    out = {}
    for r in rows:
        out.setdefault(r.order_id, []).append(r)
    return out


def transitions(order_ids=None):
    """每个"边"的样本：[(from_action, to_action, hours, order_id), ...]

    边是流程挖掘的基本单位——只看"接单到办结平均 5 小时"会掩盖
    "有一单压了 30 天、其余都是 1 小时"这种情况；分开看每条边的分布
    才能指出到底哪一步在拖。
    """
    out = []
    for oid, logs in _logs_by_order(order_ids).items():
        for prev, cur in zip(logs, logs[1:]):
            h = _hours(prev.created_at, cur.created_at)
            if h is None:
                continue
            out.append((prev.action, cur.action, h, oid))
    return out


def stage_stats(order_ids=None):
    """按边聚合的停留时长表。前端直接渲染这张表就是"瓶颈排行"。"""
    buckets = {}
    for fa, ta, h, _ in transitions(order_ids):
        buckets.setdefault((fa, ta), []).append(h)
    rows = []
    for (fa, ta), hs in buckets.items():
        s = _stat(hs)
        rows.append({
            "from": fa, "to": ta,
            "from_label": STAGE_LABEL.get(fa, fa),
            "to_label": STAGE_LABEL.get(ta, ta),
            "label": f"{STAGE_LABEL.get(fa, fa)} → {STAGE_LABEL.get(ta, ta)}",
            **s,
            # 供前端判断"这一行能不能显示中位数/P90"
            "p90_ready": s["n"] >= MIN_N_FOR_P90,
        })
    rows.sort(key=lambda x: (x["avg"] or 0), reverse=True)
    return rows


def bottleneck(top=3, order_ids=None):
    """最慢的几条边。

    只返回 `n >= MIN_N_FOR_MEDIAN` 的边：一条样本的"平均时长"不叫瓶颈，
    叫个案。前端会同时显示 n，让看的人自己判断可信度。
    """
    rows = [r for r in stage_stats(order_ids) if r["n"] >= MIN_N_FOR_MEDIAN]
    return rows[:top]


def path_stats(order_ids=None):
    """流程路径分布：同一组动作序列出现了几次。

    这才是流程挖掘的正统输出（process variant analysis）：
    18 张单里如果出现 11 条不同路径，说明流程根本没有标准化，
    比"平均时长 6 小时"这种数字重要得多。
    """
    paths = {}
    for oid, logs in _logs_by_order(order_ids).items():
        seq = " → ".join(STAGE_LABEL.get(x.action, x.action) for x in logs)
        paths.setdefault(seq, []).append(oid)
    total = sum(len(v) for v in paths.values()) or 1
    out = [{
        "path": k, "count": len(v), "ratio": round(len(v) / total, 4),
        "order_ids": v,
    } for k, v in paths.items()]
    out.sort(key=lambda x: x["count"], reverse=True)
    return out


def stuck(now=None, order_ids=None):
    """当前卡住的单：还没办结，且从最后一次动作算起已经很久。

    注意基准是**最后一次动作**，不是 created_at。一张单昨天刚被转派，
    虽然创建于 40 天前，但它是"正在推进"的，不该和"20 天没人碰"并列。
    这类单子单列在 `idle` 字段里，用来识别"看起来在转、其实在空转"。
    """
    now = now or datetime.now()
    logs = _logs_by_order(order_ids)
    q = WorkOrder.query.filter(WorkOrder.status != "done")
    if order_ids is not None:
        if not order_ids:
            return []
        q = q.filter(WorkOrder.id.in_(list(order_ids)))
    out = []
    for o in q.all():
        ls = logs.get(o.id) or []
        last = ls[-1].created_at if ls else o.created_at
        idle = _hours(last, now)
        age = age_hours(o, now)
        # 超期倍数：已经超过 SLA 多少倍。1 倍 = 刚好到期。
        # 用倍数而不是"超了几小时"，因为 24h 单超 6 小时和 72h 单超 6 小时的
        # 严重程度差三倍，绝对小时数看不出来。
        over = None
        if age is not None and o.sla_hours:
            over = round(age / o.sla_hours, 2)
        out.append({
            "id": o.id, "order_no": o.order_no,
            "status": o.status, "property_co": o.property_co or "",
            "assignee": o.assignee or "",
            "sla_hours": o.sla_hours or 24,
            "sla_level": o.sla_level or "",
            "created_at": o.created_at.strftime("%Y-%m-%d %H:%M") if o.created_at else "",
            "last_action": ls[-1].action if ls else "",
            "last_action_label": STAGE_LABEL.get(ls[-1].action, "") if ls else "未派单",
            "last_at": last.strftime("%Y-%m-%d %H:%M") if last else "",
            "idle_hours": idle,
            "age_hours": age,
            "over_ratio": over,
            # 没人接单 vs 接了不办 vs 办了没结，三种卡法处理方式完全不同
            "stage": _stuck_stage(o, ls),
            "transfers": sum(1 for x in ls if x.action == "transfer"),
        })
    out.sort(key=lambda x: (x["idle_hours"] or 0), reverse=True)
    return out


def _stuck_stage(o, logs):
    """卡在哪一环。这个名字直接进页面，所以用人话。"""
    acts = [x.action for x in logs]
    if o.status == "open" and "accept" not in acts:
        return "无人接单"
    if "transfer" in acts and acts[-1] == "transfer":
        return "转派后无人接手"
    if o.status == "doing":
        return "处理中未办结"
    return "状态异常"


# 异常规则。每条是 (code, 判定函数, 建议动作)。
#
# 判定函数返回 **命中的说明字符串**，不命中返回 None。
# 为什么不是返回 bool + 固定标题：超期这一件事有 1 倍和 3 倍两档，
# 用两条 bool 规则会同时命中，页面上就出现"超期 3 倍以上、超期 1 倍以上"
# 这种一眼就知道是代码没想清楚的输出。让规则自己给出档位，一条规则一个标签。
#
# 为什么是规则而不是模型：见模块 docstring 第三条。这里再补一条更实际的
# 理由——这些规则**本身就是管理动作**。"转派超过 2 次要督办"是一条制度，
# 用模型算出来一个 0.87 的分，制度上没法落地（谁去解释 0.87 是什么）。
def _hit_over_sla(o, s, t):
    r = s.get("over_ratio") or 0
    if r >= 3:
        return "超期 3 倍以上"
    if r >= 1:
        return "超期 1 倍以上"
    return None


def _hit_transfer(o, s, t):
    if t >= 3:
        return f"转派 {t} 次"
    if t >= 2:
        return "转派 2 次"
    return None


def _hit_long_pending(o, s, t):
    # 已办结的单不可能"长期未结"。漏了这个判断，一条 9 月 18 日当天办完
    # 的单会被算成"已挂 38 天"，把真正没办的单挤出告警榜。
    if (o.status or "") == "done":
        return None
    h = s.get("age_hours") or 0
    return f"超过 {LONG_PENDING_DAYS} 天未结" if h > LONG_PENDING_DAYS * 24 else None


def _hit_idle(o, s, t):
    if (o.status or "") == "done":
        return None
    h = s.get("idle_hours") or 0
    if h > 7 * 24:
        return f"{h / 24:.0f} 天无任何动作"
    return None


def _hit_rejected(o, s, t):
    n = s.get("reject_count") or 0
    return f"已被驳回 {n} 次" if n else None


def _hit_disputed(o, s, t):
    if (o.confirm_status == "disputed") and not (o.verdict or ""):
        return "居民不服未裁决"
    return None


ANOMALY_RULES = [
    ("over_sla", _hit_over_sla, "立即督办，要求物业书面说明"),
    ("transfer", _hit_transfer, "核查是否在推诿，必要时由居委会指定责任单位"),
    ("long_pending", _hit_long_pending, "提交居委会议事，纳入矛盾化解台账"),
    ("idle", _hit_idle, "确认责任人是否知情"),
    ("rejected", _hit_rejected, "跟踪返工质量，避免二次争议"),
    ("disputed", _hit_disputed, "居委会 3 个工作日内组织裁决"),
]


def anomaly(limit=None, order_ids=None):
    """异常工单清单。每条带命中原因与建议动作。

    `s` 是 `stuck()` 里那一行（未结单）或现算的一行（已结单），
    已结单也要能查——"办完之后被居民投诉"恰恰是最需要复盘的一类。
    """
    from app.agents.supervise import SuperviseAgent
    stuck_map = {x["id"]: x for x in stuck(order_ids=order_ids)}
    logq = WorkOrderLog.query
    if order_ids is not None:
        logq = logq.filter(WorkOrderLog.order_id.in_(list(order_ids) or [0]))
    reject_map = dict(
        logq.with_entities(WorkOrderLog.order_id, db.func.count())
        .filter(WorkOrderLog.action == "reject")
        .group_by(WorkOrderLog.order_id).all())
    trans_map = dict(
        logq.with_entities(WorkOrderLog.order_id, db.func.count())
        .filter(WorkOrderLog.action == "transfer")
        .group_by(WorkOrderLog.order_id).all())

    oq = WorkOrder.query
    if order_ids is not None:
        if not order_ids:
            return []
        oq = oq.filter(WorkOrder.id.in_(list(order_ids)))
    out = []
    for o in oq.all():
        age = age_hours(o)
        s = stuck_map.get(o.id) or {
            "idle_hours": None,
            "age_hours": age,
            "over_ratio": (round(age / (o.sla_hours or 24), 2)
                           if age is not None else None),
            "reject_count": reject_map.get(o.id, 0),
        }
        s["reject_count"] = reject_map.get(o.id, 0)
        t = trans_map.get(o.id, 0)
        hits = []
        for code, fn, advice in ANOMALY_RULES:
            label = fn(o, s, t)
            if label:
                hits.append({"code": code, "name": label, "advice": advice})
        if not hits:
            continue
        out.append({
            "id": o.id, "order_no": o.order_no, "status": o.status,
            "property_co": o.property_co or "", "assignee": o.assignee or "",
            "content": (o.content or "")[:60],
            "overdue": SuperviseAgent.is_overdue(o),
            "over_ratio": s.get("over_ratio"),
            "age_hours": s.get("age_hours"),
            "idle_hours": s.get("idle_hours"),
            "transfers": t,
            "reject_count": s["reject_count"],
            "dispute_type": o.dispute_type or "",
            "confirm_status": o.confirm_status or "",
            "reasons": hits,
            "reason_text": "、".join(h["name"] for h in hits),
        })
    # 命中条数多的在前，同分按超期倍数
    out.sort(key=lambda x: (-len(x["reasons"]), -(x["over_ratio"] or 0)))
    return out[:limit] if limit else out


def summary(now=None, order_ids=None):
    """监控台顶部卡片。**每个数单独查**，不做减法倒推。

    倒推的错误不报错、只静默偏移：`total - done - open` 在状态出现
    第四个值（比如未来加了 canceled）时会悄悄算错，而页面看着完全正常。
    """
    now = now or datetime.now()
    st = stage_stats(order_ids)
    oq = WorkOrder.query
    if order_ids is not None:
        oq = oq.filter(WorkOrder.id.in_(list(order_ids) or [0]))
    total_orders = oq.count()
    total_logs = (WorkOrderLog.query.filter(WorkOrderLog.order_id.in_(list(order_ids))).count()
                  if order_ids is not None else WorkOrderLog.query.count())
    s2 = stuck(now=now, order_ids=order_ids)
    an = anomaly(order_ids=order_ids)
    return {
        "orders": total_orders,
        "logs": total_logs,
        # 有日志的单数：可能小于总单数（某些单只落库没走流程）
        "orders_with_log": len(_logs_by_order(order_ids)),
        "paths": len(path_stats(order_ids)),
        "open": oq.filter_by(status="open").count(),
        "doing": oq.filter_by(status="doing").count(),
        "done": oq.filter_by(status="done").count(),
        "stuck": len(s2),
        "no_accept": sum(1 for x in s2 if x["stage"] == "无人接单"),
        "overdue": sum(1 for x in s2 if (x["over_ratio"] or 0) >= 1),
        "anomaly": len(an),
        "long_pending": sum(1 for x in s2
                            if (x["age_hours"] or 0) > LONG_PENDING_DAYS * 24),
        "avg_edges": len(st),
        # 样本量声明：页面必须显示，避免把 n=3 的均值当区间估计用
        "sample_note": (
            f"当前样本：{total_orders} 张工单 / {total_logs} 条流转日志。"
            f"分组后多数边 n<{MIN_N_FOR_P90}，P90 与中位数不出（显示「样本不足」）。"
        ),
        "min_n_p90": MIN_N_FOR_P90,
    }
