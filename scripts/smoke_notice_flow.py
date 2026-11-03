"""回归：收文 → 签收 → 办文 → 导出 → 发布 的整条链路

这是本轮唯一一套「纵向」测试：其他脚本都是横向查一个模块，
这里走的是社区工作者拿到上级文件后的完整动作序列，缺一环整个流程就断。
所以每条断言都在问同一个问题：**下一步还走得下去吗**。

用独立库 + 关掉后台扫描线程，不碰 data/community.db。
"""
import os
import pathlib
import sys
import zipfile

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_notice_flow.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"   # 指向死端口，强制走规则兜底
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                        # noqa: E402
from app.extensions import db                      # noqa: E402
from app.models import AuditLog, Notice            # noqa: E402

app = create_app("dev")

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
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


def audit_has(action):
    with app.app_context():
        return AuditLog.query.filter_by(action=action).count() > 0


print("=" * 66)
print("收文 · 办文 · 文档生成 全链路回归")
print("=" * 66)

street = app.test_client()
community = app.test_client()
admin = app.test_client()

print("\n[1] 登录：街道、社区、管理员")
check("街道登录", login(street, "jiedao", "123456").status_code == 302)
check("社区登录", login(community, "wang", "123456").status_code == 302)
check("管理员登录", login(admin, "admin", "admin123").status_code == 302)

# ---------------------------------------------------------------- 2 街道发文
print("\n[2] 街道在线发文（点对点投递给社区）")
DOC = """各社区、各有关单位：

为切实消除高层住宅消防安全隐患，经街道研究，决定于近期开展专项排查。现将有关事项通知如下：

一、排查范围
辖区内所有高层住宅小区，重点为 10 层以上建筑。

二、排查内容
- 消防通道、疏散楼梯是否被占用堵塞
- 电动自行车是否违规入户充电
- 室内消火栓、灭火器是否完好有效

三、工作要求
1. 各社区于 10 月 25 日前完成自查，形成问题清单。
2. 排查结果报街道综合办汇总。
"""
r = street.post("/api/notices/dispatch", json={
    "title": "关于开展高层住宅消防安全专项排查的通知",
    "issuer": "幸福里街道办事处",
    "doc_no": "幸街发〔2026〕18号",
    "issued_at": "2026-10-15",
    "deadline": "2026-10-25",
    "receiver_org": "幸福里社区",
    "body_md": DOC,
})
d = r.get_json()["data"]
pid = d["id"]
check("发文成功", r.status_code == 200 and d["title"], r.status_code)
check("类型为上级来文", d["kind"] == "superior", d["kind"])
check("标记为待签收", d["recv_status"] == "dispatched", d["recv_status"])
check("收文单位为幸福里社区", d["receiver_org"] == "幸福里社区", d["receiver_org"])
check("发文后未直接发布", d["status"] == "pending", d["status"])
check("时限写入", d["deadline"] == "2026-10-25", d["deadline"])
check("投递写入审计", audit_has("dispatch"))

print("\n[3] 驳回空内容")
check("缺标题被拒", street.post("/api/notices/dispatch",
                              json={"issuer": "x", "body_md": "y"}).get_json()["code"] != 0)
check("缺正文被拒", street.post("/api/notices/dispatch",
                              json={"title": "x", "issuer": "y"}).get_json()["code"] != 0)

# ---------------------------------------------------------------- 4 收文待办
print("\n[4] 社区收到待办")
r = community.get("/api/notices?recv=dispatched")
items = r.get_json()["data"]["items"]
check("待签收列表里有这份文件", any(i["id"] == pid for i in items),
      [i["title"] for i in items])
check("待办计数 ≥ 1", r.get_json()["data"]["counts"]["todo"] >= 1)

# 物业没有 notice.receive，不能签收；但有 notice.view，能看
prop = app.test_client()
login(prop, "wuye", "123456")
r = prop.post(f"/api/notices/{pid}/receive")
check("物业无签收权限被拒", r.status_code in (403,), r.status_code)
check("物业越权写入 deny 审计", audit_has("deny"))

