"""工作周报：取数、成稿、润色

三条口径必须先说清楚，否则周报就是一份"看着像"的假材料：

1. **周期是"本周一 00:00 至今"**，不是"最近 7 天"。
   社区报周报按自然周（周一至周日），跨周补报时才用整周区间。
   取 `[周一, 下周一)` 左闭右开，别用 `between` —— 周日 23:59:59 和
   下周一 00:00:00 会同时被算进两周。

2. **数从服务端 count 来，明细从同一次查询来**。
   正文写"本周办结 12 件"，`data_json.items` 里就得是那 12 个 id。
   前端拿 id 点开逐条核对，数不上的情况当场就能看出来（docs/14 的坑）。

3. **模型只写"存在问题 / 下周计划"两段，且失败就留空**。
   数字和事实陈述一律模板生成——模型编一个"本周化解矛盾 5 起"，
   上级一核对就穿帮。而"存在什么问题、下周打算干什么"本来就要人判断，
   模型给的是草稿，书记改完才算数。
"""
import json
from datetime import date, datetime, timedelta

from app.extensions import db
from app.models import (FdeRecord, Ledger, Notice, ResidentTicket, Task,
                        WorkItem, WorkOrder)

MAX_ITEMS = 200      # 明细快照上限，够点开核对，又不至于把 data_json 撑爆


# ---------------------------------------------------------------- 周期

def parse_period(period: str) -> tuple[date, date]:
    """'2026-W43' → (2026-10-19, 2026-10-26)，返回左闭右开区间

    非法输入退回本周，而不是报错：周报页每次打开都会传一次 period，
    手改网址传了乱码不该看到 500。
    """
    try:
        y, w = period.upper().split("-W")
        start = date.fromisocalendar(int(y), int(w), 1)
    except (ValueError, AttributeError):
        start = date.today() - timedelta(days=date.today().weekday())
    return start, start + timedelta(days=7)


def current_period(today: date | None = None) -> str:
    y, w, _ = (today or date.today()).isocalendar()
    return f"{y}-W{w:02d}"


def period_label(start: date, end: date) -> str:
    return f"{start:%Y-%m-%d} 至 {end - timedelta(days=1):%Y-%m-%d}"


# ---------------------------------------------------------------- 取数

def _span(start: date, end: date):
    return datetime.combine(start, datetime.min.time()), datetime.combine(end, datetime.min.time())


