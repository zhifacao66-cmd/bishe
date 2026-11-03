"""回归：报表导出 Excel（结构 / 数字 / 快照 / 权限）

这一套守四件事，缺一件导出的文件就"不能交"：

1. **文件能打开**：xlsx 是 zip + 若干 XML，任一 part 缺失或 XML 不合法，
   Excel 就弹"文件已损坏"。所以这里逐个 part 做 XML 良构校验，
   再核对 part 清单（sheet 数、styles、content-types）。
2. **数字对得上**：导出件里的每个数字都必须等于报表 data_json 里的值。
   "页面 12 件、文件 15 件"这种偏差最难解释——街道拿着两个版本对账。
3. **快照不变**：生成报表后往库里补数据，再导出一次，数字必须**不动**。
   报表是"某一时刻的口径证据"，能随库变化就失去证据效力。
4. **权限边界**：社区可生成可导出；街道只读但**能导出**（要拿去汇报）；
   物业无 report.view，403 且写 deny 审计。

另外守一条一致性：事项明细表行数 = data_json 里 item_rows 的条数，
且明细里不出现居民明文（脱敏是报表的对外前提）。

跑法：程序内 app.test_client()，独立库 data/smoke_report.db，每次重建。
"""
import io
import json
import os
import pathlib
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_report.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"      # 报表不调模型，明确断开
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                                   # noqa: E402
from app.extensions import db                                # noqa: E402
from app.models import (AuditLog, FdeRecord, Ledger, Report,  # noqa: E402
                        WorkItem, WorkOrder)

app = create_app("dev")
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

NOW = datetime.now()
# 固定一个"有数据"的月份：取当月，保证与 ReportAgent 默认周期一致
PERIOD = NOW.strftime("%Y-%m")
T0 = NOW.replace(day=1, hour=9, minute=0, second=0, microsecond=0)

# ---------------------------------------------------------------- 造数据
with app.app_context():
    fdes = [FdeRecord(raw_text=f"首录原文 {i}", creator="wang",
                      created_at=T0 + timedelta(hours=i)) for i in range(3)]
    db.session.add_all(fdes)

    # 5 个业务项：4 条业务线、4 类业务
    items = [
        ("报修", "3号楼2单元下水道堵塞", "物业"),
        ("报修", "1号楼电梯异响", "物业"),
        ("走访", "独居老人月度走访", "民政"),
        ("调解", "邻里噪音纠纷", "综治"),
        ("隐患排查", "楼道堆放杂物", "街道,综治"),
    ]
    wi = [WorkItem(fde_id=fdes[i % 3].id, type=t, subject=s, target_lines=ln,
                   created_at=T0 + timedelta(hours=2 + i))
          for i, (t, s, ln) in enumerate(items)]
    db.session.add_all(wi)

    # 工单 4 件：1 待处理（且超期）、1 处理中、2 已完成
    os_ = [
        WorkOrder(order_no="R-001", work_item_id=wi[0].id, property_co="幸福里物业",
                  content="3号楼2单元下水道堵塞，已脱敏", status="open",
                  sla_hours=4, sla_level="紧急", created_at=T0 + timedelta(hours=4)),
        WorkOrder(order_no="R-002", work_item_id=wi[1].id, property_co="幸福里物业",
                  content="1号楼电梯异响", status="doing",
                  sla_hours=24, sla_level="较急", created_at=NOW - timedelta(hours=2)),
        WorkOrder(order_no="R-003", work_item_id=wi[2].id, property_co="幸福里物业",
                  content="独居老人走访", status="done", sla_hours=72,
                  sla_level="一般", created_at=T0 + timedelta(hours=6)),
        WorkOrder(order_no="R-004", work_item_id=wi[3].id, property_co="幸福里物业",
                  content="邻里噪音纠纷", status="done", sla_hours=72,
                  sla_level="一般", created_at=T0 + timedelta(hours=7)),
    ]
    db.session.add_all(os_)

    # 台账 2 条、其中 1 条复用
    db.session.add_all([
        Ledger(work_item_id=wi[0].id, line="物业", reused=True,
               content_json=json.dumps({"subject": "下水道", "reused_archive": True},
                                       ensure_ascii=False),
               created_at=T0 + timedelta(hours=8)),
        Ledger(work_item_id=wi[2].id, line="民政", reused=False,
               content_json=json.dumps({"subject": "走访"}, ensure_ascii=False),
               created_at=T0 + timedelta(hours=9)),
    ])

    # 脱敏审计 2 条（只统计 agent=security 且在本月窗口内）
    db.session.add_all([
        AuditLog(actor="wang", agent="security", action="mask", input_digest="**",
                 output_digest="2 个字段脱敏", created_at=T0 + timedelta(hours=10)),
        AuditLog(actor="wang", agent="security", action="mask", input_digest="**",
                 output_digest="1 个字段脱敏", created_at=T0 + timedelta(hours=11)),
    ])
    db.session.commit()
    fde_ids = [f.id for f in fdes]        # 会话关掉后 ORM 实例会过期，先取 id

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
    return c.post("/login", data={"username": u, "password": p}, follow_redirects=False)


