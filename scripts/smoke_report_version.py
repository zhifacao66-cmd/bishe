"""回归：报表版本历史与回滚 + 审计日志游标分页

这两件事单独看都不难，难在它们和已有约定**打架**，所以必须用测试把边界钉住：

  报表这条——
  1. 「一期一份」要求 `report` 表里同月只能一行，而「能回滚」要求旧内容还在，
     听着矛盾。解是把被替掉的每一版整份挪进 `report_revision`：
     一行一期不变，历史一版不少。
  2. **覆盖前必须先归档**。顺序反了旧内容就物理消失，而且不报错。
     测法：重新生成之后，历史快照的 data_json 必须和覆盖前那份**逐字节相同**。
  3. **回滚不重号**。回滚是"用旧内容生成新一版"，版本号继续往上走，
     `rollback_from` 记着抄的是第几版。一旦允许版本号倒退，
     "2026-10 第 2 版"这个归档编号就会同时指向两份不同内容。
  4. 回滚前要把当前版也归档，否则回滚本身成了一次不可逆的覆盖。
  5. 回滚是写操作：街道能看历史（report.view），不能回滚（report.generate）。

  审计这条——
  6. 用游标不用 offset：审计是只追加表，翻页期间的写入会让 offset 分页
     看到重复、漏掉记录。测法：从头翻到尾，把 id 收成集合，必须既不重也不漏。
  7. 游标必须带 id。同一次批量写入的几条时间戳可能完全相同（秒级精度），
     只比时间会在同一秒的边界上漏记录——专门造三条同秒记录来验。
  8. 坏游标退回首屏，不报错：它只是个位置，不该让人卡在翻不动的页面上。

用独立库，不碰 data/community.db。
"""
import os
import pathlib
import sys
import urllib.parse
from datetime import datetime, timedelta

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_report_version.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"    # 报表不需要模型
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                                # noqa: E402
from app.extensions import db                             # noqa: E402
from app.models import AuditLog, Report, ReportRevision, WorkItem, WorkOrder  # noqa: E402

app = create_app("dev")

PERIOD = "2026-10"


def in_period(day, hour=9):
    return datetime(2026, 10, day, hour, 0, 0)


# ---------------------------------------------------------------- 造数据
with app.app_context():
    for i in range(1, 7):
        db.session.add(WorkItem(
            id=i, type="报修" if i % 3 else "走访", subject=f"事项 {i}",
            target_lines="物业" if i % 2 else "综治", status="done",
            created_at=in_period(i)))
    for i in range(1, 4):
        db.session.add(WorkOrder(
            id=i, order_no=f"WO-RV-{i:03d}", work_item_id=i,
            property_co="幸福里物业", content=f"楼道问题 {i}",
            sla_level="一般", sla_hours=72, status="done" if i < 3 else "open",
            created_at=in_period(i)))
    db.session.commit()

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
        print(f"  ✗ {name}   ← {detail}")


def login(c, u="wang", p="123456"):
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


def J(r):
    return r.get_json(silent=True) or {}


def msg(j):
    d = j.get("data")
    if isinstance(d, dict):
        return d.get("msg") or j.get("msg") or ""
    return j.get("msg") or ""


comm = app.test_client()
street = app.test_client()
prop = app.test_client()
anon = app.test_client()
login(comm, "wang", "123456")
login(street, "jiedao", "123456")
login(prop, "wuye", "123456")


def gen(c=None):
    return J((c or comm).post("/api/reports/generate", json={"period": PERIOD}))


def rid_of(j):
    return (j.get("data") or {}).get("report_id")


def snaps(rid):
    """历史快照：version → data_json 字符串"""
    with app.app_context():
        return {r.version: r.data_json for r in
                ReportRevision.query.filter_by(report_id=rid).all()}


def row(rid):
    with app.app_context():
        return db.session.get(Report, rid)


# ================================================================ 1 首次生成
print("\n[1] 首次生成 = 第 1 版，没有历史")
j1 = gen()
check("首次生成成功", j1.get("code") == 0, msg(j1))
rid = rid_of(j1)
check("返回 version=1", (j1.get("data") or {}).get("version") == 1,
      (j1.get("data") or {}).get("version"))
check("首次生成不算重新生成", (j1.get("data") or {}).get("regenerated") is False)
check("rollback_from 为空", (j1.get("data") or {}).get("rollback_from") is None)
with app.app_context():
    same = Report.query.filter_by(period=PERIOD, line="街道").count()
check("report 表里该月只有 1 行", same == 1, same)
check("还没有任何历史快照", snaps(rid) == {}, snaps(rid))
v = J(comm.get(f"/api/reports/{rid}/versions")).get("data") or []
check("版本列表只有 1 条", len(v) == 1, len(v))
check("那一条是当前版", v and v[0]["current"] is True, v)

