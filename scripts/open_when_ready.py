# -*- coding: utf-8 -*-
"""等服务就绪后再打开浏览器

由 启动系统.bat 拉起：Flask 启动要 2 秒（含建表），首次推理还要预热，
直接开浏览器会先看到"无法访问"。这里轮询 /api/health，通了再开。

    python scripts/open_when_ready.py [url] [--timeout 90]

本机装了系统代理，urllib 默认会走 http_proxy，所以显式用空 ProxyHandler，
否则连 127.0.0.1 也会被代理截走返回 502。
"""
import sys
import time
import urllib.request
import webbrowser

OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    url = args[0] if args else "http://127.0.0.1:5000"
    timeout = 90.0
    for i, a in enumerate(sys.argv):
        if a == "--timeout" and i + 1 < len(sys.argv):
            timeout = float(sys.argv[i + 1])

    health = url.rstrip("/") + "/api/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with OPEN.open(health, timeout=1.0) as r:
                if r.status == 200:
                    webbrowser.open(url)
                    return 0
        except Exception:
            pass
        time.sleep(0.6)
    return 1


if __name__ == "__main__":
    sys.exit(main())
