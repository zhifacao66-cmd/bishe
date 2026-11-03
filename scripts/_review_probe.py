"""整体审查用：逐角色走一遍页面与关键接口，按**权限表**判断结果对不对。

只读，绝不发写请求。

和早先版本的区别：老版本把所有非 200 都算"异常"，于是"物业打不开 /ledgers"
（本来就不该打开）和"/ledgers 真 500"混在同一张清单里，噪声盖掉了信号——
一份每行都写"异常"的报告，没人会认真看。现在从 `auth.ROLE_PERMS` 反推
每个角色**应该**看到什么：

    · 有权限 → 必须 200（否则是真故障）
    · 无权限 → 必须 403（返回 200 就是越权，比 500 严重）
    · 未登录 → API 必须 401

用法：
    python scripts/_review_probe.py                                    # 本机 5000
    python scripts/_review_probe.py --base http://47.108.235.113       # ECS
    python scripts/_review_probe.py --base https://xxx --sharecode ABC # 沙箱
"""
import argparse
import sys

import requests

sys.path.insert(0, ".")
from app.auth import ROLE_PERMS

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://127.0.0.1:5000")
ap.add_argument("--sharecode", default="")
ap.add_argument("--timeout", type=float, default=25)
args = ap.parse_args()

BASE = args.base.rstrip("/")


def url(path):
    """沙箱每个请求都要带 sharecode，缺了平台网关回的是"链接不完整"HTML。"""
    if not args.sharecode:
        return BASE + path
    sep = "&" if "?" in path else "?"
    return BASE + path + sep + "sharecode=" + args.sharecode


ACCOUNTS = [
    ("admin", "admin123", "管理员", "admin"),
    ("wang", "123456", "社区", "community"),
    ("wuye", "123456", "物业", "property"),
    ("jiedao", "123456", "街道", "street"),
]

PAGES = [
    ("/", "fde.create", "无首录权者 302 回各自主页"),
    ("/tasks", "task.view", ""),
    ("/orders", "order.view", ""),
    ("/ledgers", "ledger.view", ""),
    ("/reports", "report.view", ""),
    ("/audit", "audit.view", ""),
    ("/kb", "kb.view", ""),
    ("/notices", "notice.view", ""),
    ("/system", "user.manage", ""),
    ("/admin/users", "user.manage", ""),
    ("/health", None, "登录即可"),
]

APIS = [
    ("/api/fde/stats", "fde.view", "any"),
    ("/api/fde/dashboard", "fde.view", "any"),
    ("/api/tasks", "task.view", "rows"),
    ("/api/orders?limit=5", "order.view", "items"),
    # 转派候选人只服务于"转派"动作，所以挂的是 order.handle（仅物业）；
    # 前端也只在 CAN_HANDLE 时才渲染转派按钮。早先这里按 order.view 断言，
    # 把"设计如此"报成了"社区/街道应有权限却 403"。
    ("/api/orders/handlers", "order.handle", "any"),
    ("/api/supervision/alerts", "order.view", "any"),
    ("/api/ledgers?limit=5", "ledger.view", "items"),
    ("/api/reports", "report.view", "items"),
    ("/api/audit?limit=5", "audit.view", "items"),
    ("/api/kb/stats", "kb.view", "any"),
    ("/api/kb/docs", "kb.view", "items"),
    ("/api/notices", "notice.view", "items"),
    ("/api/health", None, "any"),
    ("/api/health/llm", None, "any"),
    ("/api/admin/users", "user.manage", "items"),
    ("/api/admin/me", None, "any"),
]

# 已知契约偏差（docs/24 分级里的"裸数组"），单列一栏，不混进异常
KNOWN_BARE = {"/api/reports", "/api/admin/users", "/api/kb/docs"}

okn = 0
bad, crash, drift, slow = [], [], [], []


def req(sess, method, path, **kw):
    return sess.request(method, url(path), timeout=args.timeout,
                        allow_redirects=False, **kw)


