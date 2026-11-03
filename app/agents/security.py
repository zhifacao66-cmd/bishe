"""保密审查Agent：识别敏感字段并按级别处理（正则先行，模型增强）。
level: 1 明文 / 2 脱敏 / 3 加密
"""
import re
from app.agents.base import BaseAgent

# 顺序很重要：身份证（18 位）必须先于手机号（11 位）匹配。
# 否则身份证中段的 "19900307451" 会被手机号正则误命中，把身份证打碎，
# 导致 `\d{17}[\dXx]` 再也匹配不上，最终只遮住 4 位、其余 14 位仍可读。
# 两个正则都加 (?<!\d)/(?!\d) 边界，避免在长数字串内部起匹配。
PATTERNS = {
    "身份证": (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), 3,
              lambda m: m[:6] + "********" + m[-4:]),
    "手机号": (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), 3,
              lambda m: m[:3] + "****" + m[-4:]),
}

# ---------------------------------------------------------------- 字段三级管控
# 1 明文   ：事项本身、楼栋等公开信息，可直接展示
# 2 脱敏   ：姓名、门牌住址等可定位到人的信息，跨角色流转前需打码
# 3 加密   ：证件号、联系方式、健康信息——《个人信息保护法》定义的敏感个人信息
LEVELS = {1: "明文", 2: "脱敏", 3: "加密"}

# 按字段名判定（键名出现即命中）
# 先判「公开键」：楼栋号属于设计文档里明确举的"明文"例子，不因值里带数字就被升格
PUBLIC_KEYS = ("楼栋", "楼号", "小区", "类型", "问题", "事项", "事由", "时间")

KEY_RULES = [
    (3, ("身份证", "证件", "社保号", "银行卡", "手机", "电话", "联系方式", "微信")),
    (3, ("健康", "病史", "病情", "血压", "血糖", "用药", "残疾", "精神", "孕")),
    (2, ("姓名", "户主", "联系人", "监护人", "住址", "地址", "门牌", "房号", "单元")),
]

# 按字段值判定（键名不明确时兜底）
VALUE_RULES = [
    (3, re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),          # 身份证
    (3, re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),           # 手机号
    (2, re.compile(r"\d+\s*单元|\d+\s*室|\d+\s*号楼")),      # 门牌（楼栋本身不算）
]


def classify_field(field: str, value) -> tuple[int, str]:
    """返回 (level, 判定依据)"""
    text = "" if value is None else str(value)
    for k in PUBLIC_KEYS:
        if k in (field or ""):
            return 1, f"字段名含「{k}」，属公开信息"
    for level, keys in KEY_RULES:
        for k in keys:
            if k in (field or ""):
                return level, f"字段名含「{k}」"
    for level, pat in VALUE_RULES:
        if text and pat.search(text):
            return level, "字段值命中敏感模式"
    return 1, ""


def classify_fields(fields: dict | None) -> list[dict]:
    """给一个业务项的所有结构化字段打密级，供 field_security 表落库"""
    out = []
    for k, v in (fields or {}).items():
        if isinstance(v, (dict, list)):        # 嵌套结构平铺成 a.b
            flat = {f"{k}.{i}": x for i, x in enumerate(v)} if isinstance(v, list) else v
            out.extend(classify_fields(flat))
            continue
        level, why = classify_field(k, v)
        out.append({"field": k, "level": level, "level_name": LEVELS[level], "reason": why})
    return out



class SecurityAgent(BaseAgent):
    name = "security"

    def run(self, payload: dict) -> dict:
        text = payload.get("raw_text", "")
        masked = text
        findings = []
        for fname, (pat, level, mask_fn) in PATTERNS.items():
            for m in pat.findall(masked):
                # type 为契约字段，field 保留兼容旧调用方
                findings.append({"type": fname, "field": fname,
                                 "value_masked": mask_fn(m), "level": level})
                masked = masked.replace(m, mask_fn(m))
        result = {"masked_text": masked, "findings": findings}
        self._audit("security_check", text, str(findings))
        return result

    def classify_fields(self, fields: dict | None) -> list[dict]:
        """字段三级管控：给业务项的结构化字段打密级（供 field_security 落库）"""
        return classify_fields(fields)
