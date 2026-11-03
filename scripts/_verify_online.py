"""线上发布后核验（只读）：登录 + 逻辑修复实测。

要点：
- 该域名本机**直连**可达，走系统代理反而 502
- 本机 DNS 间歇超时（nslookup 一半失败），解析一次固定 IP，TLS 仍按域名校验
- 任务接口返回 data.rows / data.summary（不是 items/total）
"""
import json
import socket
import time

import requests
import urllib3.util.connection as u3c

HOST = "community-agent.app.workbuddy.host"
BASE = "https://" + HOST
try:
    IP = socket.getaddrinfo(HOST, 443, socket.AF_INET)[0][4][0]
except OSError:
    IP = "49.233.240.214"
_o = u3c.create_connection
u3c.create_connection = lambda a, *x, **k: _o(
    (IP, a[1]) if a[0] == HOST else a, *x, **k)


def say(s=""):
    print(s, flush=True)


def mk():
    s = requests.Session()
    s.trust_env = False
    return s


def call(s, m, p, tries=3, **kw):
    kw.setdefault("timeout", 80)
    for i in range(tries):
        try:
            return s.request(m, BASE + p, **kw)
        except Exception as e:                             # noqa: BLE001
            say(f"   [retry {i + 1}] {type(e).__name__}")
            time.sleep(1.5 * (i + 1))
    raise RuntimeError("unreachable")


def login(user, pwd):
    s = mk()
    call(s, "GET", "/?sharecode=XMM2LKNQOR")
    r = call(s, "POST", "/login", data={"username": user, "password": pwd},
             allow_redirects=False)
    return s, r.status_code


say("== 1. 状态灯 ==")
admin, code = login("admin", "admin123")
say(f"   login admin -> {code}")
d = (call(admin, "GET", "/api/health/llm").json() or {}).get("data") or {}
say(f"   state={d.get('state')} ms={d.get('ms')} channel={d.get('channel')} "
    f"model={d.get('model')} age={d.get('state_age_s')}s")
say(f"   title={d.get('title')} / {d.get('sub')}")
say(f"   -> {'真实探活为绿' if d.get('state') == 'ok' else '非绿：' + str(d.get('state'))}")

say("\n== 2. 任务卡片与列表同源（limit 生效但统计不缩水） ==")
dd = (call(admin, "GET", "/api/tasks").json() or {}).get("data") or {}
rows = dd.get("rows") or []
sm = dd.get("summary") or {}
say(f"   rows={len(rows)} summary={json.dumps(sm, ensure_ascii=False)}")
say(f"   -> {'同源 OK' if sm.get('total') == sm.get('open', 0) + sm.get('done', 0) else '不一致!!'}")

say("\n== 3. 公告计数（todo 用 todo_filter 叠加） ==")
nd = (call(admin, "GET", "/api/notices").json() or {}).get("data") or {}
say(f"   counts={nd.get('counts')} rows={len(nd.get('items') or nd.get('rows') or [])}")

say("\n== 4. 数据隔离：只看，不写（写侧隔离在 smoke_logic[B] 用独立库验） ==")
w, wcode = login("wang", "123456")
say(f"   login wang(社区) -> {wcode}")
wr = ((call(w, "GET", "/api/tasks").json() or {}).get("data") or {}).get("rows") or []
say(f"   wang 可见 {len(wr)} 条 / admin 可见 {len(rows)} 条")
wid = {r.get("id") for r in wr}
aid = {r.get("id") for r in rows}
say(f"   wang 是 admin 的子集：{wid <= aid}")
say(f"   wang 全是 community 角色：{all(r.get('assignee_role') == 'community' for r in wr)}")
say("   （本脚本不调用任何写接口：曾用 POST /done 验越权，"
    "把演示任务误标成已完成）")

say("\n== 5. 居民端：filed 算办结（纯字段口径，读 /api/portal/my 需双因子，改看接口存在性） ==")
r = call(admin, "GET", "/api/portal/roles")
say(f"   GET /api/portal/roles -> {r.status_code} "
    f"{(r.text or '')[:80]}")

say("\n== 6. 移动端 CSS ==")
css = call(admin, "GET", "/static/css/style.css").text or ""
say(f"   len={len(css)}")
for k in ("@media (max-width:900px)", "@media (max-width:640px)", ".navtoggle",
          "side-mask", "max-width:170px", "overflow-x:auto"):
    say(f"   {'有' if k in css else '缺'} {k}")

say("\n== 7. 首页渲染（含状态灯容器） ==")
h = call(admin, "GET", "/").text or ""
for k in ('id="llmBadge"', 'id="navToggle"', 'id="sideMask"', "viewport-fit=cover"):
    say(f"   {'有' if k in h else '缺'} {k}")
say("\nDONE")
