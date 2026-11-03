"""居民端接口：填报 / 查进度 / 看公告 / 找三级管理者

与内部接口的三点不同：
1. **没有登录态**。居民凭「手机号 + 6 位查询码」双因子查自己的单子，
   不建账号、不存密码，符合最小必要。
2. **返回字段收窄**。只给业务类型、承办单位、状态、时限、处理反馈；
   不返回 fields_json、不返回内部 id 链路、手机号一律打码。
3. **必须防刷**。这是公网公开入口，接口按 IP 做滑动窗口限流，
   否则一条脚本就能把首录表和模型队列打满。
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta

from flask import Blueprint, current_app, request, session

from app.api import fail, ok
from app.auth import ROLE_NAMES
from app import arbitration, evidence
from app.extensions import db
from app.itemstate import STAGE_RESIDENT, stage as item_stage
from app.models import (AuditLog, EvidenceChain, FdeRecord, Ledger, Notice,
                        ResidentTicket, WorkItem, WorkOrder)
from app.ratelimit import allow as _rate_ok

bp = Blueprint("portal", __name__)

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")

# 业务线 → 居民看得懂的承办单位
LINE_TO_ORG = {
    "物业": "幸福里物业",
    "综治": "社区综治",
    "民政": "社区民政",
    "卫健": "社区卫生",
    "街道": "街道办事处",
}

# 查询码锁定：连错 5 次锁 30 分钟
#
# 6 位数字码只有 10^6 种，光靠"每分钟 20 次"的限流挡不住——那是 35 天跑完，
# 但攻击者只盯一个手机号、每天跑满 20 次，一个月内仍有相当概率撞对，
# 而撞对一次的后果是**读到别人家的姓名、门牌、报修内容**。
# 所以必须有"错够次数就整段时间别再试"的机制。
#
# 计数落在审计表而不是内存字典上：内存随重启清零，攻击者只要等到
# 服务重启（部署更新、平台迁移）就能把计数洗掉，锁定形同虚设。
QUERY_FAIL_MAX = 5
QUERY_FAIL_WINDOW = 30 * 60          # 秒


def _phone_key(phone: str) -> str:
    """审计里的稳定键：手机号哈希，不落明文

    不用打码后的手机号当键：`138****8000` 这种三前一后一共 7 位，
    两个不同号码撞在一起就会"别人的失败把我锁了"。
    """
    return "portal:" + hashlib.sha256(phone.encode("utf-8")).hexdigest()[:16]


def _query_locked(phone: str) -> bool:
    cutoff = datetime.now() - timedelta(seconds=QUERY_FAIL_WINDOW)
    n = (AuditLog.query
         .filter(AuditLog.actor == _phone_key(phone),
                 AuditLog.action == "query_fail",
                 AuditLog.created_at >= cutoff)
         .count())
    return n >= QUERY_FAIL_MAX


def _note_query_fail(phone: str):
    db.session.add(AuditLog(actor=_phone_key(phone), agent="portal",
                            action="query_fail",
                            input_digest="查询码不匹配 · " + mask_phone(phone)))
    db.session.commit()


def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "unknown"


def mask_phone(p: str) -> str:
    return p[:3] + "****" + p[-4:] if p and len(p) == 11 else ""


def _gen_code() -> str:
    for _ in range(30):
        code = f"{secrets.randbelow(10 ** 6):06d}"
        if not ResidentTicket.query.filter_by(code=code).first():
            return code
    raise RuntimeError("查询码生成失败")


def _remember(ticket_id: int):
    """把单号记进本机会话，方便居民回来直接看（换设备仍可用手机号+码查询）"""
    ids = [i for i in session.get("portal_tickets", []) if i != ticket_id]
    ids.insert(0, ticket_id)
    session["portal_tickets"] = ids[:10]


def _ticket_payload(t: ResidentTicket) -> dict:
    """组装居民可见的进度视图

    状态判据已收口到 `app/itemstate.py`——居民端与首录台必须用同一套，
    否则同一个事项会出现"居民看到已办结、社区看到待派发"。

    居民只关心"办没办完"：已办结 / 处理中 / 已受理（已派单与未接单都算已受理）。

    `filed`（已进台账）必须算办结：民政/综治这些线**只写台账、不产生工单**，
    早先判据写成 `all(s == "done")`，这些事项永远到不了"已办结"——
    居民申请低保复核，社区早就归档了，居民端还显示"已受理"，
    等于告诉居民"没人管"。这不是显示问题，是状态语义错了。
    """
    items = WorkItem.query.filter_by(fde_id=t.fde_id).all() if t.fde_id else []
    rows = []

    for it in items:
        orders = WorkOrder.query.filter_by(work_item_id=it.id).all()
        ledgers = Ledger.query.filter_by(work_item_id=it.id).all()
        lines = [l for l in (it.target_lines or "").split(",") if l]

        state = STAGE_RESIDENT[item_stage(it, orders=orders, filed=bool(ledgers))]

        orgs = [LINE_TO_ORG.get(l, l) for l in lines]
        sla = max([o.sla_hours for o in orders], default=None)
        feedbacks = [o.feedback for o in orders if o.feedback]
        # 居民表态是**按事项**看的：一个诉求拆成"修灯"和"低保复核"两项，
        # 灯修好了不代表低保也办了。所以把每项的确认状态各自带出去，
        # 而不是给整张诉求一个"已确认"——那个粒度会掩盖掉没办的那一项。
        cs = [o.confirm_status or "" for o in orders]
        if cs and all(c == "ok" for c in cs):
            confirm = "ok"
        elif "disputed" in cs:
            confirm = "disputed"
        else:
            confirm = ""
        rows.append({
            "type": it.type,
            "subject": it.subject,
            "orgs": orgs or ["社区服务站"],
            "state": state,
            "sla_hours": sla,
            "feedback": feedbacks[0] if feedbacks else "",
            "created_at": (orders[0].created_at.strftime("%m-%d %H:%M") if orders else
                           it.created_at.strftime("%m-%d %H:%M")),
            "confirm_status": confirm,
            "confirm_text": {"ok": "已确认解决", "disputed": "已反馈不服"}.get(confirm, ""),
            # 只有办结的事项才能表态。判据看工单状态而不是整单，理由同上。
            "can_confirm": any(o.status == "done" for o in orders),
            "verdict_text": next((o.verdict_basis for o in orders if o.verdict), ""),
        })

    if rows and all(r["state"] == "已办结" for r in rows):
        overall = "done"
    elif any(r["state"] != "已受理" for r in rows):
        # 有一项推进过（处理中/已办结）但没全完 → 整单算处理中。
        # 早先只在"有 doing"时才算，于是"一项已办结、一项还没开始"会显示"已受理"，
        # 把已经推进的那项也一起说成没动。
        overall = "doing"
    else:
        overall = "open"
    if t.status != overall:
        t.status, t.updated_at = overall, datetime.now()
        db.session.commit()

    # 整单层面的表态口径：全部事项都确认才算"已确认"。
    # 有一项不服就算"有争议"——宁可让居委会多看一条，也不能把
    # 居民的不满淹没在"大部分已确认"里。
    all_cs = [r["confirm_status"] for r in rows]
    if rows and all(c == "ok" for c in all_cs):
        confirm_overall = "ok"
    elif "disputed" in all_cs:
        confirm_overall = "disputed"
    else:
        confirm_overall = ""

    return {
        "id": t.id,
        "code": t.code,
        "name": t.name,
        "phone": mask_phone(t.phone),
        "address": t.address,
        "created_at": t.created_at.strftime("%Y-%m-%d %H:%M"),
        "status": overall,
        "status_text": {"open": "已受理", "doing": "处理中", "done": "已办结"}[overall],
        "items": rows,
        "line_summary": t.line_summary,
        "confirm_status": confirm_overall,
        "can_confirm": any(r["can_confirm"] for r in rows),
        # 附件只回文件名与大小，**不回存盘路径**：那个路径一旦透出，
        # 就等于给出"猜下一个文件名"的线索（虽然是 uuid，但没有必要给）。
        "attachments": [{"name": a.get("name", ""), "size": a.get("size", 0)}
                        for a in evidence.load_attachments(t.attachments)],
        # 争议类型由服务端下发，居民端不再写死一份中文表
        "dispute_types": arbitration.DISPUTE_TYPES,
    }


# ------------------------------------------------------------------ 居民填报

@bp.post("/report")
def report():
    data = request.get_json(silent=True) or request.form or {}
    phone = (data.get("phone") or "").strip()
    name = (data.get("name") or "").strip()[:16]
    address = (data.get("address") or "").strip()[:64]
    text = (data.get("text") or "").strip()

    # 双维度限流：手机号维度是真实约束（一条诉求对应一个人），
    # IP 维度只做兜底——代理环境下 IP 既不稳定也可伪造，单独靠它挡不住刷单。
    if not _rate_ok(f"report:phone:{phone}", 5, 60):
        return fail("提交太频繁，请稍后再试（同一手机号每分钟最多 5 次）")
    if not _rate_ok(f"report:ip:{_client_ip()}", 30, 60):
        return fail("提交太频繁，请稍后再试")

    if not PHONE_RE.match(phone):
        return fail("请填写 11 位手机号，用于接收处理结果")
    if len(text) < 5:
        return fail("请把情况说清楚一些（至少 5 个字）")
    if len(text) > 2000:
        return fail("内容过长，请控制在 2000 字以内")

    # 居民身份不进 user 表，creator 只留可识别的痕迹（姓 + 手机尾号）
    label = f"居民{name[:1] + '某' if name else ''}(尾号{phone[-4:]})"
    from app.agents.orchestrator import Orchestrator
    try:
        result = Orchestrator().handle_fde(text, source="portal", creator=label)
    except Exception as e:
        return fail(f"提交失败：{type(e).__name__}，请稍后重试或电话联系社区")

    lines = []
    for it in result["items"]:
        for l in it["target_lines"]:
            if l not in lines:
                lines.append(l)

    ticket = ResidentTicket(
        code=_gen_code(), phone=phone, name=name, address=address,
        raw_text=text, fde_id=result["fde_id"],
        item_count=len(result["items"]),
        line_summary="/".join(lines),
        status="open",
    )
    db.session.add(ticket)
    # 先 flush 拿 id：链记录的 target_id 要用，而它不允许为空。
    db.session.flush()

    # 照片：存盘 + 每张算哈希进链。
    # 落盘失败不阻断提交——居民描述已经录进去了，因为"图没传上"让整单退回
    # 等于把最要紧的信息也一起丢了。错误放在返回值里如实告诉居民。
    saved, photo_errors = evidence.save_photos(
        request.files.getlist("photos") if request.files else [],
        current_app.config["EVIDENCE_DIR"])
    if saved:
        ticket.attachments = json.dumps(saved, ensure_ascii=False)

    evidence.append("ticket", ticket.id, "submit", {
        "code": ticket.code,
        "text": evidence.mask_payload(text),
        "item_count": ticket.item_count,
        "line_summary": ticket.line_summary,
        "photos": len(saved),
    }, actor=label)
    for ph in saved:
        evidence.append("ticket", ticket.id, "attach", {
            "name": ph["name"], "sha256": ph["sha256"], "size": ph["size"],
        }, actor=label)

    db.session.commit()
    _remember(ticket.id)
    db.session.add(AuditLog(actor=label, agent="portal", action="submit",
                            input_digest="", output_digest=f"居民提交，拆出 {len(result['items'])} 项"))
    db.session.commit()

    return ok({
        "ticket_id": ticket.id,
        "code": ticket.code,
        "item_count": len(result["items"]),
        "order_count": len(result["orders"]),
        "ledger_count": len(result["ledgers"]),
        "line_summary": ticket.line_summary,
        "photos": len(saved),
        "photo_errors": photo_errors,
        "items": [{"type": it["type"], "subject": it["subject"],
                   "orgs": [LINE_TO_ORG.get(l, l) for l in it["target_lines"]]}
                  for it in result["items"]],
        "llm_used": result["llm_used"],
    })


# ------------------------------------------------------------------ 进度查询

@bp.get("/ticket")
def query_ticket():
    phone = (request.args.get("phone") or "").strip()
    code = (request.args.get("code") or "").strip()
    # 限流必须落在**手机号**维度上：查询码只有 6 位，真正的攻击是"知道手机号后枚举查询码"，
    # 而 IP 在反向代理后面既不稳定、又可被 X-Forwarded-For 伪造，按 IP 限等于没限。
    if not _rate_ok(f"query:phone:{phone}", 20, 60):
        return fail("查询太频繁，请稍后再试（同一手机号每分钟最多 20 次）")
    if not _rate_ok(f"query:ip:{_client_ip()}", 120, 60):
        return fail("查询太频繁，请稍后再试")
    if not PHONE_RE.match(phone) or not re.match(r"^\d{6}$", code):
        return fail("请填写手机号与 6 位查询码")
    # 锁定检查放在格式校验之后：格式都不对的请求不该写审计、也不该占额度
    if _query_locked(phone):
        return fail("该手机号查询码输错次数过多，已锁定 30 分钟，请稍后再试")
    t = ResidentTicket.query.filter_by(phone=phone, code=code).first()
    if not t:
        _note_query_fail(phone)
        return fail("没有查到对应记录，请核对手机号与查询码")
    _remember(t.id)
    return ok(_ticket_payload(t))


@bp.get("/my")
def my_tickets():
    out = []
    for tid in session.get("portal_tickets", []):
        t = db.session.get(ResidentTicket, tid)
        if t:
            p = _ticket_payload(t)
            out.append({k: p[k] for k in ("id", "code", "created_at", "status",
                                          "status_text", "line_summary")})
    return ok({"tickets": out})


@bp.get("/ticket/<int:tid>")
def ticket_detail(tid: int):
    t = db.session.get(ResidentTicket, tid)
    # 只允许看本会话提交过的单子，防止遍历 id 拿到别人的信息
    if not t or tid not in session.get("portal_tickets", []):
        return fail("无权查看该记录")
    return ok(_ticket_payload(t))


@bp.post("/ticket/<int:tid>/confirm")
def confirm_ticket(tid: int):
    """居民表态：解决了 / 没解决

    这是"三方博弈"里居民唯一的**反向通道**。没有它，物业点办结就是终局，
    居委会也无从知道哪件事其实没办成——所有"反复投诉"在系统里都无处显形，
    只能表现为居民又提交一遍，然后被当成新单重新派出去。
    """
    t = db.session.get(ResidentTicket, tid)
    if not t or tid not in session.get("portal_tickets", []):
        return fail("无权操作该记录，请先用手机号和查询码查询"), 403
    if not _rate_ok(f"confirm:{tid}", 10, 60):
        return fail("操作太频繁，请稍后再试"), 429

    d = request.get_json(silent=True) or request.form or {}
    # 布尔值别直接 bool("false")——字符串 "false" 是真值，会把"没解决"判成"已解决"。
    raw = d.get("ok")
    do_ok = raw is True or str(raw).lower() in ("1", "true", "yes", "ok")
    okc, msg, n = arbitration.resident_confirm(
        t, do_ok, d.get("note") or "", d.get("dispute_type") or "")
    if not okc:
        return fail(msg), 400
    db.session.commit()
    return ok({"affected": n, "msg": msg, "view": _ticket_payload(t)})


@bp.post("/ticket/<int:tid>/attach")
def attach_photo(tid: int):
    """补传照片。居民常常先打字说情况、过一会儿才想起拍图。"""
    t = db.session.get(ResidentTicket, tid)
    if not t or tid not in session.get("portal_tickets", []):
        return fail("无权操作该记录"), 403
    if not _rate_ok(f"attach:{tid}", 6, 300):
        return fail("上传太频繁，请稍后再试"), 429

    cur = evidence.load_attachments(t.attachments)
    room = evidence.MAX_PHOTOS - len(cur)
    if room <= 0:
        return fail(f"最多 {evidence.MAX_PHOTOS} 张，已上传 {len(cur)} 张"), 400

    saved, errors = evidence.save_photos(
        request.files.getlist("photos") if request.files else [],
        current_app.config["EVIDENCE_DIR"])
    if not saved:
        return fail("；".join(errors) or "没有收到照片"), 400
    saved = saved[:room]

    t.attachments = json.dumps(cur + saved, ensure_ascii=False)
    for ph in saved:
        evidence.append("ticket", t.id, "attach", {
            "name": ph["name"], "sha256": ph["sha256"], "size": ph["size"],
        }, actor=f"居民(尾号{t.phone[-4:]})")
    db.session.commit()
    return ok({"added": len(saved), "errors": errors,
               "total": len(cur) + len(saved)})


# ------------------------------------------------------------------ 公告与联系人

@bp.get("/notices")
def notices():
    from app.notice.ingest import visible_query
    items = (visible_query("resident")
             # 与内部列表同一套排序：先置顶、再按**置顶时间**。
             # 居民端早先按 published_at 排，两条置顶件的先后就和内部不一样——
             # 社区刚置顶的重点通知反而排在旧件后面，"置顶"在居民眼里没生效。
             .order_by(Notice.pinned.desc(), Notice.pinned_at.desc(),
                       Notice.published_at.desc())
             .limit(50).all())
    return ok({"items": [{
        "id": n.id, "title": n.title, "issuer": n.issuer, "doc_no": n.doc_no,
        "category": n.category,
        "issued_at": n.issued_at.strftime("%Y-%m-%d") if n.issued_at else "",
        "published_at": n.published_at.strftime("%Y-%m-%d") if n.published_at else "",
        "summary": n.summary, "pinned": n.pinned,
    } for n in items]})


@bp.get("/contacts")
def contacts():
    from app.portal_seed import grouped_contacts
    return ok({"groups": [{
        "level": g["level"], "label": g["label"],
        "items": [{"name": c.name, "duty": c.duty, "phone": c.phone,
                   "address": c.address, "service_hours": c.service_hours}
                  for c in g["items"]],
    } for g in grouped_contacts()]})


@bp.get("/roles")
def roles():
    """对外说明"四类使用者各能做什么"，也供居民端底部的说明区使用"""
    return ok({"roles": [{"key": k, "name": v} for k, v in ROLE_NAMES.items() if k != "admin"]})
