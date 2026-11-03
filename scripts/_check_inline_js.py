"""抓页面里的内联 <script> 做语法检查（node --check），防止改坏前端不报错。

要点：**按角色分组登录**。之前只用社区账号抓页面，`/audit`（街道）和
`/system`（管理员）根本没被抓过——那两页的 JS 写错了也照样"0 处语法错误"，
这种"通过"比不检查更误导。
"""
import http.cookiejar
import json
import re
import subprocess
import sys
import tempfile
import urllib.request

NODE = r"C:\Users\10517\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"
BASE = "http://127.0.0.1:5000"

# (账号, 密码, 页面列表)
GROUPS = [
    ("wang", "123456", ["/", "/tasks", "/orders", "/ledgers", "/notices",
                        "/reports", "/kb"]),
    ("jiedao", "123456", ["/audit"]),
    ("admin", "admin123", ["/system"]),
]


def open_as(user, pwd):
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),                  # 绕开系统代理，直连
        urllib.request.HTTPCookieProcessor(jar))
    op.open(BASE + "/login",
            data=f"username={user}&password={pwd}".encode(), timeout=10)
    return op


checked = bad = 0
for user, pwd, pages in GROUPS:
    op = open_as(user, pwd)
    todo = list(pages)
    if user == "wang":
        # 公告详情页与办文页也有内联脚本，用库里第一条公告现取
        try:
            raw = op.open(BASE + "/api/notices", timeout=10).read()
            items = (json.loads(raw).get("data") or {}).get("items") or []
            if items:
                nid = items[0]["id"]
                todo += [f"/notices/{nid}", f"/notices/{nid}/localize"]
        except (urllib.error.URLError, ValueError, KeyError):
            pass
    for p in todo:
        try:
            html = op.open(BASE + p, timeout=15).read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            print(f"[SKIP] {user} {p} → HTTP {e.code}")
            continue
        checked += 1
        blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
        for i, code in enumerate(blocks):
            if not code.strip():
                continue
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                             encoding="utf-8") as f:
                f.write(code)
                path = f.name
            r = subprocess.run([NODE, "--check", path], capture_output=True, text=True)
            if r.returncode != 0:
                bad += 1
                print(f"[FAIL] {user} {p} script#{i}")
                print(r.stderr.strip()[:600])

print(f"\n检查完成：{checked} 页，{bad} 处语法错误")
sys.exit(1 if bad else 0)
