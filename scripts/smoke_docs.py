"""发文台与工作周报回归（P0）

覆盖三类不变量，这三类出错都不会报错、只会悄悄错：
1. **状态机**：非法迁移必须拒绝。放过一次，流转记录就出现 done→done 那种假链路。
2. **密级与定密权**：社区不能给自己出的材料标"秘密/机密"；物业看不到敏感件。
3. **周报同源**：正文数字与取数快照必须出自同一次查询，且签发后冻结。

运行：python scripts/smoke_docs.py
"""
import os
import pathlib
import shutil
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_docs.db"
SRC = BASE / "data" / "community.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()
    s = pathlib.Path(str(SRC) + suf)
    if s.exists():
        shutil.copy2(s, str(DB) + suf)      # 连 -wal 一起拷，否则丢未 checkpoint 的事务

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["NOTICE_AUTO_IMPORT"] = "0"
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"   # 本例不测模型，避免误连

from app import create_app  # noqa: E402
from app.auth import generate_password_hash  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Notice, User, Weekly  # noqa: E402

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


def ensure_user(username, role, org, pwd):
    u = User.query.filter_by(username=username).first()
    if not u:
        u = User(username=username, name=username, role=role, org=org,
                 password_hash=generate_password_hash(pwd), status="active",
                 created_by="smoke")
        db.session.add(u)
        db.session.commit()
    return u


with app.app_context():
    # 演示库里可能已有本周周报（seed_docs 造的）。测试要自己从零生成，
    # 只清这一张表，不动业务数据 —— 周报之外的首录/工单还要拿来验同源。
    Weekly.query.filter_by(period="2026-W43").delete()
    db.session.commit()
    ensure_user("t_comm", "community", "幸福里社区", "comm123")
    ensure_user("t_prop", "property", "幸福里物业", "prop123")
    ensure_user("t_street", "street", "幸福里街道办事处", "street123")

admin = app.test_client()
login(admin, "admin", "admin123")
comm = app.test_client()
login(comm, "t_comm", "comm123")
prop = app.test_client()
login(prop, "t_prop", "prop123")
street = app.test_client()
login(street, "t_street", "street123")
anon = app.test_client()


def mk(client=None, **kw):
    body = {"title": "关于申请增补消防设施的请示", "doc_type": "请示",
            "secret_level": "内部", "urgency": "急件", "signer": "王书记",
            "to_org": "幸福里街道办事处", "body_md": "街道办事处：\n\n申请增补。\n\n妥否，请批示。"}
    body.update(kw)
    return (client or comm).post("/api/docs", json=body)


print("=" * 66)
print("① 拟稿校验")
print("=" * 66)
check("未登录拟稿 401", anon.post("/api/docs", json={"title": "x"}).status_code in (401, 403))
r = mk(title="")
check("空标题 400", r.status_code == 400, r.status_code)
r = mk(doc_type="便条")
check("非法文种 400", r.status_code == 400)
r = mk(secret_level="绝密")
check("非法密级 400", r.status_code == 400)
r = mk(secret_level="机密")
check("社区给自己标机密被拒（无定密权）", r.status_code == 400,
      r.get_json().get("msg", ""))
r = mk(doc_type="请示", to_org="")
check("上行文缺主送 400", r.status_code == 400)
r = mk(doc_type="函", to_org="")
check("函缺受文单位 400", r.status_code == 400)

r = mk()
check("正常拟稿 200", r.status_code == 200)
doc = r.get_json()["data"]
did = doc["id"]
check("初始状态为草稿", doc["doc_status"] == "draft", doc.get("doc_status"))
check("方向判为上行", doc["direction"] == "上行", doc.get("direction"))
check("发文 issuer 取自账号组织", "幸福里" in (doc["issuer"] or ""), doc.get("issuer"))

print("=" * 66)
print("② 状态机与要素校验")
print("=" * 66)
r = comm.post(f"/api/docs/{did}/send", json={})
check("草稿直接分发被拒 409", r.status_code == 409, r.status_code)
r = comm.post(f"/api/docs/{did}/archive", json={})
check("草稿直接归档被拒 409", r.status_code == 409)

r = comm.post(f"/api/docs/{did}/check", json={})
check("核稿 → checked", r.status_code == 200 and r.get_json()["data"]["doc_status"] == "checked")
r = comm.post(f"/api/docs/{did}/check", json={})
check("重复核稿 409", r.status_code == 409)

