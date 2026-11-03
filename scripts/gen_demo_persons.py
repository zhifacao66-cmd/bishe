"""合成底册生成器 —— 演示与答辩用，不含任何真实居民信息。

为什么单独写一个生成器，而不是继续用 import_ledger.py：
真实台账含居民身份证号，长期留在开发机上是风险。演示只需要"结构像"，
不需要"人真"。所以把两件事拆开：
  import_ledger.py  —— 真实台账 → 底册（方法论文档里保留，需要时可用）
  gen_demo_persons  —— 造一批结构相同的假人（日常演示、回归测试、公开截图）

**造出来的数必须撑得住答辩要讲的三个特征**，否则拿合成数据演示会把功能讲丢：

  ① 社区归属不均：一半人台账上没写清属于哪个社区（真实台账的常态）
  ② 地址精度分层：只有约 41% 能到门号 —— 这决定了底册能不能匹配工单
  ③ 权重长尾：权重 1 占 87%，所以"权重 1 不提级"才成立

另外 smoke_person.py 硬编码了三个门牌（1栋1单元101 / 2栋2单元202 /
3栋3单元303），用来验证 urgent / lift / flat 三档判据。合成数据必须
**精确造出这三个点**，否则测试会红 —— 这是本脚本里唯一不能随机的部分。

用法：
    python scripts/gen_demo_persons.py            # 预演，只打印不写库
    python scripts/gen_demo_persons.py --apply    # 清空后写入
    python scripts/gen_demo_persons.py --purge --apply   # 只清空
"""
import argparse
import datetime
import hashlib
import os
import random
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "community.db")

SEED = 20261103          # 固定种子：每次生成同一批人，回归才可比
TOTAL = 300

# ---- 分布（按演示需要配比；不是真实台账比例，见文件头说明）----
COMM = [("住武社区", 110), ("以朵社区", 15), ("滴水岩村", 25), ("", 150)]
PREC = [(3, 124), (2, 8), (1, 85), (0, 83)]
WEIGHT_DIST = [(3, 11), (2, 27), (1, 262)]

# ---- 标签构成。59 残疾 + 244 防溺 − 3 双标签 = 300 人 ----
DISABLED = 59
DROWNING = 244
DUAL = 3                 # 既是残疾儿童又是防溺水对象
DIS_LEVEL = [(1, 11), (2, 15), (3, 20), (4, 13)]     # 残疾等级 → 人数
DROWN_RISK = [("中风险", 12), ("低风险", 232)]

# ---- 测试锚点：smoke_person.py 依赖这三个门牌，不能随机 ----
#   (addr_mid, addr_room, 权重, 残疾等级, 说明)
#
# 地址用**明显虚构的顺号**（1栋1单元101 / 2栋2单元202 / 3栋3单元303）。
# 早先用的门牌是从真实台账里挑的（正好是真有一级残疾人的一户）——
# 留在演示数据里等于把"某社区某门牌住着一级残疾人"这件事讲出来了，
# 和"不用真实数据"直接冲突。顺号一眼能看出是编的，反而安全。
ANCHORS = [
    ("1栋1单元", "101", 3, 1, "权重 3 → 派单定紧急"),
    ("2栋2单元", "202", 2, 2, "权重 2 → 派单升一档"),
    ("3栋3单元", "303", 1, 3, "权重 1 → 不提级"),
]

SURNAME = ("王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾"
           "肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏"
           "韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴"
           "莫孔向汤")
GIVEN1 = "伟芳娜敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂英华玉兰春梅文辉"
GIVEN2 = "华建国明志强艳丽娟秀英霞梅兰芳军伟杰涛勇超宁康乐安康福寿"
GROUPS = ("昌盛组", "诚信组", "友善组", "繁荣组", "和谐组", "文明组",
          "爱国组", "敬业组", "富强组", "民主组")
# 村组的名字跟城市社区的"网格组"不是一回事，不能混用
VILLAGE_GROUPS = ("陈家寨组", "李家寨组", "赵家寨组", "王家院组", "田坝组",
                  "河边组", "新寨组", "老寨组", "半坡组", "大坪组")
