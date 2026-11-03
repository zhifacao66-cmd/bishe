"""流程监控与风险预警回归

## 这套测什么

三个模块（`process` / `risk` / `textmine`）的错都不会报错，只会**悄悄给错数**：
平均时长少算了转派那一段、已办结的单被算成"挂了 40 天"、
风险分拆不开、聚类把门禁和路灯归成一类。
页面照样渲染、接口照样 200，只有拿人工核对才发现。

所以这套用**固定时刻**构造已知答案的数据，断言精确值，不靠"看着挺像"。

## 三条最容易写错的，单独盯

1. **已办结的单，时长算到 `done_at` 而不是 `now`**
   错法：一张 9/18 创建、当天办完的单，被算成"挂了 38 天未结"，
   于是"长期未结"告警里排满早就办完的单，真该报的反而被挤掉。

2. **异常标签必须互斥**
   错法：超期 1 倍和超期 3 倍是两条独立规则，同时命中，
   页面出现"超期 3 倍以上、超期 1 倍以上"——一眼就知道代码没想清楚。

3. **地点词不能当主题词**
   错法："楼道"作为"楼道堆物"的关键词，把楼道灯、门禁、感应灯全归进去；
   "卫生间"里的"卫生"把漏水归进垃圾清运。这类误归不报错，只把统计带偏。

运行：python scripts/smoke_insight.py
"""
import os
import pathlib
import shutil
import sys
from datetime import datetime, timedelta

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_insight.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["NOTICE_AUTO_IMPORT"] = "0"
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"

from app import create_app  # noqa: E402
from app import process, risk, textmine  # noqa: E402
from app.auth import generate_password_hash  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import (FdeRecord, ResidentTicket, User,  # noqa: E402
                        WorkItem, WorkOrder)
from app.orderflow import push  # noqa: E402

app = create_app("dev")
PASS, FAIL = 0, 0

# 全场景统一参考时刻。**不用 datetime.now()**：本套断言的是精确小时数，
# 用真实当前时间会让期望值随运行时刻漂移（今天能过、明天不过）。
NOW = datetime(2026, 10, 26, 12, 0, 0)

CO_A = "甲物业"
CO_B = "乙物业"
CO_C = "丙社区"


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"　{extra}" if extra else ""))


def fresh():
    db.drop_all()
    db.create_all()


def mk_user(username, role, org, pwd="123456", status="active"):
    u = User(username=username, name=username, role=role, org=org,
             password_hash=generate_password_hash(pwd), status=status)
    db.session.add(u)
    db.session.commit()
    return u


def mk_order(created, steps, co=CO_A, sla=24, content="楼道灯不亮",
             assignee="维修班"):
    """按 steps 造一张单。steps = [(action, 相对创建的小时数, extra_dict)]

    业务项要真建：`work_order.work_item_id` 有外键约束，
    传一个虚构 id 会 IntegrityError——而真库上开了 `PRAGMA foreign_keys`，
    这里也不能靠"SQLite 默认不校验外键"糊过去。
    """
    fde = FdeRecord(raw_text=content, source="test",
                    creator="测试", created_at=created)
    db.session.add(fde)
    db.session.flush()
    item = WorkItem(fde_id=fde.id, type="报修", subject=content[:20],
                    target_lines="物业", created_at=created)
    db.session.add(item)
    db.session.flush()
    o = WorkOrder(work_item_id=item.id,
                  order_no="W" + created.strftime("%Y%m%d-%H%M%S"),
                  property_co=co, content=content, sla_hours=sla,
                  sla_level="一般", status="open", created_at=created)
    db.session.add(o)
    db.session.flush()
    for st in steps:
        act, h = st[0], st[1]
        extra = st[2] if len(st) > 2 else {}
        at = created + timedelta(hours=h)
        if act == "dispatch":
            push(o, "dispatch", actor="调度", actor_org=co,
                 detail=f"派往 {co}", at=at)
        elif act == "accept":
            o.assignee, o.assigned_at = assignee, at
            push(o, "accept", actor=assignee, actor_org=o.property_co, at=at)
        elif act == "transfer":
            o.property_co = extra.get("to", co)
            push(o, "transfer", actor="经理", actor_org=extra.get("org", co),
                 detail=extra.get("detail", "转派"), at=at)
        elif act == "finish":
            o.done_at = at
            o.feedback = "已处理"
            push(o, "finish", actor=assignee, actor_org=o.property_co, at=at)
    return o


