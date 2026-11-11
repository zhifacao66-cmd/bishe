"""Orchestrator 调度器：串联 FDE → 保密审查 → 拆解 → 入库 → 派单/填报

## 从"管道"升级成"黑板"

第一版是纯管道：每个 Agent 的输出直接当下一个的输入，谁也不能影响谁。
那样写的"多 Agent"和把函数顺序调一遍没有区别——答辩问"多主体强在哪"
答不上来。

现在多了一块黑板（`app/agents/blackboard.py`）：多个 Agent 可以对**同一件事**
各自表态，表态不一致时按明确规则消解，消解过程和每一方的依据都落进
`agent_step` 表（`/trace` 页面能回放）。

目前真正会分歧的只有一件事：**这张工单该按几小时办结**。

    派单Agent：按知识库《物业报修响应时限与处置规范》词表定级
    风险复核：按居民历史（30 天内重复报修、跨单位推诿史）和原文安全隐患词提异议
    消解    ：更严者胜——合规要求只能加严，不能放宽

实测（41 张单）：约 1/4 的单被升级，其中若干张从 72h 直接压到 4h。

## 为什么定级要在建单之前

如果先让 DispatchAgent 按词表建单、再让风险复核改 `sla_hours`，
工单流转链上会留下一次错的定级（`dispatch` 那条 log 写着 72h），
事后没法解释"为什么这条记录说 72 小时而工单是 4 小时"。
所以决策权收归调度器：先让两个 Agent 表态、消解完，再带着结论去建单。
Agent 只执行，不改结论。

## 事务边界

`db.session.commit()` 位置是踩过坑的：flush 只把 INSERT 发出去、事务仍开着
并持有 SQLite 写锁，而紧接着的 parser 要调本地模型（2s 左右），这段时间
锁一直被占着，并发时别人的审计/工单写入会排队到 busy_timeout 超时。
所以拆成「落原文」+「写业务项」两个短事务。**模型调用绝不能放在事务里。**
"""
import json
import time

from app.extensions import db
from app.models import FdeRecord, FieldSecurity, WorkItem
from app.agents.blackboard import Blackboard, SLA_HOURS
from app.agents.dispatch import DispatchAgent, classify_sla
from app.agents.fill import FillAgent
from app.agents.knowledge import KnowledgeAgent
from app.agents.parser import ParserAgent
from app.agents.riskreview import RiskReviewAgent
from app.agents.security import SecurityAgent


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000) if t0 else 0


