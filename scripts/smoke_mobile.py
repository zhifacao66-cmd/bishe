"""回归：窄屏（手机）能不能真的用

背景：桌面版是给电脑配的——侧栏固定 230px、正文 12 栏网格、9 张表没有滚动
容器。在 375px 的手机上正文只剩约 145px，比一个汉字表还窄；表格把整页撑出
横向滚动条，连侧栏都跟着漂。而"有些用户就只有手机"。

这套断言钉住的不是"好不好看"，而是三类会让手机端直接不可用的结构性错误：

  ① **侧栏没有抽屉形态**：手机上必须能靠汉堡打开，而且默认在屏幕外。
     漏了这条，正文宽度就永远是"屏宽 - 230"。
  ② **表格没被 .tw 包住**：表格必然超宽，裸 table 会把 document 撑宽。
  ③ **行内 grid-column / grid-template-columns 残留**：行内样式外部 CSS
     压不动，窄屏断点对它无效，单列布局会生成隐式列继续撑宽页面。

这些都是"改了模板就可能退回去"的东西，靠人眼在电脑上看不出来，
所以放进回归套件。真机视觉验收另有一套 playwright 脚本（_shot_mobile.js）。
"""
import pathlib
import re
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import os

DB = BASE / "data" / "smoke_mobile.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                                       # noqa: E402

app = create_app("dev")
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
        print(f"  ✗ {name}" + (f"  [{detail}]" if detail else ""))


def login(c):
    c.get("/logout")
    return c.post("/login", data={"username": "admin", "password": "admin123"},
                  follow_redirects=False)


CSS = (BASE / "app" / "static" / "css" / "style.css").read_text(encoding="utf-8")
# 先剥注释再断言：注释里常写着"别写成 .modal{display:flex}"，照着原文搜
# 会把反面教材当成正面事实，断言就永远在报假问题。
CSS_NC = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
# 只压规则体里的空白，**不能碰选择器**：`.a .b` 和 `.a.b` 是两种不同的匹配
CSS_FLAT = re.sub(r"\{([^}]*)\}", lambda m: "{" + re.sub(r"\s+", "", m.group(1)) + "}", CSS_NC)

# 登录后能打开的页面（含只有管理员看得到的 system / admin.users）
PAGES = ["/", "/tasks", "/orders", "/ledgers", "/kb", "/notices",
         "/reports", "/audit", "/system", "/admin/users"]

print("[1] 样式表：断点与关键规则在位")
check("900px 断点存在", "@media (max-width:900px)" in CSS_NC)
check("640px 断点存在", "@media (max-width:640px)" in CSS_NC)
check("尊重 prefers-reduced-motion", "prefers-reduced-motion" in CSS_NC)
check("表格滚动容器有横向滚动",
      ".tw{overflow-x:auto" in CSS_FLAT, "缺 .tw{overflow-x:auto}")
check("表格有可滑动提示（内阴影）",
      "inset -16px" in CSS_FLAT or "inset-16px" in CSS_FLAT)
# 单列网格里 span 7 会生成隐式列把页面撑宽，必须有压平规则
check("跨栏被压平（防隐式列）", ".content>*{grid-column:1/-1" in CSS_FLAT)
check("统计卡窄屏两列", ".content>.card.col-3" in CSS_FLAT)
# iOS 上输入框小于 16px 会触发整页放大且不还原
check("输入框 16px 防 iOS 放大",
      ".inp,.fde-input,input,select,textarea{font-size:16px;" in CSS_FLAT)
check("触摸目标抬高到 40px", ".btn{min-height:40px" in CSS_FLAT)
check("抽屉位移用 class 驱动",
      ".nav-open .sidebar{transform:none" in CSS_FLAT, "选择器丢了空格会变成另一条规则")
check("抽屉打开时锁背景滚动", ".nav-open{overflow:hidden" in CSS_FLAT)
check("触屏不残留 hover", "@media (hover:none)" in CSS_NC)
check("底部安全区（Home 条）", "safe-area-inset-bottom" in CSS)

print("\n[2] 骨架：抽屉三件套")
with app.test_client() as c:
    login(c)
    html = c.get("/").get_data(as_text=True)
