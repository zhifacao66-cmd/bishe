"""RAG 知识库冒烟测试

覆盖：检索质量 / 问答链路 / 文档增删 / 权限隔离 / 与 FDE 链路联动
运行：python scripts/smoke_kb.py
"""
import os
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# 独立库：这套测试会写数据（首录、接单、审计），
# 以前直接用 data/community.db，跑一遍就把演示库的工单状态和审计记录改了。
DB = BASE / "data" / "smoke_kb.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app  # noqa: E402
from app.models import KbDoc  # noqa: E402

app = create_app("dev")
PASS, FAIL = 0, 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {extra}")


def login(c, u):
    return c.post("/login", data={"username": u, "password": "123456"})


print("=" * 66)
print("① 索引与检索质量")
print("=" * 66)
c = app.test_client()
login(c, "wang")
st = c.get("/api/kb/stats").get_json()["data"]
check("文档数 = 10 篇", st["doc_count"] == 10, st["doc_count"])
check("切片数 > 40", st["chunk_count"] > 40, st["chunk_count"])
check("默认 BM25 检索（无额外显存）", st["dense"] is False)

cases = [
    ("高龄老人津贴怎么申请，需要哪些材料", "高龄老人津贴"),
    ("群租房怎么认定，发现后怎么处置", "群租房"),
    ("电梯坏了物业多久必须到场", "物业报修"),
    ("独居老人多久探访一次", "独居"),
    ("低保户收入变化要多久复核", "最低生活保障"),
]
for q, expect in cases:
    hits = c.get("/api/kb/search", query_string={"q": q}).get_json()["data"]["hits"]
    ok = bool(hits) and expect in hits[0]["title"]
    check(f"Top1 命中「{expect}」：{q}", ok, hits[0]["title"] if hits else "无结果")

hits = c.get("/api/kb/search", query_string={"q": "群租房形成条件"}).get_json()["data"]["hits"]
check("近义改写（认定→形成条件）仍能命中群租条款",
      bool(hits) and "群租" in (hits[0]["title"] + hits[0]["snippet"]),
      hits[0]["title"] if hits else "无结果")

print("=" * 66)
print("② 基于资料的问答（本地模型）")
print("=" * 66)
r = c.post("/api/kb/ask", json={"question": "高龄老人津贴多大年龄能领？需要哪些材料？"})
d = r.get_json()["data"]
check("问答返回非空答案", len(d["answer"]) > 30, len(d["answer"]))
check("答案标注了出处", "《" in d["answer"])
check("返回参考片段 ≥2", len(d["hits"]) >= 2, len(d["hits"]))
check("使用本地模型生成", d["llm_used"] is True)
print("    答案摘录：", d["answer"][:110].replace("\n", " "))

r = c.post("/api/kb/ask", json={"question": "社区能不能帮忙代管老人的银行卡和现金？"})
d2 = r.get_json()["data"]
check("负面条款也能答（代管银行卡）", "不得" in d2["answer"] or "不能" in d2["answer"],
      d2["answer"][:80])

print("=" * 66)
print("③ 文档新增 / 删除后索引即时生效")
print("=" * 66)
title = "残疾人两项补贴申领测试文档"
body = ("## 适用对象\n持有第二代残疾人证、残疾等级为一至二级的重度残疾人，"
        "可申请重度残疾人护理补贴。\n\n## 所需材料\n身份证、残疾人证、银行卡复印件各一份。"
        "\n\n## 时限\n社区受理后 5 个工作日内完成初审。")
r = c.post("/api/kb/docs", json={"title": title, "content": body, "category": "办事指南"})
check("新增文档成功", r.get_json()["code"] == 0)
did = r.get_json()["data"]["id"]
hits = c.get("/api/kb/search", query_string={"q": "残疾人护理补贴要什么材料"}).get_json()["data"]["hits"]
check("新增后可被检索到", bool(hits) and "残疾" in hits[0]["title"], hits[0]["title"] if hits else "无")
r = c.delete(f"/api/kb/docs/{did}")
check("删除成功", r.get_json()["code"] == 0)
hits = c.get("/api/kb/search", query_string={"q": "残疾人护理补贴要什么材料"}).get_json()["data"]["hits"]
check("删除后不再命中该文档",
      all("残疾人两项补贴申领测试文档" != h["title"] for h in hits))

print("=" * 66)
print("④ 权限隔离")
print("=" * 66)
cp = app.test_client()
login(cp, "wuye")
check("物业访问 /kb 403", cp.get("/kb").status_code == 403)
check("物业调 /api/kb/docs 403", cp.get("/api/kb/docs").status_code == 403)
check("物业调 /api/kb/ask 403", cp.post("/api/kb/ask", json={"question": "x"}).status_code == 403)

cs = app.test_client()
login(cs, "jiedao")
check("街道可访问 /kb 200", cs.get("/kb").status_code == 200)
check("街道可检索", cs.get("/api/kb/search", query_string={"q": "高龄津贴"}).get_json()["code"] == 0)
r = cs.post("/api/kb/docs", json={"title": "街道越权测试", "content": "x" * 50})
check("街道新增文档 403（只读）", r.status_code == 403)

print("=" * 66)
print("⑤ 与 FDE 主链路联动（咨询类事项自动查政策）")
print("=" * 66)
c2 = app.test_client()
login(c2, "wang")
r = c2.post("/api/fde", json={"raw_text": "居民来问高龄老人津贴怎么申请、要准备哪些材料"})
d = r.get_json()["data"]
zx = [it for it in d["items"] if it["type"] == "咨询"]
check("识别出咨询类业务项", len(zx) >= 1, [i["type"] for i in d["items"]])
if zx:
    hits = zx[0].get("kb_hits") or []
    check("咨询项附带政策依据", len(hits) >= 1, hits)
    if hits:
        check("依据指向高龄津贴文件", "高龄" in hits[0]["title"], hits[0]["title"])
        print("    命中：", "、".join(h["title"] for h in hits))

print("=" * 66)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)
