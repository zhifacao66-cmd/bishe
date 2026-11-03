"""给演示库造公文与周报样本（幂等，按标题/周期查重）

为什么走 docflow.push 而不是直接写 doc_status：
  页面上要展示"谁在哪一步签的字"。直接改字段虽然快，
  但流转记录是空的——点开详情只有当前状态，没有过程，
  演示时最该讲的"全程留痕"反而没东西可看。

运行：python scripts/seed_docs.py
"""
import pathlib
import sys
from datetime import date, datetime, timedelta

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import create_app  # noqa: E402
from app.docflow import ALLOWED_FROM, make_doc_no, push  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Notice, Weekly  # noqa: E402
from app.weekly import collect, current_period, parse_period, render  # noqa: E402

DOCS = [
    dict(
        title="关于申请增补 3 栋高层住宅消防设施的请示",
        doc_type="请示", secret_level="内部", urgency="急件",
        to_org="幸福里街道办事处", cc_org="", signer="王社工",
        issuer="幸福里社区居民委员会",
        body_md=(
            "幸福里街道办事处：\n\n"
            "我社区 3 栋高层住宅建成于 2008 年，现有消防栓水压不足，"
            "经物业与社区联合排查，需增补消防水带 12 条、灭火器 24 具。\n\n"
            "一、基本情况\n"
            "3 栋共 6 个单元、居民 216 户，现有消防设施超期服役。\n\n"
            "二、申请事项\n"
            "申请从街道消防专项经费中列支 1.8 万元用于增补。\n\n"
            "妥否，请批示。"
        ),
        steps=["check", "sign", "seal"],          # 停在已用印，待分发
    ),
    dict(
        title="关于 2026 年第三季度社区治理工作情况的报告",
        doc_type="报告", secret_level="内部", urgency="平件",
        to_org="幸福里街道办事处", cc_org="街道综治中心", signer="王社工",
        issuer="幸福里社区居民委员会",
        body_md=(
            "幸福里街道办事处：\n\n"
            "现将我社区第三季度治理工作情况报告如下。\n\n"
            "一、矛盾纠纷化解\n"
            "受理邻里纠纷 14 起，化解 13 起，1 起引导走司法途径。\n\n"
            "二、安全隐患整治\n"
            "排查楼道堆物 37 处，已清理 35 处；飞线充电 12 起，已整改。\n\n"
            "三、下一步\n"
            "重点推进电动车集中充电棚建设，预计 11 月底前完成选址。"
        ),
        steps=["check", "sign", "seal", "send", "archive"],
    ),
    dict(
        title="关于协同做好冬季供暖设备检修的函",
        doc_type="函", secret_level="公开", urgency="平件",
        to_org="幸福里物业服务有限公司", cc_org="", signer="王社工",
        issuer="幸福里社区居民委员会",
        body_md=(
            "幸福里物业服务有限公司：\n\n"
            "冬季供暖期将至，请贵司于 11 月 5 日前完成 1—8 栋供暖管道打压测试，"
            "并将检修结果书面反馈我社区。\n\n"
            "特此函达。"
        ),
        steps=["sign", "seal", "send"],
    ),
]


def main():
    app = create_app("dev")
    with app.app_context():
        made = 0
        for spec in DOCS:
            n = Notice.query.filter_by(title=spec["title"], direction="out").first()
            if n is not None:
                # 上次可能中途失败（创建成功、某个 push 抛错），这里接着往下推，
                # 而不是整份跳过——否则库里留一份停在草稿、没有文号的半成品。
                print(f"[resume] {spec['title']} 当前 {n.doc_status or '未拟稿'}")
                if not n.signer and spec.get("signer"):
                    n.signer = spec["signer"]      # 早期失败留下的空字段回填
                if "seal" in spec["steps"] and not n.doc_no:
                    n.doc_no = make_doc_no(n)
                for act in spec["steps"]:
                    if n.doc_status in ALLOWED_FROM[act]:
                        push(n, act, actor="王社工" if act != "seal" else "张主任", note="")
                        db.session.commit()
                        print(f"   → {act}: {n.doc_status}")
                continue
            n = Notice(
                title=spec["title"], doc_type=spec["doc_type"], direction="out",
                doc_status="draft", kind="original",
                level="社区", issuer=spec["issuer"],
                to_org=spec["to_org"], cc_org=spec["cc_org"],
                secret_level=spec["secret_level"], urgency=spec["urgency"],
                signer=spec["signer"], body_md=spec["body_md"],
                summary=spec["title"][:40], status="pending",
                created_by="王社工", issued_at=date.today(),
            )
            db.session.add(n)
            db.session.commit()

            if "seal" in spec["steps"]:
                n.doc_no = make_doc_no(n)
            for act in spec["steps"]:
                push(n, act, actor="王社工" if act != "seal" else "张主任",
                     note={"check": "文种、格式、密级已核",
                           "sign": "同意报出",
                           "seal": "已用印"}.get(act, ""))
                db.session.commit()
            made += 1
            print(f"[ok] {n.doc_no or '未编号'} {n.title} → {n.doc_status}")

        # 周报：上周（已过完的一周），数字用真实取数，保证点开能核
        period = current_period(date.today() - timedelta(days=7))
        if Weekly.query.filter_by(period=period).first():
            print(f"[skip] 周报已存在：{period}")
        else:
            start, end = parse_period(period)
            payload = collect(start, end)
            body = render(payload)
            body = body.replace(
                "（请结合本周实际补充；可点「模型起草」生成初稿后修改）",
                "本周超时工单集中在配件采购环节，2 张工单等待供应商到货；"
                "另有 1 件邻里纠纷经两次调解未达成一致，已引导走司法途径。")
            body = body.replace(
                "（请结合本周实际补充）",
                "1. 催办配件到货并完成 2 张超时工单闭环；\n"
                "2. 配合街道完成 3 栋消防设施增补的现场核量；\n"
                "3. 组织一次楼道堆物集中清理。")
            w = Weekly(period=period, start_date=start, end_date=end,
                       data_json=__import__("json").dumps(payload, ensure_ascii=False),
                       body_md=body, status="final", editor="王社工",
                       signed_by="王社工", signed_at=datetime.now(), revision=1)
            db.session.add(w)
            db.session.commit()
            print(f"[ok] 周报 {period}（{payload['counts']['fde']} 条首录、"
                  f"{payload['counts']['wo_new']} 张工单）已签发")

        print(f"[done] 新增公文 {made} 份")


if __name__ == "__main__":
    main()