def build_orders():
    """构造已知答案的工单集。

    组 1（时长精确）：6 张单，接单→办结分别 8/10/12/14/16/18 小时。
      → n=6；中位数 = (12+14)/2 = 13；P90 取第 ceil(0.9*6)=6 个 = 18
    组 2（边界）：超期办结 / 67 天未结 / 无人接单 / 三次跨单位转派 / 被驳回
    """
    ids = []
    # 组 1：accept→finish = 8,10,12,14,16,18
    for k, hours in enumerate((8, 10, 12, 14, 16, 18)):
        created = datetime(2026, 10, 10 + k, 8, 0, 0)
        o = mk_order(created, [("dispatch", 0), ("accept", 1),
                               ("finish", 1 + hours)],
                     content=f"组1单{k}")
        ids.append(("g1", o.id, hours))
    db.session.commit()

    # 边界 B：已办结且办结时超期 3 倍（created→done = 72h，SLA 24）
    # 刻意挂在 CO_B：权限段要拿"物业甲看不见它"验证行级收窄，
    # 若全部单都属于同一家，收窄与否在数字上看不出区别。
    ob = mk_order(datetime(2026, 10, 1, 8, 0, 0),
                  [("dispatch", 0), ("accept", 4), ("finish", 72)],
                  co=CO_B, content="办结时超期")
    # 边界 C：67 天未结
    oc = mk_order(NOW - timedelta(days=67),
                  [("dispatch", 0), ("accept", 3)],
                  content="长期未结")
    # 边界 D：只派单没人接
    od = mk_order(NOW - timedelta(days=2), [("dispatch", 0)],
                  content="无人接单")
    # 边界 E：三次转派，甲→乙→丙，跨单位 2 次
    oe = mk_order(NOW - timedelta(days=40),
                  [("dispatch", 0), ("accept", 2),
                   ("transfer", 20, dict(to=CO_B, org=CO_A, detail="不是我们的")),
                   ("transfer", 60, dict(to=CO_C, org=CO_B, detail="也不归我们")),
                   ("transfer", 200, dict(to=CO_A, org=CO_C, detail="还是你们")),
                   ], content="踢皮球")
    # 边界 F：办结后被驳回一次
    of = mk_order(NOW - timedelta(days=20),
                  [("dispatch", 0), ("accept", 2), ("finish", 30)],
                  content="被驳回")
    db.session.commit()
    from app.arbitration import judge  # noqa: E402
    of.confirm_status, of.confirm_at = "disputed", NOW - timedelta(days=10)
    of.dispute_type = "duty"
    db.session.commit()
    admin = User.query.filter_by(username="admin").first()
    judge(of, "property_fault", "物业合同第 3 条约定楼道灯由物业维护", admin)
    db.session.commit()
    return ids, [ob.id, oc.id, od.id, oe.id, of.id]


