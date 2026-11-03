"""回归：FDE 首录台看板数据真实性

首页那三块原本是写死的演示数字：柱状图 [12,9,15,5,5]（还带着一条不存在的
业务线"城管"）、折线三组固定数组、"本周节省工时 18.5h"、五条固定任务。
录十条新事项图表纹丝不动——这类"看起来正常"的假数据比报错更难发现，
因为你没有任何信号提示它是假的。

这套测试只钉一件事：**接口吐出的每个数字都要能对上库里的一条 SQL**。

A. 权限与形状（谁会 403、返回结构是否齐全）
B. 口径（业务线拆分、按天计数、节省工时公式、今日事项）
C. 联动（写进两条业务项后，四块数字必须同时变）
D. 空态（今日无录入时不许伪装成"今日 0 条"就完事，要如实回退）
E. 页面（模板里不许再出现写死的演示数字）

运行：python scripts/smoke_dashboard.py
"""
import os
import pathlib
import shutil
import sys
from datetime import date, datetime, time as dtime, timedelta

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# 独立库：会写数据（插业务项、删今日项），底座又必须有演示数据
SRC = BASE / "data" / "community.db"
DB = BASE / "data" / "smoke_dashboard.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()
    s_ = pathlib.Path(str(SRC) + suf)
    # 只拷主库会丢最近的写入（WAL 里还在），指标会平白少几十条
    if s_.exists():
        shutil.copy2(s_, str(DB) + suf)
os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from sqlalchemy import func, text  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import FieldSecurity, Ledger, WorkItem, WorkOrder  # noqa: E402

app = create_app("dev")
# 注意：这里的 app_context 必须等到**所有 test_client 建完**再 push。
# 先 push 再建 client，登录明明 302 成功，后续请求却全部 401——
# test_client 会继承外层 app ctx 里的会话缓存，登录态写不进去。
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
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


today = date.today()
monday = today - timedelta(days=today.weekday())
W_START = datetime.combine(monday, dtime.min)
W_END = datetime.combine(today + timedelta(days=1), dtime.min)
T_START = datetime.combine(today, dtime.min)

print("=" * 66)
print("A. 权限与返回结构")
print("=" * 66)

admin = app.test_client()
login(admin, "admin", "admin123")
r = admin.get("/api/fde/dashboard")
check("管理员可读看板（200）", r.status_code == 200, r.status_code)
j = r.get_json()
check("统一包一层 body", j.get("code") == 0 and j.get("msg") == "ok", j)
d = j["data"]
for k in ("week", "stats", "lines", "trend", "today"):
    check(f"data.{k} 存在", k in d)

prop = app.test_client()
login(prop, "wuye", "123456")
r = prop.get("/api/fde/dashboard")
# 状态码与 body 必须一致：只写 code=403 而 HTTP 回 200，
# 按状态码巡检的脚本会把"拒绝"记成"放行"
check("物业无首录查看权 → HTTP 403", r.status_code == 403, r.status_code)

anon = app.test_client()
check("未登录 → 401", anon.get("/api/fde/dashboard").status_code == 401)

# 所有 client 已建完，这里才进应用上下文用 ORM 对数
app.app_context().push()

print("=" * 66)
print("B. 口径必须对得上库")
print("=" * 66)

# 统计卡
item_week = WorkItem.query.filter(WorkItem.created_at >= W_START,
                                  WorkItem.created_at < W_END).count()
reused_week = Ledger.query.filter(Ledger.reused.is_(True),
                                  Ledger.created_at >= W_START,
                                  Ledger.created_at < W_END).count()
