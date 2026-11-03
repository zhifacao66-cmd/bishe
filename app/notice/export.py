"""公告文档生成：红头 docx / Markdown 归档件 / 纯文本

为什么自己拼 OOXML 而不调 Word
------------------------------
线上托管环境没有 Office、没有 LibreOffice，连字体都只有容器里那几个。
能用的只有纯 Python 的 python-docx（依赖 lxml，有 wheel 能装上），
所以「连红头带反线」这一套版式必须自己拼。

反过来，这条约束也决定了 **PDF 不在这里生成**：
容器里没有能稳定出 PDF 的库（reportlab/weasyprint 都是重依赖且有系统库要求），
硬上一旦装不上就是发布失败。公文场景里 PDF 本来就出自「打印 → 另存为 PDF」，
打印前必须人工确认版面，所以把 PDF 交给浏览器打印版是更贴近实际的做法。

版式依据 GB/T 9704-2012《党政机关公文格式》的常用简化约定：
    页边距  上 3.7cm / 下 3.5cm / 左 2.8cm / 右 2.6cm
    版头    发文机关标志，红色，小标宋，二号，居中
    文号    仿宋，三号，居中，其下加红色反线
    标题    小标宋，二号，居中
    正文    仿宋，三号，首行缩进 2 字符，行距固定值 28 磅
    落款    右对齐，右缩进 2 字符
    页码    页脚 "- 1 -"，四号

字体名用了「方正小标宋简体」「仿宋」。这两款在政务办公机上基本是标配；
如果本机没装，Word 会自动回退到宋体，**版式参数不受影响**——
字号、行距、缩进都是显式写死的，不依赖字体自身的度量。
"""
from __future__ import annotations

import io
import re

# ---- 字体与字号常量（改版式只需要动这里）
F_HEAD = "方正小标宋简体"     # 版头与标题
F_BODY = "仿宋"              # 正文、文号
F_NUM = "Times New Roman"    # 西文与数字
SZ_2 = 22                    # 二号
SZ_3 = 16                    # 三号
SZ_4 = 14                    # 四号
RED = "C00000"               # 公文红

LIST_UL = re.compile(r"^[-*·]\s+")
LIST_OL = re.compile(r"^\d+[.、)]\s*")
SEP_ROW = re.compile(r"^\|[\s:\-|]+\|$")
HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")
INLINE_NOISE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\([^)]*\)")
# 公文的层级标题：「一、」「（二）」。上级文件里这些行后面常常不空行，
# 正文紧跟下一行，按普通段落合并会粘成"一、排查范围辖区内所有……"，
# 层级感全丢。所以短且以序号开头的行单独成段。
SECTION_RE = re.compile(r"^[（(]?[一二三四五六七八九十]+[)）、.．]\s*")


class ExportUnavailable(Exception):
    """环境缺依赖或文档无法生成，由上层转成可读提示"""


# ---------------------------------------------------------------- Markdown 分块

def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _plain(text: str) -> str:
    """去掉行内标记，只留文字。公文正文不该带 ** 和反引号。"""
    def _sub(m):
        return next(g for g in m.groups() if g is not None)
    return INLINE_NOISE.sub(_sub, text or "")


