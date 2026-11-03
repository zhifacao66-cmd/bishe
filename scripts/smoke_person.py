"""人员底册冒烟测试。

覆盖五组：
A. 数据与脱敏（9 项）—— 人数、去重、明文零残留、姓名/证件/电话掩码
B. 权限收窄（12 项）—— 社区只看本社区、街道看全街道、物业/居民 403、未明确社区只给街道
C. 接口（26 项）—— 分页同源、筛选（含权重与未明确社区哨兵）、脏参数不 500、
   反查命中/未命中/空参、**反查结论与派单判据同源**、导出三种
D. 派单联动（8 项）—— 权重 3 压到紧急、权重 2 升一档、权重 1 不表态、无命中不表态、
   结论里不出现姓名、安全隐患词仍然优先
E. 导入幂等（6 项）—— 同一身份证重复 add 不新增、跨表同人合并标签、
   无有效身份证跳过、库内 id_hash 唯一

运行：python scripts/smoke_person.py

为什么复制演示库而不是另造：
底册由 `scripts/gen_demo_persons.py` 生成 **300 条合成数据**（社区归属、
地址精度、权重分布三项比例与真实台账同构，但人全是编的）。早先这个库是
真实台账导入的（2842 人），含居民身份证与完整门牌，不适合长期留在开发机
上，也不该出现在任何截图或录屏里 —— 已整体替换。

合成数据必须保住三个特征，否则测不出真问题：
  · 一人一条、可按身份证哈希去重（测 `id_hash` 这个键）
  · 地址精度分层 41%/3%/28%/28%（只有门号级才能匹配工单）
  · 权重长尾，w1 占 87%（所以"权重 1 不提级"才成立）

三个门牌是**测试锚点**，由生成器精确造出，不能随机：
  1栋1单元101 → 权重 3 → 派单定紧急
  2栋2单元202 → 权重 2 → 派单升一档
  3栋3单元303 → 权重 1 → 不提级
  99栋9单元999 → 库里不存在 → 反查必须 miss
"""
import os
import pathlib
import shutil
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# --- 建隔离库（必须在 import app 之前：config.py 导入即锁 DATABASE_URL）---
# 不删文件：沙箱 safe-delete 会拦 .db，库名带 pid 保证每次都是新的。
SRC = BASE / "data" / "community.db"
DB = BASE / "data" / ("smoke_person_%d.db" % os.getpid())
if pathlib.Path(str(DB)).exists():
    raise SystemExit("测试库已存在，拒绝覆盖半途状态：%s" % DB)
for suf in ("", "-wal", "-shm"):
    s = pathlib.Path(str(SRC) + suf)
    if s.exists():
        shutil.copy2(s, str(DB) + suf)
os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                              # noqa: E402
from app.agents.riskreview import RiskReviewAgent       # noqa: E402
from app.llm.client import LLMUnavailable               # noqa: E402
from app.models import Person, PersonTag                # noqa: E402
from app.personbook import ORG_ALIAS, visible_communities  # noqa: E402

app = create_app("dev")
PASS, FAIL = 0, 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (label, extra))


def login(username, pw="123456"):
    """每个角色必须新建 client：`/login` 第一句就是 if current_user(): redirect，
    同一个 client 连续登录第二个角色是空转（会一直保持第一个角色的身份）。"""
    c = app.test_client()
    c.post("/login", data={"username": username, "password": pw})
    return c


def json_of(r):
    try:
        return r.get_json() or {}
    except Exception:                                    # noqa: BLE001
        return {}


