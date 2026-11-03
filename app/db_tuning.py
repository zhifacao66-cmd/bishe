"""数据库并发调优（SQLite）

为什么需要这一步：
SQLite 默认使用 rollback journal，同一时刻只允许一个写事务。并发写入时，
后到的事务不会等待，而是立刻返回 SQLITE_BUSY，应用层看到的就是
`OperationalError: database is locked`。FDE 一次首录要写 FDE 记录、业务项、
工单、台账、审计等多张表，写入频次高，是多线程下的第一处瓶颈。

处置：
1. journal_mode=WAL：读写不再互斥，读请求不会被写请求阻塞（单机多线程关键）
2. busy_timeout：写-写冲突时退避重试而不是立即失败（默认 5s 偏短）
3. synchronous=NORMAL：WAL 下仍保证崩溃一致性，写入开销明显低于 FULL
4. wal_autocheckpoint：控制 -wal 文件体积，避免长时间运行无限膨胀

生产切 MySQL 时本模块自动跳过（InnoDB 行锁 + MVCC 已覆盖上述问题）。
"""
from sqlalchemy import event


def is_sqlite(uri: str) -> bool:
    return (uri or "").startswith("sqlite")


def engine_options(cfg) -> dict:
    """必须在 db.init_app 之前写入 SQLALCHEMY_ENGINE_OPTIONS"""
    if not is_sqlite(cfg.get("SQLALCHEMY_DATABASE_URI", "")):
        # MySQL：开连接探活 + 回收，避免长稳期间 MySQL 侧 wait_timeout 断连
        return {"pool_pre_ping": True, "pool_recycle": 1800, "pool_size": 10,
                "max_overflow": 20}
    return {
        "pool_pre_ping": True,
        # check_same_thread=False：允许连接跨线程复用（连接池已保证单线程持有）
        "connect_args": {"timeout": cfg.get("SQLITE_BUSY_TIMEOUT", 20) / 1000.0,
                         "check_same_thread": False},
    }


def tune_sqlite(app) -> bool:
    """给 SQLite 连接挂 PRAGMA。返回是否生效。"""
    uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if not is_sqlite(uri) or not app.config.get("SQLITE_WAL", True):
        return False

    from app.extensions import db

    busy = int(app.config.get("SQLITE_BUSY_TIMEOUT", 20000))

    with app.app_context():
        engine = db.engine

        @event.listens_for(engine, "connect")
        def _set_pragmas(dbapi_conn, _record):   # noqa: ANN001
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")      # 内存库不支持，忽略失败
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute(f"PRAGMA busy_timeout={busy}")
                cur.execute("PRAGMA wal_autocheckpoint=1000")
                cur.execute("PRAGMA foreign_keys=ON")
            finally:
                cur.close()

        # 立刻对已有连接生效（create_app 阶段池为空，这里主要是立即验证）
        with engine.connect() as conn:
            mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    app.logger.info("SQLite 并发模式: journal_mode=%s busy_timeout=%dms", mode, busy)
    return str(mode).lower() == "wal"
