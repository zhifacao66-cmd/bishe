# -*- coding: utf-8 -*-
"""生成比赛提交包。

用法:
    python scripts/make_submit.py              # 生成 F:\\bise4-submit + zip
    python scripts/make_submit.py --dry-run    # 只列清单不落盘
    python scripts/make_submit.py --out D:\\xx  # 指定输出目录

设计要点:
1. 白名单式拷贝 —— 只拷指定顶层项，新增的杂项目录默认不进包。
2. 排除规则再叠加一次 —— 防止 data/ 下的 .bak / -wal / smoke_*.db 混进来。
3. 数据库用 sqlite3.backup() 生成快照 —— 直接拷 .db 会丢 WAL 里未 checkpoint 的事务。
4. 不拷密钥(.secret_key / .llm_gateway_token / llm.publish.json)与 AI 会话目录(.workbuddy)。
"""
import argparse
import fnmatch
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = r"F:\bise4-submit"

# ---- 顶层白名单：只有这些进包 ----
INCLUDE = [
    "run.py",
    "config.py",
    "requirements.txt",
    "README.md",
    "提交说明.md",
    "启动系统.bat",
    "停止系统.bat",
    "app",
    "data",
    "docs",
    "demo",
    "thesis",
    "scripts",
]

# ---- 目录级排除（相对路径前缀匹配）----
EXCLUDE_DIRS = [
    "__pycache__",
    ".workbuddy",
    "logs",
    "output",
    "instance",
    os.path.join("scripts", "bin"),   # cloudflared.exe 53MB，非源码
    os.path.join("data", "_sync"),
    os.path.join("data", "_ecsdeploy"),
]

# ---- 文件名模式排除 ----
EXCLUDE_FILES = [
    "*.pyc",
    ".secret_key",
    ".llm_gateway_token",
    "llm.publish.json",
    "*.genie",
    "community.db.bak*",     # 备份库
    "*-wal",                 # SQLite WAL
    "*-shm",                 # SQLite 共享内存
    "smoke_*.db*",           # 测试临时库
    "audit_probe.db*",
    "navtest.db*",
    "sec_probe.db*",
    "eval.db*",
    "soak.db*",
    "NUL",
]

# 需要快照而非直拷的数据库（相对 ROOT）
DB_SNAPSHOT = "data/community.db"


def excluded(rel):
    """rel 用 / 分隔的 posix 相对路径。"""
    parts = rel.split("/")
    for d in EXCLUDE_DIRS:
        d = d.replace("\\", "/")
        if rel == d or rel.startswith(d + "/"):
            return True
    name = parts[-1]
    for pat in EXCLUDE_FILES:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def human(n):
    return "%.1f MB" % (n / 1048576.0) if n >= 1048576 else "%.0f KB" % (n / 1024.0)


def collect():
    """返回 [(绝对路径, 相对路径)]，已过滤。"""
    out = []
    for top in INCLUDE:
        abs_top = os.path.join(ROOT, top)
        if not os.path.exists(abs_top):
            print("  [跳过] 不存在: %s" % top)
            continue
        if os.path.isfile(abs_top):
            if not excluded(top):
                out.append((abs_top, top))
            continue
        for base, dirs, files in os.walk(abs_top):
            dirs[:] = sorted(d for d in dirs if not excluded(
                os.path.relpath(os.path.join(base, d), ROOT).replace("\\", "/")))
            for f in sorted(files):
                ap = os.path.join(base, f)
                rel = os.path.relpath(ap, ROOT).replace("\\", "/")
                if not excluded(rel):
                    out.append((ap, rel))
    return out


def snapshot_db(src, dst):
    """用 sqlite3 在线备份，保证 WAL 里的数据不丢。"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    s = sqlite3.connect("file:%s?mode=ro" % src.replace("\\", "/"), uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
        # 验证快照可用
        n = d.execute("select count(*) from work_item").fetchone()[0]
        return n
    finally:
        d.close()
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-zip", action="store_true")
    ap.add_argument("--skip-audit", action="store_true",
                    help="跳过生成前的凭据/前端体检（不建议）")
    a = ap.parse_args()

    # 交出去的东西必须自己先过一个底线检查。检查放在拷贝之前：
    # 包已经生成再发现问题，通常会"反正都打了就这样吧"，底线就成了摆设。
    if not a.skip_audit:
        r = subprocess.run([sys.executable, "-X", "utf8",
                            os.path.join(ROOT, "scripts", "audit_all.py"),
                            "--only", "A,D", "--quiet"],
                           cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
        if r.returncode != 0:
            print("[中止] 安全体检未通过，先处理下面几项（--skip-audit 可强行跳过）：")
            print(r.stdout[-1500:] or r.stderr[-800:])
            return 1
        print("[体检] 凭据 / 模板转义 / 请求入口 均通过")

    out_dir = a.out
    print("源目录 : %s" % ROOT)
    print("输出   : %s" % out_dir)
    print()

    items = collect()
    total = sum(os.path.getsize(p) for p, _ in items)
    print("待拷 %d 个文件，%s" % (len(items), human(total)))
    for p, rel in items:
        if rel.count("/") == 0 or rel.startswith(("app/", "data/")):
            print("   %-56s %8.0f KB" % (rel, os.path.getsize(p) / 1024.0))

    if a.dry_run:
        print("\n(dry-run，未落盘)")
        return 0

    if os.path.isdir(out_dir):
        print("\n清空旧目录 ...")
        shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    copied = 0
    for src, rel in items:
        dst = os.path.join(out_dir, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if rel == DB_SNAPSHOT:
            n = snapshot_db(src, dst)
            print("\n  [数据库快照] %s -> %d 条业务项" % (rel, n))
        else:
            shutil.copy2(src, dst)
        copied += 1

    # 统计
    n_files, n_size = 0, 0
    for base, _, files in os.walk(out_dir):
        for f in files:
            n_files += 1
            n_size += os.path.getsize(os.path.join(base, f))
    print("\n已生成 %d 个文件，%s" % (n_files, human(n_size)))

    # 自检：不该出现的东西
    bad = []
    for base, _, files in os.walk(out_dir):
        for f in files:
            rel = os.path.relpath(os.path.join(base, f), out_dir).replace("\\", "/")
            if f in (".secret_key", ".llm_gateway_token", "llm.publish.json") \
                    or f.endswith(".pyc") or f.endswith("-wal") or f.endswith("-shm") \
                    or f == "cloudflared.exe":
                bad.append(rel)
    if bad:
        print("  !! 混入不该提交的文件: %s" % bad)
    else:
        print("  自检通过：无密钥 / 无缓存 / 无 WAL / 无 53MB 二进制")

    if not a.no_zip:
        zip_path = os.path.join(os.path.dirname(out_dir),
                                "bise4-submit-%s.zip" % time.strftime("%Y%m%d"))
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for base, _, files in os.walk(out_dir):
                for f in files:
                    p = os.path.join(base, f)
                    z.write(p, os.path.join(os.path.basename(out_dir),
                                            os.path.relpath(p, out_dir)))
        print("\n[zip] %s  %s" % (zip_path, human(os.path.getsize(zip_path))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
