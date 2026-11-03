"""办文：把上级来文落成本社区的执行通知（落地件）

为什么要有这一步
----------------
上级文件写的是"各社区要做消防排查"，居民看了不知道自己该干什么。
社区真正要发出去的是"我们小区哪天查、查什么、找谁"——这就是落地件。
它和上级原文是**两份文档、两条记录**，用 parent_id 串起来，谁依据谁一查就到。

生成策略：模型优先，规则兜底
--------------------------
模型能写出通顺的社区口吻，但线上环境没有 Ollama，所以必须有一条不依赖模型的路径。
兜底模板不是"随便凑一段"，它按公文的转发件写法组织：
    依据（谁的文、什么号） → 本社区落实事项 → 时间安排 → 联系方式
即使模型不可用，社区拿到的也是一份能直接改的草稿，而不是空白页。

一条硬约束：**不新增上级没提的要求**。
模型改写时很容易"顺手加一句"，政务文件里这等于擅自加码，
所以提示词里明确禁止，兜底模板更是只做搬运不做发挥。
"""
from __future__ import annotations

import re
from datetime import date

from app.notice.export import SECTION_RE

# 上级正文里的列表符号，提取要点时要剥掉，否则会出现「1. - 消防通道……」
LIST_UL = re.compile(r"^[-*·]\s+")
LIST_OL = re.compile(r"^\d+[.、)]\s*")

# 只约束模型输出这两个字段，不让它自由发挥结构
SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "body_md": {"type": "string"},
    },
    "required": ["title", "body_md"],
}

SYSTEM = (
    "你是社区居委会的工作人员，负责把上级下发的文件转成本社区的执行通知。"
    "写作要求：公文体、简洁、不用口语；不新增上级文件里没有的要求和数字；"
    "不写落款和日期（系统会自动补）。"
)


def _points(md: str, limit: int = 4) -> list[str]:
    """从上级正文里提取要点，形如「排查范围：辖区内所有高层住宅小区」

    只取「一、二、三」的标题是不够的——"排查内容"四个字本身不是要求，
    社区看了还得翻原文。所以标题后面挂上它下面第一句，一句话把这件事说清。

    没有层级标题时退而取前几段。上级文件格式五花八门，
    兜底路径不追求好看，只追求社区不用对着空白框发呆。
    """
    lines = [l.strip() for l in (md or "").replace("\r\n", "\n").split("\n") if l.strip()]
    out: list[str] = []
    for idx, l in enumerate(lines):
        if not (SECTION_RE.match(l) and len(l) <= 28):
            continue
        name = SECTION_RE.sub("", l).rstrip("：:。. ")
        detail = ""
        if idx + 1 < len(lines):
            nxt = lines[idx + 1]
            if not SECTION_RE.match(nxt):
                nxt = LIST_UL.sub("", LIST_OL.sub("", nxt))
                detail = nxt.rstrip("。. ")[:52]
        out.append(f"{name}：{detail}" if detail else name)
        if len(out) >= limit:
            break
    if out:
        return out

    for l in lines:
        if l.endswith(("：", ":")) and len(l) < 40:      # 主送机关，不是要点
            continue
        out.append(l[:60])
        if len(out) >= limit:
            break
    return out


def _doc_ref(parent) -> str:
    """依据部分：《文件名》（文号）"""
    ref = f"《{parent.title}》"
    if parent.doc_no:
        ref += f"（{parent.doc_no}）"
    return ref


def _fallback(community: str, phone: str, parent, deadline) -> dict:
    pts = _points(parent.body_md)
    title = f"关于落实《{parent.title}》的通知"

    lines = [
        "各小区、各位居民：",
        "",
        f"根据{parent.issuer or '上级单位'}{_doc_ref(parent)}要求，结合我社区实际，"
        "现将有关事项通知如下：",
        "",
        "一、本社区落实事项",
    ]
    if pts:
        for k, p in enumerate(pts, 1):
            lines.append(f"{k}. {p}")
    else:
        lines.append("1. 按上级文件要求，逐项落实。")
    lines += [
        "",
        "二、时间安排",
        (f"请于{deadline.strftime('%Y年%m月%d日')}前完成。"
         if deadline else "具体时间按上级文件要求执行，我社区将另行通知。"),
        "",
        "三、咨询方式",
        f"如有疑问，请联系{community}" + (f"，电话 {phone}" if phone else "") + "。",
        "",
        "请广大居民理解配合，共同维护社区安全与秩序。",
    ]
    return {"title": title, "body_md": "\n".join(lines), "source": "rule"}


def draft(parent, community: str, phone: str = "", deadline=None) -> dict:
    """生成落地件草稿。返回 {title, body_md, source}

    source 记录草稿是模型写的还是规则拼的，社区看到后心里有数：
    规则拼的草稿结构对但措辞硬，值得自己再顺一遍。
    """
    base = _fallback(community, phone, parent, deadline or parent.deadline)

    prompt = f"""上级文件信息：
发文单位：{parent.issuer or '（未标注）'}
标题：{parent.title}
文号：{parent.doc_no or '（无）'}
成文日期：{parent.issued_at or '（未标注）'}
正文：
{(parent.body_md or '')[:2000]}

本社区名称：{community}
对外联系电话：{phone or '（未提供）'}
要求完成时限：{(deadline or parent.deadline) or '（上级未明确）'}

请据此写一份本社区的执行通知正文。要求：
1. 开头写明依据（上级单位 + 文件名 + 文号）；
2. 分 2 到 3 个部分，说清本社区要做什么、什么时间完成、有疑问找谁；
3. 只能使用上级文件已有的要求，不得自行增加新的义务或数据；
4. 正文用 Markdown，段落之间空一行，层级用「一、」「二、」；
5. 不要写标题、不要写落款和日期。

同时给出一个适合本社区发布的标题（不要照抄上级原标题）。"""

    try:
        from app.llm.client import LLMUnavailable, chat
        raw = chat(prompt, system=SYSTEM, schema=SCHEMA)
        import json
        d = json.loads(raw)
        body = (d.get("body_md") or "").strip()
        title = (d.get("title") or "").strip()
        if not body:
            return base
        # 模型偶尔会把落款写进正文，统一切掉，落款由版式补
        body = _drop_signature(body, community)
        return {"title": title or base["title"], "body_md": body, "source": "llm"}
    except LLMUnavailable:
        return base
    except Exception:
        # 模型返回的不是合法 JSON 等情况：宁可给模板，也不给半截内容
        return base


def _drop_signature(body: str, community: str) -> str:
    """模型有时会自作主张加落款，删掉最后两行里像落款的部分"""
    lines = body.rstrip().split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    removed = 0
    while lines and removed < 2:
        last = lines[-1].strip().replace(" ", "")
        if last == community.replace(" ", "") or \
                (len(last) <= 16 and last.endswith(("日", "号")) and "年" in last):
            lines.pop()
            removed += 1
            while lines and not lines[-1].strip():
                lines.pop()
            continue
        break
    return "\n".join(lines).strip()


__all__ = ["draft", "SCHEMA", "date"]
