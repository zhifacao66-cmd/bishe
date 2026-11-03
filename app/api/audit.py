"""审计日志接口（游标分页）

返回 `{items, counts}`。审计总量、保密审查批次这些数字必须服务端数：
明细只给一页，前端数长度会把"1000 多条留痕"显示成"这一屏 100 条"——
审计页面上数字不准，等于这份账本自己就不可信。

**为什么用游标而不用 offset**：审计日志是只追加表，新记录永远插在最前面。
`LIMIT n OFFSET m` 翻到后面时，期间的写入会让"第 3 页"整体后移，
翻页的人因此看到重复和遗漏；而 `OFFSET 50000` 还要先数过五万行再丢掉，
越翻越慢。游标锁在"上一页最后一条"上，翻到第几页都是同一个代价。

游标是 `(created_at, id)` 的 base64。**必须带 id**：同一次批量写入的几条
时间戳可能完全相同（秒级精度），只比时间会在同一秒的边界上漏记录。
"""
import base64
from datetime import datetime

from flask import Blueprint, request
from sqlalchemy import and_, func, or_

from app.api import ok
from app.auth import perm_required
from app.models import AuditLog

bp = Blueprint("audit", __name__)

DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _int_arg(name, default, cap):
    try:
        v = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    # 非正值视作没填：size=0 被夹成 1 会让"只回 1 条"看起来像查询出了问题
    return max(1, min(v, cap)) if v > 0 else default


def _encode_cursor(log) -> str:
    raw = f"{log.created_at.isoformat()}|{log.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(s: str):
    """坏游标一律当"从头开始"，不报错。

    游标由前端带着，可能来自旧版本、可能被手工改过。为它回 400 会让用户
    卡在一个翻不动的页面上——它只是个位置，退回第一页比报错更有用。
    """
    if not s:
        return None
    try:
        pad = "=" * (-len(s) % 4)
        raw = base64.urlsafe_b64decode(s + pad).decode()
        ts, _, cid = raw.rpartition("|")
        return datetime.fromisoformat(ts), int(cid)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


@bp.get("")
@perm_required("audit.view")
def list_logs():
    agent = request.args.get("agent", "").strip()
    action = request.args.get("action", "").strip()
    limit = _int_arg("limit", DEFAULT_LIMIT, MAX_LIMIT)
    cur = _decode_cursor(request.args.get("cursor", ""))

    q = AuditLog.query
    if agent:
        q = q.filter_by(agent=agent)
    if action:
        q = q.filter_by(action=action)

    # 当前筛选下的总量：不带游标数一次。无筛选时不额外打库——
    # 下面的分组统计已经拿到全集，加总即总数。
    matched = q.count() if (agent or action) else None

    if cur:
        ts, cid = cur
        q = q.filter(or_(AuditLog.created_at < ts,
                         and_(AuditLog.created_at == ts, AuditLog.id < cid)))

    # 多取一条只为判断"还有没有下一页"，省掉一次 COUNT
    rows = (q.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit + 1).all())
    has_more = len(rows) > limit
    rows = rows[:limit]

    # 分 Agent 计数走一次分组查询，一次拿全，避免每个 agent 打一次库
    by_agent = dict(AuditLog.query
                    .with_entities(AuditLog.agent, func.count(AuditLog.id))
                    .group_by(AuditLog.agent).all())
    total = sum(by_agent.values())

    return ok({
        "items": [{
            "id": l.id, "actor": l.actor, "agent": l.agent, "action": l.action,
            "input_digest": l.input_digest, "output_digest": l.output_digest,
            "created_at": l.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        } for l in rows],
        "counts": {
            "total": total,
            "security": by_agent.get("security", 0),
            "deny": AuditLog.query.filter_by(action="deny").count(),
            "by_agent": by_agent,
            "matched": total if matched is None else matched,
            "shown": len(rows),
            "has_more": has_more,
            "truncated": has_more,          # 兼容旧字段名
            "next_cursor": _encode_cursor(rows[-1]) if (has_more and rows) else "",
        },
    })


@bp.get("/agents")
@perm_required("audit.view")
def agents():
    rows = AuditLog.query.with_entities(AuditLog.agent).distinct().all()
    return ok([r[0] for r in rows if r[0]])
