"""台账接口

返回 `{items, counts}`：明细与统计分开算。

为什么统计不能由前端数明细：明细有 limit（默认只给最近 100 条），
前端的 `list.length` 数的是"这 100 条里有多少"，不是"库里有多少"。
演示库 101 条时页面就敢显示"台账总数 100"。数字错了比功能少更致命——
管理者会据此判断进度。
"""
import json
from flask import Blueprint, request
from sqlalchemy import func

from app.api import ok
from app.auth import perm_required
from app.models import Ledger
from app.search import like_all

bp = Blueprint("ledgers", __name__)

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _int_arg(name: str, default: int, cap: int) -> int:
    """取整数参数：非法/非正一律回默认值，再按上限夹住。

    非正值不能夹成 1：`size=0` 的本意是"没填"，夹成 1 就变成只回一条，
    看着像"筛出来只有一条"——又是一个数字静默出错的地方。
    """
    try:
        v = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    if v <= 0:
        return default
    return min(v, cap)


@bp.get("")
@perm_required("ledger.view")
def list_ledgers():
    line = request.args.get("line", "").strip()
    limit = _int_arg("limit", DEFAULT_LIMIT, MAX_LIMIT)
    page = _int_arg("page", 1, 1000)
    # 关键词直接搜原文 JSON：台账规模在万条以内，LIKE 比 FTS5 更合适——
    # SQLite 的默认分词器对中文是漏检的（整句一个 token），trigram 又搜不到
    # 2 字词。实测与论证见 app/search.py 顶部的说明、scripts/bench_ledger_search.py。
    # 这里只负责把用户输入交给 search.like_all：通配符转义 + 多词 AND 都在那边。
    kw = request.args.get("q", "") or ""

    q = Ledger.query
    if line:
        q = q.filter_by(line=line)
    if kw:
        # coalesce 是迁移期的兜底：老库若 search_text 还没回填（NULL），
        # 退回原来的 content_json 上搜——宁可搜出带键名的假命中，
        # 也不能让老台账直接搜不到。
        cond = like_all([func.coalesce(Ledger.search_text, Ledger.content_json)], kw)
        if cond is not None:
            q = q.filter(cond)

    matched = q.count()
    items = (q.order_by(Ledger.created_at.desc())
             .offset((page - 1) * limit).limit(limit).all())

    # 统计一律走聚合查询，且**不受 line/关键词筛选影响**：
    # 卡片写的是"台账总数/复用/覆盖业务线"，说的是整本台账的家底，
    # 不是"我当前这一屏筛出了几条"。筛选影响的是明细，不是家底。
    #
    # 注意：这里**不能**写 func.sum(Ledger.reused)。SQLAlchemy 会按列类型
    # 推断 sum 的返回类型，Boolean 的结果处理器把 23 归一成 True，再 int() 得到 1，
    # 于是"复用 23 次"显示成"复用 1 次"——数字静默出错，比报错难查得多。
    # 计数就用 count()，语义清楚也不会被类型处理器改写。
    total = Ledger.query.count()
    reused = Ledger.query.filter(Ledger.reused.is_(True)).count()
    lines = (Ledger.query.with_entities(func.count(func.distinct(Ledger.line)))
             .scalar() or 0)

    out = []
    for lg in items:
        c = json.loads(lg.content_json or "{}")
        out.append({
            "id": lg.id, "line": lg.line,
            "type": c.get("type"), "subject": c.get("subject") or c.get("content"),
            "feedback": c.get("feedback"), "order_no": c.get("order_no"),
            "room_key": c.get("room_key"), "reused": bool(lg.reused),
            "source": c.get("source"),
            "created_at": lg.created_at.strftime("%Y-%m-%d %H:%M"),
        })
    return ok({
        "items": out,
        "counts": {
            "total": int(total or 0),
            "reused": int(reused or 0),
            "lines": int(lines),
            "matched": matched,
            "page": page, "size": limit,
            "pages": max(1, (matched + limit - 1) // limit),
            "shown": len(out),
            "truncated": (page - 1) * limit + len(out) < matched,
        },
        "filter": {"line": line, "q": kw},
    })


@bp.get("/lines")
@perm_required("ledger.view")
def lines():
    """台账业务线及计数（供筛选器）"""
    rows = (Ledger.query.with_entities(Ledger.line).distinct().all())
    return ok([r[0] for r in rows])
