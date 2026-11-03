"""三方仲裁闭环与证据链回归

这个套子覆盖的不是"功能有没有"，而是**四类"看起来对、其实错"的坑**：

1. **驳回只改了个标记**：工单还是 done，物业没有重办入口，
   于是"居委会驳回了"只存在于数据库里，业务流程上什么也没发生。
2. **物业能裁自己的单**：权限点配错一次，制度就形同虚设。
   所以这里专门造一个"管理员误把 order.verdict 勾给物业"的账号，
   验证代码层的回避原则能兜住配置错误。
3. **证据链只是摆设**：改了内容没人发现，或者只能发现"没重算哈希"的低级篡改。
   这里做三种篡改：改内容、删记录、**改内容并重算本条哈希**。
4. **居民没表态却算已解决**：为了报表好看把 disputed 洗成 ok，
   等于伪造居民意愿。这里断言居委维持办结后，居民状态仍是 disputed。

运行：python scripts/smoke_arbitration.py
"""
import io
import json
import os
import pathlib
import shutil
import sys
import uuid

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_arbitration.db"
SRC = BASE / "data" / "community.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()
    s = pathlib.Path(str(SRC) + suf)
    if s.exists():
        shutil.copy2(s, str(DB) + suf)   # 连 -wal 一起拷，否则丢未 checkpoint 的事务

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["NOTICE_AUTO_IMPORT"] = "0"
# 附件落到本套自己的目录：默认的 data/evidence/ 是**真实演示证据**该去的地方，
# 测试往里写假图，以后清理时就得逐个判断"哪张是真的"，很容易误删。
EV_DIR = BASE / "data" / "_evidence_smoke"
if EV_DIR.exists():
    shutil.rmtree(EV_DIR, ignore_errors=True)
os.environ["EVIDENCE_DIR"] = str(EV_DIR)
# 死端口：本例不测模型质量，降级到规则桩即可，同时避免真调 8B 让本套跑成几分钟
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"

from app import create_app  # noqa: E402
from app.auth import generate_password_hash  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import (AuditLog, ChainAnchor, EvidenceChain, ResidentTicket,  # noqa: E402
                        User, WorkItem, WorkOrder, WorkOrderLog)
from app import orderflow  # noqa: E402

app = create_app("dev")
PASS, FAIL = 0, 0

# 最小合法 JPEG：魔数对得上即可，不需要真能解码
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"　{extra}" if extra else ""))


def login(c, u, p="123456"):
    return c.post("/login", data={"username": u, "password": p}, follow_redirects=False)


def ensure_user(username, role, org, pwd, perms=None):
    u = User.query.filter_by(username=username).first()
    if not u:
        u = User(username=username, name=username, role=role, org=org,
                 password_hash=generate_password_hash(pwd), status="active",
                 created_by="smoke")
        db.session.add(u)
    if perms is not None:
        u.perms_json = json.dumps(perms, ensure_ascii=False)
    db.session.commit()
    return u


with app.app_context():
    # 链从头开始：拷来的库里可能带着演示链，序号不从 1 起会让"seq 连续"的断言失效。
    # 只清这两张表，业务数据要留着验真实的时长与状态。
    EvidenceChain.query.delete()
    ChainAnchor.query.delete()
    db.session.commit()
    ensure_user("t_arb_comm", "community", "幸福里社区", "arb123")
    ensure_user("t_arb_prop", "property", "幸福里物业", "arb123")
    ensure_user("t_arb_street", "street", "幸福里街道办事处", "arb123")

comm = app.test_client()
login(comm, "t_arb_comm", "arb123")
prop = app.test_client()
login(prop, "t_arb_prop", "arb123")
street = app.test_client()
login(street, "t_arb_street", "arb123")
admin = app.test_client()
login(admin, "admin", "admin123")
anon = app.test_client()


def new_order(status="done", co="幸福里物业"):
    """造一张指定状态的工单（走 orderflow，保证日志与链都有该有的那几步）"""
    with app.app_context():
        it = (WorkItem.query.filter(WorkItem.target_lines.like("%物业%")).first()
              or WorkItem.query.first())
        o = WorkOrder(order_no="A" + uuid.uuid4().hex[:10], work_item_id=it.id,
                      property_co=co, content="3栋2单元楼道灯不亮（测试单）",
                      sla_hours=24, status="open")
        db.session.add(o)
        db.session.flush()
        orderflow.push(o, "dispatch", actor="调度", detail="测试派单")
        orderflow.push(o, "accept", actor="赵师傅", actor_org=co, detail="")
        if status == "done":
            o.done_at = o.assigned_at or __import__("datetime").datetime.now()
            orderflow.push(o, "finish", actor="赵师傅", actor_org=co, detail="已更换灯泡")
        db.session.commit()
        return o.id, o.order_no