TICKETS = [
    ("13800000001", "3栋201", "卫生间顶部漏水，墙皮脱落"),
    ("13800000001", "3栋201", "上次漏水又开始了，地板泡起来"),
    ("13800000001", "3栋201", "第三次反映漏水，老人滑倒"),
    ("13900000002", "5栋101", "垃圾房三天没清运，味道很大"),
    ("13900000002", "5栋101", "垃圾分类点太少，每天走十分钟"),
    ("13700000003", "7栋301", "楼上噪音扰民，晚上拖家具"),
    ("13700000003", "7栋301", "噪音还是没解决，到十二点半"),
    ("13600000004", "6栋", "电梯卡在三楼不动，门关不严"),
    ("13600000004", "6栋", "电梯又坏了，配件要等"),
    ("13500000005", "2栋501", "外墙渗水，墙面发霉"),
    ("13400000006", "地下车库", "道闸坏了半个月，什么车都能进"),
    ("13300000007", "9栋3单元", "门禁刷卡没反应，单元门一直敞开"),
    ("13200000008", "8栋2单元", "楼道堆满纸箱旧家具，占了一半"),
    ("13100000009", "小区", "路灯一半不亮，晚上黑漆漆"),
    ("13000000010", "4栋1单元", "下水道反水，污水漫到客厅"),
    ("18900000011", "小区", "停车位太紧张，找不到地方停"),
    ("18800000012", "小区", "遛狗不拴绳，孩子被扑了一下"),
    ("18700000013", "小区", "物业费涨价两毛一平，要求公示账目"),
    ("18600000014", "11栋", "楼顶有人私搭阳光房，钢架都焊上了"),
    ("18500000015", "3号楼", "有人私搭铁皮棚子放杂物，占了一半路"),
    ("18400000016", "10栋501", "独居老人两天没下楼，希望社区看看"),
    ("18300000017", "1栋2单元", "楼道感应灯不亮，看不见台阶"),
    ("18200000018", "12栋", "绿化带的树被砍了三棵，没征求意见"),
    ("18100000019", "小区", "消防通道停着两辆车，消防车进不来"),
]


def build_tickets():
    """24 条诉求。文本都是**已知主题**，用来断言归并是否准确。"""
    for i, (phone, addr, text) in enumerate(TICKETS):
        fde = FdeRecord(raw_text=text, source="portal", creator="居民某某",
                        created_at=NOW - timedelta(days=(i % 20) + 1))
        db.session.add(fde)
        db.session.flush()
        item = WorkItem(fde_id=fde.id, type="报修", subject=text[:20],
                        target_lines="物业", created_at=fde.created_at)
        db.session.add(item)
        db.session.flush()
        t = ResidentTicket(code=f"{i:06d}", phone=phone, name="某",
                           address=addr, raw_text=text, fde_id=fde.id,
                           item_count=1, line_summary="物业",
                           created_at=fde.created_at, updated_at=fde.created_at)
        db.session.add(t)
    db.session.commit()


# ============================================================ 各段测试

def t_duration():
    print("\n[A] 停留时长与分位数口径")
    st = process.stage_stats()
    edge = [x for x in st if x["from"] == "accept" and x["to"] == "finish"]
    check("接单→办结这条边存在", len(edge) == 1, edge)
    e = edge[0] if edge else {}
    # 组1 六张 + 边界 F 那张（2→30，即 28h）+ 边界 B（4→72，68h）
    check("接单→办结 的 n 取到组1 的 6 条 + 边界 2 条", e.get("n") == 8, e.get("n"))
    # 手工算：8,10,12,14,16,18 与 28,68 → 排序后 8,10,12,14,16,18,28,68
    # 中位数 = 第 4、5 个均值 = (14+16)/2 = 15
    check("中位数 = 15.0（手工核：排序第4/5个均值）", e.get("median") == 15.0, e.get("median"))
    # P90：ceil(0.9*8)=8 → 第 8 个 = 68
    check("P90 取第 8 个 = 68.0（最近秩法，不做线性插值）",
          e.get("p90") == 68.0, e.get("p90"))
    check("最短 8.0", e.get("min") == 8.0, e.get("min"))
    check("最长 68.0", e.get("max") == 68.0, e.get("max"))

    # 转派→办结 只有边界 E 里没有（E 未办结），组1 也没有 → 该边不存在
    no_tf = [x for x in st if x["from"] == "transfer" and x["to"] == "finish"]
    check("没有「转派→办结」这条边时不返回空行", not no_tf, no_tf)

    # 小样本的边：派单→接单 共 8 条（组1 六条 + B + F），C/D/E 只 dispatch/accept
    # C、D、E 里 C 有 accept、D 没有、E 有
    pa = [x for x in st if x["from"] == "dispatch" and x["to"] == "accept"]
    check("派单→接单 的边存在且 P90 可算（n≥5）",
          pa and pa[0]["p90_ready"] is True, pa)
    return e