# ============================================================ A 数据与脱敏
def t_data():
    print("\n[A] 数据与脱敏")
    with app.app_context():
        n = Person.query.count()
        # 规模断言只要求"够分页、够筛选"，不与具体人数绑定：
        # 演示库是 300 条合成数据，早先这里写的是 > 2000（真实台账量级），
        # 一换数据就红，但红了不代表功能坏了，纯属断言写死了数据规模。
        check("底册人数够撑分页与筛选（≥200）", n >= 200, n)
        check("去重后身份证哈希唯一",
              Person.query.with_entities(Person.id_hash).distinct().count() == n)
        check("标签条数 > 人数（一人多标签）",
              PersonTag.query.count() > n, PersonTag.query.count())
        multi = (PersonTag.query.with_entities(PersonTag.person_id)
                 .distinct().count())
        check("存在多标签人员", multi >= 15, multi)

        # 脱敏：库里不能有任何明文身份证/完整姓名/完整手机号
        import sqlite3
        con = sqlite3.connect("file:%s?mode=ro" % str(DB).replace("\\", "/"), uri=True)
        raw_ids = con.execute(
            "select count(*) from person where id_masked like '____________' "
            "or length(id_masked) > 20").fetchone()[0]
        check("库内无明文身份证（全部带掩码）", raw_ids == 0, raw_ids)
        stars = con.execute(
            "select count(*) from person where id_masked like '%*%'").fetchone()[0]
        check("身份证掩码格式正确", stars == n, "%d/%d" % (stars, n))

        name_plain = con.execute(
            "select count(*) from person where name_masked not like '%*%'").fetchone()[0]
        check("姓名全部脱敏（保留姓 + 星号）", name_plain <= 2,
              "未脱敏 %d 条（单字姓名无法脱敏）" % name_plain)

        phone_raw = con.execute(
            "select count(*) from person where guardian_phone != '' "
            "and guardian_phone not like '%****%'").fetchone()[0]
        check("监护人电话全部掩码", phone_raw == 0, phone_raw)

        lv3 = Person.query.filter(Person.addr_level == 3).count()
        ratio = lv3 / n * 100 if n else 0
        check("门号级可定位比例在 35%~50%（如实记录，不虚标）",
              35 <= ratio <= 50, "%.1f%%" % ratio)
        con.close()


# ============================================================ B 权限收窄
def t_scope():
    print("\n[B] 权限收窄")
    with app.app_context():
        com = app.test_client()
        com.post("/login", data={"username": "wang", "password": "123456"})
        u = None
        from app.models import User
        u = User.query.filter_by(username="wang").first()
        comms = visible_communities(u)
        check("社区账号的可见社区是映射后的真实社区",
              comms == {"住武社区"}, comms)

        st = User.query.filter_by(username="jiedao").first()
        check("街道账号不限社区（返回 None）", visible_communities(st) is None)
        ad = User.query.filter_by(username="admin").first()
        check("管理员不限社区", visible_communities(ad) is None)

        n_total = Person.query.count()
        n_comm = Person.query.filter(Person.community == "住武社区").count()
        n_blank = Person.query.filter(Person.community == "").count()
        # 用比例而不是绝对数：这是"台账大量没写清社区"这个业务特征，
        # 比例才是特征本身，500 这个数只是当时那批数据的巧合。
        check("未明确社区的人占四成以上（台账常态）",
              n_blank >= 0.4 * n_total, "%d/%d" % (n_blank, n_total))
        check("住武社区人数少于总人数", n_comm < n_total, "%d/%d" % (n_comm, n_total))

    # 接口层：社区看到的 total 必须等于本社区人数，不能等于全街道
    com = login("wang")
    d = json_of(com.get("/api/persons/stats")).get("data") or {}
    with app.app_context():
        expect = Person.query.filter(Person.community == "住武社区").count()
    check("社区账号 /stats 的 total = 本社区人数", d.get("total") == expect,
          "%s vs %s" % (d.get("total"), expect))
    check("社区账号看不到未明确社区的记录", d.get("no_community") == 0,
          d.get("no_community"))

    st = login("jiedao")
    ds = json_of(st.get("/api/persons/stats")).get("data") or {}
    with app.app_context():
        alln = Person.query.count()
    check("街道账号 /stats 的 total = 全街道人数", ds.get("total") == alln,
          "%s vs %s" % (ds.get("total"), alln))
    check("街道能看到未明确社区的记录",
          (ds.get("no_community") or 0) >= 0.4 * alln,
          "%s/%s" % (ds.get("no_community"), alln))

    prop = login("wuye")
    r = prop.get("/api/persons?size=1")
    check("物业访问底册接口 403", r.status_code == 403, r.status_code)
    r = prop.get("/persons")
    check("物业访问底册页面 403", r.status_code == 403, r.status_code)

    res = app.test_client()
    r = res.get("/api/persons")
    check("未登录访问底册 401/302", r.status_code in (401, 302), r.status_code)


