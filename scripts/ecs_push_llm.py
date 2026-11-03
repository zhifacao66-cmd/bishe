"""把本机模型通道地址推到阿里云 ECS（只换 llm.json，绝不动 data\\）。

背景
----
线上服务器没有显卡（2C2G 装不下 qwen3:8b），推理要回到本机 GPU：
本机 Ollama（11434）→ llm_gateway.py（11435，校验 X-LLM-Token）→ cloudflared 隧道
→ 服务器 C:\\app\\llm.json 里的 base_url。

麻烦在于**隧道地址每次重建都会变**（quick tunnel 没有固定域名），而
`ecs_deploy_code.py` 的 KEEP_REMOTE 明确保护 `llm.json`——那是服务器侧的运行
环境，不该被代码更新顺带覆盖。于是换隧道之后，线上会静默指向一个已经死掉的
地址：业务不报错，只是全站降级成规则桩，状态灯变红。这个缺口就是本脚本。

用法
----
    python scripts/start_llm_channel.py        # 1) 本机起 Ollama + 网关 + 隧道
    python scripts/ecs_push_llm.py             # 2) 推到服务器并核验（本脚本）
    python scripts/ecs_push_llm.py --show      # 只看两端当前配置，不动
    python scripts/ecs_push_llm.py --rollback  # 回到上一次推送前的地址

设计约束（都踩过）
------------------
1. 链路丢包：22 端口一次连接约 50% 成功、连续快速重连会被拒。所以一次 bash 调用
   只建一个连接，远端多步合并成一个 .bat。这里全程只用 2 个连接（传文件 + 执行）。
2. 不验证就写等于把线上交给运气：地址错、隧道半通、网关没起，本地都看不出来。
   所以**先本机自探隧道**（/api/tags 真拿回模型列表），通了才推；不通就拒绝，
   宁可继续用旧地址。
3. .bat 必须 ASCII + CRLF（LF 会让 if/for 多行结构错解；中文 rem 会被当命令）。
4. 不用重启应用：app/llm/channel.py 按 mtime 缓存 llm.json，换文件即生效。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ecs_deploy_code import (  # noqa: E402  复用同一套 ssh/scp 重试逻辑
    BASE, KEY, ROOT, TMP, scp, ssh, write_ascii_crlf,
)

LOCAL_CFG = os.path.join(ROOT, "llm.publish.json")
TOKEN_FILE = os.path.join(ROOT, ".llm_gateway_token")
REMOTE_LLM = "C:/app/llm.json"
STAMP = time.strftime("%Y%m%d%H%M%S")

# 备份 → 换上 → 清理。纯 ASCII + CRLF，原因见 ecs_deploy_code.py 模块注释。
PUSH_BAT = r"""@echo off
set LOG=C:\app\_llm_push.log
echo ==== PUSH {stamp} >> %LOG% 2>&1
if exist C:\app\llm.json copy /y C:\app\llm.json C:\app\llm.json.bak-{stamp} >> %LOG% 2>&1
if exist C:\app\_llm_new.json copy /y C:\app\_llm_new.json C:\app\llm.json >> %LOG% 2>&1
if exist C:\app\_llm_new.json del /q C:\app\_llm_new.json >> %LOG% 2>&1
findstr /C:"base_url" C:\app\llm.json >> %LOG% 2>&1
echo ==== DONE >> %LOG% 2>&1
"""

ROLLBACK_BAT = r"""@echo off
set LOG=C:\app\_llm_push.log
echo ==== ROLLBACK {stamp} >> %LOG% 2>&1
if exist C:\app\llm.json.bak-{stamp} copy /y C:\app\llm.json.bak-{stamp} C:\app\llm.json >> %LOG% 2>&1
findstr /C:"base_url" C:\app\llm.json >> %LOG% 2>&1
echo ==== DONE >> %LOG% 2>&1
"""


def _open_no_proxy(url, headers=None, timeout=10):
    """显式无代理：本机 IDE 注入的代理到不了 trycloudflare / 内网 IP。"""
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return op.open(req, timeout=timeout)


def probe_tunnel(url, token, tries=3, timeout=15):
    """从本机打一次隧道的 /api/tags；返回 (ok, 说明)。

    这是推送到线上之前的唯一守卫：写进去的地址必须此刻真的能推理。
    DNS 刚分配时常有 getaddrinfo 失败（传播延迟），所以给几次机会。
    """
    last = ""
    for i in range(tries):
        try:
            with _open_no_proxy(url.rstrip("/") + "/api/tags",
                                {"X-LLM-Token": token}, timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            names = [m.get("name") for m in data.get("models") or [] if m.get("name")]
            if names:
                return True, "模型：" + ", ".join(names)
            last = "接口通了但模型列表为空（Ollama 没加载模型？）"
        except Exception as e:                              # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        if i + 1 < tries:
            time.sleep(3)
    return False, last


def remote_base_url():
    """只取远端 base_url 一行。用 findstr 而不是 type：type 经 ssh 直出会空/乱码。

    llm.json 里有中文 label，findstr 会把那一行输出成乱码，这里只挑 base_url 行，
    它是纯 ASCII 的 URL。
    """
    out = ssh(r'mkdir C:\app 2>nul & findstr /C:"base_url" '
              r'C:\app\llm.json 2>nul & echo.& findstr /C:"model" C:\app\llm.json 2>nul')
    return (out or "").strip() or "(读不到 C:\\app\\llm.json)"


def wait_available(timeout=90):
    """轮询服务器 /api/health，等 llm.available 变 true；返回 (ok, data)。"""
    t0 = time.time()
    d = None
    while time.time() - t0 < timeout:
        try:
            with _open_no_proxy(BASE + "/api/health", timeout=12) as r:
                d = json.loads(r.read().decode("utf-8")).get("data") or {}
            if (d.get("llm") or {}).get("available"):
                return True, d
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(3)
    return False, d


def show():
    print("本机 llm.publish.json：")
    if not os.path.exists(LOCAL_CFG):
        print("  （不存在：先跑 python scripts/start_llm_channel.py）")
    else:
        d = json.load(open(LOCAL_CFG, encoding="utf-8"))
        print(f"  base_url : {d.get('base_url')}")
        print(f"  model    : {d.get('model')}")
        print(f"  token    : {'已配置 ' + str(len(d.get('token') or '')) + ' 字符' if d.get('token') else '缺失'}")
        tok = ""
        if os.path.exists(TOKEN_FILE):
            tok = open(TOKEN_FILE, encoding="utf-8").read().strip()
        print(f"  与网关令牌一致 : {'是' if tok and tok == d.get('token') else '否（通道必不通）'}")
    print("\n服务器 C:\\app\\llm.json：")
    try:
        print("  " + remote_base_url().replace("\r\n", "\n  "))
    except Exception as e:                                  # noqa: BLE001
        print(f"  读取失败：{type(e).__name__}: {e}")
        print("  （链路丢包常见，重跑一次通常就好）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="只看两端配置，不动服务器")
    ap.add_argument("--rollback", action="store_true", help="回到上一次推送前的地址")
    ap.add_argument("--stamp", default="", help="回滚用的备份时间戳（llm.json.bak-<stamp>）")
    ap.add_argument("--skip-probe", action="store_true", help="跳过本机隧道自探（不推荐）")
    args = ap.parse_args()

    if not os.path.exists(KEY):
        print(f"密钥不存在：{KEY}")
        return 2

    if args.show:
        show()
        return 0

    if args.rollback:
        if not args.stamp:
            print("--rollback 需要 --stamp（C:\\app\\llm.json.bak-<stamp> 的时间戳）")
            print("备份在每次成功推送时生成，时间戳见推送输出")
            return 2
        os.makedirs(TMP, exist_ok=True)
        bat = write_ascii_crlf(os.path.join(TMP, "_llm_rollback.bat"),
                               ROLLBACK_BAT.format(stamp=args.stamp))
        scp(bat, "C:/app/_llm_rollback.bat")
        print(ssh(r"call C:\app\_llm_rollback.bat").strip())
        ok, d = wait_available(60)
        print("[ecs] 回滚后 llm.available =", (d or {}).get("llm", {}).get("available"))
        return 0 if ok else 1

    # ---------- 推送 ----------
    if not os.path.exists(LOCAL_CFG):
        print(f"[ecs] 没有 {os.path.basename(LOCAL_CFG)}")
        print("      先跑：python scripts/start_llm_channel.py（它会在隧道建成后写这个文件）")
        return 2
    cfg = json.load(open(LOCAL_CFG, encoding="utf-8"))
    url, token = (cfg.get("base_url") or "").rstrip("/"), cfg.get("token") or ""
    if not url:
        print("[ecs] llm.publish.json 里没有 base_url")
        return 2

    print(f"[ecs] 1/4 目标地址 {url}")
    if not args.skip_probe:
        print("[ecs]       本机自探隧道（不通就不推，宁可继续用旧地址）…")
        ok, detail = probe_tunnel(url, token)
        if not ok:
            print(f"[ecs]       未通过：{detail}")
            print("              排查：Ollama 在跑？网关在跑？start_llm_channel.py 那个窗口还开着？")
            print("              确认无误可重跑；确实想强推加 --skip-probe（不推荐）")
            return 1
        print(f"[ecs]       通过，{detail}")

    os.makedirs(TMP, exist_ok=True)
    print("[ecs] 2/4 上传新配置（连接 1/2）")
    # 先落到临时名，再由 .bat 原子替换：避免 scp 半包把 llm.json 写成半个 JSON
    scp(LOCAL_CFG, "C:/app/_llm_new.json")

    print("[ecs] 3/4 备份旧配置 → 换上新配置（连接 2/2）")
    bat = write_ascii_crlf(os.path.join(TMP, "_llm_push.bat"),
                           PUSH_BAT.format(stamp=STAMP))
    scp(bat, "C:/app/_llm_push.bat")
    ssh(r"call C:\app\_llm_push.bat")

    print("[ecs] 4/4 等服务器把通道点亮（channel.py 按 mtime 缓存，不用重启）")
    ok, d = wait_available(90)
    if ok:
        print("      llm.available = True，status =", d.get("status"))
        print(f"[ecs] 完成。旧地址备份在 C:\\app\\llm.json.bak-{STAMP}")
        print(f"      回滚：python scripts/ecs_push_llm.py --rollback --stamp {STAMP}")
        return 0

    print("      仍是降级。三种常见原因，按顺序查：")
    print("        1) 令牌不一致：本机 .llm_gateway_token ≠ 服务器 llm.json 的 token")
    print("        2) 隧道未通：浏览器开 base_url/api/tags 应当报 401 而不是 530")
    print("        3) Ollama 没加载模型：/api/tags 返回空列表")
    print(f"      回滚到旧地址：python scripts/ecs_push_llm.py --rollback --stamp {STAMP}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
