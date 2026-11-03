"""回归：统计数字必须与库一致（页面「总数」类数字的口径）

这一套只守一件事：**页面上出现的统计数字，必须等于库里的真实条数**。

为什么值得单独一套测试：
原来三个页面的"总数"都是前端从列表里数长度，而列表都有 limit——
演示库 57 条工单只给 50 条，"已完成"就少报 7 件；审计 1000 多条显示成 200 条；
台账 101 条显示成 100 条。数字偏小会让人误判进度，比功能少更致命，
而且不看代码根本发现不了（页面上没有任何报错）。

规矩定在这里：统计一律服务端 count，且**与明细同源**（同一段权限/隔离过滤）。
本套覆盖：
  1. 契约：列表接口返回 {items, counts}
  2. 工单：三种状态数、超期数、隔离后的总数都与库一致；limit 不改变统计
  3. 台账：总数/复用/业务线数与库一致；筛选只改 matched，不改"家底"
  4. 审计：总数/保密审查/越权拒绝与库一致；按 Agent 筛选不改总数
  5. 页面：视角文案取会话单位，不写死；登录页演示卡片取账号数据
"""
import os
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_stats.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
os.environ["NOTICE_AUTO_IMPORT"] = "0"

import json                                             # noqa: E402
from datetime import datetime, timedelta                # noqa: E402

from werkzeug.security import generate_password_hash    # noqa: E402

from app import create_app                              # noqa: E402
from app.agents.supervise import SuperviseAgent         # noqa: E402
from app.api.audit import DEFAULT_LIMIT as AUDIT_LIMIT  # noqa: E402
from app.api.ledgers import DEFAULT_LIMIT as LEDGER_LIMIT  # noqa: E402
from app.api.orders import DEFAULT_LIMIT as ORDER_LIMIT, MAX_LIMIT as ORDER_MAX  # noqa: E402
from app.extensions import db                           # noqa: E402
from app.models import AuditLog, Ledger, User, WorkOrder  # noqa: E402

app = create_app("dev")

NOW = datetime.now()
MINE = "幸福里物业"
OTHER = "测试物业"

# ---------------------------------------------------------------- 造数据
with app.app_context():
    # 工单：本家 38 件（30 待接单 + 5 已完成 + 3 处理中且已超期），别家 3 件
    rows = []
    for i in range(30):
        rows.append(WorkOrder(order_no=f"W-S-{i:03d}", content=f"测试报修 {i}",
                              property_co=MINE, status="open", sla_hours=24,
                              sla_level="较急", created_at=NOW - timedelta(hours=1)))
    for i in range(5):
        rows.append(WorkOrder(order_no=f"W-S-D{i}", content=f"已办结 {i}",
                              property_co=MINE, status="done", sla_hours=24,
                              sla_level="较急", created_at=NOW - timedelta(days=3)))
    for i in range(3):
        # SLA 4h、已过 100h → 必然超期
        rows.append(WorkOrder(order_no=f"W-S-O{i}", content=f"超期单 {i}",
                              property_co=MINE, status="doing", sla_hours=4,
                              sla_level="紧急", created_at=NOW - timedelta(hours=100)))
    for i in range(2):
        rows.append(WorkOrder(order_no=f"W-X-{i}", content="别家待接单",
                              property_co=OTHER, status="open", sla_hours=24,
                              created_at=NOW - timedelta(hours=1)))
    rows.append(WorkOrder(order_no="W-X-D", content="别家已完成",
                          property_co=OTHER, status="done", sla_hours=24,
                          created_at=NOW - timedelta(days=2)))

    # 台账：12 条 / 4 条复用 / 3 条业务线
    for i in range(12):
        line = ("综治", "民政", "卫健")[i % 3]
        content = {"type": "测试", "subject": f"台账 {i}", "room_key": f"{i}栋",
                   "reused_archive": i < 4, "source": "测试用"}
        rows.append(Ledger(line=line, content_json=json.dumps(content, ensure_ascii=False),
                           reused=(i < 4), created_at=NOW - timedelta(hours=i)))
    db.session.add_all(rows)

    # 审计：250 条（其中 security 7 条、deny 3 条），确保超过 limit 200
    logs = []
    for i in range(250):
        logs.append(AuditLog(
            actor="测试", agent="security" if i < 7 else "auth",
            action="deny" if i >= 247 else "login",
            input_digest=f"#{i}", output_digest="测试用",
            created_at=NOW - timedelta(minutes=i)))
    db.session.add_all(logs)

    # 另建一个**异单位**物业账号，用来验证"视角文案取会话单位"。
    # 不能叫 wuye2：那已经是 DEMO_USERS 里的赵师傅（幸福里物业），
    # 同名两条用户会让登录路由取到哪一条不确定。
    db.session.add(User(username="wuye_other", name="赵经理", role="property",
                        org=OTHER, status="active", created_by="测试",
                        password_hash=generate_password_hash("wuye2pass")))
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


