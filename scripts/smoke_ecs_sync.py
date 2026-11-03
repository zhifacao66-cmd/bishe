"""回归：快照覆盖器（ECS 双端同步的核心逻辑）

它做的事很"危险"——整库 DELETE + INSERT。所以这里只钉五类约束：

  ① **幂等**：同一个快照跑两遍，第二遍必须什么都不做（演示时手滑连按两次不能出事）；
  ② **坏库整份拒绝**：quick_check 不过、文件过小、缺关键表，一律不动目标库——
     半新半旧的库比干脆失败更难救；
  ③ **半包不上车**：上传到一半时旁路 sha 对不上，静默跳过而不是报错；
  ④ **不认识的东西不删**：目标库独有的表、代码新加但快照里没有的列，都要活着；
  ⑤ **证据链**：目标库独有的审计行，覆盖前必须先导出成 jsonl。

第 ④ 条最容易写错：早先的想法是"整表对齐"，那会在代码升级后把新表删掉、
或者因为多一列直接 INSERT 失败。这里用列交集 + 只删双方都有的表。
"""
import pathlib
import os
import shutil
import sqlite3
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))
import snapshot_apply as sa  # noqa: E402

WORK = BASE / "data" / "_synctest"
SEED = BASE / "data" / "community.db"
PASS = FAIL = 0
FAILED = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  [{detail}]" if detail else ""))


def fresh(tag):
    """每个用例一套独立目录：库、状态文件、日志都分开，互不影响"""
    d = WORK / tag
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    live = d / "live.db"
    sa.export_snapshot(str(SEED), str(live))
    sa.DEF_STATE = str(d / "state.json")
    sa.DEF_LOG = str(d / "sync.log")
    return d, live


def snapshot(d, live):
    snap = d / "snap.db"
    sa.export_snapshot(str(live), str(snap))
    return snap


def q(path, sql):
    c = sqlite3.connect(str(path))
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def x(path, sql):
    c = sqlite3.connect(str(path))
    try:
        c.execute(sql)
        c.commit()
    finally:
        c.close()


def counts(path):
    c = sqlite3.connect(str(path))
    try:
        return sa.counts(c)
    finally:
        c.close()


print("=" * 66)
print("快照覆盖器回归")
print("=" * 66)

# ---------------------------------------------------------------- ① 基本覆盖 + 幂等
print("\n[A] 覆盖与幂等")
d, live = fresh("apply")
snap = snapshot(d, live)
x(snap, "update task set status='done' where id=1")
x(snap, "insert into audit_log (actor,agent,action,input_digest,output_digest,created_at) "
        "values ('t','t','only-in-snap','','','2026-10-23 12:00:00')")
before = counts(live)
check("快照可读且行数正确", counts(snap)["audit_log"] == before["audit_log"] + 1)

rc = sa.apply_snapshot(str(snap), str(live), None)
after = counts(live)
check("应用退出码为 0", rc == 0, f"rc={rc}")
check("审计行数已同步",
      after["audit_log"] == before["audit_log"] + 1,
      f"{before['audit_log']} -> {after['audit_log']}")
check("内容真的变了（task#1 状态）",
      q(live, "select status from task where id=1")[0][0] == "done")
check("其余表行数保持一致",
      all(after[k] == counts(snap)[k] for k in ("user", "notice", "work_order", "ledger", "kb_doc")))

rc2 = sa.apply_snapshot(str(snap), str(live), None)
check("重复应用被跳过（幂等）", rc2 == 0 and counts(live)["audit_log"] == after["audit_log"])
check("跳过时写了结论日志",
      "无变化" in (d / "sync.log").read_text(encoding="utf-8"))

# ---------------------------------------------------------------- ② 坏库整份拒绝
print("\n[B] 坏库整份拒绝")
d, live = fresh("bad")
keep = counts(live)

bad = d / "bad.db"
bad.write_bytes(b"this is definitely not a sqlite database" * 400)
rc = sa.apply_snapshot(str(bad), str(live), None)
check("非数据库文件被拒绝", rc == 2, f"rc={rc}")
check("目标库未被改动", counts(live) == keep)

small = d / "small.db"
small.write_bytes(b"x" * 100)
rc = sa.apply_snapshot(str(small), str(live), None)
check("过小文件被拒绝", rc == 2, f"rc={rc}")

empty = d / "empty.db"
c = sqlite3.connect(str(empty))
c.execute("create table task (id integer primary key)")
c.commit()
c.close()
rc = sa.apply_snapshot(str(empty), str(live), None)
check("缺关键表被拒绝", rc == 2, f"rc={rc}")
check("两次拒绝后目标库仍然完好", counts(live) == keep)

# ---------------------------------------------------------------- ③ 半包不上车
print("\n[C] 上传半包不上车")
d, live = fresh("half")
snap = snapshot(d, live)
x(snap, "insert into audit_log (actor,agent,action,input_digest,output_digest,created_at) "
        "values ('t','t','half','','','2026-10-23 12:00:00')")
