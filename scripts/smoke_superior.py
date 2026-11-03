"""回归：上级单位账号的创建、授权与督办视角

回答三个问题：
  1. 上级单位要不要单独的角色？—— 不要，street 就是上级，层级由账号档案决定；
  2. 账号怎么建？—— 自助注册只有 community 角色，街道/物业只能管理员直接建；
  3. 权限怎么配才对？—— 发文 + 督办，但不签收、不办文、不发布。

最后一条是本套测试的重点：**街道不能替社区办文**。
如果哪天有人"顺手"给街道加了 notice.localize，监督者和执行者就合并了，
收文流程存在的意义也就没了。测试在这里当看门狗。

用独立库 + 关掉后台扫描线程，不碰 data/community.db。
"""
import os
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_superior.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"   # 死端口 → 强制规则兜底
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                        # noqa: E402
from app.auth import ROLE_PERMS, SUPERIOR_LEVELS   # noqa: E402
from app.extensions import db                      # noqa: E402
from app.models import AuditLog, Notice, User      # noqa: E402

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


def user_by(username):
    with app.app_context():
        u = User.query.filter_by(username=username).first()
        if not u:
            return None
        return {"role": u.role, "org": u.org, "status": u.status,
                "perms_json": u.perms_json, "created_by": u.created_by}


print("=" * 68)
print("上级单位账号 · 授权 · 督办视角 回归")
print("=" * 68)

admin = app.test_client()
street = app.test_client()
community = app.test_client()

print("\n[1] 三层登录")
check("管理员登录", login(admin, "admin", "admin123").status_code == 302)
check("街道登录", login(street, "jiedao", "123456").status_code == 302)
check("社区登录", login(community, "wang", "123456").status_code == 302)

# ---------------------------------------------------------------- 2 权限基线
print("\n[2] 角色权限基线：街道是上级，但不是全能")
check("街道有 notice.import（能发文）", "notice.import" in ROLE_PERMS["street"])
check("街道有 task.manage（能下任务）", "task.manage" in ROLE_PERMS["street"])
check("街道有 audit.view（能查留痕）", "audit.view" in ROLE_PERMS["street"])
check("街道没有 notice.receive（不能替社区签收）",
      "notice.receive" not in ROLE_PERMS["street"])
check("街道没有 notice.localize（不能替社区办文）",
      "notice.localize" not in ROLE_PERMS["street"])
check("街道没有 notice.manage（不能代替社区对外发布）",
      "notice.manage" not in ROLE_PERMS["street"])
check("街道没有 fde.create（不产生业务数据）",
      "fde.create" not in ROLE_PERMS["street"])
check("街道没有 user.manage（账号治理不下放）",
      "user.manage" not in ROLE_PERMS["street"])
check("SUPERIOR_LEVELS 含街道", "街道" in SUPERIOR_LEVELS)

# ---------------------------------------------------------------- 3 建号校验
print("\n[3] 管理员建上级单位账号：入参校验")
r = admin.post("/api/admin/users", json={"username": "x", "name": "李主任",
                                         "org": "和平区民政局", "role": "street",
                                         "password": "minzheng2026"})
check("账号格式不合法被拒", r.status_code == 400, f"HTTP {r.status_code}")

r = admin.post("/api/admin/users", json={"username": "minzheng", "name": "李主任",
                                         "org": "和平区民政局", "role": "street",
                                         "password": "123"})
check("弱密码被拒（<8 位）", r.status_code == 400, f"HTTP {r.status_code}")

r = admin.post("/api/admin/users", json={"username": "minzheng", "name": "李主任",
                                         "org": "和平区民政局", "role": "resident",
                                         "password": "minzheng2026"})
check("居民角色不可建（走免注册通道）", r.status_code == 400, f"HTTP {r.status_code}")

r = admin.post("/api/admin/users", json={"username": "wang", "name": "重复",
                                         "org": "幸福里社区", "role": "community",
                                         "password": "duplicate2026"})
check("重复账号被拒", r.status_code == 400, f"HTTP {r.status_code}")

# ---------------------------------------------------------------- 4 建号成功
print("\n[4] 管理员建号：上级单位与物业")
r = admin.post("/api/admin/users", json={
    "username": "minzheng", "name": "李主任", "org": "和平区民政局",
    "role": "street", "password": "minzheng2026", "phone": "02212345678",
})
j = r.get_json()
check("建号返回成功", r.status_code == 200 and j.get("code") == 0, str(j)[:120])
check("角色为街道", (j.get("data") or {}).get("role") == "street")
check("状态直接为正常（不进审核队列）",
      (j.get("data") or {}).get("status") == "active")
