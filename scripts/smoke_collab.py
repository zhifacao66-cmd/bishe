"""多 Agent 协作（黑板 + 冲突消解）回归

## 这套测什么

`app/agents/blackboard.py` 和 `app/agents/riskreview.py` 的错都属于
**不会报错、只会静默给错结论**那一类：

- 消解规则写反（取更宽的一方）→ 单子照样建出来，只是时限从 4h 变 72h，
  一个月后才发现"总在超期"；
- 判重键不归一化（"501" vs "501室"）→ 重复报修永远数成 0，功能等于没写；
- 升档用 `dict.get(n, 0)` → 报修第 4 次反而不升级（这个真踩过）；
- 把"意见一致"记成"消解成功" → 页面上看起来每次协作都很顺畅，实际没冲突。

所以这套的期望值是**手工算好的等级和小时数**，不是"看着对"。

## 不依赖模型

`OLLAMA_BASE_URL` 指向一个关掉的端口，parser 必然走规则降级。
组 C 直接调 `Orchestrator._dispatch_with_review()`，绕过拆解环节，
这样测的是"协作"，不是"模型拆得准不准"——那是 smoke_rigor / 评测脚本的事。

运行：python scripts/smoke_collab.py
"""
import os
import pathlib
import sys
from datetime import datetime, timedelta

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_collab.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["NOTICE_AUTO_IMPORT"] = "0"
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"   # 强制降级

from app import create_app  # noqa: E402
from app.agents import blackboard as BB  # noqa: E402
from app.agents.blackboard import Blackboard, SLA_HOURS, new_run_id  # noqa: E402
from app.agents.riskreview import (RiskReviewAgent, addr_parts,  # noqa: E402
                                   category_of, same_place, _up, SAFETY_WORDS)
from app.auth import generate_password_hash  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import (AgentStep, FdeRecord, User, WorkItem,  # noqa: E402
                        WorkOrder, WorkOrderLog)

app = create_app("dev")
PASS, FAIL = 0, 0

CO_A = "甲物业"


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


def mk_item(subject, created=None):
    """建一条业务项。work_order.work_item_id 有外键，必须真建 FDE → WorkItem"""
    fde = FdeRecord(raw_text=subject, source="test", creator="测试",
                    created_at=created or datetime.now())
    db.session.add(fde)
    db.session.flush()
    wi = WorkItem(fde_id=fde.id, type="报修", subject=subject,
                  target_lines="物业", created_at=created or datetime.now())
    db.session.add(wi)
    db.session.flush()
    return fde, wi


def mk_order(content, hours_ago=0, co=CO_A, sla=24):
    """造一张历史工单（模拟"这户以前报过"）"""
    fde = FdeRecord(raw_text=content, source="test", creator="测试")
    db.session.add(fde)
    db.session.flush()
    wi = WorkItem(fde_id=fde.id, type="报修", subject=content,
                  target_lines="物业")
    db.session.add(wi)
    db.session.flush()
    o = WorkOrder(work_item_id=wi.id,
                  order_no="W" + datetime.now().strftime("%Y%m%d%H%M%S") + f"-{wi.id:03d}",
                  property_co=co, content=content, sla_hours=sla,
                  sla_level="一般", status="open",
                  created_at=datetime.now() - timedelta(hours=hours_ago))
    db.session.add(o)
    db.session.commit()
    return o


# ====================================================================== A 黑板