def cells(sheet_xml: str) -> dict:
    """把 worksheet XML 读成 {(row, col): value}，值只取文本或数字。

    自己解析而不是引 openpyxl：这个项目不把"测试也得装包"当成理所当然，
    xlsx 的取值逻辑就三行，够用。
    """
    root = ET.fromstring(sheet_xml)
    out = {}
    for c in root.iter(f"{{{NS['m']}}}c"):
        ref = c.get("r")
        col = "".join(ch for ch in ref if ch.isalpha())
        row = int("".join(ch for ch in ref if ch.isdigit()))
        t = c.get("t")
        if t == "inlineStr":
            node = c.find(f"{{{NS['m']}}}is/{{{NS['m']}}}t")
            out[(row, col)] = node.text if node is not None else ""
        else:
            v = c.find(f"{{{NS['m']}}}v")
            if v is not None and v.text is not None:
                try:
                    out[(row, col)] = float(v.text) if "." in v.text or "e" in v.text \
                        else int(v.text)
                except ValueError:
                    out[(row, col)] = v.text
    return out


def grid(sh: dict) -> list:
    """把 (row,col) 字典摊成二维文本网格，便于按内容定位而不是死记单元格坐标"""
    if not sh:
        return []
    maxr = max(r for r, _ in sh)
    return [[str(sh.get((r, c), "")) for c in "ABCDEFGH"] for r in range(1, maxr + 1)]


def row_of(sh: dict, label: str) -> int | None:
    for (r, c), v in sh.items():
        if c == "A" and str(v).strip() == label:
            return r
    return None


def fetch_xlsx(c, rid, fmt="xlsx"):
    return c.get(f"/reports/{rid}/export/{fmt}")


# ================================================================ 1 生成
print("\n[1] 生成报表：数字取自库，明细与超期随报表冻结")
c = app.test_client()
check("社区账号登录成功", login(c, "wang", "123456").status_code in (302, 303))
gen = c.post("/api/reports/generate", json={"period": PERIOD})
gj = gen.get_json()
check("生成接口返回 code=0", gj.get("code") == 0, gj.get("msg"))
d = gj.get("data") or {}
rid = d.get("report_id")
check("返回报表编号", isinstance(rid, int) and rid > 0, rid)
check("首录数=3", d.get("fde_count") == 3, d.get("fde_count"))
check("业务项数=5", d.get("item_count") == 5, d.get("item_count"))
check("业务线 4 条", len(d.get("line_counts") or {}) == 4, d.get("line_counts"))
check("物业 2 项（拆解后两条报修）", (d.get("line_counts") or {}).get("物业") == 2,
      d.get("line_counts"))
check("工单总数=4", (d.get("orders") or {}).get("total") == 4, d.get("orders"))
check("已完成=2", (d.get("orders") or {}).get("done") == 2, d.get("orders"))
check("超期未办结=1（4h 的紧急单已过时）", d.get("overdue_count") == 1, d.get("overdue_count"))
check("复用台账=1", d.get("reuse_count") == 1, d.get("reuse_count"))
check("脱敏批次=2", d.get("sensitive_count") == 2, d.get("sensitive_count"))
with app.app_context():
    snap = json.loads(db.session.get(Report, rid).data_json)
