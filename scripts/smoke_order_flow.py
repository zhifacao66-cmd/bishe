"""回归：工单处理人与流转记录（谁接的 / 谁转给谁 / 谁办结的）

这一套锁的是"追责链"，不是"按钮能点"：

  1. 处理人必须落到**人**（账号），不是单位名——单位记不住谁干的活；
  2. 处理人取会话身份，前端传什么都不算数（否则可以冒名）；
  3. 每次状态变更都要留下一条 `work_order_log`，且**历史行永不改写**：
     转派之后，原来那条"某某接单"还是原来那个人；
  4. 转派只能转给本单位在职的处理人，且转派**不动接单时间**
     （否则换个手 SLA 就自动复活，超期统计失真）；
  5. 办结人不等于接单人（交接班）时，assignee 保留接单人，
     办结人写在流转记录里；
  6. 上下级只读：街道能看流转记录，但不能接单/转派/办结。

用独立库，不碰 data/community.db。
"""
import os
import pathlib
import sys
from datetime import datetime, timedelta

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_orders.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"    # 规则路径，不依赖 GPU
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from werkzeug.security import generate_password_hash            # noqa: E402

from app import create_app                                      # noqa: E402
from app.extensions import db                                    # noqa: E402
from app.models import User, WorkItem, WorkOrder, WorkOrderLog  # noqa: E402

app = create_app("dev")

# ---------------------------------------------------------------- 造数据
with app.app_context():
    # 同单位的"赵师傅"来自 DEMO_USERS（演示库本来就该有两个维修工）；
    # 这里只补一个异单位的同行，用来验越权。
    db.session.add(User(username="wuye3", name="孙主管", role="property",
                        org="阳光花园物业", status="active",
                        password_hash=generate_password_hash("123456")))
    for i, kw in enumerate(["厨房水管漏水", "楼道电路跳闸", "单元门禁损坏",
                            "电梯异响", "外墙瓷砖脱落"], start=1):
        db.session.add(WorkItem(id=i, type="报修", subject=kw,
                                target_lines="物业", status="done"))
        db.session.add(WorkOrder(
            id=i, order_no=f"WO-FLOW-{i:03d}", work_item_id=i,
            property_co="幸福里物业", content=f"{i}号楼 {kw}，请尽快处理",
            sla_level="一般", sla_hours=72, status="open",
            created_at=datetime.now() - timedelta(days=i)))
    # 历史单：老版本升级上来的，没有处理人字段
    db.session.add(WorkOrder(
        id=90, order_no="WO-OLD-090", work_item_id=1,
        property_co="幸福里物业", content="历史工单：楼道灯不亮",
        sla_level="一般", sla_hours=72, status="done",
        feedback="已更换灯泡", created_at=datetime.now() - timedelta(days=30)))
    # 别家单位的单
    db.session.add(WorkOrder(
        id=91, order_no="WO-OTHER-091", work_item_id=2,
        property_co="阳光花园物业", content="隔壁小区：水管爆裂",
        sla_level="紧急", sla_hours=4, status="open",
        created_at=datetime.now() - timedelta(hours=2)))
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


def login(c, u="wuye", p="123456"):
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


def J(r):
    return r.get_json(silent=True) or {}


def msg(j):
    d = j.get("data")
    if isinstance(d, dict):
        return d.get("msg") or j.get("msg") or ""
    return j.get("msg") or ""


def logs(oid):
    with app.app_context():
        return (WorkOrderLog.query.filter_by(order_id=oid)
                .order_by(WorkOrderLog.id.asc()).all())


def order(oid):
    with app.app_context():
        return db.session.get(WorkOrder, oid)


print("=" * 70)
print("工单处理人与流转记录 · 接单 / 转派 / 办结 / 只读 / 越权")
print("=" * 70)

admin = app.test_client(); login(admin, "admin", "admin123")
comm = app.test_client(); login(comm, "wang")
prop = app.test_client(); login(prop, "wuye")          # 李经理
prop2 = app.test_client(); login(prop2, "wuye2")       # 赵师傅（同单位）
other = app.test_client(); login(other, "wuye3")       # 孙主管（异单位）
street = app.test_client(); login(street, "jiedao")

# ---------------------------------------------------------------- 1 派单留痕
print("\n[1] 派单是流转链的第一环")
from app.agents.dispatch import DispatchAgent                    # noqa: E402
with app.app_context():
    DispatchAgent().run({"work_item_id": 5, "subject": "外墙瓷砖脱落",
                         "content": "5号楼外墙瓷砖脱落"})
    new_order = WorkOrder.query.filter_by(work_item_id=5).order_by(
        WorkOrder.id.desc()).first()
    nid = new_order.id
