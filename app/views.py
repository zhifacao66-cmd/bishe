"""页面视图：登录 / 登出 + 5 个业务页面（全部挂权限守卫）"""
from flask import Blueprint, redirect, render_template, request, url_for

from app.params import safe_back
from app.auth import (DEMO_USERS, ROLE_HOME, AccountDisabled, LoginThrottled,
                      current_user, has_perm, login as do_login,
                      logout as do_logout, login_required, perm_required)
# 导出留痕的唯一实现在 app/exportlog.py（底册导出也要用同一份）。
from app.exportlog import log_export as _log_export

bp = Blueprint("views", __name__)


def _login_page(**kw):
    """登录页统一入口

    演示账号卡片上的姓名和单位**从 DEMO_USERS 取**，不在模板里写死。
    之前写死过一次，改了账号单位之后页面还显示旧名字，同一个人两个单位，
    演示时一眼就被看出来。
    """
    return render_template("login.html", demo_users=DEMO_USERS, **kw)


@bp.route("/login", methods=["GET", "POST"])
def login():
    u = current_user()
    if u:
        return redirect(url_for(ROLE_HOME.get(u.role, "views.index")))

    err = None
    username = ""
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        pwd = request.form.get("password") or ""
        try:
            user = do_login(username, pwd)
        except LoginThrottled as e:
            # 与"密码错误"分开提示：不告诉试探者"账号对不对"，只说"先别试了"
            return _login_page(err=str(e), username=username,
                               next=request.args.get("next", "")), 429
        except AccountDisabled as e:
            # 走到这里说明密码是对的，告诉本人"被停用"不构成账号枚举
            return _login_page(err=str(e), username=username,
                               next=request.args.get("next", ""))
        if user:
            nxt = request.args.get("next") or request.form.get("next") or ""
            # 防开放重定向：只接受站内相对路径
            if not (nxt.startswith("/") and not nxt.startswith("//")):
                nxt = url_for(ROLE_HOME.get(user.role, "views.index"))
            return redirect(nxt)
        err = "用户名或密码错误"
    return _login_page(err=err, username=username,
                       next=request.args.get("next", ""))


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    do_logout()
    return redirect(url_for("views.login"))


@bp.get("/")
@login_required
def index():
    # 物业/街道没有首录权限，直接落到各自的工作页，而不是弹 403
    if not has_perm("fde.create"):
        return redirect(url_for(ROLE_HOME.get(current_user().role, "views.orders")))
    return render_template("index.html")


@bp.get("/tasks")
@perm_required("task.view")
def tasks():
    return render_template("tasks.html")


@bp.get("/orders")
@perm_required("order.view")
def orders():
    return render_template("orders.html")


@bp.get("/process")
@perm_required("order.view")
def process_page():
    """流程监控台。

    权限用 `order.view` 而不是新开一个 `process.view`：
    能看工单的人本来就该知道这些单卡在哪；反过来，只给"监督者"看
    会让一线看不到自己名下的单为什么被标红。行级收窄在 API 层做
    （物业只看自己单位的单），这里只管能不能进这个页面。
    """
    return render_template("process.html")


@bp.get("/risk")
@perm_required("order.view")
def risk_page():
    """风险预警台。见 `api/insight.py` 里关于"居民诉求原文只对
    社区/街道/管理员开放"的说明——页面能进，但热点那块会按角色隐藏。"""
    return render_template("risk.html")


@bp.get("/trace")
@perm_required("fde.view")
def trace_page():
    """Agent 协作轨迹。

    权限用 `fde.view` 而不是 `order.view`：轨迹里有拆解过程、字段密级判定、
    以及"系统认定这户是反复投诉户"这类内部判断。物业持有 `order.view`
    但显然不该看到自己被投诉了多少次，所以页面和接口都用更窄的权限，
    接口侧再加一层角色硬判（见 `api/collab.py`）。
    """
    return render_template("trace.html")


@bp.get("/persons")
@perm_required("person.view")
def persons_page():
    """人员底册。

    页面只负责"看"，不给编辑：底册由街道下发、系统导入，
    在页面里改会和下发表对不上，下次导入又被覆盖，责任还说不清。
    真要改只能重导（scripts/import_ledger.py），留痕在导入批次上。
    """
    return render_template("persons.html")


@bp.get("/ledgers")
@perm_required("ledger.view")
def ledgers():
    return render_template("ledgers.html")


@bp.get("/reports")
@perm_required("report.view")
def reports():
    return render_template("reports.html")


