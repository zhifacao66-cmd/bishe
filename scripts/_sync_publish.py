"""同步最新代码与演示数据到发布副本（仅供本地发布前使用，不随包上传）。

用法：python scripts/_sync_publish.py [目标目录]
默认目标：F:\\bise4-publish

要点：
- app/ 全量镜像（剔除 __pycache__），保证不残留已删除的旧模块
- data/ 只带三类：community.db（一致性快照）、kb/、notices/
- 评测产物（e2e_*.json / soak.db / conc_*.json 等）不随包走，省体积
"""
import os
import shutil
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_args = [a for a in sys.argv[1:] if not a.startswith("--")]
DST = _args[0] if _args else r"F:\bise4-publish"

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")


def mirror(src, dst):
    """dst 完全替换为 src 的副本"""
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=IGNORE)


def snapshot_db(src, dst):
    """用 sqlite 在线备份 API 取一致快照，避免直接 cp 到半截 WAL 状态"""
    if os.path.exists(dst):
        os.remove(dst)
    con = sqlite3.connect(src)
    out = sqlite3.connect(dst)
    try:
        con.backup(out)
    finally:
        out.close()
        con.close()


def main():
    if not os.path.isdir(DST):
        os.makedirs(DST)

    # 1. 代码
    mirror(os.path.join(ROOT, "app"), os.path.join(DST, "app"))
    for f in ("run.py", "config.py", "requirements.txt"):
        shutil.copy2(os.path.join(ROOT, f), os.path.join(DST, f))

    # 发布沙箱按字符串扫描依赖清单，注释里出现 MySQL 驱动名也会被判为「需要外部数据库」
    # 而直接拒绝部署，所以发布副本用一份只含运行时依赖的干净清单（本地原文件保留说明）。
    with open(os.path.join(DST, "requirements.txt"), "w", encoding="utf-8") as fp:
        fp.write(
            "# 发布副本专用依赖清单：运行时仅需以下四项，数据库用随项目走的 SQLite。\n"
            "Flask>=3.0\n"
            "Flask-SQLAlchemy>=3.1\n"
            "requests>=2.31\n"
            "# 公文导出要用：纯 Python 库，lxml 有 manylinux wheel，沙箱能装上。\n"
            "# 装不上时 export.build() 会抛 ExportUnavailable，页面提示「无法生成 Word」，\n"
            "# 其余功能不受影响——降级路径是设计好的，不是异常。\n"
            "python-docx>=1.1\n"
        )

    # 发布沙箱探测 Python 项目入口时优先找 main.py / app.py，找不到就直接 python main.py 而失败。
    # 本项目真正入口是 run.py，这里生成一份薄壳 main.py 转发过去，避免被误判成「无启动入口」。
    #
    # create_all 必须包 try：沙箱的健康检查是"60s 内 3000 端口有没有人监听"，
    # 而这里一旦抛异常进程立刻退出，端口永远不 listening，发布的报错只会说
    # "service did not become reachable"——看不出是建表失败。
    # create_app 里的 _boot_database 已经处理过坏库自愈，走到这里还失败说明
    # 是别的问题，那就让服务照常起来：能开页面、能在审计页看到错误，
    # 比"整个站点连不上"好判断得多。启动日志逐段 flush，沙箱那边才截得到。
    with open(os.path.join(DST, "main.py"), "w", encoding="utf-8") as fp:
        fp.write(
            '"""发布沙箱入口：沙箱按 main.py 探测，实际逻辑仍由 run.py 提供。"""\n'
            "import os\n"
            "import sys\n"
            "import traceback\n\n"
            "print('[boot] importing app', flush=True)\n"
            "try:\n"
            "    from app.extensions import db\n"
            "    from run import app\n"
            "except Exception:\n"
            "    traceback.print_exc()\n"
            "    sys.stdout.flush()\n"
            "    raise\n"
            "print('[boot] app imported', flush=True)\n\n"
            'if __name__ == "__main__":\n'
            "    try:\n"
            "        with app.app_context():\n"
            "            db.create_all()\n"
            "        print('[boot] create_all ok', flush=True)\n"
            "    except Exception:\n"
            "        # 建表失败不该让整个站点连不上：页面能开会比进程秒退好排查\n"
            "        print('[boot] create_all FAILED (continue anyway)', flush=True)\n"
            "        traceback.print_exc()\n"
            "        sys.stdout.flush()\n"
            '    port = int(os.environ.get("PORT") or "3000")\n'
            "    print(f'[boot] listening on 0.0.0.0:{port}', flush=True)\n"
            "    try:\n"
            "        app.run(host=\"0.0.0.0\", port=port, threaded=True,\n"
            "                use_reloader=False)\n"
            "    except Exception:\n"
            "        traceback.print_exc()\n"
            "        sys.stdout.flush()\n"
            "        raise\n"
        )

    # 2. 数据
    ddata = os.path.join(DST, "data")
    if not os.path.isdir(ddata):
        os.makedirs(ddata)
    for sub in ("kb", "notices"):
        s = os.path.join(ROOT, "data", sub)
        d = os.path.join(ddata, sub)
        if os.path.isdir(s):
            mirror(s, d)
    snapshot_db(os.path.join(ROOT, "data", "community.db"),
                os.path.join(ddata, "community.db"))
    # 另存一份种子库：线上持久卷一旦损坏（database disk image is malformed），
    # 代码改不动那个文件，只能靠这个随包副本把它换掉（见 create_app 的 reset 逻辑）。
    shutil.copy2(os.path.join(ddata, "community.db"),
                 os.path.join(ddata, "community.db.seed"))
    # WAL/SHM 是运行时产物：本地一旦有人用发布副本试跑过（验证启动、拍照验收），
    # 这里就会留下 -wal/-shm。它们和主库是**配套**的，单独拷到线上等于把
    # "还没合并进主库的事务"丢掉，而线上又拿这堆文件当自己的库——
    # 表现就是数据莫名少几条、严重时 PRAGMA 直接报 malformed。
    # 快照API（con.backup）出来的主库本身是自洽的，这两个必须删掉。
    # 同理 .reset_db.done 和 community.db.bak 也是"本地执行过重置"的残迹：
    # 前者一旦跟着上传，线上会认为这次标记已经执行过而**跳过重置**——
    # 明明加了标记却没生效，比不加重置更难查。这些都属于本机垃圾，一律清掉。
    for junk in ("community.db-wal", "community.db-shm", "community.db.bak",
                 ".reset_db.done"):
        p = os.path.join(ddata, junk)
        if os.path.exists(p):
            os.remove(p)
            print(f"[sync] 已清理运行时残留 {junk}")
    if "--reset-remote-db" in sys.argv:
        import time as _t
        with open(os.path.join(ddata, ".reset_db"), "w", encoding="utf-8") as fp:
            fp.write(f"reset-{int(_t.time())}")
        print("[sync] 已放置重置标记 data/.reset_db（下次启动会换库）")
    else:
        flag = os.path.join(ddata, ".reset_db")
        if os.path.exists(flag):
            os.remove(flag)                 # 上一次的标记不能一直留着

    # 实例级会话密钥：沙箱里每次部署都会覆盖 app/，但根目录的隐藏文件不会被清，
    # 所以首次生成后一直复用，表现为"重新发布不会把导师的登录踢下线"。
    import secrets as _secrets
    keyfile = os.path.join(DST, ".secret_key")
    if not os.path.exists(keyfile) or not open(keyfile, encoding="utf-8").read().strip():
        with open(keyfile, "w", encoding="utf-8") as fp:
            fp.write(_secrets.token_urlsafe(48))
        print("[sync] 已生成实例级 SECRET_KEY（.secret_key）")

    # 线上模型通道：沙箱是没有显卡的无状态容器，也没有环境变量注入能力，
    # 只能靠根目录这个文件把「推理请求经隧道回本机 GPU」告诉线上实例。
    # 文件里含网关令牌，所以源文件名带 publish 后缀、不进版本库；
    # 每次同步都重写/删除，避免残留上一次的隧道地址（地址每次重建都会变）。
    src_llm = os.path.join(ROOT, "llm.publish.json")
    dst_llm = os.path.join(DST, "llm.json")
    if os.path.exists(src_llm):
        shutil.copy2(src_llm, dst_llm)
        print("[sync] 已写入线上模型通道配置 llm.json")
    elif os.path.exists(dst_llm):
        os.remove(dst_llm)
        print("[sync] 源端无 llm.publish.json，已移除旧的 llm.json（线上走规则降级）")

    # 3. 统计
    total = 0
    n = 0
    for base, _, files in os.walk(DST):
        for f in files:
            total += os.path.getsize(os.path.join(base, f))
            n += 1
    print(f"[sync] -> {DST}")
    print(f"[sync] {n} 个文件，{total / 1024 / 1024:.2f} MB")


if __name__ == "__main__":
    main()