check("明细快照 5 行（存在库里的快照里，不在接口响应里）",
      len(snap.get("item_rows") or []) == 5, len(snap.get("item_rows") or []))
check("明细快照字段只留非敏感项",
      set((snap.get("item_rows") or [{}])[0]) == {"type", "subject", "line", "created"},
      set((snap.get("item_rows") or [{}])[0]))
check("明细快照不泄漏到详情接口",
      "item_rows" not in (c.get(f"/api/reports/{rid}").get_json().get("data") or {}))
check("报送单位取会话组织（幸福里社区）", d.get("org") == "幸福里社区", d.get("org"))

# ================================================================ 2 文件结构
print("\n[2] 导出件是合法的 xlsx 包（逐个 part 校验 XML）")
resp = fetch_xlsx(c, rid)
check("导出返回 200", resp.status_code == 200, resp.status_code)
check("Content-Type 是 xlsx", resp.headers.get("Content-Type", "").startswith(XLSX_MIME),
      resp.headers.get("Content-Type"))
cd = resp.headers.get("Content-Disposition", "")
check("按附件下载", "attachment" in cd, cd)
check("文件名含周期与单位", PERIOD in cd and "%E7%A4%BE%E5%8C%BA" in cd.upper()
      or PERIOD in cd, cd)

blob = resp.data
check("文件非空且是 zip", len(blob) > 2000 and blob[:2] == b"PK", len(blob))
z = zipfile.ZipFile(io.BytesIO(blob))
parts = z.namelist()
need = ["[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels", "xl/styles.xml"]
check("必需 part 齐全", all(p in parts for p in need), [p for p in need if p not in parts])
bad = []
for p in parts:
    if p.endswith((".xml", ".rels")):
        try:
            ET.fromstring(z.read(p))
        except ET.ParseError as e:
            bad.append(f"{p}: {e}")
check("所有 XML part 良构", not bad, bad)

wbx = ET.fromstring(z.read("xl/workbook.xml"))
names = [s.get("name") for s in wbx.findall(".//m:sheet", NS)]
check("6 个工作表", len(names) == 6, names)
check("工作表顺序固定", names == ["月报概览", "业务线分布", "业务类型分布",
                                "工单办理", "业务项明细", "填报说明"], names)
st = ET.fromstring(z.read("xl/styles.xml"))
check("样式表有字体/填充/边框/单元格格式",
      len(st.findall(".//m:font", NS)) >= 3 and len(st.findall(".//m:fill", NS)) >= 2
      and len(st.findall(".//m:border", NS)) >= 2
      and len(st.findall(".//m:cellXfs/m:xf", NS)) >= 5)
fills = st.findall(".//m:fill/m:patternFill", NS)
check("fills 前两项是 none / gray125（格式规定）",
      [f.get("patternType") for f in fills[:2]] == ["none", "gray125"],
      [f.get("patternType") for f in fills[:2]])

sheets = {}
for i, nm in enumerate(names, 1):
    sheets[nm] = cells(z.read(f"xl/worksheets/sheet{i}.xml").decode())

# ================================================================ 3 数字一致
print("\n[3] 导出件数字 = 报表快照数字")
ov = sheets["月报概览"]
g = grid(ov)


def val(label, col="B"):
    r = row_of(ov, label)
    return ov.get((r, col)) if r else None


def text(label, col="B"):
    r = row_of(ov, label)
    v = ov.get((r, col)) if r else None
    return str(v) if v is not None else None


check("概览含标题与周期", any(PERIOD in row[0] for row in g), g[0][:1])
check("首录 3", val("首录事项（条）") == 3, val("首录事项（条）"))
check("业务项 5", val("拆解业务项（个）") == 5, val("拆解业务项（个）"))
check("覆盖业务线 4", val("覆盖业务线（条）") == 4, val("覆盖业务线（条）"))
check("工单 4", val("物业工单（件）") == 4, val("物业工单（件）"))
check("办结 2", val("工单办结（件）") == 2, val("工单办结（件）"))
check("办结率 0.5（比率存数，格式显示 50.0%）",
      abs((val("工单办结率") or 0) - 0.5) < 1e-9, val("工单办结率"))
