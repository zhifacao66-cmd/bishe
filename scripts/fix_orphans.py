"""清理孤儿行（只处理"指向已不存在对象"的记录）。

来历：手工清理测试数据时按依赖顺序删了 order → ledger → work_item → fde，
唯独漏了 `field_security`。它没有页面、没人点开看，删别的表时也不会报错，
于是 15 条密级行留了下来 —— 而首页"敏感字段管控"卡片把它们算进了总数，
数字比明细多，对不上。

这类行的共同特征：本表还在，指向的那一头已经没了。生产代码不删业务项
（没有删除接口），所以孤儿只可能来自脚本/手工操作；但既然能出现一次，
就可能出现第二次，所以做成可重复执行的工具，并在 `check_integrity` 里长期盯着。

用法：
    python scripts/fix_orphans.py            # 只报告（默认，不动库）
    python scripts/fix_orphans.py --apply    # 真删
    python scripts/fix_orphans.py --db data/xxx.db --apply
"""
import argparse
import os
import pathlib
import sqlite3
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent

ap = argparse.ArgumentParser()
ap.add_argument("--db", default=str(BASE / "data" / "community.db"))
ap.add_argument("--apply", action="store_true", help="真删（默认只报告）")
args = ap.parse_args()

DB = pathlib.Path(args.db)
if not DB.exists():
    print(f"库不存在：{DB}")
    sys.exit(2)

# (子表, 外键列, 父表) —— 每条都对应一次真实的"删了父、忘了子"
CHECKS = [
    ("field_security", "work_item_id", "work_item"),
    ("work_order", "work_item_id", "work_item"),
    ("ledger", "work_item_id", "work_item"),
    ("work_item", "fde_id", "fde_record"),
    ("resident_ticket", "fde_id", "fde_record"),
    # 工单流转记录故意保留：工单删了，历史仍要能查（docs/18）
]

con = sqlite3.connect(str(DB))
con.execute("PRAGMA foreign_keys=OFF")     # 手工删，先不做级联
print(f"库：{DB}")
print("模式：" + ("真删" if args.apply else "只报告"))

total = 0
for child, col, parent in CHECKS:
    try:
        rows = con.execute(
            f"SELECT {child}.id FROM {child} "
            f"LEFT JOIN {parent} ON {parent}.id = {child}.{col} "
            f"WHERE {child}.{col} IS NOT NULL AND {parent}.id IS NULL").fetchall()
    except sqlite3.OperationalError as e:
        print(f"  [SKIP] {child}.{col} → {parent}：{e}")
        continue
    ids = [r[0] for r in rows]
    if not ids:
        print(f"  [OK  ] {child}.{col} → {parent}")
        continue
    total += len(ids)
    print(f"  [孤儿] {child}.{col} → {parent}：{len(ids)} 条，id={ids[:12]}"
          + (" …" if len(ids) > 12 else ""))
    if args.apply:
        # 分批删，避免 SQL 变量上限（SQLite 默认 999）
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            con.execute(f"DELETE FROM {child} WHERE id IN ({','.join('?' * len(chunk))})",
                        chunk)
        con.commit()
        print(f"        已删除 {len(ids)} 条")

print()
if not total:
    print("没有孤儿行 ✅")
elif not args.apply:
    print(f"共 {total} 条孤儿。确认无误后加 --apply 真删。")
else:
    print(f"共清理 {total} 条孤儿。建议接着跑 python scripts/check_integrity.py")
con.close()