STREET = "以朵街道"

# 地址分区：住武社区是楼房（有栋/单元/门号），其余是村组（只能到组）
BLDGS = [str(i) for i in range(1, 29)]


def cn_idcard(birth, male):
    """生成合法格式的身份证号（校验位按 ISO 7064 MOD 11-2 算对）。

    注意：**明文只在内存里存在**，落库的只有 sha256 前 32 位和前后掩码，
    和 import_ledger.py 的处理完全一致。
    """
    body = "520201" + birth.strftime("%Y%m%d") + "%03d" % random.randint(1, 999)
    # 顺序码末位定性别：奇男偶女
    body = body[:-1] + str(random.randint(0, 9) if male else random.randint(1, 9) * 2 % 10)
    w = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    chk = "10X98765432"[sum(int(c) * k for c, k in zip(body, w)) % 11]
    return body + chk


def id_hash(v):
    s = str(v or "").strip().upper()
    return hashlib.sha256(s.encode()).hexdigest()[:32] if len(s) >= 15 else ""


def mask_id(v):
    s = str(v or "").strip().upper()
    return s[:6] + "*" * 8 + s[-4:] if len(s) >= 10 else ""


def mask_name(v):
    s = str(v or "").strip()
    return s[0] + "*" * (len(s) - 1) if s else ""


def mask_phone(v):
    """与 import_ledger.mask_phone 同一口径：保留头尾 + 4 星。

    两处口径必须一致 —— 页面和测试都按"含 ****"判断是否明文，
    这里少一个星号就会让"库里还有明文电话"的检查变红。
    """
    s = "".join(ch for ch in str(v or "") if ch.isdigit())
    if len(s) >= 11:
        return s[:3] + "****" + s[-4:]
    if len(s) >= 5:
        return s[:2] + "****" + s[-2:]
    return "****" if s else ""


def fake_name(two_char=None):
    if two_char is None:
        two_char = random.random() < 0.55
    s = random.choice(SURNAME)
    s += random.choice(GIVEN1)
    if not two_char:
        s += random.choice(GIVEN2)
    return s


def expire_ok(age, birth):
    """年龄与身份证生日自洽性检查 —— 假数据也不能自相矛盾。"""
    today = datetime.date(2026, 11, 3)
    real = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    return abs(real - age) <= 1


def birth_of(age):
    today = datetime.date(2026, 11, 3)
    y = today.year - age
    m = random.randint(1, 12)
    d = random.randint(1, 28)
    return datetime.date(y, m, d)


