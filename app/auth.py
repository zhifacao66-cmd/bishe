"""认证与权限（会话登录 + RBAC）

设计要点：
1. 权限以「功能点」表达（如 ledger.view），角色只是功能点的集合。
   新增角色只改 ROLE_PERMS，不用去翻路由代码，避免权限散落各处。
2. 页面未登录 → 302 到 /login?next=...；API 未登录 → 401 JSON，前端可区分处理。
3. 越权一律 403，并写一条 deny 审计——保密场景下"谁试图看了不该看的"本身要留痕。
4. SQLite 旧库没有 username 列，启动时做一次轻量补列（不引入 Alembic）。
"""
from datetime import datetime
import time
from functools import wraps

from flask import (current_app, g, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models import AuditLog, User

# ---------------------------------------------------------------- 角色与权限点

ROLE_NAMES = {
    "resident": "居民",
    "community": "社区工作者",
    "property": "物业",
    "street": "街道",
    "admin": "系统管理员",
}

PERMS = {
    "fde.create": "提交 FDE 首录",
    "fde.view": "查看首录数据",
    "order.view": "查看工单",
    "order.handle": "接单 / 提交处理反馈",
    # 裁决权**故意不给物业**：物业是争议当事方，让它参与裁决等于自己当法官。
    # 这条不是权限配置的疏漏，是回避原则——答辩时可以直接拿它说明
    # "为什么小区里投诉物业永远没用"，以及系统怎么用代码把这条伦理约束钉住。
    "order.verdict": "裁决工单争议（驳回重办 / 维持办结）",
    "ledger.view": "查看台账",
    "report.view": "查看报表",
    "report.generate": "生成街道报表",
    "audit.view": "查看审计日志",
    "kb.view": "使用知识库",
    "kb.manage": "维护知识库文档",
    "task.view": "查看任务看板",
    "task.manage": "下发 / 关闭任务",
    # 公告：notice.view 三方管理者都有；发布权归社区（属地口径），
    # 导入权归街道（上级文件由街道这一层接进来）——两权分离，避免"谁能进谁就能发"
    "notice.view": "查看通知公告",
    "notice.manage": "发布 / 下架 / 置顶公告",
    "notice.import": "导入 / 投递上级单位文件",
    # 收文与办文再往下拆一层：
    # 签收 = 承认"我收到了、我要办"，办文 = 把上级要求落成本社区的执行通知。
    # 拆开的好处是社区内部可以分工——主任签收，经办人办文，责任各自留痕。
    "notice.receive": "签收上级来文",
    "notice.handle": "登记收文办理结果",
    "notice.localize": "生成本社区落地件",
    # 发文与周报：notice.* 管的是"通知公告"（居民端可见的那一面），
    # doc.* 管的是"公文"（有文号、有签发人、有密级的那一面）。
    # 两者共用 notice 表但视角完全不同：一份请示不需要居民看见，
    # 一份停水通知不需要文号。权限分开，才不会让"能发公告的人"顺手能发公文。
    "doc.view": "查看公文",
    "doc.manage": "拟稿 / 核稿 / 签发 / 用印 / 分发",
    "weekly.view": "查看工作周报",
    "weekly.manage": "生成 / 修改 / 签发周报",
    # 账号治理：只有系统管理员持有。它管的是"谁能用系统"，不是"谁能办业务"，
    # 所以没有下放给街道——街道监督的是业务，不是账号。
    "user.manage": "管理账号与权限",
    # 人员底册：街道下发的重点人员台账（残疾人 / 防溺水 / 留守）。
    # 只读一个权限点就够——底册由街道下发、系统导入，不该让人在页面里改，
    # 改了会与下发表对不上，下次导入又被覆盖，责任还说不清。
    "person.view": "查看人员底册",
}

# 权限点分组：只为管理页面好读，不参与任何校验。
# 平铺 18 个复选框没人看得懂，按"他到底要办哪类事"分堆才选得动。
PERM_GROUPS = [
    ("事项录入与档案", ["fde.create", "fde.view", "ledger.view"]),
    ("工单与任务", ["order.view", "order.handle", "order.verdict", "task.view", "task.manage"]),
    ("报表与监督", ["report.view", "report.generate", "audit.view"]),
    ("知识库与公告", ["kb.view", "kb.manage", "notice.view", "notice.manage",
                    "notice.import", "notice.receive", "notice.handle",
                    "notice.localize"]),
    ("公文与周报", ["doc.view", "doc.manage", "weekly.view", "weekly.manage"]),
    ("账号治理", ["user.manage"]),
]

# 角色 → 权限点集合（对应《总体设计》角色权限矩阵）
ROLE_PERMS = {
    # 居民：免注册、免登录，凭手机号 + 查询码办事。
    # 列在这里是为了让"四类使用者"在权限矩阵里有完整位置，
    # 也便于模板用同一套 can() 判断居民能看到什么。
    "resident": {"notice.view"},
    # 社区工作者：一句话录入、看自己社区的工单进度与台账，可汇总上报；公告发布权在社区
    # 收文签收与办文落地都归社区——街道只负责发，不替社区办
    "community": {
        "fde.create", "fde.view", "order.view", "ledger.view",
        # 裁决权归社区：居委会在居民与物业之间充当仲裁方，这是三方治理的核心动作
        "order.verdict",
        "report.view", "report.generate", "kb.view", "kb.manage",
        "task.view", "notice.view", "notice.manage",
        "notice.receive", "notice.handle", "notice.localize",
        # 公文与周报是社区的日常产出物：发文权、签发权都在社区
        "doc.view", "doc.manage", "weekly.view", "weekly.manage",
        # 底册：社区只看得到本社区的（ABAC 按 community 收窄，
        # 见 app/personbook.py:_scoped），街道能看全街道
        "person.view",
    },
    # 物业：只做工单，看不到 FDE 原文、人口台账（最小必要原则）
    # doc.view 给的是"能看到发给自己的函"，密级过滤在 app/api/docs.py:_visible
    "property": {"order.view", "order.handle", "task.view", "notice.view", "doc.view"},
    # 街道：只做监督，看汇总报表、超时工单和审计留痕，不下场办事；知识库只读
    # task.manage 是街道唯一的写权限——下发任务属于"管理动作"，不是"办事动作"
    # notice.import 让街道把上级文件接进系统，但是否对外发布由社区确认
    # 公文与周报只读：街道是收文方，看得到报上来的请示与周报，但不替社区起草
    "street": {"order.view", "report.view", "audit.view", "kb.view",
               "task.view", "task.manage", "notice.view", "notice.import",
               "doc.view", "weekly.view", "person.view"},
}

# 系统管理员：全部权限点。这一行必须是 set(PERMS) 而不是手写清单，
# 目的是"新增权限点自动归管理员"，避免改代码时漏配导致管理员反而办不了事。
ROLE_PERMS["admin"] = set(PERMS)

# 角色 → 公文来源层级。
#
# 为什么要有这张表：发文接口里原本写死 level="街道"。
# 一旦管理员建了个"区民政局"账号（role 仍是 street），系统记下的层级还是"街道"，
# 元数据就是错的——文件页脚、导出 Word 的元信息头、按层级统计全跟着错。
# 层级应当由**账号档案**决定，不该由代码常量决定。
#
# 只有 street 算上级：本系统服务的是"社区—物业—街道"三级治理，
# 区级及以上不是本系统的使用主体，它们的文件通过街道这一层接进来。
# 这条边界写在这里，比写在文档里更不容易被后来的人踩过。
ROLE_LEVEL = {
    "community": "社区",
    "property": "物业",
    "street": "街道",
}

# 需要社区签收的层级。空集合时任何人都能签收，等于收文流程失效，
# 所以判定统一从这里取，不再在各处硬编码 "街道"。
SUPERIOR_LEVELS = {"街道"}

# 管理员代发时的默认层级：管理员没有业务层级，替他按街道层记
DEFAULT_LEVEL = "街道"


def level_of(u) -> str:
    """取账号所属公文层级。未知角色（含管理员）回落 DEFAULT_LEVEL。"""
    if not u:
        return DEFAULT_LEVEL
    return ROLE_LEVEL.get(u.role, DEFAULT_LEVEL)


def is_superior(level: str) -> bool:
    return (level or "") in SUPERIOR_LEVELS


# 账号命名规则：字母开头，3~20 位，只含字母/数字/下划线。
# 放在这里是因为注册页（app/admin.py）和管理员建号接口（app/api/admin.py）
# 两处都要用。各写一份迟早漂移——注册能过、建号被拒，用户只会觉得系统有 bug。
USERNAME_RE = r"^[A-Za-z][A-Za-z0-9_]{2,19}$"


def valid_username(s: str) -> bool:
    import re
    return bool(re.match(USERNAME_RE, s or ""))


# 登录后的默认落地页
ROLE_HOME = {
    "resident": "portal.home",
    "community": "views.index",
    "property": "views.orders",
    "street": "views.tasks",
    "admin": "admin.users",
}

# 演示账号（首次启动自动写入，密码统一 123456；生产必须改）
# admin 的密码单独设，因为它能改所有人的权限——演示库密码公开，
# 用同一个 123456 等于任何人拿到链接就拿到了系统管理权。
DEMO_USERS = [
    {"username": "admin", "name": "系统管理员", "role": "admin",
     "org": "平台运维", "password": "admin123"},
    {"username": "wang", "name": "王社工", "role": "community",
     "org": "幸福里社区", "password": "123456"},
    {"username": "wuye", "name": "李经理", "role": "property",
     "org": "幸福里物业", "password": "123456"},
    # 同一个物业公司有两个维修工：一个单位只挂一个账号，"转派"就永远演示不了，
    # 而且现实中交接班、分头干活是常态——处理人字段要有第二个人才说得通。
    {"username": "wuye2", "name": "赵师傅", "role": "property",
     "org": "幸福里物业", "password": "123456"},
    {"username": "jiedao", "name": "张科长", "role": "street",
     "org": "幸福里街道办事处", "password": "123456"},
]


# ---------------------------------------------------------------- 当前用户

def current_user() -> User | None:
    if "user" not in g:
        uid = session.get("uid")
        g.user = db.session.get(User, uid) if uid else None
    return g.user


def user_perms(u: User | None) -> set[str]:
    """算出这个账号**实际**拥有的权限点（唯一入口，页面与接口都走这里）

    权限来源的优先级见 models.User 的注释。集中在一处算的好处是：
    以后加"部门级""临时授权"只需要改这一个函数，不会出现
    "页面按 A 口径显示、接口按 B 口径校验"的错位。
    """
    if not u:
        return set()
    # 管理员恒定全量：即使 perms_json 被误写成 []，也不允许把自己锁在门外
    if u.role == "admin":
        return set(PERMS)
    # 待审核 / 已停用：有登录态但没有任何业务权限
    if u.status and u.status != "active":
        return set()
    if u.perms_json is not None:
        import json
        try:
            picked = json.loads(u.perms_json or "[]")
        except (ValueError, TypeError):
            picked = []
        # 过滤掉已下线的权限点，避免老数据带来"幽灵权限"
        return {p for p in picked if p in PERMS}
    return set(ROLE_PERMS.get(u.role, set()))


def has_perm(perm: str) -> bool:
    return perm in user_perms(current_user())


def role_name(role: str) -> str:
    return ROLE_NAMES.get(role, role or "")


STATUS_NAMES = {"pending": "待审核", "active": "正常", "disabled": "已停用"}


def status_name(s: str) -> str:
    return STATUS_NAMES.get(s or "active", s or "正常")


# ---------------------------------------------------------------- 守卫

def _deny_log(user: User | None, perm: str, path: str):
    """越权尝试写审计：actor 用可识别身份，不记录被protected数据本身"""
    try:
        db.session.add(AuditLog(
            actor=(user.name if user else "匿名"),
            agent="auth",
            action="deny",
            input_digest=f"{role_name(user.role) if user else '未登录'} 请求 {perm} @ {path}"[:250],
            output_digest="已拒绝：权限不足",
        ))
        db.session.commit()
    except Exception:  # 审计失败不能影响主流程返回
        db.session.rollback()


def _is_api() -> bool:
    return request.path.startswith("/api/")


def _login_redirect():
    nxt = request.full_path if request.method == "GET" else ""
    if nxt.endswith("?"):
        nxt = nxt[:-1]
    return redirect(url_for("views.login", next=nxt) if nxt else url_for("views.login"))


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u:
            if _is_api():
                return jsonify({"code": 401, "msg": "未登录或登录已过期", "data": None}), 401
            return _login_redirect()
        return view(*args, **kwargs)
    return wrapper


def perm_required(perm: str):
    """要求登录且具备指定功能点"""
    def deco(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            u = current_user()
            if not u:
                if _is_api():
                    return jsonify({"code": 401, "msg": "未登录或登录已过期", "data": None}), 401
                return _login_redirect()
            if perm not in user_perms(u):
                _deny_log(u, perm, request.path)
                if _is_api():
                    return jsonify({
                        "code": 403,
                        "msg": f"当前角色（{role_name(u.role)}）无「{PERMS.get(perm, perm)}」权限",
                        "data": None,
                    }), 403
                return render_template("403.html", perm=PERMS.get(perm, perm)), 403
            return view(*args, **kwargs)
        return wrapper
    return deco


# ---------------------------------------------------------------- 登录 / 登出

# 登录失败滑动窗口，**双维度**计数。
#
# 只有账号维度是可靠的主维度。上线实测发现：托管环境在反向代理后面，
# 每个请求带来的 X-Forwarded-For 都不一样（代理出口 IP 池），
# 按「IP|账号」计数永远都是"第 1 次"，限流形同虚设；
# 而且 XFF 本身可被客户端伪造，拿它做安全决策从根上就不成立。
#
# 所以：账号维度为主（防猜密码），IP 维度为辅且阈值放宽（防单 IP 扫大量账号）。
# 代价是攻击者可以故意失败几次把某个账号锁住（DoS），由 5 分钟的短锁定期缓解；
# 更完整的做法是加验证码，属于生产增强项。
_LOGIN_FAIL: dict[str, list[float]] = {}


class LoginThrottled(Exception):
    """失败次数超限，拒绝本次登录尝试"""


class AccountDisabled(Exception):
    """账号已被管理员停用：密码校验通过后才抛，避免泄露"这个账号存不存在"""


def _client_ip() -> str:
    """仅用于留痕与辅助限流，**不可**作为唯一安全依据（XFF 可伪造）"""
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "unknown"


def _fails(key: str, window: int) -> list[float]:
    now = time.time()
    arr = [t for t in _LOGIN_FAIL.get(key, []) if now - t < window]
    _LOGIN_FAIL[key] = arr
    return arr


def _throttle_keys(username: str) -> list[tuple[str, int]]:
    acct_limit = current_app.config.get("LOGIN_MAX_FAIL", 5)
    ip_limit = current_app.config.get("LOGIN_IP_MAX_FAIL", 30)
    return [
        (f"acct|{(username or '').lower()}", acct_limit),
        (f"ip|{_client_ip()}", ip_limit),
    ]


def login(username: str, password: str) -> User | None:
    if not username or not password:
        return None

    window = current_app.config.get("LOGIN_FAIL_WINDOW", 300)
    keys = _throttle_keys(username)
    for k, lim in keys:
        if len(_fails(k, window)) >= lim:
            raise LoginThrottled(
                f"登录失败次数过多，请约 {max(1, window // 60)} 分钟后再试")

    u = User.query.filter_by(username=username).first()
    if not u or not u.password_hash or not check_password_hash(u.password_hash, password):
        now = time.time()
        for k, _ in keys:
            _fails(k, window).append(now)
        acct_key = keys[0][0]
        # 失败尝试必须留痕：单条看是噪音，连着看就是攻击特征
        try:
            db.session.add(AuditLog(actor=username[:32], agent="auth", action="login_fail",
                                    output_digest=f"登录失败（账号维度第 "
                                                  f"{len(_LOGIN_FAIL[acct_key])} 次）"
                                                  f" 来源 {_client_ip()}"))
            db.session.commit()
        except Exception:
            db.session.rollback()
        return None

    # 成功只清账号维度；IP 维度不清，否则攻击者拿一个正确密码就能重置自己的 IP 计数
    _LOGIN_FAIL.pop(keys[0][0], None)

    # 停用判断放在密码校验之后：只有本来就知道密码的人才会看到"账号已停用"，
    # 旁人试探始终只得到"用户名或密码错误"，不构成账号枚举。
    if (u.status or "active") == "disabled":
        try:
            db.session.add(AuditLog(actor=u.name, agent="auth", action="login_blocked",
                                    output_digest=f"停用账号尝试登录被拒（{u.org}）"))
            db.session.commit()
        except Exception:
            db.session.rollback()
        raise AccountDisabled("账号已被停用，请联系系统管理员")

    session.clear()
    session["uid"] = u.id
    session["role"] = u.role
    session.permanent = True
    u.last_login_at = datetime.now()
    db.session.add(AuditLog(actor=u.name, agent="auth", action="login",
                            output_digest=f"{role_name(u.role)} 登录成功（{u.org}）"
                                          f"{'·待审核' if (u.status or 'active') == 'pending' else ''}"))
    db.session.commit()
    return u


def logout():
    u = current_user()
    if u:
        db.session.add(AuditLog(actor=u.name, agent="auth", action="logout",
                                output_digest=f"{role_name(u.role)} 退出登录"))
        db.session.commit()
    session.clear()


# ---------------------------------------------------------------- 启动初始化

def _migrate_user_table():
    """老 SQLite 库补列（ADD COLUMN 幂等），并把既有账号回填为正常状态

    注意 status 的回填：新增的 status 列在老账号上是 NULL，
    如果代码里用 `status != 'active'` 判空，所有老账号会瞬间变成"无权限"。
    所以这里既补列也补值，同一条升级语句做完，不留中间态。
    """
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    if "user" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("user")}
    added = []
    if "username" not in cols:
        db.session.execute(text("ALTER TABLE user ADD COLUMN username VARCHAR(32)"))
        added.append("username")
    if "last_login_at" not in cols:
        db.session.execute(text("ALTER TABLE user ADD COLUMN last_login_at DATETIME"))
        added.append("last_login_at")
    if "created_at" not in cols:
        db.session.execute(text("ALTER TABLE user ADD COLUMN created_at DATETIME"))
        added.append("created_at")
    if "status" not in cols:
        db.session.execute(text("ALTER TABLE user ADD COLUMN status VARCHAR(16) DEFAULT 'active'"))
        added.append("status")
    if "perms_json" not in cols:
        # NULL 而非 '[]'：老账号应继续跟随角色默认权限，而不是被清成零权限
        db.session.execute(text("ALTER TABLE user ADD COLUMN perms_json TEXT"))
        added.append("perms_json")
    if "phone" not in cols:
        db.session.execute(text("ALTER TABLE user ADD COLUMN phone VARCHAR(16) DEFAULT ''"))
        added.append("phone")
    if "created_by" not in cols:
        db.session.execute(text("ALTER TABLE user ADD COLUMN created_by VARCHAR(32) DEFAULT ''"))
        added.append("created_by")
    if "review_note" not in cols:
        db.session.execute(text("ALTER TABLE user ADD COLUMN review_note VARCHAR(128) DEFAULT ''"))
        added.append("review_note")
    if "status" in added:
        db.session.execute(text("UPDATE user SET status='active' WHERE status IS NULL"))
    db.session.commit()


def enforce_account_status(app):
    """待审核 / 已停用账号的全局拦截

    has_perm 已经把这类账号的权限清空，但仅靠它用户会到处撞 403，
    看起来像系统坏了。这里直接把人引到"审核中"页面，说明清楚在等什么。
    """
    from flask import request

    # 允许通行的端点：看自己的状态、退出、静态资源、注册
    # adminapi.me 必须放行——待审核页要靠它轮询"我通过了没有"，
    # 拦掉这个接口，审核通过后本人也永远不会知道。
    ALLOW = {"admin.pending", "admin.register", "adminapi.me",
             "views.logout", "static"}

    @app.before_request
    def _guard():
        if request.endpoint in ALLOW or request.endpoint is None:
            return None
        u = current_user()
        if not u:
            return None
        st = u.status or "active"
        if st == "disabled":
            from flask import session as _s
            _s.clear()
            if request.path.startswith("/api/"):
                return jsonify({"code": 403, "msg": "账号已被停用", "data": None}), 403
            return redirect(url_for("views.login"))
        if st == "pending":
            if request.path.startswith("/api/"):
                return jsonify({"code": 403, "msg": "账号待管理员审核", "data": None}), 403
            return redirect(url_for("admin.pending"))
        return None


def _migrate_order_table():
    """老库补 work_order 新增列（ADD COLUMN 幂等，用于升级已有演示库）

    依次是：
    - `sla_level`：SLA 分级落地后加的
    - `assignee` / `assignee_id`：处理人（单位 ≠ 人，追责要落到人）
    - `assigned_at` / `done_at`：接单/办结时刻，处理时长靠这两个算

    `work_order_log` 是新表，由启动时的 `db.create_all()` 建，不需要在这里补。
    """
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    if "work_order" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("work_order")}
    for col, ddl in (
        ("sla_level", "VARCHAR(8) DEFAULT ''"),
        ("assignee", "VARCHAR(32)"),
        ("assignee_id", "INTEGER"),
        ("assigned_at", "DATETIME"),
        ("done_at", "DATETIME"),
        # 争议与裁决：三方治理里"办完"和"办对"是两件事，这八个字段就是它们的差
        ("dispute_type", "VARCHAR(16) DEFAULT ''"),
        ("verdict", "VARCHAR(16) DEFAULT ''"),
        ("verdict_basis", "VARCHAR(256) DEFAULT ''"),
        ("verdict_by", "VARCHAR(32) DEFAULT ''"),
        ("verdict_at", "DATETIME"),
        ("confirm_status", "VARCHAR(16) DEFAULT ''"),
        ("confirm_at", "DATETIME"),
        ("confirm_note", "VARCHAR(512) DEFAULT ''"),
    ):
        if col not in cols:
            db.session.execute(text(f"ALTER TABLE work_order ADD COLUMN {col} {ddl}"))
    db.session.commit()


