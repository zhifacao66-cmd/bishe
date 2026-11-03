"""把默认口令换掉 —— 先预演，确认后才改，改完立刻验证。

为什么要单独一个脚本
------------------
`admin/admin123` 在演示期是方便，但一旦这个地址能被外网访问，它就是把后台
连同审计、导出、账号治理一起交出去。答辩/给老师看的时候留着，看完就该换。
手动改有三处容易出错：忘了改哪台、改完没验证、新密码没记下来。

这个脚本把三件事固定住：
  1. 默认只**预演**（dry-run），打印哪台机器、哪个账号还能用默认口令登录
  2. `--apply` 才真正改，且改完立刻用新口令登录一次做验证
  3. 新口令写到 `data/_new_password.txt`（已被 .gitignore 忽略），同时打印到屏

用法
----
    python scripts/rotate_password.py                      # 预演：只探测，不改
    python scripts/rotate_password.py --target ecs         # 预演线上的
    python scripts/rotate_password.py --target ecs --apply # 真改（会要求确认）
    python scripts/rotate_password.py --apply --new 'MyPw#2026x' --yes
    python scripts/rotate_password.py --apply --user community --old community123

安全约束
--------
  · 新口令至少 8 位（与后端 `reset_password` 的校验一致，少了会被 400 拒绝）
  · 只走应用自己的改密接口，不直接改库 —— 改库会绕过审计与哈希逻辑
  · 失败不动原密码：改密前先确认"旧口令能登录"，否则宁可不改
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "_new_password.txt"

TARGETS = {
    "local": "http://127.0.0.1:5000",
    "ecs": "http://47.108.235.113",
}


class Client:
    """一个带 Cookie 的最小客户端。绕过系统代理 —— 本机/ECS 走代理会 502。"""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.cj),
        )

    def _req(self, path: str, data: dict | None = None, timeout: float = 25):
        if data is None:
            req = urllib.request.Request(self.base + path)
        else:
            body = json.dumps(data).encode()
            req = urllib.request.Request(self.base + path, data=body,
                                         headers={"Content-Type": "application/json"})
        with self.op.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")

    def login(self, user: str, pw: str) -> bool:
        self.cj.clear()
        body = urllib.parse.urlencode({"username": user, "password": pw}).encode()
        req = urllib.request.Request(self.base + "/login", data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self.op.open(req, timeout=25) as r:
                r.read()
        except urllib.error.HTTPError:
            return False
        return any(c.name == "session" for c in self.cj)

    def users(self) -> list[dict]:
        _, d = self._req("/api/admin/users")
        data = d.get("data")
        if isinstance(data, list):
            return data
        return (data or {}).get("items") or []

    def reset_pw(self, uid: int, pw: str):
        return self._req("/api/admin/users/%d/password" % uid, {"password": pw})


def probe(base: str, user: str, old: str) -> tuple[bool, str]:
    c = Client(base)
    try:
        ok = c.login(user, old)
    except Exception as e:                                        # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:60]}"
    return ok, "可登录" if ok else "已失效"


def apply_one(base: str, user: str, old: str, new: str, yes: bool) -> int:
    c = Client(base)
    if not c.login(user, old):
        print("[中止] %s 用旧口令登录失败，不做任何修改" % user)
        return 1
    uid = None
    for u in c.users():
        if u.get("username") == user:
            uid = u.get("id")
            break
    if uid is None:
        print("[中止] 账号列表里找不到 %s" % user)
        return 1

    if not yes:
        print("\n即将把 %s（uid=%d）在 %s 的口令重置为：%s" % (user, uid, base, new))
        print("改完之后旧口令立即失效，正在用它登录的页面会掉线。")
        if input("确认执行？输入 yes 继续：").strip().lower() != "yes":
            print("[取消] 未做任何修改")
            return 1

    st, d = c.reset_pw(uid, new)
    if st != 200 or d.get("code") not in (0, None):
        print("[失败] 改密接口返回 %s：%s" % (st, d.get("msg")))
        return 1

    # 立刻验证：改完不能登等于把人锁在门外
    c2 = Client(base)
    if not c2.login(user, new):
        print("[失败] 新口令登录验证不通过！原口令已失效，请立刻检查。")
        return 1
    if c2.login(user, old):
        print("[警告] 旧口令仍可登录，说明重置没真正生效")
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("%s\t%s\t%s\n" % (base, user, new), encoding="utf-8")
    print("[完成] %s 的 %s 已重置，新口令已验证可登录" % (base, user))
    print("       新口令已存 %s（该文件已被 .gitignore 忽略）" % OUT.relative_to(ROOT))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="把默认口令换掉（默认只预演）")
    ap.add_argument("--target", default="local", help="local / ecs / 任意 base URL")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--old", default="admin123", help="当前口令（默认 admin123）")
    ap.add_argument("--new", default="", help="新口令；留空则生成 16 位随机")
    ap.add_argument("--apply", action="store_true", help="真正执行修改")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认")
    a = ap.parse_args()

    base = TARGETS.get(a.target, a.target.rstrip("/"))
    print("目标 %s ｜ 账号 %s" % (base, a.user))

    ok, msg = probe(base, a.user, a.old)
    print("  当前口令：%s（%s）" % (a.old, msg))

    if not ok:
        print("\n旧口令不可用，无需处理。若忘记口令，用管理员在「账号治理」页重置。")
        return 0

    if not a.apply:
        print("\n[预演] 仅探测，未做修改。")
        print("       要真的换掉，加 --apply；新口令留空会自动生成 16 位随机串。")
        return 0

    new = a.new.strip() or (secrets.token_urlsafe(12) + "#9")
    if len(new) < 8:
        print("[中止] 新口令至少 8 位（后端硬性校验）")
        return 1
    return apply_one(base, a.user, a.old, new, a.yes)


if __name__ == "__main__":
    sys.exit(main())
