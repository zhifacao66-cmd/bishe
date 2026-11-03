"""账号治理回归：自助注册 / 待审核 / 按人授权 / 防自锁

核心要守住的三件事：
1. 注册 ≠ 可用：新账号落库时必须是「待审核 + 零权限」，不能凭注册就进门。
2. 权限按人走：管理员勾几个就是几个，改了立刻生效，不用重新登录。
3. 管理员锁不死：停用自己、降级自己、停掉最后一个管理员，三种操作必须被拒。

独立库 + 模型端口指向不可用地址，保证确定性、不依赖 GPU。
"""
import json
import os
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_admin.db"
if DB.exists():
    DB.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                                    # noqa: E402
from app import auth as auth_mod                              # noqa: E402
from app.auth import PERMS, ROLE_PERMS, user_perms              # noqa: E402

# 权限点总数与社区默认项数不写死：以后加权限点不该让这套测试失败
N_PERMS = len(PERMS)
N_COMMUNITY = len(ROLE_PERMS["community"])
from app.extensions import db                                 # noqa: E402
from app.models import AuditLog, User                         # noqa: E402

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
        print(f"  ✗ {name}" + (f"  [{detail}]" if detail else ""))


def login(c, u, p="123456"):
    auth_mod._LOGIN_FAIL.clear()
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


def fetch(username):
    with app.app_context():
        u = User.query.filter_by(username=username).first()
        return (u.id, u.role, u.status, u.perms_json, len(user_perms(u)))


def audit_has(action):
    with app.app_context():
        return AuditLog.query.filter_by(action=action).count() > 0


def api_get(c, path):
    return c.get(path)


