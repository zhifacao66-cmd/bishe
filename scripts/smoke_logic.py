"""业务逻辑一致性回归（跨模块，不测单点功能）

这套测试锁的是"**同一个事实在两处算，结果必须一样**"这类问题。
它们不会让页面白屏、不会抛异常，只会让数字和状态悄悄说错话：

  A. 任务统计：明细有 limit(100)，卡片数字必须另算，不能数这一屏
  B. 任务写侧隔离：列表挡住了别家任务，按 id 办结也必须挡住
  C. 公告筛选叠加：`recv=dispatched` 是"再加一个条件"，不是"换一条查询"
  D. 督办告警：物业只该看到本公司工单，不该看到别家/别角色的任务
  E. 居民端状态：只走台账（民政/综治线）的事项必须能到"已办结"
  F. 置顶排序：内部与居民端必须按同一个键（pinned_at）排

每条都对应一个真实缺陷，修复前的表现写在断言旁边的注释里。
独立库，跑完不动演示库。

运行：python scripts/smoke_logic.py
"""
import pathlib
import shutil
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

SRC = BASE / "data" / "community.db"
DB = BASE / "data" / "smoke_logic.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()
if SRC.exists():
    for suf in ("", "-wal", "-shm"):
        s_ = pathlib.Path(str(SRC) + suf)
        if s_.exists():
            shutil.copy2(s_, str(DB) + suf)

import os                                        # noqa: E402

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from datetime import datetime, timedelta          # noqa: E402

from app import create_app                        # noqa: E402
from app.extensions import db                     # noqa: E402
from app.models import (AuditLog, FdeRecord, FieldSecurity, Ledger,  # noqa: E402
                        Notice, ResidentTicket, Task, WorkItem, WorkOrder,
                        WorkOrderLog)

app = create_app("dev")
PASS, FAIL = 0, 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {extra}")


def login(c, u, p="123456"):
    c.get("/logout")
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


def wipe():
    """按外键依赖顺序清空（有 FK，删父表前必须先删子表）"""
    for m in (WorkOrderLog, WorkOrder, Ledger, FieldSecurity, WorkItem,
              ResidentTicket, FdeRecord):
        db.session.query(m).delete()
    db.session.commit()


# ======================================================================
print("\n[A] 任务统计：不能数被 limit 截断的那一屏")
with app.app_context():
    db.session.query(Task).delete()
    now = datetime.now()
    for i in range(130):
        db.session.add(Task(title=f"统计探针{i:03d}", assignee_role="community",
                            assignee_org="幸福里社区",
                            deadline=now + timedelta(days=2)))
    # 3 条已超期
    for i in range(3):
        db.session.add(Task(title=f"超期{i}", assignee_role="community",
                            assignee_org="幸福里社区",
                            deadline=now - timedelta(hours=5)))
    db.session.commit()
    real_total = Task.query.count()
    real_overdue = Task.query.filter(Task.deadline < now,
                                     Task.status != "done").count()

with app.test_client() as c:
    login(c, "jiedao")
    d = c.get("/api/tasks").get_json()["data"]
    s = d["summary"]
    check("total 是库里的真实条数", s["total"] == real_total,
          f"{s['total']} vs {real_total}")
    check("明细仍按 limit 截断", len(d["rows"]) == 100, len(d["rows"]))
    check("truncated 标出明细不全", s.get("truncated") is True, s)
    check("超期数是全量而非这一屏", s["overdue"] == real_overdue,
          f"{s['overdue']} vs {real_overdue}")
    check("open + done == total", s["open"] + s["done"] == s["total"], s)

# ======================================================================
print("\n[B] 任务写侧隔离：列表看不到的，按 id 也不能办")
with app.app_context():
    db.session.query(Task).delete()
    t = Task(title="派给别的社区", assignee_role="community",
             assignee_org="别的社区", deadline=datetime.now() + timedelta(days=1))
    db.session.add(t)
    db.session.commit()
    tid_other = t.id
    t2 = Task(title="派给本社区", assignee_role="community",
              assignee_org="幸福里社区", deadline=datetime.now() + timedelta(days=1))
    db.session.add(t2)
    db.session.commit()
    tid_mine = t2.id
    deny_before = AuditLog.query.filter_by(action="deny").count()

