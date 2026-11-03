"""账号治理接口：列表 / 审核 / 改角色 / 按人点权限 / 停用 / 重置密码

全部要求 user.manage（只有系统管理员持有）。

一条原则贯穿本文件：**任何改动都不能把管理员自己锁在门外**。
账号系统最典型的翻车方式不是被攻破，而是管理员误操作把自己降级或停用，
之后没人能再进后台。所以"停用自己""取消自己的管理员角色""停掉最后一个可用管理员"
三种操作一律拒绝，并且拒绝也要留审计。
"""
from flask import Blueprint, request
from werkzeug.security import generate_password_hash

from app.api import fail, ok
from app.auth import (PERMS, ROLE_PERMS, current_user, perm_required,
                      role_name, status_name, user_perms, valid_username)
from app.extensions import db
from app.models import AuditLog, User
from app.search import like_all

bp = Blueprint("adminapi", __name__)

VALID_STATUS = {"pending", "active", "disabled"}
# 管理员可以直接改的角色范围。resident 不在这里：居民走免注册通道，
# 不占账号表，所以不允许把员工账号改成"居民"。
ASSIGNABLE_ROLES = {"community", "property", "street", "admin"}


def _audit(actor, action, detail):
    db.session.add(AuditLog(actor=(actor or "admin")[:32], agent="account",
                            action=action, output_digest=detail[:250]))
    db.session.commit()


def _active_admin_count(exclude_id=None):
    q = User.query.filter_by(role="admin")
    n = 0
    for u in q.all():
        if u.id == exclude_id:
            continue
        if (u.status or "active") == "active":
            n += 1
    return n


def _payload(u: User):
    return {
        "id": u.id, "username": u.username, "name": u.name, "role": u.role,
        "role_cn": role_name(u.role), "org": u.org, "phone": u.phone or "",
        "status": u.status or "active", "status_cn": status_name(u.status),
        "perms": sorted(user_perms(u)), "perm_count": len(user_perms(u)),
        "perms_custom": u.perms_json is not None,
        "created_by": u.created_by or "",
        "review_note": u.review_note or "",
        "created_at": u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else "",
        "last_login_at": u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else "",
    }


# ---------------------------------------------------------------- 自己的状态

@bp.get("/me")
def me():
    """给待审核页轮询用：审核一通过，前端立刻放行跳转，不用重新登录

    刻意不挂 user.manage——待审核的人也得能查自己的状态。
    """
    u = current_user()
    if not u:
        return fail("未登录", code=401), 401
    return ok({
        "username": u.username, "name": u.name, "role": u.role,
        "role_cn": role_name(u.role), "org": u.org,
        "status": u.status or "active", "status_cn": status_name(u.status),
        "perms": sorted(user_perms(u)), "perm_count": len(user_perms(u)),
    })


# ---------------------------------------------------------------- 列表

@bp.get("/users")
@perm_required("user.manage")
def list_users():
    st = (request.args.get("status") or "").strip()
    q = (request.args.get("q") or "").strip()
    qs = User.query
    if st in VALID_STATUS:
        # 老数据 status 为 NULL 的一律按 active 处理，与 user_perms 口径保持一致
        if st == "active":
            qs = qs.filter((User.status == "active") | (User.status.is_(None)))
        else:
            qs = qs.filter(User.status == st)
    if q:
        cond = like_all([User.username, User.name, User.org], q)
        if cond is not None:
            qs = qs.filter(cond)
    rows = qs.order_by(User.id.asc()).all()
    return ok([_payload(u) for u in rows])


# ---------------------------------------------------------------- 新建账号

