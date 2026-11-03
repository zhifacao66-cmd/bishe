"""回归：整体严谨性（入参校验 / 状态幂等 / 一期一份 / 接口契约 / 搜索分页）

这轮修的问题有一个共同点：**都不是"功能不会用"，而是"用了会得到错的结果"**。
所以断言都指向"错的那个结果不会发生"，而不是"按钮点得动"：

  1. 报表一期一份：同月连点两次生成，只能是同一条记录、版本 +1，
     而不是列表里多出一份同月报表（归档件必须一期一份才有证据效力）；
  2. 重复发布不能改写 published_at：发稿时间是证据，误触一次就把老文件
     顶到最前面，居民以为刚发的；
  3. 首录入参：传数字/超长/来源非法必须明确拒绝，不能 500；
  4. 任务受派单位必须真实存在：填错一个字的任务会变成没人认领的孤儿；
  5. 接口层：不存在的 /api 路径回 JSON 而不是 HTML 404（前端 res.json() 会炸）；
  6. 跨站写请求被拒（Sec-Fetch-Site: cross-site）；
  7. 搜索/筛选只改 matched，不改"家底"total；分页翻页不重不漏。

用独立库，不碰 data/community.db。
"""
import json
import os
import pathlib
import sys
from datetime import date, datetime, timedelta

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_rigor.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"   # 规则路径，快且不依赖 GPU
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                                    # noqa: E402
from app.extensions import db                                  # noqa: E402
from app.models import (Ledger, Notice, Report,                # noqa: E402
                        WorkItem, WorkOrder)

app = create_app("dev")

# ---------------------------------------------------------------- 造数据
with app.app_context():
    for i, kw in enumerate(["厨房水管漏水", "楼道电路跳闸", "单元门禁损坏"], start=1):
        wi = WorkItem(id=i, type="报修", subject=kw, target_lines="物业",
                      status="done")
        db.session.add(wi)
        db.session.add(WorkOrder(
            id=i, order_no=f"WO-TEST-{i:03d}", work_item_id=i,
            property_co="幸福里物业", content=f"{i}号楼 {kw}，请尽快处理",
            sla_level="一般", sla_hours=72, status="open",
            created_at=datetime.now() - timedelta(days=i)))
    for i in range(60):
        db.session.add(Ledger(
            line=["综治", "民政", "卫健", "物业"][i % 4],
            content_json=json.dumps({"type": "走访", "subject": f"第{i}号走访记录 居民{i%7}户",
                                     "room_key": f"{i%9+1}-{i%6+1}0{i%4+1}"},
                                    ensure_ascii=False),
            reused=(i % 5 == 0),
            created_at=datetime.now() - timedelta(hours=i)))
    db.session.add(Notice(id=1, title="关于开展冬季供暖设施检修的通知",
                          issuer="幸福里街道办事处", level="街道", kind="superior",
                          status="published", issued_at=date.today(),
                          published_at=datetime(2026, 9, 1, 9, 0),
                          body_md="一、检修范围\n辖区所有集中供暖小区。",
                          source_file="测试用"))
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


print("=" * 70)
print("严谨性回归 · 入参 · 幂等 · 一期一份 · 接口契约 · 搜索分页")
print("=" * 70)

admin = app.test_client(); login(admin, "admin", "admin123")
comm = app.test_client(); login(comm, "wang")
prop = app.test_client(); login(prop, "wuye")
street = app.test_client(); login(street, "jiedao")

# ---------------------------------------------------------------- 1 一期一份
print("\n[1] 报表一期一份（同月再生成 = 覆盖升版，不新增记录）")
period = "2026-10"
r1 = J(comm.post("/api/reports/generate", json={"period": period}))
r2 = J(comm.post("/api/reports/generate", json={"period": period}))
check("首次生成成功", r1.get("code") == 0, r1.get("msg"))
check("第二次也成功", r2.get("code") == 0, r2.get("msg"))
check("两次返回同一个 report_id",
      r1.get("data", {}).get("report_id") == r2.get("data", {}).get("report_id"),
      f"{r1.get('data', {}).get('report_id')} vs {r2.get('data', {}).get('report_id')}")
check("第二次标记为重新生成", r2.get("data", {}).get("regenerated") is True)
check("版本号递增到 2", r2.get("data", {}).get("version") == 2,
      r2.get("data", {}).get("version"))
with app.app_context():
    same = Report.query.filter_by(period=period, line="街道").count()
    rep = Report.query.filter_by(period=period).first()
check("库里该月只有一条报表", same == 1, f"实际 {same} 条")
check("覆盖后版本落库为 2", rep and rep.version == 2, rep.version if rep else None)

