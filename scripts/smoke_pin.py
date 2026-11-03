"""回归：公告置顶

置顶看着只是"加个按钮"，其实牵四件事，缺一件就不严谨：
  1. **谁能置顶**——置顶决定居民先读到哪份文件，跟"发不发"是同一类责任，
     所以和发布/下架同权限（notice.manage），街道和物业都不该有；
  2. **多条置顶谁在前**——只有布尔值时，几条置顶会退化成按发稿时间排，
     后置顶的反而排在后面，所以要记 pinned_at；
  3. **置顶不等于发布**——置顶只改排序，未发布/已下架的置顶件居民端仍然看不到，
     这条如果漏了，就是绕过发布审核的暗门；
  4. **能收得回来**——置顶位置有限，必须有上限、有取消、有审计。

用独立库 + 关掉后台扫描线程，不碰 data/community.db。
"""
import os
import pathlib
import re
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_pin.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from datetime import date, datetime, timedelta          # noqa: E402

from app import create_app                              # noqa: E402
from app.api.notices import MAX_PINNED                  # noqa: E402
from app.extensions import db                           # noqa: E402
from app.models import AuditLog, Notice                 # noqa: E402

app = create_app("dev")

# 造 5 条：4 条已发布（排序与上限用）+ 1 条待确认（测"置顶≠发布"）
# created_at 故意拉开，方便验证"取消置顶后回到按时间排"
with app.app_context():
    base = datetime(2026, 10, 1, 9, 0)
    rows = []
    for i in range(1, 6):
        rows.append(Notice(
            id=i, title=f"测试公告 {i}", issuer="幸福里社区", level="社区",
            kind="original", status="published" if i <= 4 else "pending",
            issued_at=date(2026, 10, i), category="通知公告",
            body_md=f"一、事项\n这是第 {i} 条测试公告的正文。",
            summary=f"第 {i} 条", source_file="测试用",
            published_at=(base + timedelta(days=i)) if i <= 4 else None,
            created_at=base + timedelta(days=i), pinned=False,
        ))
    # id=3 是最新发的，按时间排序本来在第一
    db.session.add_all(rows)
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


def pin(c, nid, body=None):
    kw = {"json": body} if body is not None else {}
    return c.post(f"/api/notices/{nid}/pin", **kw)


def items(c, qs=""):
    j = c.get("/api/notices" + qs).get_json()
    return j["data"]["items"], j["data"]


print("=" * 66)
print("公告置顶 · 权限 / 排序 / 上限 / 可见性")
print("=" * 66)

anon = app.test_client()
community = app.test_client()
street = app.test_client()
property_ = app.test_client()

print("\n[1] 登录")
check("社区", login(community, "wang", "123456").status_code == 302)
check("街道", login(street, "jiedao", "123456").status_code == 302)
check("物业", login(property_, "wuye", "123456").status_code == 302)

# ---------------------------------------------------------------- 2 权限
print("\n[2] 权限：置顶跟着 notice.manage 走")
r = pin(anon, 1)
check("未登录 401", r.status_code == 401, f"HTTP {r.status_code}")
r = pin(street, 1)
check("街道不能置顶（403）", r.status_code == 403, f"HTTP {r.status_code}")
r = pin(property_, 1)
check("物业不能置顶（403）", r.status_code == 403, f"HTTP {r.status_code}")
r = pin(community, 1)
check("社区可以置顶", r.status_code == 200 and r.get_json()["code"] == 0,
      str(r.get_json())[:120])

r = community.get("/notices/1")
html = r.get_data(as_text=True)
check("详情页出现「取消置顶」按钮", "📌 取消置顶" in html)
check("详情页副标题标出置顶", "📌 置顶" in html)
html = community.get("/notices").get_data(as_text=True)
check("列表页有「已置顶」筛选", 'data-filter="pin:1"' in html)
check("列表页筛选按钮标出额度", 'id="cPin"' in html)

html = community.get("/notices/2").get_data(as_text=True)
check("详情页显示置顶额度", f"置顶 1/{MAX_PINNED}" in html,
      str(re.findall(r"置顶 \d+/\d+", html))[:60])

# ---------------------------------------------------------------- 3 切换语义
print("\n[3] 不传 pinned 时按当前状态取反（一个按钮两用）")
with app.app_context():
    n1 = db.session.get(Notice, 1)
    check("第一次调用后已置顶", bool(n1.pinned) is True)
    check("同时写下置顶时间", n1.pinned_at is not None)
    first_at = n1.pinned_at

j = pin(community, 1).get_json()
check("第二次调用取消置顶", j["data"]["pinned"] is False, str(j)[:120])
with app.app_context():
    n1 = db.session.get(Notice, 1)
    check("取消后 pinned_at 清空", n1.pinned_at is None)

j = pin(community, 1).get_json()
check("第三次又置顶", j["data"]["pinned"] is True)
with app.app_context():
    n1 = db.session.get(Notice, 1)
    check("重新置顶换了新时间戳", n1.pinned_at is not None and n1.pinned_at >= first_at)

print("\n[4] 幂等：重复置顶不写第二条审计")
with app.app_context():
    before = AuditLog.query.filter_by(agent="notice", action="pin").count()
j = pin(community, 1, {"pinned": True}).get_json()
check("明确要求置顶但已是置顶 → 提示", "已经是置顶" in j["data"]["msg"], j["data"]["msg"])
with app.app_context():
    after = AuditLog.query.filter_by(agent="notice", action="pin").count()
check("审计没有多出一条", after == before, f"{before} → {after}")