def t_blackboard():
    print("\n[A] 黑板：只有真分歧才产生消解")

    bb = Blackboard()
    check("空黑板没有表态，resolve 返回 None",
          bb.resolve("sla") is None)

    bb.post("dispatch", "sla", ("一般", 72), reason="词表命中「垃圾」")
    check("只有一方表态 → 不消解（一致不等于裁过）",
          bb.resolve("sla") is None)
    check("单方表态时 conflicts 为空",
          bb.conflicts() == [])

    bb.post("risk", "sla", ("一般", 72), reason="无异议")
    check("两方结论相同 → 仍然不消解",
          bb.resolve("sla") is None)

    # 真分歧
    bb2 = Blackboard()
    bb2.post("dispatch", "sla", ("一般", 72), reason="词表命中")
    bb2.post("risk", "sla", ("紧急", 4), reason="这户报过 3 次")
    # conflicts 必须在 resolve **之前**查：resolve 会把该议题移出待议区
    # （否则同一诉求拆出的第 2 个业务项会跟第 1 项的表态混着比大小）
    check("conflicts 能列出分歧议题",
          bb2.conflicts() and bb2.conflicts()[0]["topic"] == "sla")
    check("conflicts 列出双方 Agent",
          bb2.conflicts()[0]["agents"] == ["dispatch", "risk"])
    res = bb2.resolve("sla")
    check("两方结论不同 → 产生消解", res is not None)
    check("更严者胜：采纳紧急 4h", res and res["value"] == "紧急 4h", res and res["value"])
    check("winner 是表态更严的 risk", res and res["winner"] == "risk")
    check("losers 记下被否的一方", res and res["losers"] == ["dispatch"])
    check("消解理由包含两方各自的依据",
          res and "词表命中" in res["reason"] and "报过 3 次" in res["reason"])
    check("claims 完整保留两条表态", res and len(res["claims"]) == 2)
    check("消解后该议题移出待议区（避免污染下一个业务项）",
          bb2.claims_on("sla") == [])
    check("消解记录本身仍然留着", len(bb2.resolutions) == 1)

    # 顺序无关：先风控后派单，结论必须一样
    bb3 = Blackboard()
    bb3.post("risk", "sla", ("紧急", 4), reason="先表态")
    bb3.post("dispatch", "sla", ("较急", 24), reason="后表态")
    res3 = bb3.resolve("sla")
    check("表态顺序不影响结果（后说的宽松结论不能翻盘）",
          res3 and res3["value"] == "紧急 4h", res3 and res3["value"])

    # 未知等级不能赢
    bb4 = Blackboard()
    bb4.post("dispatch", "sla", ("一般", 72), reason="正常")
    bb4.post("risk", "sla", ("严重", 2), reason="未知等级")
    res4 = bb4.resolve("sla")
    check("认不出的等级按最宽处理，不会赢过已知等级",
          res4 and res4["value"] == "一般 72h", res4 and res4["value"])

    # 记录与落库
    bb5 = Blackboard()
    bb5.post("dispatch", "sla", ("较急", 24), reason="测格式化")
    check("post 的 tuple 会格式化成「较急 24h」",
          bb5.claims and bb5.claims[0]["decision"] == "较急 24h",
          bb5.claims and bb5.claims[0]["decision"])
    check("消解本身也记一步轨迹",
          any(s["agent"] == "resolve" for s in bb2.steps))
    rows = bb2.pending_rows()
    check("pending_rows 行数 = 轨迹步数", len(rows) == len(bb2.steps),
          f"{len(rows)} vs {len(bb2.steps)}")
    check("同一块黑板的 run_id 一致",
          len({r.run_id for r in rows}) == 1)

    rid = new_run_id()
    check("run_id 形如 R1026-ABCD",
          rid.startswith("R") and "-" in rid and len(rid) == 10, rid)
    check("两次生成的 run_id 不同", new_run_id() != rid)

    check("severity_of 更严的等级数值更小",
          BB.severity_of("紧急") < BB.severity_of("较急") < BB.severity_of("一般"))
    check("severity_of 对未知等级返回 9", BB.severity_of("天塌了") == 9)


# ================================================================ B 风险复核

