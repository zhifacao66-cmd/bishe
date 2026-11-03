"""发文接口（上行 / 平行）

与收文（`app/api/notices.py`）分开的理由：
  收文的动作是"签收 / 办结"，发文的动作是"核稿 / 签发 / 用印 / 分发"，
  两套状态机、两套权限（街道能发文给社区，但社区不能替街道发文）。
  塞进一个文件会让"谁能干什么"变得靠读代码才能回答。

错误返回一律带 HTTP 码（400 参数错 / 403 越权 / 404 不存在 / 409 状态不允许）。
`fail()` 只构造 body，漏写码会被 Flask 用 200 发出去，
监控就会把"拒绝"统计成"放行"（docs/26 红线 2）。
"""
from datetime import date, datetime

from flask import Blueprint, request
from sqlalchemy import or_

from app.api import fail, ok
from app.auth import current_user, perm_required
from app.docflow import (ACTIONS, DOC_TYPES, LABEL, SECRET_LEVELS, STATUS_LABEL,
                         direction_of, make_doc_no, push)
from app.extensions import db
from app.models import AuditLog, DocLog, Notice

bp = Blueprint("docs", __name__)

DEFAULT_SIZE = 50
MAX_SIZE = 200


def _int_arg(name: str, default: int, cap: int) -> int:
    try:
        v = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(v, cap) if v > 0 else default


def _audit(action: str, detail: str):
    u = current_user()
    db.session.add(AuditLog(actor=u.name if u else "system", agent="doc",
                            action=action, input_digest=detail[:250],
                            created_at=datetime.now()))


def _visible(base, u):
    """按角色收窄可见范围

    写白名单而不是"排除谁"：早先别处写过"除街道外都收窄"，
    结果 admin 的 assignee_role 被判成不存在的角色，页面恒空（docs/26 红线 1）。
    这里同理——将来加角色，没有明确列进来的角色应当看不到，而不是全看到。
    """
    role = getattr(u, "role", "") or ""
    if role in ("community", "street", "admin"):
        return base
    # 物业只该看到发给自己的函；内部/敏感件一律不给。
    # 密级白名单由 policy 现算，不在这里写死三元组——否则 policy 里
    # 调整一次可见上限，列表还是老口径，出现"能进详情页、列表里却没有"。
    if role == "property":
        from app.policy import readable_secret_levels
        org = (u.org or "") if u else ""
        return base.filter(Notice.secret_level.in_(readable_secret_levels(role)),
                           Notice.to_org.contains(org) if org else False)
    return base.filter(False)


def _row(n: Notice, with_body: bool = False) -> dict:
    d = {
        "id": n.id,
        "title": n.title or "",
        "doc_no": n.doc_no or "",
        "doc_type": n.doc_type or "通知",
        "direction": direction_of(n.doc_type or ""),
        "doc_status": n.doc_status or "",
        "status_label": STATUS_LABEL.get(n.doc_status or "", n.doc_status or "未拟稿"),
        "secret_level": n.secret_level or "",
        "urgency": n.urgency or "平件",
        "to_org": n.to_org or "",
        "cc_org": n.cc_org or "",
        "signer": n.signer or "",
        "issuer": n.issuer or "",
        "created_at": n.created_at.strftime("%Y-%m-%d %H:%M") if n.created_at else "",
        "issued_at": n.issued_at.strftime("%Y-%m-%d") if n.issued_at else "",
        "sealed_at": n.sealed_at.strftime("%Y-%m-%d %H:%M") if n.sealed_at else "",
        "archived_at": n.archived_at.strftime("%Y-%m-%d") if n.archived_at else "",
        "secret_until": n.secret_until.strftime("%Y-%m-%d") if n.secret_until else "",
    }
    if with_body:
        d["body_md"] = n.body_md or ""
        logs = []
        for l in (DocLog.query.filter_by(notice_id=n.id)
                  .order_by(DocLog.id.asc()).all()):
            logs.append({
                "action": LABEL.get(l.action, l.action),
                "actor": l.actor or "",
                "at": l.created_at.strftime("%Y-%m-%d %H:%M") if l.created_at else "",
                "from": l.from_status or "未拟稿",
                "to": l.to_status or "",
                "note": l.note or "",
            })
        d["logs"] = logs
    return d