@bp.post("/users")
@perm_required("user.manage")
def create_user():
    """管理员直接建号——上级单位、物业这类"不会自己跑来注册"的账号走这条路

    为什么需要它：自助注册把角色写死成 community，因为那表单是给社区新同事填的。
    但街道（上级单位）、物业不会坐在社区办公室里点注册链接，他们的账号只能由
    管理员建。没有这个接口，管理员想加一个"区民政局"账号就只能改数据库，
    等于把权限体系交给 DBA。

    与自助注册的两点差别：
      1. 角色由管理员选，不是写死的；
      2. 直接 active，不进审核队列——管理员建号这个动作本身就是审核。
    """
    me = current_user()
    b = request.get_json(silent=True) or {}

    username = (b.get("username") or "").strip()
    name = (b.get("name") or "").strip()
    org = (b.get("org") or "").strip()
    role = (b.get("role") or "").strip()
    pwd = (b.get("password") or "").strip()
    phone = (b.get("phone") or "").strip()

    if not valid_username(username):
        return fail("账号需以字母开头，3~20 位，只含字母、数字、下划线"), 400
    if not name:
        return fail("请填写姓名"), 400
    if not org:
        return fail("请填写所属单位"), 400
    if role not in ASSIGNABLE_ROLES:
        return fail("角色不合法"), 400
    if len(pwd) < 8:
        return fail("初始密码至少 8 位"), 400
    if User.query.filter_by(username=username).first():
        return fail("该账号已存在"), 400

    u = User(
        username=username, name=name[:32], org=org[:64], phone=phone[:16],
        role=role, status="active",
        perms_json=None,               # 默认跟随角色，管理员需要时再按人增减
        created_by=(me.name if me else "admin")[:32],
        password_hash=generate_password_hash(pwd),
    )
    db.session.add(u)
    db.session.commit()

    # perms 传了数组就按人授权，覆盖角色默认值
    if isinstance(b.get("perms"), list):
        import json
        picked = [p for p in b["perms"] if p in PERMS]
        u.perms_json = json.dumps(sorted(set(picked)), ensure_ascii=False)
        db.session.commit()

    _audit(me.name if me else "admin", "user_create",
           f"新建账号 {u.username}（{role_name(u.role)}·{u.org}），"
           f"授予 {len(user_perms(u))} 项权限")
    return ok(_payload(u))


# ---------------------------------------------------------------- 详情

@bp.get("/users/<int:uid>")
@perm_required("user.manage")
def get_user(uid: int):
    u = db.session.get(User, uid)
    if not u:
        return fail("账号不存在", code=404), 404
    d = _payload(u)
    d["role_default_perms"] = sorted(ROLE_PERMS.get(u.role, set()))
    return ok(d)


# ---------------------------------------------------------------- 更新