def _migrate_ticket_table():
    """老库给 resident_ticket 补附件列

    附件存 JSON 数组而不是单列存文件名：一张图说不清"楼道堆物",
    居民会连拍三张，而"第几张是哪面墙"的意义只有居民自己知道。
    """
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    if "resident_ticket" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("resident_ticket")}
    if "attachments" not in cols:
        db.session.execute(text("ALTER TABLE resident_ticket ADD COLUMN attachments TEXT DEFAULT ''"))
    db.session.commit()


def _migrate_task_table():
    """老 SQLite 库补 task 表新增列（ADD COLUMN 幂等，用于升级已有演示库）"""
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    if "task" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("task")}
    if "assignee_org" not in cols:
        db.session.execute(text("ALTER TABLE task ADD COLUMN assignee_org VARCHAR(64)"))
    if "detail" not in cols:
        db.session.execute(text("ALTER TABLE task ADD COLUMN detail TEXT"))
    if "creator" not in cols:
        db.session.execute(text("ALTER TABLE task ADD COLUMN creator VARCHAR(32)"))
    if "closed_at" not in cols:
        db.session.execute(text("ALTER TABLE task ADD COLUMN closed_at DATETIME"))
    db.session.commit()


def _migrate_report_table():
    """老库补 report.version / rollback_from（一期一份 + 版本回滚）

    老库里的重复报表不动数据，只把每条的 version 补成 1；
    重复记录由 scripts/_dedupe_reports.py 一次性清理（要留痕，不放在启动路径上）。

    `rollback_from` 不能给默认值 0：0 不是合法版本号，写成 0 会让前端把
    "没回滚过"画成"回滚自第 0 版"。留 NULL 才是"从来没回滚过"。
    """
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    if "report" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("report")}
    if "version" not in cols:
        db.session.execute(text("ALTER TABLE report ADD COLUMN version INTEGER DEFAULT 1"))
        db.session.execute(text("UPDATE report SET version=1 WHERE version IS NULL"))
        db.session.commit()
    if "rollback_from" not in cols:
        db.session.execute(text("ALTER TABLE report ADD COLUMN rollback_from INTEGER"))
        db.session.commit()
    # 一期一份要落到数据库约束上，只在应用层判"有没有"挡不住并发两连击。
    # 老库里可能还留着重复行，建索引会失败——那就先跳过，提示去跑清理脚本。
    try:
        db.session.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_report_period_line ON report(period, line)"))
        db.session.commit()
    except Exception as e:                              # noqa: BLE001
        db.session.rollback()
        import logging
        logging.getLogger(__name__).warning(
            "report(period,line) 唯一索引未建立（可能仍有重复行，先跑 "
            "scripts/_dedupe_reports.py --apply）：%s", e)


