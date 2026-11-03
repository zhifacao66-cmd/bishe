"""模型通道配置：把「推理在哪里跑」收敛成唯一裁决点。

背景
----
开发时模型跑在本机 Ollama（127.0.0.1:11434）；线上沙箱是没有 GPU 的无状态容器，
既装不了 Ollama，也没有地方注入环境变量——发布工具只传代码和启动命令。
于是线上只有两条路：请求转发回本机 GPU，或者外接云端 API。

本模块把这件事做成「通道」：调用方只认 base_url + token + model，
不关心对面是 localhost 还是公网隧道。换通道不改代码，只改配置。

配置来源优先级（高 → 低）
1. 环境变量 LLM_BASE_URL / OLLAMA_BASE_URL、LLM_MODEL、LLM_TOKEN
   —— 本地开发、压测、评测脚本用它，临时指向别的端口不影响别人
2. 项目根 llm.json —— 发布副本携带；线上只能靠它
3. config.py 里的默认值 —— 本机 Ollama

为什么要有 token
----------------
Ollama 本身没有任何鉴权。若把 11434 直接挂上公网隧道，任何扫到地址的人
都能白嫖你的显卡，也能拿你的模型跑任意提示词。所以线上不直连 Ollama，
而是连本机的 llm_gateway.py，由它校验 X-LLM-Token 后再转发。
"""
import json
import os
from urllib.parse import urlparse

from flask import current_app

# 这些主机名一律视为「本机」：请求必须绕开系统代理，否则会被代理转发成 404
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CFG_PATH = os.path.join(_ROOT, "llm.json")

_cache = {"mtime": None, "data": {}}


def _load_file() -> dict:
    """读 llm.json，按 mtime 缓存——每次请求都读盘没必要，改了文件又能立刻生效"""
    try:
        st = os.stat(_CFG_PATH)
    except OSError:
        return {}
    if _cache["mtime"] == st.st_mtime:
        return _cache["data"]
    try:
        with open(_CFG_PATH, encoding="utf-8") as fp:
            data = json.load(fp)
        if not isinstance(data, dict):
            data = {}
    except Exception:                       # 文件写坏时不要让整个应用起不来
        data = {}
    _cache.update(mtime=st.st_mtime, data=data)
    return data


def is_local_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return True
    return host in _LOCAL_HOSTS


def mask_url(url: str) -> str:
    """对外展示用的地址：把隧道子域打掉一截。

    隧道域名本身不是密钥，但公网可扫——健康页/截图/答辩投屏上没必要把完整地址
    摊开。保留协议、前 4 位与后两段域名，足够判断「是不是接上了」。
    """
    try:
        p = urlparse(url)
        host = p.hostname or ""
        if host in _LOCAL_HOSTS:
            return f"{p.scheme}://{host}{':' + str(p.port) if p.port else ''}"
        parts = host.split(".")
        head = parts[0]
        head = head[:4] + "…" if len(head) > 4 else head + "…"
        tail = ".".join(parts[-2:]) if len(parts) >= 2 else ""
        return f"{p.scheme}://{head}.{tail}" if tail else f"{p.scheme}://{head}"
    except Exception:
        return "(无法解析)"


def resolve() -> dict:
    """算出当前生效的通道。调用方每次现算，不做进程级缓存——改配置要立刻生效。"""
    cfg = current_app.config
    f = _load_file()

    base, source = "", "default"
    for key in ("LLM_BASE_URL", "OLLAMA_BASE_URL"):
        v = (os.environ.get(key) or "").strip()
        if v:
            base, source = v, "env"
            break
    if not base:
        v = (f.get("base_url") or "").strip()
        if v:
            base, source = v, "file"
    if not base:
        base = (cfg.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").strip()
    base = base.rstrip("/")

    model = (os.environ.get("LLM_MODEL") or "").strip() \
        or (f.get("model") or "").strip() or cfg.get("LLM_MODEL") or "qwen3:8b"

    token = (os.environ.get("LLM_TOKEN") or "").strip() or (f.get("token") or "").strip()

    remote = not is_local_url(base)
    timeout = cfg.get("LLM_TIMEOUT", 60)
    try:
        if f.get("timeout"):
            timeout = int(f["timeout"])
    except Exception:
        pass
    # 隧道比本机多一跳，且模型冷启动要加载权重——远程通道给足时间，
    # 否则第一次调用必超时，降级成规则，看起来就像"模型没生效"
    if remote:
        timeout = max(int(timeout), 90)

    label = (f.get("label") or "").strip() or ("远程模型通道" if remote else "本机 Ollama")

    # 模型驻留时长。Ollama 默认 keep_alive=5m，5 分钟没人问就把 8B 权重从显存卸掉，
    # 下一个请求要重新读 4.7GB 进卡——冷加载十几秒，且最容易在加载期间超时降级。
    # 线上是"人来了才点一下"的低频使用场景，几乎每题都是冷启动，所以默认拉长到 30 分钟。
    keep_alive = (os.environ.get("LLM_KEEP_ALIVE") or "").strip() \
        or (f.get("keep_alive") or "").strip() or "30m"

    # 出网代理：**只在显式配置时才用**。
    # 踩过的坑：容器里有个 SPACE_PROXY_ENDPOINT（值是个裸内网 IP，无端口），
    # 看着像代理就拿来用，结果 requests 报 ProxyError，把本来能用的直连也弄坏了。
    # 平台的自有变量只该看，不该猜语义。
    proxy = (os.environ.get("LLM_PROXY") or "").strip() or (f.get("proxy") or "").strip()
    if proxy and "://" not in proxy:
        proxy = "http://" + proxy

    return {
        "base_url": base,
        "model": model,
        "token": token,
        "remote": remote,
        "timeout": timeout,
        "label": label,
        "source": source,
        "display": mask_url(base),
        "keep_alive": keep_alive,
        "proxy": proxy,
    }
