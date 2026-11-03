"""一键起「线上模型通道」：本机网关 + Cloudflare 隧道。

背景
----
线上部署在没有显卡的容器里，推理要回到本机 GPU。这需要两件事同时在线：
  1. scripts/llm_gateway.py —— 校验令牌后转发给 Ollama（不能让 11434 裸奔上公网）
  2. cloudflared —— 把网关映射成一个公网 https 地址

麻烦在于隧道地址**每次重建都会变**，变了就得重新发布。这个脚本把整套流程
压成一条命令，并把新地址自动写进 llm.publish.json。

用法
----
    python scripts/start_llm_channel.py          # 起通道并写入配置
    python scripts/start_llm_channel.py --show   # 只看当前配置，不起进程
    python scripts/start_llm_channel.py --push-ecs   # 起通道 + 自动推到阿里云 ECS

线上现在是阿里云 ECS（http://47.108.235.113），它没有显卡，推理靠这条通道回本机。
换隧道后必须把新地址推给服务器，否则线上会静默指向死地址：接口全 200、业务不报错，
只是拆解退回规则桩、状态灯变红。`--push-ecs` 就是把 start_llm_channel 和
ecs_push_llm 合成一条命令，日常只需要跑「启动模型服务-ECS.bat」。

推送失败不会影响通道本身：脚本会打印失败原因并继续常驻，网络恢复后单独重跑
    python scripts/ecs_push_llm.py
即可，不必重启通道。

关闭窗口或 Ctrl+C 时，两个子进程会一起退出，线上随即降级为规则引擎
（业务不会报错，只是拆解质量下降），这与论文里的消融基线一致。
"""
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLOUDFLARED = os.path.join(ROOT, "scripts", "bin", "cloudflared.exe")
TOKEN_FILE = os.path.join(ROOT, ".llm_gateway_token")
PUB_CFG = os.path.join(ROOT, "llm.publish.json")
GW_PORT = 11435
METRICS_PORT = 20242          # cloudflared 自带的 metrics 口，/quicktunnel 会回传本次域名
LOG_FILE = os.path.join(ROOT, "logs", "cloudflared.log")

# quick tunnel 分到的地址形如 https://eds-curve-expected-msg.trycloudflare.com
# ——子域名一定是**多个词用短横线连起来**。这条"至少一个短横线"不是美学要求：
# cloudflared 失败时会打印 `Post "https://api.trycloudflare.com/tunnel": ...`，
# 而 api.trycloudflare.com 也满足"xxx.trycloudflare.com"。早先的正则
# `https://[a-z0-9-]+\.trycloudflare\.com` 就把这行**失败信息**当成了成功地址，
# 于是 llm.publish.json 被写成 API 域名、脚本以为大功告成、线上全站降级——
# 一次静默的配置污染。现在要求子域名必须带短横线，并显式排除 api。
URL_RE = re.compile(r"https://([a-z0-9-]+)\.trycloudflare\.com")


def find_tunnel_url(line):
    """从一行 cloudflared 输出里取隧道地址；取不到返回 None。"""
    for m in URL_RE.finditer(line or ""):
        sub = m.group(1)
        if sub == "api" or "-" not in sub:
            continue                     # API 端点 / 占位地址，不是隧道
        return m.group(0)
    return None


def child_env():
    """起 cloudflared / 网关用的环境：去掉代理变量。

    本机 shell 里通常带着 `HTTPS_PROXY=127.0.0.1:xxxx`（IDE/沙箱注入的代理）。
    cloudflared 是 Go 程序，会照着这个变量去申请隧道，于是
    `api.trycloudflare.com` 经代理一律超时、quick tunnel 永远建不起来。
    实测：带代理 000 / 10.5s 超时，直连 200 / 4.1s。
    所以这两个子进程必须直连——它们要访问的是本机端口和 Cloudflare，
    本来就不该经过本地代理。
    """
    drop = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    return {k: v for k, v in os.environ.items() if k.lower() not in drop}


