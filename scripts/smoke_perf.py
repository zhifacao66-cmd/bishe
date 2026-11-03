"""回归：传输层加速与站内预取（gzip / 缓存头 / 资源本地化）

这套测的不是业务规则，而是**翻页为什么会慢**以及"修了之后确实没坏"：

  1. gzip 只压该压的：HTML/JSON/CSS 压，导出件（xlsx/docx）绝不压；
  2. 缓存头分层：静态资源一年 immutable、列表页骨架 60 秒、
     详情页 no-cache（正文会变）、接口一律 no-store（提交后必须拿到新数据）；
  3. 页面不再带 Set-Cookie：默认每响应刷新会话会让浏览器对 HTML 缓存更保守，
     站内预取就复用不上；
  4. 1MB 的 echarts 只在真正画图的两个页面出现，其余页面一个字节都不拉；
  5. 全站不依赖任何外部 CDN（jsdelivr 国内实测 2.9s，还卡在 head 里阻塞渲染）。

第 4、5 条是这轮"切页面慢"的两个真凶，所以单独断言：只要有人在别的页面
重新引一次 echarts，或者把某个库改回外链，这套就会红。

用独立库，不碰 data/community.db。
"""
import gzip
import json
import os
import pathlib
import re
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_perf.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                        # noqa: E402
from app.extensions import db                      # noqa: E402
from app.models import Notice, Report              # noqa: E402

app = create_app("dev")

with app.app_context():
    from datetime import date
    db.session.add(Notice(id=1, title="关于开展冬季供暖设施检修的通知",
                          issuer="幸福里街道办事处", level="街道", kind="superior",
                          recv_status="received", status="published",
                          issued_at=date.today(),
                          body_md="一、检修范围\n辖区所有集中供暖小区。",
                          source_file="测试用"))
    db.session.add(Report(id=1, period=date.today().strftime("%Y-%m"),
                          line="街道",
                          data_json=json.dumps({
                              "period": date.today().strftime("%Y-%m"),
                              "summary": "本期测试报表", "org": "幸福里社区",
                              "line_counts": {"综治": 3}, "type_counts": {"报修": 2},
                              "item_rows": [], "overview": {},
                          }, ensure_ascii=False)))
    db.session.commit()

PASS = FAIL = 0
FAILED = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name}   ← {detail}")


def login(c, u="wang", p="123456"):
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


def body_of(r):
    """取响应正文（自动解 gzip），用于断言"压了但内容没变"。"""
    raw = r.get_data()
    if r.headers.get("Content-Encoding") == "gzip":
        return gzip.decompress(raw)
    return raw


print("=" * 66)
print("传输层加速 · 压缩 · 缓存分层 · 资源本地化 回归")
print("=" * 66)

admin = app.test_client()
c = app.test_client()
print("\n[1] 登录")
check("管理员", login(admin, "admin", "admin123").status_code == 302)
check("社区", login(c).status_code == 302)

# ---------------------------------------------------------------- 2 gzip
print("\n[2] gzip：该压的压，不该压的一个字节都不动")
gz = {"Accept-Encoding": "gzip, deflate"}
plain = {"Accept-Encoding": ""}

r_gz = c.get("/tasks", headers=gz)
r_raw = c.get("/tasks", headers=plain)
check("HTML 走 gzip", r_gz.headers.get("Content-Encoding") == "gzip",
      r_gz.headers.get("Content-Encoding"))
check("HTML 解压后与明文一致", body_of(r_gz) == body_of(r_raw),
      f"{len(body_of(r_gz))} vs {len(body_of(r_raw))}")
check("压完确实更小", len(r_gz.get_data()) < len(r_raw.get_data()),
      f"{len(r_gz.get_data())} vs {len(r_raw.get_data())}")
check("Vary 含 Accept-Encoding",
      "accept-encoding" in (r_gz.headers.get("Vary") or "").lower(),
      r_gz.headers.get("Vary"))

r_api = c.get("/api/tasks", headers=gz)
check("JSON 走 gzip", r_api.headers.get("Content-Encoding") == "gzip")
check("JSON 解压后能正常解析", json.loads(body_of(r_api).decode("utf-8"))["code"] == 0)

r_css = c.get("/static/css/style.css", headers=gz)
check("CSS 走 gzip", r_css.headers.get("Content-Encoding") == "gzip")
check("CSS 解压后仍是 CSS", b"--primary" in body_of(r_css),
      body_of(r_css)[:60].decode("utf-8", "ignore"))

# 导出件：压了会毁掉 xlsx（zip 容器），必须原样发出
x = c.get("/reports/1/export/xlsx", headers=gz)
check("xlsx 导出不被压缩", x.headers.get("Content-Encoding") is None,
      x.headers.get("Content-Encoding"))
check("xlsx 仍是合法 zip（PK 魔数）", x.get_data()[:2] == b"PK", str(x.get_data()[:4]))

# 小响应不值得压：gzip 头 + 字典开销可能比内容还大
r_ping = c.get("/health", headers=gz)
check("小响应不压（<700B）", r_ping.headers.get("Content-Encoding") is None,
      f"{len(r_ping.get_data())}B / {r_ping.headers.get('Content-Encoding')}")

r_nogz = c.get("/tasks")
check("客户端不支持 gzip 时原样发", r_nogz.headers.get("Content-Encoding") is None)