lg = logs(nid)
check("派单生成了工单", new_order is not None)
check("派单写了一条流转记录", len(lg) == 1, f"{len(lg)} 条")
check("动作是 dispatch", lg and lg[0].action == "dispatch", lg[0].action if lg else None)
check("派单记录的操作人是 Agent", lg and "Agent" in (lg[0].actor or ""),
      lg[0].actor if lg else None)
check("派单后状态是 open", new_order.status == "open", new_order.status)
check("派单时还没有处理人", not new_order.assignee, new_order.assignee)

# ---------------------------------------------------------------- 2 接单
print("\n[2] 接单：处理人落到“人”，取会话身份")
r = J(prop.post(f"/api/orders/{nid}/accept", json={"assignee": "冒名的人"}))
d = r.get("data") or {}
check("接单成功", r.get("code") == 0, msg(r))
check("处理人是登录者本人，不是前端传的名字",
      d.get("assignee") == "李经理", d.get("assignee"))
check("返回了接单时间", bool(d.get("assigned_at")), d.get("assigned_at"))
o = order(nid)
check("库里 assignee 落的是李经理", o.assignee == "李经理", o.assignee)
check("assignee_id 也落了（改名后还能对上人）", o.assignee_id is not None, o.assignee_id)
check("assigned_at 已写", o.assigned_at is not None)
check("状态变 doing", o.status == "doing", o.status)
lg = logs(nid)
check("流转记录变成 2 条", len(lg) == 2, f"{len(lg)} 条")
check("第 2 条是 accept", lg[1].action == "accept", lg[1].action)
check("记录了状态迁移 open → doing",
      (lg[1].from_status, lg[1].to_status) == ("open", "doing"),
      f"{lg[1].from_status} → {lg[1].to_status}")
check("记录了操作人", lg[1].actor == "李经理", lg[1].actor)

r = J(prop.post(f"/api/orders/{nid}/accept"))
check("重复接单被拒", r.get("code") != 0, msg(r))
check("重复接单提示当前状态", "doing" in msg(r), msg(r))

# ---------------------------------------------------------------- 3 转派
print("\n[3] 转派：换人要留痕，且不能改接单时间")
before_at = order(nid).assigned_at

r = J(prop.post(f"/api/orders/{nid}/transfer", json={"assignee": "查无此人"}))
check("转给不存在的人被拒", r.get("code") != 0, msg(r))
check("拒绝时列出可选的人", "赵师傅" in msg(r), msg(r))

r = J(prop.post(f"/api/orders/{nid}/transfer", json={"assignee": "孙主管"}))
check("转给别单位的人被拒", r.get("code") != 0, msg(r))

r = J(prop.post(f"/api/orders/{nid}/transfer", json={"assignee": "李经理"}))
check("转给自己被拒（无意义操作）", r.get("code") != 0, msg(r))
check("提示无需转派", "无需转派" in msg(r), msg(r))

r = J(prop.post(f"/api/orders/{nid}/transfer",
                json={"assignee": "赵师傅", "note": "李经理请假，交接给赵师傅"}))
check("转派成功", r.get("code") == 0, msg(r))
o = order(nid)
check("处理人已换成赵师傅", o.assignee == "赵师傅", o.assignee)
check("assignee_id 同步更新", o.assignee_id is not None)
check("接单时间没被重置（SLA 不会复活）", o.assigned_at == before_at,
      f"{o.assigned_at} vs {before_at}")
check("状态仍是 doing", o.status == "doing", o.status)
lg = logs(nid)
check("流转记录 3 条", len(lg) == 3, f"{len(lg)} 条")
check("第 3 条是 transfer", lg[2].action == "transfer", lg[2].action)
check("转派记录了“从谁到谁”", "李经理" in (lg[2].detail or "")
      and "赵师傅" in (lg[2].detail or ""), lg[2].detail)
check("转派记录了说明", "请假" in (lg[2].detail or ""), lg[2].detail)
check("历史未被改写：第 2 条接单人还是李经理", lg[1].actor == "李经理", lg[1].actor)
check("转派 from/to 都是 doing",
      (lg[2].from_status, lg[2].to_status) == ("doing", "doing"),
      f"{lg[2].from_status} → {lg[2].to_status}")

# ---------------------------------------------------------------- 4 办结（交接班）
print("\n[4] 办结：谁办结记谁，但接单人不被覆盖")
r = J(prop2.post(f"/api/orders/{nid}/finish", json={"feedback": "已上门加固，瓷砖复位"}))
check("赵师傅（当前处理人）办结成功", r.get("code") == 0, msg(r))
o = order(nid)
check("状态变 done", o.status == "done", o.status)
check("done_at 已写", o.done_at is not None)
check("反馈落库", o.feedback == "已上门加固，瓷砖复位", o.feedback)
check("处理人仍是赵师傅", o.assignee == "赵师傅", o.assignee)
check("返回了处理耗时", (r.get("data") or {}).get("handle_hours") is not None,
      (r.get("data") or {}).get("handle_hours"))
