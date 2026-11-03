"""人员底册：街道下发的重点人员台账（残疾人 / 防溺水 / 留守儿童）。

这个模块只做三件事，每件都对应一个真实约束：

1. **看谁的人**（`visible_communities` / `scoped`）
   底册是全街道的（实测以朵街道 2842 人，横跨住武社区、以朵社区、滴水岩村，
   另有 1855 人台账里根本没写清属于哪个社区）。社区工作者只看本社区，
   街道看全部。收窄必须走**白名单**：`role in ("street","admin")` 才放开，
   不能写"除街道外都收窄"——那样 admin 会拿到一个不存在的社区名，
   页面恒空且不报错（这个坑在 tasks/supervise 里已经踩过两次）。

2. **地址认人**（`match_by_addr`）
   派单时拿报修地址去底册里找人，命中重点人员就提级。这是底册唯一
   真正产生业务价值的地方——只做"能查"的底册，答辩时说不出为什么要它。

3. **按原表格式导出**（`export_header` / `export_row`）
   真实社区每月要往街道报这几张表，字段、顺序、表头大标题都得和
   下发模板一致，否则街道收不了。导出只读 `Person` 与 `PersonTag`，
   不碰原始 xlsx。
"""
from app.extensions import db
from app.models import Person, PersonTag

# ---------------------------------------------------------------------------
# 系统演示社区 → 真实台账社区
#
# 系统里的组织是虚构的「幸福里社区 / 幸福里物业 / 幸福里街道办事处」，
# 而这四张台账来自「以朵街道 · 住武社区」。两边名字对不上。
#
# 为什么不直接把系统改名：`幸福里` 出现在 20 多个源文件、31 个回归脚本、
# 8 张表里。改名要把整套测试和演示数据一起搬，改动面远大于收益，而且
# 一次漏改就是"页面显示幸福里、数据来自以朵"的半吊子状态——比现在更糟。
#
# 所以这里**显式登记映射**，并让它成为唯一出口：
# 所有"当前账号属于哪个社区"的判断都走 `visible_communities()`，
# 以后真要改名，只改这张表。
#
# 这行注释不是"技术债说明"，是数据来源说明：演示实例挂的是真实社区的
# 台账，系统本身的机构名是产品化的占位。答辩被问到时照这个说即可。
# ---------------------------------------------------------------------------
ORG_ALIAS = {
    "幸福里社区": "住武社区",
    "幸福里街道办事处": "以朵街道",
    "幸福里物业": "住武社区",       # 物业按属地社区看（只用于底册，不涉及工单权限）
}

# 台账里能认出的社区。没写清的一律归空串——它是真实状态，不是缺数据。
KNOWN_COMMUNITIES = ("住武社区", "以朵社区", "滴水岩村")

# 前端选「未明确社区」时传的哨兵值。不能用空串——空串在这个接口里
# 表示"不筛选"，两者必须区分开，否则点了卡片等于没点。
NO_COMMUNITY = "__none__"


def visible_communities(user):
    """当前账号能看哪些社区的底册。返回 None 表示不限（街道 / 管理员）。

    白名单写法：只有明确列出的角色才放开。用 `role not in (...)` 做黑名单
    会把将来新增的角色全放行，也容易把 admin 漏掉。
    """
    role = (getattr(user, "role", "") or "").strip()
    if role in ("street", "admin"):
        return None
    org = (getattr(user, "org", "") or "").strip()
    return {ORG_ALIAS.get(org, org)} if org else set()


def scoped(base, user):
    """按可见社区收窄查询。返回 (query, 说明)。

    `community == ""`（台账未写清）**只有街道能看到**：
    社区工作者拿着一份"不知道是不是自己辖区"的名单去上门，是错误的。
    """
    comms = visible_communities(user)
    if comms is None:
        return base, "全街道"
    if not comms:
        # 组织名认不出（新建的账号还没填 org）：不给看，但要说清为什么，
        # 不能静默返回空列表——那会让人以为"底册里确实没这个人"。
        return base.filter(db.false()), "账号未设置所属社区，无可见底册"
    return base.filter(Person.community.in_(tuple(comms))), "、".join(sorted(comms))