for user, pwd, label, role in ACCOUNTS:
    sess = requests.Session()
    sess.trust_env = False                 # 本机代理会把 127.0.0.1 也劫走
    r = req(sess, "POST", "/login", data={"username": user, "password": pwd})
    if r.status_code not in (301, 302):
        bad.append(f"{label} 登录失败 HTTP {r.status_code}")
        continue
    perms = ROLE_PERMS.get(role, set())

    for path, need, _extra in PAGES:
        r = req(sess, "GET", path)
        allowed = (need is None) or (need in perms)
        if path == "/" and not allowed:
            # 首页对无首录权的人是 302 回各自工作页，不是 403
            if r.status_code in (301, 302):
                okn += 1
            else:
                bad.append(f"{label} 页面 / 无 fde.create 时应 302，实际 {r.status_code}")
            continue
        if allowed:
            if r.status_code == 200:
                okn += 1
            else:
                bad.append(f"{label} 页面 {path} 应有 {need or '登录'} 却 {r.status_code}")
                if r.status_code >= 500:
                    crash.append(f"{label} 页面 {path} → {r.status_code}")
        else:
            if r.status_code == 200:
                bad.append(f"{label} 页面 {path} 无 {need} 权限却 200（越权）")
            elif r.status_code in (301, 302):
                bad.append(f"{label} 页面 {path} 无权限时应 403，实际跳转 {r.status_code}")
            else:
                okn += 1

    for path, need, contract in APIS:
        allowed = (need is None) or (need in perms)
        r = req(sess, "GET", path)
        if r.elapsed.total_seconds() > 3:
            slow.append(f"{label} {path} {r.elapsed.total_seconds():.1f}s")
        if not allowed:
            if r.status_code == 200:
                bad.append(f"{label} 接口 {path} 无 {need} 权限却 200（越权）")
            elif r.status_code == 403:
                okn += 1
            else:
                bad.append(f"{label} 接口 {path} 无权限时应 403，实际 {r.status_code}")
            continue
        if r.status_code != 200:
            bad.append(f"{label} 接口 {path} 应有权限却 {r.status_code}")
            if r.status_code >= 500:
                crash.append(f"{label} 接口 {path} → {r.status_code}")
            continue
        try:
            j = r.json()
        except ValueError:
            bad.append(f"{label} 接口 {path} 返回的不是 JSON")
            continue
        if not (isinstance(j, dict) and "code" in j):
            drift.append(f"{label} 接口 {path} 没有 code/msg/data 外壳")
            continue
        if j.get("code") != 0:
            bad.append(f"{label} 接口 {path} code={j.get('code')} msg={j.get('msg')}")
            continue
        if contract == "items":
            d = j.get("data")
            if isinstance(d, list) and path not in KNOWN_BARE:
                drift.append(f"{label} 接口 {path} 返回裸数组（约定 items+counts）")
            elif isinstance(d, dict) and "items" not in d:
                drift.append(f"{label} 接口 {path} data 缺 items")
        elif contract == "rows":
            # 任务看板历史命名是 {rows, summary}，前端按 rows 用，能用但不是全站约定
            d = j.get("data") or {}
            if not isinstance(d, dict) or "rows" not in d or "summary" not in d:
                drift.append(f"{label} 接口 {path} 应为 {{rows, summary}}，实际 {type(d).__name__}")
        okn += 1

# 未登录：内部接口必须 401，居民端必须 200
anon = requests.Session()
anon.trust_env = False
for path in ("/api/tasks", "/api/orders", "/api/fde/dashboard", "/api/fde/stats"):
    r = req(anon, "GET", path)
    if r.status_code == 401:
        okn += 1
    else:
        bad.append(f"未登录 {path} 应 401，实际 {r.status_code}（会泄露数据）")
r = req(anon, "GET", "/portal")
if r.status_code == 200:
    okn += 1
else:
    bad.append(f"居民端 /portal 应免登录 200，实际 {r.status_code}")


def dump(title, lst):
    if not lst:
        return
    print(f"\n[{title}] {len(lst)} 条")
    for x in sorted(set(lst)):
        print("  ·", x)


print("=" * 70)
print(f"目标 {BASE}{'（带 sharecode）' if args.sharecode else ''}")
print(f"符合预期 {okn} 项 ｜ 异常 {len(bad)} 项（5xx {len(crash)}）"
      f" ｜ 契约偏差 {len(drift)} ｜ 慢接口 {len(slow)}")
dump("异常", bad)
dump("契约偏差", drift)
dump("慢接口（>3s）", slow)
print("\n" + ("全部符合权限表与契约 ✅" if not bad else "存在异常，见上 ⚠️"))
sys.exit(1 if bad else 0)
