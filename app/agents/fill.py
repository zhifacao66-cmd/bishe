"""填报Agent：政务类业务项 → 台账自动填充 + 历史档案复用去重。
契约：输入 {work_item_id, type, subject, target_lines, fields}
输出：{ledgers: [{line, ledger_id, reused}]}
复用规则（v1 规则版，LLM 部署后可升级为语义匹配）：
  提取门牌特征（X栋/X单元/XXX室），同业务线已有台账含相同特征 → 复用，不重复建档。
"""
import json
import re
from app.agents.base import BaseAgent
from app.extensions import db
from app.models import Ledger

ROOM_PAT = re.compile(r"\d+栋|\d+单元|\d{3,4}室|\d{3,4}户?")


def extract_room_key(text: str) -> str:
    return "".join(ROOM_PAT.findall(text or ""))


class FillAgent(BaseAgent):
    name = "fill"

    def run(self, payload: dict) -> dict:
        results = []
        room_key = extract_room_key(payload.get("subject", ""))
        for line in payload.get("target_lines", []):
            if line == "物业":
                continue  # 物业线由派单Agent负责
            reused = False
            # 查重：同业务线 + 门牌特征匹配 → 复用历史档案
            if room_key:
                dup = (Ledger.query.filter_by(line=line)
                       .filter(Ledger.content_json.contains(room_key)).first())
                reused = dup is not None
            content = {
                "type": payload.get("type"),
                "subject": payload.get("subject"),
                "fields": payload.get("fields", {}),
                "room_key": room_key,
                "reused_archive": reused,
                "source": "FDE自动填报",
            }
            lg = Ledger(
                work_item_id=payload["work_item_id"],
                line=line,
                content_json=json.dumps(content, ensure_ascii=False),
                # 同一份事实写两处：JSON 供页面展示，列供统计聚合。
                # 只写 JSON 的话，统计要么扫全表反序列化，要么口径出错。
                reused=reused,
            )
            db.session.add(lg)
            db.session.flush()
            results.append({"line": line, "ledger_id": lg.id, "reused": reused,
                            "subject": payload.get("subject")})
        self._audit("fill", payload.get("subject", ""), json.dumps(results, ensure_ascii=False))
        return {"ledgers": results}


def archive_order_feedback(order) -> Ledger:
    """工单完成 → 台账补全流程记录（供工单接口调用）"""
    content = {
        "type": "物业工单闭环",
        "order_no": order.order_no,
        "content": order.content,
        "feedback": order.feedback,
        "property_co": order.property_co,
        "source": "工单自动归档",
    }
    lg = Ledger(work_item_id=order.work_item_id, line="物业",
                content_json=json.dumps(content, ensure_ascii=False),
                reused=False)      # 工单闭环是新增记录，不是复用档案
    db.session.add(lg)
    db.session.flush()
    return lg
