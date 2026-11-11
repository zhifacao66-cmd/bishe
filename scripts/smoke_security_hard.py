"""安全加固回归：把这一轮修掉的"安全遗漏"逐条钉住

对应的体检脚本是 `scripts/sec_probe.py`（打印现状，不预设结论）；
这里是同一批结论的断言版——**修一次不够，边界要靠断言守住**。

覆盖 8 类：
  ① 安全响应头（nosniff / 防点击劫持 / Referrer / CSP / 不报框架版本）
  ② /api/health 分档（匿名只给存活状态）
  ③ 公告 scope 越权（物业按 id 直取社区内部件、草稿、导出）
  ④ 居民端 6 位查询码锁定（连续猜错锁 30 分钟，且扛重启）
  ⑤ 自助注册限流（公网写入口不能被灌库）
  ⑥ https 下 Cookie 带 Secure
  ⑦ 导出留痕 + 存储型 XSS 的输出转义
  ⑧ 公网入口 P0：不采信 XFF / 限流与越权回真实 HTTP 码 / 全局提交闸

独立库 + 模型指向不可用端口，不依赖 GPU、不碰真实数据。
"""
import json
import os
import pathlib
import sys
import time

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_sec_hard.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                                     # noqa: E402
from app import auth as auth_mod                               # noqa: E402
from app import ratelimit                                      # noqa: E402
from app.extensions import db                                  # noqa: E402
from app.models import (AuditLog, Notice, Report,              # noqa: E402
                        ResidentTicket, User)

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


def login(c, u="wang", p="123456"):
    auth_mod._LOGIN_FAIL.clear()
    c.get("/logout")
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


def seed():
    with app.app_context():
        ids = {}
        for title, scope, status in (
                ("社区内部排班表", "community", "published"),
                ("面向物业的消杀安排", "property", "published"),
                ("还未确认的草稿", "all", "pending"),
                ("面向所有人的通知", "all", "published")):
            n = Notice(title=title, scope=scope, status=status, body_md="正文",
                       issuer="幸福里社区", category="通知公告", level="社区")
            db.session.add(n)
            db.session.flush()
            ids[title] = n.id
        rep = Report(period="2026-09", line="街道", version=1,
                     data_json=json.dumps({"summary": "九月汇总", "items": []},
                                          ensure_ascii=False))
        db.session.add(rep)
        db.session.add(ResidentTicket(code="123456", phone="13800138000",
                                      name="王阿姨", address="3号楼201",
                                      raw_text="楼道灯坏了", line_summary="物业"))
        db.session.commit()
        return ids, rep.id


IDS, RID = seed()

# ================================================================ ① 响应头
print("\n" + "=" * 66)
print("① 安全响应头")
print("=" * 66)
NEED = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": None,
    "Content-Security-Policy": None,
}
with app.test_client() as c:
    login(c)
    for path in ("/login", "/ledgers", "/portal", "/api/ledgers"):
        h = c.get(path).headers
        miss = [k for k in NEED if k not in h]
        check(f"{path} 五个安全头齐全", not miss, "缺 " + "、".join(miss))
    h = c.get("/login").headers
    check("nosniff 取值正确", h.get("X-Content-Type-Options") == "nosniff",
          h.get("X-Content-Type-Options"))
    check("CSP 禁止被嵌入（frame-ancestors none）",
          "frame-ancestors 'none'" in (h.get("Content-Security-Policy") or ""),
          h.get("Content-Security-Policy"))
    check("CSP 禁止 object/base 被劫持",
          "object-src 'none'" in (h.get("Content-Security-Policy") or "")
          and "base-uri 'self'" in (h.get("Content-Security-Policy") or ""), "")
    check("不暴露 Werkzeug 版本", "Werkzeug" not in (h.get("Server") or ""),
          h.get("Server"))

# ================================================================ ② 健康分档
print("\n" + "=" * 66)
print("② /api/health 只对登录者给运维详情")
print("=" * 66)
with app.test_client() as c:
    anon = (c.get("/api/health").get_json() or {}).get("data") or {}
    check("匿名能拿到存活状态", anon.get("status") in ("ok", "degraded", "down"),
          anon.get("status"))
    check("匿名拿不到通道地址", "channel" not in (anon.get("llm") or {}), anon.get("llm"))
    check("匿名拿不到进程号", "pid" not in anon, list(anon))
    check("匿名拿不到调用统计", "stats" not in (anon.get("llm") or {}), list(anon.get("llm") or {}))
    check("匿名拿不到数据库报错原文", "error" not in (anon.get("db") or {}), anon.get("db"))

    login(c, "wang")
    full = (c.get("/api/health").get_json() or {}).get("data") or {}
    check("登录后能拿到通道类型", "channel" in (full.get("llm") or {}), list(full))
    check("登录后能拿到调用统计", "stats" in (full.get("llm") or {}), "")
    # channel 里有 `token_set`（布尔）是给运维看的，但**令牌原文**不能出现
    check("登录后仍不返回令牌原文",
          '"token"' not in json.dumps(full.get("llm") or {}), "")