# ---------------------------------------------------------------- 5 签收
print("\n[5] 社区签收")
r = community.post(f"/api/notices/{pid}/receive")
d = r.get_json()["data"]
check("签收成功", r.get_json()["code"] == 0)
check("状态变为已签收", d["row"]["recv_status"] == "received", d["row"]["recv_status"])
check("记录签收人", d["row"]["received_by"] == "王社工", d["row"]["received_by"])
check("记录签收时间", bool(d["row"]["received_at"]))
check("签收写入审计", audit_has("receive"))

r = community.post(f"/api/notices/{pid}/receive")
check("重复签收幂等不报错", r.get_json()["code"] == 0)

# 指定了收文单位的文件，别家社区不能签收
r = street.post("/api/notices/dispatch", json={
    "title": "关于另一社区的专项任务", "issuer": "幸福里街道办事处",
    "receiver_org": "和平社区", "body_md": "内容略。",
})
other_id = r.get_json()["data"]["id"]
r = community.post(f"/api/notices/{other_id}/receive")
check("非指定收文单位签收被拒 403", r.status_code == 403, r.status_code)

# ---------------------------------------------------------------- 5.5 办结
print("\n[5.5] 登记办理结果（办结）")
from app import weekly                            # noqa: E402
s0, e0 = weekly.parse_period(weekly.current_period())
with app.app_context():
    wk0 = weekly.collect(s0, e0)
# 这件来文的时限是 10-25（已过），没办结之前必须落进"欠办"——
# 这正是原来那个 bug 的表现：所有到期来文一律算欠办，因为 handled_at 永远空
check("办结前：到期未办算「欠办」", pid in wk0["items"]["doc_late"], wk0["items"]["doc_late"])
check("办结前：本周已办结为 0", wk0["counts"]["doc_in_done"] == 0, wk0["counts"]["doc_in_done"])

# 未签收不能办结：另投一份不签收
r = street.post("/api/notices/dispatch", json={
    "title": "关于报送老旧小区改造需求的通知", "issuer": "幸福里街道办事处",
    "body_md": "请各社区报送需求清单。", "deadline": "2026-12-31",
})
unrecv = r.get_json()["data"]["id"]
r = community.post(f"/api/notices/{unrecv}/handle", json={"result": "没签收就想办结"})
check("未签收不能办结 409", r.status_code == 409, r.status_code)

r = prop.post(f"/api/notices/{pid}/handle", json={"result": "物业越权办结试试"})
check("物业无办结权限 403", r.status_code == 403, r.status_code)

r = community.post(f"/api/notices/{pid}/handle", json={"result": "办了"})
check("办理结果太短被拒 400", r.status_code == 400, r.status_code)

r = community.post(f"/api/notices/{pid}/handle",
                   json={"result": "已联系物业当日疏通消防通道，回访居民确认畅通"})
d = (r.get_json().get("data") or {}).get("row") or {}
check("办结成功", r.get_json()["code"] == 0)
check("记承办人", d.get("handler") == "王社工", d.get("handler"))
check("记办结时间", bool(d.get("handled_at")), d.get("handled_at"))
check("记办理结果", "回访" in (d.get("handle_result") or ""), d.get("handle_result"))
check("办结写入审计", audit_has("handle"))

first_at = d.get("handled_at")
r = community.post(f"/api/notices/{pid}/handle", json={"result": "补充：现场照片已归档"})
d2 = (r.get_json().get("data") or {}).get("row") or {}
check("补正结果不改首次办结时间", d2.get("handled_at") == first_at,
      "%r → %r" % (first_at, d2.get("handled_at")))
check("补正后结果已更新", "现场照片" in (d2.get("handle_result") or ""))

# 周报口径：办结三件套写完，KPI 必须跟着动（这正是原来恒 0 的那一格）
with app.app_context():
    wk = weekly.collect(s0, e0)
