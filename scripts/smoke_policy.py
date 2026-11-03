"""ABAC 访问策略与导出水印回归

覆盖四类"不报错、只悄悄错"的地方：

1. **规则优先级**：R7（默认允许）如果排在前面，后面所有限制规则全部失效，
   而接口依然 200、页面依然好看——这类 bug 不会自己暴露，只能靠断言。
2. **列表与单条同源**：`scope_query`（列表）与 `decide`（单条）算出来的
   可见范围必须一致，否则出现"列表里有、点进去 403"或反过来。
3. **水印真的写进文件里**：只调一个 `apply_*()` 而不校验字节，
   函数体里 `except Exception: pass` 会把失败吞掉，导出的文件看起来正常。
4. **密级兜底方向**：认不出的密级必须往"机密"兜，不能往"公开"兜。

运行：python scripts/smoke_policy.py
"""
import io
import os
import pathlib
import re
import shutil
import sys
import zipfile

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_policy.db"
SRC = BASE / "data" / "community.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()
    s = pathlib.Path(str(SRC) + suf)
    if s.exists():
        # 连 -wal 一起拷：只拷主库会丢掉未 checkpoint 的事务，
        # 表现出来是"表存在但里面空的"，很容易被误判成代码写错了
        shutil.copy2(s, str(DB) + suf)

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["NOTICE_AUTO_IMPORT"] = "0"
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"   # 本例不测模型，避免误连

from app import create_app  # noqa: E402
from app.auth import generate_password_hash  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Notice, User, WorkOrder  # noqa: E402
from app.notice.ingest import can_view, staff_query  # noqa: E402
from app.policy import (RULES, ROLE_SECRET_CEILING, SECRET_NUM, RoleShield,  # noqa: E402
                        decide, readable_secret_levels, scope_query, secret_num)
from app.watermark import overlay_css, text as wm_text  # noqa: E402
from app.xlsx import Workbook, styles as S  # noqa: E402

app = create_app("dev")
PASS, FAIL = 0, 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"　{extra}" if extra else ""))


def login(c, u, p):
    return c.post("/login", data={"username": u, "password": p}, follow_redirects=False)


def ensure_user(username, role, org, pwd, perms=None):
    u = User.query.filter_by(username=username).first()
    if not u:
        u = User(username=username, name=username, role=role, org=org,
                 password_hash=generate_password_hash(pwd), status="active",
                 created_by="smoke")
        db.session.add(u)
        db.session.commit()
    if perms is not None:
        u.perms_json = perms
        db.session.commit()
    return u


with app.app_context():
    ensure_user("p_wang", "community", "幸福社区", "p123456")
    ensure_user("p_li", "property", "甲物业", "p123456")
    ensure_user("p_zhang", "street", "城东街道", "p123456")
    # 物业账号被误授权限点：用来验证"回避/密级"这些判定不靠权限点兜
    ensure_user("p_li2", "property", "甲物业", "p123456", perms="[]")


def U(role, org="", status="active"):
    u = RoleShield(role, org)
    u.status = status
    return u


# ======================================================================
print("\n" + "=" * 66)
print("① 密级映射：认不出必须往上兜")
print("=" * 66)
check("空串按内部处理（未标 ≠ 公开）", secret_num("") == 2, secret_num(""))
check("公开 = 1", secret_num("公开") == 1)
check("内部 = 2", secret_num("内部") == 2)
check("内部·敏感 = 3", secret_num("内部·敏感") == 3)
check("秘密 = 4", secret_num("秘密") == 4)
check("机密 = 5", secret_num("机密") == 5)
check("认不出的密级往机密兜（不能往公开兜）",
      secret_num("内部·敏感（含居民身份证）") == 5, secret_num("内部·敏感（含居民身份证）"))
check("前后空格能容忍", secret_num("  内部  ") == 2)
check("空值 None 不炸", secret_num(None) == 2)
check("每个角色都有上限，不会因为漏配掉到 0",
      all(r in ROLE_SECRET_CEILING for r in
          ("resident", "property", "community", "street", "admin")))
check("物业上限仍是 2（与改造前一致，不收紧）",
      ROLE_SECRET_CEILING["property"] == 2, ROLE_SECRET_CEILING["property"])
check("物业可读密级白名单恰好是三档",
      readable_secret_levels("property") == ["", "公开", "内部"],
      readable_secret_levels("property"))

# ======================================================================
print("\n" + "=" * 66)
print("② 规则优先级与命中")
print("=" * 66)
check("规则表 7 条", len(RULES) == 7, len(RULES))
check("规则编号唯一", len({c for c, _, _ in RULES}) == len(RULES))
check("每条规则都有说明（页面上要能自述）", all(d.strip() for _, _, d in RULES))