lst = J(comm.get("/api/reports")).get("data") or []
mine = [x for x in lst if x["period"] == period]
check("报表列表里该月只出现一次", len(mine) == 1, f"{len(mine)} 条")
check("列表带版本号", mine and mine[0].get("version") == 2)
check("列表按月倒序（新月份在前）",
      [x["period"] for x in lst] == sorted([x["period"] for x in lst], reverse=True))

# ---------------------------------------------------------------- 2 发布幂等
print("\n[2] 公告重复发布 / 重复下架：幂等且不改证据")
before = J(comm.get("/api/notices/1")).get("data") or {}
rep = J(comm.post("/api/notices/1/publish"))
after = J(comm.get("/api/notices/1")).get("data") or {}
check("重复发布返回成功但明说已发过",
      rep.get("code") == 0 and "已于" in str((rep.get("data") or {}).get("msg")),
      rep)
with app.app_context():
    n = db.session.get(Notice, 1)
    pub_at = n.published_at
check("published_at 未被改写", pub_at == datetime(2026, 9, 1, 9, 0), pub_at)
r = J(comm.post("/api/notices/1/offline"))
check("首次下架成功",
      r.get("code") == 0 and (r.get("data") or {}).get("msg") == "已下架", r)
with app.app_context():
    n = db.session.get(Notice, 1)
    off_status = n.status
r = J(comm.post("/api/notices/1/offline"))
check("重复下架不报错且明说已下架",
      r.get("code") == 0 and "已处于下架" in str((r.get("data") or {}).get("msg")), r)
check("状态确实是下架", off_status == "offline")

# ---------------------------------------------------------------- 3 首录入参
print("\n[3] 首录入参：明确拒绝，不许 500")
cases = [
    ("缺字段", {}, 0),
    ("空字符串", {"raw_text": "   "}, 0),
    ("数字", {"raw_text": 12345}, 0),
    ("对象", {"raw_text": {"a": 1}}, 0),
    ("太短", {"raw_text": "灯坏"}, 0),
    ("超长 3000 字", {"raw_text": "楼道灯坏了" * 600}, 0),
    ("来源非法", {"raw_text": "3号楼2单元301室厨房水管漏水", "source": "hacker"}, 0),
]
for name, body, _ in cases:
    r = comm.post("/api/fde", json=body)
    j = J(r)
    check(f"{name} → 明确拒绝（HTTP {r.status_code}）",
          r.status_code == 200 and j.get("code") == 1, f"HTTP {r.status_code} {j}")
r = comm.post("/api/fde", json={"raw_text": "3号楼2单元301室厨房水管漏水，已流到楼下"})
j = J(r)
check("正常首录成功", r.status_code == 200 and j.get("code") == 0, j.get("msg"))

# ---------------------------------------------------------------- 4 任务受派单位
print("\n[4] 任务受派单位必须真实存在")
base = {"title": "核查独居老人供暖", "assignee_role": "community",
        "deadline": (date.today() + timedelta(days=3)).strftime("%Y-%m-%d")}
r = J(street.post("/api/tasks", json={**base, "assignee_org": "幸福里社区"}))
check("真实单位可下发", r.get("code") == 0, r.get("msg"))
r = J(street.post("/api/tasks", json={**base, "assignee_org": "幸福里社区服务中心"}))
check("不存在单位被拒", r.get("code") == 1 and "不存在" in str(r.get("msg")), r)
check("拒绝时给出可选值", "幸福里社区" in str(r.get("msg")), r.get("msg"))
r = J(street.post("/api/tasks", json=base))
check("留空 = 广播给该角色全部单位", r.get("code") == 0, r.get("msg"))

# ---------------------------------------------------------------- 5 接口契约
print("\n[5] 接口层契约：JSON 而不是 HTML 404/405")
r = comm.get("/api/nonexistent")
check("未知 /api 路径回 JSON 404",
      r.status_code == 404 and J(r).get("code") == 404, r.status_code)
check("404 带可读中文", "接口不存在" in str(J(r).get("msg")), J(r).get("msg"))
r = comm.post("/api/orders")
check("方法不允许回 JSON 405",
      r.status_code == 405 and J(r).get("code") == 405, r.status_code)
r = comm.get("/api/orders/abc/accept")
check("路由参数类型不匹配也不给 HTML",
      "json" in (r.headers.get("Content-Type") or ""), r.headers.get("Content-Type"))
r = comm.get("/notices/999999")
check("页面路由的不存在资源仍是 404 页面（不受影响）", r.status_code == 404)

# ---------------------------------------------------------------- 6 跨站写
print("\n[6] 跨站写请求拦截")
r = street.post("/api/tasks", json={**base, "assignee_org": ""},
                headers={"Sec-Fetch-Site": "cross-site"})
