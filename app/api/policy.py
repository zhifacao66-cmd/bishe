"""ABAC 策略接口：规则自述 + 现场试算

为什么要把规则做成接口给前端：
  保密这件事最怕"说不清"。评审问"物业为什么看不到这份文件"，
  如果只能回答"代码里写了 if"，那不叫制度，叫实现细节。
  `/security/policy` 页面上现场改一个属性、立刻看到命中哪条规则、
  被哪句话挡住——规则才算可被检验。
"""
from flask import Blueprint, request

from app.api import fail, ok
from app.auth import perm_required
from app.policy import (RULES, ROLE_SECRET_CEILING, SECRET_LABEL, RoleShield,
                        decide)

bp = Blueprint("policy", __name__)


@bp.get("")
@perm_required("audit.view")
def meta():
    """规则表 + 属性域。页面首屏一次性拿全。"""
    return ok({
        "rules": [{"code": c, "name": n, "desc": d} for c, n, d in RULES],
        "roles": [{"role": r, "ceiling": ROLE_SECRET_CEILING.get(r, 1),
                   "ceiling_label": SECRET_LABEL.get(ROLE_SECRET_CEILING.get(r, 1), "公开")}
                  for r in ("resident", "property", "community", "street", "admin")],
        "secrets": [{"value": v, "label": SECRET_LABEL[v]} for v in sorted(SECRET_LABEL)],
        "actions": [{"value": a, "label": b} for a, b in (
            ("read", "读取单条"), ("list", "浏览列表"),
            ("export", "导出文件"), ("write", "修改"), ("judge", "裁决"))],
        "kinds": [{"value": k, "label": b} for k, b in (
            ("order", "工单"), ("task", "任务"), ("notice", "公告"),
            ("doc", "公文"), ("evidence", "证据链"), ("report", "报表"))],
        "channels": [{"value": "internal", "label": "内部站"},
                     {"value": "portal", "label": "居民端"}],
    })


@bp.post("/simulate")
@perm_required("audit.view")
def simulate():
    """试算一次决策。**只读**：不查库、不写审计、不改任何数据。

    为什么不做成"真的拿一个真实对象试"：
    试算工具会被反复点（演示时一天几十次），每次都查库会让审计日志
    塞满"查看策略"这种噪音，把真正的越权尝试埋掉。
    """
    d = request.get_json(silent=True) or {}
    role = (d.get("role") or "").strip()
    if role not in ROLE_SECRET_CEILING:
        return fail("请选择角色"), 400
    action = (d.get("action") or "read").strip()
    if action not in ("read", "list", "export", "write", "judge"):
        return fail("动作不合法"), 400
    kind = (d.get("kind") or "order").strip()
    try:
        secret = int(d.get("secret") or 0)
    except (TypeError, ValueError):
        return fail("密级必须是整数 1-5"), 400
    if not 1 <= secret <= 5:
        return fail("密级必须是整数 1-5"), 400

    subject = RoleShield(role, (d.get("org") or "").strip())
    if (d.get("status") or "active") == "disabled":
        subject.status = "disabled"

    dd = decide(subject, action, kind, secret=secret,
                scope=(d.get("scope") or "all").strip(),
                owner_org=(d.get("owner_org") or "").strip(),
                channel=(d.get("channel") or "internal").strip(),
                status=(d.get("res_status") or "published").strip())
    return ok({
        "allow": dd.allow, "rule": dd.rule, "reason": dd.reason,
        "mask": dd.mask, "watermark": dd.watermark, "log": dd.log,
        "attrs": dd.attrs,
    })
