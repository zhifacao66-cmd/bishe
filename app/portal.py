"""居民端页面（免登录）

居民端刻意**不复用内部端的侧边栏布局**：内部端是"一屏多卡"的工位视角，
居民多在手机上打开，需要的是大按钮、短路径、一次只做一件事。
两套布局通过页头/页脚的入口互相跳转：
    内部登录页 → 「我是居民」
    居民端页头 → 「工作人员入口」
"""
from flask import (Blueprint, abort, redirect, render_template, request, session,
                   url_for)

from app.extensions import db
from app.models import Notice, ResidentTicket
from app.notice.ingest import visible_query
from app.notice.render import render as render_md
from app.portal_seed import grouped_contacts

bp = Blueprint("portalviews", __name__)


@bp.get("/portal")
def home():
    # 一律走 visible_query("resident")：只给「已发布 且 scope=all」的公告。
    # 居民端是公开入口，绝不能只按 status 过滤——否则一条 scope=community 的
    # 内部通知只要被发布了，居民在首页就能看到（越权读取，见 docs/04）。
    notices = (visible_query("resident")
               .order_by(Notice.pinned.desc(), Notice.published_at.desc()).limit(3).all())
    recent = []
    for tid in session.get("portal_tickets", [])[:3]:
        t = db.session.get(ResidentTicket, tid)
        if t:
            recent.append(t)
    return render_template("portal/home.html", notices=notices, recent=recent,
                           groups=grouped_contacts())


@bp.get("/portal/report")
def report():
    return render_template("portal/report.html")


@bp.get("/portal/done/<int:tid>")
def done(tid: int):
    t = db.session.get(ResidentTicket, tid)
    if not t or tid not in session.get("portal_tickets", []):
        return redirect(url_for("portalviews.home"))
    return render_template("portal/done.html", ticket=t)


@bp.get("/portal/my")
def my():
    rows = []
    for tid in session.get("portal_tickets", []):
        t = db.session.get(ResidentTicket, tid)
        if t:
            rows.append(t)
    return render_template("portal/my.html", tickets=rows)


@bp.get("/portal/query")
def query():
    return render_template("portal/query.html")


@bp.get("/portal/ticket/<int:tid>")
def ticket(tid: int):
    t = db.session.get(ResidentTicket, tid)
    if not t or tid not in session.get("portal_tickets", []):
        abort(404)
    from app.api.portal import _ticket_payload
    return render_template("portal/ticket.html", ticket=t, view=_ticket_payload(t))


@bp.get("/portal/notices")
def notices():
    items = (visible_query("resident")
             .order_by(Notice.pinned.desc(), Notice.published_at.desc()).limit(100).all())
    return render_template("portal/notices.html", notices=items)


@bp.get("/portal/notice/<int:nid>")
def notice(nid: int):
    """公告详情。mode=view 网页阅读 / print 打印版 / official 发布版"""
    n = db.session.get(Notice, nid)
    # 详情页按 id 直取，必须自己再判一次状态与范围：
    # 光有列表过滤挡不住手工拼 URL 枚举 id（居民端没有登录态，更不能依赖前端）。
    if not n or n.status != "published" or (n.scope or "all") != "all":
        abort(404)
    mode = request.args.get("mode", "view")
    if mode not in ("view", "print", "official"):
        mode = "view"
    body = render_md(n.body_md)
    if mode == "view":
        return render_template("portal/notice.html", notice=n, body=body)
    from datetime import datetime
    return render_template("portal/notice_doc.html", notice=n, body=body, mode=mode,
                           now=datetime.now().strftime("%Y-%m-%d %H:%M"))


@bp.get("/portal/contacts")
def contacts():
    return render_template("portal/contacts.html", groups=grouped_contacts())