def t_review():
    print("\n[B] 风险复核：三个信号各自的触发条件")
    fresh()
    a = RiskReviewAgent()

    # 无信号必须弃权
    r = a.review({"subject": "2栋2单元202楼道灯不亮", "level": "一般"})
    check("没有信号 → level=None（不硬凑结论）", r["level"] is None, r)
    check("弃权时 signals 为空", r["signals"] == [])

    # 安全词
    r = a.review({"subject": "2栋1单元302有燃气味", "level": "一般"})
    check("安全词「燃气」→ 直接紧急", r["level"] == "紧急", r["level"])
    check("安全词置信度 0.95", r["confidence"] == 0.95, r["confidence"])
    check("安全词不受原定级影响（原定级再严也还是紧急）",
          a.review({"subject": "2栋1单元302有燃气味", "level": "紧急"})["level"] is None,
          "原定级已是最严时无需表态")
    check("「滑倒」也算安全隐患",
          a.review({"subject": "2栋2单元202地面有水老人滑倒",
                    "level": "较急"})["level"] == "紧急")
    check("常用词不误命中：「堵车」不算隐患",
          "堵车" not in "".join(SAFETY_WORDS))

    # 门牌归一化
    full = lambda s: addr_parts(s)["full"]
    check("501 与 501室 归一成同一个键",
          full("2栋2单元202室漏水") == full("2栋2单元202卫生间漏水")
          == "2栋2单元202", full("2栋2单元202室漏水"))
    check("不同门牌不串：501 与 502 是两户",
          full("2栋2单元202") != full("3栋2单元502"))
    check("12栋 不被当成 2栋",
          full("12栋1单元301") != full("2栋1单元301"))

    # 重复报修梯度（含封顶）。基线是「一般」，所以：
    #   历史 1 张（第 2 次报）→ 升 1 档 → 较急
    #   历史 2 张以上（第 3 次起）→ 升 2 档 → 紧急
    for n in range(0, 6):
        fresh()
        for i in range(n):
            mk_order("2栋2单元202卫生间漏水", hours_ago=10 + i)
        r = a.review({"subject": "2栋2单元202又漏水了", "level": "一般"})
        want = None if n == 0 else ("较急" if n == 1 else "紧急")
        check(f"历史 {n} 张单 → "
              + ("不表态" if want is None else f"升到 {want}")
              + ("（封顶 2 档）" if n >= 2 else ""),
              r["level"] == want, r["level"])

    # 封顶不倒退：第 4 次报修必须仍然升级
    fresh()
    for i in range(4):
        mk_order("2栋2单元202漏水", hours_ago=10 + i)
    r = a.review({"subject": "2栋2单元202漏水了", "level": "一般"})
    check("报修第 5 次仍然升级（dict.get(n,0) 的坑）",
          r["level"] == "紧急", r["level"])

    # 超窗不算
    fresh()
    mk_order("2栋2单元202漏水", hours_ago=24 * 40)
    r = a.review({"subject": "2栋2单元202又漏水", "level": "一般"})
    check("40 天前的单不算进 30 天窗口", r["level"] is None, r["level"])

    # 别的门牌不串
    fresh()
    for i in range(3):
        mk_order("2栋1单元301漏水", hours_ago=10 + i)
    r = a.review({"subject": "2栋2单元202楼道灯不亮", "level": "一般"})
    check("2栋的历史不影响 3栋的复核（同址才同源）", r["level"] is None, r["level"])

    # ---- 地址分级：真实诉求大多没有门号
    check("三级识别：带门号 → level 3",
          addr_parts("2栋2单元202漏水")["level"] == 3)
    check("三级识别：只有栋+单元 → level 2（电梯/楼道灯的常见写法）",
          addr_parts("7栋3单元电梯异响")["level"] == 2)
    check("三级识别：只有栋 → level 1", addr_parts("9栋门禁损坏")["level"] == 1)
    check("三级识别：没有地址 → level 0", addr_parts("楼道灯不亮")["level"] == 0)

    check("类别：电梯", category_of("7栋3单元电梯异响") == "电梯")
    check("类别：供水（下水道也算）", category_of("2栋2单元202下水道堵塞") == "供水")
    check("类别：照明（楼道灯不能被「楼道」带走）",
          category_of("12栋302楼道灯故障") == "照明")
    check("类别：认不出来就返回空", category_of("2栋2单元202第三次了") == "")

    # 公共设施：没有门号也要能判重
    fresh()
    for i in range(2):
        mk_order("7栋3单元电梯异响", hours_ago=10 + i)
    r = a.review({"subject": "7栋3单元电梯又响了", "level": "较急"})
    check("公共设施无门号也能判重：电梯报 3 次 → 升到紧急",
          r["level"] == "紧急", r["level"])

    # 同址不同类不算
    fresh()
    for i in range(2):
        mk_order("2栋2单元202卫生间漏水", hours_ago=10 + i)
    r = a.review({"subject": "2栋2单元202电梯坏了", "level": "较急"})
    check("同址不同类不算重复：漏水 3 次不等于电梯坏 3 次",
          r["level"] is None, r["level"])

    # 住宅类必须有门号
    fresh()
    for i in range(2):
        mk_order("3栋2单元漏水", hours_ago=10 + i)
    r = a.review({"subject": "3栋2单元又漏水", "level": "一般"})
    check("住宅类缺门号 → 不判重（分不清 501 和 502）",
          r["level"] is None, r["level"])

    # 单元不同不串
    fresh()
    for i in range(2):
        mk_order("7栋2单元电梯异响", hours_ago=10 + i)
    r = a.review({"subject": "7栋3单元电梯异响", "level": "较急"})
    check("同栋不同单元不串：7栋2单元 ≠ 7栋3单元",
          r["level"] is None, r["level"])

    # 升档函数
    check("_up 升一档：一般 → 较急", _up("一般") == "较急")
    check("_up 升两档：一般 → 紧急", _up("一般", 2) == "紧急")
    check("_up 已在最严则原地不动", _up("紧急", 2) == "紧急")

    # 置信度梯度
    fresh()
    mk_order("2栋2单元202漏水", hours_ago=5)
    r = a.review({"subject": "2栋2单元202又漏水", "level": "一般"})
    check("重复报修置信度 0.8（低于安全词）", r["confidence"] == 0.8, r["confidence"])

    check("时限表与派单同源",
          SLA_HOURS == {"紧急": 4, "较急": 24, "一般": 72})


