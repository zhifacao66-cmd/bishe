"""本地大模型调用封装（Ollama 协议，可经隧道指向本机 GPU）。

兼容 Qwen2.5 / Qwen3：
- Qwen3 默认开启思考模式，会先输出推理链再给答案，对结构化抽取任务纯属浪费，
  因此统一传 think=False（老模型会忽略该参数），必要时可用 LLM_THINK=1 打开。
- num_ctx 控制上下文长度：显存紧张时调小（默认 4096）。

通道
----
base_url 由 app/llm/channel.py 裁决：可以是本机 Ollama，也可以是公网隧道。
线上沙箱没有 GPU，请求经隧道回到开发者本机，推理仍在自己显卡上完成——
数据全程只在「社区浏览器 → 应用 → 本机模型」之间流动，不经过任何第三方模型服务。
"""
import os
import threading
import time
from collections import deque

import requests
from flask import current_app

from app.llm import channel
from app.llm.gate import QueueTimeout, gate

# 两套 HTTP 会话，区别只在是否吃系统代理：
# - 本机会话：必须 trust_env=False。踩过的坑：本机设了 http_proxy 时，requests 会把
#   发往 127.0.0.1:11434 的 POST 也交给代理，代理不认识这个内网地址，回 404 /api/chat；
#   于是模型调用全部失败、静默降级成规则路径——功能看着正常，但拆解质量掉一大截。
# - 远程会话：必须 trust_env=True。线上沙箱多半只能经代理出网，关掉代理就完全连不上。
_LOCAL = requests.Session()
_LOCAL.trust_env = False
_REMOTE = requests.Session()
_REMOTE.trust_env = True


class LLMUnavailable(Exception):
    pass


# ---------------- 运行期观测 ----------------
# 为什么要在进程里记这些：答辩/验收时最常被问的一句是「你这儿到底是不是真在跑模型」。
# 光看配置说"接上了"没有说服力，得能当场翻出「14:32:07 那次调用，qwen3:8b，2.4 秒，成功」。
# 只记耗时与成败，**不记 prompt 与输出正文**——那些含居民信息，进内存日志就是新的泄露面。
#
# 失败也要记原因：曾经只记 ok/ms，线上出现过一次"模型暂不可用"，事后看统计只有
# calls=1/fail=0（容器重启清零），既不知道是超时还是连不上，也没法证明已经恢复。
# 排障时"知道失败了"和"知道为什么失败"是两件事。
_STAT = {"calls": 0, "ok": 0, "fail": 0, "ms_sum": 0.0, "ms_max": 0.0}
_RECENT = deque(maxlen=20)
_STAT_LOCK = threading.Lock()
_LAST_ERROR = {"at": "", "kind": "", "detail": "", "model": ""}


def _err_kind(e: Exception) -> str:
    """把异常压成可读的分类码。

    隧道抖动、容器冷启动、模型加载超时、显存排队超限——这四种在业务上都是
    "模型不可用"，但处置完全不同：前两种重试即可，第三种要调大超时，
    第四种要降并发。错误信息不分层，排障就只能靠猜。
    """
    if isinstance(e, requests.Timeout):
        return "timeout"
    if isinstance(e, requests.ConnectionError):
        return "connect"
    resp = getattr(e, "response", None)
    if resp is not None:
        return f"http_{resp.status_code}"
    if isinstance(e, ValueError):
        return "bad_response"          # 返回体不是预期 JSON（多为网关错误页）
    return type(e).__name__


def _retryable(kind: str) -> bool:
    """只重试「对面这次没接住」的情况。

    排队超时（queue）不重试：GPU 已经过载，再塞一次只会让排队的其他人更慢；
    4xx 也不重试：请求本身有问题，重发一百次也一样。
    """
    return kind in ("connect", "timeout", "bad_response",
                    "http_502", "http_503", "http_504")


def _record(ok: bool, ms: float, model: str, err: str = "", detail: str = ""):
    global _LAST_ERROR
    with _STAT_LOCK:
        _STAT["calls"] += 1
        _STAT["ok" if ok else "fail"] += 1
        _STAT["ms_sum"] += ms
        _STAT["ms_max"] = max(_STAT["ms_max"], ms)
        _RECENT.appendleft({
            "at": time.strftime("%H:%M:%S"),
            "ok": ok, "ms": round(ms), "model": model,
            "err": err, "detail": detail[:200],
        })
        if not ok:
            _LAST_ERROR = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "kind": err, "detail": detail[:300], "model": model}


