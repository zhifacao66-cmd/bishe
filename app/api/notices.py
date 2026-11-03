"""公告管理接口（内部端）

权限分工：
    notice.view    三方管理者都能看
    notice.import  仅街道：把上级文件接进系统
    notice.manage  仅社区：确认发布 / 下架（属地口径，谁属地谁背书）

"谁能进"和"谁能发"分开，是为了让上级文件进来时有第二个人看一眼——
政务信息发布出错是要担责的，自动发布只适用于字段解析完整、置信度高的文件。
"""
from datetime import datetime

from flask import Blueprint, request

from app.api import fail, ok
from app.auth import current_user, perm_required, user_perms
from app.extensions import db
from app.models import AuditLog, Notice
from app.notice.ingest import (can_view, publish, scan_and_import, staff_query,
                               take_offline)

bp = Blueprint("notices", __name__)

# 置顶上限。置顶位置是稀缺资源——满屏都是置顶，等于没有置顶，
# 居民端第一屏也塞不下。超过时明确拒绝并要求先撤一条，
# 而不是静默把最早那条挤下去：静默挤掉会让人以为自己置顶成功了。
MAX_PINNED = 3

# 公告列表分页。原来写死 `.limit(200)`：第 201 条起静默消失，而卡片上的
# 数字还是全量——"已发布 260"配一张 200 条的列表，用户会以为丢了 60 条，
# 且没有任何提示。分页之后"第几页/共几条"能自己说明白。
DEFAULT_SIZE = 50
MAX_SIZE = 200


def _int_arg(name: str, default: int, cap: int) -> int:
    """取整数参数：非法/非正一律回默认值，再按上限夹住

    非正值不能夹成 1：`size=0` 的本意是"没填"，夹成 1 就变成"只回一条"，
    看着像"筛出来只有一条"——数字静默出错比报错难查得多。
    """
    try:
        v = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    if v <= 0:
        return default
    return min(v, cap)


def todo_filter(base=None):
    """把「待签收 + 本单位可签」这两个条件**叠加**到已有查询上

    早先这里只提供 `todo_query()`（从 `Notice.query` 起新查询），列表页在
    `recv=dispatched` 时直接 `q = todo_query()` 换掉了手里那条——
    前面按角色收的可见范围、按 status/kind 收的筛选全被无声丢弃。
    实测的后果有两个：
      · 物业账号带 `?recv=dispatched` 能列出 scope=community 的内部件
        （列表接口本该只给它"已发布 + 面向自己"的）；
      · 同时选"待签收 + 待确认"，返回的却是全部待签收，状态筛选失效。
    所以拆成"叠加条件"和"新查询"两个入口，列表页只能用前者。
    """
    # 从全表起算时也要排掉发文：一份请示的 recv_status 是空，
    # 但将来若有人给它写了个 dispatched，它就会冒充"上级来文"进待办。
    if base is None:
        from app.notice.ingest import _exclude_docs
        base = _exclude_docs(Notice.query)
    q = base.filter(Notice.recv_status == "dispatched")
    u = current_user()
    # admin 必须显式排除：它持有全量权限（含 notice.receive），org 却是"平台运维"，
    # 不排除就会被当成某个社区去比收文单位，待签数恒为 0——管理端看到"没有待办"，
    # 而系统里其实躺着待签文件。staff_query 早已按"admin 看全量"处理，这里对齐。
    if u and u.role != "admin" and "notice.receive" in user_perms(u) and u.org:
        q = q.filter(db.or_(Notice.receiver_org == "", Notice.receiver_org == u.org))
    return q


def todo_query():
    """待签收待办（从全表起算），用于卡片上的"还有几件待办"

    指定了收文单位的文件不该出现在别家的待办里——点进去也是 403，
    还会把"待签收 3"变成噪声，让人以为自己有三件要办。
    留空收文单位的（= 全体社区）每家都算一条。

    街道没有 notice.receive，它看的是全局督办视图，不做过滤：
    它的关注点本来就是"全区还有几件没签"。
    """
    return todo_filter()