# ============================================================== C 调度器集成

def t_orchestrator():
    print("\n[C] 调度器：定级在建单之前，工单与消解结论必须一致")
    fresh()

    from app.agents.orchestrator import Orchestrator
    orch = Orchestrator()
    bb = Blackboard()

    # 第 1 次：无历史，两个 Agent 应该一致（或复核弃权）
    _, wi1 = mk_item("2栋2单元202卫生间漏水")
    od1 = orch._dispatch_with_review(bb, wi1, 1)
    check("第一次报修按词表定「较急 24h」",
          od1["sla_level"] == "较急" and od1["sla_hours"] == 24,
          f"{od1['sla_level']} {od1['sla_hours']}")
    check("第一次没有冲突，不产生消解", od1["upgraded"] is False)
    check("无冲突时 note 明说「复核无异议」", "无异议" in od1["sla_note"], od1["sla_note"])

    # 第 2 次：同门牌已有一张单 → 升一档到紧急
    # subject 必须含「漏水」才会先定到较急；写成「还是漏」只会命中「一般 72h」，
    # 升一档到 24h 就停了——那是测试自己的期望写错，不是代码错（真踩过）
    _, wi2 = mk_item("2栋2单元202室卫生间还是漏水")
    od2 = orch._dispatch_with_review(bb, wi2, 2)
    check("第二次报修被提级到「紧急 4h」",
          od2["sla_level"] == "紧急" and od2["sla_hours"] == 4,
          f"{od2['sla_level']} {od2['sla_hours']}")
    check("od2 标记为已升级", od2["upgraded"] is True)
    check("升级理由写进 note", "30 天" in od2["sla_note"], od2["sla_note"])

    # 工单落库的值必须与返回一致（不是"返回改了、库里还是旧的"）
    o2 = WorkOrder.query.filter_by(order_no=od2["order_no"]).first()
    check("库里的工单时限也是 4h（不是只改了返回值）",
          o2 and o2.sla_hours == 4 and o2.sla_level == "紧急",
          o2 and f"{o2.sla_level} {o2.sla_hours}")

    # 流转日志写的必须是最终等级
    lg = (WorkOrderLog.query.filter_by(order_id=o2.id, action="dispatch")
          .order_by(WorkOrderLog.id.desc()).first())
    check("派单日志里写的是最终等级「紧急」，不是初定级",
          lg and "紧急" in (lg.detail or "") and "风险复核升级" in (lg.detail or ""),
          lg and lg.detail)
    check("派单日志只有一条（没有「先建错单再改级」）",
          WorkOrderLog.query.filter_by(order_id=o2.id, action="dispatch").count() == 1)

    # 轨迹
    bb2 = Blackboard()
    # subject 必须带"是什么问题"：判重要求同址**同类**，
    # 写成"第三次了"这种没有类别词的句子，复核会正确地弃权（见 category_of）
    _, wi3 = mk_item("2栋2单元202第三次漏水了")
    check("第三次报修仍为紧急（封顶不倒退）",
          orch._dispatch_with_review(bb2, wi3, 3)["sla_level"] == "紧急")
    seqs = [s["seq"] for s in bb2.steps]
    check("轨迹序号从 1 开始连续",
          seqs == list(range(1, len(seqs) + 1)), seqs)
    agents = [s["agent"] for s in bb2.steps]
    check("轨迹包含 dispatch / risk / resolve / dispatch 四段",
          agents.count("dispatch") == 2 and "risk" in agents and "resolve" in agents,
          agents)
    check("has 表态行的 topic 都是 sla",
          all(s["topic"] in ("", "sla") for s in bb2.steps))

    # 落库
    db.session.add_all(bb2.pending_rows())
    db.session.commit()
    saved = AgentStep.query.filter_by(run_id=bb2.run_id).order_by(AgentStep.seq).all()
    check("轨迹能落库并读回", len(saved) == len(bb2.steps), f"{len(saved)}")
    check("落库后 seq 保持连续", [r.seq for r in saved] == seqs)
    check("reason 截断到 400 字以内", all(len(r.reason or "") <= 400 for r in saved))