def build():
    """按"先定标签、再定社区、再定地址"的顺序造人。

    顺序很重要：标签决定年龄（防溺水必须是未成年人），
    社区决定地址写法（住武是楼房、村里只能到组），
    地址决定 addr_level。反过来造会造出"40 岁的防溺水对象"这种笑话。
    """
    random.seed(SEED)
    people = []

    # ---------- ① 三类人：成人残疾 / 未成年残疾（双标签）/ 纯防溺水 ----------
    n_adult_dis = DISABLED - DUAL                      # 56
    n_dual = DUAL                                      # 3
    n_drown_only = DROWNING - DUAL                     # 241
    assert n_adult_dis + n_dual + n_drown_only == TOTAL

    # 残疾等级配额：一级 11 二级 15 三级 20 四级 13 = 59
    levels = []
    for lvl, n in DIS_LEVEL:
        levels += [lvl] * n
    random.shuffle(levels)

    # 双标签的 3 人取三级残疾（低权重，不影响 w3 配额）
    dual_levels = [3, 3, 4]
    for lv in dual_levels:
        levels.remove(lv)

    # 测试锚点先造：它们的残疾等级必须**精确**对应权重档位（w3→一级、
    # w2→二级、w1→三级），所以先从配额里扣掉再单独建。
    # 早先是随机等级顶上去的，结果锚点被造了个四级残疾、
    # 权重 1，自检当场拦下 —— 这种错如果漏进库，回归会红在别处，很难查。
    for idx, (_mid, _room, _w, lv, _note) in enumerate(ANCHORS):
        levels.remove(lv)
        people.append({
            "kind": "adult_dis", "dis_level": lv, "drown_risk": None,
            "age": random.randint(35, 68), "male": random.random() < 0.5,
            "_anchor": idx,
        })

    # 其余成人残疾人
    for lvl in levels:
        age = random.randint(19, 86)
        male = random.random() < 0.52
        people.append({
            "kind": "adult_dis", "dis_level": lvl,
            "drown_risk": None, "age": age, "male": male,
        })

    # 未成年残疾 + 防溺水（双标签）
    for lvl in dual_levels:
        age = random.randint(8, 15)
        male = random.random() < 0.5
        people.append({
            "kind": "dual", "dis_level": lvl,
            "drown_risk": "低风险", "age": age, "male": male,
        })

    # 纯防溺水（未成年人）
    for _ in range(n_drown_only):
        age = random.randint(4, 17)
        male = random.random() < 0.53
        risk = "中风险" if random.random() < 12 / max(1, DROWNING) else "低风险"
        people.append({
            "kind": "drown", "dis_level": None,
            "drown_risk": risk, "age": age, "male": male,
        })

    # 中风险人数校准到 12（配额制，别靠概率飘）
    mid_target = DROWN_RISK[0][1]
    mids = [p for p in people if p["drown_risk"] == "中风险"]
    lows = [p for p in people if p["drown_risk"] == "低风险"]
    while len(mids) > mid_target:
        p = mids.pop()
        p["drown_risk"] = "低风险"
        lows.append(p)
    while len(mids) < mid_target:
        p = lows.pop()
        p["drown_risk"] = "中风险"
        mids.append(p)

    # ---------- ② 算权重（与 personbook 的阈值口径一致）----------
    for p in people:
        if p["dis_level"] == 1:
            p["weight"] = 3
        elif p["dis_level"] == 2:
            p["weight"] = 2
        elif p["dis_level"] in (3, 4):
            p["weight"] = 1
        elif p["drown_risk"] == "中风险":
            p["weight"] = 2
        else:
            p["weight"] = 1

    # ---------- ③ 分配社区（锚点必须落在住武社区）----------
    random.shuffle(people)
    anchors = [p for p in people if p.get("_anchor") is not None]
    others = [p for p in people if p.get("_anchor") is None]
    assert len(anchors) == len(ANCHORS), "锚点丢失"

    for p in anchors:
        p["community"] = "住武社区"

    rest_slots = []
    for name, n in COMM:
        left = n - (len(anchors) if name == "住武社区" else 0)
        rest_slots += [name] * left
    random.shuffle(rest_slots)
    for p, c in zip(others, rest_slots):
        p["community"] = c

    # ---------- ④ 分配地址精度 ----------
    # 村子没有"栋/单元"，只能到组 —— 硬造门号会造出
    # "滴水岩村17栋4单元304室"这种一眼假的东西（第一版真造出来了）。
    # 所以：滴水岩村一律只到组；其余按精度配额分。
    rural = [p for p in others if p["community"] == "滴水岩村"]
    urban_ish = [p for p in others if p["community"] != "滴水岩村"]

    for p in anchors:
        p["addr_level"] = 3
    for p in rural:
        p["addr_level"] = 1

    need = {3: 124 - len(anchors), 2: 8, 1: 85 - len(rural), 0: 83}
    slots = []
    for lv, n in need.items():
        slots += [lv] * n
    assert len(slots) == len(urban_ish), \
        "精度配额对不上：%d 个名额 vs %d 人" % (len(slots), len(urban_ish))
    random.shuffle(slots)
    for p, lv in zip(urban_ish, slots):
        p["addr_level"] = lv

    # ---------- ⑤ 生成地址串 ----------
    used_rooms = set()
    for p in anchors:
        mid, room, _w, _l, _note = ANCHORS[p["_anchor"]]
        p["grp"] = random.choice(GROUPS)
        p["addr_mid"], p["addr_room"] = mid, room
        p["address"] = "%s%s%s室" % (p["community"], mid, room)
        used_rooms.add((p["community"], mid, room))

    for p in others:
        comm = p["community"]
        lv = p["addr_level"]
        p["grp"] = random.choice(VILLAGE_GROUPS if comm == "滴水岩村" else GROUPS)

        if lv == 3:
            # 门号级：必须唯一，否则两户人挤同一个门牌，反查会串
            for _ in range(400):
                b = random.choice(BLDGS)
                u = random.randint(1, 4)
                r = "%d%02d" % (random.randint(1, 6), random.randint(1, 4))
                key = (comm, "%s栋%d单元" % (b, u), r)
                if key in used_rooms:
                    continue
                used_rooms.add(key)
                p["addr_mid"], p["addr_room"] = key[1], key[2]
                p["address"] = "%s%s%s室" % (comm, key[1], key[2])
                break
        elif lv == 2:
            # 到栋+单元，没有门号（只出现在安置房/廉租房）
            p["addr_mid"] = "安置房%d-%d" % (random.randint(1, 6), random.randint(1, 3))
            p["addr_room"] = ""
            p["address"] = "%s%s" % (comm, p["addr_mid"])
        elif lv == 1:
            # 只到组 / 只到栋
            if comm == "滴水岩村":
                p["addr_mid"] = p["grp"]
                p["addr_room"] = ""
                p["address"] = "%s%s" % (comm, p["grp"])
            elif comm in ("以朵社区", ""):
                p["addr_mid"] = p["grp"]
                p["addr_room"] = ""
                p["address"] = "%s%s" % (comm or STREET, p["grp"])
            else:
                b = random.choice(BLDGS)
                p["addr_mid"] = "%s栋" % b
                p["addr_room"] = ""
                p["address"] = "%s%s栋" % (comm, b)
        else:
            p["addr_mid"], p["addr_room"] = "", ""
            p["address"] = comm if random.random() < 0.5 else STREET

    # ---------- ⑥ 身份字段 ----------
    for p in people:
        p["name"] = fake_name()
        p["birth"] = birth_of(p["age"])
        p["idcard"] = cn_idcard(p["birth"], p["male"])
        assert expire_ok(p["age"], p["birth"]), "年龄与生日不自洽"

    return people


