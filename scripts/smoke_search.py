"""回归：关键词搜索的语义（LIKE 的通配符 / 多词 / 中文子串）

## 为什么单独一套

docs/17 把"台账用 LIKE、不上 FTS5"定成一条**取舍**（依据见
`app/search.py` 顶部与 `scripts/bench_ledger_search.py` 的实测）。取舍只写在
文档里不牢靠：下次谁"顺手优化"成全文索引就能改坏，而改坏的表现是
**搜出全部**或者**搜不到**——两种都不报错、都有结果、界面看不出异常。

这套测试锁的是语义，不是按钮：

1. `% ` `_` `\\` 必须当字面量。用户搜 `%` 想要"含百分号的记录"，
   修好之前拿到的是"全表"；
2. 搜的是**内容值**，不是 JSON 键名。修好之前搜 `type`、搜一个下划线
   都能把整本台账捞出来（键名 `room_key` 里带 `_`）；
3. 多词是 **AND**（逐词都要命中），不是拼成一个长串——`"电梯 3号楼"`
   拼起来必然 0 条，用户只会认为"搜索坏了"；
4. **2 字中文词必须能搜到**。这一点是同 FTS5 方案对比的核心：
   unicode61 把整句当一个 token（命中 0），trigram 对 <3 字符静默返回 0。

用独立库，不碰 data/community.db。
"""
import json
import os
import pathlib
import sys
from datetime import datetime, timedelta
from urllib.parse import quote

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_search.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                                    # noqa: E402
from app.extensions import db                                  # noqa: E402
from app.models import KbDoc, Ledger, WorkItem, WorkOrder       # noqa: E402

app = create_app("dev")

# ---------------------------------------------------------------- 造数据
LEDGERS = [
    (1, "物业", {"type": "报修", "subject": "3号楼2单元电梯运行异响", "room_key": "3-2-201"}),
    (2, "物业", {"type": "报修", "subject": "5号楼外墙渗水", "room_key": "5-1-102"}),
    (3, "民政", {"type": "咨询", "subject": "低保补贴发放比例 100% 已公示", "room_key": "7-3-301"}),
    (4, "综治", {"type": "走访", "subject": "独居老人王秀兰每周走访", "room_key": "3-2-202"}),
    (5, "综治", {"type": "综治", "subject": "楼道堆物清理", "room_key": "5-1-103"}),
]

with app.app_context():
    for i, (lid, line, content) in enumerate(LEDGERS):
        db.session.add(Ledger(
            id=lid, line=line,
            content_json=json.dumps(content, ensure_ascii=False),
            reused=False,
            created_at=datetime.now() - timedelta(hours=i)))
    orders = [
        (1, "WO-001", "3号楼2单元电梯困人", "赵师傅"),
        (2, "WO-002", "5号楼地下车库积水", "钱师傅"),
        (3, "WO-003", "7号楼消防通道堆物", ""),
    ]
    for oid, no, content, who in orders:
        db.session.add(WorkItem(id=oid, type="报修", subject=content,
                                target_lines="物业", status="open"))
        db.session.add(WorkOrder(
            id=oid, order_no=no, work_item_id=oid, property_co="幸福里物业",
            content=content, sla_level="一般", sla_hours=72, status="doing",
            assignee=who or None,
            created_at=datetime.now() - timedelta(days=oid)))
    # 知识库：seed 已灌入演示文档，这里整表换成两条可控的，断言才好写
    db.session.query(KbDoc).delete()
    for kid, title in enumerate(["冬季供暖设施检修规程", "消防通道管理办法"], start=1):
        db.session.add(KbDoc(id=kid, title=title, category="制度",
                             content=title + "正文", source="街道下发"))
    db.session.commit()

# search_text 是否真的由 ORM 事件自动填上了（不靠调用方传参）
with app.app_context():
    lg = db.session.get(Ledger, 1)
    AUTO = lg.search_text or ""

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


def login(c, u, p="123456"):
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


def J(r):
    return r.get_json(silent=True) or {}


admin = app.test_client(); login(admin, "admin", "admin123")
comm = app.test_client(); login(comm, "wang")
prop = app.test_client(); login(prop, "wuye")

print("=" * 72)
print("关键词搜索语义 · 通配符 / 键名 / 多词 AND / 中文 2 字词")
print("=" * 72)


def led(qs):
    """台账搜索 → (matched, ids)"""
    d = J(comm.get("/api/ledgers?" + qs)).get("data") or {}
    c = d.get("counts") or {}
    return c.get("matched"), [i["id"] for i in d.get("items") or []], c.get("total")


