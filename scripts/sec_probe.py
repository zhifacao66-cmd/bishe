"""信息安全体检：把"我以为做了"的项逐条打一遍，看真实响应

和 `smoke_*.py` 的区别：这个脚本**不预设结论**，只把现状打印出来对照期望，
用来发现"文档里写了、代码里没有"的偏差。发现的问题修完之后，再把结论
固化成 `smoke_security.py` 里的断言。

独立库 + 模型地址指向不可用端口，跑起来不需要 GPU、不碰真实数据。
用法：python scripts/sec_probe.py
"""
import json
import os
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "sec_probe.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                                     # noqa: E402
from app import auth as auth_mod                               # noqa: E402
from app.extensions import db                                  # noqa: E402
from app.models import (AuditLog, Notice, Report, ResidentTicket,  # noqa: E402
                        User)

app = create_app("dev")

ROWS = []


def row(area, question, ok, detail=""):
    ROWS.append((area, question, ok, detail))
    mark = "OK  " if ok else "GAP "
    print(f"  [{mark}] {question}" + (f"   → {detail}" if detail else ""))


def login(c, u="wang", p="123456"):
    """切身份前必须先退出。

    登录页对"已登录"的请求是直接跳首页的（`if u: redirect`），
    不先登出就换账号，会话里还是上一个人——探针会拿物业的身份去断言
    "社区应该看得到"，报出一个假问题，白查一轮。
    """
    auth_mod._LOGIN_FAIL.clear()
    c.get("/logout")
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


def seed():
    with app.app_context():
        db.session.add_all([
            Notice(title="社区内部排班表", scope="community", status="published",
                   body_md="本周值班：张三 / 李四", issuer="幸福里社区",
                   category="通知公告", level="社区", doc_no="幸社发〔2026〕9号"),
            Notice(title="待确认的草稿", scope="all", status="pending",
                   body_md="草稿正文", issuer="幸福里社区",
                   category="通知公告", level="社区"),
            Notice(title="面向物业的消杀安排", scope="property", status="published",
                   body_md="消杀时间本周五", issuer="幸福里社区",
                   category="通知公告", level="社区"),
        ])
        db.session.add(Report(period="2026-09", line="街道", version=1,
                              data_json=json.dumps({"summary": "九月汇总",
                                                    "items": []}, ensure_ascii=False)))
        db.session.add(ResidentTicket(code="123456", phone="13800138000",
                                      name="王阿姨", address="3号楼2单元201",
                                      raw_text="3号楼2单元楼道灯坏了",
                                      line_summary="物业"))
        db.session.commit()
        return {n.title: n.id for n in Notice.query.all()}


print("\n" + "=" * 72)
print(" ① 安全响应头（XSS / 点击劫持 / 嗅探的兜底防线）")
print("=" * 72)
HDR_NEED = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": None,               # 值不论，有就行（DENY/SAMEORIGIN）
    "Referrer-Policy": None,
    "Content-Security-Policy": None,
    "Permissions-Policy": None,
}
with app.test_client() as c:
    login(c)
    for path in ("/login", "/ledgers", "/portal", "/api/ledgers"):
        r = c.get(path)
        miss = [h for h in HDR_NEED if h not in r.headers]
        row("响应头", f"{path} 安全头齐全", not miss, "缺：" + "、".join(miss) if miss else "")
    r = c.get("/login")
    row("响应头", "不暴露 Werkzeug 版本", "Werkzeug" not in r.headers.get("Server", ""),
        r.headers.get("Server", ""))