def _migrate_audit_table():
    """审计日志补 `(created_at, id)` 复合索引

    审计改成游标分页后，每一页都是"按时间倒序取 limit 条"。
    没有索引时要全表排序后再丢前面的行，翻到深处越来越慢；
    有索引就是一次有序索引扫描 + seek，翻到第几页代价一样。

    索引列顺序不能反：`created_at` 在前才能直接满足 ORDER BY，
    `id` 在后用于同一秒内的边界定位。
    """
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    if "audit_log" not in insp.get_table_names():
        return
    try:
        db.session.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_audit_created_id ON audit_log(created_at, id)"))
        db.session.commit()
    except Exception as e:                                  # noqa: BLE001
        db.session.rollback()
        import logging
        logging.getLogger(__name__).warning("audit_log 索引未建立：%s", e)


def _migrate_notice_table():
    """老 SQLite 库给 notice 表补收文流转字段

    kind 的回填不能都用默认值：老记录里既有街道下发的来文，也有社区自己发的通知，
    一律标成 superior 会让"收文待办"里突然多出一堆假待办。
    所以按 level 认：街道下发的算上级来文，其余算原创。
    """
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    if "notice" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("notice")}
    added = []
    for name, ddl in (
        ("kind", "VARCHAR(16) DEFAULT 'superior'"),
        ("parent_id", "INTEGER"),
        ("receiver_org", "VARCHAR(64) DEFAULT ''"),
        ("recv_status", "VARCHAR(16) DEFAULT ''"),
        ("received_by", "VARCHAR(32) DEFAULT ''"),
        ("received_at", "DATETIME"),
        ("deadline", "DATE"),
        ("pinned_at", "DATETIME"),
        # ---- P0 公文属性
        ("direction", "VARCHAR(8) DEFAULT 'in'"),
        ("doc_type", "VARCHAR(16) DEFAULT '通知'"),
        ("doc_status", "VARCHAR(16) DEFAULT ''"),
        ("secret_level", "VARCHAR(16) DEFAULT ''"),
        ("secret_until", "DATE"),
        ("urgency", "VARCHAR(8) DEFAULT '平件'"),
        ("to_org", "VARCHAR(128) DEFAULT ''"),
        ("cc_org", "VARCHAR(128) DEFAULT ''"),
        ("signer", "VARCHAR(32) DEFAULT ''"),
        ("handler", "VARCHAR(32) DEFAULT ''"),
        ("handled_at", "DATETIME"),
        ("handle_result", "TEXT DEFAULT ''"),
        ("attachments", "TEXT DEFAULT ''"),
        ("seal_by", "VARCHAR(32) DEFAULT ''"),
        ("sealed_at", "DATETIME"),
        ("archived_at", "DATETIME"),
        ("doc_year", "INTEGER"),
        ("doc_seq", "INTEGER"),
    ):
        if name not in cols:
            db.session.execute(text(f"ALTER TABLE notice ADD COLUMN {name} {ddl}"))
            added.append(name)
    if "pinned_at" in added:
        # 刚加列时补一次（下面还有一条幂等回填，两处都要）
        db.session.execute(text(
            "UPDATE notice SET pinned_at=COALESCE(published_at, created_at) "
            "WHERE pinned=1 AND pinned_at IS NULL"))
    # 幂等回填：不能只在"刚加列"那一次生效。
    # 早期 `_apply_pin` 只翻 pinned 布尔值、不写 pinned_at，那批行是在列加好
    # **之后**产生的，所以上面的 `if "pinned_at" in added` 永远不会再命中它们。
    # 后果是排序里 NULL 沉底——最老的一条置顶反而排在新置顶件后面，
    # 而"置顶"的意义就是"排最前"。每次启动补一条 UPDATE，代价可以忽略。
    filled = db.session.execute(text(
        "UPDATE notice SET pinned_at=COALESCE(published_at, created_at) "
        "WHERE pinned=1 AND pinned_at IS NULL")).rowcount
    if filled:
        print(f"[db] 回填 {filled} 条置顶公告的 pinned_at", flush=True)
    if "kind" in added:
        db.session.execute(text("UPDATE notice SET kind='superior' WHERE level='街道'"))
        db.session.execute(text("UPDATE notice SET kind='original' WHERE level<>'街道' OR level IS NULL"))
    if "recv_status" in added:
        db.session.execute(text("UPDATE notice SET recv_status='' WHERE recv_status IS NULL"))
    if "direction" in added:
        # 老记录全是"收文或原创公告"，没有发文。原创公告标 inner（内部留存），
        # 不能一律给 in —— 否则"收文统计"里会凭空多出一堆本社区自己发的通知。
        db.session.execute(text("UPDATE notice SET direction='inner' WHERE kind='original'"))
        db.session.execute(text("UPDATE notice SET direction='in' "
                                "WHERE kind IS NULL OR kind<>'original'"))
    db.session.commit()