def to_row(p):
    """转成 person 表的一行 + 若干 person_tag 行。"""
    tags = []
    if p["dis_level"] is not None:
        lvl_txt = {1: "一级", 2: "二级", 3: "三级", 4: "四级"}[p["dis_level"]]
        cat = random.choice(("肢体残疾", "听力残疾", "视力残疾", "智力残疾",
                             "精神残疾", "言语残疾"))
        tags.append(("残疾", "%s|%s" % (cat, lvl_txt),
                     {1: 3, 2: 2}.get(p["dis_level"], 1)))
        risk = lvl_txt
    else:
        risk = ""

    if p["drown_risk"] is not None:
        boost = 0
        if p["kind"] == "drown" and random.random() < 0.05:
            boost = 1        # 极少数"事实无人抚养"再升一格，与台账口径一致
        tags.append(("防溺水", "%s|重点水域附近" % p["drown_risk"],
                     min(3, {3: 3, 2: 2, 1: 1}[p["weight"]] + boost) if risk else p["weight"]))
        if not risk:
            risk = p["drown_risk"]

    guardian = gphone = ""
    if p["age"] < 18:
        guardian = fake_name()
        gphone = "1%d%08d" % (random.randint(3, 9), random.randint(0, 99999999))

    return {
        "id_hash": id_hash(p["idcard"]),
        "name_masked": mask_name(p["name"]),
        "id_masked": mask_id(p["idcard"]),
        "gender": "男" if p["male"] else "女",
        "age": p["age"],
        "street": STREET,
        "community": p["community"],
        "grp": p["grp"],
        "address": p["address"][:200],
        "addr_level": p["addr_level"],
        "addr_mid": p["addr_mid"],
        "addr_room": p["addr_room"],
        "risk_level": risk,
        "guardian_masked": mask_name(guardian),
        "guardian_phone": mask_phone(gphone),
        "source": "残疾人台账" if p["dis_level"] is not None else "防溺水台账",
        "batch": "SYNTH300",
    }, tags


