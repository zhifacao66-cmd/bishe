# -*- coding: utf-8 -*-
"""启动前环境自检

给 启动系统.bat 的"环境自检"菜单用，也可以单独跑：

    python scripts/preflight.py            # 完整报告
    python scripts/preflight.py --quick    # 只输出有问题的项（启动流程用）

退出码：0 = 可以启动；1 = 存在致命项（缺 Python 依赖、端口被占等）。

为什么不用 requests：本机装了系统级 HTTP 代理（http_proxy 指向本地端口），
探测 127.0.0.1 时必须显式禁用代理，否则会拿到 502。这里统一用
urllib + 空 ProxyHandler。
"""
import argparse
import os
import socket
import sqlite3
import sys
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 表名 -> 中文标签，用于数据库规模统计
TABLES = [
    ("fde_record", "首录"),
    ("work_item", "业务项"),
    ("work_order", "工单"),
    ("ledger", "台账"),
    ("report", "报表"),
    ("field_security", "密级字段"),
    ("task", "任务"),
]

OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 绕过系统代理


def _pad(text: str, width: int) -> str:
    """按显示宽度补空格（中日韩字符占 2 列）"""
    w = sum(2 if ord(c) > 0x2E7F else 1 for c in text)
    return text + " " * max(0, width - w)


def _get_json(url: str, timeout: float = 2.0):
    with OPEN.open(url, timeout=timeout) as r:
        import json
        return json.loads(r.read().decode("utf-8"))