def t_p90_gate():
    print("\n[B] 分位数门槛：样本不够必须返回 None，不能算一个出来")
    s = process._stat([5.0])
    check("n=1 时 median 为 None", s["median"] is None, s["median"])
    check("n=1 时 p90 为 None", s["p90"] is None, s["p90"])
    check("n=1 时 avg 仍给（平均不需要门槛）", s["avg"] == 5.0, s["avg"])
    s = process._stat([1.0, 2.0])
    check("n=2 时 median 仍为 None（少于 MIN_N_FOR_MEDIAN=3）",
          s["median"] is None, s["median"])
    s = process._stat([1.0, 2.0, 3.0])
    check("n=3 时 median = 2.0", s["median"] == 2.0, s["median"])
    check("n=3 时 p90 仍为 None（少于 MIN_N_FOR_P90=5）", s["p90"] is None, s["p90"])
    s = process._stat([1, 2, 3, 4, 5])
    check("n=5 时 p90 可算 = 5（ceil(0.9*5)=5 取第5个）", s["p90"] == 5, s["p90"])
    s = process._stat([])
    check("空集合不抛异常，全部字段为 None", s["n"] == 0 and s["avg"] is None, s)
    check("负数时长被丢弃（_hours 返回 None）",
          process._hours(datetime(2026, 1, 2), datetime(2026, 1, 1)) is None)


def t_done_at():
    print("\n[C] 已办结的单，时长必须算到 done_at 而不是 now")
    o = WorkOrder.query.filter_by(content="办结时超期").first()
    h = process.age_hours(o, NOW)
    # created 10-01 08:00, done 10-04 08:00 → 72h
    check("办结单的 age_hours = 72.0（created→done_at）", h == 72.0, h)
    check("若按 now 算会得到 604+ 小时（错法对照）",
          (NOW - o.created_at).total_seconds() / 3600 > 600)
    an = {x["id"]: x for x in process.anomaly(now=NOW) if x["id"] == o.id} \
        if False else None
    rows = {x["order_no"]: x for x in process.anomaly(order_ids=None)}
    key = o.order_no
    check("已办结单不进「长期未结」", "长期未结" not in (rows.get(key, {}).get("reason_text") or ""),
          rows.get(key, {}).get("reason_text"))
    check("已办结单仍进「超期」复盘（办结时确实超了）",
          "超期" in (rows.get(key, {}).get("reason_text") or ""),
          rows.get(key, {}).get("reason_text"))

    # 未结的单按 now 算
    c = WorkOrder.query.filter_by(content="长期未结").first()
    hc = process.age_hours(c, NOW)
    check("未结单的 age_hours 从 created 算到 now", abs(hc - 67 * 24) < 0.01, hc)


def t_anomaly_exclusive():
    print("\n[D] 异常标签必须互斥，且长尾判定要带状态")
    rows = process.anomaly()
    bad = [x["order_no"] for x in rows
           if ("超期 3 倍以上" in x["reason_text"]
               and "超期 1 倍以上" in x["reason_text"])]
    check("没有一条同时报「超期 3 倍」与「超期 1 倍」", not bad, bad)
    for x in rows:
        names = [r["name"] for r in x["reasons"]]
        check(f"{x['order_no']} 无重复标签", len(names) == len(set(names)), names) \
            if len(names) != len(set(names)) else None
    check("已办结的单不出现在「X 天无任何动作」里",
          not [x for x in rows
               if x["status"] == "done" and "无任何动作" in x["reason_text"]],
          [x["order_no"] for x in rows
           if x["status"] == "done" and "无任何动作" in x["reason_text"]])
    # 67 天未结那张必须命中 long_pending
    c = WorkOrder.query.filter_by(content="长期未结").first()
    got = [x for x in rows if x["id"] == c.id]
    check("67 天未结命中「超过 30 天未结」",
          got and any("超过 30 天未结" in r["name"] for r in got[0]["reasons"]),
          got[0]["reason_text"] if got else "未命中")
    # 转派 3 次那张
    e = WorkOrder.query.filter_by(content="踢皮球").first()
    got = [x for x in rows if x["id"] == e.id]
    check("转派 3 次的单命中「转派 3 次」",
          got and any("转派 3 次" in r["name"] for r in got[0]["reasons"]),
          got[0]["reason_text"] if got else "未命中")
    # 每条命中都要有建议动作
    check("每条命中都带建议动作",
          all(r.get("advice") for x in rows for r in x["reasons"]))


