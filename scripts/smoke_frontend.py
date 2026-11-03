"""前端契约回归：统一请求入口、失败可见、公告分页

这套测试锁三件"看起来正常、坏起来也看不出来"的事：

  A. 请求入口只有一个。页面里散着 30 多处裸 fetch 时，任何一次网络抖动都
     表现为"表格停在加载中"，而且没人知道该去哪加提示；
  B. 失败必须看得见。业务拒绝（code!=0）不能 `return` 了事，
     否则失败和"还没加载完"长得一模一样；
  C. 公告列表有分页，且"共 N 条"与卡片数字同源。
     写死 limit(200) 时第 201 条静默消失，卡片却还是全量数字。

其中 A/C 是静态与接口两层都测：静态查"写法有没有跑偏"，
接口查"分页语义对不对"（越界夹取、非法参数、筛选只改 matched 不改家底）。

运行：python scripts/smoke_frontend.py
"""
import pathlib
import re
import shutil
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

SRC = BASE / "data" / "community.db"
DB = BASE / "data" / "smoke_frontend.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()
if SRC.exists():
    for suf in ("", "-wal", "-shm"):
        s_ = pathlib.Path(str(SRC) + suf)
        if s_.exists():
            shutil.copy2(s_, str(DB) + suf)

import os                                        # noqa: E402

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                        # noqa: E402
from app.extensions import db                     # noqa: E402
from app.models import Notice                     # noqa: E402

app = create_app("dev")
PASS, FAIL = 0, 0
TPL = BASE / "app" / "templates"
CSS = BASE / "app" / "static" / "css" / "style.css"


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {extra}")


def login(c, u, p="123456"):
    c.get("/logout")
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


def strip_comments(src: str) -> str:
    """去掉行注释再断言：**注释里写的反例不该让测试挂**。

    这套测试刚写好时就踩过——某段注释原文引用了"修复前是 return; 了事"，
    结果断言把这个反例当成了现行代码。测试读的是代码，不是散文。
    """
    return "\n".join(re.sub(r"//.*$", "", ln) for ln in src.splitlines())


base_src = (TPL / "base.html").read_text(encoding="utf-8")
css_src = CSS.read_text(encoding="utf-8")

# ======================================================================
print("\n[A] 只有一个请求入口")
check("base.html 提供 window.api", "window.api = function" in base_src)
check("api 带超时（默认 60s）", "DEFAULT_T = 60000" in base_src)
check("超时会中止请求（AbortController）", "AbortController" in base_src)
check("非 JSON 响应单独识别", "indexOf('json') < 0" in base_src,
      "网关 502 回 HTML 时，r.json() 抛的是 SyntaxError，会被误当成代码 bug")
check("失败时弹提示条", "netBar(msg, kind)" in base_src)
check("提示条有重试入口", "netbar-retry" in base_src)
check("提示条样式在 CSS 里", ".netbar{" in css_src and ".netbar.show" in css_src)
check("提示条 z-index 高于手机抽屉(60)",
      re.search(r"\.netbar\{[^}]*z-index:(\d+)", css_src) is not None
      and int(re.search(r"\.netbar\{[^}]*z-index:(\d+)", css_src).group(1)) > 60)

# 除 base（包装层）和 pending（要判 r.status===401 才能拦住未登录）外，
# 页面里不该再有裸 fetch
ALLOW = {"base.html", "pending.html"}
bare = []
for p in sorted(TPL.glob("*.html")):
    if p.name in ALLOW:
        continue
    src = p.read_text(encoding="utf-8")
    for i, ln in enumerate(src.splitlines(), 1):
        if re.search(r"(?<![\w.])fetch\s*\(", ln):
            bare.append(f"{p.name}:{i} {ln.strip()[:70]}")
check(f"页面没有裸 fetch（白名单 {sorted(ALLOW)}）", not bare, bare[:4])

# 迁移是"换入口"，不是"删错误处理"：业务码判断必须还在
n_api = 0
n_code = 0
for p in TPL.glob("*.html"):
    src = p.read_text(encoding="utf-8")
    n_api += len(re.findall(r"(?<![\w.])api\(", src))
    n_code += len(re.findall(r"j\.code\s*!==?\s*0", src))
