"""任务下发 + 字段三级管控 冒烟测试

覆盖：
A. 任务看板
  1. 街道登录落在 /tasks，能看到 4 条演示任务（含 1 条超时）
  2. 街道能下发任务；截止时间过去的被拒绝；受派角色非法的被拒绝
  3. 社区只能看到派给社区的任务（行级隔离）
  4. 社区不能下发任务（403 + 审计 deny）
  5. 社区能确认完成自己的任务；不能完成派给物业的任务
  6. 街道能关闭任意任务
  7. 重复完成返回失败
B. 字段三级管控
  8. FDE 提交后 field_security 有落库
  9. 手机号被判定为 3 级、姓名/房号为 2 级、楼栋为 1 级
 10. /api/fde/stats 的 secure_count 与表内 level>=2 的行数一致

运行：python scripts/smoke_tasks.py
"""
import os
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# 独立库：这套测试会写数据（改任务状态、写审计），
# 以前直接用 data/community.db，跑一遍就把演示库改了。
# 但它又**依赖演示数据**（field_security 是首录链路产生的，空库跑不了），
# 所以复制一份演示库来跑：数据齐、原库不动。
SRC = BASE / "data" / "community.db"
DB = BASE / "data" / "smoke_tasks.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()
if SRC.exists():
    import shutil
    # 只拷主库会丢数据：SQLite 开着 WAL，最近的写入还在 community.db-wal 里，
    # 不带上这个文件，拷出来的就是几小时前的旧快照。
    for suf in ("", "-wal", "-shm"):
        s_ = pathlib.Path(str(SRC) + suf)
        if s_.exists():
            shutil.copy2(s_, str(DB) + suf)
os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from datetime import datetime, timedelta  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import AuditLog, FieldSecurity, Task  # noqa: E402
from sqlalchemy import func, text                     # noqa: E402

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


def ddl(days=3):
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d 18:00")


print("=" * 66)
print("A. 任务看板")
print("=" * 66)

street = app.test_client()
r = login(street, "jiedao")
check("街道登录成功", r.status_code in (301, 302))

r = street.get("/tasks")
check("街道可打开任务看板", r.status_code == 200)

r = street.get("/api/tasks")
d = r.get_json()["data"]
check("街道看得到全部演示任务", d["summary"]["total"] >= 4, d["summary"])
check("演示数据含 1 条超时任务", d["summary"]["overdue"] >= 1, d["summary"])
check("街道 can_manage=True", d["can_manage"] is True)

# 下发
r = street.post("/api/tasks", json={
    "title": "冒烟测试任务-社区", "assignee_role": "community",
    "assignee_org": "幸福里社区", "deadline": ddl(5), "detail": "自动化测试下发"})
j = r.get_json()
check("街道下发任务成功", r.status_code == 200 and j["code"] == 0, j.get("msg"))
tid_comm = j["data"]["id"] if j["code"] == 0 else None

r = street.post("/api/tasks", json={
    "title": "冒烟测试任务-物业", "assignee_role": "property",
    "assignee_org": "幸福里物业", "deadline": ddl(2), "detail": ""})
j = r.get_json()
check("街道向物业下发任务成功", j["code"] == 0, j.get("msg"))
tid_prop = j["data"]["id"] if j["code"] == 0 else None

r = street.post("/api/tasks", json={
    "title": "过去的时间", "assignee_role": "community", "deadline": ddl(-1)})
check("截止时间在过去 → 拒绝", r.get_json()["code"] != 0)

r = street.post("/api/tasks", json={
    "title": "角色非法", "assignee_role": "street", "deadline": ddl(2)})
check("受派角色非法 → 拒绝", r.get_json()["code"] != 0)

r = street.post("/api/tasks", json={"title": "", "assignee_role": "community",
                                    "deadline": ddl(2)})
check("空标题 → 拒绝", r.get_json()["code"] != 0)

# 社区视角
comm = app.test_client()
login(comm, "wang")
r = comm.get("/api/tasks")
d = r.get_json()["data"]
check("社区看不到派给物业的任务",
      all(x["assignee_role"] == "community" for x in d["rows"]))
check("社区 can_manage=False", d["can_manage"] is False)
check("社区能看到刚下发的任务",
      any(x["id"] == tid_comm for x in d["rows"]))