print("\n" + "=" * 72)
print(" ② 公开健康接口暴露了什么（未登录可见）")
print("=" * 72)
with app.test_client() as c:
    j = c.get("/api/health").get_json() or {}
    d = (j.get("data") or {})
    leaked = []
    if "base" in (d.get("llm", {}).get("channel") or {}):
        leaked.append("通道地址")
    if "token_set" in (d.get("llm", {}).get("channel") or {}):
        leaked.append("是否带令牌")
    if "pid" in d:
        leaked.append("进程号")
    if (d.get("db") or {}).get("error"):
        leaked.append("数据库错误原文")
    if "stats" in (d.get("llm") or {}):
        leaked.append("调用统计")
    row("信息暴露", "未登录看不到部署形态", not leaked,
        "泄露：" + "、".join(leaked) if leaked else "")
    row("信息暴露", "未登录仍能拿到存活状态", d.get("status") in ("ok", "degraded", "down"),
        d.get("status"))

print("\n" + "=" * 72)
print(" ③ 公告发布范围 scope（社区内部 / 物业，不该互相能读）")
print("=" * 72)
IDS = seed()
with app.test_client() as c:
    login(c, "wuye")                        # 物业账号
    j = c.get("/api/notices").get_json() or {}
    titles = [i["title"] for i in (j.get("data") or {}).get("items", [])]
    row("越权", "物业的列表里没有社区内部公告", "社区内部排班表" not in titles,
        f"列表 {len(titles)} 条")
    nid = IDS["社区内部排班表"]
    r = c.get(f"/api/notices/{nid}")
    got = (r.get_json() or {}).get("code") == 0
    row("越权", "物业按 id 直取社区内部公告被拒", not got, f"HTTP {r.status_code}")
    r2 = c.get(f"/notices/{nid}")
    row("越权", "物业打开社区内部公告页面被拒", r2.status_code in (403, 404),
        f"HTTP {r2.status_code}")
    r3 = c.get(f"/notices/{nid}/export/docx")
    row("越权", "物业导出社区内部公告被拒", r3.status_code in (403, 404),
        f"HTTP {r3.status_code} {len(r3.data)}B")
    # 草稿：内部治理要看得见，物业不该看见"还没确认"的
    r4 = c.get(f"/api/notices/{IDS['待确认的草稿']}")
    row("越权", "物业看不到待确认草稿", (r4.get_json() or {}).get("code") != 0,
        f"HTTP {r4.status_code}")
    # 社区自己要看得到本社区范围与草稿（治理需要，不能修过头）
    lg = login(c, "wang")
    r5 = c.get(f"/api/notices/{nid}")
    row("越权", "社区自己仍能读社区内部公告", (r5.get_json() or {}).get("code") == 0,
        f"HTTP {r5.status_code} login={lg.status_code} {(r5.get_json() or {}).get('msg')}")
    r6 = c.get(f"/api/notices/{IDS['待确认的草稿']}")
    row("越权", "社区仍能读书稿（要确认/发布）", (r6.get_json() or {}).get("code") == 0,
        f"HTTP {r6.status_code}")
    login(c, "jiedao")
    r7 = c.get(f"/api/notices/{nid}")
    row("越权", "街道仍能读全部范围（监督口径）", (r7.get_json() or {}).get("code") == 0,
        f"HTTP {r7.status_code}")

print("\n" + "=" * 72)
print(" ④ 居民端 6 位查询码：连续猜错会被锁吗")
print("=" * 72)
with app.test_client() as c:
    codes = []
    for i in range(12):
        r = c.get("/api/portal/ticket", query_string={"phone": "13800138000",
                                                     "code": f"00000{i % 10}"})
        j = r.get_json() or {}
        codes.append((j.get("data") or {}).get("msg") or j.get("msg") or "")
    locked = [m for m in codes if "锁" in (m or "") or "稍后再试" in (m or "")]
    row("暴力破解", "连错 5 次后明确锁定", bool(locked), f"{len(locked)}/12 次被拒")
    row("暴力破解", "锁定提示说清多久后能再试",
        any("分钟" in (m or "") for m in locked), locked[0] if locked else "无提示")
    # 正确查询码此时也该被拒（锁定期间不能放行，否则锁了等于没锁）
    r = c.get("/api/portal/ticket", query_string={"phone": "13800138000", "code": "123456"})
    j = r.get_json() or {}
    row("暴力破解", "锁定期间正确码同样拒绝", (j.get("code") or 0) != 0,
        (j.get("data") or {}).get("msg") or j.get("msg") or "")