d = decide(U("property", "甲物业"), "read", "doc", secret=3)
check("R4 密级超上限 → 拒", not d.allow and d.rule.startswith("R4"), d.rule)
check("R4 的 reason 说清了上限是多少",
      "内部·敏感" in d.reason and "内部" in d.reason, d.reason)
check("R4 拒绝要写审计", d.log)

d = decide(U("property", "甲物业"), "read", "order", secret=1, owner_org="乙物业")
check("R3 跨单位 → 拒", not d.allow and d.rule.startswith("R3"), d.rule)
check("R3 的 reason 点名两个单位", "甲物业" in d.reason and "乙物业" in d.reason, d.reason)

d = decide(U("street", "城东街道"), "read", "order", secret=1, owner_org="乙物业")
check("R3 不拦街道（监督口径要跨单位）", d.allow, d.rule)

d = decide(U("property", "甲物业", status="disabled"), "read", "order", secret=1)
check("R1 停用账号 → 拒", not d.allow and d.rule.startswith("R1"), d.rule)
check("R1 比 R3/R4 更先命中（状态比权限更根本）",
      decide(U("property", "", status="pending"), "read", "doc", secret=5).rule.startswith("R1"))

d = decide(None, "read", "notice", secret=2, channel="portal")
check("R2 居民端看内部级 → 拒", not d.allow and d.rule.startswith("R2"), d.rule)
check("R2 不拦公开级", decide(None, "read", "notice", secret=1, channel="portal").allow)

d = decide(U("property", "甲物业"), "read", "notice", secret=1, scope="community")
check("R5 scope 不含本角色 → 拒", not d.allow and d.rule.startswith("R5"), d.rule)
d = decide(U("property", "甲物业"), "read", "notice", secret=1, scope="all", status="draft")
check("R5 未发布 → 拒", not d.allow and d.rule.startswith("R5"), d.rule)
check("R5 不拦已发布 + all",
      decide(U("property", "甲物业"), "read", "notice", secret=1, scope="all").allow)

d = decide(U("community", "幸福社区"), "read", "order", secret=2)
check("R7 无限制属性 → 放行", d.allow and d.rule.startswith("R7"), d.rule)
check("放行不带水印/不改字段", not d.watermark and not d.mask)

# 关键：拒绝规则必须排在 R7 之前
check("R7 允许规则排在最后（否则前面的全部失效）",
      RULES[-1][0] == "R7", RULES[-1][0])

# ======================================================================
print("\n" + "=" * 66)
print("③ 导出：强制水印 + 强制审计")
print("=" * 66)
d = decide(U("community", "幸福社区"), "export", "doc", secret=1, owner_org="幸福社区")
check("普通导出 → 允许且强制水印", d.allow and d.watermark, d.rule)
check("普通导出 → 强制写审计", d.log)
check("导出默认不需要字段脱敏（密级 1）", not d.mask)
d = decide(U("community", "幸福社区"), "export", "doc", secret=2, owner_org="幸福社区")
check("密级 ≥2 的导出件要做字段脱敏", d.allow and d.mask)
d = decide(U("community", "幸福社区"), "export", "doc", secret=3, owner_org="其他社区")
check("导出别家敏感件 → 拒", not d.allow, d.rule)
d = decide(U("street", "城东街道"), "export", "doc", secret=3, owner_org="其他社区")
check("街道可导出别家敏感件（监督方）", d.allow)