def _port_free(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


def check():
    """返回 [(level, name, detail, hint)]，level ∈ {OK, WARN, FAIL}"""
    rows = []

    # 1. Python
    v = "{}.{}.{}".format(*sys.version_info[:3])
    if sys.version_info >= (3, 10):
        rows.append(("OK", "Python", v, ""))
    else:
        rows.append(("FAIL", "Python", v, "需要 3.10 及以上，请升级解释器"))

    # 2. 依赖
    missing = []
    for mod, pipname in (("flask", "Flask"), ("flask_sqlalchemy", "Flask-SQLAlchemy"),
                         ("requests", "requests")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pipname)
    if missing:
        rows.append(("FAIL", "依赖", "缺少 " + " / ".join(missing),
                     "pip install -r requirements.txt"))
    else:
        # 不用 flask.__version__：3.1 起会打 DeprecationWarning，把自检输出弄脏
        from importlib.metadata import version as _ver
        try:
            f_v = _ver("flask")
            fsa_v = _ver("flask-sqlalchemy")
        except Exception:
            f_v, fsa_v = "已安装", "已安装"
        rows.append(("OK", "依赖", f"Flask {f_v} / Flask-SQLAlchemy {fsa_v}", ""))

    # 3. 端口
    port = int(os.environ.get("APP_PORT", "5000"))
    if _port_free(port):
        rows.append(("OK", f"端口 {port}", "空闲", ""))
    else:
        # 端口被占不一定是问题：可能就是系统自己在跑
        alive = False
        try:
            _get_json(f"http://127.0.0.1:{port}/api/health", 1.5)
            alive = True
        except Exception:
            pass
        if alive:
            rows.append(("OK", f"端口 {port}", "已被本系统占用（服务已在运行）", ""))
        else:
            rows.append(("FAIL", f"端口 {port}", "被其他程序占用",
                         f"结束占用进程或改端口：set APP_PORT=5001"))

    # 4. Ollama 与模型
    model = os.environ.get("LLM_MODEL", "qwen3:8b")
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
    try:
        ver = _get_json(base_url + "/api/version").get("version", "?")
        tags = _get_json(base_url + "/api/tags", 3.0).get("models", [])
        names = [m.get("name", "") for m in tags]
        rows.append(("OK", "Ollama", f"{ver} 已运行（{len(names)} 个模型）", ""))
        hit = next((m for m in tags if m.get("name") == model), None)
        if hit:
            size = hit.get("size", 0) / 1024 ** 3
            rows.append(("OK", "模型", f"{model}（{size:.2f} GB）", ""))
        else:
            rows.append(("WARN", "模型", f"未拉取 {model}",
                         f"ollama pull {model}；不拉取时系统自动降级为规则拆解"))
    except Exception:
        rows.append(("WARN", "Ollama", "未运行",
                     "启动系统.bat 会自动拉起；也可手动运行 ollama serve"))
        rows.append(("WARN", "模型", "无法确认（服务未就绪）", ""))

    # 5. 数据库
    db_path = os.path.join(BASE, "data", "community.db")
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url and not db_url.startswith("sqlite"):
        rows.append(("OK", "数据库", "外部库 DATABASE_URL（跳过本地统计）", ""))
    elif not os.path.exists(db_path):
        rows.append(("WARN", "数据库", "尚未创建",
                     "首次启动会自动建表；演示前建议跑一次 重置演示数据"))
    else:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            counts, missing_tbl = [], []
            for name, label in TABLES:
                try:
                    n = conn.execute(f"select count(*) from {name}").fetchone()[0]
                    counts.append(f"{label} {n}")
                except sqlite3.Error:
                    missing_tbl.append(label)
            conn.close()
            size = os.path.getsize(db_path) / 1024
            if missing_tbl:
                rows.append(("WARN", "数据库", f"{size:.0f} KB，缺表：{'、'.join(missing_tbl)}",
                             "重启一次应用即可自动补建；或跑 重置演示数据"))
            else:
                rows.append(("OK", "数据库", f"{size:.0f} KB · " + " · ".join(counts), ""))
                fde = int(counts[0].split()[-1])
                if fde < 10:
                    rows.append(("WARN", "演示数据", f"仅 {fde} 条首录，演示会显得很空",
                                 "选择菜单 [3] 重置演示数据"))
                else:
                    rows.append(("OK", "演示数据", "规模够支撑答辩演示", ""))
        except sqlite3.Error as e:
            rows.append(("WARN", "数据库", f"读取失败：{e}", "可删除 data/community.db 后重启重建"))

    # 6. 知识库
    kb_dir = os.path.join(BASE, "data", "kb")
    docs = [f for f in os.listdir(kb_dir)
            if f.lower().endswith((".md", ".txt"))] if os.path.isdir(kb_dir) else []
    if len(docs) >= 4:
        rows.append(("OK", "知识库", f"{len(docs)} 篇（检索可用）", ""))
    elif docs:
        rows.append(("WARN", "知识库", f"仅 {len(docs)} 篇", "RAG 召回会偏弱"))
    else:
        rows.append(("WARN", "知识库", "data/kb 下没有文档", "放入 .md/.txt 后重启即自动入库"))

    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="只输出 WARN/FAIL 项")
    args = ap.parse_args()

    # 不要强制 utf-8：启动器窗口是 cp936，Python 在 Windows 控制台走 Unicode API，
    # 强制 recode 反而会让中文变成乱码。只放宽错误处理，避免个别字符抛异常。
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    rows = check()
    fails = sum(1 for r in rows if r[0] == "FAIL")
    warns = sum(1 for r in rows if r[0] == "WARN")

    if not args.quick:
        line = "  " + "-" * 62
        print(line)
        print("  " + _pad("项目环境自检", 40) + BASE)
        print(line)
        for lv, name, detail, hint in rows:
            tag = {"OK": "[OK]  ", "WARN": "[!!]  ", "FAIL": "[XX]  "}[lv]
            print(f"  {tag}{_pad(name, 12)}{detail}")
            if hint:
                print(f"        {_pad('', 12)}→ {hint}")
        print(line)
        if fails:
            print(f"  结论：有 {fails} 项致命问题，请先修复再启动。")
        elif warns:
            print(f"  结论：可以启动（{warns} 项提示，不影响使用）。")
        else:
            print("  结论：全部通过，可以启动。")
        print(line)
    else:
        problems = [r for r in rows if r[0] != "OK"]
        for lv, name, detail, hint in problems:
            print(f"  [{'!!' if lv == 'WARN' else 'XX'}] {name}：{detail}")
            if hint:
                print(f"        → {hint}")

    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