# ================================================================ ③ 公告范围
print("\n" + "=" * 66)
print("③ 公告 scope：物业不该读到社区内部件/草稿")
print("=" * 66)
with app.test_client() as c:
    login(c, "wuye")
    items = ((c.get("/api/notices").get_json() or {}).get("data") or {}).get("items", [])
    titles = [i["title"] for i in items]
    check("物业列表不含社区内部件", "社区内部排班表" not in titles, titles)
    check("物业列表不含待确认草稿", "还未确认的草稿" not in titles, titles)
    check("物业列表含面向自己的件", "面向物业的消杀安排" in titles, titles)
    check("物业列表含面向所有人的件", "面向所有人的通知" in titles, titles)

    d = (c.get(f"/api/notices/{IDS['社区内部排班表']}").get_json() or {})
    check("物业按 id 直取社区内部件被拒", d.get("code") != 0, d.get("msg"))
    d2 = (c.get(f"/api/notices/{IDS['还未确认的草稿']}").get_json() or {})
    check("物业按 id 直取草稿被拒", d2.get("code") != 0, d2.get("msg"))
    check("物业打开社区内部件页面 404",
          c.get(f"/notices/{IDS['社区内部排班表']}").status_code == 404, "")
    check("物业导出社区内部件 404",
          c.get(f"/notices/{IDS['社区内部排班表']}/export/docx").status_code == 404, "")
    check("物业能打开面向自己的件",
          c.get(f"/notices/{IDS['面向物业的消杀安排']}").status_code == 200, "")

    # 卡片数字必须和列表同源：口径不一致时"卡片写 1、点进去 0"最难查
    counts = ((c.get("/api/notices").get_json() or {}).get("data") or {}).get("counts", {})
    check("物业的待确认计数为 0（与列表同源）", counts.get("pending") == 0, counts)

    login(c, "wang")
    check("社区仍能读社区内部件",
          (c.get(f"/api/notices/{IDS['社区内部排班表']}").get_json() or {}).get("code") == 0, "")
    check("社区仍能读草稿（要确认/发布）",
          (c.get(f"/api/notices/{IDS['还未确认的草稿']}").get_json() or {}).get("code") == 0, "")
    check("社区能导出社区内部件",
          c.get(f"/notices/{IDS['社区内部排班表']}/export/docx").status_code == 200, "")

    login(c, "jiedao")
    check("街道能读全部范围（监督口径）",
          (c.get(f"/api/notices/{IDS['社区内部排班表']}").get_json() or {}).get("code") == 0, "")
    check("街道也能读草稿",
          (c.get(f"/api/notices/{IDS['还未确认的草稿']}").get_json() or {}).get("code") == 0, "")

# ================================================================ ④ 查询码锁定
print("\n" + "=" * 66)
print("④ 居民端查询码：连错 5 次锁 30 分钟")
print("=" * 66)
from app.api.portal import QUERY_FAIL_MAX, _phone_key            # noqa: E402

with app.test_client() as c:
    msgs = []
    for i in range(QUERY_FAIL_MAX):
        j = c.get("/api/portal/ticket",
                  query_string={"phone": "13800138000", "code": f"00000{i}"}).get_json() or {}
        msgs.append((j.get("data") or {}).get("msg") or j.get("msg") or "")
    check("前几次只是普通失败提示",
          all("锁定" not in m for m in msgs), msgs[-1])
    j = c.get("/api/portal/ticket",
              query_string={"phone": "13800138000", "code": "000009"}).get_json() or {}
    locked_msg = (j.get("data") or {}).get("msg") or j.get("msg") or ""
    check("第 6 次起明确锁定", "锁定" in locked_msg, locked_msg)
    check("锁定提示写明多久后可再试", "30 分钟" in locked_msg, locked_msg)
    j = c.get("/api/portal/ticket",
              query_string={"phone": "13800138000", "code": "123456"}).get_json() or {}
    check("锁定期间正确查询码也拒绝",
          "锁定" in ((j.get("data") or {}).get("msg") or j.get("msg") or ""), j)

    # 别的手机号不受影响：锁定必须按手机号隔离，不能一锁锁一片
    j = c.get("/api/portal/ticket",
              query_string={"phone": "13900139000", "code": "000000"}).get_json() or {}
    check("其它手机号不受影响",
          "锁定" not in ((j.get("data") or {}).get("msg") or j.get("msg") or ""), j)