def blocks(md: str) -> list[dict]:
    """把 Markdown 切成结构块，docx 与 txt 共用

    只处理公告正文会出现的六种结构：标题、段落、无序列表、有序列表、引用、表格。
    和 render.py 的 HTML 版保持同一套切分规则，避免「网页看到的样子」
    与「导出文件的样子」差太远。
    """
    lines = (md or "").replace("\r\n", "\n").split("\n")
    out: list[dict] = []
    i, n = 0, len(lines)

    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue

        if s.startswith("|") and i + 1 < n and SEP_ROW.match(lines[i + 1].strip()):
            head = _cells(s)
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(_cells(lines[i].strip()))
                i += 1
            out.append({"t": "table", "head": head, "rows": rows})
            continue

        m = HEAD_RE.match(s)
        if m:
            out.append({"t": "h", "lv": min(len(m.group(1)), 6),
                        "text": _plain(m.group(2).strip())})
            i += 1
            continue

        if s.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append({"t": "quote", "text": _plain(" ".join(buf))})
            continue

        if LIST_UL.match(s):
            items = []
            while i < n and LIST_UL.match(lines[i].strip()):
                items.append(_plain(LIST_UL.sub("", lines[i].strip())))
                i += 1
            out.append({"t": "ul", "items": items})
            continue

        if LIST_OL.match(s):
            items = []
            while i < n and LIST_OL.match(lines[i].strip()):
                items.append(_plain(LIST_OL.sub("", lines[i].strip())))
                i += 1
            out.append({"t": "ol", "items": items})
            continue

        buf = []
        while i < n and lines[i].strip() and not re.match(
                r"^(#{1,6}\s|>|\||[-*·]\s|\d+[.、)]\s*)", lines[i].strip()):
            # 「一、xxx」这类短层级标题必须自己占一段：
            # 已经攒了正文 → 断开；还没攒 → 只收这一行就走。
            cur = lines[i].strip()
            if SECTION_RE.match(cur) and len(cur) <= 28:
                if buf:
                    break
                buf.append(cur)
                i += 1
                break
            buf.append(cur)
            i += 1
        if buf:
            out.append({"t": "p", "text": _plain("".join(buf))})
        else:
            i += 1
    return out


def _cn_date(d) -> str:
    return f"{d.year}年{d.month}月{d.day}日" if d else ""


def _strip_tail(md: str, issuer: str, d) -> str:
    """去掉正文末尾的落款

    解析入库时 parse.py 已经剥过一次，这里再剥一次是因为**正文来源不止一条**：
    街道在线发文、社区手工改过的落地件，都可能把落款写进正文。
    不剥的话，导出的文件里会出现两个落款——正文一个、版式一个，一眼就是错的。
    """
    lines = (md or "").rstrip().split("\n")
    if not lines:
        return md or ""
    cn, iso = _cn_date(d), (d.isoformat() if d else "")
    tails = {s.replace(" ", "") for s in (issuer, cn, iso) if s}
    if not tails:
        return md
    removed = 0
    while lines and removed < 3:
        last = lines[-1].strip().replace(" ", "")
        if not last:
            lines.pop()
            continue
        hit = last in tails
        # 单位和日期挤在同一行的写法：「幸福里街道办事处2026年10月15日」
        if not hit and issuer and cn and issuer.replace(" ", "") in last and cn in last:
            hit = True
        if not hit:
            break
        lines.pop()
        removed += 1
    return "\n".join(lines).rstrip()


def _body(n) -> str:
    d = n.issued_at or (n.created_at.date() if n.created_at else None)
    return _strip_tail(n.body_md, n.issuer, d)


# ---------------------------------------------------------------- 导出：Markdown

def to_md(n) -> str:
    """归档件：带元信息头，可直接再丢回 data/notices/ 重新入库

    这不是顺手加的功能——它让「导出 → 归档 → 再投递」形成闭环：
    社区把落地件导成 md 存档，明年上级来查，这份文件还能被系统重新识别。
    """
    d = n.issued_at or (n.created_at.date() if n.created_at else None)
    head = [
        "---",
        f"title: {n.title}",
        f"issuer: {n.issuer or ''}",
        f"doc_no: {n.doc_no or ''}",
        f"issued_at: {d.isoformat() if d else ''}",
        f"category: {n.category or '通知公告'}",
        f"level: {n.level or '社区'}",
        f"scope: {n.scope or 'all'}",
        f"pinned: {'true' if n.pinned else 'false'}",
        "---",
        "",
    ]
    return "\n".join(head) + _body(n) + "\n"


# ---------------------------------------------------------------- 导出：纯文本