def main():
    admin_c = app.test_client()
    wang_c = app.test_client()

    # ---------------------------------------------------------------- 1 管理员账号
    print("\n[1] 系统管理员演示账号")
    uid, role, st, pj, n = fetch("admin")
    check("admin 账号已初始化", uid is not None)
    check("角色为 admin", role == "admin", role)
    check("状态为 active", st == "active", st)
    check(f"admin 默认拥有全部 {N_PERMS} 项权限", n == N_PERMS, n)
    check("admin 未自定义权限（跟随全量）", pj is None, pj)

    r = login(admin_c, "admin", "admin123")
    check("admin 用独立密码登录成功", r.status_code == 302, r.status_code)
    r = login(app.test_client(), "admin", "123456")
    check("admin 不能用公用 123456 登录", r.status_code == 200, r.status_code)

    r = admin_c.get("/admin/users")
    check("admin 可访问用户管理页", r.status_code == 200, r.status_code)
    r = admin_c.get("/api/admin/users")
    body = r.get_json()
    check("admin 可拉取账号列表", r.status_code == 200 and body["code"] == 0)
    # 不写死条数：演示账号会随业务需要增加（物业有李经理和赵师傅两个维修工，
    # 转派才演示得了）。要守的是"初始账号都出现在列表里"。
    rows = body["data"]
    names = {u.get("username") for u in rows}
    check("列表含全部初始账号",
          {"admin", "wang", "wuye", "wuye2", "jiedao"} <= names, sorted(names))

    # ---------------------------------------------------------------- 2 越权保护
    print("\n[2] 非管理员访问用户管理")
    login(wang_c, "wang")
    r = wang_c.get("/admin/users")
    check("社区工作者访问 /admin/users = 403", r.status_code == 403, r.status_code)
    r = wang_c.get("/api/admin/users")
    check("社区工作者拉账号列表 = 403", r.status_code == 403, r.status_code)
    check("越权尝试写入 deny 审计", audit_has("deny"))
    r = app.test_client().get("/api/admin/users")
    check("未登录拉账号列表 = 401", r.status_code == 401, r.status_code)

    # ---------------------------------------------------------------- 3 自助注册
    print("\n[3] 新员工自助注册")
    new_c = app.test_client()
    r = new_c.post("/register", data={
        "username": "zhaoshegong", "name": "赵社工", "org": "幸福里社区",
        "phone": "13800001111", "password": "shegong2026", "password2": "shegong2026",
    }, follow_redirects=False)
    check("注册成功跳转待审核页", r.status_code == 302 and "/pending" in r.headers.get("Location", ""),
          r.headers.get("Location"))

    uid_z, role_z, st_z, pj_z, n_z = fetch("zhaoshegong")
    check("新账号角色默认社区工作者", role_z == "community", role_z)
    check("新账号状态为待审核", st_z == "pending", st_z)
    check("新账号零权限（明确给空）", n_z == 0, n_z)
    check("零权限记录为 [] 而非跟随角色", pj_z == "[]", pj_z)
    check("注册写入审计", audit_has("register"))

    r = new_c.get("/orders")
    check("待审核访问业务页被引到 /pending",
          r.status_code == 302 and "/pending" in r.headers.get("Location", ""),
          r.headers.get("Location"))
    r = new_c.get("/api/orders")
    check("待审核访问业务接口 = 403", r.status_code == 403, r.status_code)
    r = new_c.get("/pending")
    check("待审核页本身可访问", r.status_code == 200, r.status_code)
    r = new_c.get("/api/admin/me")
    check("待审核可自查状态", r.status_code == 200 and r.get_json()["data"]["status"] == "pending")

    r = app.test_client().post("/register", data={
        "username": "zhaoshegong", "name": "赵社工", "org": "幸福里社区",
        "password": "shegong2026", "password2": "shegong2026",
    }, follow_redirects=False)
    check("重复账号被拒（匿名新会话）",
          r.status_code == 200 and "已被占用" in r.get_data(as_text=True))

    r = new_c.post("/register", data={
        "username": "other01", "name": "别人", "org": "幸福里社区",
        "password": "shegong2026", "password2": "shegong2026",
    }, follow_redirects=False)
    check("已登录用户访问注册页被送回业务页",
          r.status_code == 302, r.status_code)

    r = app.test_client().post("/register", data={
        "username": "ab", "name": "短账号", "org": "幸福里社区",
        "password": "shegong2026", "password2": "shegong2026",
    })
    check("非法账号名被拒", "3~20 位" in r.get_data(as_text=True))
    r = app.test_client().post("/register", data={
        "username": "duanmima", "name": "短密码", "org": "幸福里社区",
        "password": "123456", "password2": "123456",
    })
    check("6 位弱密码被拒", "至少 8 位" in r.get_data(as_text=True))

    # ---------------------------------------------------------------- 4 审核与授权
    print("\n[4] 审核通过并按岗位授权")
    r = admin_c.get("/api/admin/users?status=pending")
    pend = [x for x in r.get_json()["data"] if x["username"] == "zhaoshegong"]
    check("管理员能看到待审核账号", len(pend) == 1, len(pend))

    r = admin_c.post(f"/api/admin/users/{uid_z}/approve",
                     json={"role": "community",
                           "perms": ["order.view", "ledger.view"]})
    d = r.get_json()["data"]
    check("审核通过返回正常状态", d["status"] == "active", d["status"])
    check("按勾选授予 2 项权限", d["perm_count"] == 2, d["perm_count"])
    check("标记为已自定义权限", d["perms_custom"] is True)
    check("审核写入审计", audit_has("user_approve"))

    r = new_c.get("/api/admin/me")
    d = r.get_json()["data"]
    check("本人自查状态即时变为 active", d["status"] == "active", d["status"])
    check("本人自查权限即时变为 2 项", d["perm_count"] == 2, d["perm_count"])

    # 权限实时生效：有 ledger.view 没 task.view
    r = new_c.get("/api/ledgers")
    check("有 ledger.view 时台账接口放行", r.status_code == 200, r.status_code)
    r = new_c.get("/api/tasks")
    check("无 task.view 时任务接口 403", r.status_code == 403, r.status_code)
    r = new_c.get("/orders")
    check("有 order.view 时工单页放行", r.status_code == 200, r.status_code)
    r = new_c.get("/ledgers")
    check("有 ledger.view 时台账页放行", r.status_code == 200, r.status_code)
    r = new_c.get("/reports")
    check("无 report.view 时报表页 403", r.status_code == 403, r.status_code)

    # ---------------------------------------------------------------- 5 权限增减
    print("\n[5] 管理员按人增减权限（实时生效，不用重新登录）")
    r = admin_c.post(f"/api/admin/users/{uid_z}", json={"perms": ["order.view"]})
    d = r.get_json()["data"]
    check("改成只有 order.view 一项", d["perm_count"] == 1, d["perm_count"])
    r = new_c.get("/api/orders")
    check("保留的 order.view 仍然放行", r.status_code == 200, r.status_code)
    r = new_c.get("/api/ledgers")
    check("被收回的 ledger.view 即时失效", r.status_code == 403, r.status_code)

    r = admin_c.post(f"/api/admin/users/{uid_z}", json={"perms": None})
    d = r.get_json()["data"]
    check(f"恢复跟随角色后拿到社区默认 {N_COMMUNITY} 项",
          d["perm_count"] == N_COMMUNITY, d["perm_count"])
    check("恢复后标记为非自定义", d["perms_custom"] is False)

    r = admin_c.post(f"/api/admin/users/{uid_z}", json={"perms": ["user.manage"]})
    check("可把「用户管理」单独授予他人", r.get_json()["data"]["perm_count"] == 1)
    z_c = app.test_client()
    login(z_c, "zhaoshegong", "shegong2026")
    r = z_c.get("/admin/users")
    check("被授予 user.manage 后可进入用户管理", r.status_code == 200, r.status_code)
    check("但其业务权限仍只有 1 项", fetch("zhaoshegong")[4] == 1)

    r = admin_c.post(f"/api/admin/users/{uid_z}", json={"perms": ["nonexistent.perm"]})
    check("不存在的权限点被过滤掉", r.get_json()["data"]["perm_count"] == 0)

    # ---------------------------------------------------------------- 6 角色变更
    print("\n[6] 改角色与停用")
    r = admin_c.post(f"/api/admin/users/{uid_z}", json={"role": "property"})
    check("角色可改为物业", r.get_json()["data"]["role"] == "property")
    r = admin_c.post(f"/api/admin/users/{uid_z}", json={"role": "root"})
    check("非法角色被拒", r.status_code == 400, r.status_code)

    r = admin_c.post(f"/api/admin/users/{uid_z}", json={"status": "disabled"})
    check("停用成功", r.get_json()["data"]["status"] == "disabled")
    check("停用后权限清零", fetch("zhaoshegong")[4] == 0)

    z2 = app.test_client()
    r = login(z2, "zhaoshegong", "shegong2026")
    check("停用账号无法登录", r.status_code == 200
          and "已被停用" in r.get_data(as_text=True))
    check("停用登录尝试写入审计", audit_has("login_blocked"))

    admin_c.post(f"/api/admin/users/{uid_z}", json={"status": "active"})
    # 恢复使用只还原"能不能登录"，不还原权限：
    # 之前已被管理员明确改成 0 项，恢复后仍是 0 项，需要重新授权。
    # 这是有意的——停用再启用不该变成一次悄悄的放权。
    check("恢复后权限不自动放权（仍为 0 项）", fetch("zhaoshegong")[4] == 0,
          fetch("zhaoshegong")[4])
    admin_c.post(f"/api/admin/users/{uid_z}", json={"perms": None})
    # 物业默认 5 项：order.view / order.handle / task.view / notice.view
    # + doc.view（P0 新增：物业要能看发给自己的函，密级过滤在 app/api/docs.py:_visible）
    check("恢复跟随角色后回到物业默认 5 项", fetch("zhaoshegong")[4] == 5,
          fetch("zhaoshegong")[4])

    # ---------------------------------------------------------------- 7 防自锁
    print("\n[7] 管理员不能把自己锁在门外")
    admin_uid = fetch("admin")[0]
    r = admin_c.post(f"/api/admin/users/{admin_uid}", json={"status": "disabled"})
    check("不能停用自己", r.status_code == 400 and "自己" in r.get_json()["msg"],
          r.get_json().get("msg"))
    r = admin_c.post(f"/api/admin/users/{admin_uid}", json={"role": "community"})
    check("不能取消自己的管理员角色", r.status_code == 400, r.status_code)
    check("admin 账号仍为管理员", fetch("admin")[1] == "admin")

    r = admin_c.post(f"/api/admin/users/{admin_uid}", json={"perms": []})
    d = r.get_json()["data"]
    check(f"admin 即使被传空权限仍是 {N_PERMS} 项", d["perm_count"] == N_PERMS, d["perm_count"])

    # 让 zhaoshegong 只拿 user.manage，去停用唯一的管理员 admin
    admin_c.post(f"/api/admin/users/{uid_z}", json={"role": "community",
                                                    "perms": ["user.manage"],
                                                    "status": "active"})
    z3 = app.test_client()
    login(z3, "zhaoshegong", "shegong2026")
    r = z3.post(f"/api/admin/users/{admin_uid}", json={"status": "disabled"})
    check("非管理员的账号管理员不能停掉唯一 admin",
          r.status_code == 400 and "至少一个" in r.get_json()["msg"],
          r.get_json().get("msg"))
    r = z3.post(f"/api/admin/users/{admin_uid}", json={"role": "community"})
    check("也不能把唯一 admin 降级",
          r.status_code == 400 and "至少一个" in r.get_json()["msg"],
          r.get_json().get("msg"))

    # 有两个管理员时就允许停用一个
    admin_c.post(f"/api/admin/users/{uid_z}", json={"role": "admin"})
    check("可以把他人设为管理员", fetch("zhaoshegong")[1] == "admin")
    r = admin_c.post(f"/api/admin/users/{uid_z}", json={"status": "disabled"})
    check("存在两个管理员时允许停用其中一个", r.get_json()["data"]["status"] == "disabled")

    # ---------------------------------------------------------------- 8 重置密码
    print("\n[8] 重置密码")
    r = admin_c.post(f"/api/admin/users/{uid_z}/password", json={"password": "123"})
    check("过短临时密码被拒", r.status_code == 400, r.status_code)
    r = admin_c.post(f"/api/admin/users/{uid_z}/password", json={"password": "temp2026ok"})
    check("重置密码成功", r.status_code == 200 and r.get_json()["code"] == 0)
    check("重置写入审计", audit_has("user_reset_pwd"))

    # ---------------------------------------------------------------- 9 老账号兼容
    print("\n[9] 老账号兼容（perms_json 为 NULL 时跟随角色）")
    uid_w, role_w, st_w, pj_w, n_w = fetch("wang")
    check("老账号 perms_json 为 NULL", pj_w is None)
    check(f"老账号仍跟随社区默认 {N_COMMUNITY} 项", n_w == N_COMMUNITY, n_w)
    check("老账号状态为 active", st_w == "active", st_w)

    print("\n" + "=" * 60)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILED:
        print("失败项：" + "；".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