# ================================================================ D 接口与权限

def prep_api():
    """造好数据、建好账号，返回后续断言要用的东西。

    **必须在 app_context 里跑**，而发请求的 `t_api()` 必须在外面跑——
    `with app.test_client()` 在 app_context 内部会复用已有的 application
    context，login 返回 302 看着成功，下一次请求却是 401。

    **只 fresh 一次**：D 段和 E 段共用一个库。早先拆成两个 prep、各自
    调一次 fresh()，结果 E 段的 drop_all 把 D 段刚建的用户全删了，
    D 段所有请求回 401——看着像"权限判断失效"，实际是测试自己拆的台。
    """
    fresh()

    from app.agents.orchestrator import Orchestrator
    orch = Orchestrator()

    # ① 一次真实的协作运行（用来验证 trace/stats）
    bb = Blackboard()
    _, wi = mk_item("2栋2单元202漏水")
    od = orch._dispatch_with_review(bb, wi, 1)
    db.session.add_all(bb.pending_rows())
    db.session.commit()

    # ② 一条只有过程步骤、没有表态的 run（E 段的边界）
    bb2 = Blackboard()
    bb2.step("security", "mask", decision="脱敏 0 处", took_ms=1)
    db.session.add_all(bb2.pending_rows())
    db.session.commit()

    for uname, role, org in [("com1", "community", "丙社区"),
                             ("prop1", "property", CO_A),
                             ("st1", "street", "街道办"),
                             ("adm", "admin", "系统")]:
        db.session.add(User(username=uname, name=uname, role=role, org=org,
                            password_hash=generate_password_hash("123456"),
                            status="active"))
    db.session.commit()
    return {"run_id": bb.run_id, "steps": len(bb.steps),
            "agents": {s["agent"] for s in bb.steps},
            "order_no": od["order_no"], "edge_run": bb2.run_id}