check("有汉堡按钮", 'id="navToggle"' in html)
check("有遮罩层", 'id="sideMask"' in html)
check("侧栏可被 JS 定位", 'id="sidebar"' in html)
check("侧栏有无障碍标签", 'aria-label="主导航"' in html)
check("汉堡带 aria-expanded", 'aria-expanded="false"' in html)
check("允许刘海屏铺满", "viewport-fit=cover" in html)
check("有 theme-color（状态栏配色）", 'name="theme-color"' in html)
check("点导航会收起抽屉",
      "closest('a')" in html and "setOpen(false)" in html)
check("Esc 能关抽屉", "Escape" in html)
check("转屏跨断点会复位", "innerWidth > 900" in html)

print("\n[3] 每个页面的表格都进了滚动容器")
with app.test_client() as c:
    login(c)
    for url in PAGES:
        r = c.get(url)
        if r.status_code != 200:
            check(f"{url} 可访问", False, f"http {r.status_code}")
            continue
        page = r.get_data(as_text=True)
        n_table = page.count("<table")
        n_wrapped = len(re.findall(r'class="tw">\s*<table', page))
        check(f"{url} 表格已包裹（{n_wrapped}/{n_table}）", n_table == n_wrapped,
              "有裸 table，窄屏会被撑宽")

print("\n[4] 模板里不许再有行内栅格")
# 行内 style 的 grid-* 外部 CSS 压不动，窄屏断点对它完全无效
bad = []
for f in sorted((BASE / "app" / "templates").glob("*.html")):
    txt = f.read_text(encoding="utf-8")
    for i, line in enumerate(txt.splitlines(), 1):
        # grid-column:1/-1 是"占满一行"，窄屏下本来就对；要禁的是写死跨几栏的那种
        if re.search(r'style="[^"]*grid-column\s*:\s*span', line) or \
           re.search(r'style="[^"]*grid-template-columns', line):
            bad.append(f"{f.name}:{i}")
check("无行内跨栏/多列栅格", not bad, ", ".join(bad[:4]))

print("\n[5] 各页统计卡用的是栅格类（不是行内 span）")
with app.test_client() as c:
    login(c)
    for url in ["/", "/orders", "/ledgers", "/audit", "/notices", "/system", "/admin/users"]:
        page = c.get(url).get_data(as_text=True)
        has_col = re.search(r'class="[^"]*\bcard\b[^"]*\bcol-[34]\b', page) is not None
        check(f"{url} 统计卡带 col-3/col-4", has_col)

print("\n[6] 居民端本来就是移动优先，别被改坏")
portal_css = (BASE / "app" / "static" / "css" / "portal.css").read_text(encoding="utf-8")
check("居民端有底部导航", ".p-nav" in portal_css)
check("居民端有安全区", "env(safe-area-inset-bottom)" in portal_css)
with app.test_client() as c:
    r = c.get("/portal")
    check("居民端可访问", r.status_code == 200, f"http {r.status_code}")
    ph = r.get_data(as_text=True) if r.status_code == 200 else ""
check("居民端 viewport-fit", "viewport-fit=cover" in ph)

print("\n[7] 弹窗在窄屏铺满，且默认是关着的")
check("窄屏 .modal-box 强制满宽", "max-width:100%!important" in CSS_FLAT)
# `.modal` 本身是 display:none，页面内 <style> 比 style.css 晚加载、同优先级必胜。
# 如果这里写成 .modal{display:flex}，所有弹窗会一进页面就全部展开。
check("没有把 .modal 写成默认 flex",
      not re.search(r"\.modal\{[^}]*display://s*flex", CSS_NC), "会把弹窗默认打开")
check("只改 .modal.show 的定位", ".modal.show{align-items:flex-end" in CSS_FLAT)

print("\n[8] 图表在尺寸变化后会重算")
idx = (BASE / "app" / "templates" / "index.html").read_text(encoding="utf-8")
rep = (BASE / "app" / "templates" / "reports.html").read_text(encoding="utf-8")
check("首页图表监听 resize", "addEventListener('resize'" in idx)
check("报表图表监听 resize", "addEventListener('resize'" in rep)
check("报表重算只挂一次监听", "__repChartResize" in rep)

print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败清单：")
    for n in FAILED:
        print("  -", n)
sys.exit(1 if FAIL else 0)