def wait_tunnel_url(cf, log_path=LOG_FILE, timeout=90):
    """等隧道域名，返回 (url, 失败原因)。

    为什么问 metrics 而不是解析 stdout：
    1. cloudflared 的 stdout 是**块缓冲**的管道，域名那一行可能几十秒才读得到；
    2. 更要命的是它的"失败信息"里也带一个 trycloudflare 域名
       （`api.trycloudflare.com`），早先按行正则一抓就把失败当成成功；
    3. `/quicktunnel` 是它自己维护的权威值，只有真建成才会有 hostname。
    stdout 仍然写进 logs/cloudflared.log，失败时从尾巴上取原因。
    """
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    probe = f"http://127.0.0.1:{METRICS_PORT}/quicktunnel"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with opener.open(probe, timeout=2) as r:
                host = (json.loads(r.read().decode("utf-8")) or {}).get("hostname")
            if host:
                return "https://" + host, ""
        except Exception:                                   # noqa: BLE001
            pass                                            # 还没起来，正常
        if cf.poll() is not None:
            break
        time.sleep(1)

    reason = "超时"
    try:
        tail = open(log_path, encoding="utf-8", errors="replace").read().splitlines()[-8:]
        for line in reversed(tail):
            if "failed" in line.lower() or " err" in line.lower():
                reason = line.strip()[:140]
                break
        else:
            if tail:
                reason = tail[-1].strip()[:140]
    except Exception:                                       # noqa: BLE001
        pass
    return None, reason


