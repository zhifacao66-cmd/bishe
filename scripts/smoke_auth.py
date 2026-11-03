"""三角色登录与权限冒烟测试

覆盖：
1. 未登录访问页面 → 302 到 /login
2. 错误密码 → 登录页返回错误提示
3. 社区工作者：能首录/台账/报表，不能接单、不能看审计
4. 物业：只看到本公司工单，能接单反馈，不能看首录/台账/审计
5. 街道：能看报表与审计，不能首录、不能接单
6. 越权尝试写入 audit_log(action=deny)
7. 登出后回到登录页

运行：python scripts/smoke_auth.py
"""
import os
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# 独立库：这套测试会写数据（首录、接单、审计），
# 以前直接用 data/community.db，跑一遍就把演示库的工单状态和审计记录改了。
DB = BASE / "data" / "smoke_auth.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import AuditLog, WorkOrder  # noqa: E402

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


def login(c, u, p="123456"):
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


print("=" * 66)
print("① 未登录与错误密码")
print("=" * 66)
c = app.test_client()
r = c.get("/")
check("GET / 未登录跳转登录页", r.status_code == 302 and "/login" in r.headers["Location"],
      f"{r.status_code} {r.headers.get('Location')}")
r = c.get("/api/orders")
check("GET /api/orders 未登录返回 401 JSON", r.status_code == 401 and r.get_json()["code"] == 401)
r = login(c, "wang", "wrong-pwd")
check("错误密码停留在登录页并提示", r.status_code == 200 and "用户名或密码错误" in r.get_data(as_text=True))
r = login(c, "not-exist")
check("不存在的账号同样提示（不泄露账号是否存在）",
      r.status_code == 200 and "用户名或密码错误" in r.get_data(as_text=True))

print("=" * 66)
print("② 社区工作者 wang")
print("=" * 66)
c = app.test_client()
r = login(c, "wang")
check("登录成功跳转首页", r.status_code == 302 and r.headers["Location"].endswith("/"))
check("GET / 首录台 200", c.get("/").status_code == 200)
check("GET /orders 工单可看 200", c.get("/orders").status_code == 200)
check("GET /ledgers 台账 200", c.get("/ledgers").status_code == 200)
check("GET /reports 报表 200", c.get("/reports").status_code == 200)
check("GET /audit 审计 403（社区无权）", c.get("/audit").status_code == 403)

r = c.post("/api/fde", json={"raw_text": "2栋2单元202室新租户李某电话13800001234",
                             "creator": "冒名者"})
d = r.get_json()["data"]
check("POST /api/fde 首录成功", r.status_code == 200 and r.get_json()["code"] == 0)
fde_id = d["fde_id"]
# 自己造一条物业类工单：不依赖库里既有数据，空库也能跑（回归测试应可重复）
r = c.post("/api/fde", json={"raw_text": "3栋2单元楼道灯不亮了，需要物业维修"})
check("物业类首录生成工单", len(r.get_json()["data"].get("orders") or []) >= 1)
with app.app_context():
    from app.models import FdeRecord
    rec = db.session.get(FdeRecord, fde_id)
    check("creator 取会话身份（不采信前端传值）", rec.creator == "王社工", rec.creator)
    oid = WorkOrder.query.filter_by(status="open").first().id

r = c.post(f"/api/orders/{oid}/accept")
check("社区接单 403", r.status_code == 403, r.status_code)
check("403 返回中文原因", "无「接单" in r.get_json()["msg"], r.get_json().get("msg"))
r = c.post("/api/reports/generate", json={"period": "2026-10"})
check("社区可生成报表", r.get_json()["code"] == 0)

print("=" * 66)
print("③ 物业 wuye")
print("=" * 66)
c = app.test_client()
r = login(c, "wuye")
check("登录后跳到工单页", r.status_code == 302 and r.headers["Location"].endswith("/orders"))
r = c.get("/")
check("GET / 重定向到工单页（不弹 403）", r.status_code == 302 and "/orders" in r.headers["Location"])
check("GET /orders 200", c.get("/orders").status_code == 200)
check("GET /ledgers 403（物业看不到人口台账）", c.get("/ledgers").status_code == 403)
check("GET /reports 403", c.get("/reports").status_code == 403)
check("GET /audit 403", c.get("/audit").status_code == 403)
check("GET /api/fde/stats 403", c.get("/api/fde/stats").status_code == 403)

lst = c.get("/api/orders").get_json()["data"]["items"]
check("工单列表只含本公司工单", all(o["property_co"] == "幸福里物业" for o in lst), len(lst))
with app.app_context():
    oid = WorkOrder.query.filter_by(status="open").first().id
check("接单成功", c.post(f"/api/orders/{oid}/accept").get_json()["code"] == 0)
check("反馈闭环成功",
      c.post(f"/api/orders/{oid}/finish", json={"feedback": "已更换楼道灯"}).get_json()["code"] == 0)

print("=" * 66)
print("④ 街道 jiedao")
print("=" * 66)
c = app.test_client()
r = login(c, "jiedao")
check("登录后跳到任务看板（街道的默认落地页）",
      r.status_code == 302 and r.headers["Location"].endswith("/tasks"),
      r.headers.get("Location"))
check("GET /tasks 200", c.get("/tasks").status_code == 200)
check("GET /reports 200", c.get("/reports").status_code == 200)
check("GET /audit 200", c.get("/audit").status_code == 200)
check("GET /orders 200（监督视角可看工单）", c.get("/orders").status_code == 200)
check("GET /ledgers 403（不接触人口明细）", c.get("/ledgers").status_code == 403)
r = c.post("/api/fde", json={"raw_text": "街道自己录一条"})
check("POST /api/fde 403", r.status_code == 403)
with app.app_context():
    oid = WorkOrder.query.first().id
check("街道接单 403", c.post(f"/api/orders/{oid}/accept").status_code == 403)
check("GET /api/audit 可读", c.get("/api/audit").get_json()["code"] == 0)
logs = c.get("/api/audit?agent=auth").get_json()["data"]["items"]
check("审计含登录记录", any(l["action"] == "login" for l in logs))
check("审计含越权拒绝记录", any(l["action"] == "deny" for l in logs))

print("=" * 66)
print("⑤ 登出")
print("=" * 66)
r = c.post("/logout")
check("登出跳登录页", r.status_code == 302 and "/login" in r.headers["Location"])
check("登出后访问 /audit 被拦截", c.get("/audit").status_code == 302)
check("登出后 API 401", c.get("/api/audit").status_code == 401)

print("=" * 66)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)
