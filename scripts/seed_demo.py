# -*- coding: utf-8 -*-
"""演示数据种子 / 一键重置

为什么需要它：答辩演示时如果库里只有十几条记录、工单全是"待处理"、报表只有当月一张，
演示就撑不住"用了一个月"的叙事。本脚本用**真实业务链路**（Orchestrator → 保密 → 拆解
→ 派单/填报）灌入 40 条口语化首录，再把时间回填到过去 45 天的工作时段，构造出工单三态、
办结反馈、多月报表和超时任务。

    python scripts/seed_demo.py                 # 增量补足到 40 条（已有数据不清空）
    python scripts/seed_demo.py --reset         # 清空业务数据重建（保留代码与知识库文件）
    python scripts/seed_demo.py --no-llm        # 不调本地模型，用规则兜底（快，约 10 秒）
    python scripts/seed_demo.py --limit 10      # 只灌 10 条

注意：--no-llm 生成的数据由规则拆解产生，可用来看界面，**不要**拿去做评测统计。
"""
import argparse
import os
import random
import sys
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# --no-llm 必须在导入 config 之前生效：config.py 在 import 时就把 OLLAMA_BASE_URL 读进类属性，
# 之后再改环境变量是没用的（第一次踩过这个坑，导致"规则兜底"其实偷偷调了 6 次模型）。
if "--no-llm" in sys.argv:
    os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:1"

from app import create_app                                    # noqa: E402
from app.extensions import db                                 # noqa: E402
from app.models import (AuditLog, FdeRecord, FieldSecurity,   # noqa: E402
                        KbDoc, Ledger, Report, Task, User, WorkItem, WorkOrder)

# ---------------------------------------------------------------------------
# 语料：全部是社区工作者真实口吻的一段话，覆盖 7 类业务项与 5 条业务线
# 前 24 条与 scripts/eval_e2e.py 的评测集同源，后 16 条为演示补充
# ---------------------------------------------------------------------------
CORPUS = [
    "2栋2单元202室新租户李某，电话13800001234，这个月搬进来的",
    "7栋3单元电梯有异响，麻烦物业尽快检修，联系电话13900001111",
    "2栋2单元202室租户李某要补办居住登记",
    "今天走访7栋独居老人王奶奶，她血压偏高，家里下水道也堵了",
    "10栋架空层有人私拉电线给电动车充电，另外6栋地下室群租问题还没处理",
    "12栋302新搬来一对小夫妻，男的是外地户口要办居住证；顺便说楼道声控灯不亮了",
    "5栋2单元低保户张阿姨来问这个月低保金为什么还没到账",
    "9栋门禁坏了，另外3栋楼道灯也坏了，都需要修",
    "8栋2单元电梯坏了，老人下不了楼，麻烦尽快修",
    "街道通知明天上午九点开会，要求上报本月消防检查台账",
    "6栋两户居民因为空调外机滴水吵起来了，需要上门调解",
    "辖区精神障碍患者刘某最近情绪不稳，家属说药快吃完了",
    "租户赵某身份证320102199003074512，电话13712345678，8月1日入住5栋601",
    "居民来问高龄老人津贴怎么申请，需要准备哪些材料",
    "2栋屋顶漏水，物业说上周就报过了还没修好",
    "11栋电梯困人已经救出来了，需要检修；另外11栋消防通道堆了杂物；还有居民反映楼下广场舞噪音太大",
    "低保户李大爷来问临时救助怎么申请",
    "今天给3栋独居老人送餐，老人说膝盖疼得厉害",
    "15栋新开了一家民宿，疑似群租经营，消防设施也不达标",
    "居民反映7栋2单元楼道堆了很多纸箱，有火灾隐患",
    "街道要求各社区本周五前上报独居老人探访台账",
    "5栋601租户赵某要办理居住证签注",
    "1栋电梯坏了，同时2栋单元门禁也坏了，都要修",
    "6栋3单元402住户反映水压太低，洗澡都成问题，电话13566667777",
    # ---- 以下 16 条为演示补充，语体与上面一致 ----
    "5栋1单元302住户反映楼上漏水到自家天花板，物业来看过一次还没解决",
    "今天走访12栋独居老人陈爷爷，血压有点高，提醒他按时吃药",
    "街道通知下周三前上报三季度矛盾纠纷排查化解台账",
    "8栋楼下电动车充电桩坏了两天，居民只能推车上楼充电",
    "3栋4单元有住户在楼道养狗，邻居投诉味道大还扰民",
    "9栋2单元新搬来一户三口之家，孩子要转学到辖区小学",
    "10栋1单元电梯按键失灵，另外11栋单元锁也坏了",
    "辖区低保户周某因病住院，家属来咨询医疗救助报销流程",
    "7栋地下室积水严重，怀疑是消防管道渗漏，需要紧急处理",
    "2栋3单元两户为停车位划线问题争执不下，请社区出面调解",
    "今天给15栋失能老人李奶奶送助餐，家属反映护理补贴还没到账",
    "13栋一层商铺油烟直排进居民楼道，多户反映无法开窗",
    "街道要求各社区摸排独居、空巢老人底数，10月25日前建好台账",
    "4栋2单元701租户退租搬离，需要办理人口信息注销",
    "16栋消防栓箱玻璃破损，灭火器压力不足，存在安全隐患",
    "小区北门人行道地砖松动，老人反映容易绊倒，希望尽快修复",
]