s = d["stats"]
check("item_week 与库一致", s["item_week"] == item_week, (s["item_week"], item_week))
check("reused_week 与库一致", s["reused_week"] == reused_week, (s["reused_week"], reused_week))
check("item_count 与库一致", s["item_count"] == WorkItem.query.count())
# 口径带 join：孤儿密级（业务项已删）不算"管控字段"，否则卡片比明细多。
# 期望值**必须走裸 SQL**：早先这里照着实现写了一遍 SQLAlchemy 表达式，
# 实现里 `query(func.count(x)).count()` 少了个 GROUP BY（恒返回 1），
# 测试抄了同一个错法 ⇒ 两边都是 1 ⇒ 全绿。断言和实现共用一套写法时，
# 测的就不是"算得对不对"，而是"两处代码长得一样不一样"。
secure_expect = db.session.execute(text(
    "SELECT count(*) FROM field_security fs "
    "JOIN work_item wi ON wi.id = fs.work_item_id WHERE fs.level >= 2")).scalar()
check("secure_count 与有效业务项的密级一致", s["secure_count"] == secure_expect,
      (s["secure_count"], secure_expect))
# 夹具自检：只有 1 条时上面那条断言对"恒返回 1"的实现也成立，等于没测
check("夹具里密级记录 >1 条（否则上条断言无意义）", secure_expect > 1, secure_expect)

# 节省工时：公式写死在测试里，页面/接口改了口径这里必须跟着改
want_min = item_week * 6 + reused_week * 10
check("saved_minutes = 业务项×6 + 复用×10", s["saved_minutes"] == want_min,
      (s["saved_minutes"], want_min))
check("saved_hours 保留一位小数", s["saved_hours"] == round(want_min / 60, 1),
      (s["saved_hours"], round(want_min / 60, 1)))
check("saved_basis 里的数字与字段同源",
      f"{item_week} 个业务项" in s["saved_basis"] and f"{reused_week} 条" in s["saved_basis"],
      s["saved_basis"])

# 业务线拆分：target_lines 是逗号分隔多值，必须逐行拆
LINES = ["物业", "综治", "民政", "卫健", "街道"]
names = d["lines"]["names"]
check("业务线只有系统里的 5 条", names == LINES, names)
check("不含不存在的业务线「城管」", "城管" not in names)
want = dict.fromkeys(LINES, 0)
for (raw,) in (db.session.query(WorkItem.target_lines)
               .filter(WorkItem.created_at >= W_START,
                       WorkItem.created_at < W_END).all()):
    for nm in str(raw or "").split(","):
        if nm.strip() in want:
            want[nm.strip()] += 1
check("柱状图每根柱子对得上库", d["lines"]["counts"] == [want[x] for x in LINES],
      (d["lines"]["counts"], [want[x] for x in LINES]))
check("合计可大于事项数（一条派多线）",
      sum(d["lines"]["counts"]) >= item_week,
      (sum(d["lines"]["counts"]), item_week))

# 趋势：本周一 ~ 今天，只到今天
T = d["trend"]
n_days = today.weekday() + 1
check("x 轴只画到今天", len(T["labels"]) == n_days, (T["labels"], n_days))
check("labels 与 dates 一一对应", len(T["dates"]) == len(T["labels"]))
check("dates 从本周一开始", T["dates"][0] == monday.isoformat(),
      (T["dates"][0], monday.isoformat()))


def by_day(model):
    rows = (db.session.query(func.date(model.created_at), func.count(model.id))
            .filter(model.created_at >= W_START, model.created_at < W_END)
            .group_by(func.date(model.created_at)).all())
    return {str(k): int(v) for k, v in rows if k}


for key, model, label in (("parsed", WorkItem, "拆解"),
                          ("dispatched", WorkOrder, "派单"),
                          ("ledger", Ledger, "台账")):
    m = by_day(model)
    want = [m.get(k, 0) for k in T["dates"]]
    check(f"趋势-{label} 逐日对得上库", T[key] == want, (T[key], want))

# 今日事项
rows = d["today"]["rows"]
check("今日事项不超过 8 行", len(rows) <= 8, len(rows))
check("每行都有事项/去向/状态",
      all(x.get("subject") is not None and x.get("line") is not None
          and x.get("status") in ("已派单", "办理中", "已完成", "待派发") for x in rows),
      rows[:2])