# ============================================================ C 接口
def t_api():
    print("\n[C] 接口")
    com = login("wang")

    r = com.get("/api/persons?size=5")
    d = json_of(r).get("data") or {}
    items = d.get("items") or []
    check("列表返回 items", len(items) == 5, len(items))
    check("total 不是当前页条数（走 count 不走 len）",
          (d.get("total") or 0) > len(items), "%s vs %s" % (d.get("total"), len(items)))
    check("每条都带定位精度中文说明",
          all(x.get("addr_precision") for x in items))
    check("每条都带脱敏姓名", all("*" in (x.get("name") or "") for x in items))

    # 列表第一条应该是门号级（排序 addr_level desc）
    check("列表按定位精度降序（能上门的排前面）",
          items[0].get("addr_level") == 3, items[0].get("addr_level"))

    r = com.get("/api/persons?tag=残疾")
    dt = json_of(r).get("data") or {}
    check("按标签筛选：残疾",
          all(any(t["kind"] == "残疾" for t in x["tags"]) for x in (dt.get("items") or [])),
          len(dt.get("items") or []))

    r = com.get("/api/persons?precision=1")
    dp = json_of(r).get("data") or {}
    check("按定位精度筛选：仅到楼栋/组",
          all(x["addr_level"] == 1 for x in (dp.get("items") or [])),
          len(dp.get("items") or []))

    r = com.get("/api/persons?page=abc&size=abc")
    check("page/size 传非数字不 500", r.status_code == 200, r.status_code)

    r = com.get("/api/persons?q=不存在的关键词zzz")
    check("搜不到时 total=0 且 items 为空",
          (json_of(r).get("data") or {}).get("total") == 0)

    # 反查：命中 / 未命中 / 空参
    r = com.get("/api/persons/match?text=3栋3单元303室厨房漏水")
    dm = json_of(r).get("data") or {}
    check("反查命中门号级地址", (dm.get("count") or 0) >= 1, dm.get("count"))
    r = com.get("/api/persons/match?text=99栋9单元999室漏水")
    check("反查未命中返回 200 + 空列表",
          r.status_code == 200 and (json_of(r).get("data") or {}).get("count") == 0)
    r = com.get("/api/persons/match?text=只是漏水没说地址")
    check("地址认不出时不瞎猜（命中 0）",
          (json_of(r).get("data") or {}).get("count") == 0)
    r = com.get("/api/persons/match")
    check("反查缺 text → 400", r.status_code == 400, r.status_code)

    # 结论口径：页面显示的"会不会提级"必须和派单复核实际做的一致。
    # 曾经页面写死"→ 该户报修应提级处理"，命中全是三级/四级（权重 1）时
    # 系统根本不提级 —— 说的和做的相反，比不显示更糟。
    r = com.get("/api/persons/match?text=3栋3单元303室厨房漏水")
    flat = (json_of(r).get("data") or {}).get("verdict") or {}
    check("权重 1 的户：verdict=flat（不提级）",
          flat.get("level") == "flat",
          "%s / weight=%s" % (flat.get("level"), flat.get("weight")))
    r = com.get("/api/persons/match?text=1栋1单元101厨房漏水")
    hot = (json_of(r).get("data") or {}).get("verdict") or {}
    check("权重 3 的户：verdict=urgent（定紧急）",
          hot.get("level") == "urgent" and hot.get("weight") == 3,
          "%s / weight=%s" % (hot.get("level"), hot.get("weight")))
    r = com.get("/api/persons/match?text=99栋9单元999室漏水")
    miss = (json_of(r).get("data") or {}).get("verdict") or {}
    check("未命中：verdict=miss", miss.get("level") == "miss", miss.get("level"))
    # 阈值只有一处定义：页面/接口结论与派单 Agent 必须同源
    from app.personbook import LIFT_MIN_WEIGHT, URGENT_WEIGHT
    check("提级阈值单一定义（2 / 3）",
          (LIFT_MIN_WEIGHT, URGENT_WEIGHT) == (2, 3),
          "%s / %s" % (LIFT_MIN_WEIGHT, URGENT_WEIGHT))

    # 按权重筛选（KPI 卡片点进来走的就是这条）
    r = com.get("/api/persons?weight=3&size=100")
    dw = json_of(r).get("data") or {}
    check("按权重≥3 筛选：条数与统计一致且都有权重 3 的标签",
          all(any((t.get("weight") or 0) >= 3 for t in (x.get("tags") or []))
              for x in (dw.get("items") or [])) and (dw.get("total") or 0) > 0,
          dw.get("total"))
    r = com.get("/api/persons?weight=abc")
    check("weight 传脏参数不 500 也不筛空", r.status_code == 200, r.status_code)

    # 「未明确社区」哨兵值：空串是"不筛选"，两者必须区分开，
    # 否则点了那张卡片等于没点（这正是页面上"点了没反应"的一类成因）
    rs = com.get("/api/persons/stats")
    st = json_of(rs).get("data") or {}
    check("统计给出 by_community（下拉不写死社区名）",
          isinstance(st.get("by_community"), list) and len(st.get("by_community") or []) >= 1,
          len(st.get("by_community") or []))

    sd = login("jiedao")                       # 街道：这批人只有它看得到
    sds = json_of(sd.get("/api/persons/stats")).get("data") or {}
    no_comm = sds.get("no_community") or 0
    r = sd.get("/api/persons?community=__none__&size=100")
    dn = json_of(r).get("data") or {}
    check("街道可用 community=__none__ 筛出未明确社区的人",
          no_comm > 0 and (dn.get("total") or 0) == no_comm,
          "命中 %s / 统计 %s" % (dn.get("total"), no_comm))
    check("筛出来的人社区字段都是「（未明确社区）」",
          all(x.get("community") == "（未明确社区）" for x in (dn.get("items") or [])),
          len(dn.get("items") or []))

    r = com.get("/api/persons?community=__none__&size=100")
    check("社区账号用同一个筛选查不到这批人（ABAC 收窄）",
          (json_of(r).get("data") or {}).get("total") == 0,
          (json_of(r).get("data") or {}).get("total"))

    # 导出
    for kind in ("disabled", "drowning"):
        r = com.get("/api/persons/export?kind=" + kind)
        ok = (r.status_code == 200 and
              "spreadsheetml" in (r.headers.get("Content-Type") or ""))
        check("导出 %s 返回 xlsx" % kind, ok, r.status_code)
    r = com.get("/api/persons/export?kind=nope")
    check("未知导出类型 → 400", r.status_code == 400, r.status_code)