def stats() -> dict:
    with _STAT_LOCK:
        n = _STAT["calls"]
        return {
            "calls": n, "ok": _STAT["ok"], "fail": _STAT["fail"],
            "avg_ms": round(_STAT["ms_sum"] / n) if n else 0,
            "max_ms": round(_STAT["ms_max"]),
            "last_error": dict(_LAST_ERROR),
            "recent": list(_RECENT),
        }


def _endpoint(cfg_obj: dict, path: str) -> str:
    return f"{cfg_obj['base_url']}{path}"


def _proxy_url() -> tuple:
    """显式配置的出网代理。返回 (url, 来源)，没配就是空。

    为什么不像通常那样直接靠 requests 的 trust_env 读 HTTPS_PROXY：
    线上沙箱里**根本没有** HTTPS_PROXY，但有一个 `SPACE_PROXY_ENDPOINT`。
    第一版看着名字像就把它的值当代理用了，结果那是平台内部端点（裸 IP、无端口），
    requests 报 `ProxyError`——**把原本能通的直连也弄坏了**。
    教训：平台自有变量只该观察，不该猜语义。代理只认显式配置。
    """
    v = (os.environ.get("LLM_PROXY") or "").strip()
    if v:
        return (v if "://" in v else "http://" + v), "LLM_PROXY"
    return "", ""


def _proxies(cfg_obj: dict):
    if not cfg_obj.get("remote"):
        return None                       # 本机地址绝不能走代理，会回 404
    url = cfg_obj.get("proxy") or _proxy_url()[0]
    return {"http": url, "https": url} if url else None


def _headers(cfg_obj: dict) -> dict:
    # 隧道对端是 llm_gateway.py，由它校验令牌后再转发给 Ollama。
    # 本机直连时该头会被 Ollama 忽略，留空即可。
    return {"X-LLM-Token": cfg_obj["token"]} if cfg_obj.get("token") else {}