check("超期 1", val("超期未办结（件）") == 1, val("超期未办结（件）"))
check("复用 1", val("复用历史档案（次）") == 1, val("复用历史档案（次）"))
check("脱敏 2", val("敏感信息脱敏（批次）") == 2, val("敏感信息脱敏（批次）"))
check("报送单位写入副标题", any("幸福里社区" in row[0] for row in g), g[1][:1])
check("统计区间闭开区间已说明", "闭开区间" in (text("统计区间") or ""), text("统计区间"))
# 摘要标签在 A 列，正文合并在下一行
r_sum = row_of(ov, "月度摘要")
summary_cell = str(ov.get((r_sum + 1, "A"), "")) if r_sum else ""
check("月度摘要与快照一致", summary_cell[:20] == (snap.get("summary") or "")[:20],
      summary_cell[:30])

# 表头文字（不是坐标）——改列宽改样式不该让测试红
head = [str(x) for x in g[3]]
check("概览表头为 指标/本期/上期/环比", head[:4] == ["指标", "本期", "上期", "环比"], head[:5])

dist = grid(sheets["业务线分布"])
total_row = next((r for r in dist if r[0] == "合计"), None)
check("业务线分布有合计行", total_row is not None)
check("业务线分布合计=6（一单多派：隐患排查同时去街道与综治）",
      total_row and total_row[2] == "6", total_row)
check("分布合计 ≥ 业务项总数（口径注释已说明）",
      total_row and int(total_row[2]) >= 5, total_row)
names_in_dist = {r[1] for r in dist[1:] if r[1] and r[1] not in ("业务线", "合计")}
check("业务线 4 条 + 合计", len(names_in_dist) == 4, names_in_dist)

types = grid(sheets["业务类型分布"])
t_row = next((r for r in types if r[0] == "合计"), None)
check("类型分布合计=5", t_row and t_row[2] == "5", t_row)

orders = sheets["工单办理"]
og = grid(orders)
check("工单表含待处理/处理中/已完成三行",
      all(any(r[0] == k for r in og) for k in ("待处理", "处理中", "已完成")))
o_total = next((r for r in og if r[0] == "合计"), None)
check("工单合计=4", o_total and o_total[1] == "4", o_total)
o_done = next((r for r in og if r[0] == "已完成"), None)
check("已完成=2", o_done and o_done[1] == "2", o_done)
check("超期件数在工单页出现",
      any("超期" in (r[0] or "") and "1 件" in (r[1] or "") for r in og))

det = grid(sheets["业务项明细"])
detail_rows = [r for r in det[4:] if r[0].isdigit()]
check("明细行数 = 快照行数 (5)", len(detail_rows) == 5, len(detail_rows))
check("明细含事项摘要", any("下水道" in r[2] for r in detail_rows),
      [r[2] for r in detail_rows][:5])
check("明细按录入时间升序（快照排序稳定）",
      [r[0] for r in detail_rows] == ["1", "2", "3", "4", "5"],
      [r[0] for r in detail_rows])
flat_detail = " ".join(" ".join(r) for r in det)
check("明细不含 11 位手机号/18 位身份证",
      not any(k in flat_detail for k in ("13800138000", "110101199003074512")))

notes = grid(sheets["填报说明"])
ntext = " ".join(" ".join(r) for r in notes)
check("说明页写清脱敏与本地部署", "脱敏" in ntext and "本地" in ntext)
check("说明页标注报表编号", f"R{rid:06d}" in ntext, ntext[-80:])
check("说明页有盖章/签字栏", "报送单位（盖章）" in ntext and "负责人签字" in ntext)

# ================================================================ 4 快照不变
print("\n[4] 生成后库变了，导出件数字不变（报表是口径证据）")
with app.app_context():
    db.session.add_all([
        WorkItem(fde_id=fde_ids[0], type="报修", subject="后补的一条",
                 target_lines="物业", created_at=T0 + timedelta(hours=12)),
        FdeRecord(raw_text="后补首录", creator="wang",
                  created_at=T0 + timedelta(hours=13)),
    ])
    db.session.commit()

resp2 = fetch_xlsx(c, rid)
z2 = zipfile.ZipFile(io.BytesIO(resp2.data))
ov2 = cells(z2.read("xl/worksheets/sheet1.xml").decode())