with app.test_client() as c:
    login(c, "wang")
    rows = c.get("/api/tasks").get_json()["data"]["rows"]
    check("列表里没有别家任务", all(r["id"] != tid_other for r in rows))

    r = c.post(f"/api/tasks/{tid_other}/done")
    check("跨单位办结被拒", r.get_json()["code"] == 403, r.get_json())
    with app.app_context():
        check("被拒后状态没变", db.session.get(Task, tid_other).status == "open")
        deny_after = AuditLog.query.filter_by(action="deny").count()
        last = (AuditLog.query.filter_by(action="deny")
                .order_by(AuditLog.id.desc()).first())
    check("越权尝试写了审计", deny_after == deny_before + 1, f"{deny_before}->{deny_after}")
    check("审计指向该任务", last and f"任务 {tid_other}" in (last.input_digest or ""),
          last.input_digest if last else "")

    ok_r = c.post(f"/api/tasks/{tid_mine}/done")
    check("本单位任务可以办结", ok_r.get_json()["code"] == 0, ok_r.get_json())


with app.test_client() as c:
    login(c, "jiedao")
    r = c.post(f"/api/tasks/{tid_other}/done")
    check("街道仍可代为关闭", r.get_json()["code"] == 0, r.get_json())

# ======================================================================
print("\n[C] 公告筛选：recv=dispatched 只能是叠加条件")
with app.app_context():
    db.session.query(Notice).delete()
    db.session.add_all([
        Notice(title="社区内部件", issuer="幸福里社区", level="社区",
               scope="community", status="published", kind="superior",
               recv_status="dispatched"),
        Notice(title="公开件", issuer="街道", level="街道", scope="all",
               status="published", kind="superior", recv_status="dispatched"),
        Notice(title="待确认件", issuer="街道", level="街道", scope="all",
               status="pending", kind="superior", recv_status="dispatched"),
        Notice(title="已下架件", issuer="街道", level="街道", scope="all",
               status="offline", kind="superior", recv_status="dispatched"),
    ])
    db.session.commit()

with app.test_client() as c:
    login(c, "wuye")
    base = [x["title"] for x in c.get("/api/notices").get_json()["data"]["items"]]
    check("不加筛选：物业只看到已发布且面向自己的", base == ["公开件"], base)

    d = c.get("/api/notices?recv=dispatched").get_json()["data"]
    titles = [x["title"] for x in d["items"]]
    check("加待签收筛选后不含内部件", "社区内部件" not in titles, titles)
    check("加待签收筛选后不含未发布件",
          not any(t in ("待确认件", "已下架件") for t in titles), titles)

    d2 = c.get("/api/notices?status=pending&recv=dispatched").get_json()["data"]
    bad = [x for x in d2["items"] if x["status"] != "pending"]
    check("status 与 recv 组合同时生效", not bad, [x["title"] for x in bad])

    d3 = c.get("/api/notices?recv=dispatched").get_json()["data"]
    # 有分页之后不能再拿 len(items) 比：那只是"这一页有几条"。
    # 卡片说的"待签收 N"对应的是**当前筛选命中数**（matched），两者必须相等。
    check("待签收计数与列表同源", d3["counts"]["todo"] == d3["counts"]["matched"],
          f'卡片 {d3["counts"]["todo"]} vs 命中 {d3["counts"]["matched"]}')

# 社区的待办还要按收文单位过滤
with app.app_context():
    n = Notice(title="只给甲社区", issuer="街道", level="街道", scope="all",
               status="pending", kind="superior", recv_status="dispatched",
               receiver_org="甲社区")
    db.session.add(n)
    db.session.commit()
with app.test_client() as c:
    login(c, "wang")
    titles = [x["title"] for x in
              c.get("/api/notices?recv=dispatched").get_json()["data"]["items"]]
    check("指定收文单位的不进别家待办", "只给甲社区" not in titles, titles)

