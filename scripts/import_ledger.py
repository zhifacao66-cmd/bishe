"""把街道下发的重点人员台账导入人员底册。

四张表分两类，处理方式完全不同：

| 表 | 实测 | 处理 |
|---|---|---|
| 残疾人台账 | 559 条真实数据 | 导入 |
| 防溺水台账 | 2466 条真实数据 | 导入 |
| 矛盾纠纷排查登记表 | 0 条（空表头模板） | 只校验列结构，不导入 |
| 留守儿童台账 | 0 条（空表头模板） | 只校验列结构，不导入 |

用法
----
    python scripts/import_ledger.py --check          # 只看会导入多少，不写库
    python scripts/import_ledger.py --apply          # 真的写库（先自动备份）
    python scripts/import_ledger.py --purge --apply   # 清空旧底册再导（换批次时用）

四条不能松的口径
----------------
1. **身份证不落明文**。只存 sha256 前 32 位（去重键）+ 前 6 后 4 的掩码。
   原表有 3000+ 条真实身份证，明文入库后备份、导出、投屏全是泄露面。
   姓名同理，只留姓。
2. **去重只能靠身份证**。原表内部有 164 个身份证重复出现（多出 166 行），
   残疾人表与防溺水表之间另有 15 人重叠（既残疾又是未成年人）。
   按姓名合并会把人并错——同社区同名同姓并不罕见。
3. **地址能识别到哪一级就记到哪一级**，不补、不猜。
   实测只有 35.6% 能到"栋+单元+门号"，53.7% 只写到"水城区"。
   强行归类会让"这户有重点人员"的提级判断变成误报。
4. **社区归属认不出就留空**。"以朵安置房"是小区名不是社区名，不能因为
   字面含"以朵"就归到以朵社区。留空的记录只有街道角色看得到。
"""
import argparse
import hashlib
import os
import re
import shutil
import sys
from collections import Counter
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# 台账原件目录。默认 data/incoming/real_ledgers，可用环境变量 LEDGER_SRC 覆盖。
# 为什么要可配置：原件里有居民身份证号，平时应该放在项目目录**之外**
# （打包、压缩、拷 U 盘都不会带走）。测试也靠这个变量指向合成夹具，
# 不再依赖真实台账 —— 否则跑一次回归就把真实数据导回演示库了。
SRC_DIR = os.environ.get("LEDGER_SRC") or os.path.join(
    BASE, "data", "incoming", "real_ledgers")
DB = os.path.join(BASE, "data", "community.db")

FILES = {
    "disabled": ("disabled.xlsx", "残疾人台账", 2),
    "drowning": ("drowning.xlsx", "防溺水台账", 2),
    "dispute": ("dispute.xlsx", "矛盾纠纷排查登记表", 2),
    "leftbehind": ("leftbehind.xlsx", "留守儿童台账", 3),
}

# 中文数字 → 阿拉伯。台账里「20栋一单元403室」和「20栋1单元403室」混着写，
# 不归一化就有 8 条地址解析不出门号（实测 level=1 的那 8 条全是这个原因）。
CN_NUM = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
          "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}

# 台账里真实出现的社区名。只认明确写了的，不做字面联想。
COMMUNITIES = (("住武社区", "住武社区"), ("滴水岩村", "滴水岩村"),
               ("以朵社区", "以朵社区"))

# 残疾等级 → 风险权重（3=重点看护 2=重点关注 1=需关注）
DIS_WEIGHT = {"一级": 3, "二级": 2, "三级": 1, "四级": 1}
# 防溺水风险等级 → 权重
DROWN_WEIGHT = {"高风险": 3, "中风险": 2, "低风险": 1}
# 风险类型里这几类单独加一档：监护缺失比"正常家庭监护"更需要盯
DROWN_TYPE_BOOST = {"事实无人抚养儿童", "监护力量薄弱", "委托寄养监护",
                    "经常独自居家", "低龄儿童"}


def norm_addr(text):
    """地址规范化：中文数字单元/栋 → 阿拉伯数字。"""
    t = (text or "").strip()
    for k, v in CN_NUM.items():
        t = t.replace(f"{k}单元", f"{v}单元")
    return t


