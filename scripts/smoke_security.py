"""安全回归：越权 / 越界读取 / 暴力破解 / 会话密钥

这些用例对应 docs/04「已知不足」里被逐个修掉的项，目的是让它们**不再复发**：
修一次性的 bug 靠手测够用，安全边界必须靠断言守住。

独立库 + 模型地址指向不可用端口，保证确定性、不依赖 GPU。
"""
import os
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_security.db"
if DB.exists():
    DB.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
os.environ["NOTICE_AUTO_IMPORT"] = "0"      # 本测试自己造公告，不扫描样例目录

from app import create_app                                    # noqa: E402
from app import auth as auth_mod                              # noqa: E402
from app.extensions import db                                 # noqa: E402
from app.models import AuditLog, Notice, User, WorkOrder      # noqa: E402

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
    auth_mod._LOGIN_FAIL.clear()          # 每个角色重新登录，避免被上一个用例的失败计数影响
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


def seed_orders():
    """造两条分属不同物业的工单，用来验证水平越权"""
    with app.app_context():
        mine = WorkOrder(order_no="W-SEC-001", content="本物业的电梯异响",
                         property_co="幸福里物业", sla_hours=24, sla_level="较急",
                         status="open")
        other = WorkOrder(order_no="W-SEC-002", content="别家物业的路灯不亮",
                          property_co="其他物业公司", sla_hours=72, sla_level="一般",
                          status="open")
        db.session.add_all([mine, other])
        db.session.commit()
        return mine.id, other.id


def seed_notice():
    """一条 scope=community 的内部通知（已发布，但只给社区看）"""
    with app.app_context():
        n = Notice(title="关于内部考核排名的通报", issuer="和平街道办事处",
                   body_md="本通知仅限社区工作者查阅。", status="published",
                   scope="community", category="通知公告", level="街道")
        db.session.add(n)
        db.session.commit()
        return n.id