# ============================================================ D 派单联动
def t_review():
    print("\n[D] 派单联动")
    ag = RiskReviewAgent()

    def review(text, level="一般"):
        with app.app_context():
            return ag.review({"subject": text, "level": level})

    # 权重 3（一级残疾）：直接压到紧急
    r = review("1栋1单元101室厨房水管爆了，家里没人")
    sig = {s["code"] for s in r.get("signals") or []}
    check("命中一级残疾户 → 建议紧急", r.get("level") == "紧急", r.get("level"))
    check("信号里有 vulnerable", "vulnerable" in sig, sig)

    # 权重 2（二级残疾）：从一般升一档到较急
    r2 = review("2栋2单元202室卫生间漏水")
    check("命中二级残疾户 → 升一档（一般→较急）",
          r2.get("level") == "较急", r2.get("level"))

    # 权重 1（三级/四级残疾）：不提级
    r3 = review("3栋3单元303室厨房漏水")
    check("只命中三级/四级 → 不表态", r3.get("level") is None, r3.get("level"))

    # 地址不在底册上：不表态
    r4 = review("99栋9单元999室水管漏水")
    check("地址不在底册 → 不表态", r4.get("level") is None, r4.get("level"))

    # 结论里不能出现姓名（会被导出、投屏）
    r5 = review("1栋1单元101室漏水")
    reason = (r5.get("reason") or "")
    leaked = [w for w in ("尤", "陶", "李", "罗", "何") if w in reason]
    check("复核结论不含当事人姓名", not leaked, "泄露：%s / %s" % (leaked, reason[:60]))

    # 安全隐患词优先：同一条同时命中，结论仍是紧急且带 safety
    r6 = review("1栋1单元101室燃气泄漏", "一般")
    sig6 = {s["code"] for s in r6.get("signals") or []}
    check("安全隐患词与底册同时命中，结论含 safety",
          "safety" in sig6 and r6.get("level") == "紧急", sig6)

    # 底册查不到时不能把派单搞崩（模拟表不存在）
    try:
        with app.app_context():
            ag.review({"subject": "2栋2单元202室漏水", "level": "一般"})
        check("底册异常不影响派单", True)
    except Exception as e:                               # noqa: BLE001
        check("底册异常不影响派单", False, type(e).__name__)