check("周报「已办结来文」计入", wk["counts"]["doc_in_done"] >= 1, wk["counts"]["doc_in_done"])
check("周报「欠办」不再把它算进去", pid not in wk["items"]["doc_late"], wk["items"]["doc_late"])

# ---------------------------------------------------------------- 6 办文
print("\n[6] 生成社区落地件（模型不可用 → 规则兜底）")
r = community.get(f"/api/notices/{pid}/draft")
d = r.get_json()["data"]
check("草稿生成成功", r.get_json()["code"] == 0)
check("走的是规则兜底", d["source"] == "rule", d["source"])
check("标题是落实类通知", "落实" in d["title"], d["title"])
check("正文写明依据文号", "幸街发" in d["body_md"], d["body_md"][:80])
check("正文含上级层级要点", "排查范围" in d["body_md"] or "排查内容" in d["body_md"])
check("正文带完成时限", "10月25日" in d["body_md"] or "2026年10月25日" in d["body_md"],
      d["body_md"][-200:])

r = community.post(f"/api/notices/{pid}/localize", json={
    "title": d["title"], "body_md": d["body_md"], "publish": False,
})
lid = r.get_json()["data"]["id"]
dd = r.get_json()["data"]
check("落地件创建成功", r.get_json()["code"] == 0 and lid)
check("落地件 kind=local", dd["kind"] == "local", dd["kind"])
check("落地件指回上级原文", dd["parent_id"] == pid, dd["parent_id"])
check("落地件发文单位是社区", dd["issuer"] == "幸福里社区", dd["issuer"])
check("未勾发布时保持待确认", dd["status"] == "pending", dd["status"])
check("办文写入审计", audit_has("localize"))

print("\n[7] 落地件与原件的依据关系")
r = community.get(f"/api/notices/{lid}")
check("落地件详情可读", r.get_json()["code"] == 0)
r = community.get(f"/notices/{pid}")
html = r.get_data(as_text=True)
check("原文页显示「已生成 1 份」", "已生成 1 份" in html)
r = community.get(f"/notices/{lid}")
html = r.get_data(as_text=True)
check("落地件页显示依据原文", "本件依据上级文件" in html)
check("落地件页有上级标题链接", "关于开展高层住宅消防安全专项排查的通知" in html)

# ---------------------------------------------------------------- 8 导出
print("\n[8] 导出三种格式")
r = community.get(f"/notices/{pid}/export/docx")
raw = r.data
check("docx 返回 200", r.status_code == 200, r.status_code)
check("docx 是 zip 包（OOXML）", raw[:2] == b"PK", raw[:8])
check("docx MIME 正确",
      "wordprocessingml" in (r.headers.get("Content-Type") or ""),
      r.headers.get("Content-Type"))
names = zipfile.ZipFile(__import__("io").BytesIO(raw)).namelist()
check("docx 内含 document.xml", "word/document.xml" in names, names[:5])
check("docx 文件名带标题",
      "UTF-8''" in (r.headers.get("Content-Disposition") or "")
      and "消防安全" in __import__("urllib.parse", fromlist=["unquote"]).unquote(
          r.headers.get("Content-Disposition") or ""),
      r.headers.get("Content-Disposition"))

# 正文是否真的写进去了
import io
import re
with zipfile.ZipFile(io.BytesIO(raw)) as z:
    xml = z.read("word/document.xml").decode("utf-8")
texts = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml))
check("docx 含版头发文机关", "幸福里街道办事处" in texts)
check("docx 含文号", "幸街发" in texts)
check("docx 含正文首句", "为切实消除高层住宅消防安全隐患" in texts)
check("docx 含表格内容", "10月25日" in texts or "10 月 25 日" in texts)
check("docx 里落款只出现一次（未重复）",
      texts.count("2026年10月15日") <= 1, texts.count("2026年10月15日"))

r = community.get(f"/notices/{pid}/export/md")
md = r.data.decode("utf-8")
check("md 返回 200", r.status_code == 200)
check("md 有元信息头", md.startswith("---") and "title:" in md)
check("md 文号可回灌", "doc_no: 幸街发〔2026〕18号" in md, md[:200])
check("md 层级可回灌", "level:" in md)