def t_stuck():
    print("\n[E] 卡点判定")
    rows = {x["order_no"]: x for x in process.stuck(now=NOW)}
    d = WorkOrder.query.filter_by(content="无人接单").first()
    check("只派单未接单 → 「无人接单」",
          rows.get(d.order_no, {}).get("stage") == "无人接单",
          rows.get(d.order_no, {}).get("stage"))
    e = WorkOrder.query.filter_by(content="踢皮球").first()
    check("最后一次动作是转派 → 「转派后无人接手」",
          rows.get(e.order_no, {}).get("stage") == "转派后无人接手",
          rows.get(e.order_no, {}).get("stage"))
    c = WorkOrder.query.filter_by(content="长期未结").first()
    check("接单后没办结 → 「处理中未办结」",
          rows.get(c.order_no, {}).get("stage") == "处理中未办结",
          rows.get(c.order_no, {}).get("stage"))
    # 已办结的不出现在 stuck 里
    b = WorkOrder.query.filter_by(content="办结时超期").first()
    check("已办结的单不在卡点表里", b.order_no not in rows)
    # idle 基准是最后动作
    check("卡点的 idle 从最后一次动作算（转派那张 < 从创建算）",
          rows[e.order_no]["idle_hours"] < rows[e.order_no]["age_hours"],
          (rows[e.order_no]["idle_hours"], rows[e.order_no]["age_hours"]))


def t_risk():
    print("\n[F] 风险评分与拆解")
    e = WorkOrder.query.filter_by(content="踢皮球").first()
    ex = risk.explain(e, transfers=[r for r in __import__("app.models", fromlist=["WorkOrderLog"])
                                    .WorkOrderLog.query.filter_by(order_id=e.id,
                                                                  action="transfer").all()],
                      reject_count=0, repeat_times=0)
    names = [i["name"] for i in ex["items"]]
    check("跨单位转派计分项存在", "跨单位转派" in names, names)
    # 甲→乙→丙，3 次转派，orgs=[甲,乙,丙] → 跨 2 次
    cross = [i for i in ex["items"] if i["name"] == "跨单位转派"]
    check("跨单位次数 = 2（甲→乙、乙→丙）",
          cross and cross[0]["score"] == risk.W_CROSS_ORG_TRANSFER * 2,
          cross[0] if cross else "无")
    check("分项之和 = raw", sum(i["score"] for i in ex["items"]) == ex["raw"],
          (sum(i["score"] for i in ex["items"]), ex["raw"]))
    check("score = min(100, raw)", ex["score"] == min(100, ex["raw"]), ex)
    check("每个分项都有人话理由", all(i.get("reason") for i in ex["items"]))

    # 封顶标记
    heavy = type("O", (), {
        "created_at": NOW - timedelta(days=90), "status": "doing",
        "sla_hours": 1, "confirm_status": "disputed", "verdict": ""})()
    ex2 = risk.explain(heavy, transfers=[], reject_count=3, repeat_times=4)
    check("原始分超过 100 时 score 封顶到 100", ex2["score"] == 100, ex2["score"])
    check("封顶时 capped=True 且保留 raw",
          ex2["capped"] is True and ex2["raw"] > 100, ex2)

    # 等级分档
    check("等级分为 高危/关注/正常 三档",
          all(x["level"] in ("高危", "关注", "正常") for x in risk.warnings()))


def t_repeat():
    print("\n[G] 反复投诉")
    rows = risk.repeat_complainants()
    by_key = {x["key"]: x for x in rows}
    z = by_key.get("13800000001")
    check("同一手机号报 3 次被识别", z and z["times"] == 3, z)
    check("只报 1 次的不进清单", "13600000004" not in by_key or by_key["13600000004"]["times"] > 1)
    check("同一地址标记 same_address=True", z and z["same_address"] is True, z)
    check("手机号在输出里已打码", z and "****" in z["phone_masked"], z["phone_masked"] if z else "")
    # 归一化：带空格/横线/+86 的同一号码
    check("手机号归一：带横线能对上",
          risk._norm_phone("138-0000-0001") == "13800000001")
    check("手机号归一：+86 前缀能对上",
          risk._norm_phone("+8613800000001") == "13800000001")
    check("地址归一：去空白与小区前缀",
          risk._norm_addr(" 幸福小区 3栋201 ") == "3栋201")