CREATORS = ["王社工", "王社工", "王社工", "李社工"]      # 王社工为主，符合单人使用场景

# 业务项 created_at 需要回填的表（field_security 无时间列，靠 work_item 关联）
SNAP = [("fde", FdeRecord), ("item", WorkItem), ("order", WorkOrder),
        ("ledger", Ledger), ("audit", AuditLog)]

# 工单反馈话术：按内容关键词匹配，避免所有反馈都是同一句"已处理完成"
FEEDBACK_RULES = [
    (("电梯",), "已联系维保单位到场检修，更换门机皮带并做运行测试，恢复正常运行，已在楼道张贴检修记录。"),
    (("门禁", "单元锁", "门锁"), "维修人员已更换门禁控制器与读卡模块，现场刷卡测试正常，同步为业主补录权限。"),
    (("灯", "照明"), "已更换损坏灯具并检查线路，声控模块一并调整灵敏度，夜间复测正常。"),
    (("漏水", "渗漏", "积水", "水压", "下水", "管道"), "已定位漏点并完成管件更换，积水抽排干净，打压测试无渗漏，后续一周回访观察。"),
    (("充电", "充电桩"), "已联系厂商更换充电模块，恢复扫码充电功能，同时在架空层张贴禁止飞线充电提示。"),
    (("垃圾", "清运"), "已协调保洁加密清运频次为每日两次，桶体及周边冲洗消杀完毕。"),
    (("消防", "灭火器", "消防栓"), "已完成器材更换与压力检测，消防通道杂物清理完毕，报街道备案。"),
    (("地砖", "路面", "松动"), "已用同色地砖更换并做防滑处理，周边松动区域一并排查加固。"),
    (("异响", "噪音"), "已加装减震垫并复测，夜间噪声降至标准范围内，已向相邻住户说明。"),
]
DEFAULT_FEEDBACK = "已安排人员现场处置并完成验收，处理结果已回访居民，居民表示满意。"


def pick_feedback(content: str) -> str:
    for kws, text in FEEDBACK_RULES:
        if any(k in (content or "") for k in kws):
            return text
    return DEFAULT_FEEDBACK


def workday_times(n: int, days: int = 45, seed: int = 20261019) -> list[datetime]:
    """生成 n 个时间点（降序）。

    刻意做成**双峰**而非均匀分布：约 30% 落在最近 72 小时内，其余铺在过去 40 天。
    均匀分布会让"最新一条"也在十天前，于是所有未办结工单一起超时，
    演示时看起来像"整个社区没人干活"；真实节奏本来就是平时零星、临近上报集中处理。
    """
    rnd = random.Random(seed)
    now = datetime.now()
    recent_n = max(3, int(n * 0.3))
    out = []
    for i in range(n):
        if i < recent_n:                                   # 最近 72 小时
            hours = i * (70.0 / max(recent_n, 1)) + rnd.uniform(0.5, 3.0)
            dt = now - timedelta(hours=hours)
            if dt.hour < 8 or dt.hour >= 19:              # 半夜两点的走访记录不合理，挪到工作时段
                dt = (dt - timedelta(days=1)).replace(
                    hour=rnd.randint(9, 17), minute=rnd.randint(0, 59),
                    second=rnd.randint(0, 59), microsecond=0)
        else:                                              # 更早的按天铺开
            frac = (i - recent_n) / max(n - recent_n - 1, 1)
            off = 3 + frac * (days - 3) + rnd.uniform(-1.0, 1.0)
            dt = now - timedelta(days=max(3.0, min(days, off)))
            while dt.weekday() >= 5:                       # 早期数据落在工作日
                dt -= timedelta(days=1)
            dt = dt.replace(hour=rnd.randint(8, 17), minute=rnd.randint(0, 59),
                            second=rnd.randint(0, 59), microsecond=0)
        if dt > now:
            dt = now - timedelta(minutes=rnd.randint(10, 90))
        out.append(dt)
    return sorted(out, reverse=True)


