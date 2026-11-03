"""数据库备份与恢复演练 —— 每天一份一致快照，带自检与轮转。

为什么不能直接复制 .db 文件
--------------------------
SQLite 开了 WAL 后，最近的事务还在 `-wal` 里没落盘。用文件管理器复制
`community.db`，拿到的是"上一次 checkpoint 时的库"——看起来正常、能打开，
但少掉最新几条工单和审计。**坏备份比没有备份更危险**，因为它让人以为有退路。

`sqlite3.Connection.backup()` 走的是 SQLite 自己的在线备份协议，会把 WAL 里
未落盘的内容一起搬过去，得到的是一个自洽的快照。项目的 ECS 同步与发布
也用的这个（红线：不替换 .db 文件，只用 backup 生成快照）。

每份备份都要过自检
------------------
生成后立刻做三件事，任何一项不过就删掉并报错（不留下"看起来有"的坏备份）：
  1. `PRAGMA integrity_check` 必须返回 ok
  2. 关键表行数与源库逐个相等（不是"非空"，是"相等"）
  3. 能被独立连接打开（模拟恢复时真的读一遍）

用法
----
    python scripts/backup_db.py                    # 备份一次 + 自检 + 轮转
    python scripts/backup_db.py --keep 30          # 保留 30 份
    python scripts/backup_db.py --list             # 列出现有备份与自检状态
    python scripts/backup_db.py --verify           # 验证最近一份能否恢复
    python scripts/backup_db.py --install-task     # 注册每日 02:00 计划任务
    python scripts/backup_db.py --remove-task      # 取消计划任务
    python scripts/backup_db.py --restore <file>   # 恢复到新文件（不覆盖现网库）
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "community.db"
DST_DIR = ROOT / "data" / "backups"
# 居民上传的证据照片就落在这里。它必须跟着数据库一起备：
# 只备库、不备图，链上还记着 sha256，真出事时能证明"这张图没被改"却拿不出图，
# 存证就废了一半。
EVIDENCE_DIR = Path(os.environ.get("EVIDENCE_DIR") or (ROOT / "data" / "evidence"))
TASK = "bise4-backup"
# 这几张表是业务核心，行数对不上说明快照不完整。
# 表名以 sqlite_master 实查为准，别凭印象写（第一版把 fde_record 写成
# field_report，取数失败返回 -1，两边都是 -1 就"相等"通过 —— 等于没检查）。
KEY_TABLES = ("work_item", "work_order", "ledger", "audit_log",
              "fde_record", "field_security", "resident_ticket")


def _counts(con: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for t in KEY_TABLES:
        try:
            out[t] = con.execute("select count(*) from %s" % t).fetchone()[0]
        except sqlite3.Error:
            out[t] = -1
    return out


def _integrity(path: Path) -> tuple[bool, str]:
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path.as_posix(), uri=True)
        try:
            r = con.execute("PRAGMA integrity_check").fetchone()[0]
            return r == "ok", r
        finally:
            con.close()
    except Exception as e:                                        # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def do_backup(keep: int, quiet: bool = False) -> Path | None:
    if not SRC.exists():
        print("[失败] 找不到 %s" % SRC)
        return None
    DST_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = DST_DIR / f"community-{ts}.db"

    # 1. 在线备份：连源库、写到目标，WAL 内容一并搬过去
    src = sqlite3.connect(str(SRC), timeout=30)
    try:
        src.execute("PRAGMA busy_timeout=20000")
        want = _counts(src)
        out = sqlite3.connect(str(dst))
        try:
            src.backup(out)
            out.commit()
        finally:
            out.close()
    finally:
        src.close()

    # 2. 自检：任何一项不过就删掉，宁可报错也不留坏备份
    ok, msg = _integrity(dst)
    if not ok:
        dst.unlink(missing_ok=True)
        print("[失败] 备份完整性检查未通过：%s（已删除）" % msg)
        return None

    chk = sqlite3.connect("file:%s?mode=ro" % dst.as_posix(), uri=True)
    try:
        got = _counts(chk)
    finally:
        chk.close()
    diff = {t: (want[t], got[t]) for t in KEY_TABLES if want[t] != got[t]}
    # -1 表示这张表读不到（表名写错/不存在）。两边都 -1 会"相等"通过，
    # 那这条校验就成了摆设 —— 必须单独拦掉，否则备份缺表也显示绿灯。
    broken = [t for t in KEY_TABLES if want[t] < 0 or got[t] < 0]
    if broken:
        dst.unlink(missing_ok=True)
        print("[失败] 关键表读不到：%s（表名或库结构变了，检查 KEY_TABLES）" % "、".join(broken))
        return None
    if diff:
        dst.unlink(missing_ok=True)
        detail = "、".join(f"{t} 源 {a} ≠ 备 {b}" for t, (a, b) in diff.items())
        print("[失败] 备份行数与源库不一致：%s（已删除）" % detail)
        return None

    size = dst.stat().st_size
    if not quiet:
        print("[完成] %s  %.1f KB  完整性 ok" % (dst.relative_to(ROOT), size / 1024))
        print("       " + "  ".join(f"{t}={got[t]}" for t in KEY_TABLES))
        print("[口径] 用 sqlite3.backup() 而非复制文件：WAL 里未 checkpoint 的事务也算在内")

    _backup_evidence(dst, quiet=quiet)
    rotate(keep, quiet=quiet)
    return dst


def _backup_evidence(dst: Path, quiet: bool = False) -> Path | None:
    """把"库里真的引用到的"附件打成一个 zip，放在备份库旁边

    只打被引用到的文件，不整目录打包：目录里会有历史残留
    （传了但事务回滚、单子被删），整包会让备份无限膨胀，
    而真正需要留的证据其实只有库里认的那些。

    缺文件时**不阻断备份**——数据库仍然是有效的备份，
    但必须报警：这是"链在、图没了"的唯一发现时机。
    """
    import json
    import zipfile

    if not EVIDENCE_DIR.exists():
        return None
    refs = []
    con = sqlite3.connect(str(dst))
    try:
        try:
            rows = con.execute(
                "select attachments from resident_ticket "
                "where attachments is not null and attachments != ''").fetchall()
        except sqlite3.OperationalError:
            return None      # 老库还没这列，没什么可备的
        for (raw,) in rows:
            try:
                items = json.loads(raw)
            except (ValueError, TypeError):
                continue
            for it in (items or []):
                if isinstance(it, dict) and it.get("stored"):
                    refs.append(it)
    finally:
        con.close()
    if not refs:
        return None

    zp = dst.with_name(dst.stem + "-evidence.zip")
    missing = []
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for it in refs:
            f = EVIDENCE_DIR / str(it["stored"])
            if not f.exists():
                missing.append(str(it.get("name") or it["stored"]))
                continue
            z.write(f, arcname=str(it["stored"]))
    if missing:
        print("[警告] 库引用了 %d 个附件但文件不在，备份里缺：%s"
              % (len(missing), "、".join(missing[:3])))
    if not quiet:
        print("[完成] %s  %d 个附件" % (zp.relative_to(ROOT), len(refs) - len(missing)))
    return zp


def rotate(keep: int, quiet: bool = False):
    files = sorted(DST_DIR.glob("community-*.db"))
    if len(files) <= keep:
        return
    drop = files[: len(files) - keep]
    for f in drop:
        f.unlink(missing_ok=True)
    if not quiet and drop:
        print("[轮转] 删除 %d 份旧备份（保留最近 %d 份）" % (len(drop), keep))


def do_list():
    files = sorted(DST_DIR.glob("community-*.db"))
    if not files:
        print("暂无备份。跑一次 python scripts/backup_db.py 生成。")
        return 0
    print("%-30s %10s  %-6s %s" % ("文件", "大小", "完整性", "关键表行数"))
    bad = 0
    for f in files:
        ok, _ = _integrity(f)
        con = sqlite3.connect("file:%s?mode=ro" % f.as_posix(), uri=True)
        try:
            c = _counts(con)
        finally:
            con.close()
        if not ok:
            bad += 1
        print("%-30s %9.1fK  %-6s %s" % (f.name, f.stat().st_size / 1024,
                                         "ok" if ok else "损坏",
                                         " ".join(f"{k}={v}" for k, v in c.items())))
    print("\n共 %d 份，损坏 %d 份" % (len(files), bad))
    return 1 if bad else 0


def do_verify():
    files = sorted(DST_DIR.glob("community-*.db"))
    if not files:
        print("[失败] 没有可验证的备份")
        return 1
    f = files[-1]
    ok, msg = _integrity(f)
    if not ok:
        print("[失败] %s 完整性检查未通过：%s" % (f.name, msg))
        return 1
    # 真的当数据库用一次：能打开、能查、能算，才算"可恢复"
    con = sqlite3.connect("file:%s?mode=ro" % f.as_posix(), uri=True)
    try:
        c = _counts(con)
        orders = con.execute("""select count(*) from work_order
                                 where status in ('open','doing','done')""").fetchone()[0]
        latest = con.execute("select max(created_at) from work_order_log").fetchone()[0]
    finally:
        con.close()
    print("[通过] %s" % f.name)
    print("       完整性 ok ｜ " + " ".join(f"{k}={v}" for k, v in c.items()))
    print("       有效工单 %d ｜ 最近流转 %s" % (orders, latest))
    print("[提示] 恢复演练：把该文件复制成 data/community.db 后启动应用；")
    print("       生产环境不要直接覆盖运行中的库（句柄占用），先停服再换。")
    return 0


def _py() -> str:
    return sys.executable


def install_task(hour: int = 2):
    cmd = (f'"{_py()}" -X utf8 "{ROOT / "scripts" / "backup_db.py"}" --keep 14')
    args = ["schtasks", "/Create", "/TN", TASK, "/TR", cmd,
            "/SC", "DAILY", "/ST", f"{hour:02d}:00", "/F"]
    r = subprocess.run(args, capture_output=True, text=True,
                       encoding="gbk", errors="replace", timeout=60)
    if r.returncode != 0:
        print("[失败] 注册计划任务出错：%s" % (r.stdout + r.stderr).strip()[:300])
        return 1
    print("[完成] 已注册计划任务 %s：每天 %02d:00 备份，保留 14 份" % (TASK, hour))
    print("       查看：schtasks /Query /TN %s" % TASK)
    print("       取消：python scripts/backup_db.py --remove-task")
    return 0


def remove_task():
    r = subprocess.run(["schtasks", "/Delete", "/TN", TASK, "/F"],
                       capture_output=True, text=True, encoding="gbk",
                       errors="replace", timeout=60)
    print("[完成] 已取消计划任务 %s" % TASK if r.returncode == 0
          else "[提示] 任务不存在或取消失败：%s" % (r.stdout + r.stderr).strip()[:200])
    return 0


def do_restore(src: str):
    """恢复到**新文件**，绝不覆盖现网库。

    覆盖运行中的 SQLite 是事故高发点：文件句柄被占用时替换会失败或写坏，
    连接池里还指着旧 inode。恢复应该是"先验证、再停服、再换文件"的人工动作，
    脚本只负责把验证过的文件摆好。
    """
    s = Path(src)
    if not s.exists():
        s = DST_DIR / src
    if not s.exists():
        print("[失败] 找不到备份文件：%s" % src)
        return 1
    ok, msg = _integrity(s)
    if not ok:
        print("[失败] 该备份不完整（%s），拒绝用于恢复" % msg)
        return 1
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ROOT / "data" / f"restored-{ts}.db"
    shutil.copy2(s, out)
    print("[完成] 已复制到 %s" % out.relative_to(ROOT))
    print("       切换步骤：停应用 → 备份现网库 → 用该文件替换 data/community.db → 启应用")
    print("       直接覆盖运行中的库会因句柄占用而失败或写坏，别这么做。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="数据库备份 / 自检 / 轮转 / 恢复演练")
    ap.add_argument("--keep", type=int, default=14, help="保留份数（默认 14）")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--restore", metavar="FILE")
    ap.add_argument("--install-task", action="store_true")
    ap.add_argument("--remove-task", action="store_true")
    ap.add_argument("--hour", type=int, default=2, help="计划任务触发小时（默认 2）")
    a = ap.parse_args()

    if a.list:
        return do_list()
    if a.verify:
        return do_verify()
    if a.restore:
        return do_restore(a.restore)
    if a.install_task:
        return install_task(a.hour)
    if a.remove_task:
        return remove_task()
    return 0 if do_backup(a.keep) else 1


if __name__ == "__main__":
    sys.exit(main())