def parse_addr(text):
    """地址分级解析。返回 (level, mid, room, community)。

    level 3 = 栋+单元+门号（能匹配到户）
          2 = 栋+单元 或 横杠格式（能到单元）
          1 = 只有组/寨 或 只有栋
          0 = 只到区/街道，认不出

    `community` 只在地址里**明确写了社区名**时才给值。
    「以朵安置房」不含社区归属——那是小区名，归谁管要看街道划定，不能猜。
    """
    t = norm_addr(text)
    if not t:
        return 0, "", "", ""

    comm = ""
    for key, name in COMMUNITIES:
        if key in t:
            comm = name
            break

    # ① 标准写法：3栋3单元303室 / 12栋2单元101
    m = re.search(r"(\d{1,3})\s*栋\s*(\d{1,2})\s*单元\s*(\d{2,5})", t)
    if m:
        return 3, f"{m.group(1)}栋{m.group(2)}单元", m.group(3), comm

    # ② 安置房/廉租房横杠写法：安置房4-2-15-2 / 3期廉租房3-3-4 / 廉租房6-4
    #    段数不固定（3~4 段），取前两段当"栋-单元"，完整串留作 mid 便于人工核对。
    m = re.search(r"(安置房|安置小区|廉租房)\s*([\d\-]{3,20})", t)
    if m:
        seg = [x for x in m.group(2).split("-") if x]
        if len(seg) >= 3:
            return 2, f"{m.group(1)}{seg[0]}-{seg[1]}", "-".join(seg[2:]), comm
        if len(seg) == 2:
            return 2, f"{m.group(1)}{seg[0]}", seg[1], comm

    # ③ 组/寨：住武社区陈家寨组 / 李家寨组 / 赵家寨 —— 只能到组
    m = re.search(r"([\u4e00-\u9fa5]{1,4}(?:寨|组))", t)
    if m:
        return 1, m.group(1), "", comm

    # ④ 只有栋，没有单元门号
    m = re.search(r"(\d{1,3})\s*栋", t)
    if m:
        return 1, f"{m.group(1)}栋", "", comm

    return 0, "", "", comm


def id_hash(v):
    s = str(v or "").strip().upper()
    return hashlib.sha256(s.encode()).hexdigest()[:32] if len(s) >= 15 else ""


def mask_id(v):
    s = str(v or "").strip().upper()
    if len(s) < 10:
        return ""
    return s[:6] + "*" * 8 + s[-4:]


def mask_name(v):
    s = str(v or "").strip()
    if not s:
        return ""
    return s[0] + "*" * (len(s) - 1)


def mask_phone(v):
    """电话掩码。**任何非空电话都必须带 `****`**——
    原表里有 5 条短号（位数不足 11），早先的分支只给了 3 个星号，
    于是"库里还有明文电话"的检查一直红着，实际是掩码格式不统一。
    掩码规则统一成"保留头尾 + 4 星"，检查与展示才有唯一口径。
    """
    s = re.sub(r"\D", "", str(v or ""))
    if not s:
        return ""
    if len(s) >= 11:
        return s[:3] + "****" + s[-4:]
    if len(s) >= 5:
        return s[:2] + "****" + s[-2:]
    return "****"


def clean_age(v):
    try:
        n = int(float(str(v).strip()))
        return n if 0 <= n <= 120 else None
    except (TypeError, ValueError):
        return None


class Collector:
    """收集 + 去重。同一个人多条记录时合并标签，取更完整的地址。"""

    def __init__(self):
        self.rows = {}          # id_hash -> dict
        self.stat = Counter()
        self.bad = []           # 脏数据明细

    def add(self, *, name, idcard, gender, age, addr, community, grp,
            tags, guardian="", gphone="", risk="", source=""):
        h = id_hash(idcard)
        if not h:
            self.stat["跳过_无有效身份证"] += 1
            return
        if not str(name or "").strip():
            self.stat["跳过_无姓名"] += 1
            return

        level, mid, room, comm_from_addr = parse_addr(addr)
        comm = community or comm_from_addr
        a = norm_addr(addr)

        hit = self.rows.get(h)
        if hit:
            self.stat["重复_合并"] += 1
            for t in tags:
                if t not in hit["tags"]:
                    hit["tags"].append(t)
            # 地址取精度更高的那份；同精度优先保留有社区归属的
            if level > hit["addr_level"]:
                hit.update(addr=a, addr_level=level, addr_mid=mid, addr_room=room)
            if comm and not hit["community"]:
                hit["community"] = comm
            if not hit["grp"] and grp:
                hit["grp"] = grp
            return

        self.rows[h] = {
            "id_hash": h, "name": mask_name(name), "id_masked": mask_id(idcard),
            "gender": str(gender or "").strip()[:2], "age": clean_age(age),
            "street": "以朵街道", "community": comm, "grp": str(grp or "").strip(),
            "address": a[:200], "addr_level": level, "addr_mid": mid,
            "addr_room": room, "risk_level": str(risk or "").strip()[:20],
            "guardian": mask_name(guardian), "gphone": mask_phone(gphone),
            "source": source, "tags": list(tags),
        }
        self.stat["新增"] += 1