check("api() 调用点覆盖了原先的 fetch 规模", n_api >= 30, f"实际 {n_api} 处")
check("业务码判断没有被一起删掉", n_code >= 15, f"实际 {n_code} 处")
check("没有残留 res.ok / r.ok 判空（api 已归一化）",
      not re.search(r"\bres\.ok\b", "".join(
          p.read_text(encoding="utf-8") for p in TPL.glob("*.html"))))

# ======================================================================
print("\n[B] 转义只有一份")
check("base 的 esc 转义 5 个字符",
      re.search(r"replace\(/\[&<>\"'\]/g", base_src) is not None)
dup = [p.name for p in TPL.glob("*.html")
       if p.name != "base.html"
       and re.search(r"function\s+esc\s*\(", strip_comments(p.read_text(encoding="utf-8")))]
check("页面不再自定义 esc（会覆盖全局那份）", not dup, dup)

# ======================================================================
print("\n[C] 失败看得见，不是停在“加载中”")
notices_src = (TPL / "notices.html").read_text(encoding="utf-8")
check("notices 业务失败会把原因写进表格",
      "esc(j.msg || '加载失败')" in notices_src,
      "原来 `if (j.code !== 0) return;`，表格永远停在加载中")
silent = []
for p in sorted(TPL.glob("*.html")):
    src = strip_comments(p.read_text(encoding="utf-8"))
    if re.search(r"if\s*\(\s*j\.code\s*!==?\s*0\s*\)\s*return\s*;", src):
        silent.append(p.name)
check("没有“业务失败直接 return”的静默写法", not silent, silent)

# ======================================================================
print("\n[D] 公告分页：语义、越界、非法参数")
with app.app_context():
    have = Notice.query.count()
    for i in range(max(0, 9 - have)):
        db.session.add(Notice(title=f"分页测试件{i}", issuer="幸福里社区",
                              level="社区", scope="all", status="published",
                              kind="original", recv_status="received"))
    db.session.commit()