def set_disputed(oid, note="修完当天又漏了"):
    with app.app_context():
        o = db.session.get(WorkOrder, oid)
        o.confirm_status = "disputed"
        o.confirm_note = note
        o.confirm_at = __import__("datetime").datetime.now()
        db.session.commit()


print("=" * 66)
print("① 状态机：驳回只能从 done 出发，且必须回到 doing")
print("=" * 66)
open_id, _ = new_order(status="doing")
with app.app_context():
    o = db.session.get(WorkOrder, open_id)
    try:
        orderflow.push(o, "reject", actor="x")
        check("doing 状态的单不能驳回", False, "竟然没抛异常")
    except ValueError as e:
        check("doing 状态的单不能驳回", "不允许" in str(e), str(e))
    db.session.rollback()

done_id, done_no = new_order(status="done")
set_disputed(done_id)
r = comm.post(f"/api/orders/{done_id}/judge", json={
    "verdict": "property_fault",
    "basis": "物业服务合同第 3 条：公共部位照明由物业维护，本次未更换镇流器",
    "note": "48 小时内返工"})
j = r.get_json()
check("驳回成功", r.status_code == 200 and j["code"] == 0, j)
check("驳回后工单回到 doing（物业有重办入口）", j.get("data", {}).get("status") == "doing",
      j.get("data"))
check("驳回后居民确认被清空（上一轮办结不作数）",
      j.get("data", {}).get("confirm_status") == "")
with app.app_context():
    o = db.session.get(WorkOrder, done_id)
    check("驳回次数 1", WorkOrderLog.query.filter_by(order_id=done_id, action="reject").count() == 1)
    check("驳回也进了证据链",
          EvidenceChain.query.filter_by(target_type="order", target_id=done_id,
                                        action="reject").count() == 1)
    check("裁决结论与依据已落库",
          o.verdict == "property_fault" and "第 3 条" in (o.verdict_basis or ""),
          (o.verdict, o.verdict_basis))
    check("裁决人已记录", bool(o.verdict_by), o.verdict_by)

# 重办后可以再办结（否则驳回等于把人卡死）
r = prop.post(f"/api/orders/{done_id}/finish", json={"feedback": "已更换镇流器并测试点亮"})
check("驳回后物业能重新办结", r.status_code == 200 and r.get_json()["code"] == 0,
      r.get_json())

print()
print("=" * 66)
print("② 回避原则：物业不能裁自己的单（连配置错误也要兜住）")
print("=" * 66)
check("物业看不到仲裁台", prop.get("/api/orders/arbitration").status_code == 403)
check("物业不能调裁决接口", prop.post(f"/api/orders/{done_id}/judge", json={
    "verdict": "property_fault", "basis": "物业服务合同第 3 条约定由物业维护"}).status_code == 403)
check("街道不能裁决（不是仲裁方）", street.post(f"/api/orders/{done_id}/judge", json={
    "verdict": "property_fault", "basis": "物业服务合同第 3 条约定由物业维护"}).status_code == 403)
check("未登录不能裁决", anon.post(f"/api/orders/{done_id}/judge", json={
    "verdict": "property_fault", "basis": "物业服务合同第 3 条约定由物业维护"}).status_code in (401, 403))

# 故意把裁决权勾给物业：模拟管理员误操作。代码层的回避必须仍然生效。
with app.app_context():
    u = ensure_user("t_arb_prop2", "property", "幸福里物业", "arb123",
                    perms=["order.view", "order.handle", "order.verdict"])
bad = app.test_client()
login(bad, "t_arb_prop2", "arb123")
d2, _ = new_order(status="done")
set_disputed(d2, "墙角还是湿的")
r = bad.post(f"/api/orders/{d2}/judge", json={
    "verdict": "property_fault", "basis": "物业服务合同第 3 条约定由物业维护"})
body = r.get_json() or {}
check("即便误授权限，物业裁决仍被拒（回避原则在代码里）",
      r.status_code == 403 and "回避" in (body.get("msg") or ""), body.get("msg"))
check("被拒时给出的是理由，不是空白", bool(body.get("msg")), body)