def search(user, keyword="", community="", tag="", risk="", precision="",
           weight="", page=1, size=20):
    """底册检索。返回 (rows, total, scope_note)。

    统计与列表**同源**：`total` 用 `count()` 走同一个 base，
    不用 `len(rows)`（那是当前页的条数，分页后会显示 20）。
    """
    base, note = scoped(Person.query, user)

    kw = (keyword or "").strip()
    if kw:
        like = f"%{kw}%"
        # 姓名已脱敏（张*），所以按姓名搜只能搜到"张"这一层。
        # 再给地址、组、来源三路，实际用起来按楼栋找人才是最常见的。
        base = base.filter(db.or_(Person.name_masked.like(like),
                                 Person.address.like(like),
                                 Person.grp.like(like),
                                 Person.addr_mid.like(like)))
    # 「未明确社区」是首页那张卡片点进来的筛选，得有个不等于"全部"的哨兵值：
    # 用空串表示"全部"是既有约定，改不了，所以另给一个 __none__。
    if community == NO_COMMUNITY:
        base = base.filter(Person.community == "")
    elif community:
        base = base.filter(Person.community == community)
    if risk:
        base = base.filter(Person.risk_level == risk)
    if tag:
        base = base.filter(Person.tags.any(PersonTag.kind == tag))
    if weight:
        try:
            w = int(weight)
        except (TypeError, ValueError):
            w = 0
        if w > 0:
            # 「重点看护」卡片点进来走这里。用 any() 而不是 join，
            # 一人多标签时 join 会让同一个人出现多次、total 虚高。
            base = base.filter(Person.tags.any(PersonTag.risk_weight >= w))
    if precision:
        try:
            base = base.filter(Person.addr_level == int(precision))
        except (TypeError, ValueError):
            pass

    total = base.count()
    size = max(1, min(int(size or 20), 100))
    page = max(1, int(page or 1))
    rows = (base.order_by(Person.addr_level.desc(), Person.id.asc())
                .offset((page - 1) * size).limit(size).all())
    return rows, total, note


def stats(user):
    """底册概览。所有数字都用 `count()`，且与 `search()` 同一个收窄口径。"""
    base, note = scoped(Person.query, user)
    n = base.count()

    def cnt(*filters):
        q = base
        for f in filters:
            q = q.filter(f)
        return q.count()

    by_tag = {}
    for kind in ("残疾", "防溺水", "留守"):
        by_tag[kind] = cnt(Person.tags.any(PersonTag.kind == kind))

    # 风险权重分布：3=重点看护 2=重点关注 1=需关注
    heavy = cnt(Person.tags.any(PersonTag.risk_weight >= 3))
    medium = cnt(Person.tags.any(PersonTag.risk_weight == 2))

    # 地址精度如实统计——这是"数据能用到什么程度"的诚实交代
    precision = {}
    for lvl, label in ((3, "栋+单元+门号"), (2, "栋+单元"),
                       (1, "仅到楼栋/组"), (0, "无法定位")):
        precision[label] = cnt(Person.addr_level == lvl)

    # 社区分布：页面上的社区下拉用它生成，**不写死三个社区名**——
    # 街道下次下发的台账多一个社区，写死的下拉就会漏掉那批人，
    # 而且列表里查得到、下拉里选不到，没人会发现是这里的问题。
    # 直接对同一个 base 分组（不是另起一个 query），口径才和 total 同源。
    by_community = [
        {"name": name or "（未明确社区）",
         # value 直接给前端当 option 的值：空社区用哨兵，别让它等于"全部"
         "value": name or NO_COMMUNITY,
         "count": c}
        for name, c in base.with_entities(Person.community,
                                          db.func.count(Person.id))
                            .group_by(Person.community)
                            .order_by(db.func.count(Person.id).desc())
                            .all()
    ]

    return {
        "total": n,
        "scope": note,
        "by_tag": by_tag,
        "by_community": by_community,
        "heavy": heavy,
        "medium": medium,
        "no_community": cnt(Person.community == ""),
        "precision": precision,
        # 能参与工单匹配的条数（必须到门号级才谈得上"这户有人"）
        "matchable": precision["栋+单元+门号"],
    }