print("=" * 66)
print("C. 联动：写进两条业务项，四块数字必须同时变")
print("=" * 66)

# 先钉一条"今日已有"的事项当基线。
#
# 为什么不能省：今日为空时 `_today_rows` 会**回退到最近 6 条历史事项**并置
# `fallback=True`。此时 `before["today"]["by_status"]` 是那 6 条历史事项的分布，
# 而插完两条之后今日非空、走的是真实当日统计——拿历史分布 +2 去比真实值，
# 这条断言必然失败，且和代码对错无关。之前它是绿的，只是因为库里恰好
# 有当日数据；一旦清空（或被别的脚本清掉）就翻红，属于测试对数据状态的
# 隐含依赖，必须由测试自己把前置状态摆好。
anchor = WorkItem(fde_id=None, type="走访", subject="基线-今日已有事项",
                  target_lines="综治", status="pending", created_at=datetime.now())
db.session.add(anchor)
db.session.commit()

before = admin.get("/api/fde/dashboard").get_json()["data"]
i1 = WorkItem(fde_id=None, type="报修", subject="联动测试-楼道灯", target_lines="综治",
              status="pending", created_at=datetime.now())
i2 = WorkItem(fde_id=None, type="走访", subject="联动测试-独居老人", target_lines="民政,卫健",
              status="pending", created_at=datetime.now())
db.session.add_all([i1, i2])
db.session.commit()

after = admin.get("/api/fde/dashboard").get_json()["data"]
check("业务项总数 +2", after["stats"]["item_count"] == before["stats"]["item_count"] + 2)
check("本周业务项 +2", after["stats"]["item_week"] == before["stats"]["item_week"] + 2)
check("节省工时 +12 分钟",
      after["stats"]["saved_minutes"] == before["stats"]["saved_minutes"] + 12,
      (before["stats"]["saved_minutes"], after["stats"]["saved_minutes"]))
check("综治 +1", after["lines"]["counts"][1] == before["lines"]["counts"][1] + 1)
check("民政、卫健各 +1",
      after["lines"]["counts"][2] == before["lines"]["counts"][2] + 1
      and after["lines"]["counts"][3] == before["lines"]["counts"][3] + 1)
check("今日拆解 +2", after["trend"]["parsed"][-1] == before["trend"]["parsed"][-1] + 2,
      (before["trend"]["parsed"][-1], after["trend"]["parsed"][-1]))

today_db = WorkItem.query.filter(WorkItem.created_at >= T_START).count()
check("今日事项计数跟着变", after["today"]["count"] in (today_db, min(today_db, 8)),
      (after["today"]["count"], today_db))
bs = after["today"]["by_status"]
check("状态分布四项齐全", set(bs) == {"待派发", "已派单", "办理中", "已完成"}, bs)
check("状态分布之和 == 今日事项数（同一口径）", sum(bs.values()) == after["today"]["count"],
      (sum(bs.values()), after["today"]["count"]))
check("新插的两条落进「待派发」", bs["待派发"] == before["today"]["by_status"]["待派发"] + 2,
      (before["today"]["by_status"], bs))
subs = [x["subject"] for x in after["today"]["rows"]]
check("新选项出现在今日事项里", "联动测试-楼道灯" in subs, subs)
check("无工单的事项标「待派发」",
      next(x for x in after["today"]["rows"] if x["subject"] == "联动测试-楼道灯")["status"] == "待派发")
check("多条去向用 / 连接",
      next(x for x in after["today"]["rows"] if x["subject"] == "联动测试-独居老人")["line"] == "民政/卫健")

# 工单状态要反映到事项状态上
o = WorkOrder(work_item_id=i1.id, order_no="T-LINK-001", property_co="测试物业",
              content="联动测试", status="doing", created_at=datetime.now())
db.session.add(o)
db.session.commit()
after2 = admin.get("/api/fde/dashboard").get_json()["data"]
check("工单 doing → 事项显示「办理中」",
      next(x for x in after2["today"]["rows"] if x["subject"] == "联动测试-楼道灯")["status"] == "办理中")