@bp.get("")
@perm_required("doc.view")
def index():
    u = current_user()
    q = _visible(Notice.query.filter(Notice.direction == "out"), u)

    status = (request.args.get("status") or "").strip()
    if status:
        q = q.filter(Notice.doc_status == status)
    kw = (request.args.get("q") or "").strip()
    if kw:
        like = f"%{kw}%"
        q = q.filter(or_(Notice.title.like(like), Notice.doc_no.like(like),
                         Notice.to_org.like(like)))

    total = q.count()
    page = _int_arg("page", 1, 10000)
    size = _int_arg("size", DEFAULT_SIZE, MAX_SIZE)
    rows = (q.order_by(Notice.id.desc()).offset((page - 1) * size).limit(size).all())
    return ok({
        "items": [_row(n) for n in rows],
        "counts": {
            "total": total,
            "page": page,
            "pages": max(1, (total + size - 1) // size),
            "draft": Notice.query.filter(Notice.direction == "out",
                                          Notice.doc_status == "draft").count(),
            "pending": Notice.query.filter(Notice.direction == "out",
                                            Notice.doc_status.in_(("checked", "signed", "sealed"))).count(),
        },
    })


@bp.post("")
@perm_required("doc.manage")
def create():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    doc_type = (data.get("doc_type") or "").strip()
    if not title:
        return fail("公文标题不能为空"), 400
    if len(title) > 120:
        return fail("标题过长（≤120 字）"), 400
    if doc_type not in DOC_TYPES:
        return fail(f"文种只能是：{'、'.join(DOC_TYPES)}"), 400

    secret = (data.get("secret_level") or "").strip()
    if secret not in SECRET_LEVELS:
        return fail(f"密级标识只能是：{'、'.join(x or '未标' for x in SECRET_LEVELS)}"), 400
    # 定密权边界：社区自己产生的材料不能标"秘密/机密"，那两档只能继承上级来文
    if secret in ("秘密", "机密"):
        return fail("社区无定密权，「秘密 / 机密」只能由上级来文继承，"
                    "本社区材料请标「内部」或「内部·敏感」"), 400

    to_org = (data.get("to_org") or "").strip()
    if doc_type in ("请示", "报告") and not to_org:
        return fail("上行文必须填写主送机关"), 400
    if doc_type == "函" and not to_org:
        return fail("函必须填写受文单位"), 400

    u = current_user()
    n = Notice(
        title=title,
        doc_type=doc_type,
        direction="out",
        doc_status="draft",
        kind="original",
        # 发文时 level 记的是"谁在发"，由账号档案决定，不写死常量
        level={"community": "社区", "street": "街道"}.get(getattr(u, "role", ""), "社区"),
        issuer=(data.get("issuer") or (u.org if u else "") or "社区居委会").strip()[:64],
        to_org=to_org[:128],
        cc_org=(data.get("cc_org") or "").strip()[:128],
        secret_level=secret,
        urgency=(data.get("urgency") or "平件").strip(),
        signer=(data.get("signer") or "").strip()[:32],
        body_md=(data.get("body_md") or "").strip(),
        summary=(data.get("summary") or "").strip()[:256],
        created_by=u.name if u else "",
        status="pending",          # 未发布到公告栏；发文走自己的状态机
        issued_at=date.today(),
    )
    db.session.add(n)
    db.session.commit()
    _audit("doc_create", f"拟稿「{title}」{doc_type}")
    db.session.commit()
    return ok(_row(n))


@bp.get("/<int:doc_id>")
@perm_required("doc.view")
def detail(doc_id):
    n = db.session.get(Notice, doc_id)
    if not n or n.direction != "out":
        return fail("公文不存在"), 404
    u = current_user()
    # 密级判断走 ABAC 单一决策点（与 /docs/<id> 页面、导出三处同源）。
    # 403 的 body 带的是规则算出来的原因，不是一句"无权查看"——
    # "密级为「内部·敏感」，你的角色「物业」可见上限为「内部」"这种话
    # 才让人知道该找谁去要权限。
    from app.policy import decide, secret_num
    d = decide(u, "read", "doc", secret=secret_num(n.secret_level))
    if not d.allow:
        return fail(d.reason), 403
    return ok(_row(n, with_body=True))


@bp.patch("/<int:doc_id>")
@perm_required("doc.manage")
def edit(doc_id):
    n = db.session.get(Notice, doc_id)
    if not n or n.direction != "out":
        return fail("公文不存在"), 404
    if n.doc_status not in ("draft", "checked"):
        return fail(f"「{LABEL.get(n.doc_status, n.doc_status)}」之后不能再改稿，"
                    "需要修改请退回拟稿"), 409

    data = request.get_json(silent=True) or {}
    for field, cap in (("title", 120), ("to_org", 128), ("cc_org", 128),
                       ("signer", 32), ("issuer", 64), ("summary", 256)):
        if field in data:
            setattr(n, field, (data.get(field) or "").strip()[:cap])
    if "body_md" in data:
        n.body_md = (data.get("body_md") or "").strip()
    if "doc_type" in data:
        if data["doc_type"] not in DOC_TYPES:
            return fail(f"文种只能是：{'、'.join(DOC_TYPES)}"), 400
        n.doc_type = data["doc_type"]
    if "secret_level" in data:
        if data["secret_level"] not in SECRET_LEVELS:
            return fail("密级标识非法"), 400
        n.secret_level = data["secret_level"]
    if "urgency" in data:
        n.urgency = (data["urgency"] or "平件").strip()

    u = current_user()
    _audit("doc_edit", f"改稿「{n.title}」")
    db.session.commit()
    return ok(_row(n, with_body=True))


@bp.post("/<int:doc_id>/number")
@perm_required("doc.manage")
def number(doc_id):
    """手动取号。不自动取号是刻意的：
    文号一旦生成就要报出去，提前占号而最终没发，会让号段出现空洞——
    上级核对时"少了一个号"是要解释的。"""
    n = db.session.get(Notice, doc_id)
    if not n or n.direction != "out":
        return fail("公文不存在"), 404
    if n.doc_status in ("sent", "archived"):
        return fail("已分发的公文不能再改文号"), 409
    if n.doc_no:
        return fail(f"已有文号 {n.doc_no}，如需重排请先说明原因"), 409
    n.doc_no = make_doc_no(n)
    _audit("doc_number", f"取号 {n.doc_no}「{n.title}」")
    db.session.commit()
    return ok(_row(n))


@bp.post("/<int:doc_id>/<action>")
@perm_required("doc.manage")
def step(doc_id, action):
    if action not in ACTIONS:
        return fail("未知动作"), 404
    n = db.session.get(Notice, doc_id)
    if not n or n.direction != "out":
        return fail("公文不存在"), 404

    # 用印时若还没取号，先补一个：盖章件没文号等于废件，
    # 而"忘了取号"是最常见的一步，卡在这里比事后补更省事。
    if action == "seal" and not n.doc_no:
        n.doc_no = make_doc_no(n)

    u = current_user()
    note = (request.get_json(silent=True) or {}).get("note") or ""
    try:
        push(n, action, actor=(u.name if u else ""), actor_id=(getattr(u, "id", None)),
             note=note)
    except ValueError as e:
        db.session.rollback()
        return fail(str(e)), 409

    _audit(f"doc_{action}", f"{LABEL[action]}「{n.title}」{n.doc_no}")
    db.session.commit()
    return ok(_row(n, with_body=True))


@bp.get("/meta")
@perm_required("doc.view")
def meta():
    """给前端下拉用的枚举，避免文种/密级在页面里再写一遍"""
    return ok({
        "doc_types": [{"key": k, "dir": v} for k, v in DOC_TYPES.items()],
        "secret_levels": [x for x in SECRET_LEVELS if x],
        "urgencies": ["平件", "急件", "特急"],
        "actions": [{"key": k, "label": v[1], "to": v[0]} for k, v in ACTIONS.items()],
    })