lg = logs(nid)
check("流转记录 4 条", len(lg) == 4, f"{len(lg)} 条")
check("第 4 条是 finish", lg[3].action == "finish", lg[3].action)
check("办结人是实际操作的赵师傅", lg[3].actor == "赵师傅", lg[3].actor)
check("办结记录里带着反馈原文",
      "瓷砖复位" in (lg[3].detail or ""), lg[3].detail)

r = J(prop.post(f"/api/orders/{nid}/finish", json={"feedback": "again"}))
check("已办结的单不能再办结", r.get("code") != 0, msg(r))
r = J(prop.post(f"/api/orders/{nid}/transfer", json={"assignee": "赵师傅"}))
check("已办结的单不能转派", r.get("code") != 0, msg(r))
r = J(prop.post(f"/api/orders/{nid}/accept"))
check("已办结的单不能接单", r.get("code") != 0, msg(r))

# 交接班：李经理接的单，赵师傅直接办结（没走转派）
print("\n[4b] 交接班：接单人 ≠ 办结人")
r = J(prop.post("/api/orders/1/accept"))
check("李经理接单", r.get("code") == 0, msg(r))
r = J(prop2.post("/api/orders/1/finish", json={"feedback": "代班处理完毕"}))
check("赵师傅直接办结成功", r.get("code") == 0, msg(r))
o = order(1)
check("接单人仍是李经理（不被覆盖）", o.assignee == "李经理", o.assignee)
lg = logs(1)
check("办结人记的是赵师傅", lg[-1].action == "finish" and lg[-1].actor == "赵师傅",
      f"{lg[-1].action}/{lg[-1].actor}")

# ---------------------------------------------------------------- 5 处理耗时
print("\n[5] 处理耗时只用真实接单时间算")
r = J(prop.post("/api/orders/2/accept"))
check("先接单", r.get("code") == 0, msg(r))
with app.app_context():
    o = db.session.get(WorkOrder, 2)
    o.assigned_at = datetime.now() - timedelta(hours=10)   # 模拟干了 10 小时
    db.session.commit()
r = J(prop.post("/api/orders/2/finish", json={"feedback": "更换跳闸空开"}))
hh = (r.get("data") or {}).get("handle_hours")
check("办结返回处理耗时", hh is not None, hh)
check("耗时约 10 小时", hh is not None and 9.9 <= hh <= 10.1, hh)

# 老单（没有接单时间）办结：耗时应为 None，而不是 0
with app.app_context():
    o = db.session.get(WorkOrder, 3)
    o.status = "doing"
    db.session.commit()
r = J(prop.post("/api/orders/3/finish", json={"feedback": "老单补办结"}))
check("老单没有接单时间时耗时为 null（不拿 0 冒充）",
      (r.get("data") or {}).get("handle_hours") is None,
      (r.get("data") or {}).get("handle_hours"))
check("老单办结后补上了处理人", order(3).assignee == "李经理", order(3).assignee)

# ---------------------------------------------------------------- 6 详情与流转
print("\n[6] 详情接口：完整流转链可查")
r = J(prop.get(f"/api/orders/{nid}"))
d = r.get("data") or {}
check("详情成功", r.get("code") == 0, msg(r))
check("详情带 assignee", d.get("assignee") == "赵师傅", d.get("assignee"))
check("详情带处理耗时", d.get("handle_hours") is not None, d.get("handle_hours"))
ev = d.get("events") or []
check("详情带 4 条流转", len(ev) == 4, len(ev))
check("流转按时间正序（派单在最前）", ev and ev[0].get("action") == "dispatch",
      ev[0].get("action") if ev else None)
check("流转里有中文动作名", ev and ev[0].get("label") == "派单",
      ev[0].get("label") if ev else None)
check("流转里带操作人", all(x.get("actor") for x in ev), [x.get("actor") for x in ev])
check("流转里带时刻", all(x.get("at") for x in ev))
check("event_count 与 events 一致", d.get("event_count") == len(ev),
      f"{d.get('event_count')} vs {len(ev)}")

r = J(comm.get("/api/orders/90"))
d = r.get("data") or {}
check("历史老单能打开详情", r.get("code") == 0, msg(r))
check("历史老单没有处理人不报错", d.get("assignee") == "", repr(d.get("assignee")))
check("历史老单流转为空", (d.get("events") or []) == [], d.get("events"))