def reset_all(app):
    """清空并重建全部表，随后恢复演示账号、任务与知识库"""
    print("[重置] 删除并重建所有表 ...")
    db.drop_all()
    db.create_all()
    from app.auth import _migrate_task_table, _migrate_user_table, seed_tasks, seed_users
    _migrate_user_table()
    _migrate_task_table()
    seed_users()
    seed_tasks()
    from app.kb.seed import import_dir
    n = import_dir(app)
    print(f"[重置] 演示账号 3 个 / 任务 4 条 / 知识库文档 {n} 篇 已恢复")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="清空业务数据后重建")
    ap.add_argument("--no-llm", action="store_true", help="不调用本地模型（规则兜底，速度快）")
    ap.add_argument("--limit", type=int, default=0, help="只灌前 N 条（调试用）")
    args = ap.parse_args()

    if args.no_llm:
        # 兜底路径由模块顶部的环境变量切换，这里只做提示（避免误以为在用模型）
        print("[模式] 规则兜底：不调用本地模型，拆解质量低于模型产出，仅供界面演示")

    app = create_app(os.environ.get("FLASK_ENV", "dev"))
    texts = CORPUS[:args.limit] if args.limit else CORPUS

    with app.app_context():
        if args.reset:
            reset_all(app)
        # 增量模式：已入库的原文跳过，避免重复灌入同一句话
        exist = {r.raw_text for r in FdeRecord.query.all()}
        todo = [t for t in texts if t not in exist]
        if not todo:
            print("[跳过] 语料已全部入库。要重灌请加 --reset")
            report(app)
            return

        from app.agents.orchestrator import Orchestrator
        orch = Orchestrator()
        times = workday_times(len(todo))
        print(f"[灌入] {len(todo)} 条首录（{'规则兜底' if args.no_llm else '本地模型'}），"
              f"时间回填至 {times[-1]:%Y-%m-%d} ~ {times[0]:%Y-%m-%d}")

        stat = {"items": 0, "orders": 0, "ledgers": 0, "llm": 0, "fail": 0}
        from sqlalchemy import func
        for i, (text, ts) in enumerate(zip(todo, times), 1):
            marks = {k: (db.session.query(func.max(m.id)).scalar() or 0) for k, m in SNAP}
            try:
                r = orch.handle_fde(text, creator=CREATORS[i % len(CREATORS)])
            except Exception as e:                                     # noqa: BLE001
                stat["fail"] += 1
                print(f"  ! 第 {i} 条失败：{str(e)[:80]}")
                continue
            # 回填时间：本条链路新产生的所有行
            for k, model in SNAP:
                db.session.query(model).filter(model.id > marks[k]).update(
                    {"created_at": ts}, synchronize_session=False)
            db.session.commit()
            stat["items"] += len(r["items"])
            stat["orders"] += len(r["orders"])
            stat["ledgers"] += len(r["ledgers"])
            stat["llm"] += 1 if r["llm_used"] else 0
            if i % 5 == 0 or i == len(todo):
                print(f"  · {i}/{len(todo)} 已灌入（业务项累计 {stat['items']}，"
                      f"工单 {stat['orders']}，台账 {stat['ledgers']}）")

        shape_orders()
        make_reports(app)
        report(app, stat)


