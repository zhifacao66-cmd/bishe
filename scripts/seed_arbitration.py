"""给演示库造两条争议工单，让仲裁台不是空的

幂等：已经有 disputed 的单就跳过，不会重复造。

为什么需要这个：仲裁台是这一轮的核心界面，但演示库里所有工单都是
"物业办完、居民没表态"的状态——打开页面只有 0，看不到任何东西。
而"造一条争议"必须走 `arbitration.resident_confirm()` 而不是直接改字段，
否则链上不会有 `dispute` 记录，详情页的时间线会缺一块，
演示时反而露怯。

运行：python scripts/seed_arbitration.py
"""
import os
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
os.environ.setdefault("NOTICE_AUTO_IMPORT", "0")

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import ResidentTicket, User, WorkOrder  # noqa: E402
from app import arbitration  # noqa: E402

app = create_app("dev")

# 一条待裁（居民说没修好）、一条已裁（居委会判业主自用）
PLAN = [
    {
        "note": "修完当天晚上又漏了，墙上还是湿的，维修工说不是他们负责的",
        "dispute_type": "quality",
        "judge": None,
    },
    {
        "note": "灯是换好了，但走廊另一头还是黑的，物业说那一段不归他们管",
        "dispute_type": "duty",
        "judge": {
            "verdict": "public_duty",
            "basis": "该段属公共部位照明，依据《物业服务合同》第 3 条由物业维护，"
                     "但本次故障为线路老化，费用应从公共收益列支",
            "note": "已通知物业下周统一更换线路",
        },
    },
]


def _force_dispute(order, spec, ticket):
    """不经过 resident_confirm 时，手工把"居民不服"落到位并补链"""
    from app import evidence
    order.confirm_status = "disputed"
    order.confirm_note = spec["note"]
    order.dispute_type = spec["dispute_type"]
    order.confirm_at = __import__("datetime").datetime.now()
    evidence.append("order", order.id, "dispute", {
        "order_no": order.order_no,
        "result": "disputed",
        "note": spec["note"][:180],
        "dispute_type": spec["dispute_type"],
    }, actor=f"居民(尾号{ticket.phone[-4:]})" if ticket else "居民")
    db.session.commit()


def main():
    with app.app_context():
        existing = (WorkOrder.query
                    .filter(WorkOrder.confirm_status == "disputed").count())
        if existing:
            print(f"[skip] 库里已有 {existing} 张争议单，不再重复造")
            return 0

        judge_user = (User.query.filter_by(username="wang").first()
                      or User.query.filter_by(role="community").first())
        if judge_user is None:
            print("[失败] 找不到社区账号，先跑 scripts/seed_demo.py")
            return 1

        done = (WorkOrder.query.filter_by(status="done")
                .order_by(WorkOrder.done_at.desc()).limit(len(PLAN)).all())
        if len(done) < len(PLAN):
            print(f"[失败] 已办结工单只有 {len(done)} 张，不够造 {len(PLAN)} 条争议")
            return 1

        # 用一个"代居民提交"的诉求单挂在链上，让争议有来源可查。
        # 复用现有 ticket 的 fde（不新建 fde，避免再造一遍拆解）；
        # 没有就跳过这一步，争议本身仍然成立。
        ticket = ResidentTicket.query.order_by(ResidentTicket.id.desc()).first()

        for order, spec in zip(done, PLAN):
            if ticket:
                okc, msg, n = arbitration.resident_confirm(
                    ticket, False, spec["note"], spec["dispute_type"])
                if not okc:
                    # 演示库那张诉求下的工单和这两张对不上时会走到这里。
                    # 落字段之外**必须补一条链**：详情页的存证时间线读的是
                    # evidence_chain，不写链就会出现"居民不服、链上没有这一步"，
                    # 演示时一眼就看出是手改的数据库。
                    print(f"[warn] 走居民表态失败（{msg}），改为直接落字段：{order.order_no}")
                    _force_dispute(order, spec, ticket)
            else:
                _force_dispute(order, spec, None)

            if spec["judge"]:
                okj, res = arbitration.judge(
                    order, spec["judge"]["verdict"], spec["judge"]["basis"],
                    judge_user, note=spec["judge"]["note"],
                    dispute_type=spec["dispute_type"])
                print(f"  [{'ok' if okj else '失败'}] {order.order_no} 裁决 → {res}")
                db.session.commit()
            else:
                print(f"  [ok] {order.order_no} 已置为待裁决")

        from app import evidence
        evidence.anchor("seed-arbitration")
        db.session.commit()

        st = arbitration.stats()
        print(f"[完成] 待裁 {st['pending']} / 已裁 {st['judged']} / "
              f"驳回 {st['rejected']}（驳回率 {st['reject_rate']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