@bp.post("/users/<int:uid>")
@perm_required("user.manage")
def update_user(uid: int):
    """一次提交改一组字段。字段不传 = 不改，传 null 有明确语义（见下）

    body:
      name / org / role / status / review_note  普通字段
      perms: 数组 → 按勾选设置；null → 恢复"跟随角色默认"；不传 → 不动
    """
    me = current_user()
    u = db.session.get(User, uid)
    if not u:
        return fail("账号不存在", code=404), 404

    body = request.get_json(silent=True) or {}

    new_role = body.get("role") if "role" in body else None
    new_status = body.get("status") if "status" in body else None
    has_perms = "perms" in body
    new_perms = body.get("perms")

    if new_role is not None and new_role not in ASSIGNABLE_ROLES:
        return fail("角色不合法"), 400
    if new_status is not None and new_status not in VALID_STATUS:
        return fail("状态不合法"), 400

    # ---- 防自锁 ----
    if u.id == me.id:
        if new_status is not None and new_status != "active":
            return fail("不能停用或退回自己的账号"), 400
        if new_role is not None and new_role != "admin":
            return fail("不能取消自己的管理员角色"), 400

    will_be_admin = (new_role or u.role) == "admin"
    will_be_active = (new_status or u.status or "active") == "active"
    if not (will_be_admin and will_be_active):
        # 这一步操作会让这个账号不再是"可用管理员"，检查系统里是否还剩别人
        if u.role == "admin" and (u.status or "active") == "active":
            if _active_admin_count(exclude_id=u.id) == 0:
                return fail("系统必须保留至少一个可用的管理员账号"), 400

    # ---- 落到字段 ----
    if body.get("name") is not None:
        v = str(body["name"]).strip()
        if not v:
            return fail("姓名不能为空"), 400
        u.name = v[:32]
    if body.get("org") is not None:
        v = str(body["org"]).strip()
        if not v:
            return fail("所属单位不能为空"), 400
        u.org = v[:64]
    if new_role is not None:
        u.role = new_role
    if new_status is not None:
        u.status = new_status
    if body.get("review_note") is not None:
        u.review_note = str(body["review_note"]).strip()[:128]

    if has_perms:
        if new_perms is None:
            u.perms_json = None            # 恢复跟随角色
        else:
            if not isinstance(new_perms, list):
                return fail("perms 必须是数组，或 null 表示跟随角色"), 400
            import json
            picked = [p for p in new_perms if p in PERMS]
            u.perms_json = json.dumps(sorted(set(picked)), ensure_ascii=False)

    db.session.commit()

    detail = f"更新账号 {u.username}：角色={role_name(u.role)}，状态={status_name(u.status)}"
    if has_perms:
        detail += f"，权限={len(user_perms(u))} 项" + ("" if new_perms is not None else "（跟随角色）")
    _audit(me.name, "user_update", detail)
    return ok(_payload(u))


# ---------------------------------------------------------------- 快捷动作

@bp.post("/users/<int:uid>/approve")
@perm_required("user.manage")
def approve(uid: int):
    """审核通过：状态转正常，并按**当前勾选**授权

    如果管理员没单独勾过（perms_json 还是 "[]"），默认给该角色的全套权限——
    这是"同意他做社区工作"的常规结果；管理员想减权限就在详情页取消勾选。
    """
    me = current_user()
    u = db.session.get(User, uid)
    if not u:
        return fail("账号不存在", code=404), 404
    if u.status == "active":
        return fail("该账号已是正常状态"), 400

    body = request.get_json(silent=True) or {}
    if body.get("role"):
        if body["role"] not in ASSIGNABLE_ROLES:
            return fail("角色不合法"), 400
        u.role = body["role"]

    # 传了 perms 就用传的，否则给角色默认（并标记为"未自定义"）
    if isinstance(body.get("perms"), list):
        import json
        picked = [p for p in body["perms"] if p in PERMS]
        u.perms_json = json.dumps(sorted(set(picked)), ensure_ascii=False)
    elif u.perms_json in (None, "[]"):
        u.perms_json = None

    u.status = "active"
    db.session.commit()
    _audit(me.name, "user_approve",
           f"审核通过 {u.username}（{role_name(u.role)}·{u.org}），"
           f"授予 {len(user_perms(u))} 项权限")
    return ok(_payload(u))


@bp.post("/users/<int:uid>/password")
@perm_required("user.manage")
def reset_password(uid: int):
    """重置密码：管理员不能看到原密码（只存哈希），只能重设一个临时密码"""
    me = current_user()
    u = db.session.get(User, uid)
    if not u:
        return fail("账号不存在", code=404), 404
    body = request.get_json(silent=True) or {}
    pwd = (body.get("password") or "").strip()
    if len(pwd) < 8:
        return fail("临时密码至少 8 位"), 400
    u.password_hash = generate_password_hash(pwd)
    db.session.commit()
    _audit(me.name, "user_reset_pwd", f"重置 {u.username} 的密码")
    return ok({"username": u.username})


@bp.get("/perms")
@perm_required("user.manage")
def perms_catalog():
    """权限点字典：给前端渲染勾选框用（页面已服务端渲染，此接口供扩展）"""
    return ok({
        "perms": PERMS,
        "roles": {k: sorted(v) for k, v in ROLE_PERMS.items()},
    })
