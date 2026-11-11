"""拆解Agent：自然语言 → 结构化业务项列表。

双路径设计：
- LLM 路径（默认）：本地 Ollama 模型 + 强约束 prompt + JSON 容错解析
- 规则路径（兜底）：关键词匹配，保证模型未部署/超时/输出非法时流程不中断

设计要点：
1. prompt 内固化「业务线映射规则」和 few-shot 示例，降低小模型的漂移
2. 解析层做三重容错：去 markdown 围栏 → 兼容 {"items": [...]} 包装 → 单对象转数组
3. 归一化层做域校验：type 必须在枚举内，target_lines 必须落在 5 条业务线内，
   非法值不允许直接透传到下游 Agent（派单/填报依赖这些字段决策）
"""
import json
import re

from app.agents.base import BaseAgent
from app.llm.client import chat, LLMUnavailable

# 业务项类型枚举（与《总体设计》业务分类一致）
TYPES = ("报修", "人口变动", "走访", "调解", "咨询", "隐患排查", "其他")
# 业务线枚举（去向）
LINES = ("物业", "综治", "民政", "卫健", "街道")

SYSTEM = """你是社区业务拆解助手，负责把社区工作者的一段口语化记录，拆成可流转的结构化业务项。

【输出格式】只输出一个 JSON 数组，不要任何解释文字、不要 markdown 代码块。数组每项包含 4 个字段：
- type: 字符串，只能是 "报修" / "人口变动" / "走访" / "调解" / "咨询" / "隐患排查" / "其他" 之一
- subject: 字符串，20 字以内的事项摘要，去掉口语和语气词
- fields: 对象，抽取到的关键字段（如 楼栋/房号/姓名/电话/时间/问题描述），没有则给空对象
- target_lines: 字符串数组，只能是 "物业" / "综治" / "民政" / "卫健" / "街道" 中的一个或多个

【类型判定】
- 报修：设施设备损坏需维修（电梯、灯、门禁、水管、漏水、绿化设施）
- 人口变动：人口登记、迁入迁出、租户变动、居住证
- 走访：上门探视、慰问、核查（独居/高龄/残疾/低保/患者）
- 调解：邻里矛盾、纠纷（噪音、车位、赔偿、装修）
- 咨询：居民来问政策、补贴、办理流程
- 隐患排查：消防通道堵塞、群租、私拉电线、楼道堆物等风险，不涉及已发生的损坏
- 其他：会议通知、报表上报等行政事务

【业务线映射规则】
- 物业：设施设备维修、公共区域问题、门禁、电梯、漏水、保洁、绿化
- 综治：人口登记、租户变动、矛盾纠纷、治安、消防隐患、群租
- 民政：低保、独居/高龄老人、残疾、救助、走访慰问
- 卫健：健康状况、慢病、精神障碍、疫情防控、体检
- 街道：需要上报街道统筹、跨部门协调的事项

【拆分原则（最重要）】
- 一句话里含多件事，必须拆成多项。特别注意"另外/还有/顺便/同时/也/以及/加上"等
  并列或追加表述：前后各是一件事，**即使类型相同也要拆开**（例：楼下垃圾桶满了，
  另外健身器材也坏了 → 2 项，都是报修，不要合并成 1 项）
- **反过来，同一件事的后果、原因、补充说明不要拆**：例如"电梯坏了，老人下不了楼，
  麻烦尽快修"是 1 件事（老人下不了楼是后果，不是新事项）；"低保户家孩子考上大学，
  上门了解助学需求"是 1 件事（上门了解是走访方式，不是咨询）
- 判据：能否独立成为一条要流转、要办结的工单/台账；不能就是同一件事
- 同一件事涉及多条业务线，可给多个 target_lines
- 不要臆造原文没有的信息；原文没提到的字段不要填

【示例】
输入：2栋2单元202室新租户李某，电话13800001234，顺便说楼道灯也坏了
输出：[{"type":"人口变动","subject":"2栋2单元202室新租户李某登记","fields":{"楼栋":"3栋","单元":"2单元","房号":"501室","姓名":"李某","电话":"13800001234"},"target_lines":["综治"]},{"type":"报修","subject":"3栋2单元楼道灯损坏","fields":{"楼栋":"3栋","单元":"2单元","问题":"楼道灯损坏"},"target_lines":["物业"]}]

输入：今天去看望7栋的王奶奶，她血压有点高，家里下水道还堵了
输出：[{"type":"走访","subject":"走访7栋独居老人王奶奶","fields":{"楼栋":"7栋","姓名":"王奶奶","情况":"血压偏高"},"target_lines":["民政","卫健"]},{"type":"报修","subject":"7栋王奶奶家下水道堵塞","fields":{"楼栋":"7栋","问题":"下水道堵塞"},"target_lines":["物业"]}]

输入：1栋楼下垃圾桶满了好几天没人清运，另外3栋的健身器材也坏了
输出：[{"type":"报修","subject":"1栋垃圾桶满溢未清运","fields":{"楼栋":"1栋","问题":"垃圾桶满溢"},"target_lines":["物业"]},{"type":"报修","subject":"3栋健身器材损坏","fields":{"楼栋":"3栋","问题":"健身器材损坏"},"target_lines":["物业"]}]

输入：10栋架空层有人私拉电线给电动车充电，很危险
输出：[{"type":"隐患排查","subject":"10栋架空层私拉电线充电","fields":{"楼栋":"10栋","问题":"私拉电线给电动车充电"},"target_lines":["综治"]}]

输入：明天上午九点街道开会，要求各社区上报本月消防检查台账
输出：[{"type":"其他","subject":"街道消防检查台账上报会议","fields":{"时间":"明天上午九点","事项":"上报消防检查台账"},"target_lines":["街道","综治"]}]"""