def read_disabled(c):
    """残疾人台账：17 列，表头在第 2 行，社区列（第 8 列）是明确的。"""
    import openpyxl
    p = os.path.join(SRC_DIR, FILES["disabled"][0])
    wb = openpyxl.load_workbook(p, data_only=True)
    ws = wb.worksheets[0]
    for r in range(3, ws.max_row + 1):
        name = ws.cell(row=r, column=2).value
        if not name:
            continue
        c.stat["残疾_原始行"] += 1
        cat = str(ws.cell(row=r, column=11).value or "").strip()
        lvl = str(ws.cell(row=r, column=12).value or "").strip()
        comm = str(ws.cell(row=r, column=8).value or "").strip()
        if comm and comm not in [x[1] for x in COMMUNITIES]:
            comm = ""       # 台账里出现过的非本街道社区名，不采信
        c.add(name=name, idcard=ws.cell(row=r, column=3).value,
              gender=ws.cell(row=r, column=6).value,
              age=ws.cell(row=r, column=5).value,
              addr=ws.cell(row=r, column=10).value, community=comm,
              grp=ws.cell(row=r, column=9).value,
              tags=[("残疾", f"{cat}|{lvl}", DIS_WEIGHT.get(lvl, 1))],
              risk=lvl, source="残疾人台账")
    wb.close()


def read_drowning(c):
    """防溺水台账：21 列，无社区列，只能从地址推。"""
    import openpyxl
    p = os.path.join(SRC_DIR, FILES["drowning"][0])
    wb = openpyxl.load_workbook(p, data_only=True)
    ws = wb.worksheets[0]
    for r in range(3, ws.max_row + 1):
        name = ws.cell(row=r, column=2).value
        if not name:
            continue
        c.stat["防溺_原始行"] += 1
        risk = str(ws.cell(row=r, column=8).value or "").strip()
        rtype = str(ws.cell(row=r, column=9).value or "").strip()
        w = DROWN_WEIGHT.get(risk, 1)
        if rtype in DROWN_TYPE_BOOST:
            w = min(3, w + 1)
        c.add(name=name, idcard=ws.cell(row=r, column=5).value,
              gender=ws.cell(row=r, column=3).value,
              age=ws.cell(row=r, column=4).value,
              addr=ws.cell(row=r, column=7).value, community="", grp="",
              tags=[("防溺水", f"{risk}|{rtype}", w)],
              guardian=ws.cell(row=r, column=13).value,
              gphone=ws.cell(row=r, column=14).value,
              risk=risk, source="防溺水台账")
    wb.close()


def check_templates():
    """两张空模板：确认列结构没变。模板是导出格式的依据，列变了导出就错位。"""
    import openpyxl
    from app.personbook import EXPORT_SPECS
    out = []
    for key, spec_key in (("dispute", None), ("leftbehind", "leftbehind")):
        p = os.path.join(SRC_DIR, FILES[key][0])
        if not os.path.exists(p):
            out.append((FILES[key][1], "文件缺失", 0, 0))
            continue
        wb = openpyxl.load_workbook(p, data_only=True)
        ws = wb.worksheets[0]
        hdr_row = FILES[key][2]
        cols = [str(ws.cell(row=hdr_row, column=i).value or "").strip()
                for i in range(1, ws.max_column + 1)]
        cols = [x for x in cols if x]
        data_rows = sum(1 for r in range(hdr_row + 1, ws.max_row + 1)
                        if ws.cell(row=r, column=2).value)
        status = "空模板（%d 列）" % len(cols)
        if spec_key and spec_key in EXPORT_SPECS:
            mine = [x[0].replace("\n", "") for x in EXPORT_SPECS[spec_key]["columns"]]
            if len(mine) != len(cols):
                status += " ← 与导出规格列数不一致(%d)" % len(mine)
        out.append((FILES[key][1], status, data_rows, len(cols)))
        wb.close()
    return out


def report(c):
    print("\n=== 清洗结果 ===")
    for k in sorted(c.stat):
        print("  %-20s %d" % (k, c.stat[k]))

    rows = list(c.rows.values())
    print("\n=== 去重后人数：%d ===" % len(rows))

    lvl = Counter(r["addr_level"] for r in rows)
    print("地址定位精度：")
    for level, label in ((3, "栋+单元+门号"), (2, "栋+单元"),
                         (1, "仅到楼栋/组"), (0, "无法定位")):
        n = lvl.get(level, 0)
        print("  %-14s %5d  %5.1f%%" % (label, n, n / len(rows) * 100 if rows else 0))

    print("社区归属：")
    for k, v in Counter(r["community"] or "（未明确）" for r in rows).most_common():
        print("  %-14s %5d" % (k, v))

    print("标签分布：")
    tag_kind = Counter(t[0] for r in rows for t in r["tags"])
    for k, v in tag_kind.most_common():
        print("  %-10s %5d" % (k, v))
    multi = sum(1 for r in rows if len({t[0] for t in r["tags"]}) > 1)
    print("  多标签人数  %5d" % multi)

    w = Counter(max(t[2] for t in r["tags"]) for r in rows)
    print("最高风险权重：3=%d  2=%d  1=%d"
          % (w.get(3, 0), w.get(2, 0), w.get(1, 0)))