snap_v1 = (j1.get("data") or {}).get("summary")
d1 = J(comm.get(f"/api/reports/{rid}")).get("data") or {}
# 覆盖前把库里的原始 data_json 抄下来，用来验证"归档的就是那一份"
with app.app_context():
    raw_v1 = db.session.get(Report, rid).data_json

# ================================================================ 2 重新生成
print("\n[2] 重新生成 = 覆盖升版，旧版整份归档")
j2 = gen()
check("重新生成成功", j2.get("code") == 0, msg(j2))
check("版本号升到 2", (j2.get("data") or {}).get("version") == 2)
check("regenerated 标记为真", (j2.get("data") or {}).get("regenerated") is True)
check("报表 id 不变（同一份归档件）", rid_of(j2) == rid, f"{rid_of(j2)} vs {rid}")
with app.app_context():
    same = Report.query.filter_by(period=PERIOD, line="街道").count()
check("行数仍是 1（一期一份没破）", same == 1, same)

hist = snaps(rid)
check("旧版进了历史表", 1 in hist, list(hist))
check("历史快照与覆盖前那份逐字节相同", hist.get(1) == raw_v1,
      "顺序反了就会静默丢数据（旧内容已物理消失）")
with app.app_context():
    rv1 = ReportRevision.query.filter_by(report_id=rid, version=1).first()
    rv1_reason = rv1.reason if rv1 else None
    rv1_actor = rv1.actor if rv1 else None
check("归档原因记为重新生成", rv1_reason == "regenerate", rv1_reason)
check("归档记下了操作人", rv1_actor == "王社工", rv1_actor)

v = J(comm.get(f"/api/reports/{rid}/versions")).get("data") or []
check("版本列表 2 条", len(v) == 2, len(v))
check("按版本号降序，当前版在最前",
      [x["version"] for x in v] == [2, 1], [x["version"] for x in v])
check("只有一条 current", sum(1 for x in v if x["current"]) == 1)
check("旧版带归档原因（重新生成）",
      [x for x in v if x["version"] == 1][0]["reason"] == "重新生成",
      [x for x in v if x["version"] == 1][0]["reason"])

# ================================================================ 3 回滚
print("\n[3] 回滚：抄旧内容生成新一版，版本号不倒退")
snap_v2 = J(comm.get(f"/api/reports/{rid}")).get("data") or {}
r = J(comm.post(f"/api/reports/{rid}/rollback", json={"version": 1}))
check("回滚成功", r.get("code") == 0, msg(r))
d3 = r.get("data") or {}
check("回滚后版本号继续往上（3），不改回 1", d3.get("version") == 3, d3.get("version"))
check("rollback_from 记为 1", d3.get("rollback_from") == 1, d3.get("rollback_from"))
check("回滚后摘要与第 1 版一致", d3.get("summary") == snap_v1,
      f"现在={str(d3.get('summary'))[:40]}")
check("回滚后业务项数与第 1 版一致",
      d3.get("item_count") == d1.get("item_count"),
      f'{d3.get("item_count")} vs {d1.get("item_count")}')
with app.app_context():
    same = Report.query.filter_by(period=PERIOD, line="街道").count()
    db_row = db.session.get(Report, rid)
    rv2 = ReportRevision.query.filter_by(report_id=rid, version=2).first()
check("回滚不新增行（还是 1 行）", same == 1, same)
check("回滚时当前版（第 2 版）已归档", rv2 is not None)
check("那个归档的原因写的是回滚前留档", rv2 and rv2.reason == "rollback",
      rv2.reason if rv2 else None)
check("库里的 version 也同步成 3", db_row.version == 3, db_row.version)

v = J(comm.get(f"/api/reports/{rid}/versions")).get("data") or []
vers = [x["version"] for x in v]
check("版本号不重号且递增", vers == sorted(set(vers), reverse=True), vers)
check("历史里有 1 和 2", {1, 2}.issubset(set(vers)), vers)
check("当前版单独标记，且是 3",
      [x["version"] for x in v if x["current"]] == [3], vers)
check("当前版标着回滚自第 1 版",
      [x for x in v if x["current"]][0]["rollback_from"] == 1)

# 导出仍然只读快照：回滚后导出的是旧版数字
xl = comm.get(f"/reports/{rid}/export/xlsx")
check("回滚后导出仍可下载", xl.status_code == 200, xl.status_code)
check("导出件是 xlsx", "spreadsheetml" in (xl.headers.get("Content-Type") or ""),
      xl.headers.get("Content-Type"))