print("\n" + "=" * 72)
print(" ⑤ 自助注册：能无限灌库 / 枚举账号吗")
print("=" * 72)
with app.app_context():
    before = User.query.filter(User.created_by == "自助注册").count()
with app.test_client() as c:
    codes = []
    for i in range(8):
        r = c.post("/register", data={"username": f"probe{i}", "name": f"探测{i}",
                                      "org": "某社区", "phone": "13800000000",
                                      "password": "Passw0rd!", "password2": "Passw0rd!"})
        codes.append(r.status_code)
        c.get("/logout")                     # 注册即登录，先退出才能注册下一个
with app.app_context():
    made = User.query.filter(User.created_by == "自助注册").count() - before
# 断言看**真的建了几个账号**，不看 HTTP 码：第一次注册成功后会 SPA 跳转，
# 用状态码判断会把"被重定向"当成"注册成功"
row("滥用", "同 IP 连续注册被限流", made < 8, f"8 次尝试建了 {made} 个账号，码 {codes}")

print("\n" + "=" * 72)
print(" ⑥ 会话 Cookie（HTTPS 下的 Secure 标记）")
print("=" * 72)
with app.test_client() as c:
    r = c.post("/login", data={"username": "wang", "password": "123456"},
               headers={"X-Forwarded-Proto": "https"})
    ck = " | ".join(r.headers.getlist("Set-Cookie"))
    row("会话", "https 访问时 Cookie 带 Secure", "Secure" in ck, ck[:130])
    row("会话", "Cookie 带 HttpOnly", "HttpOnly" in ck, "")
    row("会话", "Cookie 带 SameSite", "SameSite" in ck, "")

print("\n" + "=" * 72)
print(" ⑦ 导出留痕（谁把公文/报表拿走了）")
print("=" * 72)
with app.app_context():
    before = AuditLog.query.count()
with app.test_client() as c:
    login(c, "wang")
    r = c.get(f"/notices/{IDS['社区内部排班表']}/export/docx")
    r2 = c.get("/reports/1/export/xlsx")
with app.app_context():
    after = AuditLog.query.count()
    acts = [a.action for a in AuditLog.query.order_by(AuditLog.id.desc()).limit(10)]
row("审计", "导出公文留痕", r.status_code == 200 and after > before + 1,
    f"HTTP {r.status_code}，审计新增 {after - before} 条，最近：{acts[:3]}")
row("审计", "导出报表留痕", r2.status_code == 200, f"HTTP {r2.status_code}")

print("\n" + "=" * 72)
print(" ⑧ 错误页与跨站写")
print("=" * 72)
with app.test_client() as c:
    r = c.get("/api/does-not-exist")
    row("错误处理", "/api 不存在路径回 JSON", r.status_code == 404 and
        r.headers.get("Content-Type", "").startswith("application/json"), r.status_code)
    r = c.post("/api/fde", json={"text": "x"},
               headers={"Sec-Fetch-Site": "cross-site"})
    row("CSRF", "跨站写被 403", r.status_code == 403, r.status_code)
    r = c.post("/api/fde", json={"text": "x"},
               headers={"Sec-Fetch-Site": "same-origin", "Origin": "http://evil.example"})
    row("CSRF", "伪造 Origin 但 Sec-Fetch 同站（真实浏览器不会这么发）",
        r.status_code in (200, 400, 401), r.status_code)

print("\n" + "=" * 72)
gaps = [r for r in ROWS if not r[2]]
print(f" 合计 {len(ROWS)} 项，通过 {len(ROWS) - len(gaps)}，**待修 {len(gaps)}**")
for g in gaps:
    print(f"   · [{g[0]}] {g[1]}  {g[3]}")
print("=" * 72 + "\n")
sys.exit(0)
