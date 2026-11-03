"""督办接口：超时告警扫描"""
from flask import Blueprint

from app.agents.supervise import SuperviseAgent
from app.api import ok
from app.auth import current_user, perm_required

bp = Blueprint("supervision", __name__)
agent = SuperviseAgent()


@bp.get("/alerts")
@perm_required("order.view")
def alerts():
    """超时告警。**按调用者身份收窄**，不是全表扫描。

    这个接口挂的是 order.view，物业也持有。全局扫描会让物业看到
    别家公司的工单号与内容、以及社区的任务标题——列表接口明明做了
    单位隔离，唯独这里漏了。传范围而不是在这里 filter，是为了让
    扫描逻辑（告警文案、超时判据）仍然只有一份。
    """
    u = current_user()
    scope = {}
    if u:
        if u.role == "property":
            scope["property_co"] = u.org or ""
        scope["role"] = u.role
        scope["org"] = u.org or ""
    return ok(agent.run(scope))
