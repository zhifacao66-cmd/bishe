# -*- coding: utf-8 -*-
"""一次性修复：把 audit_wiring 探针误写进演示库的行删掉、工单字段还原。

背景：`config.py` 在导入时就锁定了 DATABASE_URL，探针在设环境变量之前就 import 了
app，于是打到 data/community.db 上。删除的行与还原值都来自 13:57 那一次运行，
不是猜的：audit 517-523 / work_order_log 121-123 / task 5 / ledger 61。

先 sqlite3.backup() 快照到 data/backups/，再在单事务里改。dry-run 默认。
用法：python scripts/_fix_wiring_pollution.py [--apply]
"""
import os
import sqlite3
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "community.db")
TODAY = "2026-10-26 13:57:"

STEPS = [
    ("删审计（探针登录+接单/转派/办结/建任务）",
     "delete from audit_log where id between 517 and 523"),
    ("删工单流转（1 号单，探针产生）",
     "delete from work_order_log where id between 121 and 123"),
    ("删任务（连通性体检任务）",
     "delete from task where title='连通性体检任务'"),
    ("删台账（办结时 LedgerAgent 补的）",
     "delete from ledger where id=61"),
    ("还原 1 号工单六个字段",
     "update work_order set status='open', assignee=null, assignee_id=null, "
     "assigned_at=null, done_at=null, feedback=null where id=1"),
]


def main():
    apply = "--apply" in sys.argv
    con = sqlite3.connect(DB)
    con.execute("pragma foreign_keys=off")
    before = {t: con.execute("select count(*) from %s" % t).fetchone()[0]
              for t in ("audit_log", "work_order_log", "task", "ledger", "work_order")}
    print("修复前：%s" % before)
    row = con.execute("select status, assignee, done_at, feedback from work_order where id=1").fetchone()
    print("1 号工单当前：%s" % (row,))

    if not apply:
        print("\n预演（加 --apply 才写）：")
        for label, sql in STEPS:
            print("  · %s" % label)
            print("    %s" % sql)
        con.close()
        return 0

    bak = os.path.join(BASE, "data", "backups",
                       "community-before-wiring-fix-%s.db" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(os.path.dirname(bak), exist_ok=True)
    dst = sqlite3.connect(bak)
    with dst:
        con.backup(dst)          # 必须用 backup()，直接拷 .db 会丢 WAL 里的事务
    dst.close()
    print("已备份 → %s" % os.path.basename(bak))

    with con:
        for label, sql in STEPS:
            cur = con.execute(sql)
            print("  %-40s 影响 %d 行" % (label, cur.rowcount))
    after = {t: con.execute("select count(*) from %s" % t).fetchone()[0]
             for t in ("audit_log", "work_order_log", "task", "ledger", "work_order")}
    print("修复后：%s" % after)
    print("1 号工单：%s" % (con.execute(
        "select status, assignee, done_at, feedback from work_order where id=1").fetchone(),))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