check("权限数 = 街道默认", (j.get("data") or {}).get("perm_count")
      == len(ROLE_PERMS["street"]),
      f"{(j.get('data') or {}).get('perm_count')} vs {len(ROLE_PERMS['street'])}")
check("记录创建人", (user_by("minzheng") or {}).get("created_by") == "系统管理员",
      str((user_by("minzheng") or {}).get("created_by")))
check("建号写审计", audit_has("user_create"))

# 按人授权：只给看，不给发
r = admin.post("/api/admin/users", json={
    "username": "yuejian", "name": "赵干事", "org": "和平区民政局",
    "role": "street", "password": "yuejian2026",
    "perms": ["notice.view", "report.view"],
})
check("按人授权建号成功", r.status_code == 200 and r.get_json().get("code") == 0)
check("按人授权数与提交一致", (r.get_json().get("data") or {}).get("perm_count") == 2,
      str((r.get_json().get("data") or {}).get("perm_count")))
check("按人授权落库为自定义", (user_by("yuejian") or {}).get("perms_json") is not None)

# ---------------------------------------------------------------- 5 越权
print("\n[5] 只有管理员能建号")
r = street.post("/api/admin/users", json={
    "username": "hack1", "name": "越权", "org": "x",
    "role": "admin", "password": "hack12345",
})
check("街道建号被拒 403", r.status_code == 403, f"HTTP {r.status_code}")
r = community.post("/api/admin/users", json={
    "username": "hack2", "name": "越权", "org": "x",
    "role": "admin", "password": "hack12345",
})
check("社区建号被拒 403", r.status_code == 403, f"HTTP {r.status_code}")
r = app.test_client().post("/api/admin/users", json={
    "username": "hack3", "name": "越权", "org": "x",
    "role": "admin", "password": "hack12345",
})
check("未登录建号 401", r.status_code == 401, f"HTTP {r.status_code}")

# ---------------------------------------------------------------- 6 新账号可用
print("\n[6] 新账号能登录并按角色授权")
new = app.test_client()
check("新建街道账号可登录", login(new, "minzheng", "minzheng2026").status_code == 302)
r = new.get("/api/admin/me")
d = r.get_json().get("data") or {}
check("登录后身份正确", d.get("role") == "street" and d.get("org") == "和平区民政局",
      str(d)[:120])

# 只读账号：能看不能发
ro = app.test_client()
login(ro, "yuejian", "yuejian2026")
r = ro.post("/api/notices/dispatch", json={
    "title": "越权发文测试", "issuer": "和平区民政局", "body_md": "正文"})
check("只给 notice.view 的账号不能发文 403", r.status_code == 403,
      f"HTTP {r.status_code}")

# ---------------------------------------------------------------- 7 层级由账号决定
print("\n[7] 发文层级取账号档案，不写死")
BODY = """各社区：

现将冬季供暖设施检修有关事项通知如下：

一、检修范围
辖区内所有集中供暖小区。

二、时间安排
11 月 1 日至 11 月 15 日完成。

三、工作要求
各社区做好居民告知，配合供热单位入户。
"""
r = new.post("/api/notices/dispatch", json={
    "title": "关于开展冬季供暖设施检修的通知",
    "issuer": "和平区民政局",
    "doc_no": "和民发〔2026〕31号",
    "receiver_org": "幸福里社区",
    "body_md": BODY,
})
j = r.get_json()
NID = (j.get("data") or {}).get("id")
check("上级单位发文成功", r.status_code == 200 and j.get("code") == 0, str(j)[:120])
check("层级按账号记（街道）", (j.get("data") or {}).get("level") == "街道",
      str((j.get("data") or {}).get("level")))
check("来源标注带层级", "街道在线发文" in ((j.get("data") or {}).get("source_file") or ""),
      str((j.get("data") or {}).get("source_file")))
check("落为上级来文", (j.get("data") or {}).get("kind") == "superior")
check("初始状态待签收", (j.get("data") or {}).get("recv_status") == "dispatched")
check("投递后不直接发布（等社区确认）",
      (j.get("data") or {}).get("status") == "pending")

# ---------------------------------------------------------------- 8 上级不能代签代办
print("\n[8] 上级单位不能替社区签收、办文、发布")
r = new.post(f"/api/notices/{NID}/receive")
check("街道签收被拒 403", r.status_code == 403, f"HTTP {r.status_code}")
r = new.get(f"/api/notices/{NID}/draft")
check("街道取落地件草稿被拒 403", r.status_code == 403, f"HTTP {r.status_code}")
r = new.post(f"/api/notices/{NID}/localize",
             json={"title": "街道替社区办的文", "body_md": "正文"})
check("街道办文被拒 403", r.status_code == 403, f"HTTP {r.status_code}")
r = new.post(f"/api/notices/{NID}/publish")
check("街道发布被拒 403", r.status_code == 403, f"HTTP {r.status_code}")