# ---------------------------------------------------------------- 1 写入侧
print("\n[1] search_text 由 ORM 自动维护（不靠每个调用方记得传参）")
check("新建台账自动生成检索文本", "电梯" in AUTO and "3号楼" in AUTO, AUTO)
check("键名不进检索文本", "room_key" not in AUTO and "subject" not in AUTO, AUTO)
check("布尔标记不进检索文本", "True" not in AUTO and "False" not in AUTO, AUTO)

with app.app_context():
    lg = db.session.get(Ledger, 1)
    lg.content_json = json.dumps(
        {"type": "报修", "subject": "3号楼2单元电梯运行异响（已复测）",
         "room_key": "3-2-201"}, ensure_ascii=False)
    db.session.commit()
    UPDATED = db.session.get(Ledger, 1).search_text or ""
check("改内容后检索文本同步更新", "已复测" in UPDATED, UPDATED)

# ---------------------------------------------------------------- 2 通配符
print("\n[2] 通配符必须当字面量（改前搜 % 拿到全表）")
m, ids, total = led("q=" + quote("%"))
check("搜 % 只命中真含百分号的那条", m == 1 and ids == [3], f"{m} {ids}")
m2, ids2, _ = led("q=" + quote("100%"))
check("搜 100% 命比例那条", m2 == 1 and ids2 == [3], f"{m2} {ids2}")
m3, ids3, _ = led("q=" + quote("_"))
check("搜 _ 不命中全部（改前靠键名 room_key 里的下划线命中全表）",
      m3 == 0, f"{m3} {ids3}")
m4, _, _ = led("q=" + quote("\\"))
check("搜单个反斜杠：不 500、不命中", m4 == 0, m4)
r = comm.get("/api/ledgers?q=" + quote("'"))
check("搜单引号：不 500（参数化查询兜住）", r.status_code == 200, r.status_code)

# ---------------------------------------------------------------- 3 只搜内容
print("\n[3] 搜的是内容值，不是 JSON 键名")
for key in ("room_key", "subject", "type"):
    m, _, _ = led("q=" + quote(key))
    check(f"搜键名 {key} 不命中", m == 0, m)
m, ids, _ = led("q=" + quote("3-2-201"))
check("房门特征（值）仍能精确搜到", m == 1 and ids == [1], f"{m} {ids}")

# ---------------------------------------------------------------- 4 中文子串
print("\n[4] 中文子串：2 字词必须能搜到（FTS5 默认分词器正是在这里漏检）")
for kw, want in (("电梯", 1), ("渗水", 1), ("走访", 1), ("3号楼", 1)):
    m, _, _ = led("q=" + quote(kw))
    check(f"搜「{kw}」命中 {want} 条", m == want, m)
m, _, _ = led("q=" + quote("电梯运行异响"))
check("长词按连续子串命中（不依赖分词边界）", m == 1, m)
m, _, _ = led("q=" + quote("电梯异响"))
check("跨词的长串不命中（子串语义，不做语义联想）→ 0 条", m == 0, m)

# ---------------------------------------------------------------- 5 多词 AND
print("\n[5] 多词是 AND：逐词都要命中")
m, ids, _ = led("q=" + quote("电梯 3号楼"))
check("「电梯 3号楼」命中同一条", m == 1 and ids == [1], f"{m} {ids}")
m, _, _ = led("q=" + quote("电梯 渗水"))
check("两个词分属不同记录 → 0 条（AND 而非 OR）", m == 0, m)
m, ids, _ = led("q=" + quote("电梯\u30003号楼"))
check("全角空格也当分隔符", m == 1 and ids == [1], f"{m} {ids}")
m, _, _ = led("q=" + quote("  电梯   3号楼  "))
check("前后空白与重复空格不影响", m == 1, m)
m, _, _ = led("q=" + quote("电梯 不存在的词"))
check("其中一个词不存在 → 0 条", m == 0, m)
m, _, _ = led("q=" + quote("3号楼 2单元 电梯 运行 异响"))
check("超过 4 个词只取前 4 个（不报错、结果仍收窄）", m == 1, m)

# ---------------------------------------------------------------- 6 不改家底
print("\n[6] 搜索只改 matched，不改家底")
m, _, total = led("q=" + quote("电梯"))
check("matched 收窄到 1", m == 1, m)
check("total 仍是 5", total == 5, total)
m, _, total = led("")
check("空关键词 = 不过滤（5 条全回）", m == 5 and total == 5, f"{m} {total}")
m, _, total = led("q=" + quote("   "))
check("纯空白关键词 = 不过滤", m == 5, m)

