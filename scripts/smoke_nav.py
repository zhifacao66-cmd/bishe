"""回归：页面之间的连续性（返回、来源、待办计数）

这套测的不是业务规则，而是**导航**：
  1. 从列表点进详情、从详情点进办文，每一步都能原样退回原来那一屏；
  2. 返回参数不能变成开放重定向；
  3. "待签收"这个数字和下面列出来的条数必须对得上。

第 3 条是这轮发现的真问题：计数用的是全表，列表却按单位过滤，
于是"待签收 3"点进去只有 1 条。数字和明细对不上，比不显示更糟。

用独立库 + 关掉后台扫描线程，不碰 data/community.db。
"""
import os
import pathlib
import re
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_nav.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                        # noqa: E402
from app.extensions import db                      # noqa: E402
from app.models import Notice, User                # noqa: E402

app = create_app("dev")

# 这套测试用独立空库，先把要测的两条来文造出来：
#   id=1 待签收（测"签收 → 去办文"的引导）
#   id=2 已签收（测详情页的返回与时间线）
from datetime import date                          # noqa: E402
with app.app_context():
    db.session.add_all([
        Notice(id=1, title="关于开展冬季供暖设施检修的通知",
               issuer="幸福里街道办事处", level="街道", kind="superior",
               recv_status="dispatched", receiver_org="幸福里社区",
               status="pending", issued_at=date.today(),
               body_md="一、检修范围\n辖区所有集中供暖小区。", source_file="测试用"),
        Notice(id=2, title="关于开展高层住宅消防安全专项排查的通知",
               issuer="幸福里街道办事处", level="街道", kind="superior",
               recv_status="received", received_by="王社工",
               status="pending", issued_at=date.today(),
               body_md="一、排查范围\n辖区所有高层住宅。", source_file="测试用"),
    ])
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


def login(c, u, p):
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


def back_href(html):
    m = re.search(r'href="([^"]*)">← 返回列表', html)
    return m.group(1) if m else None


print("=" * 66)
print("页面连续性 · 返回链路 · 待办计数 回归")
print("=" * 66)

admin = app.test_client()
community = app.test_client()
street = app.test_client()

print("\n[1] 登录")
check("管理员", login(admin, "admin", "admin123").status_code == 302)
check("社区", login(community, "wang", "123456").status_code == 302)
check("街道", login(street, "jiedao", "123456").status_code == 302)

# ---------------------------------------------------------------- 2 返回参数
print("\n[2] 详情页返回参数：合法值原样带回")
r = community.get("/notices/1", query_string={"back": "/notices?recv=dispatched"})
h = back_href(r.get_data(as_text=True))
check("带回筛选条件", h == "/notices?recv=dispatched", str(h))
check("顶部与底部两处返回都在",
      r.get_data(as_text=True).count("← 返回列表") == 2,
      str(r.get_data(as_text=True).count("← 返回列表")))

r = community.get("/notices/1")
check("不带参数回落默认列表", back_href(r.get_data(as_text=True)) == "/notices",
      str(back_href(r.get_data(as_text=True))))

print("\n[3] 返回参数不能变成开放重定向")
for bad in ("//evil.com", "https://evil.com", "/\\evil.com",
            "javascript:alert(1)", "//evil.com/notices"):
    r = community.get("/notices/1", query_string={"back": bad})
    h = back_href(r.get_data(as_text=True))
    check(f"拒绝 {bad!r}", h == "/notices", str(h))

# ---------------------------------------------------------------- 4 办文页
print("\n[4] 办文页的取消与提交都延续同一条返回链")
r = community.get("/notices/1/localize",
                  query_string={"back": "/notices?recv=dispatched"})
html = r.get_data(as_text=True)
check("取消回原文详情，且带上来路",
      'href="/notices/1?back=/notices?recv%3Ddispatched"' in html,
      "未找到预期的取消链接")
check("提交后跳到落地件仍记住来路",
      'var backUrl = "/notices?recv=dispatched"' in html)
r = community.get("/notices/1/localize")
check("不带参数时取消回落原文详情",
      'href="/notices/1">取消，回到原文' in r.get_data(as_text=True))

# ---------------------------------------------------------------- 5 列表筛选
print("\n[5] 筛选条件进网址，刷新不丢")
r = community.get("/notices", query_string={"status": "pending"})
html = r.get_data(as_text=True)
# 不写死按钮数量：筛选会随功能增加（最近就多了"已置顶"），
# 数量断言每次都要跟着改，反而盖住真正要看的东西——到底是哪一档筛不见了
check("列表页筛选按钮齐全（全部/已发布/待确认/已下架/待签收/已置顶）",
      all(f'data-filter="{f}"' in html for f in
          ("", "status:published", "status:pending", "status:offline",
           "recv:dispatched", "pin:1")))
check("含待签收筛选", 'data-filter="recv:dispatched"' in html)
check("详情链接带 back 参数", "detailHref" in html)
check("滚动位置按屏记忆", "notices:scroll:" in html)

# ---------------------------------------------------------------- 6 待办计数
print("\n[6] 待签收计数必须与明细一致")
with app.app_context():
    # 造一个"别家社区"：一份指定给它的来文不该算进幸福里社区的待办
    other = User(username="xingfu2", name="赵社工", org="幸福里二社区",
                 role="community", status="active",
                 password_hash="x")
    db.session.add(other)
    db.session.add(Notice(
        title="指定给二社区的来文", issuer="幸福里街道办事处",
        level="街道", kind="superior", recv_status="dispatched",
        receiver_org="幸福里二社区", status="pending",
        body_md="正文", source_file="测试",
    ))
    db.session.commit()

j = community.get("/api/notices").get_json()["data"]
todo_count = j["counts"]["todo"]
listed = len(community.get("/api/notices",
                           query_string={"recv": "dispatched"}).get_json()["data"]["items"])
check("计数与明细条数一致", todo_count == listed, f"计数 {todo_count} / 明细 {listed}")
check("别家社区的待办不算进来", todo_count == 1, str(todo_count))

j2 = street.get("/api/notices").get_json()["data"]
check("街道看全局督办数（含两家）", j2["counts"]["todo"] == 2, str(j2["counts"]["todo"]))

r = community.get("/notices/1")
check("详情页的「还有 N 条」与计数同源", "还有 <b>1</b> 条" in r.get_data(as_text=True),
      "未找到待办提示")

print("\n[7] 别家社区的来文点进去签不了")
with app.app_context():
    nid = Notice.query.filter_by(title="指定给二社区的来文").first().id
r = community.post(f"/api/notices/{nid}/receive")
check("非指定单位签收被拒 403", r.status_code == 403, f"HTTP {r.status_code}")

# ---------------------------------------------------------------- 8 引导
print("\n[8] 签收与发布后都有下一步引导")
html = community.get("/notices/1").get_data(as_text=True)
check("签收后问「现在去办文」", "现在去办文" in html)
check("发布后问「接着处理待签收」", "现在去处理" in html or "接着处理待签收" in html)
check("签收引导带上办文页与来路",
      '/notices/1/localize?back=' in html)

print("\n" + "=" * 66)
print(f"通过 {PASS} 项 / 失败 {FAIL} 项")
if FAILED:
    print("失败项：")
    for x in FAILED:
        print("  -", x)
print("=" * 66)

for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass

sys.exit(1 if FAIL else 0)