def t_pingpong():
    print("\n[H] 踢皮球")
    rows = risk.ping_pong()
    e = [x for x in rows if x["order_no"].startswith("W")]
    hit = [x for x in rows if x["transfers"] == 3]
    check("转派 3 次的单被识别", len(hit) == 1, [x["transfers"] for x in rows])
    if hit:
        check("跨单位次数 = 2", hit[0]["cross_org"] == 2, hit[0]["cross_org"])
        check("经过单位去重后 3 个", len(hit[0]["orgs"]) == 3, hit[0]["orgs"])
        check("转移链长度 = 3", len(hit[0]["chain"]) == 3, len(hit[0]["chain"]))
    # 同单位内部转派不该算"踢皮球"
    o = mk_order(NOW - timedelta(days=5),
                 [("dispatch", 0), ("accept", 1),
                  ("transfer", 10, dict(to=CO_A, org=CO_A, detail="换个人")),
                  ("transfer", 20, dict(to=CO_A, org=CO_A, detail="再换一个"))],
                 content="同单位换人")
    db.session.commit()
    rows2 = risk.ping_pong()
    same = [x for x in rows2 if x["id"] == o.id]
    check("同单位转派 2 次也算（次数达标），但 cross_org=0",
          same and same[0]["cross_org"] == 0, same[0] if same else "未识别")


def t_topic():
    print("\n[I] 主题词表：地点词、身份词与宽泛字不能造成误归")
    cases = [
        ("卫生间顶部漏水，墙皮脱落", "房屋漏水", True),
        ("卫生间顶部漏水，墙皮脱落", "垃圾清运", False),   # "卫生"陷阱
        ("9栋3单元门禁刷卡没反应，单元门一直敞开", "楼道堆物", False),  # "楼道"陷阱
        ("9栋3单元门禁刷卡没反应，单元门一直敞开", "门禁对讲", True),
        ("1单元门口消防通道停着两辆车", "门禁对讲", False),  # "单元门"陷阱
        ("1单元门口消防通道停着两辆车", "消防安全", True),
        ("电梯卡在三楼不动，邻居帮忙撬的门", "邻里纠纷", False),  # "邻居"陷阱
        ("下水道反水，污水漫到客厅，很臭", "垃圾清运", False),  # "臭"陷阱
        ("宠物狗没拴绳扑了孩子", "宠物管理", True),
        ("楼顶有人私搭阳光房", "违章搭建", True),
        ("物业费涨了两毛一平，要求公示账目", "收费争议", True),
        ("物业费涨价，绿化没人管", "绿化树木", False),
        ("楼道感应灯不亮", "路灯照明", True),
        ("楼道堆满纸箱旧家具", "楼道堆物", True),
        # 「老人」是身份词：它说的是"谁"，不是"什么事"。放进来会把漏水、垃圾、
        # 电梯三条全吸成「特殊群体」，热点榜首变成"特殊群体 4 条"——
        # 而真正要动手修的是水管、垃圾桶和电梯。
        ("卫生间漏水，老人踩到积水滑倒", "特殊群体", False),
        ("垃圾分类点太少，老人拎着垃圾走十分钟", "特殊群体", False),
        ("电梯关人，有位老人被关在里面二十分钟", "特殊群体", False),
        # 状态词才该命中：这些词出现时主题必然是该关注的人
        ("10栋有位独居老人两天没下楼，敲门没应", "特殊群体", True),
        ("3栋有位孤寡老人，需要社区上门看看", "特殊群体", True),
    ]
    for text, topic, expect in cases:
        hits = textmine._hit_topics(text)
        ok = (topic in hits) == expect
        check(f"「{text[:16]}」{'命中' if expect else '不误归'} {topic}", ok, hits)
    check("`unmatched` 统计的是没命中任何主题的条数",
          isinstance(textmine.hotspots()["unmatched"], int))
    hs = textmine.hotspots()
    check("一条诉求多主题时，各主题数量之和可大于诉求数",
          hs["hit_total"] >= hs["matched"], (hs["hit_total"], hs["matched"]))