# ======================================================================
print("\n[D] 督办告警：范围必须跟调用者身份走")
with app.app_context():
    db.session.query(WorkOrderLog).delete()      # 有外键，先删流转记录
    db.session.query(WorkOrder).delete()
    now = datetime.now()
    for co in ("幸福里物业", "别家物业"):
        db.session.add(WorkOrder(order_no=f"W-LOGIC-{co}", property_co=co,
                                 content=f"{co}超时单", sla_hours=1, sla_level="紧急",
                                 status="open", created_at=now - timedelta(hours=6)))
    db.session.add(Task(title="社区的活", assignee_role="community",
                        assignee_org="幸福里社区",
                        deadline=now - timedelta(hours=2)))
    db.session.commit()

with app.test_client() as c:
    login(c, "wuye")
    alerts = c.get("/api/supervision/alerts").get_json()["data"]["alerts"]
    codes = [a.get("order_no") for a in alerts]
    check("物业只看到本公司超时单", codes == ["W-LOGIC-幸福里物业"], codes)
    check("物业看不到社区任务告警",
          not any(a["kind"] == "task_overdue" for a in alerts),
          [a["kind"] for a in alerts])

with app.test_client() as c:
    login(c, "jiedao")
    alerts = c.get("/api/supervision/alerts").get_json()["data"]["alerts"]
    check("街道看得到全部工单告警",
          len([a for a in alerts if a["kind"] == "order_overdue"]) == 2,
          len(alerts))
    check("街道看得到任务告警",
          any(a["kind"] == "task_overdue" for a in alerts))

# ======================================================================
print("\n[E] 居民端状态：台账归档算办结")
with app.app_context():
    wipe()
    rec = FdeRecord(raw_text="居民申请低保复核", source="portal", creator="居民张某")
    db.session.add(rec)
    db.session.flush()
    wi = WorkItem(fde_id=rec.id, type="咨询", subject="低保复核申请",
                  fields_json="{}", target_lines="民政", status="pending")
    db.session.add(wi)
    db.session.flush()
    db.session.add(Ledger(work_item_id=wi.id, line="民政",
                          content_json='{"type":"咨询","subject":"低保复核申请"}',
                          reused=False))
    t = ResidentTicket(code="654321", phone="13800138000", name="张",
                       raw_text="居民申请低保复核", fde_id=rec.id,
                       item_count=1, line_summary="民政", status="open")
    db.session.add(t)
    # 模拟建单写路径：状态由写接口同步一次，渲染函数不再 commit
    from app.itemstate import sync_ticket_status
    sync_ticket_status(rec.id)
    db.session.commit()
    tid = t.id
    wid = wi.id

with app.test_client() as c:
    d = c.get("/api/portal/ticket?phone=13800138000&code=654321").get_json()["data"]
    check("只走台账的事项显示已办结", d["items"][0]["state"] == "已办结",
          d["items"][0]["state"])
    check("整单状态同步为已办结", d["status"] == "done", d["status"])
    with app.app_context():
        check("写路径把状态落库", db.session.get(ResidentTicket, tid).status == "done")

# P0：GET 只读，不许再回写状态（预取/重试/爬虫不该产生写）
with app.app_context():
    t2 = db.session.get(ResidentTicket, tid)
    t2.status = "open"
    db.session.commit()
with app.test_client() as c:
    d = c.get("/api/portal/ticket?phone=13800138000&code=654321").get_json()["data"]
    check("GET 仍按事项实时计算", d["status"] == "done", d["status"])
    with app.app_context():
        check("GET 后库状态未被改写",
              db.session.get(ResidentTicket, tid).status == "open")

# 工单在处理中 → 处理中
with app.app_context():
    db.session.add(WorkOrder(order_no="W-LOGIC-T1", work_item_id=wid,
                             property_co="幸福里物业", content="楼道灯不亮",
                             sla_hours=72, status="doing"))
    db.session.commit()
