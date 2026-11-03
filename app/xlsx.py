"""最小 xlsx 写入器：一个 zip + 几段 XML，不依赖 openpyxl

为什么不用 openpyxl
-------------------
和 notice/export.py 手拼 docx 是同一个理由：线上托管环境装什么不确定，
requirements.txt 里多写一个包，就等于把「能不能发布」押在这个包能不能装上。
openpyxl 体量大（依赖 et_xmlfile），而报表要用的东西其实很少——
单元格、合并、列宽、行高、冻结、筛选、几种样式。

xlsx 本质上就是 zip 里塞几份 XML，写出来大约 300 行，可控、可测、零依赖，
代价只是不能用公式和图表（这两样本系统也确实不需要：报表数字全部由服务端算好，
图表在网页上用 ECharts 看，导出件要的是"打开就能交"而不是"打开还能算"）。

几个容易踩的格式坑（都已在实现里处理）
--------------------------------------
1. `[Content_Types].xml` 必须覆盖每一个 part，漏一个 Excel 直接报"文件损坏"。
2. `styles.xml` 的 fills 前两项**必须**是 none 和 gray125，位置不能换。
3. worksheet 子元素的顺序受 schema 约束：autoFilter 在 mergeCells 之前，
   printOptions/pageMargins/pageSetup 在最后。顺序错了 Excel 会静默丢格式。
4. 文本要用 inlineStr（省掉 sharedStrings 的引用记数），首尾空格必须
   `xml:space="preserve"`，否则" 附注"会被吃掉一个空格。
5. 日期要转成 1900 日历的序列号（以 1899-12-30 为原点），不能直接写字符串，
   否则 Excel 里是文本，没法排序和做图。
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date, datetime
from xml.sax.saxutils import escape

# XML 1.0 不允许的控制字符（制表/换行/回车之外）会让 Excel 判文件损坏
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_ILLEGAL_SHEET = re.compile(r"[\[\]:*?/\\]")

# ---------------------------------------------------------------- 样式定义
# 一个单元格的格式就五件事：字体 / 填充 / 边框 / 对齐 / 数字格式。
# 用不可变元组当 key，写入时按 key 去重编号，最终只生成真正用到的组合。

FONTS = {
    # alias: (字体名, 字号, 加粗, 颜色)
    "body":  ("宋体", 10.5, False, "1F2937"),
    "bold":  ("宋体", 10.5, True, "1F2937"),
    "title": ("黑体", 16, True, "15243B"),
    "sub":   ("宋体", 10, False, "6B7280"),
    "head":  ("黑体", 10.5, True, "FFFFFF"),
    "note":  ("宋体", 9.5, False, "6B7280"),
}

FILLS = {
    None: None,
    "head": "206BC4",     # 表头蓝（和网页主色一致）
    "label": "F5F8FD",    # 指标名列浅蓝
    "total": "EEF3FB",    # 合计行
    "soft": "FBFCFE",
}


class Style:
    """样式描述。用元组存，方便直接当 dict key 去重。"""

    __slots__ = ("font", "fill", "border", "h", "v", "wrap", "fmt")

    def __init__(self, font="body", fill=None, border="thin", h="left",
                 v="center", wrap=False, fmt=None):
        self.font, self.fill, self.border = font, fill, border
        self.h, self.v, self.wrap, self.fmt = h, v, wrap, fmt

    def key(self):
        return (self.font, self.fill, self.border, self.h, self.v, self.wrap, self.fmt)


class styles:
    """报表用得到的样式预设。

    命名按用途而不是按外观：将来把表头从蓝改成灰，只需要动这里一处，
    不用去每个 sheet 里搜"那个蓝色的样式叫什么"。
    """
    title = Style(font="title", border=None, h="left")
    sub = Style(font="sub", border=None, h="left")
    head = Style(font="head", fill="head", h="center", wrap=True)
    head_l = Style(font="head", fill="head", h="left", wrap=True)
    text = Style(h="left", wrap=True)
    text_c = Style(h="center")
    num = Style(h="right", fmt="#,##0")
    pct = Style(h="right", fmt="0.0%")
    delta = Style(h="right", fmt="+0.0%;-0.0%;0.0%")
    money = Style(h="right", fmt="#,##0.00")
    label = Style(font="bold", fill="label", h="left", wrap=True)
    total = Style(font="bold", fill="total", h="center")
    total_num = Style(font="bold", fill="total", h="right", fmt="#,##0")
    total_pct = Style(font="bold", fill="total", h="right", fmt="0.0%")
    note = Style(font="note", border=None, h="left", wrap=True)
    note_c = Style(font="note", border=None, h="center")
    stamp = Style(h="center", fmt="yyyy-mm-dd hh:mm")


# 内置数字格式（id 固定，不要自造）：0 常规 / 1 整数 / 3 千分位 / 10 百分比两位
BUILTIN_FMT = {"general": 0, "0": 1, "#,##0": 3, "0.00": 2, "0%": 9, "0.00%": 10}
CUSTOM_FMT_START = 164          # 164 以下被内置格式占用，自定义必须从这里起


def _col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _ref(r: int, c: int) -> str:
    return f"{_col(c)}{r}"


def _argb(hex6: str) -> str:
    return "FF" + hex6.upper()


def _serial(d) -> float:
    """日期 → Excel 序列号。原点取 1899-12-30，自动吃掉 1900 闰年那个 bug。"""
    if isinstance(d, datetime):
        base = datetime(1899, 12, 30)
        delta = d - base
        return delta.days + delta.seconds / 86400
    if isinstance(d, date):
        return (d - date(1899, 12, 30)).days
    return float(d)


def _num(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    return repr(float(v))


def _text(v: str) -> str:
    return escape(_CTRL.sub("", v))


def _esc_hf(v: str) -> str:
    """页眉页脚文本转义。

    `escape()` 会把 `&` 转成 `&amp;`，这正是我们要的：
    Excel 在读取后会把 `&amp;` 还原成 `&` 字面量，而不会把它当格式引导符。
    但不能在这之后再去拼接 `&amp;C` 这类格式码——所以格式码写在
    调用处（`to_xml` 里），只有**用户内容**过这个函数。
    """
    return escape(_CTRL.sub("", (v or "")).replace("\r", " ").replace("\n", " "))


class Sheet:
    def __init__(self, name, widths=None, freeze=None, autofilter=None,
                 tab_color=None, landscape=False):
        self.name = _ILLEGAL_SHEET.sub("_", (name or "Sheet"))[:31]
        self.widths = list(widths or [])
        self.freeze = freeze            # (rows, cols) 冻结行列数
        self.autofilter = autofilter    # "A4:F30"
        self.tab_color = tab_color
        self.landscape = landscape
        self.cells: dict[tuple[int, int], tuple] = {}
        self.merges: list[str] = []
        self.heights: dict[int, float] = {}
        # 打印页眉/页脚（导出水印用）。只在打印与打印预览里可见，
        # 屏幕上看不见——这是 Excel 的机制，不是我们没做。
        # 要"屏幕上也能看见"的水印，得同时往数据区写一行（见 app/watermark.py）。
        self.header_text = ""
        self.footer_text = ""

    # ---- 写
    def cell(self, r, c, value=None, style=None):
        if value is None or value == "":
            if style is not None:                     # 空值但要有底色/边框
                self.cells[(r, c)] = (None, style)
            return self
        self.cells[(r, c)] = (value, style or styles.text)
        return self

    def row(self, r, values, style=None, styles_=None):
        for i, v in enumerate(values, 1):
            st = styles_[i - 1] if styles_ else style
            self.cell(r, i, v, st)
        return self

    def merge(self, ref: str):
        self.merges.append(ref)
        return self

    def height(self, r, h):
        self.heights[r] = h
        return self

    def width(self, c, w):
        while len(self.widths) < c:
            self.widths.append(0)
        self.widths[c - 1] = w
        return self

    # ---- 输出
    def _dimension(self):
        if not self.cells:
            return "A1"
        rs = [k[0] for k in self.cells]
        cs = [k[1] for k in self.cells]
        return f"A1:{_ref(max(rs), max(cs))}" if (max(rs), max(cs)) != (1, 1) else "A1"

    def to_xml(self, book: "_StyleBook") -> str:
        out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
               '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
               ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">']

        pr = '<pageSetUpPr fitToPage="1"/>'
        tab = f'<tabColor rgb="{_argb(self.tab_color)}"/>' if self.tab_color else ""
        if pr or tab:
            out.append(f"<sheetPr>{tab}{pr}</sheetPr>")

        out.append(f'<dimension ref="{self._dimension()}"/>')

        pane = ""
        if self.freeze:
            fr, fc = self.freeze
            top = _ref(fr + 1, fc + 1)
            pane = (f'<pane xSplit="{fc}" ySplit="{fr}" topLeftCell="{top}"'
                    f' activePane="bottomRight" state="frozen"/>')
        out.append('<sheetViews><sheetView tabSelected="1" workbookViewId="0">'
                   f'{pane}</sheetView></sheetViews>')
        out.append('<sheetFormatPr defaultRowHeight="18"/>')

        if self.widths:
            cols = []
            for i, w in enumerate(self.widths, 1):
                if w:
                    cols.append(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>')
            if cols:
                out.append("<cols>" + "".join(cols) + "</cols>")

        out.append("<sheetData>")
        for r in sorted({k[0] for k in self.cells}):
            h = self.heights.get(r)
            attr = f' ht="{h}" customHeight="1"' if h else ""
            out.append(f'<row r="{r}"{attr}>')
            for c in sorted(k[1] for k in self.cells if k[0] == r):
                v, st = self.cells[(r, c)]
                sid = book.xf(st)
                ref = _ref(r, c)
                if v is None:
                    out.append(f'<c r="{ref}" s="{sid}"/>')
                elif isinstance(v, bool):
                    out.append(f'<c r="{ref}" s="{sid}" t="b"><v>{_num(v)}</v></c>')
                elif isinstance(v, (int, float)):
                    out.append(f'<c r="{ref}" s="{sid}"><v>{_num(v)}</v></c>')
                elif isinstance(v, (datetime, date)):
                    out.append(f'<c r="{ref}" s="{sid}"><v>{_serial(v)}</v></c>')
                else:
                    out.append(f'<c r="{ref}" s="{sid}" t="inlineStr"><is>'
                               f'<t xml:space="preserve">{_text(str(v))}</t></is></c>')
            out.append("</row>")
        out.append("</sheetData>")

        if self.autofilter:
            out.append(f'<autoFilter ref="{self.autofilter}"/>')
        if self.merges:
            out.append(f'<mergeCells count="{len(self.merges)}">'
                       + "".join(f'<mergeCell ref="{m}"/>' for m in self.merges)
                       + "</mergeCells>")

        # 打印：横向/缩放铺满一页宽，省得街道打印出来断成两页
        out.append('<printOptions horizontalCentered="1"/>')
        out.append('<pageMargins left="0.51" right="0.51" top="0.75" bottom="0.75"'
                   ' header="0.3" footer="0.3"/>')
        out.append(f'<pageSetup paperSize="9" scale="100"'
                   f' orientation="{"landscape" if self.landscape else "portrait"}"'
                   ' fitToWidth="1" fitToHeight="0"/>')

        # 页眉/页脚必须在 pageSetup 之后（CT_Worksheet 的元素顺序是硬约束，
        # 顺序错了 Excel 直接报"内容有问题、是否恢复"）。
        # 内容里的 `&` 是 Excel 的格式引导符（&C 居中、&8 字号），
        # 所以文本里的 & 要写成 &amp;，否则水印里的"·"没事、但一旦带上"&"就坏。
        if self.header_text or self.footer_text:
            parts = []
            if self.header_text:
                parts.append(f'<oddHeader>&amp;C&amp;8{_esc_hf(self.header_text)}</oddHeader>')
            if self.footer_text:
                parts.append(f'<oddFooter>&amp;R&amp;8{_esc_hf(self.footer_text)}</oddFooter>')
            out.append("<headerFooter>" + "".join(parts) + "</headerFooter>")

        out.append("</worksheet>")
        return "".join(out)


class _StyleBook:
    """把用到的 Style 编号成 styles.xml 里的下标（去重、顺序稳定）"""

    def __init__(self):
        self.fonts = [("宋体", 10.5, False, "1F2937")]
        self.fills = [None, "gray125"]      # 0/1 是格式规定的占位
        self.borders = [None]
        self.numfmts: list[str] = []
        self.xfs: list[tuple] = []
        self._xf_idx: dict[tuple, int] = {}

    def _font(self, alias):
        spec = FONTS[alias]
        if spec not in self.fonts:
            self.fonts.append(spec)
        return self.fonts.index(spec)

    def _fill(self, alias):
        rgb = FILLS.get(alias)
        if rgb is None:
            return 0
        if rgb not in self.fills:
            self.fills.append(rgb)
        return self.fills.index(rgb)

    def _border(self, kind):
        if not kind:
            return 0
        if kind not in self.borders:
            self.borders.append(kind)
        return self.borders.index(kind)

    def _fmt(self, code):
        if not code:
            return 0
        if code in BUILTIN_FMT:
            return BUILTIN_FMT[code]
        if code not in self.numfmts:
            self.numfmts.append(code)
        return CUSTOM_FMT_START + self.numfmts.index(code)

    def xf(self, st: Style) -> int:
        k = st.key()
        if k in self._xf_idx:
            return self._xf_idx[k]
        idx = len(self.xfs)
        self.xfs.append((self._font(st.font), self._fill(st.fill),
                         self._border(st.border), self._fmt(st.fmt),
                         st.h, st.v, st.wrap))
        self._xf_idx[k] = idx
        return idx

    # ---- 序列化
    def to_xml(self) -> str:
        o = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']

        if self.numfmts:
            o.append(f'<numFmts count="{len(self.numfmts)}">')
            for i, code in enumerate(self.numfmts):
                o.append(f'<numFmt numFmtId="{CUSTOM_FMT_START + i}"'
                         f' formatCode="{escape(code, {chr(34): "&quot;"})}"/>')
            o.append("</numFmts>")

        o.append(f'<fonts count="{len(self.fonts)}">')
        for name, size, bold, color in self.fonts:
            b = "<b/>" if bold else ""
            o.append(f'<font>{b}<sz val="{size}"/><color rgb="{_argb(color)}"/>'
                     f'<name val="{escape(name)}"/><charset val="134"/></font>')
        o.append("</fonts>")

        o.append(f'<fills count="{len(self.fills)}">')
        for rgb in self.fills:
            if rgb is None:
                o.append('<fill><patternFill patternType="none"/></fill>')
            elif rgb == "gray125":
                o.append('<fill><patternFill patternType="gray125"/></fill>')
            else:
                o.append('<fill><patternFill patternType="solid">'
                         f'<fgColor rgb="{_argb(rgb)}"/><bgColor indexed="64"/>'
                         '</patternFill></fill>')
        o.append("</fills>")

        o.append(f'<borders count="{len(self.borders)}">')
        for kind in self.borders:
            if not kind:
                o.append('<border><left/><right/><top/><bottom/><diagonal/></border>')
            else:
                sides = "".join(
                    f'<{t} style="{kind}"><color rgb="FFD9E1EC"/></{t}>'
                    for t in ("left", "right", "top", "bottom"))
                o.append(f'<border>{sides}<diagonal/></border>')
        o.append("</borders>")

        o.append('<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0"'
                 ' borderId="0"/></cellStyleXfs>')

        o.append(f'<cellXfs count="{len(self.xfs)}">')
        for fid, flid, bid, nid, h, v, wrap in self.xfs:
            attrs = [f'numFmtId="{nid}"', f'fontId="{fid}"', f'fillId="{flid}"',
                     f'borderId="{bid}"']
            if fid:
                attrs.append('applyFont="1"')
            if flid:
                attrs.append('applyFill="1"')
            if bid:
                attrs.append('applyBorder="1"')
            if nid:
                attrs.append('applyNumberFormat="1"')
            al = f'<alignment horizontal="{h}" vertical="{v}"'
            if wrap:
                al += ' wrapText="1"'
            al += "/>"
            attrs.append('applyAlignment="1"')
            o.append(f'<xf {" ".join(attrs)}>{al}</xf>')
        o.append("</cellXfs>")

        o.append('<cellStyles count="1"><cellStyle name="常规" xfId="0"'
                 ' builtinId="0"/></cellStyles>')
        o.append("</styleSheet>")
        return "".join(o)


class Workbook:
    def __init__(self, creator="社区减负智能体"):
        self.sheets: list[Sheet] = []
        self.creator = creator

    def add(self, name, **kw) -> Sheet:
        sh = Sheet(name, **kw)
        self.sheets.append(sh)
        return sh

    def to_bytes(self) -> bytes:
        if not self.sheets:
            raise ValueError("工作簿至少要有一个工作表")
        book = _StyleBook()
        # 先让样式表收齐所有 xf，再序列化（顺序在这里固定下来）
        sheets_xml = [sh.to_xml(book) for sh in self.sheets]

        ov = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
            '<Default Extension="xml" ContentType="application/xml"/>',
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        ]
        for i in range(1, len(self.sheets) + 1):
            ov.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml"'
                      ' ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
        ov.append('<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>')
        ov.append('<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>')
        ov.append('<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>')
        ov.append("</Types>")

        root_rels = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
            '</Relationships>')

        wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
              '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
              ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
              '<bookViews><workbookView activeTab="0"/></bookViews><sheets>']
        for i, sh in enumerate(self.sheets, 1):
            wb.append(f'<sheet name="{escape(sh.name)}" sheetId="{i}" r:id="rId{i}"/>')
        wb.append('</sheets><calcPr calcId="0"/></workbook>')

        wb_rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
        for i in range(1, len(self.sheets) + 1):
            wb_rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
        wb_rels.append(f'<Relationship Id="rId{len(self.sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>')
        wb_rels.append("</Relationships>")

        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
                ' xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/"'
                ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                f'<dc:creator>{escape(self.creator)}</dc:creator>'
                f'<cp:lastModifiedBy>{escape(self.creator)}</cp:lastModifiedBy>'
                f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
                f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
                '</cp:coreProperties>')

        app = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"'
               ' xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
               f'<Application>{escape(self.creator)}</Application></Properties>')

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", "".join(ov))
            z.writestr("_rels/.rels", root_rels)
            z.writestr("docProps/core.xml", core)
            z.writestr("docProps/app.xml", app)
            z.writestr("xl/workbook.xml", "".join(wb))
            z.writestr("xl/_rels/workbook.xml.rels", "".join(wb_rels))
            z.writestr("xl/styles.xml", book.to_xml())
            for i, xml in enumerate(sheets_xml, 1):
                z.writestr(f"xl/worksheets/sheet{i}.xml", xml)
        return buf.getvalue()
