"""极简 Markdown → HTML（用于公告正文）

为什么不装 markdown 库：
1. 公告正文只用到标题、段落、列表、引用、表格五种结构，自己实现 60 行足够；
2. 正文来自**外部文件**，必须先 html.escape 再套标签。现成库的扩展与白名单
   策略还要额外配置，自己写反而更容易保证"任何一个 < 都进不来"。

安全约定：所有文本先转义，再在转义结果上插入受控标签；
因此文件中写 <script> 只会显示成字面量，不会执行。
"""
from __future__ import annotations

import html
import re

LIST_UL = re.compile(r"^[-*·]\s+")
LIST_OL = re.compile(r"^\d+[.、)]\s*")
SEP_ROW = re.compile(r"^\|[\s:\-|]+\|$")
# 公文的层级标题：「一、」「（二）」。上级文件里这些行后面常常不空行，
# 正文紧跟下一行，按普通段落合并会粘成"一、排查范围辖区内所有……"，
# 网页上看着就是标题和正文糊成一团。短且带序号的行单独成段。
SECTION_RE = re.compile(r"^[（(]?[一二三四五六七八九十]+[)）、.．]\s*")


def _inline(text: str) -> str:
    """行内样式。text 必须已经过 html.escape。"""
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # 链接只保留文字，避免居民端跳出到不可控站点
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return text


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def render(md: str) -> str:
    if not md:
        return ""
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)

    while i < n:
        raw = lines[i]
        s = raw.strip()

        if not s:
            i += 1
            continue

        # ---- 表格：| 表头 | ... 紧跟一行 |---|---|
        if s.startswith("|") and i + 1 < n and SEP_ROW.match(lines[i + 1].strip()):
            head = _cells(s)
            i += 2
            body = []
            while i < n and lines[i].strip().startswith("|"):
                body.append(_cells(lines[i].strip()))
                i += 1
            out.append('<table class="notice-table"><thead><tr>'
                       + "".join(f"<th>{_inline(html.escape(c))}</th>" for c in head)
                       + "</tr></thead><tbody>")
            for row in body:
                row = row + [""] * (len(head) - len(row))
                out.append("<tr>" + "".join(f"<td>{_inline(html.escape(c))}</td>"
                                            for c in row[:len(head)]) + "</tr>")
            out.append("</tbody></table>")
            continue

        # ---- 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            lvl = min(len(m.group(1)) + 1, 6)   # 公告标题已是 h1，正文从 h3 起
            out.append(f"<h{lvl}>{_inline(html.escape(m.group(2).strip()))}</h{lvl}>")
            i += 1
            continue

        # ---- 引用
        if s.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append("<blockquote>" + _inline(html.escape(" ".join(buf))) + "</blockquote>")
            continue

        # ---- 无序列表
        if LIST_UL.match(s):
            items = []
            while i < n and LIST_UL.match(lines[i].strip()):
                items.append(LIST_UL.sub("", lines[i].strip()))
                i += 1
                # 列表项换行续写（缩进两格以上）
                while i < n and lines[i].startswith("  ") and lines[i].strip():
                    items[-1] += " " + lines[i].strip()
                    i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(html.escape(t))}</li>" for t in items) + "</ul>")
            continue

        # ---- 有序列表
        if LIST_OL.match(s):
            items = []
            while i < n and LIST_OL.match(lines[i].strip()):
                items.append(LIST_OL.sub("", lines[i].strip()))
                i += 1
            out.append("<ol>" + "".join(f"<li>{_inline(html.escape(t))}</li>" for t in items) + "</ol>")
            continue

        # ---- 段落：连续非空行合成一段
        buf = []
        while i < n and lines[i].strip() and not re.match(r"^(#{1,6}\s|>|\||[-*·]\s|\d+[.、)]\s*)",
                                                          lines[i].strip()):
            cur = lines[i].strip()
            # 「一、xxx」这类短层级标题必须自己占一段：
            # 已经攒了正文 → 断开；还没攒 → 只收这一行就走。
            if SECTION_RE.match(cur) and len(cur) <= 28:
                if buf:
                    break
                buf.append(cur)
                i += 1
                break
            buf.append(cur)
            i += 1
        if buf:
            out.append("<p>" + _inline(html.escape("".join(buf))) + "</p>")
        else:
            i += 1

    return "\n".join(out)