def collect(start: date, end: date) -> dict:
    """一次取数，返回 counts 与 items（明细 id）。

    所有 count 都用 `query(...).count()` 而不是 `len(rows)`：
    列表要限量（MAX_ITEMS），用 len 数出来的是"列表里几条"，
    不是"库里几条"——两者在数据多的时候必然对不上（docs/26 红线 4）。
    """
    lo, hi = _span(start, end)
    now = datetime.now()

    def q(model, *criteria):
        return db.session.query(model).filter(*criteria)

    # 本周首录与拆解
    fde_ids = [x for (x,) in q(FdeRecord, FdeRecord.created_at >= lo,
                              FdeRecord.created_at < hi).with_entities(FdeRecord.id)]
    # 业务项要按 fde_id 关联，不是按主键 —— 写成 WorkItem.id.in_(fde_ids)
    # 数出来的是"业务项主键恰好等于某条首录 id"的那几条，几乎恒为 0，
    # 而且不报错，周报上就是"拆解 0 项"。
    item_cnt = q(WorkItem, WorkItem.fde_id.in_(fde_ids)).count() if fde_ids else 0

    # 工单：新派 / 办结 / 超时未结
    wo_new = q(WorkOrder, WorkOrder.created_at >= lo, WorkOrder.created_at < hi)
    wo_done = q(WorkOrder, WorkOrder.done_at >= lo, WorkOrder.done_at < hi)
    # 超时：此刻仍未办结，且创建时间已超过 SLA。这不是"本周"的统计，
    # 而是"截至此刻"的欠账——周报里单列一段，否则欠账会被周而复始地漏掉。
    wo_open = q(WorkOrder, WorkOrder.status != "done").all()
    wo_over = [o for o in wo_open
               if o.created_at and now > o.created_at + timedelta(hours=o.sla_hours or 24)]

    # 台账
    lg_new = q(Ledger, Ledger.created_at >= lo, Ledger.created_at < hi)

    # 收文：本周新收 / 已签收 / 已办结
    doc_in = q(Notice, Notice.direction == "in", Notice.created_at >= lo,
               Notice.created_at < hi)
    doc_in_received = q(Notice, Notice.direction == "in", Notice.received_at >= lo,
                        Notice.received_at < hi)
    doc_in_done = q(Notice, Notice.direction == "in", Notice.handled_at >= lo,
                    Notice.handled_at < hi)
    # 收文欠办：有 deadline、已过期、但没办结
    doc_late = q(Notice, Notice.direction == "in", Notice.handled_at.is_(None),
                 Notice.deadline.isnot(None), Notice.deadline < date.today())

    # 发文
    doc_out = q(Notice, Notice.direction == "out", Notice.created_at >= lo,
                Notice.created_at < hi)
    doc_sent = q(Notice, Notice.direction == "out", Notice.doc_status.in_(("sent", "archived")),
                 Notice.created_at >= lo, Notice.created_at < hi)

    # 任务（街道下发）
    tk_new = q(Task, Task.created_at >= lo, Task.created_at < hi)
    tk_done = q(Task, Task.closed_at >= lo, Task.closed_at < hi)
    tk_late = q(Task, Task.closed_at.is_(None), Task.deadline.isnot(None),
                Task.deadline < date.today())

    # 居民诉求
    tp_new = q(ResidentTicket, ResidentTicket.created_at >= lo, ResidentTicket.created_at < hi)

    counts = {
        "fde": len(fde_ids),
        "items": item_cnt,
        "wo_new": wo_new.count(),
        "wo_done": wo_done.count(),
        "wo_overdue": len(wo_over),
        "ledger": lg_new.count(),
        "doc_in": doc_in.count(),
        "doc_in_received": doc_in_received.count(),
        "doc_in_done": doc_in_done.count(),
        "doc_out": doc_out.count(),
        "doc_sent": doc_sent.count(),
        "doc_late": doc_late.count(),
        "task_new": tk_new.count(),
        "task_done": tk_done.count(),
        "task_late": tk_late.count(),
        "ticket": tp_new.count(),
    }
    items = {
        "fde": fde_ids[:MAX_ITEMS],
        "wo_new": [x for (x,) in wo_new.with_entities(WorkOrder.id).limit(MAX_ITEMS)],
        "wo_done": [x for (x,) in wo_done.with_entities(WorkOrder.id).limit(MAX_ITEMS)],
        "wo_overdue": [o.id for o in wo_over[:MAX_ITEMS]],
        "ledger": [x for (x,) in lg_new.with_entities(Ledger.id).limit(MAX_ITEMS)],
        "doc_in": [x for (x,) in doc_in.with_entities(Notice.id).limit(MAX_ITEMS)],
        "doc_late": [x for (x,) in doc_late.with_entities(Notice.id).limit(MAX_ITEMS)],
        "doc_out": [x for (x,) in doc_out.with_entities(Notice.id).limit(MAX_ITEMS)],
        "task_new": [x for (x,) in tk_new.with_entities(Task.id).limit(MAX_ITEMS)],
        "task_late": [x for (x,) in tk_late.with_entities(Task.id).limit(MAX_ITEMS)],
        "ticket": [x for (x,) in tp_new.with_entities(ResidentTicket.id).limit(MAX_ITEMS)],
    }
    return {
        "period": current_period(start),
        "start": start.isoformat(),
        "end": (end - timedelta(days=1)).isoformat(),
        "collected_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "counts": counts,
        "items": items,
        "note": "counts 与 items 同一次查询产生；items 为明细 id 快照，最多 200 条",
    }


# ---------------------------------------------------------------- 成稿