def todo_count(role: str) -> int:
    """我这边"待签收"有几件：与列表点进去看到的必须一致

    早先这里直接用 `todo_query().count()`（全局 + 只看 org），物业账号
    卡片显示"待签收 4"、点进去只有 1 条——因为列表那边还叠了
    staff_query 的可见范围。卡片数字和列表条数打架，是这个项目里
    反复出现过的一类问题：数字必须与它指向的那份明细同源。
    """
    return todo_filter(staff_query(role)).count()


def _actor() -> str:
    u = current_user()
    return u.name if u else "system"


def _apply_pin(n: Notice, target: bool) -> str:
    """置顶/取消置顶，顺带维护 pinned_at；返回错误提示，正常返回空串

    pinned 和 pinned_at 必须一起改：只翻布尔值不记录时间，
    多条置顶就会退化成按发稿时间排，后置顶的反而排在后面。
    """
    target = bool(target)
    if target and not n.pinned:
        cur = Notice.query.filter(Notice.pinned.is_(True), Notice.id != n.id).count()
        if cur >= MAX_PINNED:
            return f"已有 {cur} 条置顶，最多 {MAX_PINNED} 条。请先取消一条再置顶。"
    if target:
        # 已经是置顶但时间戳缺失（老库回填前写入的），补一次，避免排到末尾
        if n.pinned_at is None:
            n.pinned_at = datetime.now()
        n.pinned = True
    else:
        n.pinned = False
        n.pinned_at = None
    return ""


def _local_progress(ids: list[int]) -> dict:
    """批量取上级来文的办文进度：生成了几份本社区落地件、其中几份已对外发布

    上级单位真正关心的不是"我发了几份文"，而是"下面办没办、办完发没发出去"。
    没有这一列，街道发完文就只能靠打电话问，系统的价值等于一个邮件列表。
    一次查完按 parent_id 分组，避免列表页 N+1。
    """
    out: dict[int, dict] = {}
    if not ids:
        return out
    for pid, st in (db.session.query(Notice.parent_id, Notice.status)
                    .filter(Notice.parent_id.in_(ids)).all()):
        e = out.setdefault(pid, {"count": 0, "published": 0})
        e["count"] += 1
        if st == "published":
            e["published"] += 1
    return out


def _row(n: Notice, detail: bool = False, progress: dict | None = None) -> dict:
    d = {
        "id": n.id, "title": n.title, "issuer": n.issuer, "doc_no": n.doc_no,
        "category": n.category, "level": n.level, "scope": n.scope,
        "status": n.status, "pinned": n.pinned,
        "pinned_at": n.pinned_at.strftime("%Y-%m-%d %H:%M") if n.pinned_at else "",
        "issued_at": n.issued_at.strftime("%Y-%m-%d") if n.issued_at else "",
        "published_at": n.published_at.strftime("%Y-%m-%d %H:%M") if n.published_at else "",
        "source_file": n.source_file, "parse_note": n.parse_note,
        "summary": n.summary,
        "created_at": n.created_at.strftime("%Y-%m-%d %H:%M") if n.created_at else "",
        # 收文流转
        "kind": n.kind or "superior", "parent_id": n.parent_id,
        "receiver_org": n.receiver_org or "", "recv_status": n.recv_status or "",
        "received_by": n.received_by or "",
        "received_at": n.received_at.strftime("%Y-%m-%d %H:%M") if n.received_at else "",
        "deadline": n.deadline.strftime("%Y-%m-%d") if n.deadline else "",
        # 办结三件套：周报算"已办结/欠办"读的就是 handled_at，
        # 页面也要显示"谁办的、办成什么"，两边必须同一来源
        "handler": n.handler or "",
        "handled_at": n.handled_at.strftime("%Y-%m-%d %H:%M") if n.handled_at else "",
        "handle_result": n.handle_result or "",
    }
    # 办文进度：上级来文才有"下面办没办"，落地件自己不需要再往下数
    p = (progress or {}).get(n.id)
    # 只有**单条**渲染（progress 未提供）才补查一次。批量渲染时 progress 里
    # 没有这个 key 就是"它没有落地件"，不是"没查"——早先这里不判断 progress is None，
    # 于是每一条没有落地件的公告都会再发一次 `_local_progress([n.id])`，
    # 一页 200 条就是 200 次查询，页面在演示库上都要慢一拍。
    if progress is None and (n.kind or "superior") == "superior":
        p = _local_progress([n.id]).get(n.id)
    d["local_count"] = (p or {}).get("count", 0)
    d["local_published"] = (p or {}).get("published", 0)
    if detail:
        d["body_md"] = n.body_md
    return d