# ================================================================ 4 回滚的边界
print("\n[4] 回滚的边界：目标版本与权限")
r = J(comm.post(f"/api/reports/{rid}/rollback", json={"version": 3}))
check("回滚到当前版本被拒", r.get("code") == 1, msg(r))
check("提示说明当前就是这一版", "当前版本" in msg(r), msg(r))

r = J(comm.post(f"/api/reports/{rid}/rollback", json={"version": 99}))
check("回滚到不存在的版本被拒", r.get("code") == 1, msg(r))
check("拒绝时列出可回滚的版本", "可回滚" in msg(r), msg(r))

r = J(comm.post(f"/api/reports/{rid}/rollback", json={"version": "第二版"}))
check("版本号不是整数被拒", r.get("code") == 1, msg(r))
check("提示要整数", "整数" in msg(r), msg(r))

r = J(comm.post(f"/api/reports/{rid}/rollback", json={}))
check("不传 version 时默认回上一版", r.get("code") == 0, msg(r))
check("默认目标是第 2 版", (r.get("data") or {}).get("rollback_from") == 2,
      (r.get("data") or {}).get("rollback_from"))
check("版本号仍是往上走（4）", (r.get("data") or {}).get("version") == 4,
      (r.get("data") or {}).get("version"))

print("\n[4b] 谁能看、谁能改")
r = street.get(f"/api/reports/{rid}/versions")
check("街道能看版本历史", r.status_code == 200 and J(r).get("code") == 0, r.status_code)
r = street.post(f"/api/reports/{rid}/rollback", json={"version": 1},
                headers={"Sec-Fetch-Site": "same-origin"})
check("街道不能回滚（403）", r.status_code == 403, r.status_code)
with app.app_context():
    deny = AuditLog.query.filter_by(action="deny", agent="auth").count()
check("越权尝试留了 deny 审计", deny >= 1, deny)

r = prop.get(f"/api/reports/{rid}/versions")
check("物业看不了报表历史（403）", r.status_code == 403, r.status_code)
r = anon.get(f"/api/reports/{rid}/versions")
check("未登录 401", r.status_code == 401, r.status_code)

r = comm.get("/api/reports/999999/versions")
check("不存在的报表 404", r.status_code == 404, r.status_code)

# 列表接口带上回滚标记
lst = J(comm.get("/api/reports")).get("data") or []
tgt = [x for x in lst if x["id"] == rid]
check("列表带 rollback_from", tgt and tgt[0].get("rollback_from") == 2,
      tgt[0] if tgt else None)

# ================================================================ 5 审计游标分页
print("\n[5] 审计日志游标分页：不重、不漏、坏游标不报错")
# 造 240 条不同时间的记录，用来翻页
with app.app_context():
    base_t = datetime(2026, 9, 1, 0, 0, 0)
    for i in range(240):
        db.session.add(AuditLog(
            actor="tester", agent="parser" if i % 2 else "fill", action="test_page",
            input_digest=f"输入 {i}", output_digest=f"输出 {i}",
            created_at=base_t + timedelta(minutes=i)))
    # 三条同一秒（批量写入的真实形态）——只比时间会在这一秒边界漏记录
    same = datetime(2026, 9, 2, 3, 0, 0)
    for k in range(3):
        db.session.add(AuditLog(actor="tester", agent="parser", action="test_same_sec",
                                input_digest=f"同秒 {k}", created_at=same))
    db.session.commit()


def walk(client, query, page_size=50, cap=100):
    """按游标翻到底，返回 (所有行, 翻了几页)"""
    out, pages, cursor = [], 0, ""
    while True:
        q = f"{query}{'&' if '?' in query else '?'}limit={page_size}"
        if cursor:
            q += "&cursor=" + urllib.parse.quote(cursor)
        j = J(client.get(q))
        if j.get("code") != 0:
            return out, -1
        d = j.get("data") or {}
        out += d.get("items") or []
        cursor = (d.get("counts") or {}).get("next_cursor") or ""
        pages += 1
        if not cursor or pages > cap:
            return out, pages


first = J(street.get("/api/audit?limit=50")).get("data") or {}
c0 = first.get("counts") or {}
check("首屏 50 条", len(first.get("items") or []) == 50, len(first.get("items") or []))
check("首屏标了还有更多", c0.get("has_more") is True)
check("首屏给了 next_cursor", bool(c0.get("next_cursor")))

with app.app_context():
    db_total = AuditLog.query.count()
check("total 就是库里的行数", c0.get("total") == db_total, f'{c0.get("total")} vs {db_total}')

rows, pages = walk(street, "/api/audit")
ids = [x["id"] for x in rows]
check("翻到底拿到全部记录", len(rows) == db_total, f"{len(rows)} vs {db_total}")
check("没有重复记录", len(ids) == len(set(ids)), f"{len(ids)} 去重后 {len(set(ids))}")
with app.app_context():
    all_ids = {x.id for x in AuditLog.query.all()}