def shape_orders():
    """工单状态分布：早期的办结、新近的处理中或待处理，并补写闭环台账

    同时把**处理人与流转记录**补齐：演示库里每张单都该能点开看到
    "谁派的 → 谁接的 →（有没有换手）→ 谁办结的"。
    没有这一段，92 个工单点开全是"无记录"，处理人列全是"—"，
    等于把刚做的功能藏起来了。
    """
    from datetime import datetime as _dt

    from app import orderflow
    from app.agents.fill import archive_order_feedback
    from app.models import WorkOrderLog

    WorkOrderLog.query.delete()          # 重建演示库时清掉上一次的流水
    orders = WorkOrder.query.order_by(WorkOrder.created_at.asc()).all()
    if not orders:
        return
    staff = (User.query.filter_by(role="property", status="active",
                                  org="幸福里物业").order_by(User.id).all()
             or [None])
    now = _dt.now()
    n = len(orders)
    n_done, n_doing = int(n * 0.6), int(n * 0.2)
    for i, o in enumerate(orders):
        o.order_no = "W" + o.created_at.strftime("%Y%m%d-%H%M%S") + f"-{o.work_item_id:02d}"
        t0 = o.created_at
        orderflow.push(o, "dispatch", actor="派单Agent", actor_org="系统",
                       detail=f"派往 {o.property_co}（{o.sla_level or '一般'} / {o.sla_hours}h）",
                       at=t0)
        if i < n_done:
            who = staff[i % len(staff)]
            t_accept = min(t0 + timedelta(hours=1), now)
            o.assignee, o.assignee_id = (who.name, who.id) if who else (None, None)
            o.assigned_at = t_accept
            orderflow.push(o, "accept", actor=who.name if who else "物业",
                           actor_id=who.id if who else None,
                           actor_org=who.org if who else "", at=t_accept,
                           detail=f"接单处理（{o.sla_level or '一般'} / {o.sla_hours}h）")
            # 三成的单中途换手：流转记录里要有"转派"可看，否则功能等于没演示到
            if len(staff) > 1 and i % 10 < 3:
                nxt = staff[(i + 1) % len(staff)]
                t_tr = min(t_accept + timedelta(hours=1), now)
                o.assignee, o.assignee_id = nxt.name, nxt.id
                orderflow.push(o, "transfer", actor=who.name, actor_id=who.id,
                               actor_org=who.org, at=t_tr,
                               detail=f"{who.name} → {nxt.name}：交接班")
                who = nxt
            o.feedback = pick_feedback(o.content)
            t_done = min(o.assigned_at + timedelta(
                hours=random.Random(o.id).randint(3, 30)), now - timedelta(minutes=5))
            o.done_at = t_done
            orderflow.push(o, "finish", actor=who.name if who else "物业",
                           actor_id=who.id if who else None,
                           actor_org=who.org if who else "", at=t_done,
                           detail=o.feedback)
            lg = archive_order_feedback(o)
            lg.created_at = t_done
            db.session.add(AuditLog(
                actor="幸福里物业", agent="work_order", action="finish",
                output_digest=f"工单{o.order_no} 完成：{o.feedback[:60]}",
                created_at=t_done))
        elif i < n_done + n_doing:
            who = staff[i % len(staff)]
            t_accept = min(t0 + timedelta(hours=1), now)
            o.assignee, o.assignee_id = (who.name, who.id) if who else (None, None)
            o.assigned_at = t_accept
            orderflow.push(o, "accept", actor=who.name if who else "物业",
                           actor_id=who.id if who else None,
                           actor_org=who.org if who else "", at=t_accept,
                           detail=f"接单处理（{o.sla_level or '一般'} / {o.sla_hours}h）")
            db.session.add(AuditLog(
                actor="幸福里物业", agent="work_order", action="accept",
                output_digest=f"工单{o.order_no} 已接单", created_at=t_accept))
        # 其余保持待接单（只有派单一条流水）
    db.session.commit()
    print(f"[工单] 共 {n} 件：办结 {n_done} / 处理中 {min(n_doing, n - n_done)} / "
          f"待处理 {n - n_done - n_doing}，流转记录 {WorkOrderLog.query.count()} 条")


def make_reports(app):
    """生成上个月与本月两份月报，让报表页有环比可比"""
    from app.agents.report import ReportAgent
    agent = ReportAgent()
    now = datetime.now()
    prev = f"{now.year - 1}-12" if now.month == 1 else f"{now.year}-{now.month - 1:02d}"
    Report.query.delete()
    for p in (prev, now.strftime("%Y-%m")):
        d = agent.run({"period": p})
        print(f"[报表] {d['period']}：首录 {d['fde_count']} 条 / 业务项 {d['item_count']} 个 / "
              f"工单 {d['orders']['total']} 件")


def report(app, stat=None):
    from flask import has_app_context
    if not has_app_context():
        with app.app_context():
            return report(app, stat)
    if stat:
        print(f"\n[完成] LLM 产出 {stat['llm']} 条，失败 {stat['fail']} 条")
    rows = [("首录 fde_record", FdeRecord), ("业务项 work_item", WorkItem),
            ("密级 field_security", FieldSecurity), ("工单 work_order", WorkOrder),
            ("台账 ledger", Ledger), ("报表 report", Report),
            ("任务 task", Task), ("审计 audit_log", AuditLog),
            ("知识库 kb_doc", KbDoc), ("账号 user", User)]
    print("\n当前库内容：")
    for label, m in rows:
        print(f"  {label:24s} {m.query.count():>5d}")
    db_path = app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")
    if os.path.exists(db_path):
        print(f"  数据库文件 {db_path} ({os.path.getsize(db_path) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