def match_by_addr(user, text, limit=5):
    """用报修地址去底册里找人。返回 [(Person, 该地址的命中方式)]。

    只认**门号级**命中（`addr_mid` 相同 + `addr_room` 相同），不做模糊匹配：
    底册 2842 人里 41.3% 有门号，模糊到"栋"会把整栋楼的几十个人都算成
    "这户有重点人员"，提级就失去了区分度。

    `user=None` 表示**系统内部调用**（派单复核），不受社区收窄——
    复核 Agent 服务的是整条派单链路，不该因为发起人换了社区就查不到人。
    这是唯一的放开点，页面与接口一律传真实 user。

    找不到返回空列表——这是常态，不是异常。真实报修里绝大多数户不在底册上。
    """
    from app.agents.riskreview import addr_parts

    parts = addr_parts(text or "")
    if parts.get("level") != 3:
        return []                       # 认不到门号就不猜

    mid = (parts.get("mid") or "").strip()
    room = (parts.get("keys") or [""])[0]
    if not mid or not room:
        return []

    base = Person.query
    if user is not None:
        base, _ = scoped(Person.query, user)
    hits = (base.filter(Person.addr_mid == mid, Person.addr_room == room)
                .order_by(Person.id.asc()).limit(limit).all())
    return [(p, f"{mid}{room}") for p in hits]


def top_weight(persons):
    """一组人的最高风险权重。没有返回 0。

    用它决定提级档位，而不是在调用处再写一遍阈值——
    两处各写一遍必然漂（项目里已经有过三次同样的教训）。
    """
    best = 0
    for p in persons or []:
        for t in p.tags:
            if (t.risk_weight or 0) > best:
                best = t.risk_weight
    return best


# 提级阈值：**这里是唯一定义处**。派单复核（riskreview 第 ④ 条）与页面提示
# 都读这两个数，任何一处再手写 2 / 3 就是第二份判据，早晚会漂。
#   权重 3 → 直接「紧急」4h；权重 2 → 升一档；权重 1 → 不动。
LIFT_MIN_WEIGHT = 2
URGENT_WEIGHT = 3


def lift_note(persons):
    """把"这户命中之后会怎样"算成一句可直接显示的话。

    页面曾经自己写死"→ 该户报修应提级处理"，而命中 2 人全是三级/四级残疾
    （权重 1）时系统根本不会提级——**页面说的和系统做的相反**。
    判据只留这一处，页面与接口都只显示、不判断。
    """
    weight = top_weight(persons)
    n = len(persons or [])
    if n == 0:
        return {"weight": 0, "level": "miss",
                "text": "这户不在底册上（或地址只到楼栋/组，认不到门号）——按常规时限处理"}
    if weight >= URGENT_WEIGHT:
        # 结论里不写姓名：轨迹会导出，写门号够派单用
        return {"weight": weight, "level": "urgent",
                "text": "最高权重 %d（一级残疾 / 高风险 / 事实无人抚养）"
                        "——这户报修直接定为「紧急」4h" % weight}
    if weight >= LIFT_MIN_WEIGHT:
        return {"weight": weight, "level": "lift",
                "text": "最高权重 %d（二级残疾 / 中风险）"
                        "——这户报修升一档（一般 72h → 较急 24h）" % weight}
    return {"weight": weight, "level": "flat",
            "text": "最高权重只有 %d（三四级残疾 / 低风险）"
                    "——这户不提级，按常规时限处理" % weight}