# ---------------------------------------------------------------- 7 列表
print("\n[7] 列表：处理人可见、可按人搜、筛选不动家底")
r = J(prop.get("/api/orders?limit=50"))
d = r.get("data") or {}
c = d.get("counts") or {}
item = [x for x in (d.get("items") or []) if x["id"] == nid]
check("列表项带 assignee", item and item[0].get("assignee") == "赵师傅",
      item[0].get("assignee") if item else None)
check("列表项带 assigned_at", item and bool(item[0].get("assigned_at")))
check("列表项带 done_at", item and bool(item[0].get("done_at")))
check("列表项带 handle_hours", item and item[0].get("handle_hours") is not None)
base_total = c.get("total")
r2 = J(prop.get("/api/orders?q=赵师傅"))
c2 = (r2.get("data") or {}).get("counts") or {}
check("按处理人搜索能命中", c2.get("matched", 0) >= 1, c2.get("matched"))
check("搜索不改家底 total", c2.get("total") == base_total,
      f"{c2.get('total')} vs {base_total}")
check("搜索后 matched 小于 total", c2.get("matched", 0) < base_total,
      f"{c2.get('matched')} / {base_total}")

# ---------------------------------------------------------------- 8 权限
print("\n[8] 权限：街道只读，物业只能碰自己单位")
r = J(street.get(f"/api/orders/{nid}"))
check("街道能看流转记录", r.get("code") == 0, msg(r))
r = J(street.post("/api/orders/4/accept"))
check("街道不能接单", r.get("status_code") == 403 or r.get("code") != 0,
      f"http={r.get('status_code')} code={r.get('code')}")
r = J(street.get("/api/orders/handlers"))
check("街道不能取处理人名单", r.get("status_code") == 403 or r.get("code") != 0,
      f"http={r.get('status_code')}")
r = J(comm.post(f"/api/orders/4/accept"))
check("社区也不能接单（只有物业能办）",
      r.get("status_code") == 403 or r.get("code") != 0, f"http={r.get('status_code')}")

r = J(other.get("/api/orders/91"))
check("异单位物业能看自己单位的单", r.get("code") == 0, msg(r))
r = J(other.get(f"/api/orders/{nid}"))
check("异单位物业看不到别家单详情",
      r.get("status_code") == 403 or r.get("code") != 0, f"http={r.get('status_code')}")
r = J(other.post("/api/orders/4/accept"))
check("异单位物业不能接别家的单",
      r.get("status_code") == 403 or r.get("code") != 0, f"http={r.get('status_code')}")
check("越权尝试没改动工单状态", order(4).status == "open", order(4).status)

# ---------------------------------------------------------------- 9 处理人名单
print("\n[9] 处理人名单只给本单位、只给该看的人")
r = J(prop.get("/api/orders/handlers"))
rows = (r.get("data") or {}).get("items") or []
names = [x.get("name") for x in rows]
check("物业能取名单", r.get("code") == 0, msg(r))
check("名单含本单位同事", "赵师傅" in names, names)
check("名单不含异单位的人", "孙主管" not in names, names)
check("名单不含手机号等多余字段",
      all(set(x.keys()) == {"name", "username", "org"} for x in rows),
      rows[0] if rows else None)
check("名单按 items/counts 包着（不吐裸数组）",
      (r.get("data") or {}).get("counts", {}).get("total") == len(rows),
      r.get("data"))

# ---------------------------------------------------------------- 10 日志不改写
print("\n[10] 流转记录只追加，从不改写")
with app.app_context():
    rows = WorkOrderLog.query.filter_by(order_id=2).order_by(WorkOrderLog.id).all()
    seq = [(x.action, x.actor) for x in rows]
check("工单 2 的事件顺序：accept → finish", [s[0] for s in seq] == ["accept", "finish"], seq)
check("两条事件的操作人各自独立", seq[0][1] == "李经理" and seq[1][1] == "李经理", seq)
with app.app_context():
    all_logs = WorkOrderLog.query.all()
    total_logs = len(all_logs)
    no_missing_no = sum(1 for x in all_logs if not x.order_no)
check("每条日志都有 order_no（不必 join）", no_missing_no == 0, f"{no_missing_no} 条缺")
# 4（派单→接单→转派→办结）+ 2（交接班）+ 2（耗时）+ 1（老单补办结）
check("日志条数等于成功动作数", total_logs == 9, total_logs)

# ---------------------------------------------------------------- 收尾
print("\n" + "=" * 70)
print(f"结果：通过 {PASS} / 失败 {FAIL}")
if FAILED:
    print("失败项：")
    for f in FAILED:
        print("  -", f)
print("=" * 70)
sys.exit(1 if FAIL else 0)