with app.test_client() as c:
    d = c.get("/api/portal/ticket?phone=13800138000&code=654321").get_json()["data"]
    check("有工单在处理 → 处理中", d["items"][0]["state"] == "处理中",
          d["items"][0]["state"])
    check("整单算处理中", d["status"] == "doing", d["status"])

# 一项办结 + 一项未开始 → 整单不能倒退成"已受理"
with app.app_context():
    wi2 = WorkItem(fde_id=db.session.get(ResidentTicket, tid).fde_id,
                   type="报修", subject="门禁失效", fields_json="{}",
                   target_lines="物业", status="pending")
    db.session.add(wi2)
    db.session.commit()
with app.test_client() as c:
    d = c.get("/api/portal/ticket?phone=13800138000&code=654321").get_json()["data"]
    check("部分推进时整单不再显示已受理", d["status"] == "doing", d["status"])

# ======================================================================
print("\n[F] 置顶排序：内部与居民端同一个键")
with app.app_context():
    db.session.query(Notice).delete()
    t0 = datetime.now() - timedelta(days=10)
    db.session.add_all([
        Notice(title="先发后置顶", issuer="街道", level="街道", scope="all",
               status="published", pinned=True, pinned_at=datetime.now(),
               published_at=t0, body_md="x"),
        Notice(title="后发先置顶", issuer="街道", level="街道", scope="all",
               status="published", pinned=True,
               pinned_at=datetime.now() - timedelta(days=2),
               published_at=t0 + timedelta(days=5), body_md="x"),
        Notice(title="普通件", issuer="街道", level="街道", scope="all",
               status="published", pinned=False, published_at=t0 + timedelta(days=1),
               body_md="x"),
    ])
    db.session.commit()

with app.test_client() as c:
    login(c, "wang")
    inner = [x["title"] for x in c.get("/api/notices").get_json()["data"]["items"]]
    res = [x["title"] for x in c.get("/api/portal/notices").get_json()["data"]["items"]]
    check("内部：后置顶的排最前", inner[:2] == ["先发后置顶", "后发先置顶"], inner)
    check("居民端与内部顺序一致", res == inner, f"{res} vs {inner}")
    check("未置顶的排在置顶件之后", res[-1] == "普通件", res)

print("\n[G] 老库回填：置顶位有值但没记时间")
with app.app_context():
    legacy = Notice(title="早期置顶件", issuer="街道", level="街道", scope="all",
                    status="published", pinned=True, pinned_at=None,
                    published_at=datetime.now() - timedelta(days=30), body_md="x")
    db.session.add(legacy)
    db.session.commit()
    legacy_id = legacy.id

    # 迁移函数是幂等的：不只在"刚加列"那一次生效，否则早期只翻布尔值的行永远漂着
    from app.auth import _migrate_notice_table
    _migrate_notice_table()
    row = db.session.get(Notice, legacy_id)
    check("启动回填补上了 pinned_at", row.pinned_at is not None, row.pinned_at)
    check("回填值取发稿时间", row.pinned_at == row.published_at,
          f"{row.pinned_at} vs {row.published_at}")

    # 再跑一次不能改写已填好的值（幂等）
    before = row.pinned_at
    _migrate_notice_table()
    again = db.session.get(Notice, legacy_id).pinned_at
    check("重复回填不改已验证的值", again == before, f"{before} -> {again}")

print("\n[H] 管理员视角：收窄只对受派方，不能把 admin 也筛掉")
# 缺陷现场：tasks._scoped() 原写 `if u.role != "street"`，admin 被收窄成
# assignee_role == "admin" —— 一个**不存在的受派角色**。结果是管理员登录后
# 工单页一条都没有（物业反而看得到自己的 5 条），统计卡片全 0。
# 同一形态在 supervise.py 也有一份，表现为管理员打开督办永远"无告警"。
with app.app_context():
    db.session.query(Task).delete()
    now = datetime.now()
    db.session.add_all([
        Task(title="派给社区甲", assignee_role="community",
             assignee_org="幸福里社区", deadline=now + timedelta(days=1)),
        Task(title="派给社区乙", assignee_role="community",
             assignee_org="别的社区", deadline=now + timedelta(days=1)),
        Task(title="派给物业", assignee_role="property",
             assignee_org="幸福里物业", deadline=now + timedelta(days=1)),
        Task(title="已超期的社区件", assignee_role="community",
             assignee_org="别的社区", deadline=now - timedelta(hours=6)),
    ])
    db.session.commit()
    real_total = Task.query.count()