check("id 集合与库里完全一致", set(ids) == all_ids,
      f"缺 {len(all_ids - set(ids))} 条，多 {len(set(ids) - all_ids)} 条")
times = [x["created_at"] for x in rows]
check("全局按时间倒序", times == sorted(times, reverse=True))
check("翻页次数 = 向上取整", pages == -(-db_total // 50), f"{pages} 页")

last = J(street.get("/api/audit?limit=500")).get("data") or {}
cl = last.get("counts") or {}
check("一次取够就没下一页", cl.get("has_more") is False)
check("取够时 next_cursor 为空", not cl.get("next_cursor"), cl.get("next_cursor"))

print("\n[5b] 同秒边界（游标必须带 id）")
same_rows, _ = walk(street, "/api/audit?action=test_same_sec", page_size=1)
check("同秒 3 条一条不少", len(same_rows) == 3, len(same_rows))
check("同秒 3 条不重复", len({x["id"] for x in same_rows}) == 3, len(same_rows))

print("\n[5c] 筛选 + 游标")
with app.app_context():
    sec_n = AuditLog.query.filter_by(agent="security").count()
f1 = J(street.get("/api/audit?agent=security&limit=5")).get("data") or {}
check("筛选后 matched 等于该 Agent 条数",
      (f1.get("counts") or {}).get("matched") == sec_n,
      f'{(f1.get("counts") or {}).get("matched")} vs {sec_n}')
check("筛选后 total 仍是全量（家底不随筛选变）",
      (f1.get("counts") or {}).get("total") == db_total,
      (f1.get("counts") or {}).get("total"))
sec_rows, _ = walk(street, "/api/audit?agent=security", page_size=5)
check("筛选后翻页也不重不漏",
      len(sec_rows) == sec_n and len({x["id"] for x in sec_rows}) == sec_n,
      f"{len(sec_rows)} vs {sec_n}")
check("筛选后每行 Agent 都对",
      all(x["agent"] == "security" for x in sec_rows))
check("按 action 也能筛",
      len((J(street.get("/api/audit?action=deny")).get("data") or {}).get("items") or []) >= 1)

print("\n[5d] 参数边界")
r = J(street.get("/api/audit?cursor=这不是游标"))
check("坏游标不报错", r.get("code") == 0, msg(r))
check("坏游标退回首屏",
      len((r.get("data") or {}).get("items") or []) == 100,
      len((r.get("data") or {}).get("items") or []))
r = J(street.get("/api/audit?cursor=abc"))
check("半截 base64 也退回首屏", r.get("code") == 0, msg(r))
r = J(street.get("/api/audit?limit=0")).get("data") or {}
check("limit=0 回默认 100 条", len(r.get("items") or []) == 100, len(r.get("items") or []))
r = J(street.get("/api/audit?limit=-5")).get("data") or {}
check("limit 为负回默认", len(r.get("items") or []) == 100, len(r.get("items") or []))
r = J(street.get("/api/audit?limit=abc")).get("data") or {}
check("limit 非数字回默认", len(r.get("items") or []) == 100, len(r.get("items") or []))
r = J(street.get("/api/audit?limit=99999")).get("data") or {}
check("limit 超上限被夹到 500",
      len(r.get("items") or []) == min(500, db_total), len(r.get("items") or []))

r = J(comm.get("/api/audit"))
check("社区工作者没有 audit.view → 403", comm.get("/api/audit").status_code == 403,
      comm.get("/api/audit").status_code)

# ================================================================ 6 审计里能看到回滚
print("\n[6] 回滚在审计里留痕")
logs = J(street.get("/api/audit?agent=report&limit=50")).get("data") or {}
acts = [x["action"] for x in (logs.get("items") or [])]
check("审计里有 rollback_report", "rollback_report" in acts, acts[:6])
check("审计里有 generate/regenerate",
      {"generate_report", "regenerate_report"} & set(acts), acts[:6])
rb = [x for x in (logs.get("items") or []) if x["action"] == "rollback_report"]
check("回滚审计记的是人不是 system", rb and rb[0]["actor"] == "王社工",
      rb[0]["actor"] if rb else None)
check("回滚审计写明从哪版到哪版", rb and "回滚到第" in (rb[0]["input_digest"] or ""),
      rb[0]["input_digest"] if rb else None)

# ================================================================ 汇总
print("\n" + "=" * 60)
print(f"结果：通过 {PASS} / 失败 {FAIL}  |  共 {PASS + FAIL} 项")
if FAILED:
    print("失败项：")
    for x in FAILED:
        print("  -", x)
print("=" * 60)
sys.exit(1 if FAIL else 0)