def _migrate_ledger_table():
    """老库给 ledger 补 reused / search_text 两列，并各自回填

    reused：回填不能直接把老记录当"未复用"——历史档案复用的次数是减负效果的
    核心指标，清零等于把这个数抹掉。老记录里 JSON 带 "reused_archive": true
    的才算复用，其余（含早期没有这个键的工单闭环记录）都算新建。

    search_text：新增的检索列。不回填的话老台账一律搜不到——而且不报错，
    界面上就是"搜了没结果"。这里按 content 的叶子值重建（规则与
    app/search.py:content_text 一致），一次 UPDATE 批量补完。
    """
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    if "ledger" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("ledger")}
    if "reused" not in cols:
        db.session.execute(text("ALTER TABLE ledger ADD COLUMN reused BOOLEAN DEFAULT 0"))
        db.session.execute(text("UPDATE ledger SET reused=0 WHERE reused IS NULL"))
        # JSON 是按 json.dumps 默认分隔符写的（': ' 后有空格），用 LIKE 认这一段；
        # 只做一次性回填，之后一律读写真实列。
        db.session.execute(text(
            "UPDATE ledger SET reused=1 WHERE content_json LIKE '%\"reused_archive\": true%'"))
        db.session.commit()
    if "search_text" not in cols:
        db.session.execute(text("ALTER TABLE ledger ADD COLUMN search_text TEXT"))
        _backfill_ledger_search_text()
        db.session.commit()


