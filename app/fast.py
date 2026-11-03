"""传输层加速：gzip 压缩 + 静态资源长缓存 + 可预取的页面缓存头。

为什么单独抽一个模块
------------------
线上链路是"公网 → 反向代理 → 容器"，容器 CPU 弱、单次往返 300ms 起。
翻页慢的主因从来不是 SQL（本地渲染 3~36ms），而是四件事：

1. **每个页面都在 head 里同步拉 1MB 的 echarts**（jsdelivr 国内实测 2.9s，
   弱网更久），而且只有首页/报表两个页面真的用得到；
2. **静态资源 `Cache-Control: no-cache`**，翻一次页就重新验证一次，白吃一个 RTT；
3. **HTML 与 JSON 都不压缩**，18KB / 31KB 明文过弱网；
4. 页面渲染完还要串行等 1~2 个 XHR 才出数据。

本模块负责 2、3，并为 4 的前端预取提供"可缓存但不会太陈旧"的响应头。
压缩对 >700B 的文本响应生效（gzip 6 级，压缩比换 CPU 的折中）；
静态资源带 `?v=<mtime>` 时给一年 immutable，不带则退化为 10 分钟。

**API 一律 no-store**：列表数据不能进 HTTP 缓存，否则提交完再拉列表
可能拿到旧 JSON。前端预取走 sessionStorage 内存副本，不受这里影响。
"""
from __future__ import annotations

import gzip
import os
import re

from flask import request, url_for

# 只压这些：二进制（图片/字体/zip）再压一遍纯属浪费 CPU
_TEXTUAL = {
    "text/html", "text/plain", "text/css", "text/xml",
    "application/json", "application/javascript", "text/javascript",
    "application/xml", "image/svg+xml",
}

_MIN_COMPRESS = 700          # 低于这个长度，gzip 头本身就不划算

# 详情页是服务端渲染正文的（公告原文/落地件/用户详情），内容会变，
# 缓存它等于让人看到改之前的版本，所以这类页面一律不缓存骨架。
_DETAIL_PAGE = re.compile(r"^/(notices|admin/users)/\d+")


def _add_vary(resp, field: str) -> None:
    cur = resp.headers.get("Vary")
    if not cur:
        resp.headers["Vary"] = field
    elif field.lower() not in cur.lower():
        resp.headers["Vary"] = cur + ", " + field