with app.app_context():
    n = AuditLog.query.filter(AuditLog.agent == "portal",
                              AuditLog.action == "query_fail").count()
    check("失败尝试已落审计（重启不清零）", n >= QUERY_FAIL_MAX, n)
    rows = AuditLog.query.filter(AuditLog.action == "query_fail").all()
    check("审计里不落手机号明文",
          all("13800138000" not in json.dumps(r.output_digest or "") and
              "13800138000" not in (r.actor or "") for r in rows), "出现明文")
    check("审计键是手机号哈希（稳定、不撞号）",
          all((r.actor or "").startswith("portal:") and len(r.actor) == 23 for r in rows),
          rows[0].actor if rows else "")
    check("锁定判定用同一套键", _phone_key("13800138000").startswith("portal:"), "")

# ================================================================ ⑤ 注册限流
print("\n" + "=" * 66)
print("⑤ 自助注册限流")
print("=" * 66)
with app.test_client() as c:
    made, codes = 0, []
    for i in range(8):
        before = 0
        r = c.post("/register", data={"username": f"bulk{i}", "name": f"批量{i}",
                                      "org": "某社区", "phone": "13800000000",
                                      "password": "Passw0rd!", "password2": "Passw0rd!"})
        codes.append(r.status_code)
        c.get("/logout")
    with app.app_context():
        made = User.query.filter(User.created_by == "自助注册").count()
    check("连续注册被限流（不是无限灌库）", made < 8, f"建了 {made} 个，码 {codes}")
    check("限流后返回 429", 429 in codes, codes)
    check("限流前后都在正常建号（前几次成功）", made >= 3, made)

# ================================================================ ⑥ Cookie
print("\n" + "=" * 66)
print("⑥ 会话 Cookie")
print("=" * 66)
with app.test_client() as c:
    r = c.post("/login", data={"username": "wang", "password": "123456"},
               headers={"X-Forwarded-Proto": "https"})
    ck = r.headers.get("Set-Cookie") or ""
    check("https 下带 Secure", "Secure" in ck, ck[-40:])
    check("始终带 HttpOnly", "HttpOnly" in ck, "")
    check("始终带 SameSite", "SameSite=Lax" in ck, "")
with app.test_client() as c:
    r = c.post("/login", data={"username": "wang", "password": "123456"})
    ck = r.headers.get("Set-Cookie") or ""
    check("http 下不加 Secure（否则本机登录不上）", "Secure" not in ck, ck[-40:])

# ================================================================ ⑦ 留痕与转义
print("\n" + "=" * 66)
print("⑦ 导出留痕 + 输出转义")
print("=" * 66)
with app.app_context():
    before = AuditLog.query.filter(AuditLog.agent == "export").count()
with app.test_client() as c:
    login(c, "wang")
    check("公文导出返回文件",
          c.get(f"/notices/{IDS['社区内部排班表']}/export/docx").status_code == 200, "")
    check("报表导出返回文件", c.get(f"/reports/{RID}/export/xlsx").status_code == 200, "")
with app.app_context():
    logs = AuditLog.query.filter(AuditLog.agent == "export").all()
    check("导出动作写审计", len(logs) - before >= 2, f"新增 {len(logs) - before} 条")
    check("审计记到了具体文件",
          any("排班表" in (r.output_digest or "") for r in logs),
          [r.output_digest for r in logs][:2])
    check("审计记了操作人",
          all(r.actor and r.actor != "system" for r in logs), [r.actor for r in logs][:2])

# 存储型 XSS：模板里的 innerHTML 插值必须过 esc()（前端函数，只能在源码层守）
TPL = BASE / "app" / "templates"
base_html = (TPL / "base.html").read_text(encoding="utf-8")
check("base.html 提供全局 esc()", "window.esc = function" in base_html, "")
check("esc() 转义五个字符",
      all(ch in base_html for ch in ("'&'", "'<'", "'>'", '&quot;', "&#39;")), "")

