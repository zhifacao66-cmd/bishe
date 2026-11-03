"""造演示数据：居民诉求 + 工单流转时间线

## 为什么要造这批数据

流程监控与长尾预警这两个功能，方法写好了但**样本不够**：
原来库里 18 张工单、1 条居民诉求，`P90` 出不来、"反复投诉"凑不出人、
"踢皮球"一次都没有。在这种数据上做的分析，答辩时会被一句
"这结论是拿几条数据算的"问穿。

这批数据不是为了让图好看，是为了让**方法能成立**：

  · 时间跨度拉到 56 天 → 停留时长有了量级差异，瓶颈排序才有意义
  · 三组同一手机号的反复报事 → "反复投诉"这条信号从 0 变成 3
  · 两张单跨单位转派 → "踢皮球"从 0 变成 2，且能画出转移链
  · 四张单超过 30 天未结 → 长尾预警有了真实对象
  · 15 个主题覆盖 → 热点榜不再是"1 类 1 条"

## 数据是编的，但每一项都合乎业务逻辑

- 时间线按真实处理节奏走：派单当天接单、漏水隔天上门、电梯等配件拖一周
- 反复投诉的第二次、第三次措辞会**升级**（第一次陈述、第二次不满、
  第三次带后果"老人滑倒"）——这是真实投诉的演进方式，也是为什么
  单看文本能区分"新问题"和"上次没解决"
- 跨单位转派的情节是"物业说不是自己的责任→推给社区→社区指派另一家"，
  这正是"踢皮球"的实际形态

## 必须走真实链路

`orderflow.push()` 是状态变更的**唯一**入口，造数据也不例外。
直接 `order.status = "done"` 会得到一张"办结了但日志里没有这一步"的单，
证据链断在那儿，`check_integrity.py` 会报。所以这里全程调 push(at=...)。

幂等：按 `ResidentTicket.raw_text` 前 20 字判重，重复执行只补缺不重造。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import create_app
from app import arbitration, evidence
from app.extensions import db
from app.models import FdeRecord, ResidentTicket, WorkItem, WorkOrder
from app.orderflow import push

# 判重与清理都靠**内容前 20 字**，不在正文里加标记。
#
# 第一版用 `[演示]` 前缀拼进 raw_text 来标记，结果两层污染：
#   ① 文本分析被污染——`ngrams()` 的高频词榜首变成了"演示"（24 条都有它），
#      真热点被挤到第二行；
#   ② 居民诉求原文里带个"［演示］"很不像话，演示时一眼看得出是造的。
# 内容前 20 字足够唯一（24 条各不相同），且对文本分析零影响。
SEED_HEAD = 20

# ---------------------------------------------------------------- 数据定义
#
# `hours` 是"距诉求提交多少小时"，不是绝对时刻。这样无论哪天跑，
# 时间线都保持"刚刚发生"的观感，不会变成一堆 2026 年 9 月的陈年数据。
#
# steps 里每个动作的偏移是**相对工单创建时刻**的小时数。
# 状态机约束由 push() 自己校验，写错了会直接抛 ValueError 而不是静默造出怪数据。

CASES = [
    # ============ 反复投诉 A：3 栋漏水，报了三次 ============
    dict(phone="13800001111", name="张", addr="3栋2单元201", days=52,
         text="3栋2单元201室卫生间顶部漏水，墙皮已经整片脱落。找过物业，"
              "说是楼上防水层的问题，楼上是租户，房东电话打不通。",
         typ="报修", lines="物业",
         order=dict(co="幸福里物业", sla=24, lvl="较急",
                    steps=[("dispatch", 0), ("accept", 2), ("finish", 30)])),
    dict(phone="13800001111", name="张", addr="3栋2单元201", days=33,
         text="上次反映的漏水，物业来人涂了一层防水胶，不到一周又开始滴。"
              "现在客厅地板已经泡起来了，这样反复修什么时候是个头。",
         typ="报修", lines="物业",
         order=dict(co="幸福里物业", sla=24, lvl="较急",
                    steps=[("dispatch", 0), ("accept", 5), ("finish", 61)])),
    dict(phone="13800001111", name="张", addr="3栋2单元201", days=11,
         text="第三次反映了。3栋2单元201卫生间还是漏水，昨天家里老人踩到积水"
              "滑倒，去医院拍了片子。这个问题到底该谁负责，能不能给个说法。",
         typ="报修", lines="物业",
         order=dict(co="幸福里物业", sla=12, lvl="紧急",
                    steps=[("dispatch", 0), ("accept", 1), ("finish", 9)],
                    dispute=dict(dtype="duty", note="只是来涂了一次胶，漏水根本没解决，老人滑倒的事没人管",
                                 after=14))),

    # ============ 反复投诉 B：消防通道停车，报了两次 ============
    dict(phone="13900002222", name="李", addr="5栋1单元", days=44,
         text="5栋1单元门口消防通道长期停着两辆车，推婴儿车都要侧着身子过。"
              "万一着火消防车根本进不来，希望管一管。",
         typ="报修", lines="物业",
         order=dict(co="幸福里物业", sla=48, lvl="一般",
                    steps=[("dispatch", 0), ("accept", 6), ("finish", 90)])),
    dict(phone="13900002222", name="李", addr="5栋1单元", days=18,
         text="消防通道那两辆车还在，上次贴了张纸条就不管了。物业说不归他们管，"
              "那到底归谁管？这事我要往街道反映。",
         typ="报修", lines="物业,综治",
         order=dict(co="幸福里物业", sla=24, lvl="较急",
                    steps=[("dispatch", 0), ("accept", 2), ("finish", 40)])),

    # ============ 反复投诉 C：噪音，报了两次 ============
    dict(phone="13700003333", name="王", addr="7栋3单元101", days=30,
         text="7栋3单元101楼上每天晚上十一点以后还在拖家具、小孩跑来跑去。"
              "上门说过两次，态度还行但第二天照旧。孩子上初三，实在影响休息。",
         typ="调解", lines="综治",
         order=dict(co="幸福里物业", sla=72, lvl="一般",
                    steps=[("dispatch", 0), ("accept", 12), ("finish", 100)])),
    dict(phone="13700003333", name="王", addr="7栋3单元101", days=7,
         text="噪音越来越厉害，昨晚一直到十二点半。找物业说让我自己报警，"
              "报警了警察来了说让找社区调解。到底哪个环节能管？",
         typ="调解", lines="综治",
         order=dict(co="幸福里物业", sla=48, lvl="较急",
                    steps=[("dispatch", 0), ("accept", 3)])),  # 未办结

    # ============ 踢皮球 A：6 栋电梯，跨单位转派 3 次，56 天未结 ============
    dict(phone="13600004444", name="赵", addr="6栋", days=56,
         text="6栋电梯从上个月开始经常卡在3楼不动，门也关不严。上周有位老人"
              "被关在里面二十多分钟，是邻居撬开门才出来的。这个必须尽快处理。",
         typ="报修", lines="物业",
         order=dict(co="幸福里物业", sla=24, lvl="紧急",
                    steps=[("dispatch", 0), ("accept", 3),
                           # 物业说电梯维保是外包的，转给维保单位
                           ("transfer", 26, dict(to="广厦物业", org="幸福里物业",
                                                 detail="电梯维保由广厦物业承包，转其处理")),
                           # 广厦说主板要等厂家，又推回社区协调
                           ("transfer", 120, dict(to="幸福里物业", org="广厦物业",
                                                  detail="主板需厂家发货，且该梯维保合同已到期，请社区协调")),
                           # 社区协调后又派回物业
                           ("transfer", 400, dict(to="广厦物业", org="幸福里社区",
                                                  detail="经协调，仍由广厦物业先行抢修，费用另议"))])),

    # ============ 踢皮球 B：2 栋外墙渗水，跨单位转派 2 次，47 天未结 ============
    dict(phone="13500005555", name="陈", addr="2栋1单元501", days=47,
         text="2栋外墙渗水，我家501卧室整片墙面发霉，衣柜背板都烂了。"
              "怀疑是外墙保温层开裂，下雨就往里渗。",
         typ="报修", lines="物业",
         order=dict(co="幸福里物业", sla=48, lvl="较急",
                    steps=[("dispatch", 0), ("accept", 8),
                           ("transfer", 40, dict(to="幸福里社区", org="幸福里物业",
                                                 detail="外墙属公共部位大修，需动用维修资金，请社区牵头")),
                           ("transfer", 200, dict(to="幸福里物业", org="幸福里社区",
                                                  detail="维修资金申请已受理，先行排查渗漏点"))])),

    # ============ 其他：覆盖各主题 ============
    dict(phone="13400006666", name="刘", addr="南门", days=38,
         text="小区南门垃圾房已经三天没有清运，味道很大，苍蝇很多，"
              "旁边就是儿童游乐区，这样太不卫生了。",
         typ="报修", lines="物业",
         order=dict(co="幸福里物业", sla=12, lvl="较急",
                    steps=[("dispatch", 0), ("accept", 1)])),  # 未结（open→doing 后停）

    dict(phone="13300007777", name="周", addr="12栋", days=36,
         text="12栋楼下绿化带的树被物业砍了三棵，说是挡光。但是没征求业主意见，"
              "也没公示，这些树长了十几年了。",
         typ="报修", lines="物业",
         # 「无人接单」指的是**派了没人接**，不是"这张单凭空存在"。
         # 写成 steps=[] 会造出一张零流转记录的工单：状态 open、日志为空，
         # 页面看不出异常，但 `check_integrity` 立刻报"无流转记录"——
         # 而且从追责角度，谁派的、什么时候派的全都查不到。
         order=dict(co="幸福里物业", sla=72, lvl="一般", steps=[("dispatch", 0)])),

    dict(phone="13200008888", name="吴", addr="地下车库", days=25,
         text="地下车库入口道闸坏了半个月，随便什么车都能进，"
              "现在车库里停满了外面的车，业主自己反倒没位置。",
         typ="报修", lines="物业",
         order=dict(co="幸福里物业", sla=24, lvl="一般",
                    steps=[("dispatch", 0), ("accept", 4), ("finish", 20)])),

    dict(phone="13100009999", name="郑", addr="3号楼", days=22,
         text="3号楼后面有人私搭了一个铁皮棚子放杂物，占了一半的道路，"
              "晚上黑灯瞎火的容易撞上，反映过没人管。",
         typ="报修", lines="综治,物业",
         order=dict(co="幸福里物业", sla=72, lvl="一般",
                    steps=[("dispatch", 0), ("accept", 20), ("finish", 96)])),

    dict(phone="13000001010", name="孙", addr="小区", days=20,
         text="小区里遛狗不拴绳的越来越多，昨天孩子在楼下被一条大狗扑了一下，"
              "虽然没咬到但吓得不轻。希望管管养犬的。",
         typ="调解", lines="综治",
         order=dict(co="幸福里物业", sla=48, lvl="较急",
                    steps=[("dispatch", 0), ("accept", 5), ("finish", 70)])),

    dict(phone="18900002020", name="马", addr="9栋3单元", days=17,
         text="9栋3单元门禁刷卡没反应已经一周了，单元门一直敞着，"
              "谁都能进，楼道里还帖了不少小广告。",
         typ="报修", lines="物业",
         order=dict(co="幸福里物业", sla=24, lvl="较急",
                    steps=[("dispatch", 0), ("accept", 2), ("finish", 16)])),

    dict(phone="18800003030", name="胡", addr="小区", days=15,
         text="物业费今年涨了两毛一平，但是服务没有任何变化，绿化没人管、"
              "保洁也减了人。要求公示收支账目，涨价的依据是什么。",
         typ="咨询", lines="物业",
         order=dict(co="幸福里物业", sla=120, lvl="一般",
                    steps=[("dispatch", 0), ("accept", 30), ("finish", 180)])),

    dict(phone="18700004040", name="高", addr="8栋2单元", days=13,
         text="8栋2单元楼道堆满了纸箱和旧家具，占了半个楼道，"
              "消防通道都被堵死了。跟那户人家说过，人家说放自己家门口不关别人事。",
         typ="报修", lines="物业,综治",
         order=dict(co="幸福里物业", sla=24, lvl="较急",
                    steps=[("dispatch", 0), ("accept", 6), ("finish", 30)])),

    dict(phone="18600005050", name="林", addr="小区", days=10,
         text="小区路灯有一半不亮，晚上回家黑漆漆的，尤其是东边那条路，"
              "女同志下班晚不敢走。",
         typ="报修", lines="物业",
         order=dict(co="幸福里物业", sla=48, lvl="一般",
                    steps=[("dispatch", 0), ("accept", 3), ("finish", 36)])),

    dict(phone="18500006060", name="何", addr="4栋1单元", days=8,
         text="4栋1单元下水道反水，厨房地漏往外冒污水，臭得没法做饭，"
              "已经漫到客厅了。物业说要去疏通主管道。",
         typ="报修", lines="物业",
         order=dict(co="幸福里物业", sla=6, lvl="紧急",
                    steps=[("dispatch", 0), ("accept", 1), ("finish", 4)])),

    dict(phone="18400007070", name="罗", addr="10栋2单元501", days=6,
         text="10栋2单元501室住着一位独居老人，姓钱，今年82岁。这两天没见到"
              "人下楼，敲门也没应。希望社区上门看看，别出事。",
         typ="走访", lines="民政", order=None),  # 纯民政，不派物业工单

    dict(phone="18300008080", name="梁", addr="小区", days=5,
         text="小区停车位太紧张，晚上七点回来根本找不到地方停，"
              "能不能把北边那块空地重新划一下车位，或者错时共享。",
         typ="咨询", lines="物业,综治",
         order=dict(co="幸福里物业", sla=120, lvl="一般",
                    steps=[("dispatch", 0), ("accept", 10), ("finish", 60)])),

    dict(phone="18200009090", name="宋", addr="11栋", days=4,
         text="11栋楼顶有人私搭阳光房，已经建了半个月了，钢架都焊上了，"
              "看着挺危险，刮大风会不会掉下来。",
         typ="报修", lines="综治,物业",
         order=dict(co="幸福里物业", sla=72, lvl="较急",
                    steps=[("dispatch", 0), ("accept", 14), ("finish", 80)])),

    dict(phone="18100010101", name="唐", addr="小区", days=3,
         text="垃圾分类点太少了，整个小区就两个，每天要走十分钟去扔垃圾，"
              "老人拎着垃圾走那么远不方便，建议多设几个点。",
         typ="咨询", lines="物业",
         order=dict(co="幸福里物业", sla=120, lvl="一般",
                    steps=[("dispatch", 0), ("accept", 6), ("finish", 72)])),

    dict(phone="18000011111", name="许", addr="1栋2单元", days=2,
         text="1栋2单元楼道感应灯不亮，晚上上楼看不见台阶，"
              "上次邻居就踩空崴了脚。灯泡自己换不了，物业能不能来看看。",
         typ="报修", lines="物业",
         order=dict(co="幸福里物业", sla=12, lvl="较急",
                    steps=[("dispatch", 0), ("accept", 1), ("finish", 5)])),
]


def _label(name, phone):
    """creator 的写法与 portal.report() 保持一致（姓 + 手机尾号）。

    造数据不该在库里留下另一种格式——两种格式混着，将来按 creator 统计
    "哪个人报事最多"就会漏掉一半。
    """
    return f"居民{name + '某' if name else ''}(尾号{phone[-4:]})"


def _no_prefix(t):
    return "W" + t.strftime("%Y%m%d-%H%M%S")


def _heads():
    """24 条诉求的前 20 字，用来判重与清理。"""
    return [c["text"][:SEED_HEAD] for c in CASES]


def _exists(text):
    return (ResidentTicket.query
            .filter(ResidentTicket.raw_text.like(text[:SEED_HEAD] + "%"))
            .first())


def build_case(case, now):
    """造一条：诉求 → FDE → 业务项 → 工单 → 流转 → （可选）居民不服"""
    t0 = now - timedelta(days=case["days"], hours=(case["days"] * 7) % 11)
    text = case["text"]
    phone, name, addr = case["phone"], case["name"], case["addr"]
    label = _label(name, phone)

    fde = FdeRecord(raw_text=text, source="portal", creator=label, created_at=t0)
    db.session.add(fde)
    db.session.flush()

    # **业务线两种写法不能混用**：
    #   `WorkItem.target_lines` 用逗号（"物业,综治"，与填报 Agent 一致）
    #   `ResidentTicket.line_summary` 用斜杠（"物业/综治"，居民端显示用）
    # 早先两边都塞同一个字符串，`check_integrity` 立刻报"业务线与实际去向不符"——
    # 它按 split(",") 和 split("/") 分别拆，一边拆出 "物业/综治" 整串，
    # 另一边拆出两段，永远对不上。
    item = WorkItem(fde_id=fde.id, type=case["typ"],
                    subject=case["text"][:60],
                    fields_json="{}", target_lines=case["lines"].replace("/", ","),
                    status="pending", created_at=t0)
    db.session.add(item)
    db.session.flush()

    ticket = ResidentTicket(
        code=_gen_code(), phone=phone, name=name, address=addr,
        raw_text=text, fde_id=fde.id, item_count=1,
        line_summary=case["lines"].replace(",", "/"), status="open",
        created_at=t0, updated_at=t0,
    )
    db.session.add(ticket)
    db.session.flush()
    evidence.append("ticket", ticket.id, "submit", {
        "code": ticket.code,
        "text": evidence.mask_payload(text),
        "item_count": 1,
        "line_summary": case["lines"],
        "photos": 0,
    }, actor=label, at=t0)

    od = case.get("order")
    if not od:
        # 纯民政类：没有工单，业务项留在待办池里。
        # `WorkItem.status` 是死字段（全站无写入），判进度看工单+台账，
        # 所以这里不用改它。
        return ticket, None

    order = WorkOrder(
        work_item_id=item.id,
        order_no=_no_prefix(t0) + f"-{item.id:02d}",
        property_co=od["co"], content=case["text"],
        sla_hours=od["sla"], sla_level=od["lvl"],
        status="open", created_at=t0,
    )
    db.session.add(order)
    db.session.flush()

    finish_at = None
    for step in od["steps"]:
        act, hours = step[0], step[1]
        extra = step[2] if len(step) > 2 else {}
        at = t0 + timedelta(hours=hours)
        if act == "dispatch":
            push(order, "dispatch", actor="调度员", actor_org=od["co"],
                 detail=f"派往 {od['co']}（{od['lvl']} / {od['sla']}h）", at=at)
        elif act == "accept":
            order.assignee, order.assigned_at = "维修班", at
            push(order, "accept", actor="维修班", actor_org=order.property_co,
                 detail=f"接单处理（{od['lvl']} / {od['sla']}h）", at=at)
        elif act == "transfer":
            # 转派必须同时改 property_co 和 assignee：
            # 只改日志不改单据，会出现"日志说转给了广厦、工单还挂在幸福里"，
            # 于是物业列表里这张单谁也认领不到。
            order.property_co = extra["to"]
            push(order, "transfer", actor="值班经理", actor_org=extra["org"],
                 detail=extra["detail"], at=at)
        elif act == "finish":
            order.done_at = at
            order.feedback = "已上门处理并反馈居民。"
            push(order, "finish", actor="维修班", actor_org=order.property_co,
                 detail=order.feedback, at=at)
            finish_at = at

    # 居民不服：必须在办结之后，否则"不服"没有对象
    dp = od.get("dispute")
    if dp and finish_at:
        arbitration.resident_confirm(
            ticket, ok=False, note=dp["note"], dispute_type=dp["dtype"],
            at=finish_at + timedelta(hours=dp["after"]))

    # 诉求状态必须跟着明细走。
    #
    # 不写这一步的后果：`resident_ticket.status` 停在 open，而工单早就 done，
    # 于是居民端进度条（`_ticket_payload` 现算）说"已办结"、库里的字段说
    # "还没动"，`check_integrity` 一查就是 22 单不一致。
    # 判据必须与 `app/itemstate.py` 同一套，这里不自己写 `all(o.status=="done")`。
    ticket.status = _ticket_state(item, order)
    return ticket, order


def _ticket_state(item, order):
    """诉求整单状态：与 `portal._ticket_payload` 的 overall 同一口径。

    单事项时：已办结 → done；处理中 → doing；其余（已受理）→ open。
    """
    from app.itemstate import STAGE_RESIDENT, stage as item_stage
    orders = [order] if order else []
    state = STAGE_RESIDENT[item_stage(item, orders=orders, filed=False)]
    if state == "已办结":
        return "done"
    if state == "处理中":
        return "doing"
    return "open"


def _gen_code():
    import random
    import string
    for _ in range(50):
        c = "".join(random.choices(string.digits, k=6))
        if not ResidentTicket.query.filter_by(code=c).first():
            return c
    raise RuntimeError("查询码生成失败：库里的 6 位码已用尽")


def clean():
    """一键清理本脚本造的数据。**只删正文匹配 CASES 那些条**，不动真实数据。"""
    from sqlalchemy import or_
    tiles = ResidentTicket.query.filter(
        or_(*[ResidentTicket.raw_text.like(h + "%") for h in _heads()])).all()
    oids = []
    for t in tiles:
        for it in WorkItem.query.filter_by(fde_id=t.fde_id).all():
            for o in WorkOrder.query.filter_by(work_item_id=it.id).all():
                oids.append(o.id)
    from app.models import WorkOrderLog
    WorkOrderLog.query.filter(WorkOrderLog.order_id.in_(oids or [0])).delete(
        synchronize_session=False)
    WorkOrder.query.filter(WorkOrder.id.in_(oids or [0])).delete(
        synchronize_session=False)
    fde_ids = [t.fde_id for t in tiles if t.fde_id]
    WorkItem.query.filter(WorkItem.fde_id.in_(fde_ids or [0])).delete(
        synchronize_session=False)
    ResidentTicket.query.filter(
        or_(*[ResidentTicket.raw_text.like(h + "%") for h in _heads()])).delete(
        synchronize_session=False)
    FdeRecord.query.filter(FdeRecord.id.in_(fde_ids or [0])).delete(
        synchronize_session=False)
    db.session.commit()
    print(f"[clean] 删除诉求 {len(tiles)} 条、工单 {len(oids)} 张及对应流转")
    print("[clean] 证据链记录未删（只增不减的设计），如需彻底重置请重建库")


def main():
    app = create_app()
    with app.app_context():
        if "--clean" in sys.argv:
            clean()
            return
        now = datetime.now()
        made = skipped = 0
        for c in CASES:
            if _exists(c["text"]):
                skipped += 1
                continue
            try:
                t, o = build_case(c, now)
                made += 1
            except Exception as e:
                db.session.rollback()
                print(f"[err] 造「{c['text'][:24]}…」失败：{type(e).__name__}: {e}")
        db.session.commit()
        print(f"[done] 新建 {made} 条，跳过 {skipped} 条（已存在）")
        print(f"[done] 当前：诉求 {ResidentTicket.query.count()} / "
              f"工单 {WorkOrder.query.count()}")


if __name__ == "__main__":
    main()