with app.test_client() as c:
    login(c, "admin", "admin123")

    def q(params=""):
        j = c.get("/api/notices" + params).get_json()
        assert j["code"] == 0, j
        return j["data"]

    d = q()
    cnt = d["counts"]
    for k in ("matched", "page", "size", "pages", "shown", "truncated"):
        check(f"counts 带分页字段 {k}", k in cnt, list(cnt))

    total = cnt["matched"]
    check("默认页容量不超过上限", cnt["shown"] <= cnt["size"])
    check("默认第 1 页", cnt["page"] == 1)
    check("pages 与 matched/size 自洽",
          cnt["pages"] == max(1, -(-total // cnt["size"])),
          f'{cnt["pages"]} vs {total}/{cnt["size"]}')

    d2 = q("?size=3")
    check("size=3 只回 3 条", len(d2["items"]) == 3, len(d2["items"]))
    check("size=3 时 pages 按 3 算", d2["counts"]["pages"] == max(1, -(-total // 3)),
          d2["counts"]["pages"])
    check("truncated 表示还有后续", d2["counts"]["truncated"] is (total > 3),
          f'{d2["counts"]["truncated"]} / total={total}')

    # 逐页取，必须不重不漏
    ids, pages = [], d2["counts"]["pages"]
    for pg in range(1, pages + 1):
        ids += [x["id"] for x in q(f"?size=3&page={pg}")["items"]]
    all_ids = [x["id"] for x in q("?size=200")["items"]]
    check("逐页合并等于一次取全（不漏）", set(ids) == set(all_ids),
          f"{len(set(all_ids)) - len(set(ids))} 条漏掉")
    check("逐页合并没有重复", len(ids) == len(set(ids)), f"{len(ids)} vs {len(set(ids))}")

    # 越界与非法参数：都不能把"没填"理解成"只要一条"
    over = q("?size=3&page=999")["counts"]
    check("页码越界夹到最后一页", over["page"] == over["pages"], over)
    check("越界那一页仍有数据", over["shown"] > 0, over)
    for bad in ("?page=0", "?page=-5", "?page=abc"):
        check(f"{bad} 回第 1 页", q(bad + "&size=3")["counts"]["page"] == 1)
    for bad in ("?size=0", "?size=-1", "?size=abc"):
        got = q(bad)["counts"]["size"]
        check(f"{bad} 回默认页容量", got == 50, got)
    check("size 超过上限被夹住", q("?size=9999")["counts"]["size"] == 200)

    # 筛选只改 matched，不动家底
    before = q()["counts"]
    filt = q("?status=pending&size=3")
    after = filt["counts"]
    check("筛选后 matched 变小", after["matched"] <= before["matched"],
          f'{after["matched"]} vs {before["matched"]}')
    check("筛选不改家底 published",
          after["published"] == before["published"],
          f'{after["published"]} vs {before["published"]}')
    check("筛选与卡片同源：matched == pending 家底",
          after["matched"] == after["pending"],
          f'{after["matched"]} vs {after["pending"]}')
    check("筛选后列表里确实全是 pending",
          all(x["status"] == "pending" for x in filt["items"]),
          [x["status"] for x in filt["items"]][:5])

    # 置顶件必须出现在第一页（分页不能把置顶挤出视线）
    pin = q("?pin=1")
    if pin["counts"]["matched"]:
        first = q("?size=5")
        check("置顶件排在第一页",
              any(x["pinned"] for x in first["items"]),
              [x["pinned"] for x in first["items"]])

# ======================================================================
print("\n[E] 页面上的分页控件")
check("列表页有上一页/下一页/页码", all(k in notices_src for k in
      ('id="pgPrev"', 'id="pgNext"', 'id="pgInfo"')))
check("列表页显示命中条数", 'id="listHint"' in notices_src)
check("页码进网址（返回时还在原页）",
      "get('page')" in notices_src and "set('page'" in notices_src)
check("换筛选回到第 1 页", re.search(r"PAGE = 1;", notices_src) is not None)
check("用 replaceState 而不是 push",
      "history.replaceState" in notices_src and "pushState" not in notices_src)
check("分页条样式存在", ".pgbar{" in css_src)
check("手机分页按钮够大（≥40px）",
      re.search(r"@media \(max-width:640px\)[\s\S]*?\.pgbar \.btn\{min-height:40px",
                css_src) is not None)

# ======================================================================
print("\n[F] 渲染后的内联脚本能编译")
node = shutil.which("node") or r"C:\Users\10517\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"
if not pathlib.Path(node).exists() and not shutil.which(node):
    print("  [SKIP] 本机没有 node，跳过语法检查")
else:
    import tempfile
    import subprocess

    SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)
    bad = []
    tmp = tempfile.mkdtemp(prefix="smoke_fe_")
    with app.test_client() as c:
        login(c, "admin", "admin123")
        # 新增页面必须一起编：内联脚本语法错 = 整块不执行 = 页面空白，
        # 而服务端渲染照样 200，光看状态码发现不了。
        pages = ["/", "/tasks", "/orders", "/ledgers", "/notices", "/kb",
                 "/reports", "/audit", "/system", "/admin/users",
                 "/docs", "/weekly", "/security/policy",
                 "/process", "/risk", "/trace", "/persons"]
        for url in pages:
            html = c.get(url).get_data(as_text=True)
            for i, body in enumerate(SCRIPT.findall(html)):
                if not body.strip():
                    continue
                f = pathlib.Path(tmp) / f"p{i}.js"
                f.write_text(body, encoding="utf-8")
                r = subprocess.run([node, "--check", str(f)],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    bad.append(f"{url} script#{i}: {r.stderr.strip().splitlines()[-1][:90]}")
    check(f"{len(pages)} 个内部页面的内联脚本全部可编译", not bad, bad[:3])

    # 居民端是独立布局（portal/base.html），它自己的 request 封装与转义函数
    # 都在这一套模板里，内部站那 12 个页面覆盖不到。居民端一旦脚本挂了，
    # 表现是"页面能打开、按钮没反应"，用户只会觉得系统坏了。
    bad2 = []
    with app.test_client() as pc:
        for url in ["/portal", "/portal/report", "/portal/notices", "/portal/query"]:
            html = pc.get(url).get_data(as_text=True)
            for i, body in enumerate(SCRIPT.findall(html)):
                if not body.strip():
                    continue
                f = pathlib.Path(tmp) / f"m{i}.js"
                f.write_text(body, encoding="utf-8")
                r = subprocess.run([node, "--check", str(f)],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    bad2.append(f"{url} script#{i}: {r.stderr.strip().splitlines()[-1][:90]}")
    check("4 个居民端页面的内联脚本全部可编译", not bad2, bad2[:3])

print()
print("=" * 66)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