# ---------------------------------------------------------------- 3 缓存分层
print("\n[3] 缓存分层：静态一年、骨架 60 秒、详情页不缓存、接口不缓存")
r1 = c.get("/static/css/style.css", query_string={"v": "123"})
check("带版本号的静态资源 immutable",
      "immutable" in (r1.headers.get("Cache-Control") or "")
      and "max-age=31536000" in (r1.headers.get("Cache-Control") or ""),
      r1.headers.get("Cache-Control"))
r2 = c.get("/static/css/style.css")
check("不带版本号只给 10 分钟",
      r2.headers.get("Cache-Control") == "public, max-age=600",
      r2.headers.get("Cache-Control"))

r3 = c.get("/tasks")
cc = r3.headers.get("Cache-Control") or ""
check("列表页骨架可缓存", "max-age=60" in cc and "private" in cc, cc)
check("列表页按 Cookie 区分（不同角色侧边栏不同）",
      "cookie" in (r3.headers.get("Vary") or "").lower(), r3.headers.get("Vary"))

r4 = c.get("/notices/1")
check("详情页不缓存正文",
      r4.headers.get("Cache-Control") == "private, no-cache",
      r4.headers.get("Cache-Control"))

r5 = c.get("/api/ledgers")
check("接口一律 no-store", r5.headers.get("Cache-Control") == "no-store",
      r5.headers.get("Cache-Control"))

check("页面响应不再刷新会话 Cookie（不再每页 Set-Cookie）",
      not r3.headers.getlist("Set-Cookie"), str(r3.headers.getlist("Set-Cookie")))

# 登录/退出这类真的改了会话的，仍然要下发 Cookie
c2 = app.test_client()
r_login = login(c2)
check("登录仍然下发 Cookie", bool(r_login.headers.getlist("Set-Cookie")),
      str(r_login.headers.getlist("Set-Cookie")))

# ---------------------------------------------------------------- 4 资源本地化
print("\n[4] echarts：本地资源 + 只在画图的页面加载")
r_home = c.get("/")
h = r_home.get_data(as_text=True)
m = re.search(r'src="/static/vendor/echarts\.min\.js\?v=(\d+)"', h)
check("首页引本地 echarts 且带版本号", bool(m), "没找到 vendor/echarts 引用")
check("版本号形如 mtime", bool(m) and int(m.group(1)) > 1_600_000_000,
      m.group(1) if m else "")

vendor = BASE / "app" / "static" / "vendor" / "echarts.min.js"
check("echarts 文件确实在仓库里", vendor.exists(), str(vendor))
if vendor.exists():
    check("echarts 体积正常（≈1MB，不是占位文件）",
          vendor.stat().st_size > 900_000, f"{vendor.stat().st_size}B")

# 不画图的页面一个字节都不该拉。
# reports 是例外：它的图表是在"点开某份报表"时才懒加载的，
# 页面上只有一段构造 <script> 的字符串，不会有真正的 script 标签。
for path in ("/tasks", "/orders", "/ledgers", "/notices", "/reports", "/kb"):
    html = c.get(path).get_data(as_text=True)
    check(f"{path} 不加载 echarts",
          not re.search(r'<script[^>]*vendor/echarts', html))
check("reports 的图表确实是懒加载",
      "ensureEcharts" in c.get("/reports").get_data(as_text=True))

# ---------------------------------------------------------------- 5 无外链依赖
print("\n[5] 全站不依赖外部 CDN（国内访问 jsdelivr 实测 2.9s）")
PAGES = ["/", "/tasks", "/orders", "/ledgers", "/notices", "/reports", "/kb"]
for path in PAGES:
    html = c.get(path).get_data(as_text=True)
    ext = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    check(f"{path} 无外部资源", not ext, str(ext[:2]))

# ---------------------------------------------------------------- 6 预取钩子
print("\n[6] 站内预取：映射表覆盖每个侧边栏页面")
h = c.get("/").get_data(as_text=True)
check("base 模板带预取映射", "PAGE_API" in h)
check("预取走 sessionStorage（与接口 no-store 不冲突）", "__pf:" in h)
for p in PAGES:
    if p == "/":
        continue
    check(f"映射表包含 {p}", f"'{p}'" in h or f'"{p}"' in h)

# 预取的接口地址必须真实存在，否则悬停就是白发一次 404
api_calls = set(re.findall(r"'(/api/[a-z/]+)'", h))
check("映射表里的接口都非空", len(api_calls) >= 8, str(sorted(api_calls)))
print("     映射接口：" + ", ".join(sorted(api_calls)[:10]))

# ---------------------------------------------------------------- 7 页面完整性
print("\n[7] 压缩后页面依然完整")
for path in PAGES + ["/notices/1"]:
    _r = c.get(path, headers=gz)
    _body = body_of(_r)
    html = _body.decode("utf-8", "ignore")
    # 别锁死整个开始标签：加上 id/aria 之后 `class="sidebar"` 后面还有别的属性，
    # 整串比对会失配。要验的是"侧栏还在、文档收尾完整"。
    check(f"{path} 结构完整", html.rstrip().endswith("</html>")
          and 'class="sidebar"' in html and 'id="sidebar"' in html,
          f"http={_r.status_code} len={len(_body)}")

print()
print("=" * 66)
print(f"通过 {PASS} 项 / 失败 {FAIL} 项")
print("=" * 66)
if FAILED:
    print("失败项：" + "、".join(FAILED))
sys.exit(1 if FAIL else 0)