def t_api(ctx):
    print("\n[D] 接口与权限：轨迹是内部复盘记录，物业和街道都看不到")
    run_id, n_steps = ctx["run_id"], ctx["steps"]

    def as_user(name, pwd="123456"):
        """每个角色**必须用独立的 client**。

        `/login` 第一句就是 `if current_user(): redirect(...)` —— 已登录状态下
        再 POST 一次是空转，不会换账号。用一个 client 连续登录 prop1→st1→com1，
        最后三次请求其实全是 prop1，于是"社区也被 403"看起来像权限判断写错了，
        实际是登录根本没生效。这类错只在"测试里切换角色"时出现，
        真实用户不会（他们先 logout）。
        """
        c = app.test_client()
        r = c.post("/login", data={"username": name, "password": pwd})
        if r.status_code != 302:
            print(f"  [WARN] {name} 登录未成功：{r.status_code}")
        return c

    prop = as_user("prop1")
    for url in ["/api/collab/runs", "/api/collab/stats",
                "/api/collab/trace?run_id=" + run_id, "/trace"]:
        r = prop.get(url)
        check(f"物业访问 {url.split('?')[0]} → 403",
              r.status_code == 403, r.status_code)

    st = as_user("st1")
    for url in ["/api/collab/runs", "/api/collab/stats", "/trace"]:
        r = st.get(url)
        check(f"街道访问 {url} → 403（轨迹归社区内部复盘，街道走审计）",
              r.status_code == 403, r.status_code)

    com = as_user("com1")
    check("社区能看运行列表", com.get("/api/collab/runs").status_code == 200)
    check("社区能看协作统计", com.get("/api/collab/stats").status_code == 200)
    r = com.get("/trace")
    check("社区能打开轨迹页", r.status_code == 200, r.status_code)
    check("轨迹页渲染了协作说明，不是白页",
          "更严者胜" in r.get_data(as_text=True))

    j = com.get("/api/collab/runs").get_json()["data"]
    check("运行列表带 items", isinstance(j["items"], list) and len(j["items"]) >= 1)
    check("列表里能对上是刚跑的那次",
          any(x["run_id"] == run_id for x in j["items"]))
    check("列表里带工单号", any(ctx["order_no"] in x["orders"] for x in j["items"]))

    r = com.get("/api/collab/trace")
    check("缺 run_id → 400", r.status_code == 400, r.status_code)
    r = com.get("/api/collab/trace?run_id=R0000-NOPE")
    check("不存在的 run_id → 404", r.status_code == 404, r.status_code)

    d = com.get("/api/collab/trace?run_id=" + run_id).get_json()["data"]
    check("trace 返回 steps", len(d["steps"]) == n_steps, len(d["steps"]))
    check("trace 的 summary 带步数", d["summary"]["steps"] == n_steps)
    check("trace 的 summary 带 agent 去重列表",
          set(d["summary"]["agents"]) == ctx["agents"])
    check("steps 按 seq 升序",
          [s["seq"] for s in d["steps"]] == sorted(s["seq"] for s in d["steps"]))

    st = com.get("/api/collab/stats").get_json()["data"]
    check("stats 有 decisions 字段", "decisions" in st)
    check("stats 的 upgrade_rate 是比例不是百分数",
          st["upgrade_rate"] <= 1, st["upgrade_rate"])
    check("stats 带口径说明",
          "口径" in st.get("note", "") or "saved_hours" in st.get("note", ""))


# ==================================================================== E 边界

def t_edge(ctx):
    print("\n[E] 边界：只有过程步骤的 run 不崩、除零不炸")

    client = app.test_client()
    client.post("/login", data={"username": "adm", "password": "123456"})

    r = client.get("/api/collab/stats")
    st = r.get_json()["data"]
    check("有轨迹时 stats 正常返回", r.status_code == 200, r.status_code)
    check("upgrade_rate 是 0~1 的比例（不是百分数）",
          0 <= st["upgrade_rate"] <= 1, st.get("upgrade_rate"))
    check("decisions 与 reviewed 都不为负",
          st["decisions"] >= 0 and st["reviewed"] >= 0)

    d = client.get("/api/collab/trace?run_id=" + ctx["edge_run"]).get_json()["data"]
    check("没有表态的 run 也能看轨迹", len(d["steps"]) == 1)
    check("没有表态时不产生 resolutions", d["resolutions"] == [])

    r = client.get("/api/collab/runs")
    check("运行列表能列出只有过程步骤的 run",
          r.status_code == 200 and any(x["run_id"] == ctx["edge_run"]
                                       for x in r.get_json()["data"]["items"]))

    r = client.get("/api/collab/runs?limit=abc")
    check("limit 传非数字不 500", r.status_code in (200, 400), r.status_code)


def main():
    with app.app_context():
        t_blackboard()
        t_review()
        t_orchestrator()
        ctx = prep_api()      # 只 fresh 一次，D/E 共用
    # 请求段必须在 app_context 之外：见 prep_api 的说明
    t_api(ctx)
    t_edge(ctx)

    print("\n" + "=" * 66)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