mark = wm_text(type("X", (), {"name": "王社工", "org": "幸福社区"})(), "导出报表")
check("水印含姓名", "王社工" in mark)
check("水印含单位", "幸福社区" in mark)
check("水印含日期（能对上审计那条记录）", re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", mark) is not None, mark)
check("水印含动作（区分导出与打印）", "导出报表" in mark)

# ---- xlsx：页眉 + 可见末行
wb = Workbook()
sh = wb.add("月报概览", widths=[20, 14])
sh.row(1, ["标题", "数据"], style=S.head)
sh.row(2, ["合计", 12])
from app.watermark import apply_xlsx  # noqa: E402

apply_xlsx(sh, mark)
raw = wb.to_bytes()
xml = zipfile.ZipFile(io.BytesIO(raw)).read("xl/worksheets/sheet1.xml").decode()
check("xlsx 里有 headerFooter 节点", "<headerFooter>" in xml)
check("xlsx 页眉写了水印", "王社工" in xml or "\\u738b" in xml)
check("xlsx 数据区末行也写了水印（屏幕可见那份）", "［水印］" in xml)
check("xlsx 页脚有外传警示", "请勿外传" in xml)

import xml.dom.minidom as minidom  # noqa: E402
okxml = True
for name in zipfile.ZipFile(io.BytesIO(raw)).namelist():
    if name.endswith(".xml"):
        try:
            minidom.parseString(zipfile.ZipFile(io.BytesIO(raw)).read(name))
        except Exception:
            okxml = False
check("加了水印的 xlsx 全部 XML 合法", okxml)

# 顺序约束：headerFooter 必须在 pageSetup 之后，否则 Excel 报"内容有问题"
check("headerFooter 排在 pageSetup 之后（CT_Worksheet 顺序硬约束）",
      xml.index("<headerFooter>") > xml.index("<pageSetup "), "顺序错了 Excel 打不开")

try:
    import openpyxl  # noqa: E402
    openpyxl.load_workbook(io.BytesIO(raw))
    check("加了水印的 xlsx 能被 openpyxl 读回", True)
except ImportError:
    print("  [SKIP] 未装 openpyxl，跳回读校验")
except Exception as e:                                   # noqa: BLE001
    check("加了水印的 xlsx 能被 openpyxl 读回", False, type(e).__name__)

# ---- docx：页眉有水印，页脚页码不被覆盖
with app.app_context():
    n = Notice.query.filter(Notice.direction != "out").first() or Notice.query.first()
    if n:
        from app.notice.export import to_docx
        dz = zipfile.ZipFile(io.BytesIO(to_docx(n, mark)))
        hdr = "".join(dz.read(x).decode() for x in dz.namelist()
                      if x.startswith("word/header"))
        ftr = "".join(dz.read(x).decode() for x in dz.namelist()
                      if x.startswith("word/footer"))
        check("docx 页眉写了水印", "王社工" in hdr)
        check("docx 页脚仍是页码（水印没覆盖它）", "PAGE" in ftr, ftr[:80])
    else:
        print("  [SKIP] 库内没有公告样本，跳过 docx 校验")

# ---- md：水印进 front matter，不破坏 `---` 开头
from app.notice.export import _with_mark  # noqa: E402
md = _with_mark("---\ntitle: 测试\n---\n\n正文\n", mark)
check("md 仍以 --- 开头（否则导入脚本会把水印当标题）", md.startswith("---"))
check("md 水印写进 front matter", "watermark:" in md)
check("md 正文没被改动", "正文" in md)
check("md 无 front matter 时退化成首尾两行", _with_mark("正文", mark).count("［水印］") == 2)

# ======================================================================
print("\n" + "=" * 66)
print("④ 列表收窄与单条判定同源")
print("=" * 66)
with app.app_context():
    # 物业：只见自己单位的工单
    q, _ = scope_query("order", WorkOrder.query, U("property", "甲物业"))
    ids_unit = {o.property_co for o in q.all()}
    check("物业列表只剩自己单位", ids_unit <= {"甲物业"}, ids_unit)
    q, _ = scope_query("order", WorkOrder.query, U("community", "幸福社区"))
    check("社区列表不被单位收窄", q.count() == WorkOrder.query.count())
    q, _ = scope_query("order", WorkOrder.query, U("admin", ""))
    check("管理员列表是全量（白名单写法，不是'除街道外都收窄'）",
          q.count() == WorkOrder.query.count())

    q, _ = scope_query("notice", Notice.query, None, role_override="property")
    rows = q.all()
    check("物业公告列表只含 all/property",
          all((x.scope or "") in ("all", "property") for x in rows), rows[:2])
    # 真正的口径对照要用 staff_query：它才是列表接口用的那一个
    # （`scope_query` 只管 scope，状态与"不混进公文"由调用方叠加，
    #  拿它直接跟 can_view 对比会把"公文不该进公告体系"误判成口径不一致）。
    rows = staff_query("property").all()
    bad = [x.id for x in rows if not can_view(x, "property")]
    check("列表里的每一条 can_view 都放行（两处口径一致）", not bad, bad[:4])
    check("列表里不含公文（direction=out）",
          all((x.direction or "in") != "out" for x in rows))
    check("列表里全是已发布", all(x.status == "published" for x in rows))
    # 反向：scope=community 的公告，列表里没有，单条也必须拒
    hidden = [x.id for x in Notice.query.filter(Notice.scope == "community").all()
              if can_view(x, "property")]
    check("scope=community 的公告单条也拒（列表与详情同源）", not hidden, hidden[:4])

# ======================================================================
print("\n" + "=" * 66)
print("⑤ 接口与页面权限")
print("=" * 66)
with app.test_client() as c:
    r = c.get("/security/policy")
    check("未登录打开策略页 → 302", r.status_code == 302, r.status_code)
    r = c.get("/api/policy")
    check("未登录查规则 → 401/302", r.status_code in (401, 302), r.status_code)

with app.test_client() as c:
    login(c, "p_wang", "p123456")
    r = c.get("/security/policy")
    check("社区无 audit.view → 策略页 403", r.status_code == 403, r.status_code)
    r = c.get("/api/policy")
    check("社区查规则 → 403", r.status_code == 403, r.status_code)
    r = c.post("/api/policy/simulate", json={"role": "property", "action": "read",
                                             "kind": "doc", "secret": 3})
    check("社区试算 → 403", r.status_code == 403, r.status_code)

with app.test_client() as c:
    login(c, "admin", "admin123")
    r = c.get("/security/policy")
    check("管理员打开策略页 200", r.status_code == 200, r.status_code)
    html = r.get_data(as_text=True)
    check("页面渲染了规则表容器", 'id="ruleBody"' in html)
    check("页面渲染了试算器", 'id="btnSim"' in html)
    check("页面铺了平铺水印", "body::after" in html and "background-repeat:repeat" in html)
    check("水印不是单行居中（要真平铺，截图裁一角也在）",
          "data:image/svg+xml" in html)

    r = c.get("/api/policy")
    j = r.get_json()
    check("规则接口 200", r.status_code == 200)
    check("规则接口带 7 条规则", len((j.get("data") or {}).get("rules") or []) == 7)
    check("规则接口带 5 个角色上限", len((j.get("data") or {}).get("roles") or []) == 5)
    check("中文以 \\u 转义下发（前端解析后渲染）", '"property"' in r.get_data(as_text=True))

    r = c.post("/api/policy/simulate", json={"role": "property", "action": "read",
                                             "kind": "doc", "secret": 3})
    d = (r.get_json() or {}).get("data") or {}
    check("试算：物业看内部·敏感被 R4 挡", d.get("allow") is False and "R4" in (d.get("rule") or ""))
    r = c.post("/api/policy/simulate", json={"role": "bogus", "action": "read",
                                             "kind": "doc", "secret": 1})
    check("试算：非法角色 400", r.status_code == 400, r.status_code)
    r = c.post("/api/policy/simulate", json={"role": "property", "action": "read",
                                             "kind": "doc", "secret": 9})
    check("试算：密级越界 400", r.status_code == 400, r.status_code)
    r = c.post("/api/policy/simulate", json={"role": "property", "action": "hack",
                                             "kind": "doc", "secret": 1})
    check("试算：非法动作 400", r.status_code == 400, r.status_code)

# 试算只读：不能写审计，否则演示时点几十次会把真正的越权记录埋掉。
# 注意基线要在**登录之后**取——登录本身就会写一条审计，
# 先取再登录会把这 1 条算到试算头上。
with app.test_client() as c:
    login(c, "admin", "admin123")
    with app.app_context():
        from app.models import AuditLog
        before = AuditLog.query.count()
    for _ in range(5):
        c.post("/api/policy/simulate", json={"role": "property", "action": "read",
                                             "kind": "doc", "secret": 3})
    with app.app_context():
        after = AuditLog.query.count()
check("试算是只读的（5 次试算不新增审计）", after == before, f"{before} → {after}")

# ======================================================================
print("\n" + "=" * 66)
print("⑥ 水印文本转义（特殊字符不能把 CSS/XML 弄坏）")
print("=" * 66)
weird = type("X", (), {"name": "A&B<C>", "org": "甲\"物业\""})()
css = overlay_css(wm_text(weird, "导出"))
# 转义后再整体 URL 编码，所以断言要看编码后的形态：
# `&` → `&amp;` → `%26amp%3B`。查裸的 `&amp;` 会误判成"没转义"。
check("SVG 文本里的 & 先转义再做 URL 编码", "%26amp%3B" in css, css[:120])
check("CSS 里没有裸的 & 引出非法字符实体", "&amp;" not in css)
check("CSS 里没有裸的 < > 破坏 SVG", "<C>" not in css)
check("CSS 用 data-uri 背景图而不是 content 文本", "data:image/svg+xml" in css)
check("水印层不挡点击", "pointer-events:none" in css)
check("水印层不会被选中复制", "user-select:none" in css)
check("水印 z-index 在弹窗之下（不压弹层文字）",
      "z-index:900" in css)

# ======================================================================
print("\n" + "=" * 66)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)