class Orchestrator:
    def __init__(self):
        self.parser = ParserAgent()
        self.security = SecurityAgent()
        self.dispatch = DispatchAgent()
        self.risk = RiskReviewAgent()
        self.fill = FillAgent()
        self.knowledge = KnowledgeAgent()

    # ------------------------------------------------------------------ 主链路

    def handle_fde(self, raw_text: str, source: str = "text", creator: str = "",
                   run_id: str = "", queue_timeout: float | None = None) -> dict:
        bb = Blackboard(run_id)

        # ① 保密审查（先脱敏再入库/入模型，最小暴露）
        t = time.perf_counter()
        sec = self.security.run({"raw_text": raw_text})
        found = sec["findings"]
        bb.step("security", "mask",
                decision=f"脱敏 {len(found)} 处"
                         + ("（" + "、".join(f["type"] for f in found[:3]) + "）"
                            if found else ""),
                reason="身份证/手机号按正则遮蔽，原文不入库也不进模型",
                took_ms=_ms(t))

        # ② 落库原始记录（保存脱敏后文本）
        rec = FdeRecord(raw_text=sec["masked_text"], source=source, creator=creator)
        db.session.add(rec)
        # 必须在此提交，而不是 flush。
        db.session.commit()
        # 回填 fde_id：①步发生时记录还没建出来
        bb.steps[0]["fde_id"] = rec.id

        # ③ 拆解（调模型，约 2s，必须在事务外）
        t = time.perf_counter()
        parsed = self.parser.run({"raw_text": sec["masked_text"],
                                  "queue_timeout": queue_timeout})
        degraded = parsed["source"] != "llm"
        bb.step("parser", "parse",
                decision=f"拆出 {len(parsed['items'])} 个业务项",
                reason=("模型结构化输出（SCHEMA 约束解码）" if not degraded
                        else "模型不可用，走规则预分句降级"),
                took_ms=_ms(t), degraded=degraded, fde_id=rec.id)

        # ④ 业务项入库 + 物业类派单 + 政务类填报
        saved, orders, ledgers = [], [], []
        for it in parsed["items"]:
            wi = WorkItem(
                fde_id=rec.id, type=it.get("type"), subject=it.get("subject"),
                fields_json=json.dumps(it.get("fields", {}), ensure_ascii=False),
                target_lines=",".join(it.get("target_lines", [])),
            )
            db.session.add(wi)
            db.session.flush()
            # ⑦ 字段三级管控：给每个结构化字段打密级并落库（field_security）
            #    这样"哪条业务项的哪个字段属于敏感个人信息"是可查、可审计的，
            #    而不是只靠脱敏后的文本，事后无法举证。
            sec_fields = self.security.classify_fields(it.get("fields"))
            for sf in sec_fields:
                db.session.add(FieldSecurity(work_item_id=wi.id,
                                             field=sf["field"], level=sf["level"]))
            saved.append({"id": wi.id, "type": wi.type, "subject": wi.subject,
                          "target_lines": it.get("target_lines", []),
                          "security": sec_fields,
                          "sec_max": max([sf["level"] for sf in sec_fields], default=1),
                          "kb_hits": self._kb_lookup(wi.type, wi.subject, it.get("fields"))})
            lines = it.get("target_lines") or []

            # ⑤ 去向含"物业" → 定级 → 风险复核 → 消解 → 建单
            if "物业" in lines:
                orders.append(self._dispatch_with_review(bb, wi, rec.id))

            # ⑥ 政务业务线 → 填报Agent 写台账（复用去重）
            if any(l != "物业" for l in lines):
                fr = self.fill.run({
                    "work_item_id": wi.id, "type": wi.type,
                    "subject": wi.subject, "target_lines": lines,
                    "fields": it.get("fields", {}),
                })
                ledgers.extend(fr["ledgers"])
                reused = sum(1 for x in fr["ledgers"] if x["reused"])
                bb.step("fill", "fill",
                        decision=f"写台账 {len(fr['ledgers'])} 条"
                                 + (f"，复用历史档案 {reused} 条" if reused else ""),
                        reason="按门牌特征查同业务线历史档案，命中则复用不重复建档",
                        fde_id=rec.id, work_item_id=wi.id)

        db.session.commit()

        # ⑧ 轨迹落库（独立提交：轨迹写失败不能把主链路一起回滚）
        try:
            db.session.add_all(bb.pending_rows())
            db.session.commit()
        except Exception:       # noqa: BLE001 —— 轨迹是旁路，宁可丢也不能拖垮主链路
            db.session.rollback()

        return {
            "fde_id": rec.id,
            "run_id": bb.run_id,
            "masked_text": sec["masked_text"],
            "sensitive_findings": found,
            "items": saved,
            "orders": orders,
            "ledgers": ledgers,
            "llm_used": parsed["source"] == "llm",
            "collab": {
                "steps": len(bb.steps),
                "conflicts": bb.conflicts(),
                "resolutions": bb.resolutions,
            },
        }

    # ------------------------------------------------------------------ 派单

    def _dispatch_with_review(self, bb: Blackboard, wi, fde_id: int) -> dict:
        """定级 → 复核 → 消解 → 建单。三个 Agent 在这里第一次真正互相影响。"""
        subject = (wi.subject or "").strip()

        # 表态 1：派单Agent 按规范词表定级
        level, hours = classify_sla(subject)
        bb.step("dispatch", "claim", topic="sla",
                decision=f"{level} {hours}h",
                reason="按《物业报修响应时限与处置规范》词表命中定级",
                fde_id=fde_id, work_item_id=wi.id)

        # 表态 2：风险复核（可以弃权）
        rv = self.risk.review({"subject": subject, "level": level})
        if rv.get("level"):
            bb.step("risk", "claim", topic="sla",
                    decision=f"{rv['level']} {rv['hours']}h",
                    reason=f"{rv['reason']}（置信度 {rv['confidence']}）",
                    fde_id=fde_id, work_item_id=wi.id)

        # 消解：有分歧才有记录；一致或弃权都不产生结论
        res = bb.resolve("sla", fde_id=fde_id, work_item_id=wi.id)
        if res and res["winner"] == "risk":
            level, hours = rv["level"], rv["hours"]
            note = f"风险复核升级：{rv['reason']}"
        elif res:
            # 理论上不会发生（复核只会加严），但代码不假设 Agent 的行为
            level = res["value"].split(" ")[0]
            hours = SLA_HOURS.get(level, hours)
            note = f"消解采纳派单Agent 定级（复核意见 {res['value']}）"
        else:
            note = "按词表命中定级，复核无异议"

        t = time.perf_counter()
        od = self.dispatch.run({
            "work_item_id": wi.id,
            "subject": subject,
            "content": subject,      # 只传本项摘要，不传整段原文（最小必要）
            "sla_level": level,
            "sla_hours": hours,
            "sla_note": note,
        })
        bb.step("dispatch", "dispatch",
                decision=f"{od['order_no']} → {od['property_co']}（{level} {hours}h）",
                reason=note, took_ms=_ms(t), fde_id=fde_id, work_item_id=wi.id,
                order_no=od["order_no"])
        od["sla_note"] = note
        od["upgraded"] = bool(res and res["winner"] == "risk")
        od["review_signals"] = rv.get("signals", [])
        return od

    # ------------------------------------------------------------------ 知识库

    def _kb_lookup(self, item_type: str, subject: str, fields: dict | None) -> list[dict]:
        """咨询类事项去知识库找政策依据（纯检索，几毫秒，不调模型不拖慢主链路）"""
        if item_type != "咨询" or not subject:
            return []
        query = subject + " " + " ".join(str(v) for v in (fields or {}).values())
        hits = self.knowledge.run({"query": query, "top_k": 2})["hits"]
        seen, out = set(), []
        for h in hits:                      # 同一文档的多个片段只保留最相关的一条
            if h["title"] in seen:
                continue
            seen.add(h["title"])
            out.append({"title": h["title"], "heading": h["heading"],
                        "score": h["score"], "snippet": h["snippet"]})
        return out
