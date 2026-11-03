"""一次性清理：同一 (period, line) 的历史重复报表

背景：报表早先是每次生成就 insert 一条，演示库里同一个 2026-10 堆了十几份，
列表刷屏、导出件各带一个编号。改成"一期一份 + 重新生成升版本"之后，
老数据还要单独收拾一次——放在启动路径上做删除动作太危险，所以独立成脚本。

规则：同组保留 id 最小那条（对外链接最稳定），内容取该组最新一份，
version 记为原组内条数（= 历史生成过几次），其余删除。全程写审计。

用法：
  python scripts/_dedupe_reports.py            # 先看会动哪些，不写库
  python scripts/_dedupe_reports.py --apply    # 真删
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
from collections import defaultdict

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("DATABASE_URL", "sqlite:///" + str(BASE / "data" / "community.db"))

from app import create_app              # noqa: E402
from app.extensions import db           # noqa: E402
from app.models import AuditLog, Report  # noqa: E402

APPLY = "--apply" in sys.argv
app = create_app("dev")

with app.app_context():
    rows = Report.query.order_by(Report.id).all()
    groups: dict[tuple, list[Report]] = defaultdict(list)
    for r in rows:
        groups[(r.period, r.line)].append(r)

    plan = {k: v for k, v in groups.items() if len(v) > 1}
    if not plan:
        print("没有重复报表，无需清理。")
        raise SystemExit(0)

    total_del = 0
    for (period, line), grp in sorted(plan.items()):
        grp.sort(key=lambda r: (r.generated_at or r.created_at, r.id))
        keep, dupes = grp[0], grp[1:]
        newest = grp[-1]
        print(f"[{period} / {line}] 共 {len(grp)} 条 → 保留 id={keep.id}"
              f"（内容取 id={newest.id} 的最新快照），删除 {len(dupes)} 条："
              f"{[d.id for d in dupes]}")
        total_del += len(dupes)
        if APPLY:
            keep.data_json = newest.data_json
            keep.generated_at = newest.generated_at
            keep.version = len(grp)
            for d in dupes:
                db.session.delete(d)
            db.session.add(AuditLog(
                actor="system", agent="maintenance", action="dedupe_report",
                input_digest=f"{period}/{line} 保留 {keep.id}，删除 {len(dupes)} 条",
                output_digest=json.dumps({"version": len(grp)}, ensure_ascii=False),
            ))

    if APPLY:
        db.session.commit()
        print(f"\n已清理 {total_del} 条重复报表。")
    else:
        print(f"\n预演完成：将删除 {total_del} 条。加 --apply 才会真正写库。")