def login(c, u, p):
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


def api(c, path):
    return c.get(path).get_json()


# ================================================================ 1 契约
print("\n[1] 三个列表接口统一返回 {items, counts}")
c = app.test_client()
login(c, "wuye", "123456")             # 物业：只有 order.view
cw = app.test_client()
login(cw, "wang", "123456")            # 社区：有 order.view + ledger.view

for cli, path in ((c, "/api/orders"), (cw, "/api/ledgers")):
    d = api(cli, path)["data"]
    check(f"{path} 是 dict 且含 items/counts",
          isinstance(d, dict) and "items" in d and "counts" in d, type(d).__name__)
    check(f"{path} items 是列表", isinstance(d["items"], list))
    check(f"{path} counts 是 dict", isinstance(d["counts"], dict))

# ================================================================ 2 工单
print("\n[2] 工单统计与库一致，且受数据隔离约束")
with app.app_context():
    exp_total = WorkOrder.query.filter_by(property_co=MINE).count()
    exp_open = WorkOrder.query.filter_by(property_co=MINE, status="open").count()
    exp_doing = WorkOrder.query.filter_by(property_co=MINE, status="doing").count()
    exp_done = WorkOrder.query.filter_by(property_co=MINE, status="done").count()
    exp_over = sum(1 for o in WorkOrder.query.filter(
        WorkOrder.property_co == MINE, WorkOrder.status != "done").all()
        if SuperviseAgent.is_overdue(o))
    all_total = WorkOrder.query.count()

d = api(c, "/api/orders")["data"]
c_ = d["counts"]
check("总数等于本物业真实条数", c_["total"] == exp_total, f'{c_["total"]} vs {exp_total}')
check("待接单数一致", c_["open"] == exp_open, f'{c_["open"]} vs {exp_open}')
check("处理中数一致", c_["doing"] == exp_doing)
check("已完成数一致", c_["done"] == exp_done, f'{c_["done"]} vs {exp_done}')
check("超期数一致", c_["overdue"] == exp_over, f'{c_["overdue"]} vs {exp_over}')
check("三种状态之和等于总数", c_["open"] + c_["doing"] + c_["done"] == c_["total"])
check("看不到别家工单在内（隔离）", c_["total"] < all_total,
      f'{c_["total"]} vs 全库 {all_total}')
check("明细里全为本单位工单",
      all(o["property_co"] == MINE for o in d["items"]))

print("\n[3] limit 只影响明细，不影响统计")
d = api(c, f"/api/orders?limit=5")["data"]
check("limit=5 时明细 5 条", len(d["items"]) == 5, len(d["items"]))
check("limit=5 时总数不变", d["counts"]["total"] == exp_total, d["counts"]["total"])
check("标记为已截断", d["counts"]["truncated"] is True)
check("shown 记为实际返回条数", d["counts"]["shown"] == 5)
d2 = api(c, f"/api/orders?limit={ORDER_MAX + 999}")["data"]
check(f"超大 limit 被夹到 {ORDER_MAX}", d2["counts"]["shown"] == min(exp_total, ORDER_MAX),
      d2["counts"]["shown"])
d3 = api(c, "/api/orders?limit=abc")["data"]
check("非法 limit 回落到默认值",
      d3["counts"]["shown"] == min(exp_total, ORDER_LIMIT), d3["counts"]["shown"])

# 别家物业看到的只有自己的 3 件
c2 = app.test_client()
login(c2, "wuye_other", "wuye2pass")
d = api(c2, "/api/orders")["data"]
check("另一家物业总数=3", d["counts"]["total"] == 3, d["counts"]["total"])
check("另一家待接单=2", d["counts"]["open"] == 2, d["counts"]["open"])
check("另一家看不到本家工单", d["counts"]["total"] < exp_total)

# ================================================================ 4 台账
print("\n[4] 台账统计与库一致（尤其「复用」不能退化成布尔）")
with app.app_context():
    lg_total = Ledger.query.count()
    lg_reused = Ledger.query.filter(Ledger.reused.is_(True)).count()
    lg_lines = len({r[0] for r in Ledger.query.with_entities(Ledger.line).all() if r[0]})
    lg_zongzhi = Ledger.query.filter_by(line="综治").count()

d = api(cw, "/api/ledgers")["data"]
lc = d["counts"]
check("台账总数一致", lc["total"] == lg_total, f'{lc["total"]} vs {lg_total}')
check("复用数与库一致", lc["reused"] == lg_reused, f'{lc["reused"]} vs {lg_reused}')
check("复用数没有退化成 1（布尔求和陷阱）",
      not (lg_reused > 1 and lc["reused"] == 1), lc["reused"])