r = comm.post("/api/tasks", json={"title": "越权下发", "assignee_role": "community",
                                  "deadline": ddl(2)})
check("社区下发任务 → 403", r.status_code == 403)
with app.app_context():
    denied = AuditLog.query.filter_by(agent="auth", action="deny") \
        .order_by(AuditLog.id.desc()).first()
    check("越权已写审计", denied is not None and "task" in (denied.input_digest or ""))

r = comm.post(f"/api/tasks/{tid_comm}/done")
check("社区能完成派给自己的任务", r.get_json()["code"] == 0, r.get_json().get("msg"))
r = comm.post(f"/api/tasks/{tid_comm}/done")
check("重复完成 → 失败", r.get_json()["code"] != 0)

r = comm.post(f"/api/tasks/{tid_prop}/done")
check("社区不能完成派给物业的任务", r.get_json()["code"] != 0 or r.status_code == 403)

# 物业视角
prop = app.test_client()
login(prop, "wuye")
r = prop.get("/api/tasks")
d = r.get_json()["data"]
check("物业只看得到派给物业的任务",
      all(x["assignee_role"] == "property" for x in d["rows"]))
r = prop.post(f"/api/tasks/{tid_prop}/done")
check("物业能完成自己的任务", r.get_json()["code"] == 0)

# 街道关闭
r = street.post(f"/api/tasks/{tid_prop}/done")
check("已完成任务再关闭 → 失败", r.get_json()["code"] != 0)

print()
print("=" * 66)
print("B. 字段三级管控")
print("=" * 66)

r = comm.post("/api/fde", json={
    "raw_text": "2栋2单元202室的租户李某，电话13800001234，反映楼道灯坏了，另外屋里水管漏水"})
j = r.get_json()
check("FDE 提交成功", j["code"] == 0, j.get("msg"))
items = j["data"]["items"]
check("返回的每个业务项都带 security 字段", all("security" in it for it in items))

flat = [s for it in items for s in it["security"]]
levels = {s["field"]: s["level"] for s in flat}
check("电话字段判为 3 级（加密）", levels.get("电话") == 3, levels)
check("姓名判为 2 级（脱敏）", levels.get("姓名") == 2, levels)
check("房号判为 2 级（脱敏）", levels.get("房号") == 2, levels)
check("楼栋判为 1 级（明文）", levels.get("楼栋") == 1, levels)

with app.app_context():
    rows = FieldSecurity.query.all()
    check("field_security 已落库", len(rows) > 0, f"{len(rows)} 行")
    n3 = FieldSecurity.query.filter_by(level=3).count()
    check("库内存在 3 级字段", n3 > 0, f"{n3} 个")
    # 每行都必须挂在真实业务项上
    from app.models import WorkItem
    orphan = [x.id for x in rows if db.session.get(WorkItem, x.work_item_id) is None]
    check("无孤儿业务项引用", not orphan, orphan)

r = comm.get("/api/fde/stats")
d = r.get_json()["data"]
with app.app_context():
    # 口径必须带 join：孤儿密级（业务项早被删了）不算管控字段。
    # 早先两边都是"裸 count"，看着一致，其实一起把不存在的业务项算了进去。
    #
    # 再往下一层还有个坑：期望值不能照抄实现里的 SQLAlchemy 表达式。
    # 实现一度写成 `db.session.query(func.count(X)).join(...).count()`——
    # 外层 count() 把"已经 count 过的子查询"再数一遍行数，恒等于 1；
    # 这里若也这么写，两边都是 1，全绿，而卡片上真的写着 1（实际 43）。
    # 所以期望值走裸 SQL，和实现没有任何共用代码。
    expect = db.session.execute(text(
        "SELECT count(*) FROM field_security fs "
        "JOIN work_item wi ON wi.id = fs.work_item_id WHERE fs.level >= 2")).scalar()
check("stats.secure_count 与有效业务项的密级一致", d["secure_count"] == expect,
      f"{d['secure_count']} vs {expect}")
check("夹具里密级记录 >1 条（否则上条断言对恒返回 1 的实现也成立）", expect > 1, expect)

print()
print("=" * 66)
print(f"结果：PASS {PASS} / {PASS + FAIL}" + ("  ✅ 全部通过" if FAIL == 0 else f"  ❌ {FAIL} 项失败"))
print("=" * 66)
sys.exit(0 if FAIL == 0 else 1)