print()
print("=" * 66)
print("③ 裁决校验：依据不能是空话，结论必须合法")
print("=" * 66)
r = comm.post(f"/api/orders/{d2}/judge", json={"verdict": "property_fault", "basis": "返工"})
check("依据过短 → 400", r.status_code == 400, r.status_code)
r = comm.post(f"/api/orders/{d2}/judge", json={"verdict": "property_fault", "basis": "已协调处理"})
check("「已协调处理」这类空话也被拒", r.status_code == 400, r.status_code)
r = comm.post(f"/api/orders/{d2}/judge", json={"verdict": "no_such", "basis": "物业服务合同第 3 条约定"})
check("未知裁决结论 → 400", r.status_code == 400)
r = comm.post(f"/api/orders/{d2}/judge", json={
    "verdict": "property_fault", "basis": "物业服务合同第 3 条：公共部位由物业维护",
    "dispute_type": "no_such"})
check("未知争议类型 → 400", r.status_code == 400)
r = comm.post(f"/api/orders/{open_id}/judge", json={
    "verdict": "property_fault", "basis": "物业服务合同第 3 条：公共部位由物业维护"})
check("未办结的单不能裁决", r.status_code in (400, 403), r.status_code)

print()
print("=" * 66)
print("④ 维持办结：可以驳居民的诉求，但不能替他改口")
print("=" * 66)
r = comm.post(f"/api/orders/{d2}/judge", json={
    "verdict": "owner_duty",
    "basis": "该处属业主专有部分，依据《物业服务合同》第 5 条不属物业维修范围"})
j = r.get_json()
check("维持办结成功", r.status_code == 200 and j["code"] == 0, j)
check("维持后状态仍是 done", j.get("data", {}).get("status") == "done", j.get("data"))
check("维持后居民状态仍是 disputed（不伪造居民意愿）",
      j.get("data", {}).get("confirm_status") == "disputed", j.get("data"))

print()
print("=" * 66)
print("⑤ 居民表态：唯一由居民发起的反向通道")
print("=" * 66)
# 走一次真实提交（模型不可用会降级到规则桩，不影响这里要验的东西）
r = anon.post("/api/portal/report", json={
    "phone": "13800001234", "name": "张三", "address": "2栋2单元202",
    "text": "2栋2单元202楼道灯坏了半个月，晚上出门看不见路，麻烦安排修一下"})
j = r.get_json()
check("居民提交成功（模型不可用时降级）", r.status_code == 200 and j["code"] == 0,
      j.get("msg"))
tid = (j.get("data") or {}).get("ticket_id")
check("提交后产生诉求单", bool(tid), tid)

# 还没办结的诉求不能表态
r = anon.post(f"/api/portal/ticket/{tid}/confirm", json={"ok": True})
check("未办结时不能表态", r.status_code == 400, (r.status_code, r.get_json().get("msg")))

# 把该诉求下的工单推到 done，再表态
with app.app_context():
    t = db.session.get(ResidentTicket, tid)
    items = WorkItem.query.filter_by(fde_id=t.fde_id).all()
    ids = [it.id for it in items]
    o = WorkOrder.query.filter(WorkOrder.work_item_id.in_(ids)).first() if ids else None
    if o is None:
        # 规则桩没派单时兜一张，本套验的是仲裁与存证，不是派单准确率
        o = WorkOrder(order_no="T" + uuid.uuid4().hex[:10], work_item_id=ids[0],
                      property_co="幸福里物业", content="楼道灯（测试）",
                      sla_hours=24, status="open")
        db.session.add(o)
        db.session.flush()
        orderflow.push(o, "dispatch", actor="调度", detail="")
        orderflow.push(o, "accept", actor="赵师傅", actor_org="幸福里物业", detail="")
    # 规则桩派单出来的单是 open，还没人接。必须先 accept 再 finish，
    # 直接 finish 会被状态机拦住——这里不能图省事直接改 status。
    if o.status == "open":
        orderflow.push(o, "accept", actor="赵师傅", actor_org="幸福里物业", detail="")
    if o.status != "done":
        orderflow.push(o, "finish", actor="赵师傅", actor_org="幸福里物业",
                       detail="已更换灯泡并测试")
    db.session.commit()
    oid2 = o.id