def _backfill_ledger_search_text():
    """按 content_json 重建老台账的 search_text（幂等：只补 NULL 的）。"""
    from sqlalchemy import text
    from app.search import content_text
    rows = db.session.execute(text(
        "SELECT id, content_json FROM ledger WHERE search_text IS NULL")).fetchall()
    if not rows:
        return
    db.session.execute(
        text("UPDATE ledger SET search_text=:t WHERE id=:i"),
        [{"t": content_text(cj or ""), "i": i} for i, cj in rows])


def seed_users():
    """幂等写入演示账号：已存在则跳过，不覆盖用户改过的密码"""
    created = []
    for d in DEMO_USERS:
        if User.query.filter_by(username=d["username"]).first():
            continue
        db.session.add(User(
            username=d["username"], name=d["name"], role=d["role"], org=d["org"],
            status="active", perms_json=None, created_by="系统初始化",
            password_hash=generate_password_hash(d["password"]),
        ))
        created.append(d["username"])
    if created:
        db.session.commit()
    return created


def seed_tasks():
    """幂等写入演示任务：表空时才写，避免覆盖演示中被操作过的数据。
    特意留一条**已超时**任务，让督办Agent 一登录就有告警可演示。
    """
    from datetime import timedelta
    from app.models import Task
    if Task.query.count() > 0:
        return 0
    now = datetime.now()
    demo = [
        {"title": "10 月人口变动台账补录", "assignee_role": "community",
         "assignee_org": "幸福里社区", "detail": "按综治口径补齐 9 月以来新入住、迁出人员，10 月 25 日前完成",
         "deadline": now - timedelta(hours=6), "status": "open"},          # 已超时
        {"title": "高层住宅消防通道专项排查", "assignee_role": "community",
         "assignee_org": "幸福里社区", "detail": "逐栋排查电动车进楼、杂物堆放，形成排查台账",
         "deadline": now + timedelta(days=5), "status": "open"},
        {"title": "3 栋电梯年检材料准备", "assignee_role": "property",
         "assignee_org": "幸福里物业", "detail": "整理维保记录与检验申请，配合街道联合检查",
         "deadline": now + timedelta(days=2), "status": "open"},
        {"title": "9 月独居老人走访全覆盖", "assignee_role": "community",
         "assignee_org": "幸福里社区", "detail": "已完成 32 户走访，记录已归档",
         "deadline": now - timedelta(days=10), "status": "done"},
    ]
    for d in demo:
        db.session.add(Task(creator="张科长", closed_at=None if d["status"] == "open" else now,
                            **d))
    db.session.commit()
    return len(demo)