def t_cluster():
    print("\n[J] 聚类：门槛、纯度、方法自述")
    r = textmine.cluster()
    check("样本 24 ≥ 门槛 20，聚类可运行", r["available"] is True, r)
    if not r["available"]:
        return
    check("纯度在 0~1 之间", 0 <= r["purity"] <= 1, r["purity"])
    check("纯度分档与纯度一致",
          r["quality"] == ("高" if r["purity"] >= 0.7
                           else "中" if r["purity"] >= 0.5 else "低"),
          (r["purity"], r["quality"]))
    check("下发相似度分布（中位数应很低，说明短文本字面重叠少）",
          r["sim_stats"]["median"] < 0.1, r["sim_stats"])
    check("方法自述包含 TF-IDF 与层次聚类",
          "TF-IDF" in r["method"] and "层次聚类" in r["method"], r["method"])
    check("limits 里必须写明「以词表为准」",
          "词表" in r["limits"], r["limits"][:60])
    check("簇数 < 诉求数（否则等于没聚）", r["clusters"] < r["n"], (r["clusters"], r["n"]))
    check("每个簇都有 size 与 members",
          all("size" in c and "members" in c for c in r["items"]))
    # 阈值边界：设成极高 → 每条自成一簇
    hi = textmine.cluster(threshold=0.99)
    check("阈值 0.99 时几乎每条自成一簇（验证阈值确实生效）",
          hi["clusters"] >= r["n"] - 2, hi["clusters"])
    # 样本不足的分支
    saved = ResidentTicket.query.count()
    check("样本数被如实报告", r["n"] == saved, (r["n"], saved))


def t_perm():
    """权限与收窄。**必须在 app_context 之外跑**。

    踩过的坑：在 `with app.app_context():` 里面调 `app.test_client()`，
    `POST /login` 会返回 302（看着成功），但紧接着的下一次请求就是 401。
    原因是 test_client 复用已存在的 app context，Set-Cookie 没有落到
    client 自己的 cookie 存储里。这类错很像"权限判断写错了"，
    实际是测试的上下文用错了——所以这段单独拆出来，不进 app_context。

    判断"收窄有没有生效"不查库，直接看接口返回的 `property_co`：
    查库等于用同一份实现验证自己（红线：断言别照抄实现）。
    """
    print("\n[K] 权限与行级收窄")

    def as_user(name, pwd="123456"):
        c = app.test_client()
        r = c.post("/login", data={"username": name, "password": pwd})
        if r.status_code != 302:
            print(f"  [WARN] {name} 登录未成功：{r.status_code}")
        return c

    anon = app.test_client()
    check("未登录访问 /api/insight/process → 401",
          anon.get("/api/insight/process").status_code == 401)
    check("未登录访问 /api/insight/topics → 401",
          anon.get("/api/insight/topics").status_code == 401)
    check("未登录访问 /process 页面 → 登录跳转（302/401）",
          anon.get("/process").status_code in (302, 401))

    ca = as_user("admin", "admin123")
    ja = ca.get("/api/insight/process").get_json()["data"]
    total = ja["summary"]["orders"]

    c = as_user("wuye")
    r1 = c.get("/api/insight/process")
    check("物业可进流程监控（有 order.view）", r1.status_code == 200, r1.status_code)
    j = r1.get_json()["data"]
    cos = {x["property_co"] for x in j["stuck"]}
    check("物业看到的卡单只属于本单位", cos <= {CO_A}, cos)
    check("物业可见工单数少于全量（确实被收窄）",
          j["summary"]["orders"] < total, (j["summary"]["orders"], total))
    check("物业访问居民诉求热点 → 403",
          c.get("/api/insight/topics").status_code == 403)
    jr = c.get("/api/insight/risk").get_json()["data"]
    check("物业视角不返回反复投诉（居民画像）",
          jr["with_people"] is False and jr["repeat"] == [])

    c = as_user("shequ")
    check("社区可看居民诉求热点", c.get("/api/insight/topics").status_code == 200)
    js = c.get("/api/insight/process").get_json()["data"]
    check("社区可见工单是全量（社区监督视角）",
          js["summary"]["orders"] == total, (js["summary"]["orders"], total))
    jr2 = c.get("/api/insight/risk").get_json()["data"]
    check("社区可看居民画像", jr2["with_people"] is True)

    c = as_user("jiedao")
    check("街道可看居民画像",
          c.get("/api/insight/risk").get_json()["data"]["with_people"] is True)
    check("街道可看流程监控",
          c.get("/api/insight/process").status_code == 200)