def render(data: dict, org: str = "本社区") -> str:
    """模板成稿。只陈述取到的事实，不做任何推断。"""
    c = data["counts"]
    head = f"# {org}工作周报"
    sub = f"> 报告周期：{data['start']} 至 {data['end']}　生成时间：{data['collected_at'][:16]}"

    body = [
        head, "", sub, "",
        "## 一、本周概况", "",
        f"本周受理群众诉求 {c['fde']} 件，拆解为业务事项 {c['items']} 项；"
        f"派发工单 {c['wo_new']} 张、办结 {c['wo_done']} 张；"
        f"新增台账 {c['ledger']} 条；接收上级来文 {c['doc_in']} 份、办结 {c['doc_in_done']} 份；"
        f"制发公文 {c['doc_out']} 份；承办街道任务 {c['task_new']} 项、完成 {c['task_done']} 项。",
        "",
        "## 二、收文与办文", "",
        f"- 本周接收上级来文 **{c['doc_in']}** 份，签收 **{c['doc_in_received']}** 份，办结 **{c['doc_in_done']}** 份",
        f"- 制发公文 **{c['doc_out']}** 份，其中已完成分发 **{c['doc_sent']}** 份",
        f"- 逾期未办结来文 **{c['doc_late']}** 份",
        "",
        "## 三、工单与台账", "",
        f"- 派发物业工单 **{c['wo_new']}** 张，办结 **{c['wo_done']}** 张",
        f"- 超时未办结工单 **{c['wo_overdue']}** 张（截至生成时刻的欠账，不限于本周）",
        f"- 新增台账 **{c['ledger']}** 条",
        "",
        "## 四、任务与督办", "",
        f"- 承办街道下发任务 **{c['task_new']}** 项，完成 **{c['task_done']}** 项",
        f"- 逾期未完成任务 **{c['task_late']}** 项",
        "",
        "## 五、居民诉求", "",
        f"- 本周受理居民诉求 **{c['ticket']}** 件",
        "",
        "## 六、存在问题", "",
        "（请结合本周实际补充；可点「模型起草」生成初稿后修改）",
        "",
        "## 七、下周计划", "",
        "（请结合本周实际补充）",
        "",
    ]
    return "\n".join(body)


def polish(data: dict, org: str = "本社区") -> str:
    """让本地模型写"存在问题 / 下周计划"两段，返回 Markdown 片段。

    失败一律返回空串，由调用方保留模板占位——周报不能因为模型挂了就生成不了。
    调用前必须已经 commit 完取数（模型调用绝不放在数据库事务里，docs/26 红线 11）。
    """
    c = data["counts"]
    fact = (f"本周受理诉求{c['fde']}件、业务事项{c['items']}项、派单{c['wo_new']}张、"
            f"办结{c['wo_done']}张、超时未结{c['wo_overdue']}张、台账{c['ledger']}条、"
            f"收文{c['doc_in']}份、办结{c['doc_in_done']}份、逾期未办{c['doc_late']}份、"
            f"任务{c['task_new']}项、完成{c['task_done']}项、逾期{c['task_late']}项、"
            f"居民诉求{c['ticket']}件。")
    prompt = (
        "你是社区工作者，写工作周报的两个小节。只依据下面的数据，不要编造任何数字或事件。\n"
        f"数据：{fact}\n"
        "要求：\n"
        "1. 输出两段 Markdown，标题分别是「## 六、存在问题」「## 七、下周计划」；\n"
        "2. 存在问题从上面的欠账里提炼（超时工单、逾期来文、逾期任务），没有欠账就写工作短板；\n"
        "3. 下周计划写 3 条以内，具体可执行，不要空话；\n"
        "4. 不要出现「大幅」「显著」这类没有数据支撑的词。"
    )
    try:
        from app.llm.client import LLMUnavailable, chat
        text = chat(prompt, system="你是社区工作者，只根据给定数据写作，不编造事实。")
        text = (text or "").strip()
        # 模型偶尔会带 ```markdown 围栏，去掉以免渲染成代码块
        if text.startswith("```"):
            text = "\n".join(x for x in text.splitlines() if not x.strip().startswith("```"))
        return text.strip()
    except LLMUnavailable:
        return ""
    except Exception:
        # 润色是锦上添花，任何异常都不该让周报生成失败
        return ""


def apply_polish(body_md: str, extra: str) -> str:
    """把模型写的两段替换掉模板里的占位段；extra 为空则原样返回"""
    if not extra.strip():
        return body_md
    idx = body_md.find("## 六、存在问题")
    if idx < 0:
        return body_md + "\n" + extra
    return body_md[:idx] + extra.rstrip() + "\n"


def dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