def install(app) -> None:
    """挂到 app 上：模板 asset() + 统一的传输层响应处理。"""

    # ---------- 模板：带版本号的静态资源地址 ----------
    def asset(path: str) -> str:
        """`asset('css/style.css')` → `/static/css/style.css?v=1712345678`

        用 mtime 当版本号，改文件自动换 URL，配合 immutable 长缓存就
        既有"永远命中本地缓存"又有"改了立刻生效"，不用手动维护版本常量。
        """
        fp = os.path.join(app.static_folder or "", path.replace("/", os.sep))
        try:
            ver = int(os.path.getmtime(fp))
        except OSError:
            ver = 0
        return url_for("static", filename=path, v=ver)

    app.jinja_env.globals["asset"] = asset

    # ---------- 响应：压缩 + 缓存策略 ----------
    @app.after_request
    def _transport(resp):                            # noqa: ANN001
        path = request.path or ""
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()

        # 1) 静态资源：带版本号就给一年，不带就 10 分钟
        if path.startswith("/static/"):
            if resp.status_code == 200:
                resp.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable"
                    if request.args.get("v") else "public, max-age=600")

        # 2) 页面：只缓存骨架
        #    列表页里真正的数据全部走 XHR（no-store），所以骨架缓久一点
        #    不会让人看到旧数据，却能让"悬停预取 → 点击"和"进详情 → 后退"
        #    直接命中磁盘缓存、零往返。详情页是服务端渲染正文的，不在此列。
        #    private + Vary: Cookie：不同角色侧边栏不同，绝不能跨账号复用。
        elif ctype == "text/html":
            if resp.status_code == 200 and not resp.headers.get("Cache-Control"):
                if _DETAIL_PAGE.match(path):
                    resp.headers["Cache-Control"] = "private, no-cache"
                else:
                    resp.headers["Cache-Control"] = "private, max-age=60"
                _add_vary(resp, "Cookie")

        # 3) 接口数据：禁止任何缓存，换来的是一致性
        elif ctype == "application/json":
            if request.method == "GET":
                resp.headers["Cache-Control"] = "no-store"

        # 4) gzip
        #    注意排除的只是**附件**：Werkzeug 给静态文件也加
        #    `Content-Disposition: inline; filename=...`，早先按"有这个头就不压"
        #    判断，结果 CSS/JS 全都没压上（用户下载的 xlsx/docx 才是 attachment）。
        accept = (request.headers.get("Accept-Encoding") or "").lower()
        disposition = (resp.headers.get("Content-Disposition") or "").lower()
        if ("gzip" in accept and resp.status_code == 200
                and ctype in _TEXTUAL
                and "Content-Encoding" not in resp.headers
                and not disposition.startswith("attachment")
                and (not resp.direct_passthrough or path.startswith("/static/"))):
            try:
                resp.direct_passthrough = False      # 允许取出 send_file 的流
                data = resp.get_data()
                if len(data) >= _MIN_COMPRESS:
                    packed = gzip.compress(data, 6)
                    if len(packed) < len(data):
                        resp.set_data(packed)
                        resp.headers["Content-Encoding"] = "gzip"
                        resp.headers["Content-Length"] = str(len(packed))
                        _add_vary(resp, "Accept-Encoding")
            except Exception:                        # noqa: BLE001
                pass                                 # 压不动就原样发，别把页面搞挂

        return resp

    # ---------- 安全响应头 ----------
    # 这一层是"就算前端某处漏了转义"的兜底，不是可选项：
    #   nosniff          禁止浏览器猜类型（上传/导出件被当 HTML 执行）
    #   X-Frame-Options  防点击劫持（审计页一个"回滚"按钮被套进 iframe 就能骗点）
    #   Referrer-Policy  跨站时不把带 id 的路径带出去
    #   CSP              唯一能挡住"新出现的注入点"的机制
    # CSP 里 script/style 仍是 unsafe-inline：11 个页面都是内联脚本，
    # 换成 nonce 要给每个 <script> 打标记，收益是"再加一层护栏"，
    # 但真正的防线是**输出转义**（base.html 里的全局 esc），这里不重复造。
    # frame-ancestors / base-uri / object-src 三项不影响内联脚本，直接收紧。
    _CSP = ("default-src 'self'; img-src 'self' data:; font-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; form-action 'self'; base-uri 'self'; "
            "frame-ancestors 'none'; object-src 'none'")

    @app.after_request
    def _secure_headers(resp):                       # noqa: ANN001
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        h.setdefault("Content-Security-Policy", _CSP)
        # Server 头不在这里写：开发服务器会再追一条自己的，两条并存等于白改。
        # 详见 app/fast.py 末尾 `_HardenedWSGI` 与 run.py 的 request_handler。

        # Cookie 的 Secure 标记**不能在这里加**：Flask 存会话是在
        # process_response 的最后一步（after_request 跑完之后），此刻响应头里
        # 根本没有 Set-Cookie。所以改在 WSGI 层包一层，见文件末尾 `_SecureCookie`。

        return resp

    # ---------- WSGI 包装：https 下给 Cookie 补 Secure ----------
    app.wsgi_app = _HardenedWSGI(app.wsgi_app)


class _HardenedWSGI:
    """WSGI 层的两件收尾工作（after_request 做不了的那部分）

    1. **https 下给 Set-Cookie 补 `Secure`**
       为什么不能在 after_request 里做：Flask 存会话是 process_response 的
       最后一步（after_request 跑完之后），那时响应头里根本没有 Set-Cookie。
       为什么不写死 `SESSION_COOKIE_SECURE=True`：本机 http 调试时浏览器会
       直接丢弃这个 Cookie，登录不上；写死 False 则线上（托管平台是 https）
       会话能在明文信道被截获。真实协议只有 `X-Forwarded-Proto` 或
       `wsgi.url_scheme` 知道，两个都不说 https 就不动（本地开发即如此）。

    2. **去掉应用自己加的重复 Server 头**
       Werkzeug 开发服务器会给每个响应再追一条自己的 `Server: ...`，
       如果应用也写一条，响应里就有两条。这里只保留第一条，
       banner 的实际取值由服务器层决定（见 run.py 的 request_handler）。
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        proto = (environ.get("HTTP_X_FORWARDED_PROTO") or "").split(",")[0].strip().lower()
        https = proto == "https" or environ.get("wsgi.url_scheme") == "https"

        def _start(status, headers, exc_info=None):
            fixed, has_server = [], False
            for k, v in headers:
                low = k.lower()
                if low == "server":
                    if has_server:                    # 后出现的那条直接丢掉
                        continue
                    has_server = True
                    v = "community-agent"
                elif low == "set-cookie" and https and "secure" not in v.lower():
                    v = v + "; Secure"
                fixed.append((k, v))
            # 自己**不补** Server：应用补一条、服务器再追一条，扫描器读到的
            # 仍可能是两条里的任意一条。banner 统一由服务器层设置
            # （开发：run.py 的 request_handler；生产：gunicorn 默认不带版本）。
            return start_response(status, fixed, exc_info)

        return self.wsgi_app(environ, _start)