# 签发要求签发人：先清掉
r = comm.patch(f"/api/docs/{did}", json={"signer": ""})
check("草稿可改稿 200", r.status_code == 200)
r = comm.post(f"/api/docs/{did}/sign", json={})
check("无签发人拒绝签发 409", r.status_code == 409, r.get_json().get("msg", ""))

r = comm.patch(f"/api/docs/{did}", json={"signer": "王书记"})
r = comm.post(f"/api/docs/{did}/seal", json={})
check("未签发直接用印 409", r.status_code == 409)

r = comm.post(f"/api/docs/{did}/sign", json={})
check("签发 → signed", r.status_code == 200 and r.get_json()["data"]["doc_status"] == "signed")
r = comm.post(f"/api/docs/{did}/sign", json={})
check("重复签发 409", r.status_code == 409)

r = comm.post(f"/api/docs/{did}/seal", json={})
j = r.get_json()
check("用印 → sealed", r.status_code == 200 and j["data"]["doc_status"] == "sealed")
no1 = j["data"]["doc_no"]
check("用印自动取号", bool(no1), no1)
check("文号格式 社〔年〕N号", no1.startswith("社〔") and no1.endswith("号"), no1)

r = comm.post(f"/api/docs/{did}/send", json={})
check("分发 → sent", r.status_code == 200 and r.get_json()["data"]["doc_status"] == "sent")
r = comm.post(f"/api/docs/{did}/send", json={})
check("重复分发 409", r.status_code == 409)
r = comm.patch(f"/api/docs/{did}", json={"title": "改已分发的"})
check("已分发后不可改稿 409", r.status_code == 409)

r = comm.post(f"/api/docs/{did}/archive", json={})
check("归档 → archived", r.status_code == 200 and r.get_json()["data"]["doc_status"] == "archived")
r = comm.post(f"/api/docs/{did}/archive", json={})
check("重复归档 409", r.status_code == 409)

r = comm.get(f"/api/docs/{did}")
logs = [l["action"] for l in r.get_json()["data"]["logs"]]
check("流转记录完整五步", logs == ["核稿", "签发", "用印", "分发", "归档"], logs)
check("每一步都记了经办人", all(l["actor"] for l in r.get_json()["data"]["logs"]))

print("=" * 66)
print("③ 文号递增与退回")
print("=" * 66)
r = mk(title="第二份请示", doc_type="请示", to_org="幸福里街道办事处")
d2 = r.get_json()["data"]["id"]
for act in ("sign", "seal"):
    comm.post(f"/api/docs/{d2}/{act}", json={})
r = comm.get(f"/api/docs/{d2}")
no2 = r.get_json()["data"]["doc_no"]
check("第二份文号递增", no2 and no2 != no1, f"{no1} vs {no2}")

# 退回拟稿
r = mk(title="第三份请示", doc_type="请示", to_org="幸福里街道办事处")
d3 = r.get_json()["data"]["id"]
comm.post(f"/api/docs/{d3}/check", json={})
r = comm.post(f"/api/docs/{d3}/reject", json={})
check("退回 → 回到草稿", r.status_code == 200 and r.get_json()["data"]["doc_status"] == "draft")

print("=" * 66)
print("④ 密级与角色可见性")
print("=" * 66)
r = mk(title="含居民信息的内部件", doc_type="通知", secret_level="内部·敏感",
       to_org="幸福里物业", signer="王书记")
secret_id = r.get_json()["data"]["id"]
check("内部·敏感件可建", r.status_code == 200)
r = prop.get(f"/api/docs/{secret_id}")
check("物业看敏感件 403", r.status_code == 403, r.status_code)
r = prop.get("/api/docs?size=200")
ids = [x["id"] for x in r.get_json()["data"]["items"]]
check("物业列表不含敏感件", secret_id not in ids)

r = street.post("/api/docs", json={"title": "街道越权发文", "doc_type": "通知",
                                   "to_org": "x", "body_md": "x"})
check("街道不能发文 403", r.status_code == 403, r.status_code)
r = street.get("/api/docs")
check("街道可查看发文列表", r.status_code == 200)
r = prop.patch(f"/api/docs/{secret_id}", json={"title": "x"})
check("物业不能改稿 403", r.status_code == 403)

print("=" * 66)
print("⑤ 周报生成与冻结")
print("=" * 66)
r = street.post("/api/weekly/generate", json={"period": "2026-W43"})
check("街道不能生成周报 403", r.status_code == 403, r.status_code)