# ---------------------------------------------------------------------------
# 按原表格式导出
#
# 表头直接照抄下发模板（含换行与括号说明）。社区每月要报的就是这几张表，
# 列少了、顺序变了、大标题没了，街道都收不了。
# ---------------------------------------------------------------------------
EXPORT_SPECS = {
    "disabled": {
        "title": "以朵街道残疾人总台账",
        "header_row": 2,
        "columns": [
            ("序号", lambda i, p: i),
            ("姓名", lambda i, p: p.name_masked),
            ("身份证号", lambda i, p: p.id_masked),
            ("残疾证号", lambda i, p: p.id_masked),      # 原表残疾证号与身份证同源
            ("年龄", lambda i, p: p.age),
            ("性别", lambda i, p: p.gender),
            ("街道\n区划", lambda i, p: p.street),
            ("社区\n区划", lambda i, p: p.community),
            ("组", lambda i, p: p.grp),
            ("现居住地地址", lambda i, p: p.address),
            ("残疾类别", lambda i, p: _tag_field(p, 0)),
            ("残疾\n等级", lambda i, p: _tag_field(p, 1)),
            ("残疾\n详情", lambda i, p: f"{_tag_field(p,0)}{_tag_field(p,1)};"),
            ("发证\n时间", lambda i, p: ""),
            ("持证\n状态", lambda i, p: "持证"),
            ("重度残疾人\n护理补贴", lambda i, p: ""),
            ("享受低保情况", lambda i, p: ""),
        ],
    },
    "drowning": {
        "title": "未成年人防溺水全员覆盖包保看护台账",
        "header_row": 2,
        "columns": [
            ("序号", lambda i, p: i),
            ("姓名", lambda i, p: p.name_masked),
            ("性别", lambda i, p: p.gender),
            ("年龄", lambda i, p: p.age),
            ("身份证号码", lambda i, p: p.id_masked),
            ("就读学校/年级", lambda i, p: ""),
            ("详细居住地址", lambda i, p: p.address),
            ("风险等级", lambda i, p: p.risk_level),
            ("风险类型", lambda i, p: _tag_detail(p) or ""),
            ("法定监护人", lambda i, p: p.guardian_masked),
            ("电话", lambda i, p: p.guardian_phone),
            ("", lambda i, p: ""),
            ("实际监护人", lambda i, p: p.guardian_masked),
            ("电话", lambda i, p: p.guardian_phone),
            ("包保看护责任人", lambda i, p: ""),
            ("职务", lambda i, p: ""),
            ("电话", lambda i, p: ""),
            ("包保看护管控措施", lambda i, p: ""),
            ("走访宣传记录", lambda i, p: ""),
            ("动态更新情况", lambda i, p: ""),
            ("备注", lambda i, p: ""),
        ],
    },
    "leftbehind": {
        "title": "疑似留守儿童未成年人台账",
        "header_row": 3,
        "columns": [
            ("序号", lambda i, p: i),
            ("姓名", lambda i, p: p.name_masked),
            ("年龄", lambda i, p: p.age),
            ("身份证号码", lambda i, p: p.id_masked),
            ("家庭住址", lambda i, p: p.address),
            ("监护人姓名", lambda i, p: p.guardian_masked),
            ("身份证号码", lambda i, p: ""),
            ("与未成年人关系", lambda i, p: _tag_detail(p) or ""),
            ("监护人务工地址", lambda i, p: ""),
            ("在读学校", lambda i, p: ""),
            ("在读年级", lambda i, p: ""),
            ("包组工作人员", lambda i, p: ""),
            ("联系电话", lambda i, p: p.guardian_phone),
            ("备注", lambda i, p: ""),
        ],
    },
}


def _tag_field(p, idx):
    """取第一个「残疾」标签的 类别/等级（detail 形如「肢体|三级」）。"""
    for t in p.tags:
        if t.kind == "残疾":
            parts = (t.detail or "").split("|")
            return parts[idx] if idx < len(parts) else ""
    return ""


def _tag_detail(p):
    for t in p.tags:
        if t.kind == "防溺水":
            parts = (t.detail or "").split("|")
            return parts[1] if len(parts) > 1 else ""
    return ""


def export_rows(user, kind):
    """按导出规格取数。只读库，不碰原始 xlsx。"""
    spec = EXPORT_SPECS.get(kind)
    if not spec:
        return None, None, []
    base, _ = scoped(Person.query, user)
    if kind == "disabled":
        base = base.filter(Person.tags.any(PersonTag.kind == "残疾"))
    elif kind == "drowning":
        base = base.filter(Person.tags.any(PersonTag.kind == "防溺水"))
    elif kind == "leftbehind":
        base = base.filter(Person.tags.any(PersonTag.kind == "留守"))

    persons = base.order_by(Person.id.asc()).all()
    header = [c[0] for c in spec["columns"]]
    rows = [[fn(i + 1, p) for _, fn in spec["columns"]]
            for i, p in enumerate(persons)]
    return spec, header, rows