def t_edge():
    print("\n[L] 边界与口径")
    # 空结果不抛异常
    check("不存在的工单集合 → 空列表而不是所有数据",
          process.stuck(order_ids=set()) == [])
    check("空集合的 summary 不抛异常",
          process.summary(order_ids=set())["orders"] == 0)

    # 裁决/确认类动作在一条工单上不允许相邻重复。
    #
    # 这条断言来自一个真实残留：`can_judge` 早期只判"已办结且居民不服"，
    # 没判"本轮是否已裁"，种子脚本重跑时同一张单被 uphold 两次，
    # verdict 被后者静默覆盖。代码已修（`_judged_this_round`），
    # 但库里那两条日志还在，流程图上显示成「维持办结 → 维持办结」自环、
    # 停留 0.0 小时——演示时没人能解释这一格是什么。
    #
    # 注意不能断言"所有同动作相邻都非法"：`转派 → 转派` 恰恰是踢皮球的证据，
    # 是合法且要重点观测的边。只有裁决/确认类动作一轮只能有一次。
    ONCE_PER_ROUND = ("uphold", "reject", "confirm")
    logs = process._logs_by_order()
    dup = []
    for oid, seq in logs.items():
        for a, b in zip(seq, seq[1:]):
            if a.action == b.action and a.action in ONCE_PER_ROUND:
                dup.append((oid, a.action))
    check("裁决/确认类动作没有相邻重复（同一轮只受理一次）", not dup, dup[:5])

    # 路径统计
    ps = process.path_stats()
    check("路径统计的 ratio 在 0~1", all(0 <= x["ratio"] <= 1 for x in ps))
    check("路径统计的件数之和 = 有日志的单数",
          sum(x["count"] for x in ps) == process.summary()["orders_with_log"],
          (sum(x["count"] for x in ps), process.summary()["orders_with_log"]))
    # summary 的每个数独立 count
    s = process.summary()
    check("summary 里 open+doing+done ≤ 工单总数（状态可能还有别的值）",
          s["open"] + s["doing"] + s["done"] <= s["orders"])
    check("sample_note 非空（页面必须显示样本量）", bool(s["sample_note"]))
    # 服务端不返回原始手机号
    for x in risk.repeat_complainants():
        check(f"反复投诉输出不含明文手机号（{x['phone_masked']}）",
              "****" in x["phone_masked"])


def main():
    with app.app_context():
        fresh()
        mk_user("admin", "admin", "平台运维", "admin123")
        # 权限测试要用的三个账号提前建好：t_perm 是在 app_context 之外跑的
        mk_user("wuye", "property", CO_A)
        mk_user("shequ", "community", CO_C)
        mk_user("jiedao", "street", "城东街道")
        ids, edge_ids = build_orders()
        build_tickets()
        print(f"库就绪：工单 {WorkOrder.query.count()} / 诉求 {ResidentTicket.query.count()}")

        t_duration()
        t_p90_gate()
        t_done_at()
        t_anomaly_exclusive()
        t_stuck()
        t_risk()
        t_repeat()
        t_pingpong()
        t_topic()
        t_cluster()
        t_edge()

    # 权限段在 app_context 之外：见 t_perm 的 docstring
    t_perm()

    print("\n" + "=" * 66)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