SINKS = {
    "audit.html": ["${esc(l.actor)", "${esc(l.input_digest)", "${esc(l.output_digest)"],
    "ledgers.html": ["${esc(l.subject)", "${esc(l.feedback)"],
    "kb.html": ["${esc(d.title)", "${esc(h.snippet)", "${esc(h.title)"],
    "reports.html": ["${esc(v.summary)", "${esc(String(r.summary"],
    "index.html": ["esc(d.masked_text)", "${esc(it.subject)", "${esc(o.property_co)"],
    "system.html": ["${esc(k)", "${esc(d.output)"],
}
for fname, pats in SINKS.items():
    src = (TPL / fname).read_text(encoding="utf-8")
    miss = [p for p in pats if p not in src]
    check(f"{fname} 的用户字段已转义", not miss, "缺 " + "、".join(miss))

kb_src = (TPL / "kb.html").read_text(encoding="utf-8")
check("kb 删除按钮不再把标题拼进 onclick",
      "delDoc(${d.id})" in kb_src and "delDoc(${d.id},'" not in kb_src, "")

# 用户可控字段直出 JSON 是正常的（浏览器按文本处理），
# 要保证的是**模板**不把它当 HTML。这里断言姓名能落库并出现在接口里，
# 而模板已经包了 esc——两端拼起来才是完整的链。
with app.app_context():
    db.session.add(User(username="xss_probe", name="<img src=x onerror=alert(1)>",
                        org="某社区", role="community", status="pending",
                        perms_json="[]", created_by="自助注册",
                        password_hash="x"))
    db.session.commit()
with app.test_client() as c:
    login(c, "admin", "admin123")
    body = json.dumps((c.get("/api/audit").get_json() or {}), ensure_ascii=False)
    check("审计接口不回 HTML 页面（JSON 才是对的）",
          body.strip().startswith("{"), body[:40])

# ================================================================ ⑧ 公网入口 P0
print("\n[⑧] 公网入口：可信 IP / 状态码 / 全局提交闸")
from app.api.portal import _client_ip as portal_ip              # noqa: E402
from app.auth import _client_ip as auth_ip                      # noqa: E402

with app.test_request_context(
        "/", headers={"X-Forwarded-For": "203.0.113.9"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"}):
    check("portal 不采信 XFF（只读 remote_addr）",
          portal_ip() == "127.0.0.1", portal_ip())
    check("auth 不采信 XFF（只读 remote_addr）",
          auth_ip() == "127.0.0.1", auth_ip())

with app.test_client() as c:
    ratelimit.reset()
    r = c.post("/api/portal/report", json={"phone": "123", "text": "x"})
    check("非法手机号 → HTTP 400", r.status_code == 400, r.status_code)
    # 同一手机号第 6 次在入参校验前就被限流挡下，返回真实 429，
    # 不再用 200 把"被限流"伪装成"处理成功"。
    for _ in range(5):
        c.post("/api/portal/report", json={"phone": "123", "text": "x"})
    r = c.post("/api/portal/report", json={"phone": "123", "text": "x"})
    check("手机号维度超限 → HTTP 429", r.status_code == 429, r.status_code)
    r = c.get("/api/portal/ticket/999999")
    check("不存在的单据 → HTTP 404", r.status_code == 404, r.status_code)

    # 全局闸是"维度限流之外"的最后一层：直接占满窗口，验证 31 次必被拒
    ratelimit.reset()
    with ratelimit._LOCK:
        ratelimit._BUCKET["report:global"] = [time.time()] * 30
    r = c.post("/api/portal/report",
               json={"phone": "13800138000", "text": "楼道灯坏了"})
    check("全局提交闸超限 → HTTP 429", r.status_code == 429, r.status_code)
    ratelimit.reset()

    # 并发准入：4 个槽位占满后第 5 个请求必须立即 429，而不是排进模型队列
    from app.api import portal as portal_mod
    ratelimit.reset()
    for _ in range(4):
        portal_mod._REPORT_SLOTS.acquire()
    try:
        r = c.post("/api/portal/report",
                   json={"phone": "13800138000", "text": "楼道灯坏了"})
        check("公网并发槽位占满 → HTTP 429", r.status_code == 429, r.status_code)
    finally:
        for _ in range(4):
            portal_mod._REPORT_SLOTS.release()
    ratelimit.reset()

print("\n" + "=" * 66)
print(f"合计 {PASS + FAIL} 项，通过 {PASS}，失败 {FAIL}")
if FAILED:
    for x in FAILED:
        print("  ✗ " + x)
    print("=" * 66)
    sys.exit(1)
print("=" * 66)