# ---------------------------------------------------------------- 4 排序
print("\n[5] 排序：置顶优先，多条置顶按置顶时间倒序")
pin(community, 4)
pin(community, 2)          # 最后置顶，应排第一
rows, meta = items(community)
order = [x["id"] for x in rows]
check("置顶件全部排在未置顶件之前",
      order[:3] == [2, 4, 1], f"实际 {order[:4]}")
check("最后置顶的排在第一位", order[0] == 2, f"实际 {order[0]}")
check("未置顶的按发布时间倒序",
      order[3:] == [5, 3], f"实际 {order[3:]}")

print("\n[6] 取消置顶后回到按时间排")
pin(community, 2, {"pinned": False})
pin(community, 4, {"pinned": False})
rows, _ = items(community)
order = [x["id"] for x in rows]
check("取消后只剩 id=1 置顶", order[0] == 1, f"实际 {order}")
check("其余按发布时间倒序", order[1:] == [5, 4, 3, 2], f"实际 {order[1:]}")

# ---------------------------------------------------------------- 5 上限
print(f"\n[7] 上限：最多 {MAX_PINNED} 条")
for nid in (2, 3, 4):
    pin(community, nid, {"pinned": True})
rows, meta = items(community)
pinned_ids = [x["id"] for x in rows if x["pinned"]]
check(f"正好置顶 {MAX_PINNED} 条", len(pinned_ids) == MAX_PINNED, f"实际 {pinned_ids}")
check("接口回报上限值", meta["max_pinned"] == MAX_PINNED)
j = pin(community, 5, {"pinned": True}).get_json()
check("第 4 条被拒绝", j["code"] != 0, str(j)[:120])
check("拒绝理由说清上限", "最多" in j["msg"], j["msg"])
check("拒绝时不悄悄挤掉已有的", len(pinned_ids) == MAX_PINNED)

# 满额时界面上要看得出来，不能等点了才知道
html = community.get("/notices/4").get_data(as_text=True)
m = re.search(r'<button[^>]*id="btnPin"[^>]*>', html)
check("满额时置顶按钮置灰", bool(m) and "disabled" in m.group(0), str(m)[:100])
check("页面说明为什么不能置顶", "已满，先取消一条" in html)
j = pin(community, 1, {"pinned": False}).get_json()
check("已置顶的仍可取消（不受上限影响）", j["code"] == 0, str(j)[:120])
j = pin(community, 5, {"pinned": True}).get_json()
check("腾出位置后可以再置顶", j["code"] == 0, str(j)[:120])

# ---------------------------------------------------------------- 6 计数与筛选
print("\n[8] 数字与明细同源")
rows, meta = items(community)
pinned_in_list = sum(1 for x in rows if x["pinned"])
check("counts.pinned 等于置顶条数", meta["counts"]["pinned"] == pinned_in_list,
      f'{meta["counts"]["pinned"]} vs {pinned_in_list}')
rows, _ = items(community, "?pin=1")
check("?pin=1 只返回置顶件", all(x["pinned"] for x in rows) and len(rows) == pinned_in_list,
      f"返回 {len(rows)} 条")

# ---------------------------------------------------------------- 7 可见性
print("\n[9] 置顶不等于发布（关键）")
with app.app_context():
    n5 = db.session.get(Notice, 5)
    check("id=5 是待确认状态", n5.status == "pending", n5.status)
    check("id=5 已被置顶", bool(n5.pinned) is True)
j = anon.get("/api/portal/notices").get_json()
ids = [x["id"] for x in j["data"]["items"]]
check("居民端看不到未发布的置顶件", 5 not in ids, f"实际 {ids}")

with app.app_context():
    n2 = db.session.get(Notice, 2)
    n2.status = "offline"
    db.session.commit()
j = anon.get("/api/portal/notices").get_json()
ids = [x["id"] for x in j["data"]["items"]]
check("居民端看不到已下架的置顶件", 2 not in ids, f"实际 {ids}")
check("居民端能看到已发布的置顶件", 3 in ids or 4 in ids, f"实际 {ids}")

# 未发布时置顶，接口要提醒
j = pin(community, 5, {"pinned": False}).get_json()
j = pin(community, 5, {"pinned": True}).get_json()
check("未发布件置顶时给出提醒", "居民端" in j["data"]["msg"], j["data"]["msg"])

# ---------------------------------------------------------------- 8 审计
print("\n[10] 审计留痕")
with app.app_context():
    pin_logs = AuditLog.query.filter_by(agent="notice", action="pin").all()
    unpin_logs = AuditLog.query.filter_by(agent="notice", action="unpin").all()
    check("有置顶审计", len(pin_logs) > 0, f"{len(pin_logs)} 条")
    check("有取消置顶审计", len(unpin_logs) > 0, f"{len(unpin_logs)} 条")
    check("审计写明是哪份公告",
          any("《测试公告" in (g.output_digest or "") for g in pin_logs),
          (pin_logs[0].output_digest or "")[:60])
    check("审计记了操作人", all(g.actor for g in pin_logs))

# ---------------------------------------------------------------- 9 补全表单
print("\n[11] 补全字段接口不能绕过时间戳")
with app.app_context():
    n3 = db.session.get(Notice, 3)
    n3.pinned = False
    n3.pinned_at = None
    db.session.commit()
j = community.patch("/api/notices/3", json={"pinned": True}).get_json()
check("PATCH pinned=true 成功", j["code"] == 0, str(j)[:120])
with app.app_context():
    n3 = db.session.get(Notice, 3)
    check("PATCH 也写了 pinned_at", n3.pinned_at is not None)

print("\n" + "=" * 66)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败项：")
    for x in FAILED:
        print("  -", x)
print("=" * 66)
sys.exit(1 if FAIL else 0)