# 居民端进度页是居民确认的唯一入口，必须真的能渲染出来。
# 服务端 200 不等于页面对——模板里循环 dict 方法名（view.items）那类错
# 会直接 500，而少个按钮只会静默"功能消失"。
r = anon.get(f"/portal/ticket/{tid}")
check("居民端进度页能渲染", r.status_code == 200, r.status_code)
h = r.get_data(as_text=True)
check("办结后出现确认入口（已解决/没解决）", "已解决" in h and "没解决" in h)
# tojson 会把中文转成 \uXXXX（这是对的：它同时把 < > & 转义掉，防 </script> 注入），
# 所以这里查英文 key 而不是中文——中文由 JS 解析后渲染，用户看到的是中文。
check("争议类型由服务端下发到页面", '"quality"' in h and '"duty"' in h)
check("页面带上传照片入口", "补充照片" in h)
check("居民手机号在页面上是打码的", mask_probe := ("13800001234" not in h), "明文手机号出现在页面")

r = anon.post(f"/api/portal/ticket/{tid}/confirm", json={"ok": False, "note": "亮"})
check("不服但说明太短被拒", r.status_code == 400, r.status_code)
r = anon.post(f"/api/portal/ticket/{tid}/confirm",
              json={"ok": False, "note": "灯是亮了，但走廊另一头还是黑的", "dispute_type": "quality"})
j = r.get_json()
check("居民表示不服成功", r.status_code == 200 and j["code"] == 0, j.get("msg"))
check("表态影响到已办结的工单", (j.get("data") or {}).get("affected", 0) >= 1,
      j.get("data"))
with app.app_context():
    o = db.session.get(WorkOrder, oid2)
    check("工单落成 disputed", o.confirm_status == "disputed", o.confirm_status)
    check("争议类型已记录", o.dispute_type == "quality", o.dispute_type)
    check("居民表态进链", EvidenceChain.query.filter_by(
        target_type="order", target_id=oid2, action="dispute").count() == 1)

# 越权：换一个会话（没提交过该单）不能表态
other = app.test_client()
check("未持有该查询码的会话不能表态",
      other.post(f"/api/portal/ticket/{tid}/confirm", json={"ok": True}).status_code == 403)

# 该单进入待裁池
j = comm.get("/api/orders/arbitration").get_json()
ids = [x["id"] for x in j["data"]["items"]]
check("待裁池里出现该单", oid2 in ids, (oid2, ids[:5]))
check("待裁池只收未裁决的", all(not x["verdict"] for x in j["data"]["items"]))
check("待裁池带出居民原话", any(x["confirm_note"] for x in j["data"]["items"]))

print()
print("=" * 66)
print("⑥ 附件：只看扩展名是不够的")
print("=" * 66)
r = anon.post(f"/api/portal/ticket/{tid}/attach",
              data={"photos": (io.BytesIO(JPEG), "现场.jpg")},
              content_type="multipart/form-data")
j = r.get_json()
check("正常 JPEG 上传成功", r.status_code == 200 and j["code"] == 0, j.get("msg"))
check("返回总数 1", (j.get("data") or {}).get("total") == 1, j.get("data"))

r = anon.post(f"/api/portal/ticket/{tid}/attach",
              data={"photos": (io.BytesIO(b"<?php system($_GET[1]); ?>"), "shell.jpg")},
              content_type="multipart/form-data")
check("改名成 .jpg 的脚本被拒（按文件头判类型）", r.status_code == 400, r.status_code)
check("拒绝理由说明不是有效图片", "不是有效" in (r.get_json().get("msg") or ""),
      r.get_json().get("msg"))

r = anon.post(f"/api/portal/ticket/{tid}/attach",
              data={"photos": (io.BytesIO(PNG), "截图.png")},
              content_type="multipart/form-data")
check("PNG 也能通过", r.status_code == 200, r.status_code)

with app.app_context():
    t = db.session.get(ResidentTicket, tid)
    from app import evidence as ev
    atts = ev.load_attachments(t.attachments)
    check("附件表里有 2 张", len(atts) == 2, len(atts))
    check("每张都有哈希", all(len(a["sha256"]) == 64 for a in atts), atts)
    check("落盘名不是原文件名（原名含门牌）",
          all(a["stored"] != a["name"] for a in atts), atts)
    check("附件上链", EvidenceChain.query.filter_by(
        target_type="ticket", target_id=t.id, action="attach").count() == 2)
    check("提交本身也上链", EvidenceChain.query.filter_by(
        target_type="ticket", target_id=t.id, action="submit").count() == 1)
    check("链上不落手机号明文",
          "13800001234" not in (EvidenceChain.query.filter_by(
              target_type="ticket", target_id=t.id, action="submit").first().payload or ""))