@bp.get("/reports/<int:rid>/export/<fmt>")
@perm_required("report.view")
def report_export(rid: int, fmt: str):
    """导出报表：xlsx（报送件）

    权限用 report.view 而非 generate：街道要拿文件去汇报、社区要拿去盖章，
    "能看"就该"能导出"，否则只能截屏贴进 Word。

    内容只取报表快照（report.data_json），不重新统计——同一份报表什么时候
    导都是同一组数字，归档件才有证据效力。
    """
    import io

    from flask import abort, send_file

    from app.extensions import db
    from app.models import Report
    from app.report.export import build

    if fmt.lower() not in ("xlsx", "excel"):
        abort(400, description=f"不支持导出格式：{fmt}")

    r = db.session.get(Report, rid)
    if not r:
        abort(404)
    from app.policy import decide
    from app.watermark import text as wm_text
    d = decide(current_user(), "export", "report")
    if not d.allow:
        abort(403, description=d.reason)
    mark = wm_text(current_user(), "导出报表") if d.watermark else ""
    data, fname = build(r, mark=mark)
    _log_export("report", f"导出报表 {r.period} 第 {r.version} 版（{fname}）")
    return send_file(
        io.BytesIO(data), as_attachment=True, download_name=fname,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.get("/audit")
@perm_required("audit.view")
def audit():
    return render_template("audit.html")


@bp.get("/security/policy")
@perm_required("audit.view")
def policy():
    """访问策略：规则自述 + 现场试算

    权限用 `audit.view` 而不是新开一个：能看审计日志的人，本来就该知道
    "什么被谁看走了"。反过来，业务账号看这个页面没有意义——
    他们改不了规则，看了只会问"为什么我不能看"。
    """
    return render_template("policy.html")


@bp.get("/kb")
@perm_required("kb.view")
def kb():
    return render_template("kb.html")


@bp.get("/health")
def health():
    return {"status": "ok"}


@bp.get("/system")
@perm_required("user.manage")
def system():
    # 模型通道自检页：只给 admin。它暴露部署形态（通道地址、模型、延迟），
    # 属于系统运维信息，不是业务信息，不该让普通社区账号看到。
    return render_template("system.html")


@bp.get("/notices")
@perm_required("notice.view")
def notices():
    # 筛选条件进 URL，刷新与"从详情返回"都能原样恢复
    return render_template("notices.html", cur_status=request.args.get("status") or "",
                           cur_recv=request.args.get("recv") or "",
                           cur_pin=request.args.get("pin") or "")


@bp.get("/docs")
@perm_required("doc.view")
def docs():
    """发文台：拟稿、核稿、签发、用印、分发、归档

    页面只做壳，列表与详情都走 /api/docs —— 和公告页一样，
    避免"服务端渲染一套、接口又一套"导致两处口径打架。
    """
    return render_template("docs.html")


@bp.get("/docs/<int:did>")
@perm_required("doc.view")
def doc_detail(did: int):
    """公文详情：按 GB/T 9704 的要素顺序排版，可直接打印"""
    from flask import abort
    from app.extensions import db
    from app.models import DocLog, Notice
    from app.notice.render import render as render_md
    from app.docflow import LABEL
    n = db.session.get(Notice, did)
    if not n or n.direction != "out":
        abort(404)
    u = current_user()
    # 页面与接口同一口径：接口挡住了、模板还在渲染，等于没修。
    # 密级判断统一走 ABAC（与 /api/docs/<id> 命中同一条规则、同一句 reason），
    # 不再在两处各写一遍 `secret_level in (...)`。
    from app.policy import decide, secret_num
    if not decide(u, "read", "doc", secret=secret_num(n.secret_level)).allow:
        abort(403)
    logs = (DocLog.query.filter_by(notice_id=n.id).order_by(DocLog.id.asc()).all())
    return render_template("doc_detail.html", doc=n, body=render_md(n.body_md),
                           logs=logs, labels=LABEL, back=safe_back("/docs"))


@bp.get("/weekly")
@perm_required("weekly.view")
def weekly():
    return render_template("weekly.html")


@bp.get("/weekly/<int:wid>")
@perm_required("weekly.view")
def weekly_detail(wid: int):
    from flask import abort
    import json as _json
    from app.extensions import db
    from app.models import Weekly
    from app.notice.render import render as render_md
    w = db.session.get(Weekly, wid)
    if not w:
        abort(404)
    try:
        data = _json.loads(w.data_json or "{}")
    except (ValueError, TypeError):
        data = {}
    return render_template("weekly_detail.html", w=w, body=render_md(w.body_md),
                           data=data, back=safe_back("/weekly"))


@bp.get("/notices/<int:nid>")
@perm_required("notice.view")
def notice_detail(nid: int):
    from flask import abort
    from app.extensions import db
    from app.models import Notice
    from app.notice.render import render as render_md
    n = db.session.get(Notice, nid)
    if not n:
        abort(404)
    # 范围校验和接口同一个口径：物业不该按 id 直取社区内部公告。
    # 页面是最容易漏的一层——接口挡住了、模板还在渲染，等于没修。
    from app.notice.ingest import can_view, staff_query
    u = current_user()
    if not can_view(n, u.role if u else ""):
        abort(404)
    # 落地件要把"依据的上级文件"一起带出来，页面上才能两件对着看
    parent = db.session.get(Notice, n.parent_id) if n.parent_id else None
    children = (staff_query(u.role if u else "", Notice.query.filter_by(parent_id=n.id))
                .order_by(Notice.id.desc()).all())
    # 办完这一件之后"还有多少件等着办"——详情页用它给出继续办理的引导，
    # 不让用户办完一条就断在这里、必须自己回列表再找下一条。
    # 走 todo_query：别家的待办不算进我的数里，否则"还有 3 条"点过去只有 1 条
    from app.api.notices import MAX_PINNED, todo_count
    todo = todo_count(u.role if u else "")
    # 置顶额度要在按钮旁边就看得见。等点了被拒才知道满了，
    # 用户只会觉得"这功能坏了"，而不是"该先撤一条"
    pinned_count = Notice.query.filter(Notice.pinned.is_(True)).count()
    return render_template("notice_detail.html", notice=n, body=render_md(n.body_md),
                           parent=parent, children=children,
                           back=safe_back("/notices"), todo_count=todo,
                           pinned_count=pinned_count, max_pinned=MAX_PINNED)


@bp.get("/notices/<int:nid>/export/<fmt>")
@perm_required("notice.view")
def notice_export(nid: int, fmt: str):
    """导出文档：docx（红头公文）/ md（归档件）/ txt（纯文本）

    权限用的是 notice.view 而不是 manage：居民端和街道都需要拿文件去贴、去报，
    "能看"就该"能导出"，否则只能截屏。
    """
    import io

    from flask import abort, send_file

    from app.extensions import db
    from app.models import Notice
    from app.notice.export import ExportUnavailable, build
    from app.notice.ingest import can_view

    n = db.session.get(Notice, nid)
    if not n:
        abort(404)
    # 导出比"看"更严重：文件一旦下载就脱离系统，拦不住二次传播。
    # 所以这里必须自己判一次范围，不能假设"列表里有就等于能导出"。
    from app.policy import decide, secret_num
    from app.watermark import text as wm_text
    u = current_user()
    if not can_view(n, (u.role if u else "")):
        abort(404)
    # 密级这一层交给 ABAC：同一个规则表也管着 docs.py 的接口和 doc_detail 页面，
    # 三处口径从此只有一份（以前是手写两遍，改一处漏一处）。
    # owner_org 传空：公文归属由 scope 表达，scope 已在 can_view 里判过。
    d = decide(u, "export", "doc", secret=secret_num(n.secret_level))
    if not d.allow:
        abort(403, description=d.reason)
    mark = wm_text(u, "导出公文") if d.watermark else ""
    try:
        data, fname, mime = build(n, fmt, mark=mark)
    except ExportUnavailable as e:
        abort(400, description=str(e))
    # 导出动作本身要留痕：文件下载后脱离系统，事后追"这份公文是谁拿走的"
    # 只能靠这条记录。放在 build 之后，导出失败（格式不支持）不记账。
    _log_export("notice", f"导出公文《{n.title}》 {fmt}（{fname}）")
    return send_file(io.BytesIO(data), mimetype=mime,
                     as_attachment=True, download_name=fname)


@bp.get("/notices/<int:nid>/localize")
@perm_required("notice.localize")
def notice_localize(nid: int):
    """办文页：在上级原文基础上生成社区落地件"""
    from flask import abort

    from app.extensions import db
    from app.models import Notice

    n = db.session.get(Notice, nid)
    # 与 `/api/notices/<id>/localize` 同源：看不了正文的人不能打开办文页
    # （页面会把原文渲染进编辑器）。少了这一句，out 公文虽然在 API 被拒，
    # 从页面仍能拿到完整正文。
    from app.notice.ingest import can_view
    _u = current_user()
    if not n or not can_view(n, _u.role if _u else ""):
        abort(404)
    from datetime import date as _date
    from app.notice.render import render as render_md
    own = f"/notices/{nid}"
    b = safe_back(own)
    return render_template("notice_localize.html", notice=n, today=_date.today(),
                           body=render_md(n.body_md),
                           # 办文页的"取消"要回到原文详情，而原文详情又记着列表来源，
                           # 这是一条返回链，把 back 继续传下去才不断。
                           # back 恰好就是原文详情自己时置空——否则会生成
                           # /notices/3?back=/notices/3 这种自我指涉的地址。
                           back=(b if b != own else ""))
