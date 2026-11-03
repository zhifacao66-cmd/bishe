#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""快照覆盖器：把一份 SQLite 快照的内容整份覆盖进目标库。

一条命令做四件事：校验快照 → 事务内覆盖 → 记录状态 → （必要时）重启应用。

## 为什么不用「直接替换 .db 文件」

- Windows 下运行中的进程持有文件句柄，替换会撞上「另一个程序正在使用此文件」；
- 就算换成功，SQLAlchemy 的连接池仍指向旧句柄，页面能打开、数据却是旧的。
  这类问题没有报错，排查成本极高。
- 库只有几百行，DELETE + INSERT 全表是毫秒级，代价可以忽略。

## 为什么快照必须用 sqlite3.backup() 生成

库开着 WAL，**主文件里不含尚未 checkpoint 的事务**。直接拷 `community.db`
会丢掉最近的写入，而且丢得很安静——拷出来的库能打开、`quick_check` 也 ok。

## 用法

    python snapshot_apply.py                      # 应用默认快照
    python snapshot_apply.py --watch 5            # 常驻，每 5 秒看一次
    python snapshot_apply.py --export D:\\out.db   # 把当前库导出成快照（给 pull 用）
    python snapshot_apply.py --status             # 打印状态 JSON

退出码：0 成功/无变化，2 拒绝（快照不可用），3 覆盖失败。
"""
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

# 服务器上的固定位置；本机调用时全部由命令行覆盖
DEF_LIVE = os.environ.get("SYNC_LIVE_DB") or r"C:\app\data\community.db"
DEF_INCOMING = os.environ.get("SYNC_INCOMING") or r"C:\data-incoming\community.db.new"
DEF_STATE = os.environ.get("SYNC_STATE") or r"C:\data-incoming\applied.json"
DEF_LOG = os.environ.get("SYNC_LOG") or r"C:\app\logs\sync.log"

# 少一张就说明这不是本系统的库，整份拒绝
REQUIRED = {"user", "task", "notice", "work_order", "ledger", "audit_log"}

# 覆盖会丢掉目标库独有的审计行；先导出再删，别让证据链无声消失
ORPHAN_KEEP = "audit_log"


# ---------------------------------------------------------------- 基础工具

def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    line = "[%s] %s" % (_now(), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(DEF_LOG), exist_ok=True)
        with open(DEF_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def sha256_file(path, buf=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(buf), b""):
            h.update(chunk)
    return h.hexdigest()


def q(name):
    """表名/列名加引号（表名里可能出现保留字）"""
    return '"' + str(name).replace('"', '""') + '"'


def tables(conn, schema="main"):
    sql = ("select name from %s.sqlite_master where type='table' "
           "and name not like 'sqlite_%%' order by name" % schema)
    return [r[0] for r in conn.execute(sql)]


def columns(conn, table, schema="main"):
    return [r[1] for r in conn.execute("pragma %s.table_info(%s)" % (schema, q(table)))]


def counts(conn, schema="main"):
    out = {}
    for t in tables(conn, schema):
        try:
            out[t] = conn.execute("select count(*) from %s.%s" % (schema, q(t))).fetchone()[0]
        except Exception:
            out[t] = None
    return out


def digest(path):
    """目标库当前状态的指纹（表名 + 行数 + 最大 rowid）。

    为什么不能只看快照 sha：sha 相同只说明**要推的东西没变**，不代表**对方没被改过**。
    线上有人登录一次就会多一条 audit_log，此时再推同一份快照会被「已应用过」挡住，
    两端就永远差那一条。把目标库指纹一起记下来，才能区分这两种情况。
    """
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    except Exception:
        return None
    parts = []
    try:
        for t in tables(c):
            try:
                n = c.execute("select count(*) from main.%s" % q(t)).fetchone()[0]
            except Exception:
                continue
            try:
                mx = c.execute("select coalesce(max(rowid),0) from main.%s" % q(t)).fetchone()[0]
            except Exception:
                mx = 0
            parts.append("%s:%s:%s" % (t, n, mx))
    finally:
        c.close()
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 快照校验

def validate(path):
    """返回 (ok, reason, counts)。坏库整份拒绝，绝不半新半旧。"""
    if not os.path.exists(path):
        return False, "快照不存在", {}
    size = os.path.getsize(path)
    if size < 8192:
        return False, "快照仅 %d 字节，疑似未传完" % size, {}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    except Exception as e:
        return False, "打不开快照：%s: %s" % (type(e).__name__, e), {}
    try:
        chk = c.execute("pragma quick_check").fetchone()[0]
        if chk != "ok":
            return False, "quick_check=%s" % chk, {}
        have = set(tables(c))
        missing = REQUIRED - have
        if missing:
            return False, "缺少关键表：%s" % ",".join(sorted(missing)), {}
        return True, "", counts(c)
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e), {}
    finally:
        c.close()


def load_state():
    try:
        with open(DEF_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(**kw):
    d = load_state()
    d.update(kw)
    try:
        os.makedirs(os.path.dirname(DEF_STATE), exist_ok=True)
        with open(DEF_STATE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("状态文件写入失败：%s" % e)
    return d


# ---------------------------------------------------------------- 导出

def export_snapshot(live, dest):
    """用 backup() 取一致快照。直接拷文件会丢掉 WAL 里未 checkpoint 的事务。"""
    if os.path.exists(dest):
        os.remove(dest)
    src = sqlite3.connect(live, timeout=30)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    c = sqlite3.connect("file:%s?mode=ro" % dest.replace("\\", "/"), uri=True)
    try:
        dest_counts = counts(c)
    finally:
        c.close()
    return {"ok": True, "dest": dest, "bytes": os.path.getsize(dest),
            "sha256": sha256_file(dest), "counts": dest_counts}


# ---------------------------------------------------------------- 覆盖

def _dump_orphans(conn, incoming, out_path):
    """目标库有、快照没有的审计行 —— 覆盖前先存档"""
    try:
        live_ids = {r[0] for r in conn.execute("select id from main.%s" % q(ORPHAN_KEEP))}
        src = sqlite3.connect("file:%s?mode=ro" % incoming.replace("\\", "/"), uri=True)
        try:
            src_ids = {r[0] for r in src.execute("select id from %s" % q(ORPHAN_KEEP))}
            extra = sorted(live_ids - src_ids)
            if not extra:
                return 0, None
            cols = columns(conn, ORPHAN_KEEP)
            sel = "select %s from main.%s where id in (%s)" % (
                ",".join(q(c) for c in cols), q(ORPHAN_KEEP),
                ",".join(str(int(i)) for i in extra))
            rows = [dict(zip(cols, r)) for r in conn.execute(sel)]
        finally:
            src.close()
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        return len(rows), out_path
    except Exception as e:
        log("孤儿审计导出失败（继续，但请留意）：%s" % e)
        return 0, None


def _kb_fingerprint(live):
    """kb_doc 变了才需要重启应用（内存索引不会自己刷新）"""
    try:
        c = sqlite3.connect("file:%s?mode=ro" % live.replace("\\", "/"), uri=True)
        try:
            return c.execute(
                "select coalesce(count(*),0), coalesce(sum(length(coalesce(content,''))),0), "
                "coalesce(max(id),0) from kb_doc").fetchone()
        finally:
            c.close()
    except Exception:
        return None


def _request_sha(incoming):
    """显式推送请求：push 会写 <snapshot>.apply，内容是要应用的 sha。

    轮询任务只认这个标记。没有标记就一个字都不动——
    否则服务器上刚产生的工单、审计会在 60 秒内被静默覆盖回旧快照。
    """
    p = incoming + ".apply"
    if not os.path.exists(p):
        return None
    try:
        return open(p, encoding="utf-8").read().split()[0].strip()
    except Exception:
        return None


def apply_snapshot(incoming, live, restart_task=None, force=False, require_request=False):
    # 先看有没有显式推送请求。轮询任务（每分钟）只认这个标记：
    # 没标记就直接退出，绝不"顺手纠正"目标库——服务器端自己写的工单和审计
    # 一旦被纠正，用户看到的就是"我刚操作完，审计里什么都没有"。
    req = _request_sha(incoming)
    if require_request and not req:
        log("无显式推送请求，跳过（目标库数据保持不动）")
        return 0

    # 旁路校验文件：上传到一半时主数据与 sha 文件对不上，静默等待重传，
    # 不要每分钟刷一条"拒绝应用"，也别把半个库当成坏库报错。
    sha_path = incoming + ".sha"
    if os.path.exists(sha_path) and not force:
        try:
            want = open(sha_path, encoding="utf-8").read().split()[0].strip()
        except Exception:
            want = ""
        if want and os.path.exists(incoming) and sha256_file(incoming) != want:
            log("快照与校验文件不一致（可能正在上传），本次跳过")
            return 0

    ok, reason, src_counts = validate(incoming)
    if not ok:
        log("拒绝应用：%s" % reason)
        return 2

    sha = sha256_file(incoming)
    if req and req != sha:
        log("请求的 sha 与快照不一致（可能正在上传），本次跳过")
        return 0

    explicit = bool(force or req)
    st = load_state()
    live_dg = digest(live)
    if not explicit and st.get("sha256") == sha:
        log("无变化（sha=%s 已应用过），跳过" % sha[:12])
        return 0
    if explicit and st.get("sha256") == sha:
        # 显式推送就该照做：上次应用后目标库可能被写过，sha 相同不代表内容一致
        log("显式推送：快照与上次相同，仍按请求覆盖一次")

    kb_before = _kb_fingerprint(live)
    conn = sqlite3.connect(live, timeout=30)
    conn.isolation_level = None
    orphans, orphan_path = 0, None
    try:
        conn.execute("pragma busy_timeout=20000")
        conn.execute("pragma foreign_keys=off")
        conn.execute("attach database ? as src", (incoming,))

        live_t = set(tables(conn, "main"))
        src_t = set(tables(conn, "src"))
        common = sorted(live_t & src_t)
        # 目标库独有、快照里没有的表：不动。宁可留着，也不要删掉不认识的表。
        skipped = sorted(live_t - src_t)

        if ORPHAN_KEEP in common:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            orphans, orphan_path = _dump_orphans(
                conn, incoming,
                os.path.join(os.path.dirname(os.path.abspath(incoming)),
                             "audit-orphan-%s.jsonl" % stamp))

        conn.execute("begin immediate")
        for t in common:
            conn.execute("delete from main.%s" % q(t))
        for t in common:
            lc = columns(conn, t, "main")
            sc = set(columns(conn, t, "src"))
            use = [c for c in lc if c in sc]   # 按列交集：新代码多出来的列走默认值
            if not use:
                continue
            collist = ",".join(q(c) for c in use)
            conn.execute("insert into main.%s (%s) select %s from src.%s"
                         % (q(t), collist, collist, q(t)))
        conn.execute("commit")
    except Exception as e:
        try:
            conn.execute("rollback")
        except Exception:
            pass
        log("覆盖失败已回滚：%s: %s" % (type(e).__name__, e))
        return 3
    finally:
        try:
            conn.execute("detach database src")
        except Exception:
            pass
        conn.close()

    now_counts = counts(sqlite3.connect(live))
    kb_after = _kb_fingerprint(live)
    need_restart = kb_before is not None and kb_after is not None and kb_before != kb_after

    save_state(sha256=sha, applied_at=_now(), counts=now_counts,
               orphans=orphans, orphan_file=orphan_path,
               live_digest=digest(live))

    # 请求处理完就销掉标记，避免同一份快照被反复应用
    if req:
        try:
            os.remove(incoming + ".apply")
        except Exception:
            pass

    detail = ", ".join("%s=%s" % (k, now_counts[k]) for k in
                       ("user", "task", "notice", "work_order", "ledger", "kb_doc", "audit_log")
                       if k in now_counts)
    log("已覆盖 sha=%s 共 %d 张表（%s）" % (sha[:12], len(common), detail))
    if skipped:
        log("跳过目标库独有的表（未删除）：%s" % ",".join(skipped))
    if orphans:
        log("目标库独有的 %d 条审计已存档：%s" % (orphans, orphan_path))

    if need_restart and restart_task:
        # kb_doc 变了：内存里的检索索引还是旧的，不重启会继续用旧文档回答，页面看不出异常
        log("kb_doc 内容变化，重启应用任务 %s" % restart_task)
        for act in ("/end", "/run"):
            subprocess.run(["schtasks", act, "/tn", restart_task],
                           capture_output=True, text=True)
    elif need_restart:
        log("kb_doc 内容变化，但未指定重启任务：检索索引仍是旧的，建议重启应用")

    return 0


def status(live, incoming):
    out = {"live": live, "incoming": incoming, "state": load_state()}
    if os.path.exists(live):
        c = sqlite3.connect("file:%s?mode=ro" % live.replace("\\", "/"), uri=True)
        try:
            out["live_counts"] = counts(c)
        finally:
            c.close()
    if os.path.exists(incoming):
        out["incoming_sha256"] = sha256_file(incoming)
        out["incoming_bytes"] = os.path.getsize(incoming)
        out["incoming_mtime"] = datetime.fromtimestamp(
            os.path.getmtime(incoming)).strftime("%Y-%m-%d %H:%M:%S")
    out["now"] = _now()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", default=DEF_LIVE)
    ap.add_argument("--incoming", default=DEF_INCOMING)
    ap.add_argument("--export", metavar="DEST", help="把当前库导出成快照")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--watch", type=int, metavar="SEC", help="常驻，每 SEC 秒检查一次")
    ap.add_argument("--force", action="store_true", help="sha 相同也照做")
    ap.add_argument("--require-request", action="store_true",
                    help="只应用带显式推送请求（.apply 标记）的快照，供轮询任务使用")
    ap.add_argument("--restart-task", default=None, help="kb 变化时重启的计划任务名")
    ap.add_argument("--state", default=None, help="状态文件路径（本机使用时改到项目内）")
    ap.add_argument("--log", dest="log_path", default=None, help="日志文件路径")
    a = ap.parse_args(argv)

    # 本机执行时不能往 C:\ 根目录写，必须允许改到项目目录里
    global DEF_STATE, DEF_LOG
    if a.state:
        DEF_STATE = a.state
    if a.log_path:
        DEF_LOG = a.log_path

    if a.export:
        print(json.dumps(export_snapshot(a.live, a.export), ensure_ascii=False))
        return 0
    if a.status:
        print(json.dumps(status(a.live, a.incoming), ensure_ascii=False, indent=2))
        return 0
    if a.watch:
        log("守护启动：每 %d 秒检查 %s" % (a.watch, a.incoming))
        while True:
            try:
                apply_snapshot(a.incoming, a.live, a.restart_task, a.force,
                               require_request=a.require_request)
            except Exception as e:
                log("守护循环异常（继续）：%s: %s" % (type(e).__name__, e))
            time.sleep(a.watch)
    return apply_snapshot(a.incoming, a.live, a.restart_task, a.force,
                          require_request=a.require_request)


if __name__ == "__main__":
    sys.exit(main())