check("cross-site 写请求被拒 403", r.status_code == 403 and J(r).get("code") == 403,
      r.status_code)
r = street.post("/api/tasks", json={**base, "assignee_org": ""},
                headers={"Sec-Fetch-Site": "same-origin"})
check("same-origin 正常放行", J(r).get("code") == 0, J(r).get("msg"))
r = comm.get("/api/orders", headers={"Sec-Fetch-Site": "cross-site"})
check("读请求不受限制", r.status_code == 200)

# ---------------------------------------------------------------- 7 工单搜索
print("\n[7] 工单搜索 / 状态筛选 / 分页")
all_ = J(prop.get("/api/orders?limit=50")).get("data") or {}
c_all = all_.get("counts") or {}
check("不筛选时 matched == total", c_all.get("matched") == c_all.get("total"))
r = J(prop.get("/api/orders?q=门禁")).get("data") or {}
check("关键词命中 1 条", (r.get("counts") or {}).get("matched") == 1,
      (r.get("counts") or {}).get("matched"))
check("命中项内容正确", any("门禁" in i["content"] for i in r.get("items") or []))
check("筛选不改家底 total",
      (r.get("counts") or {}).get("total") == c_all.get("total"),
      f"{r.get('counts', {}).get('total')} vs {c_all.get('total')}")
r = J(prop.get("/api/orders?q=完全不存在的词")).get("data") or {}
check("查无结果时 matched=0 且 items 为空",
      (r.get("counts") or {}).get("matched") == 0 and not r.get("items"))
r = J(prop.get("/api/orders?status=open")).get("data") or {}
check("按状态筛选只回该状态",
      all(i["status"] == "open" for i in r.get("items") or []), len(r.get("items") or []))
check("状态筛选不改家底", (r.get("counts") or {}).get("total") == c_all.get("total"))
r = J(prop.get("/api/orders?status=不合法")).get("data") or {}
check("非法状态值被忽略（回全部）",
      (r.get("counts") or {}).get("matched") == c_all.get("total"))

# ---------------------------------------------------------------- 8 台账搜索分页
print("\n[8] 台账搜索 + 分页：不重不漏")
p1 = J(comm.get("/api/ledgers?limit=20&page=1")).get("data") or {}
p2 = J(comm.get("/api/ledgers?limit=20&page=2")).get("data") or {}
c1, c2 = p1.get("counts") or {}, p2.get("counts") or {}
ids1 = [i["id"] for i in p1.get("items") or []]
ids2 = [i["id"] for i in p2.get("items") or []]
check("第一页 20 条", len(ids1) == 20, len(ids1))
check("第二页 20 条", len(ids2) == 20, len(ids2))
check("两页无重复", not (set(ids1) & set(ids2)), set(ids1) & set(ids2))
check("total 是家底（60 条）", c1.get("total") == 60, c1.get("total"))
check("pages 算对（60/20=3）", c1.get("pages") == 3, c1.get("pages"))
check("末页 truncated=False 的判定正确",
      (J(comm.get("/api/ledgers?limit=20&page=3")).get("data") or {})
      .get("counts", {}).get("truncated") is False)
r = J(comm.get("/api/ledgers?q=第7号")).get("data") or {}
check("台账关键词命中", (r.get("counts") or {}).get("matched") == 1,
      (r.get("counts") or {}).get("matched"))
check("搜索不改家底", (r.get("counts") or {}).get("total") == 60)
r = J(comm.get("/api/ledgers?line=民政")).get("data") or {}
check("业务线筛选生效", all(i["line"] == "民政" for i in r.get("items") or []))
check("业务线筛选不改家底", (r.get("counts") or {}).get("total") == 60)
r = J(comm.get("/api/ledgers?page=0&limit=0")).get("data") or {}
check("page=0/limit=0 视作没填（回默认 100 条上限内全部）",
      len(r.get("items") or []) == 60, len(r.get("items") or []))

# ---------------------------------------------------------------- 9 页面契约
print("\n[9] 页面：搜索框 / 分页控件确实渲染出来")
html_orders = comm.get("/orders").get_data(as_text=True)
html_ledgers = comm.get("/ledgers").get_data(as_text=True)
for name, html, ids in [
    ("工单页", html_orders, ['id="q"', 'id="pgPrev"', 'id="pgNext"', 'id="stFilter"']),
    ("台账页", html_ledgers, ['id="q"', 'id="pgPrev"', 'id="pgNext"']),
]:
    for token in ids:
        check(f"{name}含 {token}", token in html)

print()
print("=" * 70)
print(f"通过 {PASS} / 失败 {FAIL}")
if FAILED:
    print("失败项：")
    for f in FAILED:
        print("  -", f)
print("=" * 70)
sys.exit(1 if FAIL else 0)