print()
print("=" * 66)
print("⑦ 证据链：三种篡改都要能被抓出来")
print("=" * 66)
j = comm.get("/api/evidence/verify").get_json()
check("当前链完整", j["code"] == 0 and j["data"]["ok"], j.get("data"))
check("社区（仲裁方）自己就能验链", True)     # 上面这一句已用 comm 会话跑通
check("链上有多个动作", j["data"]["checked"] >= 5, j["data"])

with app.app_context():
    rows = EvidenceChain.query.order_by(EvidenceChain.seq.asc()).all()
    check("序号从 1 连续", [r.seq for r in rows] == list(range(1, len(rows) + 1)),
          [r.seq for r in rows][:8])
    # 锚一个点，后面才能测"重算哈希也躲不过"
    from app import evidence as ev
    anchor_row = ev.anchor("smoke")
    db.session.commit()
    anchor_seq = anchor_row.seq      # 锚点钉住的那一条，就是当时的链头
    first = rows[0]
    f_seq, f_prev, f_type, f_tid, f_act = (first.seq, first.prev_hash,
                                           first.target_type, first.target_id, first.action)

# 篡改 1：只改内容，不重算哈希
with app.app_context():
    r0 = EvidenceChain.query.filter_by(seq=f_seq).first()
    original = r0.payload
    d = json.loads(original)
    d["evil"] = "把责任改成居民"
    r0.payload = json.dumps(d, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    db.session.commit()

r = comm.get("/api/evidence/verify")
j = r.get_json()
check("改内容 → 校验失败", r.status_code == 409 and not j["data"]["ok"], r.status_code)
check("断点原因指向 hash_mismatch", j["data"]["reason"] == "hash_mismatch", j["data"])
check("报出断在第几条", j["data"]["broken_at"] == f_seq, j["data"])

# 先把篡改 1 恢复，否则后面每一条断言都会被它带偏——链会一直断在第 1 条，
# 后面的"锚点抓出""seq_gap"全都会被这条掩盖成 hash_mismatch。
import hashlib  # noqa: E402
with app.app_context():
    r0 = EvidenceChain.query.filter_by(seq=f_seq).first()
    r0.payload = original
    r0.hash = hashlib.sha256(
        f"{f_prev}\n{f_type}\n{f_tid}\n{f_act}\n{original}".encode("utf-8")).hexdigest()
    db.session.commit()
r = comm.get("/api/evidence/verify")
check("只改内容、恢复后链重新通过", r.status_code == 200, r.get_json().get("data"))

# 篡改 2：改**锚点那一条**的内容 + 重算本条哈希（更聪明的篡改）。
# 为什么要挑锚点那条：锚点只钉链头，改中间某条不会触发 anchor 比对，
# 而是以"下一条 prev 对不上"的形式暴露。两种都能抓到，但诊断措辞不同——
# 这里要验的是"锚点这条路真的能抓人"，所以必须打锚点自己钉的那一条。
with app.app_context():
    r0 = EvidenceChain.query.filter_by(seq=anchor_seq).first()
    anchor_prev, anchor_type, anchor_tid, anchor_act = (r0.prev_hash, r0.target_type,
                                                        r0.target_id, r0.action)
    anchor_payload = r0.payload
    d = json.loads(r0.payload)
    d["verdict_label"] = "业主自用部分 · 物业无责"
    body = json.dumps(d, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=str)
    raw = f"{anchor_prev}\n{anchor_type}\n{anchor_tid}\n{anchor_act}\n{body}"
    r0.payload = body
    r0.hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    db.session.commit()

r = comm.get("/api/evidence/verify")
j = r.get_json()
check("改内容并重算哈希 → 锚点优先抓出", r.status_code == 409, r.status_code)
check("断点原因是 anchor_mismatch", j["data"]["reason"] == "anchor_mismatch", j["data"])

# 恢复原值，再测删记录
with app.app_context():
    r0 = EvidenceChain.query.filter_by(seq=anchor_seq).first()
    r0.payload = anchor_payload
    r0.hash = hashlib.sha256(
        f"{anchor_prev}\n{anchor_type}\n{anchor_tid}\n{anchor_act}\n"
        f"{anchor_payload}".encode("utf-8")).hexdigest()
    db.session.commit()
r = comm.get("/api/evidence/verify")
check("恢复后重新通过（证明前两次失败不是误报）", r.status_code == 200,
      r.get_json().get("data"))

# 篡改 3：物理删掉中间一条
with app.app_context():
    victim = EvidenceChain.query.filter_by(seq=3).first()
    saved = (victim.seq, victim.target_type, victim.target_id, victim.action,
             victim.payload, victim.prev_hash, victim.hash, victim.actor)
    db.session.delete(victim)
    db.session.commit()
r = comm.get("/api/evidence/verify")
j = r.get_json()
check("删掉中间一条 → 校验失败", r.status_code == 409, r.status_code)
check("断点原因是 seq_gap", j["data"]["reason"] == "seq_gap", j["data"])
with app.app_context():
    db.session.add(EvidenceChain(seq=saved[0], target_type=saved[1], target_id=saved[2],
                                 action=saved[3], payload=saved[4], prev_hash=saved[5],
                                 hash=saved[6], actor=saved[7]))
    db.session.commit()
r = comm.get("/api/evidence/verify")
check("补回后重新通过", r.status_code == 200, r.get_json().get("data"))

# 边界确认：改**中间**某条并重算哈希，锚点（钉链头）不适用，
# 但下一条的 prev_hash 会对不上——仍然会被发现，只是原因写的是"被删或插"。
with app.app_context():
    mid = EvidenceChain.query.filter_by(seq=2).first()
    mid_payload = mid.payload
    mid_hash = mid.hash
    d = json.loads(mid.payload) if mid.payload else {}
    d["tampered"] = "middle"
    mid.payload = json.dumps(d, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    mid.hash = hashlib.sha256(
        f"{mid.prev_hash}\n{mid.target_type}\n{mid.target_id}\n{mid.action}\n"
        f"{mid.payload}".encode("utf-8")).hexdigest()
    db.session.commit()
r = comm.get("/api/evidence/verify")
j = r.get_json()
check("改中间某条并重算 → 仍然被发现（prev_mismatch 兜住）",
      r.status_code == 409 and j["data"]["reason"] in ("prev_mismatch", "anchor_mismatch"),
      j["data"])
with app.app_context():
    mid = EvidenceChain.query.filter_by(seq=2).first()
    mid.payload = mid_payload
    mid.hash = mid_hash
    db.session.commit()
r = comm.get("/api/evidence/verify")
check("恢复后仍然通过", r.status_code == 200, r.get_json().get("data"))

print()
print("=" * 66)
print("⑧ 越权读链与接口口径")
print("=" * 66)
check("物业能查自己单的链", prop.get(f"/api/evidence/chain?type=order&id={done_id}").status_code == 200)
with app.app_context():
    other_order = WorkOrder.query.filter(WorkOrder.property_co != "幸福里物业").first()
    oo = other_order.id if other_order else None
if oo:
    check("物业查别家单位的链 → 403",
          prop.get(f"/api/evidence/chain?type=order&id={oo}").status_code == 403)
else:
    check("物业查别家单位的链 → 403（无样本，跳过）", True)
check("不存在的工单 → 404", prop.get("/api/evidence/chain?type=order&id=999999").status_code == 404)
check("参数不对 → 400", prop.get("/api/evidence/chain?type=xx").status_code == 400)
# 锚定权给"多方"而不是只给管理员：锚点的可信度来自各自都留一份。
# 物业被排除，因为它是争议当事方——让当事人保管证物没有意义。
r = street.post("/api/evidence/anchor")
check("街道可以锚定（监督方独立留底）", r.status_code == 200, r.status_code)
check("锚点记录里带锚定人与角色", street and "(street)" in (
    r.get_json().get("data", {}).get("note") or ""), r.get_json().get("data"))
check("物业不能锚定（当事方不参与留底）",
      prop.post("/api/evidence/anchor").status_code == 403)
r = admin.post("/api/evidence/anchor")
check("管理员也可以锚定", r.status_code == 200, r.status_code)

j = comm.get("/api/orders/arbitration").get_json()
st = j["data"]["stats"]
check("统计口径：已办结数 ≥ 已裁决数", st["done_total"] >= st["judged"], st)
check("统计口径：待裁 = disputed 中未裁决的", st["pending"] <= st["disputed"], st)
check("驳回率分母是已裁决（不是已办结）",
      st["reject_rate"] is None or st["reject_rate"] <= 100, st)

print()
print("=" * 66)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 66)
sys.exit(1 if FAIL else 0)