def to_txt(n) -> str:
    """纯文本件：给不装 Word 的终端看，也方便粘进微信群"""
    d = n.issued_at or (n.created_at.date() if n.created_at else None)
    out = [n.title or "", "=" * 40, ""]
    for k, v in (("发文单位", n.issuer), ("发文字号", n.doc_no),
                 ("成文日期", _cn_date(d)), ("类别", n.category)):
        if v:
            out.append(f"{k}：{v}")
    out.append("")
    for b in blocks(_body(n)):
        if b["t"] in ("p", "h", "quote"):
            out.append(b["text"])
            out.append("")
        elif b["t"] in ("ul", "ol"):
            for k, it in enumerate(b["items"], 1):
                out.append(("" if b["t"] == "ul" else f"{k}. ") + it)
            out.append("")
        elif b["t"] == "table":
            for row in [b["head"]] + b["rows"]:
                out.append(" | ".join(row))
            out.append("")
    out.append("-" * 40)
    out.append(f"（本件由社区智能体系统据《{n.source_file or '系统记录'}》生成）")
    return "\n".join(out)


# ---------------------------------------------------------------- docx 底层工具

def _set_font(run, ascii_font: str, ea_font: str, size_pt: float,
              bold: bool = False, color: str = "000000") -> None:
    """python-docx 只管西文字体，中文必须自己写 w:rFonts 的 eastAsia，
    否则 Word 里看到的永远是等线/宋体，字号缩进全对但字体不对。"""
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    f = run.font
    f.name = ascii_font
    f.size = Pt(size_pt)
    f.bold = bold
    f.color.rgb = RGBColor.from_string(color)
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    rfonts.set(qn("w:ascii"), ascii_font)
    rfonts.set(qn("w:hAnsi"), ascii_font)
    rfonts.set(qn("w:eastAsia"), ea_font)
    rfonts.set(qn("w:cs"), ascii_font)


