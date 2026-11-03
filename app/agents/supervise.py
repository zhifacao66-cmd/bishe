"""督办Agent：扫描超时工单与到期任务，产出告警。
规则：工单 created_at + sla_hours < now 且未完成 → 超时；任务 deadline < now 且未完成 → 到期。

**扫描必须有范围**：早先这里直接 `WorkOrder.query` 全表扫，而接口只要求
`order.view`（物业也持有）。结果是物业账号调一次 `/api/supervision/alerts`
就能拿到别家物业的工单号与内容，还能看到社区的任务标题——
读侧在别处都做了隔离，唯独这个"顺带扫出来"的接口漏了。
"""
from datetime import datetime, timedelta

from sqlalchemy import func

from app.agents.base import BaseAgent
from app.models import Task, WorkOrder


class SuperviseAgent(BaseAgent):
    name = "supervise"

    def run(self, payload: dict = None) -> dict:
        """payload 可选：{property_co, role, org}，用于把扫描收窄到调用者的范围

        不传 = 全局扫描（街道/督办视图的语义）。传了按对应维度过滤。
        """
        p = payload or {}
        co = (p.get("property_co") or "").strip()
        role = (p.get("role") or "").strip()
        org = (p.get("org") or "").strip()
        now = datetime.now()

        # 状态列可能为 NULL（老库补列前写入的），coalesce 成 open 再比，
        # 否则 `!= 'done'` 对 NULL 求值为 NULL，那批单子会从扫描里消失。
        ostatus = func.coalesce(WorkOrder.status, "open")
        tstatus = func.coalesce(Task.status, "open")

        oq = WorkOrder.query.filter(ostatus != "done")
        if co:
            oq = oq.filter(WorkOrder.property_co == co)

        alerts = []
        for o in oq.all():
            ddl = o.created_at + timedelta(hours=o.sla_hours or 24)
            if now > ddl:
                alerts.append({
                    "kind": "order_overdue", "order_id": o.id, "order_no": o.order_no,
                    "title": f"工单 {o.order_no} 已超过 {o.sla_hours}h 时限",
                    "content": o.content, "status": o.status,
                    "deadline": ddl.strftime("%Y-%m-%d %H:%M"),
                    "overdue_hours": round((now - ddl).total_seconds() / 3600, 1),
                })

        tq = Task.query.filter(tstatus != "done")
        # 只有 community / property 是"受派方"，才按角色+单位收窄。
        # 原先写 `role and role != "street"`，admin 传进来也被收窄成
        # assignee_role=="admin"——一个不存在的受派角色，结果管理员打开督办
        # 永远"无告警"：不是没超时，是查错了范围。
        if role in ("community", "property"):
            # 非街道只看派给本角色的任务；单位为空的是"发给全体的"，也算
            tq = tq.filter(Task.assignee_role == role)
            if org:
                tq = tq.filter(func.coalesce(Task.assignee_org, "").in_(["", org]))
        for t in tq.all():
            if t.deadline and now > t.deadline:
                alerts.append({
                    "kind": "task_overdue", "task_id": t.id, "title": t.title,
                    "assignee_role": t.assignee_role,
                    "deadline": t.deadline.strftime("%Y-%m-%d %H:%M"),
                })
        if alerts:
            self._audit("supervise_scan", "", f"{len(alerts)} 条超时告警")
        return {"alerts": alerts, "scanned_at": now.strftime("%Y-%m-%d %H:%M:%S")}

    @staticmethod
    def is_overdue(order) -> bool:
        return (order.status != "done"
                and datetime.now() > order.created_at + timedelta(hours=order.sla_hours or 24))