# ---------------------------------------------------------------- 7 迁移兜底
print("\n[7] 老库 search_text 为 NULL 时，退到 content_json 上搜（宁可有假命中，不可搜不到）")
with app.app_context():
    from sqlalchemy import text as sql_text
    db.session.execute(sql_text("UPDATE ledger SET search_text=NULL WHERE id=2"))
    db.session.commit()
m, ids, _ = led("q=" + quote("渗水"))
check("search_text 为空的老台账仍搜得到", m == 1 and ids == [2], f"{m} {ids}")
with app.app_context():
    from app.auth import _backfill_ledger_search_text
    _backfill_ledger_search_text()
    db.session.commit()
    fixed = db.session.get(Ledger, 2).search_text or ""
check("回填函数能补齐 NULL", "渗水" in fixed, fixed)
m, ids, _ = led("q=" + quote("_"))
check("回填后搜 _ 仍不命中（不走 content_json 的键名）", m == 0, m)

# ---------------------------------------------------------------- 8 工单
print("\n[8] 工单搜索：处理人 / 多词 / 通配符")


def ords(qs):
    d = J(prop.get("/api/orders?" + qs)).get("data") or {}
    c = d.get("counts") or {}
    return c.get("matched"), [i["id"] for i in d.get("items") or []], c.get("total")


m, ids, _ = ords("q=" + quote("赵师傅"))
check("按处理人搜到单", m == 1 and ids == [1], f"{m} {ids}")
m, ids, _ = ords("q=" + quote("赵 师傅"))
check("处理人拆两词也能命中（AND across 列）", m == 1 and ids == [1], f"{m} {ids}")
m, ids, _ = ords("q=" + quote("消防"))
check("按内容搜到单", m == 1 and ids == [3], f"{m} {ids}")
m, _, _ = ords("q=" + quote("%"))
check("工单搜 % 不命中全部", m == 0, m)
m, _, _ = ords("q=" + quote("_"))
check("工单搜 _ 不命中全部", m == 0, m)
m, _, total = ords("q=" + quote("电梯"))
check("工单 matched 收窄但 total 不变", m == 1 and total == 3, f"{m} {total}")
m, ids, _ = ords("q=" + quote("电梯") + "&status=doing")
check("搜索与状态筛选可叠加", m == 1 and ids == [1], f"{m} {ids}")
m, _, _ = ords("q=" + quote("电梯") + "&status=done")
check("叠加一个不匹配的状态 → 0 条（筛选真生效）", m == 0, m)

# ---------------------------------------------------------------- 9 知识库 / 账号
print("\n[9] 知识库与账号列表：同一套 LIKE 构造，同样不认元字符")
d = J(comm.get("/api/kb/docs?q=" + quote("供暖"))).get("data") or {}
titles = [x["title"] for x in (d if isinstance(d, list) else d.get("items") or [])]
check("知识库标题搜「供暖」命中 1 条", len(titles) == 1, titles)
d = J(comm.get("/api/kb/docs?q=" + quote("_"))).get("data") or {}
titles = [x["title"] for x in (d if isinstance(d, list) else d.get("items") or [])]
check("知识库搜 _ 不命中全部", len(titles) == 0, titles)
d = J(admin.get("/api/admin/users?q=" + quote("%"))).get("data") or {}
users = d if isinstance(d, list) else d.get("items") or []
check("账号列表搜 % 不命中全部", len(users) == 0, len(users))
d = J(admin.get("/api/admin/users?q=wang")).get("data") or {}
users = d if isinstance(d, list) else d.get("items") or []
check("账号列表正常搜到 wang", len(users) == 1, [u.get("username") for u in users])

# ---------------------------------------------------------------- 10 落库不存明文
print("\n[10] 检索文本是 content 的第二份拷贝，必须与审计同等脱敏")
with app.app_context():
    db.session.add(Ledger(
        id=90, line="民政",
        content_json=json.dumps({"subject": "联系居民 13800138000 核实",
                                 "idcard": "320102199001011234"}, ensure_ascii=False),
        reused=False, created_at=datetime.now()))
    db.session.commit()
    txt = db.session.get(Ledger, 90).search_text or ""
check("search_text 内无手机号明文", "13800138000" not in txt, txt)
check("search_text 内无身份证明文", "320102199001011234" not in txt, txt)
check("脱敏后的号码仍保留可检索片段", "138****8000" in txt, txt)
m, _, _ = led("q=" + quote("核实"))
check("脱敏不影响按内容搜到这条", m >= 1, m)

# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 72)
print(f"通过 {PASS}   失败 {FAIL}")
if FAILED:
    print("失败项：")
    for n in FAILED:
        print("  -", n)
print("=" * 72)
sys.exit(1 if FAIL else 0)