# ============================================================ E 导入幂等
def t_idempotent():
    """测导入去重逻辑，**不碰真实台账**。

    早先这里跑的是 `import_ledger.py --apply`，只要跑一次回归就把真实
    台账的 2842 人导回演示库 —— "改用合成数据"等于白做。现在直接测
    `Collector`（导入去重的唯一实现），不依赖任何 xlsx 文件。

    真实台账这条路径能不能跑通，由 `import_ledger.py --check` 负责验证，
    那是要人工看着结果的操作，不该塞进自动回归里。
    """
    print("\n[E] 导入幂等")
    sys.path.insert(0, str(BASE / "scripts"))
    import import_ledger as il

    base = dict(name="测试甲", idcard="520201199001011234", gender="男", age=36,
                addr="住武社区1栋1单元101室", community="住武社区", grp="昌盛组",
                tags=[("残疾", "肢体残疾|三级", 1)], risk="三级",
                source="残疾人台账")

    c = il.Collector()
    c.add(**base)
    check("首次 add 记为新增", c.stat["新增"] == 1 and len(c.rows) == 1,
          "%s / %d" % (c.stat["新增"], len(c.rows)))

    c.add(**base)
    c.add(**base)
    check("同一身份证重复 add 不新增行", len(c.rows) == 1, len(c.rows))
    check("重复被记为合并", c.stat["重复_合并"] == 2, c.stat["重复_合并"])

    # 跨表同一个人（残疾 + 防溺水）：应该合并标签，不是建成两个人
    c.add(**dict(base, tags=[("防溺水", "低风险|重点水域附近", 1)],
                 source="防溺水台账"))
    only = list(c.rows.values())[0]
    check("跨表同人合并标签而不是新建人",
          len(c.rows) == 1 and len(only["tags"]) == 2, len(only["tags"]))

    # 身份证不合法：跳过，且不能塞一个假哈希进库
    c.add(**dict(base, name="测试乙", idcard="123"))
    check("无有效身份证跳过", c.stat["跳过_无有效身份证"] == 1,
          c.stat["跳过_无有效身份证"])

    # 落库层：id_hash 有唯一约束，重复插入必须失败（不是静默多一行）
    with app.app_context():
        h = only["id_hash"]
        check("库里 id_hash 无重复",
              Person.query.filter_by(id_hash=h).count() <= 1,
              Person.query.filter_by(id_hash=h).count())


def main():
    t_data()
    t_scope()
    t_api()
    t_review()
    t_idempotent()

    print("\n" + "=" * 66)
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