# 结构化输出约束：强制顶层为数组、枚举值合法，避免模型输出单对象或漏拆
SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": list(TYPES)},
            "subject": {"type": "string"},
            "fields": {"type": "object"},
            "target_lines": {
                "type": "array",
                "items": {"type": "string", "enum": list(LINES)},
            },
        },
        "required": ["type", "subject", "fields", "target_lines"],
    },
}


class ParserAgent(BaseAgent):
    name = "parser"

    def __init__(self, use_schema: bool = True, use_retry: bool = True,
                 think: bool | None = None, audit: bool = True):
        """可配置化，便于消融实验对比不同策略组合。

        use_schema: True 用 JSON Schema 约束解码；False 退回 format:"json"
        use_retry : True 开启预分句校验重试
        think     : 覆盖思考链开关，None 表示用配置文件
        audit     : 是否写审计日志（评测时关闭，避免污染审计表）
        """
        self.use_schema = use_schema
        self.use_retry = use_retry
        self.think = think
        self.audit = audit
        self.retry_count = 0      # 预分句重试实际触发次数（供消融实验统计）

    def run(self, payload: dict) -> dict:
        text = payload["raw_text"]
        # 只有公网匿名入口会传更短的排队上限；内部入口保持全局 LLM_QUEUE_TIMEOUT
        queue_timeout = payload.get("queue_timeout")
        source = "llm"
        try:
            kw = {"schema": SCHEMA} if self.use_schema else {"json_mode": True}
            out = chat(text, system=SYSTEM, think=self.think, **kw,
                       queue_timeout=queue_timeout)
            items = self._normalize(self._loads(out))
            # 预分句校验：并列词切出的子句数 > 输出项数 → 疑似漏拆，带提示重试一次
            if self.use_retry:
                segs = self._split_clauses(text)
                if len(segs) > len(items) and len(segs) > 1:
                    self.retry_count += 1
                    hint = (
                        f"注意：这段话包含 {len(segs)} 件独立事项，已按并列词切分为："
                        + " ｜ ".join(segs)
                        + f"。你必须输出 {len(segs)} 个业务项，逐条对应，不得合并。"
                    )
                    out2 = chat(text, system=SYSTEM + "\n\n" + hint,
                                think=self.think, queue_timeout=queue_timeout, **kw)
                    items2 = self._normalize(self._loads(out2))
                    if len(items2) > len(items):
                        items = items2
        except (LLMUnavailable, ValueError, json.JSONDecodeError, TypeError):
            items = self._fallback(text)
            source = "fallback"
        result = {"items": items, "source": source}
        if self.audit:
            self._audit("parse", text, json.dumps(items, ensure_ascii=False))
        return result

    @staticmethod
    def _split_clauses(text: str) -> list:
        """按并列/追加连接词粗切子句，用于检测漏拆（不追求语义精确，只做计数校验）"""
        parts = re.split(r"[，,；;、]?\s*(?:另外|还有|顺便(?:说|提)?|同时|此外|以及|加上)\s*", text)
        parts = [p.strip(" ，,；;、。") for p in parts]
        parts = [p for p in parts if len(p) >= 4]
        if len(parts) <= 1:
            # 退化情形：用分号切开再判
            sub = [p.strip(" ，,。") for p in re.split(r"[；;]", text) if len(p.strip()) >= 4]
            parts = sub if len(sub) > 1 else parts
        return parts

    # ---------- 解析容错 ----------
    @staticmethod
    def _loads(raw: str) -> list:
        """把模型输出尽力解析成 list，失败抛异常由上层降级"""
        s = (raw or "").strip()
        # ① 去 markdown 围栏 ```json ... ```
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S).strip()
        try:
            data = json.loads(s)
        except json.JSONDecodeError:
            # ② 从噪声文本里截取第一个 JSON 数组
            m = re.search(r"\[.*\]", s, flags=re.S)
            if not m:
                raise
            data = json.loads(m.group(0))
        # ③ 兼容 {"items": [...]} / {"data": [...]} 包装
        if isinstance(data, dict):
            for k in ("items", "data", "result", "list"):
                if isinstance(data.get(k), list):
                    data = data[k]
                    break
            else:
                data = [data]        # 单对象 → 数组
        if not isinstance(data, list) or not data:
            raise ValueError("LLM 输出非有效数组")
        return data

    @staticmethod
    def _normalize(items: list) -> list:
        """域校验与字段归一化，防止非法值污染下游决策"""
        clean = []
        for it in items:
            if not isinstance(it, dict):
                continue
            t = str(it.get("type", "")).strip()
            subject = str(it.get("subject", "")).strip()
            if not subject:
                continue
            # type 兜底：不在枚举内则归为「其他」
            if t not in TYPES:
                t = "其他"
            # target_lines 过滤到合法业务线，全非法则按 type 给默认去向
            raw_lines = it.get("target_lines") or it.get("target_line") or []
            if isinstance(raw_lines, str):
                raw_lines = re.split(r"[,,/、;；\s]+", raw_lines)
            lines = [x for x in (str(v).strip() for v in raw_lines) if x in LINES]
            if not lines:
                lines = {"报修": ["物业"], "人口变动": ["综治"], "走访": ["民政"],
                         "调解": ["综治"], "咨询": ["综治"],
                         "隐患排查": ["综治"]}.get(t, ["综治"])
            # 去重保序
            lines = list(dict.fromkeys(lines))
            fields = it.get("fields")
            if not isinstance(fields, dict):
                fields = {}
            clean.append({"type": t, "subject": subject[:40],
                          "fields": fields, "target_lines": lines})
        if not clean:
            raise ValueError("归一化后无有效业务项")
        return clean

    # ---------- 规则兜底 ----------
    def _fallback(self, text: str) -> list:
        """规则桩：关键词匹配，保证模型不可用时流程仍可跑通"""
        items = []
        if any(k in text for k in ("坏", "修", "漏水", "灯", "堵", "电梯", "门禁")):
            items.append({"type": "报修", "subject": text[:30],
                          "fields": {}, "target_lines": ["物业"]})
        if any(k in text for k in ("租户", "入住", "搬", "登记", "人口")):
            items.append({"type": "人口变动", "subject": text[:30],
                          "fields": {}, "target_lines": ["综治"]})
        if any(k in text for k in ("走访", "老人", "探视", "慰问")):
            items.append({"type": "走访", "subject": text[:30],
                          "fields": {}, "target_lines": ["民政"]})
        if any(k in text for k in ("纠纷", "调解", "噪音", "矛盾")):
            items.append({"type": "调解", "subject": text[:30],
                          "fields": {}, "target_lines": ["综治"]})
        if not items:
            items.append({"type": "其他", "subject": text[:30],
                          "fields": {}, "target_lines": ["综治"]})
        return items