(snap.parent / "half.db.sha").write_text("0" * 64 + "\n", encoding="utf-8")
before = counts(live)
shutil.copy(str(snap), str(d / "half.db"))
rc = sa.apply_snapshot(str(d / "half.db"), str(live), None)
check("sha 不符时静默跳过", rc == 0 and counts(live) == before)
check("日志写明是上传未完成",
      "不一致" in (d / "sync.log").read_text(encoding="utf-8"))

# ---------------------------------------------------------------- ④ 不认识的东西不删
print("\n[D] 目标库独有的表与列")
d, live = fresh("extra")
x(live, "create table _extra_by_code (id integer primary key, note text)")
x(live, "insert into _extra_by_code (note) values ('keep me')")
x(live, "alter table task add column extra_col text")
snap = snapshot(d, live)          # 快照里带着 _extra_by_code，但先删掉模拟"对方没有"
x(snap, "drop table _extra_by_code")
rc = sa.apply_snapshot(str(snap), str(live), None)
check("应用成功", rc == 0, f"rc={rc}")
check("目标库独有的表未被删除",
      q(live, "select name from sqlite_master where name='_extra_by_code'") != [])
check("独有表里的数据还在",
      q(live, "select note from _extra_by_code") == [("keep me",)])
check("代码新加的列仍在",
      "extra_col" in [r[1] for r in q(live, "pragma table_info(task)")])

# ---------------------------------------------------------------- ⑤ 证据链
print("\n[E] 覆盖前导出孤儿审计")
d, live = fresh("orphan")
snap = snapshot(d, live)
x(live, "insert into audit_log (actor,agent,action,input_digest,output_digest,created_at) "
        "values ('x','x','only-on-live','','','2026-10-23 12:00:00')")
before = counts(live)["audit_log"]
rc = sa.apply_snapshot(str(snap), str(live), None)
after = counts(live)["audit_log"]
orphans = list(d.glob("audit-orphan-*.jsonl"))
check("覆盖后回到快照的行数", rc == 0 and after == before - 1, f"{before} -> {after}")
check("孤儿审计已导出", len(orphans) == 1, f"找到 {len(orphans)} 个")
if orphans:
    body = orphans[0].read_text(encoding="utf-8")
    check("导出内容正确", "only-on-live" in body)

# ---------------------------------------------------------------- ⑥ 轮询不许覆盖
print("\n[F] 轮询任务不许碰服务器端自己的写入")
d, live = fresh("gate")
snap = snapshot(d, live)
sa.apply_snapshot(str(snap), str(live), None)           # 先对齐一次
# 模拟"有人在服务器上办了件事"：多出一条审计
x(live, "insert into audit_log (actor,agent,action,input_digest,output_digest,created_at) "
        "values ('x','x','done-on-server','','','2026-10-23 13:03:00')")
before = counts(live)["audit_log"]
rc = sa.apply_snapshot(str(snap), str(live), None, require_request=True)
check("没有 .apply 标记 → 一个字都不动", rc == 0 and counts(live)["audit_log"] == before,
      f"{before} -> {counts(live)['audit_log']}")
check("日志写明跳过的原因",
      "无显式推送请求" in (d / "sync.log").read_text(encoding="utf-8"))

# 有标记（sha 与已应用过的相同）→ 必须照做，因为服务器可能被写过
sha = sa.sha256_file(str(snap))
(d / "snap.db.apply").write_text(sha + "\n", encoding="utf-8")
rc = sa.apply_snapshot(str(snap), str(live), None, require_request=True)
check("有标记 → 即便 sha 相同也覆盖", rc == 0 and counts(live)["audit_log"] == before - 1,
      f"{before} -> {counts(live)['audit_log']}")
check("被覆盖的服务器端审计先存档",
      any((d / f).name.startswith("audit-orphan") for f in os.listdir(d)))
check("标记用掉即销，不会反复应用", not (d / "snap.db.apply").exists())

# 标记里的 sha 与快照不符（还在上传）→ 不许动
(d / "snap.db.apply").write_text("0" * 64 + "\n", encoding="utf-8")
x(live, "insert into audit_log (actor,agent,action,input_digest,output_digest,created_at) "
        "values ('y','y','keep-me','','','2026-10-23 13:04:00')")
before2 = counts(live)["audit_log"]
rc = sa.apply_snapshot(str(snap), str(live), None, require_request=True)
check("标记与快照不符 → 跳过，不动目标库",
      rc == 0 and counts(live)["audit_log"] == before2)
check("不符时也写了原因",
      "请求的 sha 与快照不一致" in (d / "sync.log").read_text(encoding="utf-8"))

print("\n" + "=" * 66)
print(f"通过 {PASS} 项 / 失败 {FAIL} 项")
if FAILED:
    print("失败项：")
    for n in FAILED:
        print("  -", n)
print("=" * 66)

shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if FAIL else 0)