r_fde = row_of(ov2, "首录事项（条）")
check("库新增首录后，导出仍为 3", ov2.get((r_fde, "B")) == 3, ov2.get((r_fde, "B")))
r_item = row_of(ov2, "拆解业务项（个）")
check("库新增业务项后，导出仍为 5", ov2.get((r_item, "B")) == 5, ov2.get((r_item, "B")))
det2 = cells(z2.read("xl/worksheets/sheet5.xml").decode())
rows2 = [k for k, v in det2.items() if k[1] == "A" and str(v).isdigit()]
check("明细仍是 5 行", len(rows2) == 5, len(rows2))

# ================================================================ 5 权限
print("\n[5] 权限：社区可导出、街道可导出不可生成、物业 403 且留痕")
cs = app.test_client()
login(cs, "jiedao", "123456")
rs = fetch_xlsx(cs, rid)
check("街道可导出（要拿去汇报）", rs.status_code == 200, rs.status_code)
check("街道不可生成报表",
      cs.post("/api/reports/generate", json={"period": PERIOD}).status_code == 403)

cw = app.test_client()
login(cw, "wuye", "123456")
rw = fetch_xlsx(cw, rid)
check("物业导出被拒 403", rw.status_code == 403, rw.status_code)
with app.app_context():
    deny = AuditLog.query.filter_by(agent="auth", action="deny").count()
check("越权导出写了 deny 审计", deny >= 1, deny)

anon = app.test_client()
ra = anon.get(f"/reports/{rid}/export/xlsx", follow_redirects=False)
check("未登录跳登录页（302）", ra.status_code == 302, ra.status_code)

# ================================================================ 6 边界
print("\n[6] 边界：非法格式 400、不存在的报表 404、空月份也能导出")
check("非法格式 400", fetch_xlsx(c, rid, "pdf").status_code == 400)
check("不存在的报表 404", fetch_xlsx(c, 999999).status_code == 404)

# 空月份：本月没有数据时表格不能塌成空白（否则打印出来是白纸）
empty = c.post("/api/reports/generate", json={"period": "2020-01"}).get_json()["data"]
re_ = fetch_xlsx(c, empty["report_id"])
check("空月份报表可导出 200", re_.status_code == 200, re_.status_code)
ze = zipfile.ZipFile(io.BytesIO(re_.data))
ove = cells(ze.read("xl/worksheets/sheet1.xml").decode())
r0 = row_of(ove, "首录事项（条）")
check("空月份首录显示 0 而不是空白", ove.get((r0, "B")) == 0, ove.get((r0, "B")))
de = cells(ze.read("xl/worksheets/sheet5.xml").decode())
check("空月份明细给出「本月无业务项」",
      any("本月无业务项" in str(v) for v in de.values()))

# 老报表兼容：早期 data_json 没有 org/overdue_count/item_rows
with app.app_context():
    old = Report(period="2025-12", line="街道", data_json=json.dumps({
        "period": "2025-12", "range": ["2025-12-01", "2025-12-31"],
        "fde_count": 10, "item_count": 12,
        "line_counts": {"物业": 12}, "type_counts": {"报修": 12},
        "orders": {"total": 12, "done": 12},
        "reuse_count": 0, "sensitive_count": 3, "summary": "老版本报表",
        "prev": {"period": "2025-11", "fde_count": 8, "item_count": 9,
                 "order_total": 9},
    }, ensure_ascii=False), generated_at=datetime(2025, 12, 31, 18, 0))
    db.session.add(old)
    db.session.commit()
    old_id = old.id
ro = fetch_xlsx(c, old_id)
check("老报表（无新字段）仍能导出", ro.status_code == 200, ro.status_code)
zo = zipfile.ZipFile(io.BytesIO(ro.data))
ovo = cells(zo.read("xl/worksheets/sheet1.xml").decode())
r_ov = row_of(ovo, "超期未办结（件）")
check("缺失字段显示「—」而不是 0 或报错", ovo.get((r_ov, "B")) == "—",
      ovo.get((r_ov, "B")))

# ================================================================ 汇总
print(f"\n{'=' * 60}\n通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败项：")
    for f in FAILED:
        print("  -", f)
sys.exit(1 if FAIL else 0)