def chat(prompt: str, system: str = "", json_mode: bool = False,
         schema: dict | None = None, think: bool | None = None,
         queue_timeout: float | None = None) -> str:
    """调用模型，返回文本。失败抛 LLMUnavailable。

    schema: 传入 JSON Schema 时使用「结构化输出」约束解码，模型输出必然符合该结构，
            比 json_mode=True（仅保证是合法 JSON）稳定得多。
    think:  覆盖配置中的思考链开关，用于消融实验对比。
    queue_timeout: 单次请求的排队上限，None 表示用全局配置（公网匿名入口会调短）。
    """
    cfg = current_app.config
    ch = channel.resolve()
    payload = {
        "model": ch["model"],
        "messages": [
            *([{"role": "system", "content": system}] if system else []),
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "think": cfg.get("LLM_THINK", False) if think is None else think,  # Qwen3 关闭思考链，提速
        "keep_alive": ch.get("keep_alive", "30m"),   # 避免每次提问都重新加载 4.7GB 权重
        "options": {
            "num_ctx": cfg.get("LLM_NUM_CTX", 4096),  # 控制显存占用
            "temperature": cfg.get("LLM_TEMPERATURE", 0.2),  # 抽取任务低温度更稳
        },
    }
    if schema is not None:
        payload["format"] = schema          # 结构化输出：强制符合 JSON Schema
    elif json_mode:
        payload["format"] = "json"
    # 显存感知排队：并发请求在此串行通过，拿不到槽位就降级，不把压力透传给 GPU。
    # max_hold = 请求超时 + 30s 余量：超过这个时长还没交还槽位，就认定它卡在网络层了。
    gate.configure(cfg.get("LLM_MAX_CONCURRENCY", 1),
                   cfg.get("LLM_QUEUE_TIMEOUT", 40),
                   ch["timeout"] + 30)
    sess = _REMOTE if ch["remote"] else _LOCAL
    where = "远程模型通道" if ch["remote"] else "本地模型"

    # 远程通道重试一次。公网隧道 + 容器冷启动是两条独立的失败来源：
    # 隧道长连接空闲后重建、边缘节点回 502；容器被回收后首个请求打空。
    # 实测线上同一问题隔 47 秒再问就正常——这类失败重一次就好，不该直接告诉用户
    # "模型不可用"。本机通道不重试：连不上基本是 Ollama 没启动，重试只是白等。
    attempts = 2 if ch["remote"] else 1
    t0 = time.perf_counter()
    for i in range(attempts):
        try:
            with gate.slot(timeout=queue_timeout):
                r = sess.post(_endpoint(ch, "/api/chat"), json=payload,
                              headers=_headers(ch), timeout=ch["timeout"],
                              proxies=_proxies(ch))
                r.raise_for_status()
                try:
                    text = r.json()["message"]["content"]
                except (ValueError, KeyError, TypeError) as e:
                    raise ValueError(f"返回体不是预期的 chat 结构: {e}")
            _record(True, (time.perf_counter() - t0) * 1000, ch["model"])
            return text
        except QueueTimeout as e:
            _record(False, (time.perf_counter() - t0) * 1000, ch["model"], "queue", str(e))
            raise LLMUnavailable(str(e))
        except Exception as e:
            kind = _err_kind(e)
            if i + 1 < attempts and _retryable(kind):
                time.sleep(0.8)             # 给隧道重连一点时间，别把重试打成连环撞
                continue
            _record(False, (time.perf_counter() - t0) * 1000, ch["model"], kind, str(e))
            hint = {"connect": "隧道或模型服务连不上",
                    "timeout": f"推理超时（>{ch['timeout']}s）",
                    "bad_response": "网关返回内容无法解析"}.get(kind, "")
            raise LLMUnavailable(f"{where}不可用: {hint + '；' if hint else ''}{e}")


def probe() -> dict:
    """探活并**带回原因**。

    只返回 True/False 是不够的：线上出现过"探活失败"和"探活超时"两种情况，
    前者是令牌错，后者是容器出不去网，处置完全不同，但布尔值长得一模一样。
    """
    ch = channel.resolve()
    sess = _REMOTE if ch["remote"] else _LOCAL
    t0 = time.perf_counter()
    info = {
        "ok": False, "ms": 0, "error": "", "detail": "",
        "remote": ch["remote"],
        # 远程探活的超时不能太小。实测线上容器到公网 **TLS 握手本身要 9 秒**，
        # 原来给 6 秒 → 每次探活都超时 → 健康页永远显示"降级"，
        # 而实际推理可能是好的。探活误报比不报更伤人：会让人去修不存在的故障。
        "timeout_s": 25 if ch["remote"] else 3,
    }
    try:
        r = sess.get(_endpoint(ch, "/api/tags"), headers=_headers(ch),
                     timeout=info["timeout_s"], proxies=_proxies(ch))
        info["ok"] = r.ok
        if not r.ok:
            info["error"] = f"http_{r.status_code}"
            info["detail"] = (r.text or "")[:160]
            if r.status_code == 401:
                info["detail"] = "令牌与网关不一致（llm.json 的 token ≠ 网关的 token）"
    except Exception as e:                     # noqa: BLE001
        info["error"] = _err_kind(e)
        info["detail"] = str(e)[:200]
    info["ms"] = round((time.perf_counter() - t0) * 1000)
    return info


def probe_net() -> dict:
    """出网自检：直连能力 + 代理通道，分开测。

    线上排障最耗时的一步是"到底是谁不通"。这里把三件事拆开：
    1. 直连：DNS 能否解析、443 能否建成 TCP、TLS 能否握手；
    2. 代理：平台注入的代理变量在不在、经代理的 HTTPS 请求能否成功；
    3. 结论：给出"该走哪条路"的判断，而不是让人对着三个 null 猜。
    只报「有没有」和耗时，不回显代理地址的凭据部分。
    """
    import socket
    import ssl
    from urllib.parse import urlparse

    ch = channel.resolve()
    if not ch["remote"]:
        # 本机通道根本不走公网，测 443 只会得到"127.0.0.1 拒绝连接"这种噪音
        return {"skipped": "本机通道（127.0.0.1），不涉及出网"}
    host = urlparse(ch["base_url"]).hostname or ""
    proxy_url, proxy_key = _proxy_url()
    out = {
        "host": (host[:4] + "…" + host[-18:]) if len(host) > 22 else host,
        "proxy_env_keys": sorted(k for k in os.environ if "proxy" in k.lower()),
        "proxy": {"in_use": bool(proxy_url), "from": proxy_key},
        "direct": {"dns_ms": None, "tcp_ms": None, "tls_ms": None, "error": ""},
        "error": "",
    }
    if proxy_url:
        # 代理地址可能含凭据，只露 host:port
        p = urlparse(proxy_url)
        out["proxy"]["endpoint"] = f"{p.scheme}://{p.hostname}:{p.port or ''}"
    # 平台变量的原始形态（脱敏后只留前 32 字符）：不是标准 URL 时，
    # 排障必须看到它长什么样，否则只能对着"没找到代理"四个字猜。
    for k in ("SPACE_PROXY_ENDPOINT", "X_IDE_AUTH_PROXY"):
        v = (os.environ.get(k) or "").strip()
        if v:
            import re as _re
            raw = _re.sub(r"//[^/@]*@", "//***@", v)
            out.setdefault("proxy_raw", {})[k] = raw[:32] + ("…" if len(raw) > 32 else "")

    # —— 直连能力 ——
    try:
        t = time.perf_counter()
        ip = socket.gethostbyname(host)
        out["direct"]["dns_ms"] = round((time.perf_counter() - t) * 1000)
        out["direct"]["ip_head"] = ip.rsplit(".", 1)[0] + ".x"
    except Exception as e:                               # noqa: BLE001
        out["direct"]["error"] = f"DNS 解析失败: {e}"
    if out["direct"]["dns_ms"] is not None:
        try:
            t = time.perf_counter()
            with socket.create_connection((host, 443), timeout=20):
                out["direct"]["tcp_ms"] = round((time.perf_counter() - t) * 1000)
        except Exception as e:                           # noqa: BLE001
            out["direct"]["error"] = f"443 连不上: {e}"
        if out["direct"]["tcp_ms"] is not None:
            try:
                t = time.perf_counter()
                ctx = ssl.create_default_context()
                with socket.create_connection((host, 443), timeout=20) as s:
                    with ctx.wrap_socket(s, server_hostname=host):
                        out["direct"]["tls_ms"] = round((time.perf_counter() - t) * 1000)
            except Exception as e:                       # noqa: BLE001
                out["direct"]["error"] = f"TLS 握手失败: {e}"

    # —— 代理通道（真正决定能不能用模型的那条） ——
    if proxy_url:
        t0 = time.perf_counter()
        try:
            r = _REMOTE.get(_endpoint(ch, "/api/tags"), headers=_headers(ch),
                            timeout=12, proxies={"http": proxy_url,
                                                 "https": proxy_url})
            out["proxy"]["ok"] = r.ok
            out["proxy"]["status"] = r.status_code
        except Exception as e:                           # noqa: BLE001
            out["proxy"]["ok"] = False
            out["proxy"]["detail"] = str(e)[:160]
        out["proxy"]["ms"] = round((time.perf_counter() - t0) * 1000)

    # —— 结论 ——
    if out["proxy"].get("ok"):
        out["verdict"] = "经平台代理可出网，模型通道可用"
    elif out["direct"]["tls_ms"] is not None:
        out["verdict"] = "可直连公网，模型通道可用"
    elif proxy_url:
        out["verdict"] = "代理不通，且无法直连——模型将降级为规则路径"
    else:
        out["verdict"] = "既无直连能力、也没发现代理变量——模型将降级为规则路径"
    return out


def is_available() -> bool:
    """通道是否可达。

    注意：这只证明「能连上」，不证明「能推理」。模型加载失败、显存不足、
    请求排队超限时，本函数仍返回 True——真正的推理状态看 stats() 的 fail/last_error。
    """
    return probe()["ok"]


def running_models() -> list:
    """返回已下载的模型名列表，用于前端展示/自检"""
    ch = channel.resolve()
    sess = _REMOTE if ch["remote"] else _LOCAL
    try:
        r = sess.get(_endpoint(ch, "/api/tags"), headers=_headers(ch),
                     timeout=6 if ch["remote"] else 3, proxies=_proxies(ch))
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return []


def describe() -> dict:
    """通道自检信息。地址做掩码，令牌只报「有没有配」，绝不回显。"""
    ch = channel.resolve()
    return {
        "channel": "remote" if ch["remote"] else "local",
        "label": ch["label"],
        "base": ch["display"],
        "model": ch["model"],
        "remote": ch["remote"],
        "token_set": bool(ch["token"]),
        "source": ch["source"],           # env / file / default：配置从哪来的
        "timeout_s": ch["timeout"],
        "keep_alive": ch.get("keep_alive", ""),
        "proxy_set": bool(ch.get("proxy")),      # 只报有没有配，不回显地址
    }