def _bottom_border(par, color: str = RED, sz: int = 12) -> None:
    """给段落加下边框，用来画公文那条红色反线
    sz 单位是 1/8 磅，12 即 1.5 磅——国标要求反线约 2 磅，视觉上 12~16 都合适。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    ppr = par._p.get_or_add_pPr()
    pbdr = ppr.find(qn("w:pBdr"))
    if pbdr is None:
        pbdr = OxmlElement("w:pBdr")
        ppr.append(pbdr)
    bot = OxmlElement("w:bottom")
    bot.set(qn("w:val"), "single")
    bot.set(qn("w:sz"), str(sz))
    bot.set(qn("w:space"), "1")
    bot.set(qn("w:color"), color)
    pbdr.append(bot)


def _shade(cell, hex_fill: str = "F2F2F2") -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tcpr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    tcpr.append(shd)


def _page_number(section) -> None:
    """页脚居中 "- 1 -"，四号仿宋"""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    p = section.footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for txt in ("- ",):
        _set_font(p.add_run(txt), F_NUM, F_BODY, SZ_4)
    r = p.add_run()
    _set_font(r, F_NUM, F_BODY, SZ_4)
    r._r.append(OxmlElement("w:fldChar"))
    r._r[-1].set(qn("w:fldCharType"), "begin")
    r2 = p.add_run()
    _set_font(r2, F_NUM, F_BODY, SZ_4)
    it = OxmlElement("w:instrText")
    it.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    it.text = " PAGE "
    r2._r.append(it)
    r3 = p.add_run()
    _set_font(r3, F_NUM, F_BODY, SZ_4)
    r3._r.append(OxmlElement("w:fldChar"))
    r3._r[-1].set(qn("w:fldCharType"), "end")
    _set_font(p.add_run(" -"), F_NUM, F_BODY, SZ_4)


def _par(doc, text: str = "", *, size: float = SZ_3, font: str = F_BODY,
         align=None, indent_chars: float = 0.0, right_indent_chars: float = 0.0,
         space_before: float = 0, space_after: float = 0,
         line_pt: float = 28, color: str = "000000", bold: bool = False):
    """公文正文的标准段落：字号驱动的缩进 + 固定行距"""
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
    from docx.shared import Pt

    p = doc.add_paragraph()
    if align is not None:
        p.alignment = align
    pf = p.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    pf.line_spacing = Pt(line_pt)
    pf.space_before = Pt(space_before)
    pf.space_after = Pt(space_after)
    if indent_chars:
        pf.first_line_indent = Pt(size * indent_chars)
    if right_indent_chars:
        pf.right_indent = Pt(size * right_indent_chars)
    if text:
        _set_font(p.add_run(text), F_NUM, font, size, bold=bold, color=color)
    return p


# ---------------------------------------------------------------- 导出：docx

def to_docx(n, mark: str = "") -> bytes:
    """生成红头公文 docx，返回字节流（直接塞进 send_file）"""
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm, Pt
    except ImportError as e:                                   # 缺依赖要能说清楚
        raise ExportUnavailable("服务器未安装 python-docx，无法生成 Word 文件") from e

    doc = Document()

    # ---- 页面：国标页边距
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21), Cm(29.7)
    sec.top_margin, sec.bottom_margin = Cm(3.7), Cm(3.5)
    sec.left_margin, sec.right_margin = Cm(2.8), Cm(2.6)
    sec.header_distance, sec.footer_distance = Cm(1.5), Cm(2.8)
    _page_number(sec)

    # 默认样式也设一遍：不设的话表格里的字会退回等线，和正文明显不搭
    normal = doc.styles["Normal"]
    normal.font.name = F_NUM
    normal.font.size = Pt(SZ_3)

    # ---- 版头（红色发文机关标志）
    if n.issuer:
        _par(doc, n.issuer, size=SZ_2, font=F_HEAD, color=RED,
             align=WD_ALIGN_PARAGRAPH.CENTER, line_pt=34, space_after=10)

    # ---- 发文字号 + 红色反线
    if n.doc_no:
        _par(doc, n.doc_no, size=SZ_3, align=WD_ALIGN_PARAGRAPH.CENTER,
             line_pt=28, space_after=4)
    else:
        # 没有文号（社区落地件常见）也要有反线，否则版式塌成普通文档
        _par(doc, "", size=8, line_pt=8)
    rule = _par(doc, "", size=8, line_pt=8, space_after=16)
    _bottom_border(rule)

    # ---- 标题
    _par(doc, n.title or "通知", size=SZ_2, font=F_HEAD,
         align=WD_ALIGN_PARAGRAPH.CENTER, line_pt=34,
         space_before=10, space_after=18)

    # ---- 正文
    bs = blocks(_body(n))
    first_para = True
    for b in bs:
        if b["t"] == "p":
            text = b["text"]
            # 主送机关（"各社区、各有关单位："）按公文规矩顶格，不缩进
            is_addressee = bool(re.fullmatch(r"[^。！？]{1,40}[：:]", text.strip()))
            _par(doc, text,
                 indent_chars=0 if (is_addressee and first_para) else 2)
            first_para = False
        elif b["t"] == "h":
            # 正文里的一、二、三 本来就靠人工写，这里只处理 markdown 小标题
            _par(doc, b["text"], indent_chars=2, bold=True)
            first_para = False
        elif b["t"] == "quote":
            _par(doc, b["text"], indent_chars=2, size=SZ_3)
            first_para = False
        elif b["t"] == "ul":
            for it in b["items"]:
                _par(doc, "· " + it, indent_chars=2)
            first_para = False
        elif b["t"] == "ol":
            for k, it in enumerate(b["items"], 1):
                _par(doc, f"{k}. {it}", indent_chars=2)
            first_para = False
        elif b["t"] == "table":
            _table(doc, b["head"], b["rows"])
            first_para = False

    # ---- 落款：右对齐，右缩进 2 字符
    # 纸质公文在落款前留 2 行当印章区，但电子发文里那 2 行常常刚好把落款挤到
    # 第二页，出现"整页只有落款"的版式。改成段前 14 磅，一页能装下就装下。
    d = n.issued_at or (n.created_at.date() if n.created_at else None)
    if n.issuer:
        _par(doc, n.issuer, right_indent_chars=2, align=WD_ALIGN_PARAGRAPH.RIGHT,
             space_before=14)
    if d:
        _par(doc, _cn_date(d), right_indent_chars=2, align=WD_ALIGN_PARAGRAPH.RIGHT)

    # 水印放在最后：前面所有 _par 都可能动页眉页脚（比如 _page_number 写页脚），
    # 先打后写会被覆盖，而且这种覆盖在屏幕上完全看不出来。
    if mark:
        from app.watermark import apply_docx
        apply_docx(doc, mark)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _with_mark(text: str, mark: str) -> str:
    """纯文本 / Markdown 的水印。

    Markdown 走 **front matter 里加一个 `watermark:` 键**，而不是在正文顶上
    糊一行——md 的第一行必须是 `---`，否则所有按 front matter 解析的导入脚本
    （`app/notice/ingest.py` 自己就是）都会把这行当成正文标题，
    重新导入时文件名与标题全乱。这个断言在 `smoke_notice_flow` 里就有。

    说清楚边界：md 与 txt **没有页眉概念**，也没有任何防篡改能力，
    别人删掉这行就没了。所以对这两种格式，真正起作用的证据是
    `audit_log` 里那条导出记录，水印只是"看的人会注意到的提醒"。
    """
    if not mark:
        return text
    if text.startswith("---"):
        idx = text.find("\n---", 3)
        if idx != -1:
            return text[:idx + 1] + f"watermark: {mark}\n" + text[idx + 1:]
    bar = f"［水印］{mark}　本文件含内部信息，请勿外传"
    return f"{bar}\n\n{text}\n\n{bar}\n"


def _table(doc, head: list[str], rows: list[list[str]]) -> None:
    """公文里的附表：网格线 + 表头浅灰底，字号比正文小一号"""
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.shared import Pt

    cols = max([len(head)] + [len(r) for r in rows]) if (head or rows) else 1
    t = doc.add_table(rows=1, cols=cols)
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER

    for i in range(cols):
        c = t.rows[0].cells[i]
        c.text = ""
        p = c.paragraphs[0]
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after = Pt(2)
        _set_font(p.add_run(head[i] if i < len(head) else ""), F_NUM, F_BODY, SZ_4, bold=True)
        _shade(c)

    for row in rows:
        cells = t.add_row().cells
        for i in range(cols):
            p = cells[i].paragraphs[0]
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            _set_font(p.add_run(row[i] if i < len(row) else ""), F_NUM, F_BODY, SZ_4)


# ---------------------------------------------------------------- 统一入口

FORMATS = {
    "docx": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "md": ("md", "text/markdown; charset=utf-8"),
    "txt": ("txt", "text/plain; charset=utf-8"),
}


def build(n, fmt: str, mark: str = "") -> tuple[bytes, str, str]:
    """返回 (字节内容, 文件名, MIME)。文件名用「标题.扩展名」，浏览器下载即得可用名。

    `mark` 是导出水印（姓名·单位·时间·动作），由 `app/policy.py` 的导出决策
    要求生成。为空时行为与改造前完全一致。
    """
    fmt = (fmt or "docx").lower()
    if fmt not in FORMATS:
        raise ExportUnavailable(f"不支持导出格式：{fmt}")
    ext, mime = FORMATS[fmt]

    if fmt == "docx":
        data = to_docx(n, mark)
    elif fmt == "md":
        data = _with_mark(to_md(n), mark).encode("utf-8")
    else:
        data = _with_mark(to_txt(n), mark).encode("utf-8")

    safe = re.sub(r'[\\/:*?"<>|\r\n]', "_", (n.title or "公告")).strip()[:60] or "公告"
    if ext == "docx":
        # Word 打开时显示的名字，比 HTTP 头里的 filename 更可靠
        safe = re.sub(r"\s+", "", safe)
    return data, f"{safe}.{ext}", mime