def init_auth(app):
    """在应用工厂里调用：注入模板变量 + 初始化账号"""
    @app.context_processor
    def _inject_auth():
        u = current_user()
        # 页面水印：内部账号一律铺一层平铺水印（姓名·单位·日期）。
        # 这是等保 2.0 里"运维与操作可追溯"的常规做法，也是截屏外流后
        # 唯一还能追到人的线索——比在页脚写一行"版权所有"有用得多。
        # 居民端不加：居民本来就在自己手机上，水印上也没有可追责的主体。
        wm = ""
        if u:
            from app.watermark import overlay_css, text as wm_text
            try:
                wm = overlay_css(wm_text(u, "内部查阅"))
            except Exception:            # noqa: BLE001
                wm = ""
        return {
            "current_user": u,
            "can": has_perm,
            "wm_css": wm,
            "ROLE_NAMES": ROLE_NAMES,
            "ROLE_HOME": ROLE_HOME,
            "role_name": role_name,
            "status_name": status_name,
            "user_perms": user_perms,
        }

    with app.app_context():
        db.create_all()
        _migrate_user_table()
        _migrate_order_table()
        _migrate_ticket_table()
        _migrate_task_table()
        _migrate_notice_table()
        _migrate_ledger_table()
        _migrate_report_table()
        _migrate_audit_table()
        seed_users()
        seed_tasks()

    enforce_account_status(app)