r = community.get(f"/notices/{pid}/export/txt")
txt = r.data.decode("utf-8")
check("txt 返回 200", r.status_code == 200)
check("txt 含发文单位行", "发文单位：幸福里街道办事处" in txt)
check("txt 层级标题独立成行", "\n一、排查范围\n" in txt, txt[txt.find("一、"):][:30])

r = community.get(f"/notices/{pid}/export/pdf")
check("不支持的格式被拒 400", r.status_code == 400, r.status_code)

r = prop.get(f"/notices/{pid}/export/docx")
# 这是**未发布**的上级来文（status=pending）。早先这里断言 200，
# 等于承认"草稿也能被物业导走"，属于被修掉的越权：
# 物业只该拿到已发布、且面向自己的件。发布之后能不能导，见第 9 节。
check("未发布的上级来文物业导不走", r.status_code == 404, r.status_code)

# ---------------------------------------------------------------- 9 发布
print("\n[9] 落地件发布到居民端")
r = community.post(f"/api/notices/{lid}/publish")
check("发布成功", r.get_json()["code"] == 0, r.get_json())
with app.app_context():
    n = db.session.get(Notice, lid)
    check("库中状态为 published", n.status == "published", n.status)
    check("记录了发布时间", n.published_at is not None)

r = prop.get(f"/notices/{lid}/export/docx")
check("物业能导出已发布且面向全体的件（能看到就该能导出）",
      r.status_code == 200, r.status_code)

r = app.test_client().get("/portal/notices")
html = r.get_data(as_text=True)
check("居民端能看到落地件", "关于落实" in html)
check("居民端看不到另一社区的待办件", "关于另一社区的专项任务" not in html)

r = app.test_client().get(f"/portal/notice/{lid}")
check("居民端打开落地件详情", r.status_code == 200, r.status_code)
r = app.test_client().get(f"/portal/notice/{lid}?mode=print")
check("打印版可访问", r.status_code == 200)
r = app.test_client().get(f"/portal/notice/{lid}?mode=official")
check("发布版可访问", r.status_code == 200)

# ---------------------------------------------------------------- 10 目录投递
print("\n[10] 目录批量投递也要进收文流程")
import tempfile                                    # noqa: E402

from app.notice.ingest import scan_and_import      # noqa: E402

# 换成临时目录：不扫 data/notices 的样例，免得断言撞到别的文件上
tmp = pathlib.Path(tempfile.mkdtemp())
app.config["NOTICE_DIR"] = str(tmp)
(tmp / "2026-10-20-关于开展冬季供暖排查的通知.md").write_text(
    "---\ntitle: 关于开展冬季供暖排查的通知\n"
    "issuer: 幸福里街道办事处\ndoc_no: 幸街发〔2026〕22号\n"
    "issued_at: 2026-10-20\n---\n各社区：\n\n请于 11 月 5 日前完成供暖排查。\n",
    encoding="utf-8")

with app.app_context():
    r = scan_and_import(actor="张科长")
    check("扫描导入成功", len(r["imported"]) == 1, r)
    n = Notice.query.filter_by(title="关于开展冬季供暖排查的通知").first()
    check("文件已入库", n is not None)
    if n:
        check("目录投递的也标为待签收", n.recv_status == "dispatched", n.recv_status)
        check("目录投递的也是上级来文", n.kind == "superior", n.kind)
        check("目录投递的收文单位为全体", n.receiver_org == "", n.receiver_org)
for f in tmp.glob("*"):
    try:
        f.unlink()
    except OSError:
        pass

# ---------------------------------------------------------------- 11 总结
print("\n" + "=" * 66)
print(f"通过 {PASS} 项 / 失败 {FAIL} 项")
if FAILED:
    print("失败项：")
    for x in FAILED:
        print("  -", x)
print("=" * 66)

for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass          # Windows 上连接池偶尔还占着句柄，删不掉就算了，下次启动会覆盖

sys.exit(1 if FAIL else 0)