def probe_channel(url, token, tries=14, timeout=15):
    """拿到地址先自己打一次 `/api/tags`。

    不验证就写配置，等于把线上交给运气：地址错了、隧道半通、网关没起，
    三种情况本地都看不出来，而线上的表现是**全站降级**——业务不报错，
    只是拆解质量变差，很难第一时间归因。
    返回 (是否通过, 模型名列表 或 失败原因)。

    为什么要等这么久（14 次 × 4 秒）：quick tunnel 的域名是刚申请的，
    Cloudflare 那边**DNS 记录要十几秒才生效**，第一次请求常常直接
    `getaddrinfo failed`（本机还查不到这个名字），这属于正常传播延迟，
    不是配置错误。实测：域名分配后立刻 nslookup 是空的，几十秒后才有 A/AAAA。
    只试两三次就判定失败，会把一条好通道误杀在传播窗口里。
    """
    import urllib.request

    # 显式无代理：urllib 默认会读 HTTPS_PROXY，而本机那个代理到不了 trycloudflare
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    target = url.rstrip("/") + "/api/tags"
    last = ""
    for i in range(tries):
        try:
            req = urllib.request.Request(target)
            if token:
                req.add_header("X-LLM-Token", token)
            with opener.open(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            names = [m.get("name") for m in data.get("models") or [] if m.get("name")]
            if names:
                return True, names
            last = "接口通了但模型列表为空（Ollama 没加载模型？）"
        except Exception as e:                              # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        if i + 1 < tries:
            time.sleep(4)
    return False, last


def show():
    if not os.path.exists(PUB_CFG):
        print("尚未配置线上通道（llm.publish.json 不存在）")
        return
    d = json.load(open(PUB_CFG, encoding="utf-8"))
    print(f"通道：{d.get('label')}")
    print(f"地址：{d.get('base_url')}")
    print(f"模型：{d.get('model')}")
    print(f"令牌：{'已配置' if d.get('token') else '缺失'}")


def main():
    if "--show" in sys.argv:
        show()
        return 0

    # 起通道后顺带把新地址推给阿里云 ECS（换隧道不推 = 线上静默降级）
    push_ecs = "--push-ecs" in sys.argv

    if not os.path.exists(CLOUDFLARED):
        print(f"缺少 {CLOUDFLARED}")
        print("下载：https://github.com/cloudflare/cloudflared/releases/latest/download/"
              "cloudflared-windows-amd64.exe")
        return 1

    # 1. Ollama 必须先在跑——否则通道通了也是空转
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as r:
            models = [m["name"] for m in json.loads(r.read()).get("models", [])]
        print(f"[1/4] Ollama 在线，模型：{', '.join(models) or '（空）'}")
    except Exception as e:                                  # noqa: BLE001
        print(f"[1/4] Ollama 未启动（{type(e).__name__}）——先运行 scripts/start_ollama.bat")
        return 1

    py = sys.executable
    env = child_env()
    gw = subprocess.Popen([py, os.path.join(ROOT, "scripts", "llm_gateway.py"),
                           "--port", str(GW_PORT)],
                          cwd=ROOT, env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    print(f"[2/4] 模型网关已启动（pid {gw.pid}，监听 127.0.0.1:{GW_PORT}）")

    print("[3/4] 正在申请隧道地址…")
    time.sleep(1.0)                      # 给网关一点时间进入监听状态
    cf, url = None, None
    try:
        # 为什么要重试：quick tunnel 申请是**一次性的 HTTPS 请求**，
        # cloudflared 给这个请求内置了 10 秒超时，失败就整个退出、不重试。
        # 而本机到 api.trycloudflare.com 的 TLS 握手实测在 0.6~19 秒之间抖动
        # （同一条命令连打三次：0.6s / 9.0s / 15.6s），握手慢的那一次必然失败。
        # 这不是网络坏了，是抖动撞上固定超时——重跑就能过，那就让它自己重跑。
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        for attempt in range(1, 6):
            log = open(LOG_FILE, "a", encoding="utf-8")
            log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} attempt {attempt} =====\n")
            log.flush()
            cf = subprocess.Popen(
                [CLOUDFLARED, "tunnel", "--url", f"http://127.0.0.1:{GW_PORT}",
                 "--no-autoupdate",
                 # 必须走 http2：默认的 QUIC 走 UDP 7844，本机实测连上了边缘
                 # （netstat 看得见 ESTABLISHED）但 Cloudflare 侧始终不回
                 # "Tunnel has been created"，访问域名只得到
                 # "Cloudflare Tunnel error" 页——即路由没注册成功。
                 # 换成 http2（TCP 443）后一次就通。
                 "--protocol", "http2",
                 # 显式指定 metrics 口，用来读本次分到的域名（见 wait_tunnel_url）
                 "--metrics", f"127.0.0.1:{METRICS_PORT}"],
                cwd=ROOT, env=env,
                stdout=log, stderr=subprocess.STDOUT)
            url, why = wait_tunnel_url(cf)
            if url:
                break
            print(f"      第 {attempt}/5 次没成功（{why[:80]}），5 秒后重试…")
            try:
                cf.terminate()
                cf.wait(timeout=5)
            except Exception:                               # noqa: BLE001
                pass
            cf = None
            time.sleep(5)
        if not url:
            print("[3/4] 连续 5 次都没拿到隧道地址。")
            print("      这台机器到 api.trycloudflare.com 的 TLS 握手在 0.6~19 秒之间抖动，")
            print("      cloudflared 内置 10 秒超时，握手慢的那次就会失败——过一会儿重跑通常能过。")
            return 1
        print(f"[3/4] 隧道已分配：{url}")

        token = ""
        if os.path.exists(TOKEN_FILE):
            token = open(TOKEN_FILE, encoding="utf-8").read().strip()
        print("[3/4] 正在验证通道（首次访问要等 Cloudflare 建路由，通常 10~30 秒）…")
        ok, detail = probe_channel(url, token)
        if not ok:
            print(f"[4/4] 验证没通过：{detail}")
            print("      配置**未改动**——宁可继续用旧地址，也不能把线上写到错地址上。")
            return 1
        print(f"[4/4] 验证通过，可用模型：{', '.join(detail)}")

        cfg = {"label": "本机 Ollama · 隧道", "base_url": url, "token": token,
               "model": models[0] if models else "qwen3:8b", "timeout": 180,
               # keep_alive 必须显式写：Ollama 默认 5 分钟空闲就卸载 4.7GB 权重，
               # 线上每隔几分钟来一次请求，等于一直冷加载（首字 20s+）。
               "keep_alive": "30m"}
        with open(PUB_CFG, "w", encoding="utf-8") as fp:
            json.dump(cfg, fp, ensure_ascii=False, indent=2)
        print(f"[4/4] 已写入 {os.path.basename(PUB_CFG)}")

        if push_ecs:
            print("\n[ECS] 把新地址推到阿里云服务器（只换 llm.json，绝不动 data\\）…")
            print("      链路一次只许连一次，22 端口约 50% 丢包，慢是正常的。")
            rc = subprocess.call(
                [sys.executable, "-X", "utf8",
                 os.path.join(ROOT, "scripts", "ecs_push_llm.py")],
                cwd=ROOT,
            )
            if rc == 0:
                print("[ECS] 推送完成，服务器已指向本机模型（/api/health 应为 ok）")
            else:
                print(f"[ECS] 推送失败（退出码 {rc}），但**通道本身仍在运行**。")
                print("      服务器继续用旧地址，表现为「接口全 200、状态灯红、拆解走规则」。")
                print("      网络恢复后单独重跑即可，不用重启通道：")
                print("          python scripts\\ecs_push_llm.py")
        else:
            print("\n提示：想让服务器立刻用上新地址，加 --push-ecs 重跑，或单独执行")
            print("      python scripts\\ecs_push_llm.py")

        print("\n这个窗口要一直开着；关掉就等于模型通道断开（业务自动降级，不报错）。\n")

        cf.wait()                                           # 保持前台，Ctrl+C 一起退
    except KeyboardInterrupt:
        print("\n[退出] 正在停止隧道与网关…")
    finally:
        for p in (cf, gw):
            if p is None:
                continue
            try:
                p.terminate()
            except Exception:                               # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