def summarize(rows, tags):
    """tags 是扁平列表，每项 (id_hash, kind, detail, weight)。"""
    from collections import Counter
    print("\n=== 合成底册构成 ===")
    print("  人数 %d | 标签 %d" % (len(rows), len(tags)))
    print("  社区：", dict(Counter(r["community"] or "（未明确）" for r in rows)))
    lab = {3: "栋+单元+门号", 2: "栋+单元", 1: "仅到楼栋/组", 0: "无法定位"}
    for lv in (3, 2, 1, 0):
        n = sum(1 for r in rows if r["addr_level"] == lv)
        print("  %-14s %4d  %.1f%%" % (lab[lv], n, 100.0 * n / max(1, len(rows))))
    # 每人取最高权重（与 personbook.top_weight 同口径）
    best = {}
    for h, _kind, _detail, w in tags:
        best[h] = max(best.get(h, 0), w)
    print("  权重：", dict(sorted(Counter(best.values()).items())))
    print("  标签：", dict(Counter(t[1] for t in tags)))


def purge(conn):
    n1 = conn.execute("select count(*) from person").fetchone()[0]
    # 先删子表：外键约束会拦
    conn.execute("delete from person_tag")
    conn.execute("delete from person")
    conn.commit()
    print("  已清空 person_tag / person（原 %d 人）" % n1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库")
    ap.add_argument("--purge", action="store_true", help="只清空底册")
    args = ap.parse_args()

    if not os.path.exists(DB):
        raise SystemExit("找不到库：%s" % DB)

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys=ON")

    if args.purge:
        if not args.apply:
            n = conn.execute("select count(*) from person").fetchone()[0]
            print("（预演）将清空 %d 人；加 --apply 执行" % n)
            return
        purge(conn)
        return

    people = build()
    rows, tags = [], []
    for p in people:
        r, ts = to_row(p)
        rows.append(r)
        tags.extend((r["id_hash"],) + t for t in ts)

    # 自检：锚点必须存在且权重正确，否则 smoke_person 会红
    print("=== 自检：测试锚点 ===")
    for mid, room, w, lv, note in ANCHORS:
        hit = [r for r in rows if r["addr_mid"] == mid and r["addr_room"] == room]
        got = None
        if hit:
            h = hit[0]["id_hash"]
            got = max((t[3] for t in tags if t[0] == h), default=None)
        ok = hit and got == w
        print("  %-12s%-5s 权重应=%d 实=%s  %s  [%s]"
              % (mid, room, w, got, "OK" if ok else "!! 不符", note))
        if not ok:
            raise SystemExit("锚点未造对，中止（否则回归必红）")

    summarize(rows, tags)

    if not args.apply:
        print("\n（预演模式，未写库。加 --apply 才真正替换）")
        return

    conn.execute("delete from person_tag")
    conn.execute("delete from person")
    for r in rows:
        conn.execute(
            "insert into person (id_hash,name_masked,id_masked,gender,age,street,"
            "community,grp,address,addr_level,addr_mid,addr_room,risk_level,"
            "guardian_masked,guardian_phone,source,batch) "
            "values (:id_hash,:name_masked,:id_masked,:gender,:age,:street,"
            ":community,:grp,:address,:addr_level,:addr_mid,:addr_room,:risk_level,"
            ":guardian_masked,:guardian_phone,:source,:batch)", r)
    pid = {r[1]: r[0] for r in conn.execute("select id, id_hash from person")}
    for h, kind, detail, w in tags:
        conn.execute("insert into person_tag (person_id,kind,detail,risk_weight) "
                     "values (?,?,?,?)", (pid[h], kind, detail, w))
    conn.commit()
    print("\n写入完成：person=%d  person_tag=%d"
          % (conn.execute("select count(*) from person").fetchone()[0],
             conn.execute("select count(*) from person_tag").fetchone()[0]))
    conn.close()


if __name__ == "__main__":
    main()
