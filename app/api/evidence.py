"""证据链接口：查看某个对象的链、校验全链、锚定链头

三个端点对应三种问法：
  · 「这件事的证据能查到哪一步？」→ /chain
  · 「谁敢改过东西吗？」→ /verify
  · 「今天这条链的状态留底了吗？」→ /anchor

权限分两档：
  · 查某个工单/诉求的链 → `order.view`：经手人自己就该看得见，
    否则"留痕"只对领导可见，一线反而没法自证清白。
  · 校验全链 / 锚定 → `audit.view`：这是监督动作，不是办事动作。
"""
from flask import Blueprint, request

from app import evidence
from app.api import fail, ok
from app.auth import current_user, perm_required
from app.extensions import db
from app.models import EvidenceChain

bp = Blueprint("evidence", __name__)

# 链记录的对外形状：**不回 payload 原文**，只回摘要。
# payload 里虽然已经过脱敏，但它终究是"内容"，而查链的人要的通常只是
# "有没有这一步、什么时候、谁做的"。要内容就去看业务表，那里有权限控制。
def _row(r):
    return {
        "seq": r.seq,
        "action": r.action,
        "actor": r.actor or "",
        "at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "",
        "hash": (r.hash or "")[:16],
        "prev": (r.prev_hash or "")[:16],
        "summary": _summarize(r),
    }


def _summarize(r):
    """从 payload 里挑一句人话，挑不出来就回动作名

    链记录是给非技术角色看的（居委会、街道），满屏 JSON 没人读得下去。
    """
    import json
    try:
        d = json.loads(r.payload or "{}")
    except (ValueError, TypeError):
        return ""
    for k in ("verdict_label", "detail", "result", "name", "line_summary"):
        v = d.get(k)
        if v:
            return str(v)[:80]
    return ""


@bp.get("/chain")
@perm_required("order.view")
def chain():
    ttype = (request.args.get("type") or "order").strip()
    tid = request.args.get("id", type=int)
    if ttype not in ("order", "ticket") or not tid:
        return fail("请指定 type=order|ticket 与 id"), 400
    if ttype == "order":
        # 按单位收窄：物业只能查自己的单。
        # `order.view` 管的是"能不能进工单功能"，它并不区分"哪一张"——
        # 只靠这个权限点，A 物业能拿 B 物业的单号去查它的整条流转与裁决记录，
        # 连裁决依据（往往点名具体责任人和合同条款）都看得到。
        from app.models import WorkOrder
        o = db.session.get(WorkOrder, tid)
        if not o:
            return fail("工单不存在"), 404
        # 改走 ABAC 单点决策：R3（跨单位）用的是同一张规则表，
        # 以后加"按小区隔离""按涉密级别隔离"都只改 policy 一处。
        from app.policy import decide
        d = decide(current_user(), "read", "evidence", owner_org=o.property_co or "")
        if not d.allow:
            return fail(d.reason), 403
    rows = evidence.chain_of(ttype, tid)
    return ok({"items": [_row(r) for r in rows], "total": len(rows),
               "head": evidence.head()[1][:16]})


@bp.get("/verify")
@perm_required("order.view")
def verify():
    """校验全链

    权限用 `order.view` 而不是 `audit.view`：**仲裁方自己必须能验证**。
    居委会要向居民和物业证明"记录没被改过"，如果这个能力只在街道手里，
    它在现场就没有任何说服力。校验只回 ok/断点，不吐内容，风险可控。

    返回 `ok=False` 时**同时给 HTTP 409**：如果只回 `{code:1}`，
    外部监控拿到 200 会把"链断了"算成"接口正常"，这和"拒绝被算成放行"
    是同一类错误（见 docs/06 监控口径）。
    """
    res = evidence.verify()
    if not res["ok"]:
        # fail() 只构造 body，不带 data；断点信息对排障很关键，
        # 所以显式挂上去，而不是塞进 msg 里让人正则去抠。
        body = fail(res.get("detail") or "证据链校验未通过")
        body["data"] = res
        return body, 409
    return ok(res)


@bp.post("/anchor")
@perm_required("order.view")
def anchor():
    """把当前链头钉一份，供日后比对

    **谁能锚，是一个设计决定**：居委会、街道、管理员都能锚，物业不能。
    理由：锚点的价值来自"多方各自留一份"，居委会今天锚一次、街道每周锚一次，
    两边的记录互相印证；如果只有管理员能锚，它就退化成运维脚本的一次执行，
    居委会被质疑时反而拿不出自己那一份。
    物业被排除，仍然是因为它是争议当事方——让它参与留底，等于让当事人保管证物。

    为什么还要手动按钮：演示时要能当场点一下、当场看到"锚点已记录"。
    纯后台定时任务在答辩现场无法展示，等于这个能力不存在。
    """
    u = current_user()
    if not u or u.role not in ("community", "street", "admin"):
        return fail("锚定由居委会或街道留底；物业作为争议当事方不参与"), 403
    note = f"anchor by {u.name or u.username}({u.role})"
    row = evidence.anchor(note)
    db.session.commit()
    return ok({"seq": row.seq, "hash": (row.head_hash or "")[:16],
               "total": row.total, "note": row.note})


@bp.get("/stats")
@perm_required("audit.view")
def stats():
    from sqlalchemy import func
    from app.models import ChainAnchor
    total = db.session.query(func.count(EvidenceChain.id)).scalar() or 0
    by_action = dict(db.session.query(EvidenceChain.action, func.count(EvidenceChain.id))
                     .group_by(EvidenceChain.action).all())
    last_anchor = db.session.query(ChainAnchor).order_by(ChainAnchor.id.desc()).first()
    seq, h = evidence.head()
    return ok({
        "total": total,
        "head_seq": seq,
        "head_hash": h[:16],
        "by_action": by_action,
        "anchors": db.session.query(func.count(ChainAnchor.id)).scalar() or 0,
        "last_anchor": {"seq": last_anchor.seq, "at": last_anchor.created_at.strftime(
            "%Y-%m-%d %H:%M") if last_anchor and last_anchor.created_at else ""}
        if last_anchor else None,
    })
