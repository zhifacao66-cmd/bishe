"""居民端 + 公告模块冒烟测试

覆盖四类风险，共 26 项：
  1. 居民端可访问性（未登录、移动布局页、越权隔离）
  2. 填报 → 查询码 → 进度查询 的完整闭环
  3. 公告解析（md/docx/txt）→ 自动发布 / 待确认 → 打印版与发布版
  4. 四角色权限矩阵（谁能看、谁能导入、谁能发布）

用独立库 + 指向不可用的模型地址：
  - 独立库避免污染 data/community.db
  - 模型地址指到 127.0.0.1:9 强制走规则降级，这样测试是确定性的、不依赖 GPU
    （要验证模型路径请跑 scripts/eval_e2e.py，那是另一回事）
"""
import os
import sys
import pathlib

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_portal.db"
if DB.exists():
    DB.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"   # 必然连不上 → 规则降级
os.environ["NOTICE_AUTO_IMPORT"] = "1"

from app import create_app            # noqa: E402
from app.extensions import db         # noqa: E402
from app.models import Notice, ResidentTicket, WorkOrder  # noqa: E402
from app import ratelimit
from app.api import portal as portal_api                   # noqa: E402

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


def login(c, u, p="123456"):
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


def main():
    c = app.test_client()
    ratelimit.reset()

    print("\n[1] 居民端可访问性")
    for path in ["/portal", "/portal/report", "/portal/query", "/portal/my",
                 "/portal/notices", "/portal/contacts"]:
        r = c.get(path)
        check(f"GET {path} = 200", r.status_code == 200, r.status_code)
    r = c.get("/portal")
    html = r.get_data(as_text=True)
    check("首页含工作人员入口", "工作人员入口" in html)
    check("首页引用独立样式 portal.css", "portal.css" in html)
    check("首页不引用内部端 style.css", "css/style.css" not in html)

    r = c.get("/login")
    check("登录页含居民入口", "我是居民" in r.get_data(as_text=True))

    print("\n[2] 公告自动导入（三种格式）")
    with app.app_context():
        total = Notice.query.count()
        pub = Notice.query.filter_by(status="published").count()
        pen = Notice.query.filter_by(status="pending").count()
        docx_n = Notice.query.filter(Notice.source_file.like("%.docx")).count()
        txt_n = Notice.query.filter(Notice.source_file.like("%.txt")).count()
    check(f"导入 {total} 条（≥4）", total >= 4, total)
    check(f"自动发布 {pub} 条", pub >= 3, pub)
    check(f"待确认 {pen} 条（缺发文单位那条）", pen == 1, pen)
    check("含 .docx 解析结果", docx_n == 1, docx_n)
    check("含 .txt 解析结果", txt_n == 1, txt_n)

    with app.app_context():
        n = Notice.query.filter(Notice.source_file.like("%.md"), Notice.status == "published").first()
        nid = n.id
        has_table = "|" in n.body_md and "---" in n.body_md
        tail = n.body_md.strip().splitlines()[-1]
        # 正文末尾不应残留落款（否则页面会显示两遍）
        no_sign = n.issuer not in tail and "年" not in tail[-12:]
        pending = Notice.query.filter_by(status="pending").first()
        pid = pending.id
    check("Markdown 表格进正文", has_table)
    check("落款已从正文剔除", no_sign, tail[:30])

    print("\n[3] 输出：网页版 / 打印版 / 发布版")
    r = c.get(f"/portal/notice/{nid}")
    check("网页版 200", r.status_code == 200)
    body = r.get_data(as_text=True)
    check("网页版含发布版/打印版入口", "发布版" in body and "打印版" in body)

    r = c.get(f"/portal/notice/{nid}?mode=official")
    off = r.get_data(as_text=True)
    check("发布版 200", r.status_code == 200)
    check("发布版有红头样式", 'class="redhead"' in off and "#d0021b" in off)
    check("发布版含发文字号", "幸街发" in off)
    check("发布版有打印触发", "window.print()" in off)
    check("发布版用仿宋公文正文字号 16px", "font-size: 16px" in off or "16px" in off)

    r = c.get(f"/portal/notice/{nid}?mode=print")
    pr = r.get_data(as_text=True)
    check("打印版 200", r.status_code == 200)
    check("打印版有 @page A4", "@page" in pr and "A4" in pr)
    check("打印版隐藏工具栏", ".toolbar { display: none" in pr)
    check("打印版带打印时间戳", "打印时间" in pr)

    r = c.get(f"/portal/notice/{pid}")
    check("待确认公告居民端 404", r.status_code == 404, r.status_code)
    r = c.get(f"/portal/notice/{nid}?mode=xxx")
    check("非法 mode 回落网页版", r.status_code == 200)

    print("\n[4] 居民填报 → 查询码 → 查进度")
    ratelimit.reset()
    r = c.post("/api/portal/report", json={
        "text": "2栋2单元202楼道灯坏了半个月，另外老人身份证要办高龄补贴，电话13800001111",
        "phone": "13800001111", "name": "王阿姨", "address": "2栋2单元202"})
    j = r.get_json()
    check("提交成功", j["code"] == 0, j.get("msg"))
    d = j["data"]
    code = d["code"]
    check("返回 6 位查询码", len(code) == 6 and code.isdigit(), code)
    check("拆出 2 个业务项", d["item_count"] == 2, d["item_count"])
    check("生成 1 条工单", d["order_count"] == 1, d["order_count"])
    check("生成 1 条台账", d["ledger_count"] == 1, d["ledger_count"])

    r = c.get(f"/api/portal/ticket?phone=13800001111&code={code}")
    j = r.get_json()
    check("凭码查询成功", j["code"] == 0, j.get("msg"))
    v = j["data"]
    check("返回手机号已打码", v["phone"] == "138****1111", v["phone"])
    check("事项带承办单位", all(it["orgs"] for it in v["items"]))
    check("不返回内部字段 fields_json", "fields_json" not in r.get_data(as_text=True))

    r = c.get("/api/portal/my")
    check("我的反映列表非空", r.get_json()["data"]["tickets"])
    tid = v["id"]
    r = c.get(f"/portal/done/{tid}")
    check("提交成功页 200", r.status_code == 200)
    check("成功页显示查询码", code in r.get_data(as_text=True))

    # 办理进度页必须真能渲染。早先模板写成 view.items，而 view 是 dict，
    # Jinja 会取到 dict 自带的 .items 方法 → for 循环一个内置方法 → 整页 500 JSON。
    # 这里按"页面有内容"断，不只看状态码：500 时返回的是 JSON，状态码也是 500 罢了。
    r = c.get(f"/portal/ticket/{tid}")
    html = r.get_data(as_text=True)
    check("办理进度页 200", r.status_code == 200, r.status_code)
    check("进度页渲染成 HTML 而非错误 JSON",
          "text/html" in r.headers.get("Content-Type", "") and "服务内部错误" not in html)
    check("进度页显示标题与状态", "办理进度" in html and "已受理" in html)
    check("进度页列出拆出的事项", "楼道灯" in html, html[:80])
    check("进度页状态用状态徽标而非业务线配色",
          "tag tag-open" in html and 'class="lv' not in html)

    # 换设备：没有本地会话时，凭手机号 + 查询码找回后要能进同一页
    c2 = app.test_client()
    r = c2.get(f"/api/portal/ticket?phone=13800001111&code={code}")
    check("新设备凭码查询成功", r.get_json()["code"] == 0)
    r = c2.get(f"/portal/ticket/{tid}")
    check("新设备能打开进度页", r.status_code == 200, r.status_code)

    # 没凭据不该看到别人的单子
    c3 = app.test_client()
    r = c3.get(f"/portal/ticket/{tid}")
    check("无会话直接访问进度页被拒", r.status_code == 404, r.status_code)

    r = c.get("/api/portal/ticket?phone=13800001111&code=000000")
    check("错误查询码被拒", r.get_json()["code"] != 0)
    r = c.get("/api/portal/ticket?phone=13900000000&code=" + code)
    check("手机号与码不匹配被拒", r.get_json()["code"] != 0)
    r = c.post("/api/portal/report", json={"text": "灯坏了需要修", "phone": "123"})
    check("手机号格式校验", r.get_json()["code"] != 0)
    r = c.post("/api/portal/report", json={"text": "灯坏", "phone": "13800001111"})
    check("内容过短校验", r.get_json()["code"] != 0)

    print("\n[5] 防刷限流")
    ratelimit.reset()
    codes = []
    for i in range(7):
        r = c.post("/api/portal/report", json={
            "text": f"测试限流第{i}条，楼道灯不亮需要维修", "phone": "13700000001"})
        codes.append(r.get_json()["code"])
    check("前 5 次放行", all(x == 0 for x in codes[:5]), codes)
    check("第 6 次起拦截", codes[5] != 0 and codes[6] != 0, codes[5:])

    print("\n[6] 四角色权限矩阵")
    ratelimit.reset()
    cases = [
        # (账号, 角色名, 期望 notice.view, 期望 scan 导入, 期望 publish 发布)
        ("wuye", "物业", True, False, False),
        ("jiedao", "街道", True, True, False),
        ("wang", "社区", True, False, True),
    ]
    for user, label, can_view, can_import, can_publish in cases:
        c2 = app.test_client()
        login(c2, user)
        r = c2.get("/notices")
        check(f"{label} 可查看公告页", (r.status_code == 200) == can_view, r.status_code)
        r = c2.post("/api/notices/scan", json={})
        check(f"{label} 导入上级文件 = {can_import}", (r.status_code == 200) == can_import,
              r.status_code)
        r = c2.post(f"/api/notices/{nid}/publish")
        check(f"{label} 发布公告 = {can_publish}", (r.status_code == 200) == can_publish,
              r.status_code)

    c3 = app.test_client()
    r = c3.get("/notices", follow_redirects=False)
    check("未登录访问公告页 → 302", r.status_code == 302, r.status_code)
    r = c3.get("/api/notices")
    check("未登录调公告接口 → 401", r.status_code == 401, r.status_code)

    print("\n[7] 待确认 → 补全 → 发布")
    c4 = app.test_client()
    login(c4, "wang")
    r = c4.post(f"/api/notices/{pid}/publish")
    # 业务拒绝走统一响应（HTTP 200 + code≠0），与 403 的权限拒绝区分开
    j = r.get_json()
    check("缺发文单位不能发布", j["code"] != 0 and "发文单位" in j.get("msg", ""), j.get("msg"))
    with app.app_context():
        check("被拒后状态未变", db.session.get(Notice, pid).status == "pending")
    r = c4.patch(f"/api/notices/{pid}", json={"issuer": "幸福里街道办事处"})
    check("补全发文单位成功", r.status_code == 200 and r.get_json()["code"] == 0)
    r = c4.post(f"/api/notices/{pid}/publish")
    check("补全后可发布", r.status_code == 200)
    r = c4.get(f"/portal/notice/{pid}")
    check("发布后居民端可见", r.status_code == 200)

    print("\n[8] 下架")
    r = c4.post(f"/api/notices/{pid}/offline")
    check("下架成功", r.status_code == 200)
    r = c4.get(f"/portal/notice/{pid}")
    check("下架后居民端 404", r.status_code == 404, r.status_code)
    with app.app_context():
        check("下架记录仍保留（可追溯）",
              db.session.get(Notice, pid).status == "offline")

    print("\n[9] 重复扫描去重")
    c5 = app.test_client()
    login(c5, "jiedao")
    r = c5.post("/api/notices/scan", json={})
    d = r.get_json()["data"]
    check("第二次扫描全部跳过", not d["imported"] and not d["pending"], d["msg"])

    print("\n[10] 数据留痕")
    with app.app_context():
        from app.models import AuditLog
        acts = {a.action for a in AuditLog.query.filter_by(agent="notice").all()}
        check("公告操作写审计", bool(acts), acts)
        check("含 publish 与 offline 留痕", {"publish", "offline"} <= acts, acts)
        t = ResidentTicket.query.first()
        check("居民提交落库", t is not None and t.fde_id is not None)
        o = WorkOrder.query.first()
        check("工单内容为脱敏摘要", o is not None and "13800001111" not in (o.content or ""),
              o.content if o else "")

    print(f"\n{'=' * 56}")
    print(f"通过 {PASS} / {PASS + FAIL}")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  -", f)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