@bp.get("")
@perm_required("notice.view")
def index():
    status = (request.args.get("status") or "").strip()
    recv = (request.args.get("recv") or "").strip()
    kind = (request.args.get("kind") or "").strip()
    pin = (request.args.get("pin") or "").strip()
    # 范围按角色收口：物业只该看到"已发布 + 面向自己"的，看不见草稿与内部件
    u = current_user()
    q = staff_query(u.role if u else "")
    if status:
        q = q.filter_by(status=status)
    if recv:
        # 必须用 todo_filter(q) 叠加在现有条件上，不能换成 todo_query()——
        # 换掉等于把上面的 staff_query/status/kind 全部丢弃（见 todo_filter 注释）
        q = todo_filter(q) if recv == "dispatched" else q.filter_by(recv_status=recv)
    if kind:
        q = q.filter_by(kind=kind)
    if pin in ("1", "true", "yes"):
        q = q.filter(Notice.pinned.is_(True))
    # 先按置顶、再按置顶时间：后置顶的排前面，符合"刚设为重点"的直觉；
    # 未置顶的 pinned_at 为 NULL，DESC 时排最后，正好让位
    page = _int_arg("page", 1, 10000)
    size = _int_arg("size", DEFAULT_SIZE, MAX_SIZE)
    # 筛选只收窄明细（matched），不动家底（published/pending/offline/todo/pinned）
    matched = q.count()
    pages = max(1, (matched + size - 1) // size)
    # 页码越界回到最后一页：不然 `?page=99` 得到一张空表，
    # 而页脚写着"99 / 3"，用户只会以为自己筛错了
    if page > pages:
        page = pages
    rows = (q.order_by(Notice.pinned.desc(), Notice.pinned_at.desc(),
                       Notice.created_at.desc())
            .offset((page - 1) * size).limit(size).all())
    prog = _local_progress([n.id for n in rows])
    # 卡片数字必须和列表同源：物业看到的"待确认"若按全表算，会出现
    # 卡片写 1 条、点进去 0 条——哪边才是真的，用户没法判断
    base = staff_query(u.role if u else "")
    counts = {s: base.filter_by(status=s).count()
              for s in ("published", "pending", "offline")}
    counts["todo"] = todo_count(u.role if u else "")
    counts["pinned"] = base.filter(Notice.pinned.is_(True)).count()
    # 分页字段与 /api/ledgers、/api/orders 同名，前端三处逻辑可以照抄
    counts.update({"matched": matched, "page": page, "size": size,
                   "pages": pages, "shown": len(rows),
                   "truncated": matched > len(rows)})
    return ok({"items": [_row(n, progress=prog) for n in rows], "counts": counts,
               "max_pinned": MAX_PINNED})


@bp.post("/dispatch")
@perm_required("notice.import")
def dispatch():
    """在线发文：街道把文件直接投给社区，不必先落盘

    和「扫描目录」的区别在**是谁发的、发给谁**：
    目录投递是批量分发，谁放的看不出来；这里点对点投递，投递人、收文单位、
    时限都留痕，社区工作台上会出现一条待办。两种方式并存，
    对应"批量转发"和"定向交办"两种真实场景。
    """
    d = request.get_json(silent=True) or {}
    title = (d.get("title") or "").strip()
    body = (d.get("body_md") or "").strip()
    issuer = (d.get("issuer") or "").strip()
    if not title:
        return fail("请填写文件标题")
    if not body:
        return fail("请填写文件正文")
    if not issuer:
        return fail("请填写发文单位")

    from app.notice.parse import _parse_date
    from app.auth import level_of

    lv = level_of(current_user())
    n = Notice(
        title=title[:160], issuer=issuer[:64],
        doc_no=(d.get("doc_no") or "").strip()[:64],
        issued_at=_parse_date(d.get("issued_at")) or datetime.now().date(),
        category=(d.get("category") or "通知公告").strip()[:32],
        level=lv, scope=(d.get("scope") or "all").strip(),
        body_md=body, summary=body.replace("\n", "")[:120],
        kind="superior", recv_status="dispatched",
        receiver_org=(d.get("receiver_org") or "").strip()[:64],
        deadline=_parse_date(d.get("deadline")),
        status="pending",           # 社区签收确认后才对外发布
        source_file=f"{lv}在线发文", created_by=_actor(),
    )
    db.session.add(n)
    db.session.commit()
    db.session.add(AuditLog(
        actor=_actor(), agent="notice", action="dispatch",
        output_digest=f"投递上级文件《{n.title}》→ {n.receiver_org or '全体社区'}"))
    db.session.commit()
    return ok(_row(n, detail=True))


@bp.post("/<int:nid>/receive")
@perm_required("notice.receive")
def receive(nid: int):
    """签收：社区确认"收到了、要办"

    签收只留痕，不改发布状态——上级原文该不该让居民看到是另一件事，
    由社区在发布环节决定，两件事绑在一起会让"收到就得马上公开"变成硬约束。
    """
    n = db.session.get(Notice, nid)
    if not n:
        return fail("公告不存在")
    u = current_user()
    if n.receiver_org and u and u.org and u.org != n.receiver_org \
            and n.recv_status == "dispatched":
        return fail(f"该文件指定收文单位为「{n.receiver_org}」，本账号无权签收"), 403
    if n.recv_status == "received":
        return ok({"msg": "已签收过", "row": _row(n, detail=True)})
    n.recv_status = "received"
    n.received_by = u.name if u else ""
    n.received_at = datetime.now()
    db.session.add(AuditLog(
        actor=n.received_by or "system", agent="notice", action="receive",
        output_digest=f"签收上级文件《{n.title}》"))
    db.session.commit()
    return ok({"msg": "已签收", "row": _row(n, detail=True)})


@bp.post("/<int:nid>/handle")
@perm_required("notice.handle")
def handle(nid: int):
    """登记收文办理结果（办结）

    为什么单独一个接口，而不是挂在签收里：签收是"我收到了"，办结是"我办完了"，
    两件事之间可能隔几天、可能换人。合并之后周报只能算出"收到没收到"，
    算不出"办没办"——而现在周报里的「已办结/欠办」正是靠 `handled_at` 算的。

    前置条件是**已签收**：没签收就办结，在公文实务里不存在这个组合。
    """
    from app.docflow import handle_inbound
    from app.policy import decide, secret_num

    n = db.session.get(Notice, nid)
    if not n:
        return fail("公告不存在"), 404
    if (n.kind or "superior") == "local" or n.direction == "out":
        return fail("办结只针对上级来文；本级发文请走发文流程"), 400

    u = current_user()
    # 和查看详情同一条 ABAC 规则：看不了正文的人不能替它办结
    d = decide(u, "read", "doc", secret=secret_num(n.secret_level))
    if not d.allow:
        return fail(d.reason), 403

    if n.recv_status != "received":
        return fail("请先签收再登记办理结果：没签收就办结，"
                    "在公文里是不存在的状态"), 409

    data = request.get_json(silent=True) or {}
    try:
        handle_inbound(n, data.get("result") or "",
                       actor=(u.name if u else ""), actor_id=(u.id if u else None))
    except ValueError as e:
        return fail(str(e)), 400

    db.session.add(AuditLog(
        actor=(u.name if u else "") or "system", agent="notice", action="handle",
        output_digest=f"办结上级来文《{n.title}》："
                      f"{(n.handle_result or '')[:60]}"))
    db.session.commit()
    return ok({"msg": "已登记办理结果", "row": _row(n, detail=True)})


@bp.get("/<int:nid>/draft")
@perm_required("notice.localize")
def draft(nid: int):
    """取落地件草稿：优先模型，模型不可用降级规则模板"""
    n = db.session.get(Notice, nid)
    if not n:
        return fail("公告不存在")
    u = current_user()
    org = (u.org if u and u.org else "") or "本社区"
    # 咨询电话取本单位对外公开的那个号，不用个人手机——
    # 落在通知上的号码是要张贴出去的，写个人手机等于把社工的手机号公开
    from app.models import OrgContact
    c = OrgContact.query.filter_by(name=org, active=True).first()
    phone = (c.phone if c and c.phone else "")
    from app.notice.localize import draft as make_draft
    d = make_draft(n, org, phone)
    return ok({"title": d["title"], "body_md": d["body_md"], "source": d["source"],
               "parent_id": n.id, "deadline": n.deadline.strftime("%Y-%m-%d") if n.deadline else ""})


@bp.post("/<int:nid>/localize")
@perm_required("notice.localize")
def localize(nid: int):
    """创建社区落地件：一条新记录，parent_id 指回上级原文"""
    p = db.session.get(Notice, nid)
    if not p:
        return fail("上级文件不存在")
    d = request.get_json(silent=True) or {}
    title = (d.get("title") or "").strip()
    body = (d.get("body_md") or "").strip()
    if not title or not body:
        return fail("标题与正文都要填")

    u = current_user()
    org = (u.org if u and u.org else "") or "本社区"
    from app.notice.parse import _parse_date

    n = Notice(
        title=title[:160], issuer=org, doc_no="",
        issued_at=datetime.now().date(),
        category="通知公告", level="社区",
        scope=(d.get("scope") or "all").strip(),
        body_md=body, summary=body.replace("\n", "")[:120],
        kind="local", parent_id=p.id, recv_status="",
        deadline=_parse_date(d.get("deadline")) or p.deadline,
        status="published" if d.get("publish") else "pending",
        source_file=f"依据 {p.source_file or '上级文件'}", created_by=_actor(),
    )
    if n.status == "published":
        n.published_at = datetime.now()
    db.session.add(n)
    db.session.commit()
    db.session.add(AuditLog(
        actor=_actor(), agent="notice", action="localize",
        output_digest=f"依据《{p.title}》生成落地件《{n.title}》"))
    db.session.commit()
    return ok(_row(n, detail=True))


@bp.get("/<int:nid>")
@perm_required("notice.view")
def detail(nid: int):
    n = db.session.get(Notice, nid)
    if not n:
        return fail("公告不存在")
    u = current_user()
    if not can_view(n, u.role if u else ""):
        return fail("公告不存在")
    return ok(_row(n, detail=True))


@bp.post("/scan")
@perm_required("notice.import")
def scan():
    """扫描 data/notices/ 目录：解析 → 编排 → 自动发布（字段完整时）"""
    publish_now = request.get_json(silent=True) or {}
    r = scan_and_import(actor=_actor(), publish=bool(publish_now.get("auto_publish", True)))
    return ok({
        "imported": r["imported"], "updated": r["updated"],
        "skipped": r["skipped"], "pending": r["pending"], "failed": r["failed"],
        "msg": (f"新发布 {len(r['imported'])} 条，待确认 {len(r['pending'])} 条，"
                f"已存在跳过 {len(r['skipped'])} 条，失败 {len(r['failed'])} 条"),
    })


@bp.post("/<int:nid>/publish")
@perm_required("notice.manage")
def do_publish(nid: int):
    okk, msg = publish(nid, _actor())
    return ok({"msg": msg}) if okk else fail(msg)


@bp.post("/<int:nid>/offline")
@perm_required("notice.manage")
def do_offline(nid: int):
    okk, msg = take_offline(nid, _actor())
    return ok({"msg": msg}) if okk else fail(msg)


@bp.post("/<int:nid>/pin")
@perm_required("notice.manage")
def do_pin(nid: int):
    """置顶 / 取消置顶。不传 pinned 时按当前状态取反（列表上一个按钮两用）

    为什么用 notice.manage 而不是新开权限点：置顶决定"哪份文件排在最前面"，
    和发布/下架一样是属地发布口径的一部分——排在前面的文件居民最先读到，
    这跟"发不发"是同一类责任，不该拆给两个人。
    """
    n = db.session.get(Notice, nid)
    if not n:
        return fail("公告不存在")
    d = request.get_json(silent=True) or {}
    was = bool(n.pinned)
    target = (not was) if d.get("pinned") is None else bool(d.get("pinned"))
    err = _apply_pin(n, target)
    if err:
        return fail(err)
    if target == was:
        # 重复点不该留下两条"置顶"审计，否则追责时看不出到底改了几次
        return ok({"msg": "已经是置顶" if was else "当前未置顶",
                   "pinned": was, "pinned_at": ""})
    db.session.add(AuditLog(
        actor=_actor(), agent="notice", action="pin" if n.pinned else "unpin",
        output_digest=f"{'置顶' if n.pinned else '取消置顶'}公告《{n.title}》"))
    db.session.commit()
    msg = "已置顶" if n.pinned else "已取消置顶"
    if n.pinned and n.status != "published":
        # 置顶只影响排序，不影响可见性——不说明的话，人会以为置顶等于发布
        msg += "（该公告尚未发布，居民端暂时看不到）"
    return ok({"msg": msg, "pinned": n.pinned,
               "pinned_at": n.pinned_at.strftime("%Y-%m-%d %H:%M") if n.pinned_at else ""})


@bp.patch("/<int:nid>")
@perm_required("notice.manage")
def patch(nid: int):
    """补全字段：解析不出来的部分由人补，补完即可发布"""
    n = db.session.get(Notice, nid)
    if not n:
        return fail("公告不存在")
    d = request.get_json(silent=True) or {}
    from app.notice.parse import _parse_date

    if "title" in d and str(d["title"]).strip():
        n.title = str(d["title"]).strip()[:160]
    if "issuer" in d:
        n.issuer = str(d["issuer"]).strip()[:64]
    if "doc_no" in d:
        n.doc_no = str(d["doc_no"]).strip()[:64]
    if "issued_at" in d:
        n.issued_at = _parse_date(d["issued_at"]) or n.issued_at
    if "category" in d and str(d["category"]).strip():
        n.category = str(d["category"]).strip()[:32]
    if "summary" in d:
        n.summary = str(d["summary"]).strip()[:256]
    if not n.issuer:
        return fail("发文单位仍为空，无法通过校验")
    # 补全表单里也带置顶开关，走同一套校验与时间戳维护，
    # 不能在这里直接 n.pinned = bool(...)，否则 pinned_at 不跟着变
    if "pinned" in d:
        err = _apply_pin(n, bool(d["pinned"]))
        if err:
            return fail(err)

    db.session.add(AuditLog(actor=_actor(), agent="notice", action="edit",
                            output_digest=f"补全公告《{n.title}》字段"))
    db.session.commit()
    return ok(_row(n, detail=True))