def main():
    mine_id, other_id = seed_orders()
    inner_notice_id = seed_notice()

    # ---------------------------------------------------------------- 1
    print("\n[1] 未登录访问内部接口")
    c = app.test_client()
    for path in ("/api/orders", "/api/ledgers", "/api/reports"):
        r = c.get(path)
        check(f"GET {path} = 401", r.status_code == 401, r.status_code)
    # 首录是写接口（POST），未登录同样必须先被挡在权限守卫上
    r = c.post("/api/fde", json={"text": "3栋电梯异响"})
    check("POST /api/fde = 401", r.status_code == 401, r.status_code)

    # ---------------------------------------------------------------- 2
    print("\n[2] 水平越权：物业不能操作别家的工单（IDOR）")
    c = app.test_client()
    login(c, "wuye")
    r = c.post(f"/api/orders/{other_id}/accept")
    check("跨主体接单被拒 403", r.status_code == 403, r.status_code)
    check("拒绝原因不含别家单号明文", "W-SEC-002" not in (r.get_json() or {}).get("msg", ""))
    with app.app_context():
        o = db.session.get(WorkOrder, other_id)
        check("别家工单状态未被改动", o.status == "open", o.status)
        denied = AuditLog.query.filter_by(agent="auth", action="deny").count()
        check("越权尝试已写审计", denied >= 1, f"deny 记录 {denied} 条")

    r = c.post(f"/api/orders/{mine_id}/accept")
    check("本主体接单成功", r.status_code == 200, r.status_code)
    with app.app_context():
        check("本主体工单已转 doing",
              db.session.get(WorkOrder, mine_id).status == "doing")

    r = c.post(f"/api/orders/{other_id}/finish", json={"feedback": "越权尝试"})
    check("跨主体办结被拒 403", r.status_code == 403, r.status_code)

    # 列表侧隔离与写侧一致
    data = c.get("/api/orders").get_json()["data"]["items"]
    check("列表只含本物业工单",
          all(o["property_co"] == "幸福里物业" for o in data),
          {o["property_co"] for o in data})
    check("列表不含别家工单", all(o["order_no"] != "W-SEC-002" for o in data))

    # ---------------------------------------------------------------- 3
    print("\n[3] 越界读取：居民端只能看 scope=all 的公告")
    c = app.test_client()
    r = c.get(f"/portal/notice/{inner_notice_id}")
    check("scope=community 公告详情 404", r.status_code == 404, r.status_code)
    html = c.get("/portal/notices").get_data(as_text=True)
    check("列表不含内部通报标题", "内部考核排名" not in html)
    html = c.get("/portal").get_data(as_text=True)
    check("首页不含内部通报标题", "内部考核排名" not in html)

    # 社区自己可以看到
    c = app.test_client()
    login(c, "wang")
    r = c.get(f"/api/notices/{inner_notice_id}")
    check("社区端可读取该公告", r.status_code == 200, r.status_code)

    # ---------------------------------------------------------------- 4
    print("\n[4] 暴力破解：登录失败限流")
    c = app.test_client()
    auth_mod._LOGIN_FAIL.clear()
    codes = []
    for i in range(7):
        r = c.post("/login", data={"username": "wang", "password": "wrong-pass"},
                   follow_redirects=False)
        codes.append(r.status_code)
    check("前 5 次返回普通失败页", codes[:5] == [200] * 5, codes)
    check("第 6 次起返回 429", codes[5] == 429 and codes[6] == 429, codes)
    check("锁定期间正确密码也拒绝",
          c.post("/login", data={"username": "wang", "password": "123456"},
                 follow_redirects=False).status_code == 429)
    with app.app_context():
        n = AuditLog.query.filter_by(agent="auth", action="login_fail").count()
        check("失败尝试已写审计", n >= 5, f"login_fail {n} 条")
    auth_mod._LOGIN_FAIL.clear()
    r = c.post("/login", data={"username": "wang", "password": "123456"},
               follow_redirects=False)
    check("解除限流后正常登录", r.status_code == 302, r.status_code)

    # ---------------------------------------------------------------- 4b
    print("\n[4b] 代理/伪造 XFF 不能绕过限流，账号维度是主约束")
    # 线上实测：托管环境的每个请求 X-Forwarded-For 都不同（代理出口 IP 池），
    # 按「IP|账号」计数永远都是"第 1 次"，限流形同虚设。
    # 现在代码已不再采信 XFF（只认 ProxyFix 归一的 remote_addr，见 security_hard ⑧），
    # 这里继续验证：无论请求头怎么变，账号维度都会在 5 次后锁定。
    auth_mod._LOGIN_FAIL.clear()
    c = app.test_client()
    codes = []
    for i in range(7):
        r = c.post("/login", data={"username": "wuye", "password": "bad"},
                   headers={"X-Forwarded-For": f"203.0.113.{100 + i}"},
                   follow_redirects=False)
        codes.append(r.status_code)
    check("IP 每次变化时账号维度仍会锁定", codes[5] == 429 and codes[6] == 429, codes)
    auth_mod._LOGIN_FAIL.clear()

    # 同一 IP 换账号撞库 → 由 IP 辅助维度兜住（阈值高于账号维度）
    ip_headers = {"X-Forwarded-For": "198.51.100.7"}
    blocked_at = None
    for i in range(40):
        r = c.post("/login", data={"username": f"scan{i}", "password": "x"},
                   headers=ip_headers, follow_redirects=False)
        if r.status_code == 429:
            blocked_at = i
            break
    check("单 IP 扫大量账号会被兜底拦截", blocked_at is not None,
          f"40 次内未拦截（blocked_at={blocked_at}）")
    auth_mod._LOGIN_FAIL.clear()

    # ---------------------------------------------------------------- 5
    print("\n[5] 会话安全：密钥与 Cookie 属性")
    check("dev 允许使用开发默认密钥", app.config["SECRET_KEY"] == "dev-secret-key-change-me")

    keyfile = BASE / ".secret_key"
    had_keyfile = keyfile.exists()
    backup = keyfile.read_text(encoding="utf-8") if had_keyfile else None
    if keyfile.exists():                    # 从干净状态开始，才能验证"首次生成"
        keyfile.unlink()
    try:
        prod = create_app("prod")
        check("prod 未配置时不再用默认密钥",
              prod.config["SECRET_KEY"] != "dev-secret-key-change-me")
        check("prod 密钥已落盘", keyfile.exists())
        check("落盘密钥长度足够",
              len(prod.config["SECRET_KEY"]) >= 32, len(prod.config["SECRET_KEY"]))
        check("再次创建应用复用同一密钥（重启不掉登录）",
              create_app("prod").config["SECRET_KEY"] == prod.config["SECRET_KEY"])

        os.environ["SECRET_KEY"] = "explicit-test-key"
        try:
            check("显式配置的 SECRET_KEY 优先",
                  create_app("prod").config["SECRET_KEY"] == "explicit-test-key")
        finally:
            os.environ.pop("SECRET_KEY", None)
    finally:
        # 还原现场：原本有就写回，原本没有就删掉
        if had_keyfile:
            keyfile.write_text(backup, encoding="utf-8")
        elif keyfile.exists():
            keyfile.unlink()

    c = app.test_client()
    r = c.post("/login", data={"username": "wuye", "password": "123456"},
               follow_redirects=False)
    cookie = " ".join(r.headers.getlist("Set-Cookie"))
    check("会话 Cookie 带 HttpOnly", "HttpOnly" in cookie, cookie[:120])
    check("会话 Cookie 带 SameSite=Lax", "SameSite=Lax" in cookie, cookie[:120])

    # ---------------------------------------------------------------- 收尾
    print(f"\n{'=' * 46}")
    print(f"通过 {PASS} / {PASS + FAIL}")
    if FAILED:
        print("失败项：")
        for x in FAILED:
            print("  -", x)
    print("=" * 46)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