check("派单趋势今日 +1", after2["trend"]["dispatched"][-1] == after["trend"]["dispatched"][-1] + 1)

# 只有台账、没有工单（民政/综治线的常态）：必须和居民端一样算"已完成"。
# 早先这里只看工单，这类事项永远显示"待派发"，而居民端早就是"已办结"——
# 同一个事项两个页面两种说法，看的人只会相信说"没人管"的那个。
lg = Ledger(work_item_id=i2.id, line="民政",
            content_json='{"note":"联动测试归档"}', created_at=datetime.now())
db.session.add(lg)
db.session.commit()
after3 = admin.get("/api/fde/dashboard").get_json()["data"]
check("只有台账的事项 → 已完成",
      next(x for x in after3["today"]["rows"] if x["subject"] == "联动测试-独居老人")["status"] == "已完成",
      after3["today"]["by_status"])
check("台账也计入趋势", after3["trend"]["ledger"][-1] == after2["trend"]["ledger"][-1] + 1)
check("已完成计数 +1", after3["today"]["by_status"]["已完成"] == after2["today"]["by_status"]["已完成"] + 1)

print("=" * 66)
print("D. 空态：今日没录入时如实回退，而不是假装有数据")
print("=" * 66)

# 不能直接 DELETE：work_item 被工单、台账、字段密级三张表引用，
# 硬删会撞外键。把时间挪到上周既达到"今天没录过"的效果，又不破坏引用。
WorkItem.query.filter(WorkItem.created_at >= T_START).update(
    {WorkItem.created_at: W_START - timedelta(days=7)}, synchronize_session=False)
db.session.commit()
empty = admin.get("/api/fde/dashboard").get_json()["data"]
check("今日为空 → fallback 标记", empty["today"]["fallback"] is True, empty["today"])
check("回退时给的是历史事项（不空表）", len(empty["today"]["rows"]) > 0, empty["today"]["count"])
check("回退时 count 与行数一致（说的是「这几条」）",
      empty["today"]["count"] == len(empty["today"]["rows"]),
      (empty["today"]["count"], len(empty["today"]["rows"])))
check("今日折线落到 0 而不是消失", empty["trend"]["parsed"][-1] == 0,
      empty["trend"]["parsed"])

print("=" * 66)
print("E. 页面里不许再有写死的演示数字")
print("=" * 66)

comm = app.test_client()
login(comm, "wang", "123456")
html = comm.get("/").get_data(as_text=True)
check("页面能打开", len(html) > 1000)
check("页面调看板接口", "/api/fde/dashboard" in html)
# 注释里写着"以前这里是 [12,9,15,5,5]"是有价值的事故记录，不该为了过测试删掉，
# 所以先剥离注释，只检查真正会执行的代码。
import re  # noqa: E402
code = re.sub(r"/\*[\s\S]*?\*/", "", html)
code = re.sub(r"<!--[\s\S]*?-->", "", code)
for bad, why in (("18.5h", "写死的节省工时"), ("[12,9,15,5,5]", "写死的柱状图"),
                 ("城管", "不存在的业务线"), ("[8,10,7,12,9]", "写死的折线"),
                 ("3栋楼道灯维修", "写死的今日任务")):
    check(f"不含{why}", bad not in code, bad)
check("今日任务表体由前端渲染", 'id="todayBody"' in code)
check("节省工时标注为估算", "估算" in code)
check("柱状图数据来自接口变量", "L.counts" in code and "L.names" in code)

print()
print("=" * 66)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
print("=" * 66)

# 先把连接池放掉再删文件：Windows 上库还开着就删不掉（PermissionError）
try:
    db.session.remove()
    db.engine.dispose()
    for suf in ("", "-wal", "-shm"):
        p = pathlib.Path(str(DB) + suf)
        if p.exists():
            p.unlink()
except OSError:
    pass          # 删不掉不影响结论，下次运行开头会重建
sys.exit(1 if FAIL else 0)