def apply(c, purge=False):
    from app import create_app
    from app.extensions import db
    from app.models import Person, PersonTag

    # 收集器用短字段名（name/guardian/gphone），模型列名带脱敏后缀。
    # 映射集中放这里，改动只有一处；散在各处必然漏。
    FIELD_MAP = {"name": "name_masked", "guardian": "guardian_masked",
                 "gphone": "guardian_phone", "addr": "address"}

    batch = datetime.now().strftime("%y%m%d%H%M")
    app = create_app("dev")
    with app.app_context():
        if purge:
            # 先删子表再删主表：反了会 FOREIGN KEY constraint failed（person_tag 指向 person）
            PersonTag.query.delete()
            old = Person.query.delete()
            db.session.commit()
            print("  已清空旧底册 %d 人" % old)

        exist = {p.id_hash for p in Person.query.with_entities(Person.id_hash).all()}
        added = merged = 0
        for r in c.rows.values():
            tags = r.pop("tags")
            row = {FIELD_MAP.get(k, k): v for k, v in r.items()}
            if row["id_hash"] in exist:
                p = Person.query.filter_by(id_hash=row["id_hash"]).first()
                have = {(t.kind, t.detail) for t in p.tags}
                for kind, detail, w in tags:
                    if (kind, detail) not in have:
                        db.session.add(PersonTag(person_id=p.id, kind=kind,
                                                 detail=detail, risk_weight=w))
                merged += 1
                continue
            p = Person(batch=batch, **row)
            db.session.add(p)
            db.session.flush()
            for kind, detail, w in tags:
                db.session.add(PersonTag(person_id=p.id, kind=kind,
                                         detail=detail, risk_weight=w))
            added += 1
        db.session.commit()
        print("  写入：新增 %d 人，补充标签 %d 人，批次 %s" % (added, merged, batch))
        print("  库内合计：%d 人 / %d 标签"
              % (Person.query.count(), PersonTag.query.count()))


def backup():
    """用 sqlite3.backup() 而不是复制文件：复制会丢 WAL 里未 checkpoint 的事务。"""
    import sqlite3
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(BASE, "data", "backups", "community-before-import-%s.db" % stamp)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    src = sqlite3.connect(DB)
    out = sqlite3.connect(dst)
    with out:
        src.backup(out)
    out.close()
    src.close()
    print("  已备份 → %s" % os.path.relpath(dst, BASE))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只统计，不写库")
    ap.add_argument("--apply", action="store_true", help="写库（自动备份）")
    ap.add_argument("--purge", action="store_true", help="先清空旧底册")
    args = ap.parse_args()

    missing = [f for f, _, _ in FILES.values()
               if not os.path.exists(os.path.join(SRC_DIR, f))]
    if missing:
        print("缺少源文件：%s" % "、".join(missing))
        print("当前源目录：%s" % SRC_DIR)
        print()
        print("台账原件里有居民身份证号，**默认不放在项目内**。")
        print("它们现在应该在：F:\\bise4-archive\\ledger-originals\\")
        print("要跑真实台账，先指过去：")
        print()
        print('  export LEDGER_SRC="F:/bise4-archive/ledger-originals"')
        print("  python scripts/import_ledger.py --check")
        print()
        print("演示/答辩不需要真实台账 —— 底册已由 gen_demo_persons.py")
        print("生成 300 条合成数据，结构与真实台账同构，不含任何真人。")
        return 1

    print("=== 一、两张空模板的列结构核对 ===")
    for name, status, rows, cols in check_templates():
        print("  %-22s %-28s 数据行=%d 列=%d" % (name, status, rows, cols))

    print("\n=== 二、两张真实数据表清洗 ===")
    c = Collector()
    read_disabled(c)
    read_drowning(c)
    report(c)

    if args.check or not args.apply:
        print("\n（预演模式，未写库。加 --apply 才真正导入）")
        return 0

    print("\n=== 三、写库 ===")
    backup()
    apply(c, purge=args.purge)
    return 0


if __name__ == "__main__":
    sys.exit(main())