# ---------------------------------------------------------------- 9 社区签收
print("\n[9] 社区签收：收文待办里出现，签完留痕")
r = community.get("/api/notices?recv=dispatched")
items = (r.get_json().get("data") or {}).get("items") or []
check("社区待签收列表含该文件", any(x["id"] == NID for x in items),
      f"共 {len(items)} 条")
r = community.post(f"/api/notices/{NID}/receive")
check("社区签收成功", r.status_code == 200 and r.get_json().get("code") == 0)
with app.app_context():
    n = db.session.get(Notice, NID)
    check("签收人留痕", n.received_by == "王社工", str(n.received_by))
    check("签收时间留痕", n.received_at is not None)
    check("签收不改发布状态（两件事解耦）", n.status == "pending", str(n.status))
check("签收写审计", audit_has("receive"))

# ---------------------------------------------------------------- 10 督办视角
print("\n[10] 上级督办视角：看得到办没办")
r = new.get("/api/notices")
items = (r.get_json().get("data") or {}).get("items") or []
row = next((x for x in items if x["id"] == NID), None)
check("街道能在列表里看到该文件", row is not None)
check("已签收状态可见", (row or {}).get("recv_status") == "received")
check("未办文可见", (row or {}).get("local_count") == 0,
      str((row or {}).get("local_count")))
check("已发布落地件数为 0", (row or {}).get("local_published") == 0)

# 社区办文
r = community.get(f"/api/notices/{NID}/draft")
check("社区能取到落地件草稿", r.status_code == 200 and r.get_json().get("code") == 0)
draft = r.get_json().get("data") or {}
check("草稿带回来源 id", draft.get("parent_id") == NID)
r = community.post(f"/api/notices/{NID}/localize", json={
    "title": draft.get("title") or "幸福里社区关于供暖检修的通知",
    "body_md": draft.get("body_md") or "正文",
    "publish": False,
})
j = r.get_json()
LID = (j.get("data") or {}).get("id")
check("社区生成落地件成功", r.status_code == 200 and j.get("code") == 0, str(j)[:120])
check("落地件是上级来文之外的独立记录", (j.get("data") or {}).get("kind") == "local")
check("落地件指回原文", (j.get("data") or {}).get("parent_id") == NID)

r = new.get("/api/notices")
row = next((x for x in (r.get_json().get("data") or {}).get("items") or []
            if x["id"] == NID), None)
check("街道看到「已办文」", (row or {}).get("local_count") == 1,
      str((row or {}).get("local_count")))
check("街道看到「落地件未发布」", (row or {}).get("local_published") == 0,
      str((row or {}).get("local_published")))

# 社区发布落地件
r = community.post(f"/api/notices/{LID}/publish")
check("社区发布落地件成功", r.status_code == 200 and r.get_json().get("code") == 0)
r = new.get("/api/notices")
row = next((x for x in (r.get_json().get("data") or {}).get("items") or []
            if x["id"] == NID), None)
check("街道看到「落地件已发布」", (row or {}).get("local_published") == 1,
      str((row or {}).get("local_published")))

# ---------------------------------------------------------------- 11 街道其余权限
print("\n[11] 街道的其余权限边界")
r = new.get("/api/reports")
check("街道可看报表", r.status_code == 200, f"HTTP {r.status_code}")
r = new.get("/api/audit")
check("街道可看审计", r.status_code == 200, f"HTTP {r.status_code}")
r = new.get("/api/ledgers")
check("街道看不到居民台账 403", r.status_code == 403, f"HTTP {r.status_code}")
r = new.post("/api/fde", json={"text": "3栋501报修漏水"})
check("街道不能首录 403", r.status_code == 403, f"HTTP {r.status_code}")
r = new.get("/api/admin/users")
check("街道不能管账号 403", r.status_code == 403, f"HTTP {r.status_code}")

# ---------------------------------------------------------------- 12 停用即失效
print("\n[12] 停用上级账号后立即失去全部权限")
with app.app_context():
    uid = User.query.filter_by(username="minzheng").first().id
r = admin.post(f"/api/admin/users/{uid}", json={"status": "disabled"})
check("管理员停用成功", r.status_code == 200 and r.get_json().get("code") == 0)
r = new.get("/api/notices")
check("停用后立即 403", r.status_code == 403, f"HTTP {r.status_code}")

# ---------------------------------------------------------------- 13 总结
print("\n" + "=" * 68)
print(f"通过 {PASS} 项 / 失败 {FAIL} 项")
if FAILED:
    print("失败项：")
    for x in FAILED:
        print("  -", x)
print("=" * 68)

for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass

sys.exit(1 if FAIL else 0)
