import os
from app import create_app
from app.extensions import db

# 托管环境（注入 PORT 而非 APP_PORT）自动切 prod：关掉 DEBUG，
# 否则 Werkzeug 调试器会把源码与调用栈暴露在公网，也避免热重载 fork 子进程。
_HOSTED = bool(os.environ.get("PORT")) and not os.environ.get("APP_PORT")
app = create_app(os.environ.get("FLASK_ENV") or ("prod" if _HOSTED else "dev"))


@app.cli.command("init-db")
def init_db():
    """初始化数据库：flask init-db"""
    db.create_all()
    print("数据库初始化完成")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    # Werkzeug 开发服务器会自带 `Server: Werkzeug/3.1.9 Python/3.13.14`，
    # 这是**服务器层**加的，应用里改不掉（响应头改完它还会再追一条）。
    # 它等于把"该查哪个 CVE"直接告诉扫描器，所以在这里把 banner 改掉。
    # 生产环境由 gunicorn 负责，它的 Server 头本来就不带版本号。
    from werkzeug.serving import WSGIRequestHandler

    class _NoVersionHandler(WSGIRequestHandler):
        server_version = "community-agent"
        sys_version = ""
    # APP_PORT：压测/长稳脚本会拉起独立端口的被测实例，避免与日常开发实例抢端口
    # PORT：托管平台（发布沙箱 / 反向代理）注入的端口，优先级低于 APP_PORT
    port = int(os.environ.get("APP_PORT") or os.environ.get("PORT") or "5000")
    # 由 PORT 判定是否处于托管环境：此时必须绑 0.0.0.0 才能被反向代理转发进来，
    # 本机开发仍绑回环地址（不把 5000 端口暴露到局域网）。
    hosted = bool(os.environ.get("PORT")) and not os.environ.get("APP_PORT")
    host = os.environ.get("APP_HOST") or ("0.0.0.0" if hosted else "127.0.0.1")
    # APP_TEST=1：压测模式，关闭自动重载（否则 werkzeug 会 fork 子进程，PID 与资源采样对不上）
    # 托管环境同样必须关掉重载：自动重载会 fork 子进程，平台按主进程判存活会误判为启动失败。
    test_mode = os.environ.get("APP_TEST") == "1" or hosted
    # 自动重载默认监视整个项目目录，改论文/文档/演示数据也会触发重启（会话空档期父进程被回收会导致服务掉线）。
    # 这里把非代码目录排除，只保留 app/ 与 run.py/config.py 的变更触发重载。
    exclude = [p for p in os.environ.get(
        "APP_WATCH_EXCLUDE", "thesis/*,docs/*,data/*,.workbuddy/*,scripts/*,*.md,*.db"
    ).split(",") if p.strip()]
    print(f"[run] host={host} port={port} reloader={not test_mode}", flush=True)
    app.run(host=host, port=port,
            use_reloader=not test_mode, threaded=True, exclude_patterns=exclude,
            request_handler=_NoVersionHandler)
