"""公告目录定时扫描：把「文件丢进去就自动上线」做成真的自动

启动时扫一次，只能覆盖「部署时文件已经在目录里」的情况。运行期间上级单位
发来新文件，用户仍然得点一下「扫描」或者重启——这就不叫自动化了，只是
把手工动作挪到了别处。这里起一个守护线程按固定间隔比对目录，变了就导入。

四个设计约束：

1. **daemon 线程**：不能阻止进程退出，Ctrl+C / 进程结束时立刻收手。
2. **线程内自带 app context**：后台线程没有请求上下文，不包 app_context
   直接用 db 会抛 RuntimeError，而线程里的异常没人接管，表现为"功能静默失效"。
3. **先比签名再解析**：每轮只做 stat 比对（文件名 + mtime + size），
   目录没变就什么都不做，避免每分钟重复读盘解析 docx。
4. **异常全部吞掉并记日志**：一轮失败不能让线程死掉，否则要等下次重启才恢复。

重复扫描是安全的：ingest 以 source_hash 做去重，同一份文件不会重复发布。
因此 dev 自动重载导致的父子双进程、gunicorn 多 worker 都只是多扫几次，
不会产生重复公告——这一点是"敢开定时任务"的前提。
"""
from __future__ import annotations

import threading
from pathlib import Path

from app.notice.parse import SUPPORTED


def _signature(directory: Path) -> tuple:
    """目录当前指纹：文件名 + mtime + 大小。任何一项变了就认为需要重扫。"""
    items = []
    try:
        for p in sorted(directory.iterdir()):
            if not p.is_file() or p.name.startswith((".", "~$")):
                continue
            if p.suffix.lower() not in SUPPORTED:
                continue
            st = p.stat()
            items.append((p.name, int(st.st_mtime), st.st_size))
    except FileNotFoundError:
        pass
    return tuple(items)


def start_notice_watcher(app, interval: int | None = None):
    """启动后台扫描线程；返回 stop 事件（测试里可提前停掉），已关闭时返回 None"""
    if interval is None:
        interval = int(app.config.get("NOTICE_SCAN_INTERVAL", 60))
    if interval <= 0:
        app.logger.info("公告定时扫描已关闭（NOTICE_SCAN_INTERVAL=0）")
        return None

    from app.notice.ingest import notice_dir

    stop = threading.Event()

    # 初始指纹必须在**启动线程之前**、在主线程里采集。
    # 如果放到线程体里采，线程真正被调度可能晚于"用户把文件丢进来"的那一瞬间，
    # 于是这份文件会被当成初始状态，之后怎么比对都"没变化"，永远不会被导入。
    try:
        with app.app_context():
            state = {"sig": _signature(notice_dir())}
    except Exception:
        state = {"sig": None}

    def loop():
        while not stop.is_set():
            try:
                with app.app_context():
                    d = notice_dir()
                    sig = _signature(d)
                    if sig != state["sig"]:
                        from app.notice.ingest import scan_and_import
                        r = scan_and_import(actor="system")
                        changed = r["imported"] + r["pending"] + r["updated"]
                        if changed:
                            app.logger.info(
                                "[公告自动发布] 新增 %d / 更新 %d / 待确认 %d / 跳过 %d",
                                len(r["imported"]), len(r["updated"]),
                                len(r["pending"]), len(r["skipped"]))
                        if r["failed"]:
                            app.logger.warning("[公告自动发布] 解析失败：%s",
                                               [f["file"] for f in r["failed"]])
                        state["sig"] = sig
            except Exception as e:                  # 单轮失败不影响后续轮次
                app.logger.warning("[公告自动发布] 扫描异常：%s: %s",
                                   type(e).__name__, e)
            stop.wait(interval)

    t = threading.Thread(target=loop, name="notice-watcher", daemon=True)
    t.start()
    app.logger.info("[公告自动发布] 定时扫描已启动，间隔 %ds（目录 %s）",
                    interval, app.config.get("NOTICE_DIR"))
    return stop
