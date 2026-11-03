"""本机模型网关：给公网隧道加一道令牌校验，避免 Ollama 裸奔。

为什么需要它
------------
Ollama 的 11434 端口没有任何鉴权。若直接用 cloudflared 把 11434 暴露出去，
任何人扫到那个随机域名就能：① 白嫖显卡跑自己的任务；② 用任意提示词调用你的模型；
③ 把显存打满导致答辩现场降级。隧道地址虽随机，但会出现在浏览器请求、日志、截屏里。

所以线上配置指向本脚本（默认 11435），由它：
- 校验 X-LLM-Token，不对就 401，连转发都不做；
- 只放行白名单路径（/api/chat、/api/tags、/api/ps），其他一律 404；
- 把请求体原样转发到 127.0.0.1:11434，响应原样回传（不解析、不改写）。

用法
----
    set LLM_GW_TOKEN=一串足够长的随机串
    python scripts/llm_gateway.py                  # 监听 127.0.0.1:11435
    python scripts/llm_gateway.py --port 11436 --upstream http://127.0.0.1:11434

令牌也可以放在项目根的 .llm_gateway_token 文件里（首次运行自动生成并打印一次）。
该令牌同时写进发布副本的 llm.json，两边一致才能通。
"""
import argparse
import hmac
import http.client
import json
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(ROOT, ".llm_gateway_token")

ALLOW = {"/api/chat", "/api/tags", "/api/ps"}
UPSTREAM_TIMEOUT = 180          # 8B 模型冷启动 + 长上下文可能超过 60s


def load_token() -> str:
    tok = (os.environ.get("LLM_GW_TOKEN") or "").strip()
    if tok:
        return tok
    if os.path.exists(TOKEN_FILE):
        tok = open(TOKEN_FILE, encoding="utf-8").read().strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(32)
    with open(TOKEN_FILE, "w", encoding="utf-8") as fp:
        fp.write(tok)
    print(f"[gateway] 已生成令牌并写入 {TOKEN_FILE}")
    print(f"[gateway] 令牌：{tok}")
    return tok


class Handler(BaseHTTPRequestHandler):
    token = ""
    upstream_host = "127.0.0.1"
    upstream_port = 11434
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):      # 默认实现会打两行噪音，自己收干净
        pass

    def _deny(self, code: int, msg: str):
        body = json.dumps({"error": msg}, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        got = self.headers.get("X-LLM-Token", "")
        # 定长比较，避免通过响应时间猜令牌
        return bool(got) and hmac.compare_digest(got, self.token)

    def _proxy(self, method: str):
        path = urlparse(self.path).path
        if path not in ALLOW:
            self._deny(404, f"path not allowed: {path}")
            return
        if not self._auth_ok():
            self._deny(401, "invalid token")
            return

        body = b""
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            body = self.rfile.read(n)

        t0 = time.perf_counter()
        conn = http.client.HTTPConnection(self.upstream_host, self.upstream_port,
                                          timeout=UPSTREAM_TIMEOUT)
        try:
            conn.request(method, path, body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status)
            self.send_header("Content-Type",
                             resp.getheader("Content-Type") or "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            ms = (time.perf_counter() - t0) * 1000
            print(f"[gateway] {method} {path} -> {resp.status} {len(data)}B {ms:.0f}ms",
                  flush=True)
        except Exception as e:                          # noqa: BLE001
            print(f"[gateway] {method} {path} 转发失败: {type(e).__name__}: {e}", flush=True)
            self._deny(502, f"upstream error: {type(e).__name__}")
        finally:
            conn.close()

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址，默认只本机（隧道进程也在本机，不需要对外）")
    ap.add_argument("--port", type=int, default=11435)
    ap.add_argument("--upstream", default="http://127.0.0.1:11434")
    args = ap.parse_args()

    u = urlparse(args.upstream)
    Handler.token = load_token()
    Handler.upstream_host = u.hostname or "127.0.0.1"
    Handler.upstream_port = u.port or 11434

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"[gateway] 监听 http://{args.host}:{args.port} -> "
          f"{Handler.upstream_host}:{Handler.upstream_port}（需 X-LLM-Token）", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[gateway] 已停止", flush=True)


if __name__ == "__main__":
    sys.exit(main())