with app.test_client() as c:
    login(c, "admin", "admin123")
    d = c.get("/api/tasks").get_json()["data"]
    check("admin 看得到任务（不是空列表）", len(d["rows"]) > 0, len(d["rows"]))
    check("admin 的 total 等于全库条数", d["summary"]["total"] == real_total,
          f"{d['summary']['total']} vs {real_total}")
    check("admin 跨角色跨单位都能看到",
          {r["assignee_role"] for r in d["rows"]} == {"community", "property"},
          {r["assignee_role"] for r in d["rows"]})

    # 督办告警同一口径：admin 必须扫全量，别的社区的超期也要报
    a = c.get("/api/supervision/alerts").get_json()["data"]["alerts"]
    check("admin 督办扫到全局超时任务",
          any(x.get("title") == "已超期的社区件" for x in a), a[:2])

with app.test_client() as c:
    # 演示账号：wang=社区，wuye=物业（同一个前缀容易记混，标签写角色不写账号）
    login(c, "wuye")
    w = c.get("/api/tasks").get_json()["data"]
    check("物业只看派给物业的任务",
          w["rows"] and all(r["assignee_role"] == "property" for r in w["rows"]),
          [r["assignee_role"] for r in w["rows"]])
    check("物业的 total 小于全库", w["summary"]["total"] < real_total,
          f"{w['summary']['total']} vs {real_total}")

with app.test_client() as c:
    login(c, "wang")
    cm = c.get("/api/tasks").get_json()["data"]
    check("社区只看派给社区的任务",
          cm["rows"] and all(r["assignee_role"] == "community" for r in cm["rows"]),
          [r["assignee_role"] for r in cm["rows"]])
    # 跨单位：幸福里社区不能看到"别的社区"的件（含那条超期的）
    titles = {r["title"] for r in cm["rows"]}
    check("社区看不到别家社区的件", "派给社区乙" not in titles, titles)
    check("社区看得到本单位的件", "派给社区甲" in titles, titles)

with app.test_client() as c:
    login(c, "jiedao")
    j = c.get("/api/tasks").get_json()["data"]
    check("街道仍看全量", j["summary"]["total"] == real_total,
          f"{j['summary']['total']} vs {real_total}")

# admin 的 org 是"平台运维"，不属于任何社区。凡是"按 org 比收文单位"的地方
# 都必须把它排除，否则管理员看到"没有待签"，而文件其实躺着没人签。
with app.app_context():
    db.session.query(Notice).delete()
    db.session.add(Notice(
        title="待签收的上级来文", issuer="街道", level="街道", scope="all",
        kind="superior", status="published", recv_status="dispatched",
        receiver_org="幸福里社区", body_md="x"))
    db.session.commit()

with app.test_client() as c:
    login(c, "admin", "admin123")
    cnt = c.get("/api/notices").get_json()["data"]["counts"]
    check("admin 的待签数不为 0（不被自己的 org 过滤掉）",
          cnt.get("todo", 0) >= 1, cnt)
    login(c, "wang")
    cnt2 = c.get("/api/notices").get_json()["data"]["counts"]
    check("该社区自己的待签仍是 1", cnt2.get("todo") == 1, cnt2)

print()
print("=" * 66)
print(f"结果：PASS {PASS} / {PASS + FAIL}"
      + ("  ✅ 全部通过" if FAIL == 0 else f"  ❌ {FAIL} 项失败"))
print("=" * 66)
sys.exit(0 if FAIL == 0 else 1)
