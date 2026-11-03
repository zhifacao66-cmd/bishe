"""流程监控与风险预警接口

三个端点：
  · /process  流程挖掘（边停留时长 / 路径分布 / 卡点 / 异常清单）
  · /risk     长尾预警（风险排序 / 反复投诉 / 踢皮球 / 长期未结）
  · /topics   投诉热点（主题归并 / 高频字串 / 聚类可用性）

## 权限与收窄

两个层面，缺一个都出事：

**功能级**：`order.view` 才能进。没有这个权限的账号连页面都不该看到——
一张"社区有多少超期单"的图，本身就是一份工作评价。

**行级**：走 `policy.scope_query("order", ...)` 收窄。物业登录只能看自己
单位的单，看不到同行和别的小区。这里复用同一处实现，不另写一份
`filter_by(property_co=org)`——docs/26 红线 1 记录过这类分叉的代价。

**/topics 单独加了一条角色判断**：居民诉求原文里有邻里纠纷、家庭困难、
独居老人这类内容，物业不需要知道这些才能修灯。这是"最小必要"原则，
不是不信任物业。所以走代码硬判，而不是给一个可以被误勾的权限点。
"""
from flask import Blueprint, request

from app import process, risk, textmine
from app.api import fail, ok
from app.auth import current_user, perm_required
from app.models import WorkOrder
from app.policy import scope_query

bp = Blueprint("insight", __name__)

# 清单类接口一次最多返回多少条。
# 不分页：这两张表是"待办清单"不是"档案库"，一个人能处理的告警就那么多，
# 分页反而让"最危险的排在第二页"。超过 200 条说明系统已经失控，
# 该看的是汇总数字而不是逐条。
MAX_ROWS = 200


def _visible():
    """当前用户可见的工单 id 集合。返回 (ids, user)。

    ids 为 `None` 表示"全部可见"（admin/street）——但这里**始终传集合**，
    因为下游函数对 `None` 的处理是"不过滤"，而"不过滤"和"全部可见"
    在语义上是同一件事，但走 `None` 分支会少一次 `IN` 查询。
    为了口径统一，一律走集合：数据集就这个量级，多一次 id 查询比
    "两条代码路径各有各的 bug"划算。
    """
    u = current_user()
    q, _ = scope_query("order", WorkOrder.query, u)
    return process.visible_ids(q), u


@bp.get("/process")
@perm_required("order.view")
def get_process():
    """流程监控台。一次返回页面要的全部数据，不做 5 个接口各拉一遍。

    合成一个请求的理由：这五块数据必须**同一时刻**的快照。
    分开请求会出现"卡片说 8 张卡单、列表里只有 6 张"——中间那 0.3 秒
    有人办结了一张。数字打架的观感比慢一点糟得多。
    """
    ids, _u = _visible()
    return ok({
        "summary": process.summary(order_ids=ids),
        "stages": process.stage_stats(order_ids=ids),
        "paths": process.path_stats(order_ids=ids)[:10],
        "bottlenecks": process.bottleneck(top=3, order_ids=ids),
        "stuck": process.stuck(order_ids=ids)[:MAX_ROWS],
        "anomalies": process.anomaly(limit=MAX_ROWS, order_ids=ids),
        # 门槛值随数据下发，页面用它决定显示"样本不足"还是具体数字，
        # 不在前端硬编码 5 这个魔法数（改后端一处即可）
        "min_n_p90": process.MIN_N_FOR_P90,
        "min_n_median": process.MIN_N_FOR_MEDIAN,
        "long_pending_days": process.LONG_PENDING_DAYS,
        "can_see_topics": _can_see_topics(_u),
    })


@bp.get("/risk")
@perm_required("order.view")
def get_risk():
    """长尾预警台。

    `with_people` 决定是否统计"反复投诉"：按居民手机号聚合出来的
    "某户报了几次事"属于居民画像，物业不需要。管理员/社区/街道要——
    他们才是要做矛盾化解的人。
    """
    ids, u = _visible()
    with_people = _can_see_topics(u)
    return ok({
        "summary": risk.summary(order_ids=ids, with_people=with_people),
        "warnings": risk.warnings(limit=MAX_ROWS, order_ids=ids),
        "repeat": risk.repeat_complainants() if with_people else [],
        "ping_pong": risk.ping_pong(order_ids=ids)[:MAX_ROWS],
        "long_pending": risk.long_pending(order_ids=ids)[:MAX_ROWS],
        # 权重随数据下发，页面上要能显示"这个分是怎么来的"
        "weights": {
            "repeat": risk.W_REPEAT,
            "cross_org_transfer": risk.W_CROSS_ORG_TRANSFER,
            "same_org_transfer": risk.W_SAME_ORG_TRANSFER,
            "over_sla": risk.W_OVER_SLA,
            "over_sla_3x": risk.W_OVER_SLA_3X,
            "long_pending": risk.W_LONG_PENDING,
            "rejected": risk.W_REJECTED,
            "disputed_unjudged": risk.W_DISPUTED_UNJUDGED,
            "cap": risk.SCORE_CAP,
        },
        "with_people": with_people,
    })


@bp.get("/topics")
@perm_required("order.view")
def get_topics():
    """投诉热点。物业 403 —— 见模块 docstring 的"最小必要"那条。"""
    u = current_user()
    if not _can_see_topics(u):
        return fail("居民诉求原文仅对社区、街道与管理员开放"), 403
    limit = request.args.get("top", type=int) or 30
    limit = max(1, min(limit, 100))
    return ok({
        "summary": textmine.summary(),
        "hotspots": textmine.hotspots(),
        "ngrams": textmine.ngrams(top=limit),
        # 聚类这块**如实返回不可用**，页面要显示原因。
        # 写一个空数组糊过去最省事，但那等于让看的人以为"跑了、没结果"。
        "cluster": textmine.cluster(),
        "min_n_for_cluster": textmine.MIN_N_FOR_CLUSTER,
    })


def _can_see_topics(u):
    """能不能看居民诉求原文。

    白名单：community / street / admin。物业、居民端、未登录一律不给。
    与 arbitration 的`回避原则`不同——那里是"当事方不能判自己"，
    这里是"非必要不接触"，两件事分开写，不合并成一个"敏感角色"概念。
    """
    return getattr(u, "role", "") in ("community", "street", "admin")
