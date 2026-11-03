"""Agent 基类：契约约定见《总体设计》第四节。
每个 Agent 单一职责，输入/输出均为 dict，异常由调度器统一兜底。
"""
import re

from app.extensions import db
from app.models import AuditLog

# 审计摘要在落库前统一再做一次敏感值遮蔽。
# 原因：Agent 的 input_digest 往往是脱敏前的原文，若直接入库，
# 手机号/身份证明文会绕过脱敏进入审计日志（等保检查的重点项）。
_AUDIT_MASKS = (
    # 身份证在前：避免 18 位号码的中段被手机号规则先命中而残缺
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), lambda m: m[:6] + "********" + m[-4:]),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), lambda m: m[:3] + "****" + m[-4:]),
)


def mask_sensitive(text: str) -> str:
    """对任意文本做敏感值遮蔽（审计/日志兜底用）。"""
    s = text or ""
    for pat, fn in _AUDIT_MASKS:
        s = pat.sub(lambda m: fn(m.group(0)), s)
    return s


class BaseAgent:
    name = "base"

    def run(self, payload: dict) -> dict:
        raise NotImplementedError

    def _audit(self, action: str, input_digest: str, output_digest: str):
        # 先脱敏再截断（顺序不能反：截断可能把号码切半导致漏检）
        db.session.add(AuditLog(
            actor="system", agent=self.name, action=action,
            input_digest=mask_sensitive(input_digest)[:250],
            output_digest=mask_sensitive(output_digest)[:250],
        ))
        db.session.commit()
