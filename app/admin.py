"""账号治理页面：自助注册 / 待审核落地页 / 管理员用户与权限管理

为什么需要一个"注册 + 审核"的流程，而不是直接发账号：
社区工作者是流动岗位，新同事到岗时管理员往往不在现场。如果只能由管理员
建号，实际结果就是"共用老账号"——审计里全是同一个名字，责任反而说不清。
所以让新人自己填表，管理员事后审，审的时候顺手给他点权限。

权限模型的取舍见 app/models.py 里 User 的注释：
角色给默认值，管理员按人增减，两者都有明确优先级。
"""
from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for, session)
from werkzeug.security import generate_password_hash

from app.auth import (PERM_GROUPS, PERMS, ROLE_NAMES, ROLE_PERMS, current_user,
                      login_required, perm_required, role_name, status_name,
                      user_perms, valid_username)
from app.extensions import db
from app.models import AuditLog, User
from app.params import safe_back

bp = Blueprint("admin", __name__)


def _valid_username(s: str) -> bool:
    return valid_username(s)


def _audit(actor: str, action: str, detail: str):
    db.session.add(AuditLog(actor=actor[:32], agent="account",
                            action=action, output_digest=detail[:250]))
    db.session.commit()


# ---------------------------------------------------------------- 自助注册

@bp.route("/register", methods=["GET", "POST"])
def register():
    """新员工自助注册：落库即"社区工作者 + 待审核 + 零权限"

    这里的默认值全部是**最保守**的：角色填 community 只是为了让表单有个
    默认选项，真正生效的是管理员审核时勾的那几个权限点。
    """
    if current_user():
        return redirect(url_for("views.index"))

    err = None
    form = {"username": "", "name": "", "org": "幸福里社区", "phone": ""}

    if request.method == "POST":
        # 自助注册是**公网可写的入口**：不限流的话，一个脚本能在一分钟内
        # 灌进几千个待审核账号——管理员列表被刷屏，审核功能等于废掉；
        # 同时"该账号已被占用"这句提示本身就是账号枚举工具。
        # 按 IP 限 5 次/小时（弱于按账号限，但注册阶段还没有账号可用）。
        from app.ratelimit import allow
        ip = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
              or request.remote_addr or "unknown")
        if not allow(f"register:{ip}", 5, 3600):
            return render_template("register.html", err="提交太频繁，请 1 小时后再试，"
                                   "或直接联系管理员开通账号",
                                   form=form), 429

        form = {
            "username": (request.form.get("username") or "").strip(),
            "name": (request.form.get("name") or "").strip(),
            "org": (request.form.get("org") or "").strip(),
            "phone": (request.form.get("phone") or "").strip(),
        }
        pwd = request.form.get("password") or ""
        pwd2 = request.form.get("password2") or ""

        if not _valid_username(form["username"]):
            err = "账号需以字母开头，3~20 位，只含字母、数字、下划线"
        elif not form["name"]:
            err = "请填写真实姓名，管理员要靠它核对身份"
        elif not form["org"]:
            err = "请填写所属单位"
        elif len(pwd) < 8:
            # 社区账号里挂着居民信息，6 位密码在离线爆破面前基本等于没有
            err = "密码至少 8 位"
        elif pwd != pwd2:
            err = "两次输入的密码不一致"
        elif User.query.filter_by(username=form["username"]).first():
            err = "该账号已被占用，请换一个"

        if not err:
            u = User(
                username=form["username"], name=form["name"], org=form["org"],
                phone=form["phone"][:16], role="community",
                status="pending",
                perms_json="[]",          # 明确"一个权限都不给"，等管理员点
                created_by="自助注册",
                password_hash=generate_password_hash(pwd),
            )
            db.session.add(u)
            db.session.commit()
            _audit(u.name, "register", f"自助注册申请：{u.username}（{u.org}），待审核")
            # 注册即登录，让他能立刻看到"在等审核"，而不用反复试登录
            session.clear()
            session["uid"] = u.id
            session["role"] = u.role
            session.permanent = True
            return redirect(url_for("admin.pending"))

    return render_template("register.html", err=err, form=form)


# ---------------------------------------------------------------- 待审核

@bp.get("/pending")
@login_required
def pending():
    """待审核落地页：说明在等谁、等什么、还要多久能好

    这页也承担"审核通过后自动进入"的作用——前端轮询 /api/admin/me，
    一旦状态变成 active 就跳走，不需要用户手动刷新或重新登录。
    （权限是从数据库实时算的，所以管理员一点保存，这边立刻就生效。）
    """
    u = current_user()
    st = u.status or "active"
    if st == "active":
        from app.auth import ROLE_HOME
        return redirect(url_for(ROLE_HOME.get(u.role, "views.index")))
    return render_template("pending.html", u=u, status=st)


# ---------------------------------------------------------------- 管理员：用户列表

@bp.get("/admin/users")
@perm_required("user.manage")
def users():
    st = (request.args.get("status") or "").strip()
    qs = User.query
    if st == "pending":
        qs = qs.filter(User.status == "pending")
    elif st == "disabled":
        qs = qs.filter(User.status == "disabled")
    elif st == "active":
        # 老数据 status 为 NULL 一律视为正常，与 user_perms 口径一致
        qs = qs.filter(db.or_(User.status == "active", User.status.is_(None)))
    rows = qs.order_by(User.id.asc()).all()

    all_rows = User.query.all()
    counts = {
        "total": len(all_rows),
        "pending": sum(1 for r in all_rows if (r.status or "active") == "pending"),
        "active": sum(1 for r in all_rows if (r.status or "active") == "active"),
        "disabled": sum(1 for r in all_rows if (r.status or "active") == "disabled"),
    }
    return render_template("admin_users.html", rows=rows, counts=counts,
                           perms_of=user_perms, status_name=status_name,
                           groups=PERM_GROUPS, perms=PERMS,
                           role_perms=ROLE_PERMS, role_names=ROLE_NAMES,
                           # ROLE_PERMS 的值是 set，模板里 tojson 会报
                           # "Object of type set is not JSON serializable"，
                           # 前端要的是数组，这里先排好序转成 list
                           role_perms_json={k: sorted(v) for k, v in ROLE_PERMS.items()})


@bp.get("/admin/users/<int:uid>")
@perm_required("user.manage")
def user_detail(uid: int):
    from flask import abort
    u = db.session.get(User, uid)
    if not u:
        abort(404)
    me = current_user()

    # 打开页面时权限框该勾成什么样，有三种情况，必须在后端算清楚：
    #   管理员      → 全量（且不可编辑）
    #   待审核      → 按角色默认预勾。他库里存的是 "[]"，那是"还没授权"的占位，
    #                 不是"管理员决定不给"，照实渲染会让管理员手点 11 下才能通过审核
    #   其余        → 如实反映（跟随角色或已自定义）
    is_admin = (u.role == "admin")
    st = u.status or "active"
    custom = u.perms_json is not None
    follow_role = is_admin or (not custom) or (st == "pending")
    if is_admin:
        shown = set(PERMS)
    elif follow_role:
        shown = set(ROLE_PERMS.get(u.role, set()))
    else:
        shown = user_perms(u)

    return render_template(
        "admin_user.html", u=u, me=me, is_admin=is_admin,
        groups=PERM_GROUPS, perms=PERMS, role_perms=ROLE_PERMS,
        role_names=ROLE_NAMES, effective=sorted(shown),
        follow_role=follow_role, custom=custom,
        status_name=status_name,
        # 列表页是按 status 筛的（?status=pending），返回时不带上就把筛选丢了
        back=safe_back("/admin/users"),
    )
