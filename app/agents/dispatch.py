"""派单Agent：物业类业务项 → 脱敏工单。
契约：输入 {work_item_id, subject, content}；输出 {order_no, ...}
工单内容只含最小必要信息（楼栋/事项/时限），不含居民敏感信息。

SLA 分级依据知识库《物业报修响应时限与处置规范》（data/kb/04）：
    紧急（电梯困人 / 燃气泄漏 / 水管爆裂 / 停电）→ 办结 4 h
    较急（电梯故障停运 / 门禁失效 / 屋顶漏水）      → 办结 24 h
    一般（楼道灯 / 健身器材 / 下水道堵塞等）        → 办结 72 h
早先实现里所有工单一律 24 h，与系统自己提供检索的规范相互矛盾，
督办 Agent 因此会把本该 72 h 的"楼道灯不亮"在第 25 小时就报成超时。
"""
from datetime import datetime

from app.agents.base import BaseAgent
from app.agents.blackboard import SLA_HOURS
from app.extensions import db
from app.models import WorkOrder

DEFAULT_PROPERTY_CO = "幸福里物业"

# (等级, 办结时限小时, 关键词) —— 顺序即优先级，先命中先生效
SLA_RULES = [
    ("紧急", 4, ("电梯困人", "困人", "燃气", "煤气味", "水管爆裂", "爆管", "停电", "漏电")),
    ("较急", 24, ("电梯", "门禁", "单元锁", "屋顶", "屋面", "漏水", "渗漏", "水压")),
    ("一般", 72, ("楼道灯", "声控灯", "照明", "健身器材", "下水道", "堵塞", "地砖",
                  "路面", "垃圾", "杂物", "充电", "噪音", "油烟", "消防")),
]
DEFAULT_SLA = ("一般", 72)


def classify_sla(text: str) -> tuple[str, int]:
    """按规范给工单定级；未命中关键词的按"一般"处理（宁可宽限，不误报超时）"""
    t = text or ""
    for level, hours, kws in SLA_RULES:
        if any(k in t for k in kws):
            return level, hours
    return DEFAULT_SLA


class DispatchAgent(BaseAgent):
    name = "dispatch"

    def run(self, payload: dict) -> dict:
        order_no = "W" + datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{payload['work_item_id']:02d}"
        # 工单内容只带本业务项摘要，不带整段首录原文：
        # ① 最小必要原则——整段原文可能含其他业务线事项（如人口登记）；
        # ② 物业只需知道"哪个位置、什么问题"。
        content = (payload.get("content") or payload["subject"] or "").strip()
        # 再防一手：万一上游漏脱敏，此处按同一套规则兜底
        from app.agents.security import PATTERNS
        for _fname, (pat, _level, mask_fn) in PATTERNS.items():
            for m in pat.findall(content):
                content = content.replace(m, mask_fn(m))

        # 定级来源有两条路：
        #   ① 调度器没给 → 本 Agent 按词表自己判（旧路径，也是消融基线）；
        #   ② 调度器给了 → 说明黑板上已消解过（风险复核可能有异议），
        #      Agent 只负责执行，不再改结论。
        # 决策权上移是刻意的：让这个 Agent 自己算完了再被覆盖，等于
        # "先建单再改级"，工单上会留下一次错的定级痕迹，事后说不清。
        preset_level = (payload.get("sla_level") or "").strip()
        if preset_level in ("紧急", "较急", "一般"):
            level = preset_level
            hours = int(payload.get("sla_hours") or SLA_HOURS[level])
            note = payload.get("sla_note") or "调度器定级"
        else:
            level, hours = classify_sla(content)
            note = "按词表命中定级"

        order = WorkOrder(
            order_no=order_no,
            work_item_id=payload["work_item_id"],
            property_co=DEFAULT_PROPERTY_CO,
            content=content,
            sla_hours=hours,
            sla_level=level,
            status="open",
        )
        db.session.add(order)
        db.session.flush()
        # 派单本身也是流转链的第一环。少了这一条，"这张单是谁派的、
        # 什么时候派的"就只剩审计里那句话，工单时间线从"接单"才开始。
        from app import orderflow
        orderflow.push(order, "dispatch", actor="派单Agent", actor_org="系统",
                       detail=f"派往 {DEFAULT_PROPERTY_CO}（{level} / {hours}h，{note}）")
        self._audit("dispatch", payload["subject"],
                    f"工单{order_no} → {DEFAULT_PROPERTY_CO}（{level}/{hours}h，{note}）")
        return {
            "id": order.id, "order_no": order_no,
            "property_co": DEFAULT_PROPERTY_CO, "sla_hours": hours,
            "sla_level": level, "status": "open", "content": content,
            "content_stub": content,   # 供调度器喂给复核 Agent，避免再查库
        }