r = comm.post("/api/weekly/generate", json={"period": "2026-W43"})
j = r.get_json()
check("生成周报 200", r.status_code == 200 and j["code"] == 0)
w = j["data"]
wid = w["id"]
counts = w["data"]["counts"]
check("取数快照含全部口径", set(counts) >= {"fde", "items", "wo_new", "doc_in", "task_new", "ticket"},
      list(counts))
check("周期区间为自然周", w["range"].count("至") == 1, w.get("range"))
check("初始为草稿", w["status"] == "draft")

# 同源：counts 与 items 数量一致（未触及 200 上限时）
same = all(len(w["data"]["items"].get(k, [])) == counts.get(k, 0)
           for k in ("fde", "wo_new", "wo_done", "ledger", "task_new", "ticket"))
check("数字与明细同源", same,
      {k: (counts.get(k), len(w["data"]["items"].get(k, [])))
       for k in ("fde", "wo_new", "doc_in")})

r = comm.post("/api/weekly/generate", json={"period": "2026-W43"})
with app.app_context():
    n_rows = Weekly.query.filter_by(period="2026-W43").count()
check("重复生成不新增行（覆盖草稿）", n_rows == 1, n_rows)

r = comm.post(f"/api/weekly/{wid}/finalize")
check("占位未填拒绝签发 409", r.status_code == 409, r.get_json().get("msg", ""))

body = w["body_md"].replace("（请结合本周实际补充；可点「模型起草」生成初稿后修改）",
                            "本周超时工单集中在配件采购。")
body = body.replace("（请结合本周实际补充）", "1. 催办配件到货；2. 完成消防通道复查。")
r = comm.patch(f"/api/weekly/{wid}", json={"body_md": body})
check("补完可保存 200", r.status_code == 200)
r = comm.post(f"/api/weekly/{wid}/finalize")
check("签发 200 → final", r.status_code == 200 and r.get_json()["data"]["status"] == "final")
r = comm.patch(f"/api/weekly/{wid}", json={"body_md": "改已签发的"})
check("签发后不可改 409", r.status_code == 409)
r = comm.post("/api/weekly/generate", json={"period": "2026-W43"})
check("签发后重新生成被拒 409", r.status_code == 409)

r = comm.post("/api/weekly/generate", json={"period": "2026-W43", "force": True})
j = r.get_json()
check("强制生成出新修订版", r.status_code == 200 and j["data"]["revision"] == 2, j.get("msg", ""))
with app.app_context():
    n_rows2 = Weekly.query.filter_by(period="2026-W43").count()
check("旧版本仍在（不覆盖历史）", n_rows2 == 2, n_rows2)
r = comm.get("/api/weekly/999999")
check("不存在的周报 404", r.status_code == 404)

print("=" * 66)
print("⑥ 页面与权限")
print("=" * 66)
check("社区 /docs 200", comm.get("/docs").status_code == 200)
check("社区 /docs/<id> 200", comm.get(f"/docs/{did}").status_code == 200)
check("社区 /weekly 200", comm.get("/weekly").status_code == 200)
check("社区 /weekly/<id> 200", comm.get(f"/weekly/{wid}").status_code == 200)
check("未登录 /docs 302", anon.get("/docs").status_code == 302)
check("未登录 /weekly 302", anon.get("/weekly").status_code == 302)
check("物业打开发文页 200（只读可见）", prop.get("/docs").status_code == 200)
check("物业打开敏感件页面 403", prop.get(f"/docs/{secret_id}").status_code == 403)

with app.app_context():
    n = db.session.get(Notice, did)
    check("发文未误进公告栏", n.status == "pending", n.status)
    check("发文不投居民端（direction=out）", n.direction == "out")

# 公告口径必须把发文排掉：请示里有经费数额，推进居民端是泄密
j = comm.get("/api/notices?size=200").get_json()
ids = [x["id"] for x in j["data"]["items"]]
check("公文不出现在公告列表", did not in ids and secret_id not in ids, ids)
j = comm.get("/api/notices?recv=dispatched").get_json()
check("公文不冒充上级来文进待签",
      all(x["id"] not in (did, secret_id) for x in j["data"]["items"]))
check("公告总数不含公文", j["data"]["counts"]["matched"] >= 0)

print()
print("=" * 66)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)
