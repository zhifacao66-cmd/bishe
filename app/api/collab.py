"""Agent 协作轨迹接口：把"系统内部怎么想的"变成可查的记录

三个端点：
  · /runs   最近的运行列表（一次诉求处理 = 一个 run_id）
  · /trace  单次运行的逐步轨迹 + 冲突消解记录
  · /stats  协作统计：多少单被复核提级、各 Agent 耗时、降级次数

## 为什么要单独一层接口

`audit_log` 里已经有一份"Agent 做过什么"的记录，但它刻意只存 250 字的
输入/输出摘要，而且**它是合规账本**：谁看、留多久、怎么脱敏都有约束。
把拆解过程、密级判定、冲突消解塞进审计表，等于给审计表开一个新泄露面。

轨迹表回答的是另一个问题：**"这个结论是谁定的、依据是什么、有没有人不同意"**。
这个问题只有内部人要问，所以访问权限比审计还窄。

## 为什么物业看不到

轨迹里必然出现：脱敏命中的原文片段类型、字段密级判定、居民是否重复投诉、
历史工单被谁推诿过。这些是"系统对被监督对象的内部判断"。
把判断过程给被判断的一方看，等于把举报人的线索交出去——哪怕内容已经脱敏，
"系统认定这户是反复投诉户"这件事本身就是敏感信息。

所以这里代码硬判角色，不走可配置权限点（同 `/api/insight/topics` 的做法）。
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from flask import Blueprint, request

from app.api import fail, ok
from app.auth import current_user, perm_required
from app.models import AgentStep

bp = Blueprint("collab", __name__)

# 轨迹能看的角色。**必须与页面权限点 `fde.view` 覆盖的角色完全一致**——
# 页面上进得去、接口却 403（或者反过来）这种"半边开"的状态最麻烦：
# 用户看到的是一张空白页，而日志里记的是"放行"。
#
# 为什么没有街道：街道是监督方，走的是 `audit.view`（合规留痕）。
# 轨迹是社区自己的内部复盘记录，两类读者的关注点不同——审计问"这件事
# 发生过吗"，轨迹问"当时为什么这么定"。混在一起会让两边的权限都不好收窄。
# 为什么没有物业/居民：物业是被监督方（见文件头），居民只看结果。
TRACE_ROLES = ("community", "admin")

LEVEL_HOURS = {"紧急": 4, "较急": 24, "一般": 72}


def _can_view(u) -> bool:
    return bool(u) and (u.role or "") in TRACE_ROLES


def _level_of(decision: str) -> str:
    for name in LEVEL_HOURS:
        if (decision or "").startswith(name):
            return name
    return ""


def _int_arg(name, default, lo, hi):
    """安全取整数参数。

    `int(request.args.get("limit", 20))` 在 `?limit=abc` 上直接抛 ValueError
    → 500。分页参数是最常被手工改的东西（用户把 URL 里的 20 改成 abc、
    或者前端拼了个空值），500 会让人以为"整个接口挂了"。
    这里统一兜到默认值，再由 lo/hi 夹在合理区间。
    """
    raw = (request.args.get(name) or "").strip()
    try:
        v = int(raw) if raw else default
    except (TypeError, ValueError):
        v = default
    return max(lo, min(v, hi))


@bp.get("/runs")
@perm_required("fde.view")
def runs():
    """最近运行列表：按 run_id 分组，带步数与是否发生消解"""
    u = current_user()
    if not _can_view(u):
        return fail("无权查看 Agent 协作轨迹"), 403

    limit = _int_arg("limit", 20, 1, 100)
    # 先取最近的一批 step，再按 run_id 归并。
    # 不写 GROUP BY + 子查询的原因：SQLite 上这条语句会退化成
    # "扫全表再分组"，而轨迹表是全库增长最快的表。先取 id 倒序的
    # 2000 行、在内存里归并，代价可控且结果一样。
    rows = (AgentStep.query.order_by(AgentStep.id.desc()).limit(2000).all())

    groups = {}
    order = []
    for r in rows:
        if r.run_id not in groups:
            groups[r.run_id] = {
                "run_id": r.run_id, "steps": 0, "agents": set(),
                "conflicts": 0, "upgraded": 0, "orders": [],
                "at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
                "fde_id": r.fde_id, "degraded": False,
            }
            order.append(r.run_id)
        g = groups[r.run_id]
        g["steps"] += 1
        if r.agent:
            g["agents"].add(r.agent)
        if r.action == "resolve":
            g["conflicts"] += 1
        if r.action == "dispatch" and "升级" in (r.reason or ""):
            g["upgraded"] += 1
        if r.order_no and r.order_no not in g["orders"]:
            g["orders"].append(r.order_no)
        if r.degraded:
            g["degraded"] = True
        if r.fde_id:
            g["fde_id"] = g["fde_id"] or r.fde_id

    items = []
    for rid in order[:limit]:
        g = groups[rid]
        items.append({
            "run_id": g["run_id"], "at": g["at"], "steps": g["steps"],
            "agents": sorted(g["agents"]), "conflicts": g["conflicts"],
            "upgraded": g["upgraded"], "orders": g["orders"][:5],
            "fde_id": g["fde_id"], "degraded": g["degraded"],
        })
    return ok({"items": items, "counts": {"runs": len(order), "shown": len(items)}})


@bp.get("/trace")
@perm_required("fde.view")
def trace():
    """单次运行的逐步轨迹

    `claims` 只给"参与消解的结论"（topic 非空），`steps` 给全部步骤。
    两者分开是因为页面要画两条线：一条是时间线（谁在什么时候做了什么），
    一条是对峙图（两个 Agent 各说各的、最后由谁裁定）。
    """
    u = current_user()
    if not _can_view(u):
        return fail("无权查看 Agent 协作轨迹"), 403

    run_id = (request.args.get("run_id") or "").strip()
    if not run_id:
        return fail("缺少 run_id"), 400

    rows = (AgentStep.query.filter_by(run_id=run_id)
            .order_by(AgentStep.seq, AgentStep.id).all())
    if not rows:
        return fail("没有找到这条运行记录"), 404

    steps = [r.to_dict() for r in rows]
    claims = [s for s in steps if s["topic"]]

    # 消解结果：resolve 行的 reason 里已经写明"谁定什么 → 采纳谁"，
    # 这里再从 claims 反推出结构化的一份，免得前端去解析中文。
    by_topic = defaultdict(dict)
    for c in claims:
        by_topic[c["topic"]][c["agent"]] = c
    resolutions = []
    for topic, m in by_topic.items():
        if len({c["decision"] for c in m.values()}) < 2:
            continue
        # 更严者胜：与 blackboard.resolve() 同一口径
        winner = min(m.values(), key=lambda c: LEVEL_HOURS.get(_level_of(c["decision"]), 99)
                     if topic == "sla" else 0)
        resolutions.append({
            "topic": topic,
            "claims": [{"agent": c["agent"], "value": c["decision"],
                        "reason": c["reason"]} for c in m.values()],
            "winner": winner["agent"], "value": winner["decision"],
        })

    total_ms = sum(s["took_ms"] for s in steps)
    return ok({
        "run_id": run_id,
        "fde_id": rows[0].fde_id,
        "at": steps[0]["at_full"],
        "steps": steps,
        "claims": claims,
        "resolutions": resolutions,
        "summary": {
            "steps": len(steps),
            "elapsed_ms": total_ms,
            "degraded": any(s["degraded"] for s in steps),
            "agents": sorted({s["agent"] for s in steps if s["agent"]}),
        },
    })


@bp.get("/stats")
@perm_required("fde.view")
def stats():
    """协作统计：这是"多 Agent 比单 Agent 强在哪"唯一能出数的地方

    口径说明（答辩会被追问，先写清楚）：

    · `reviewed`：风险复核**表过态**的单数，不等于"有异议"——表态和原定级
      一致时不算升级，但说明复核跑过。
    · `upgraded`：复核给出的等级比派单更严的单数。**取严格大于**，
      同级不算（同级时 risk 根本不会表态，见 riskreview.review 的提前返回）。
    · `saved_hours`：把每张升级单的"原时限 − 新时限"加总。这个数**不能**
      念成"社区省了多少小时"——它表示"制度上要求的响应窗口被压缩了多少"，
      是压力指标，不是效率指标。
    · 只统计 `topic="sla"` 的 claim 行，不碰 `audit_log`。
    """
    u = current_user()
    if not _can_view(u):
        return fail("无权查看 Agent 协作轨迹"), 403

    days = _int_arg("days", 30, 1, 365)
    since = datetime.now() - timedelta(days=days)

    rows = (AgentStep.query.filter(AgentStep.created_at >= since)
            .order_by(AgentStep.id).all())

    # ---- 定级：按 (run_id, work_item_id) 归并两个 Agent 的表态
    groups = defaultdict(dict)
    for r in rows:
        if r.topic == "sla" and r.action == "claim" and r.agent in ("dispatch", "risk"):
            groups[(r.run_id, r.work_item_id)][r.agent] = r.decision

    total = len(groups)
    reviewed = sum(1 for g in groups.values() if "risk" in g)
    upgraded = 0
    saved = 0
    pairs = Counter()
    for g in groups.values():
        d, k = _level_of(g.get("dispatch", "")), _level_of(g.get("risk", ""))
        if not d or not k:
            continue
        if LEVEL_HOURS.get(k, 99) < LEVEL_HOURS.get(d, 99):
            upgraded += 1
            saved += LEVEL_HOURS[d] - LEVEL_HOURS[k]
            pairs[f"{d}→{k}"] += 1

    # ---- 各 Agent 耗时（只算真正耗时的步骤，脱敏/规则类通常是 0~2ms）
    timings = defaultdict(list)
    degraded = 0
    for r in rows:
        if r.took_ms:
            timings[r.agent].append(r.took_ms)
        if r.degraded:
            degraded += 1
    agents = []
    for name, vals in timings.items():
        vals.sort()
        agents.append({
            "agent": name, "n": len(vals),
            "avg_ms": round(sum(vals) / len(vals), 1),
            "p90_ms": vals[max(0, int(len(vals) * 0.9) - 1)],
            "max_ms": vals[-1],
        })
    agents.sort(key=lambda x: -x["avg_ms"])

    return ok({
        "days": days,
        "decisions": total,
        "reviewed": reviewed,
        "upgraded": upgraded,
        "upgrade_rate": round(upgraded / total, 4) if total else 0,
        "saved_hours": saved,
        "pairs": dict(pairs.most_common()),
        "degraded_steps": degraded,
        "steps": len(rows),
        "agents": agents,
        "note": ("saved_hours 是响应窗口被压缩的量，不是节省的人力工时；"
                 "upgrade_rate 的分母是所有派过单的业务项，不含未派单事项"),
    })