check("业务线数一致", lc["lines"] == lg_lines, f'{lc["lines"]} vs {lg_lines}')
check("默认 limit 生效（明细被截到 %d 条）" % LEDGER_LIMIT,
      lc["shown"] == min(lg_total, LEDGER_LIMIT), lc["shown"])

print("\n[5] 台账筛选只改 matched，不改「家底」")
d = api(cw, "/api/ledgers?line=%E7%BB%BC%E6%B2%BB")["data"]
check("筛「综治」matched 变为该线条数", d["counts"]["matched"] == lg_zongzhi,
      f'{d["counts"]["matched"]} vs {lg_zongzhi}')
check("筛「综治」总数仍是全库", d["counts"]["total"] == lg_total, d["counts"]["total"])
check("筛「综治」复用数仍是全库", d["counts"]["reused"] == lg_reused)
check("筛「综治」明细全是综治", all(x["line"] == "综治" for x in d["items"]))

# ================================================================ 6 审计
print("\n[6] 审计统计与库一致")
c3 = app.test_client()
login(c3, "jiedao", "123456")          # 街道是唯一有 audit.view 的角色
with app.app_context():
    au_total = AuditLog.query.count()
    au_sec = AuditLog.query.filter_by(agent="security").count()
    au_deny = AuditLog.query.filter_by(action="deny").count()

ac = api(c3, "/api/audit")["data"]["counts"]
check("审计总数与库一致", ac["total"] == au_total, f'{ac["total"]} vs {au_total}')
check("保密审查数与库一致", ac["security"] == au_sec, f'{ac["security"]} vs {au_sec}')
check("越权拒绝数与库一致", ac["deny"] == au_deny, f'{ac["deny"]} vs {au_deny}')
check("明细被截到 limit", ac["shown"] == min(au_total, AUDIT_LIMIT), ac["shown"])
check("标记已截断", ac["truncated"] is (au_total > AUDIT_LIMIT))

ac2 = api(c3, "/api/audit?agent=security")["data"]["counts"]
check("筛 security 后 matched 正确", ac2["matched"] == au_sec, ac2["matched"])
check("筛 security 后「总数」不变（不会误读成总共只有 7 条）",
      ac2["total"] == au_total, ac2["total"])
check("筛 security 后保密审查卡不变", ac2["security"] == au_sec)

# ================================================================ 7 页面
print("\n[7] 页面文案取会话数据，不写死演示单位")
html = c.get("/orders").get_data(as_text=True)
check("物业页写明本单位", "当前视角：幸福里物业" in html)
check("物业页没有把别家名字写死", "测试物业" not in html)
html2 = c2.get("/orders").get_data(as_text=True)
check("换一家物业，视角文案跟着变", "当前视角：测试物业" in html2,
      "仍显示别家名字")
check("换一家物业后不残留原写死文案", "当前视角：幸福里物业" not in html2)

html3 = c3.get("/orders").get_data(as_text=True)
check("街道看到的是监督视角", "监督视角" in html3 and "当前视角" not in html3)

anon = app.test_client()
lp = anon.get("/login").get_data(as_text=True)
check("登录页演示卡片读账号数据（单位已同步）", "幸福里街道办事处" in lp)
check("登录页不再残留旧单位名", "和平街道" not in lp)
# 数量不再写死：物业有两个账号（李经理 / 赵师傅，转派要用），
# 硬编码 "== 4" 会在加人时变成一条莫名其妙的失败。
# 真正要守的是"每个演示账号都有一张卡、且卡片数据来自账号表"。
with app.app_context():
    demo_us = [u.username for u in User.query.filter(
        User.username.in_(["admin", "wang", "wuye", "wuye2", "jiedao"])).all()]
check("登录页演示卡片覆盖全部演示账号",
      all(f'data-u="{u}"' in lp for u in ["admin", "wang", "wuye", "wuye2", "jiedao"]),
      f"库里有 {sorted(demo_us)}，页面 {lp.count('class=\"role\"')} 张")
check("登录页卡片数与演示账号数一致", lp.count('class="role"') == 5,
      lp.count('class="role"'))

# ================================================================ 8 报表口径
print("\n[8] 报表的复用次数与台账页同源")
with app.app_context():
    from app.agents.report import ReportAgent
    import inspect
    src = inspect.getsource(ReportAgent.run)
check("报表不再自己扫 JSON 数复用",
      "reused_archive" not in src and "lg.reused" in src,
      "报表还在解析 content_json")

print("\n" + "=" * 66)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败项：")
    for x in FAILED:
        print("  -", x)
print("=" * 66)
sys.exit(1 if FAIL else 0)
