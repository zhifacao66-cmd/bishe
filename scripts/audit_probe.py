"""严谨性探测：模拟真实使用者，专挑边界与非法路径。

不是回归测试，是体检——每条只回答"系统是否明确地拒绝了坏输入"。
注意本项目约定：业务失败也是 HTTP 200 + code=1，所以判成功必须看 code。

运行：python scripts/audit_probe.py
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "audit_probe.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()
shutil.copy(BASE / "data" / "community.db", DB)
wal = BASE / "data" / "community.db-wal"
if wal.exists():
    shutil.copy(wal, pathlib.Path(str(DB) + "-wal"))

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"   # 强制规则路径，快且不依赖 GPU
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app  # noqa: E402

app = create_app("dev")

FINDINGS: list[tuple[str, str, str, str]] = []


def rec(sev: str, code: str, verdict: str, evidence: str = ""):
    FINDINGS.append((sev, code, verdict, evidence))


def login(c, u, p="123456"):
    return c.post("/login", data={"username": u, "password": p}).status_code


def call(c, method, url, body=None):
    r = c.get(url) if method == "GET" else c.post(url, json=body if body is not None else {})
    try:
        j = r.get_json(silent=True)
    except Exception:
        j = None
    return r, j


def ok(r, j) -> bool:
    return r.status_code == 200 and isinstance(j, dict) and j.get("code") == 0


def show(label, r, j):
    if isinstance(j, dict):
        print(f"  {label:18s} HTTP {r.status_code} code={j.get('code')}  {str(j.get('msg'))[:36]}")
    else:
        ct = (r.headers.get("Content-Type") or "").split(";")[0]
        print(f"  {label:18s} HTTP {r.status_code} 非JSON({ct})")


# ---------------------------------------------------------------- A 首录输入
print("=" * 78); print("A. 首录输入边界（POST /api/fde）"); print("=" * 78)
wc = app.test_client(); login(wc, "wang")

cases = [
    ("空字符串", {"raw_text": ""}, False),
    ("纯空白", {"raw_text": "   \n\t  "}, False),
    ("纯标点", {"raw_text": "。。。！！！？？？"}, None),
    ("超长 4000 字", {"raw_text": "楼道灯坏了" * 800}, None),
    ("单个字符", {"raw_text": "灯"}, None),
    ("注入式文本", {"raw_text": "<script>alert(1)</script>'; DROP TABLE work_item;--"}, None),
    ("缺 raw_text", {}, False),
    ("raw_text 是数字", {"raw_text": 12345}, False),
    ("source 非法值", {"raw_text": "楼道灯坏了三天", "source": "hacker"}, None),
]
for name, body, should_fail in cases:
    r, j = call(wc, "POST", "/api/fde", body)
    show(name, r, j)
    d = (j or {}).get("data") or {} if isinstance(j, dict) else {}
    n = len(d.get("items") or []) if isinstance(d, dict) else 0
    if r.status_code >= 500:
        rec("高", "A1", f"首录「{name}」导致 5xx", f"HTTP {r.status_code}")
    elif should_fail is False and ok(r, j):
        rec("中", "A2", f"首录「{name}」被接受", f"拆出 {n} 项")
    if ok(r, j):
        print(f"      -> 拆出 {n} 项")

# 非法 JSON body
r = wc.post("/api/fde", data="{bad json", content_type="application/json")
print(f"  {'非法JSON体':18s} HTTP {r.status_code} {(r.headers.get('Content-Type') or '')[:30]}")
if r.status_code >= 500:
    rec("高", "A3", "非法 JSON 体导致 5xx", f"HTTP {r.status_code}")

# ---------------------------------------------------------------- B 工单状态机
print(); print("=" * 78); print("B. 工单状态机（合法序列 + 非法回退）"); print("=" * 78)
pc = app.test_client(); login(pc, "wuye")
r, j = call(pc, "GET", "/api/orders?size=50")
orders = (((j or {}).get("data") or {}).get("items") or []) if isinstance(j, dict) else []
opened = [o for o in orders if o.get("status") == "open"]
print(f"  可读 {len(orders)} 条，其中 open {len(opened)} 条")
if opened:
    oid = opened[0]["id"]
    seq = [("直接办结(应拒)", "finish"), ("受理(应过)", "accept"), ("重复受理(应拒)", "accept"),
           ("办结(应过)", "finish"), ("重复办结(应拒)", "finish"), ("办结后受理(应拒)", "accept")]
    for label, act in seq:
        r, j = call(pc, "POST", f"/api/orders/{oid}/{act}", {"feedback": "楼道灯已更换"})
        show(f"{label}", r, j)
        good = ok(r, j)
        if label.endswith("(应拒)") and good:
            rec("中", "B1", f"工单状态机：{label[:-4]}被接受", f"id={oid} /{act}")
        if label.endswith("(应过)") and not good:
            rec("高", "B2", f"工单状态机：{label[:-4]}被拒绝", f"id={oid} /{act} {j.get('msg')}")
else:
    print("  (无 open 工单可测，跳过)")

for url, act in [("/api/orders/999999/accept", "accept"), ("/api/orders/abc/accept", "accept")]:
    r, j = call(pc, "POST", url, {})
    show(f"{act} {url.split('/')[-2]}", r, j)
    if r.status_code >= 500:
        rec("高", "B3", "工单不存在/非法 id 导致 5xx", url)
    if not isinstance(j, dict):
        rec("低", "B4", "非法路径返回 HTML 而非 JSON", url)

# ---------------------------------------------------------------- C 任务下发
print(); print("=" * 78); print("C. 任务下发校验"); print("=" * 78)
sc = app.test_client(); login(sc, "jiedao")
task_cases = [
    ("正常", {"title": "测试任务", "assignee_role": "community", "deadline": "2026-12-31"}, True),
    ("缺标题", {"assignee_role": "community"}, False),
    ("标题纯空白", {"title": "   ", "assignee_role": "community"}, False),
    ("标题 300 字", {"title": "任" * 300, "assignee_role": "community"}, False),
    ("非法日期", {"title": "日期测试", "assignee_role": "community", "deadline": "2026-13-45"}, False),
    ("日期非格式", {"title": "日期测试2", "assignee_role": "community", "deadline": "下周三"}, False),
    ("过去日期", {"title": "过期任务", "assignee_role": "community", "deadline": "2020-01-01"}, False),
    ("非法角色", {"title": "角色测试", "assignee_role": "hacker"}, False),
    ("缺角色", {"title": "无角色"}, False),
    ("受派社区但无组织", {"title": "组织缺失", "assignee_role": "community"}, None),
]
for name, body, expect_ok in task_cases:
    r, j = call(sc, "POST", "/api/tasks", body)
    show(name, r, j)
    if ok(r, j) and expect_ok is False:
        rec("中", "C1", f"下发任务「{name}」被接受", str(j.get("msg")))
    if not ok(r, j) and expect_ok is True:
        rec("高", "C2", f"下发任务「{name}」被拒绝", str(j.get("msg")))
    if r.status_code >= 500:
        rec("高", "C3", f"下发任务「{name}」5xx", f"HTTP {r.status_code}")

r, j = call(sc, "GET", "/api/tasks?size=50")
tasks = (((j or {}).get("data") or {}).get("items") or []) if isinstance(j, dict) else []
done = [t for t in tasks if t.get("status") == "done"]
if done:
    tid = done[0]["id"]
    r, j = call(sc, "POST", f"/api/tasks/{tid}/finish", {})
    show(f"重复办结任务{tid}", r, j)
    if ok(r, j):
        rec("中", "C4", "已办结任务可重复办结", f"id={tid}")

# ---------------------------------------------------------------- D 公告流转
print(); print("=" * 78); print("D. 公告流转 / 置顶 / 不存在 id"); print("=" * 78)
r, j = call(wc, "GET", "/api/notices?status=published&size=50")
pub = (((j or {}).get("data") or {}).get("items") or []) if isinstance(j, dict) else []
print(f"  已发布 {len(pub)} 条")
if pub:
    nid = pub[0]["id"]
    r, j = call(wc, "POST", f"/api/notices/{nid}/publish", {})
    show(f"重复发布 id={nid}", r, j)
    if ok(r, j):
        rec("低", "D1", "已发布公告可重复发布且返回成功（无幂等提示）", f"id={nid}")
    r, j = call(wc, "POST", f"/api/notices/{nid}/offline", {})
    show(f"下架 id={nid}", r, j)
    r, j = call(wc, "POST", f"/api/notices/{nid}/offline", {})
    show("重复下架", r, j)
    if ok(r, j):
        rec("低", "D2", "已下架公告可重复下架且返回成功", f"id={nid}")

for url in ["/api/notices/999999", "/api/notices/999999/publish", "/api/notices/999999/pin"]:
    r, j = call(wc, "GET" if url.endswith("999999") else "POST", url, {})
    show(url.replace("/api/notices/", "公告 "), r, j)
    if ok(r, j):
        rec("高", "D3", "不存在的公告返回成功", url)
    if r.status_code >= 500:
        rec("高", "D4", "不存在的公告导致 5xx", url)

# ---------------------------------------------------------------- E 居民端
print(); print("=" * 78); print("E. 居民端提交 / 查询 / 限流"); print("=" * 78)
rc = app.test_client()
portal_cases = [
    ("全空", {}, False),
    ("缺内容", {"phone": "13800138000"}, False),
    ("缺手机号", {"text": "楼下垃圾桶满了"}, False),
    ("手机号 10 位", {"text": "路灯不亮", "phone": "1380013800"}, False),
    ("手机号含字母", {"text": "路灯不亮", "phone": "1380013800a"}, False),
    ("手机号非 1 开头", {"text": "路灯不亮", "phone": "20000000000"}, False),
    ("内容 4 字", {"text": "灯坏了", "phone": "13800138001"}, False),
    ("内容 3000 字", {"text": "灯" * 3000, "phone": "13800138002"}, False),
    ("正常", {"text": "3号楼2单元电梯有异响", "phone": "13800138003", "name": "张阿姨"}, True),
]
for name, body, expect_ok in portal_cases:
    r, j = call(rc, "POST", "/api/portal/report", body)
    show(name, r, j)
    if ok(r, j) and expect_ok is False:
        rec("中", "E1", f"居民提交「{name}」被接受", str(j.get("msg")))
    if not ok(r, j) and expect_ok is True:
        rec("高", "E2", f"居民提交「{name}」被拒绝", str(j.get("msg")))
    if r.status_code >= 500:
        rec("高", "E3", f"居民提交「{name}」5xx", f"HTTP {r.status_code}")

# 查询码
r, j = call(rc, "GET", "/api/portal/ticket?phone=13800138003&code=000000")
show("错误查询码", r, j)
if ok(r, j):
    rec("严重", "E4", "错误查询码居然查到单据（越权读取他人诉求）", "code=000000")
r, j = call(rc, "GET", "/api/portal/ticket?phone=&code=")
show("空手机号+空码", r, j)
if ok(r, j):
    rec("严重", "E5", "空手机号+空查询码可读取单据", "")
r, j = call(rc, "GET", "/api/portal/ticket/1")
show("按 id 直取他人单据", r, j)
if ok(r, j):
    rec("严重", "E6", "居民端按 id 可直接读他人单据（无归属校验）", "/api/portal/ticket/1")

# 限流
codes = []
for i in range(12):
    r, j = call(rc, "POST", "/api/portal/report", {"text": f"限流测试第{i}条", "phone": "13900139000"})
    codes.append((r.status_code, (j or {}).get("code") if isinstance(j, dict) else None))
blocked = sum(1 for c in codes if c[1] != 0)
print(f"  同号连打 12 次：被拒 {blocked} 次，状态码={sorted({c[0] for c in codes})}")
if blocked == 0:
    rec("中", "E7", "居民端同号连打 12 次无限流", str(codes[:3]))

# ---------------------------------------------------------------- F 分页/参数
print(); print("=" * 78); print("F. 分页与参数边界"); print("=" * 78)
for url in ["/api/orders?page=0", "/api/orders?page=-1", "/api/orders?page=99999",
            "/api/orders?size=0", "/api/orders?size=-5", "/api/orders?size=99999",
            "/api/orders?page=abc", "/api/orders?size=abc",
            "/api/notices?page=-1", "/api/notices?size=0"]:
    r, j = call(pc, "GET", url)
    d = ((j or {}).get("data") or {}) if isinstance(j, dict) else {}
    items = d.get("items") or []
    tot = d.get("total")
    print(f"  {url:26s} HTTP {r.status_code} code={(j or {}).get('code') if isinstance(j,dict) else '-'} "
          f"返回 {len(items)} 条 total={tot}")
    if r.status_code >= 500:
        rec("高", "F1", "分页参数越界 5xx", url)
    if len(items) > 200:
        rec("中", "F2", "size 未设上限，单次可拉取过多数据", f"{url} → {len(items)} 条")

# ---------------------------------------------------------------- G 权限
print(); print("=" * 78); print("G. 权限边界"); print("=" * 78)
probes = [
    ("物业", "wuye", "GET", "/api/kb/stats", None, 403),
    ("物业", "wuye", "GET", "/api/ledgers", None, 403),
    ("物业", "wuye", "GET", "/api/reports", None, 403),
    ("物业", "wuye", "POST", "/api/tasks", {"title": "越权下发"}, 403),
    ("街道", "jiedao", "POST", "/api/notices/1/localize", {}, 403),
    ("街道", "jiedao", "GET", "/api/ledgers", None, 403),
    ("社区", "wang", "GET", "/admin/users", None, 403),
    ("社区", "wang", "GET", "/api/admin/users", None, 403),
]
for role, acc, method, url, body, expect in probes:
    c = app.test_client(); login(c, acc)
    r, j = call(c, method, url, body)
    show(f"{role} {method} {url[:22]}", r, j)
    if r.status_code >= 500:
        rec("高", "G1", f"{role} 访问 {url} 5xx", f"HTTP {r.status_code}")
    elif r.status_code != expect:
        rec("严重", "G2", f"{role} 访问 {url} 状态异常", f"HTTP {r.status_code}（期望 {expect}）")

anon = app.test_client()
for url in ["/api/orders", "/api/tasks", "/api/ledgers", "/api/admin/users", "/api/fde/stats"]:
    r, j = call(anon, "GET", url)
    show(f"未登录 GET {url}", r, j)
    if r.status_code == 200 and ok(r, j):
        rec("严重", "G3", f"未登录可读 {url}", f"HTTP {r.status_code}")

# ---------------------------------------------------------------- H 登录安全
print(); print("=" * 78); print("H. CSRF / 爆破 / 口令强度 / 会话"); print("=" * 78)
c1 = app.test_client()
c1.get("/login")
before = c1.get_cookie("session")
login(c1, "wang")
after = c1.get_cookie("session")
if before and after and before.value == after.value:
    rec("高", "H4", "登录后 session 未重新生成（会话固定）", "Set-Cookie 值未变")
else:
    print("  会话：登录后 session 已更换 ✓")

sc2 = app.test_client(); login(sc2, "jiedao")
r, j = call(sc2, "POST", "/api/tasks", {"title": "无令牌下发", "assignee_role": "community", "deadline": "2026-12-31"})
show("无 CSRF 令牌下发", r, j)
if ok(r, j):
    rec("中", "H1", "写接口无 CSRF 防护，跨站表单可代发", "POST /api/tasks 无令牌仍成功")

bc = app.test_client()
res = [login(bc, "wang", f"wrong{i}") for i in range(20)]
print(f"  20 次错密码：状态码={sorted(set(res))}")
if 429 not in res:
    rec("中", "H2", "登录接口无限流，可离线字典爆破", f"状态码={sorted(set(res))}")

ac = app.test_client(); login(ac, "admin", "admin123")
r, j = call(ac, "POST", "/api/admin/users",
            {"username": "weaktest", "name": "弱口令", "role": "community",
             "org": "幸福里社区", "password": "1"})
show("建号口令=1", r, j)
if ok(r, j):
    rec("中", "H3", "可创建 1 位口令账号（无强度校验）", str(j.get("msg")))
r, j = call(ac, "POST", "/api/admin/users",
            {"username": "weaktest2", "name": "弱口令二", "role": "community",
             "org": "幸福里社区", "password": "123456"})
show("建号口令=123456", r, j)
if ok(r, j):
    rec("低", "H5", "可创建与演示账号相同的弱口令", str(j.get("msg")))

# ---------------------------------------------------------------- I 页面与错误页
print(); print("=" * 78); print("I. 页面级：不存在的资源 / 未知接口 / 405"); print("=" * 78)
for url in ["/notices/999999", "/orders/999999", "/reports/999999", "/kb/999999",
            "/api/nonexistent", "/api/orders/abc"]:
    r = wc.get(url)
    ct = (r.headers.get("Content-Type") or "").split(";")[0]
    print(f"  GET {url:24s} HTTP {r.status_code} {ct}")
    if r.status_code >= 500:
        rec("高", "I1", "不存在的资源导致 5xx", url)
r = wc.get("/api/tasks")
ct = (r.headers.get("Content-Type") or "").split(";")[0]
print(f"  GET /api/tasks (应为405或空列表)  HTTP {r.status_code} {ct}")

# ---------------------------------------------------------------- 汇总
print(); print("=" * 78)
print(f"共发现 {len(FINDINGS)} 个问题")
print("=" * 78)
order = {"严重": 0, "高": 1, "中": 2, "低": 3}
for sev, code, verdict, ev in sorted(FINDINGS, key=lambda x: (order.get(x[0], 9), x[1])):
    print(f"[{sev}] {code}  {verdict}")
    if ev:
        print(f"        {ev}")

out = BASE / "data" / "audit_probe_result.json"
out.write_text(json.dumps(
    [{"severity": s, "code": c, "verdict": v, "evidence": e} for s, c, v, e in FINDINGS],
    ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n结果已写入 {out}")
