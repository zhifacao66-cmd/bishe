import os
from datetime import timedelta

from flask import Flask
from config import config_map
from app.extensions import db


# 这些字样说明"文件本身坏了/打不开"，而不是业务写错了。
# 只在这种时候才允许拿种子库覆盖——普通锁冲突、超时绝不碰数据。
_CORRUPT_SIGNS = ("malformed", "not a database", "encrypted", "disk i/o error",
                  "unable to open database file")


def _sqlite_path(app) -> str:
    uri = app.config.get("SQLALCHEMY_DATABASE_URI") or ""
    if not uri.startswith("sqlite"):
        return ""
    path = uri.split("sqlite:///", 1)[-1]
    return "" if path in ("", ":memory:") else path


def _boot_log(app, msg: str) -> None:
    """启动期错误必须留下**一行可读**的记录。

    沙箱里只看得到 stderr 的尾部一小段，完整 traceback 会被截在中间，
    真正那句 `OperationalError: xxx` 经常正好被切掉。所以把一句话结论
    打到 stdout 首行、同时落 data/boot_error.log，远端也能一眼看到。
    """
    import time as _t

    print(f"[db][FATAL] {msg}", flush=True)
    try:
        folder = os.path.dirname(_sqlite_path(app)) or "data"
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "boot_error.log"), "a",
                  encoding="utf-8") as fp:
            fp.write(f"{_t.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:                                # noqa: BLE001
        pass


def _heal_sqlite(app, exc: Exception) -> bool:
    """库文件坏了就用随包种子库换掉它。返回是否真的换过。"""
    import shutil

    path = _sqlite_path(app)
    if not path:
        return False
    low = f"{type(exc).__name__}: {exc}".lower()
    if not any(s in low for s in _CORRUPT_SIGNS):
        return False
    folder = os.path.dirname(path) or "."
    seed = os.path.join(folder, "community.db.seed")
    if not os.path.exists(seed):
        print("[db] 需要自愈但找不到 data/community.db.seed，跳过", flush=True)
        return False
    try:
        os.makedirs(folder, exist_ok=True)
        for suf in ("-wal", "-shm"):                 # 残留 WAL 会和主库对不上
            p = path + suf
            if os.path.exists(p):
                os.remove(p)
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak")        # 坏库也留一份，可能还能捞数据
        shutil.copy2(seed, path)
        print(f"[db] 库无法打开，已用种子库覆盖：{path}", flush=True)
        try:
            with open(os.path.join(folder, "boot_error.log"), "a",
                      encoding="utf-8") as fp:
                fp.write("已用种子库覆盖并自愈成功（原库备份为 community.db.bak）\n")
        except Exception:                            # noqa: BLE001
            pass
        return True
    except Exception as e:                           # noqa: BLE001
        _boot_log(app, f"自愈失败：{type(e).__name__}: {e}")
        return False


def _boot_database(app) -> bool:
    """建表 / 补列 / 种子账号：失败可以降级，但**不许把进程带走**。

    托管环境里"进程没起来"和"进程起来了但库坏了"是两种完全不同的处境：
    前者分享链接直接打不开、平台只报一句"没在端口上响应"；
    后者还能打开页面看到健康检查说 db.ok=false。后者能救，前者只能改代码重发。
    所以这里把异常吞下来换成降级，自救（换库）再给一次机会。
    """
    from app.auth import init_auth
    from app.db_tuning import tune_sqlite

    def _stage():
        tune_sqlite(app)      # SQLite 开 WAL + busy_timeout（MySQL 自动跳过）
        init_auth(app)        # 上下文注入 + 建表补列 + 演示账号（幂等）

    try:
        _stage()
        return True
    except Exception as e:                           # noqa: BLE001
        _boot_log(app, f"数据库初始化失败：{type(e).__name__}: {e}")
        if not _heal_sqlite(app, e):
            return False
    try:
        db.engine.dispose()                          # 丢掉指向旧文件 inode 的连接
    except Exception:                                # noqa: BLE001
        pass
    try:
        _stage()
        print("[db] 自愈后重新初始化成功", flush=True)
        return True
    except Exception as e:                           # noqa: BLE001
        _boot_log(app, f"自愈后仍失败（已降级启动）：{type(e).__name__}: {e}")
        return False


def _maybe_reset_sqlite(app):
    """按标记文件重置 SQLite 库（仅发布环境需要）。

    为什么要有：托管环境的 data/ 是持久卷，部署只换代码不换库。
    一旦这个库在写入途中被强杀成损坏（`database disk image is malformed`），
    后续每次写都 500，而本地既改不动也连不上那个文件——没有这个入口就只能干瞪眼。

    设计成"显式标记 + 幂等"：
    - 只有 data/.reset_db 存在时才动手，平时一行都不执行；
    - 标记里写一个 id，执行后把 id 写进 .reset_db.done，重复部署不会再重置一次
      （否则每次发布都会把演示数据冲掉，越修越糟）。
    """
    import os
    import shutil

    uri = app.config.get("SQLALCHEMY_DATABASE_URI") or ""
    if not uri.startswith("sqlite"):
        return
    path = uri.split("sqlite:///", 1)[-1]
    folder = os.path.dirname(path)
    flag = os.path.join(folder, ".reset_db")
    done = os.path.join(folder, ".reset_db.done")
    if not os.path.exists(flag):
        return
    try:
        want = open(flag, encoding="utf-8").read().strip()
        if os.path.exists(done) and open(done, encoding="utf-8").read().strip() == want:
            return                                  # 这一版标记已经执行过
        seed = os.path.join(folder, "community.db.seed")
        if not os.path.exists(seed):
            print("[db] 发现重置标记，但缺少 data/community.db.seed，跳过", flush=True)
            return
        for suf in ("-wal", "-shm"):                # 残留 WAL 会和主库对不上
            p = path + suf
            if os.path.exists(p):
                os.remove(p)
        if os.path.exists(path):
            shutil.copy2(path, path + ".bak")       # 先留一份，损坏库也可能还能捞数据
        shutil.copy2(seed, path)
        with open(done, "w", encoding="utf-8") as fp:
            fp.write(want)
        print(f"[db] 已按标记重置数据库：{path}", flush=True)
    except Exception as e:                          # noqa: BLE001
        print(f"[db] 重置失败（继续用原库）：{type(e).__name__}: {e}", flush=True)


def create_app(env="dev"):
    app = Flask(__name__)
    app.config.from_object(config_map[env])
    app.permanent_session_lifetime = timedelta(hours=8)
    # 不刷新会话 cookie：默认每个响应都重发一次 Set-Cookie，除了让每个页面
    # 都多出几十字节的响应头，还会让浏览器对"带 Set-Cookie 的响应"缓存更保守，
    # 站内预取的 HTML 就复用不上。这里只在会话内容真变了（登录/退出）时才下发。
    app.config["SESSION_REFRESH_EACH_REQUEST"] = False

    _maybe_reset_sqlite(app)

    # 会话密钥：显式配置优先；未配置时开发用固定默认值（重启不掉登录），
    # 生产现场随机生成（宁可重启失效，也不能用公开密钥让人伪造任意角色）。
    if not app.config.get("SECRET_KEY"):
        import os as _os
        from config import resolve_secret_key
        app.config["SECRET_KEY"] = (
            _os.environ.get("SECRET_KEY")
            or resolve_secret_key(app.config.get("DEBUG", False))
        )

    # 并发相关引擎参数必须在 db.init_app 之前注入
    from app.db_tuning import engine_options, tune_sqlite
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = engine_options(app.config)

    db.init_app(app)

    # 数据库初始化：**不许把进程带走**。
    #
    # 踩过的坑：发布沙箱的 data/ 是持久卷，卷上的 SQLite 一旦被强杀成
    # `database disk image is malformed`，tune_sqlite() 里那次 engine.connect()
    # 就是第一个撞上去的地方——它抛异常 → create_app 抛异常 → 进程起不来 →
    # 平台报"服务没有在指定端口响应" → 分享链接直接打不开。
    # 最难受的是这形成了死锁：修库的入口（/api/health/db-check、重置标记）
    # 全在应用里，应用起不来就一个都用不上，只能靠改代码绕。
    #
    # 所以顺序改成：先自愈（有 seed 就换库），愈合不成就**降级启动**——
    # 页面能打开、健康检查能看、日志有据可查，业务接口 500 也比整体失联强。
    _boot_database(app)

    # 模型并发闸门按配置初始化（观测接口一开始就能看到 limit）
    from app.llm.gate import gate
    gate.configure(app.config["LLM_MAX_CONCURRENCY"], app.config["LLM_QUEUE_TIMEOUT"])

    # 传输层：gzip + 静态资源长缓存 + 页面预取缓存头（详见 app/fast.py 头注释）
    from app.fast import install as install_fast
    install_fast(app)

    # 注册页面视图
    from app.views import bp as views_bp
    app.register_blueprint(views_bp)

    # 账号治理：自助注册 / 待审核 / 用户与权限管理
    from app.admin import bp as admin_bp
    app.register_blueprint(admin_bp)

    # 居民端：独立布局的公开入口（免登录、手机优先）
    from app.portal import bp as portal_bp
    app.register_blueprint(portal_bp)

    # 注册 API 蓝图
    from app.api.fde import bp as fde_bp
    from app.api.tasks import bp as tasks_bp
    from app.api.orders import bp as orders_bp
    from app.api.ledgers import bp as ledgers_bp
    from app.api.reports import bp as reports_bp
    from app.api.supervision import bp as supervision_bp
    from app.api.audit import bp as audit_bp
    from app.api.kb import bp as kb_bp
    from app.api.health import bp as health_bp
    from app.api.portal import bp as portal_api_bp
    from app.api.notices import bp as notices_bp
    from app.api.admin import bp as admin_api_bp
    from app.api.docs import bp as docs_bp
    from app.api.evidence import bp as evidence_bp
    from app.api.weekly import bp as weekly_bp
    from app.api.policy import bp as policy_bp
    from app.api.insight import bp as insight_bp
    from app.api.collab import bp as collab_bp
    app.register_blueprint(fde_bp, url_prefix="/api/fde")
    app.register_blueprint(tasks_bp, url_prefix="/api/tasks")
    app.register_blueprint(orders_bp, url_prefix="/api/orders")
    app.register_blueprint(ledgers_bp, url_prefix="/api/ledgers")
    app.register_blueprint(reports_bp, url_prefix="/api/reports")
    app.register_blueprint(supervision_bp, url_prefix="/api/supervision")
    app.register_blueprint(audit_bp, url_prefix="/api/audit")
    app.register_blueprint(kb_bp, url_prefix="/api/kb")
    app.register_blueprint(health_bp, url_prefix="/api/health")
    app.register_blueprint(portal_api_bp, url_prefix="/api/portal")
    app.register_blueprint(notices_bp, url_prefix="/api/notices")
    app.register_blueprint(admin_api_bp, url_prefix="/api/admin")
    # 公文（发文侧）与周报：与 /api/notices 共用 notice 表，但状态机与权限独立
    app.register_blueprint(docs_bp, url_prefix="/api/docs")
    # 存证链：与工单/诉求同库同事务，但校验与锚定是独立的监督动作
    app.register_blueprint(evidence_bp, url_prefix="/api/evidence")
    app.register_blueprint(weekly_bp, url_prefix="/api/weekly")
    # ABAC 策略：规则自述 + 现场试算。只读，不碰业务数据
    app.register_blueprint(policy_bp, url_prefix="/api/policy")
    app.register_blueprint(insight_bp, url_prefix="/api/insight")
    # Agent 协作轨迹：把"哪个 Agent 定了什么、谁不同意、谁裁定"落成可查记录，
    # 与 audit_log 分工不同（审计给监管看，轨迹给内部复盘/答辩看）
    app.register_blueprint(collab_bp, url_prefix="/api/collab")
    # 人员底册（街道下发的残疾人 / 防溺水 / 留守儿童台账）。
    # 社区角色只看本社区、街道看全街道，收窄在 app/personbook.py:scoped()。
    from app.api.persons import bp as persons_bp
    app.register_blueprint(persons_bp, url_prefix="/api/persons")

    # 侧栏状态灯：首屏由服务端渲染真实状态，之后由 /api/health/llm 轮询刷新。
    # 放在这里注册（而不是 auth 的 context_processor）是因为 health 已经
    # import 了 auth，反向再 import 会成环。
    from app.api.health import llm_badge
    app.jinja_env.globals["llm_badge"] = llm_badge

    # 居民端基础数据：三级管理者公开信息（幂等）
    from app.portal_seed import seed_org_contacts
    try:
        with app.app_context():
            seed_org_contacts()
    except Exception as e:                           # noqa: BLE001
        # 库不可用时这里会挂：同样只记一笔，别把进程带走
        _boot_log(app, f"居民端联系人初始化失败：{type(e).__name__}: {e}")

    # 上级文件自动导入：data/notices/ 里放什么就发什么。
    # 启动时先同步扫一次（保证"部署完立刻可见"），再起后台线程按间隔轮询
    # （保证"运行期间丢进去的文件也会自动上线"，不需要重启或点按钮）。
    if app.config.get("NOTICE_AUTO_IMPORT"):
        try:
            with app.app_context():
                from app.notice.ingest import scan_and_import
                scan_and_import(actor="system")
        except Exception as e:      # 导入失败不能拖垮整个应用启动
            app.logger.warning("公告自动导入失败：%s", e)

        try:
            from app.notice.watcher import start_notice_watcher
            start_notice_watcher(app)
        except Exception as e:
            app.logger.warning("公告定时扫描启动失败：%s", e)

    # 知识库：把 data/kb 下的文件增量入库，并建立检索索引
    from app.kb.seed import init_kb_docs
    from app.kb.store import init_kb
    try:
        init_kb_docs(app)
        init_kb(app)
    except Exception as e:                           # noqa: BLE001
        _boot_log(app, f"知识库初始化失败：{type(e).__name__}: {e}")

    _install_error_recorder(app)
    return app


def _install_error_recorder(app):
    """未捕获异常落审计，别让线上只回一张 500 白页。

    为什么需要：发布环境拿不到 stderr，出了 500 只有"Internal Server Error"一行，
    既不能定位也没法给用户交代。这里把异常类型与堆栈尾部写进 audit_log
    （**先脱敏再截断**，与 BaseAgent 同一套规则），管理员在审计页就能直接看到。
    """
    from flask import request
    from werkzeug.exceptions import HTTPException

    @app.errorhandler(Exception)

    def _on_error(e):                                   # noqa: ANN001
        if isinstance(e, HTTPException):
            return e
        tb_tail, name = "", type(e).__name__
        try:
            import traceback
            from app.agents.base import mask_sensitive
            from app.models import AuditLog
            lines = [ln for ln in traceback.format_exc().strip().splitlines()
                     if ln.strip() and not ln.strip().startswith("^")]
            tb_tail = " | ".join(lines[-4:])
            db.session.rollback()                       # 出错的事务不能带着脏状态提交
            db.session.add(AuditLog(
                actor="system", agent="error",
                action=(request.path or "?")[:80],
                input_digest=mask_sensitive(tb_tail)[:240],
                output_digest=name,
            ))
            db.session.commit()
        except Exception:                               # noqa: BLE001
            db.session.rollback()                       # 记日志失败不能再抛，否则循环
        app.logger.exception("unhandled error: %s", name)
        body = {"code": 500, "msg": "服务内部错误（已记录，管理员可在审计页查看）",
                "data": None}
        if app.config.get("DEBUG"):
            body["debug"] = tb_tail                     # 本地开发直接看到堆栈尾部
        return body, 500

    # ---- 接口层：不存在的路径 / 方法不对，也要回 JSON ----
    # 早先这类请求落到 Flask 默认 HTML 404 页，前端 `const j = await res.json()`
    # 直接抛 SyntaxError，控制台一片红、页面停在"加载中…"，比报错还难查。
    @app.errorhandler(404)
    def _on_404(e):                                     # noqa: ANN001
        if request.path.startswith("/api/"):
            return {"code": 404, "msg": f"接口不存在：{request.path}", "data": None}, 404
        return e

    @app.errorhandler(405)
    def _on_405(e):                                     # noqa: ANN001
        if request.path.startswith("/api/"):
            return {"code": 405,
                    "msg": f"该接口不支持 {request.method} 方法", "data": None}, 405
        return e

    # ---- 跨站写请求拦截 ----
    # 用 Sec-Fetch-Site 而不是比对 Origin 与 Host：托管平台在前面还有一层网关，
    # Host 可能被改写，Origin/Host 一比就会把正常请求全判成跨站（线上直接瘫痪）。
    # Sec-Fetch-Site 由浏览器自己声明来源关系，不依赖任何代理配置：
    #   same-origin / none（地址栏直达）= 放行
    #   same-site（同站不同子域）= 放行（本系统的子域没有额外信任边界）
    #   cross-site = 拒绝
    # 没有这个头的是 curl / 压测 / 服务端调用，不属于浏览器 CSRF 场景，放行。
    # 老浏览器不发这个头，靠 SESSION_COOKIE_SAMESITE=Lax 兜底。
    @app.before_request
    def _block_cross_site_write():
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        site = request.headers.get("Sec-Fetch-Site", "")
        if site == "cross-site":
            return {"code": 403, "msg": "跨站请求已被拒绝", "data": None}, 403
        return None
