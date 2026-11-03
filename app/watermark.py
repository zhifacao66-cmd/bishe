"""导出水印：让"这份文件是谁拿的"跟着文件走

## 能做什么、不能做什么（这一节比代码重要）

| 载体 | 做法 | 强度 |
|---|---|---|
| 内部站页面 | CSS 平铺斜向水印（`overlay_css`） | 真水印，截屏带痕 |
| xlsx | 打印页眉/页脚 + 末行署名 | 屏幕上看不见，打印/PDF 可见 |
| docx | 页眉页脚段落 | Word 里可见，被摘成纯文本会丢 |
| 图片附件 | PIL 角标 | 本轮未做（改原图不安全，要出副本） |

**必须诚实的三点：**

1. Excel 的"真水印"是在工作表里插艺术字或图片背景，openpyxl 没有这个能力。
   硬造一个背景图会让文件在 WPS/Excel 里报"内容有问题"。所以 xlsx 只做
   页眉页脚——它**在屏幕上完全看不见**，只在你打印或转 PDF 时出现。
   当众说"Excel 导出带水印"而对方打开屏幕截图看不到，比不做水印更伤人。

2. 水印只能**事后追溯**，不能防止传播。它唯一的作用是：截图流出后，
   能看到"李四·幸福社区·10-26 14:32"这一行字，从而定位到人。
   所以水印必须和审计日志成对出现——只看水印、查不到那条审计，
   依然追不到人（导出记录里有同样的姓名与时间戳）。

3. 水印**可以被裁剪、被 P 掉**。它是"提高追溯概率"，不是"阻止泄露"。
   论文里写"水印防止了数据泄露"是外行话。
"""
from datetime import datetime


def text(user, purpose="导出", when=None):
    """水印文案：姓名 · 单位 · 时间 · 动作

    四个要素缺一不可：
      姓名 → 追到人（只写单位等于只知道"社区导的"）
      单位 → 归属（同名的人不罕见）
      时间 → 对上审计日志那条记录的唯一线索
      动作 → 区分"导出"与"打印"，两种行为的责任不同
    """
    name = getattr(user, "name", "") or getattr(user, "username", "") or "匿名"
    org = getattr(user, "org", "") or ""
    t = when or datetime.now()
    parts = [name]
    if org:
        parts.append(org)
    parts.append(t.strftime("%Y-%m-%d %H:%M"))
    parts.append(purpose)
    return " · ".join(parts)


def apply_xlsx(sh, mark):
    """给工作表打水印（对象是 `app/xlsx.py` 的自研写入器，不是 openpyxl）

    两道一起上，因为它们的可见范围不同：
      ① 打印页眉 —— 打印 / 转 PDF / 打印预览时显示在页面顶部，报送件看得到
      ② 数据区最后一行 —— 屏幕上也看得见，截图传出去会带上

    只上 ① 的问题：对方打开 xlsx 截个图发群里，那一行水印根本不在画面里
    （页眉只在打印视图渲染），等于没做。
    只上 ② 的问题：一打印就混进正文数据，排序时会被当成一条记录，
    复制区域做汇总时还会被算进去。所以两道都要。

    写入失败绝不阻断导出：拿不到文件比没有水印更糟。
    """
    try:
        sh.header_text = mark
        sh.footer_text = "本表含个人信息，请勿外传"
        last = max([k[0] for k in sh.cells] or [1])
        # 空一行再写：贴着最后一行会被当成数据，也容易被拖选区域一起删掉
        sh.cell(last + 2, 1, f"［水印］{mark}")
        sh.height(last + 2, 14)
    except Exception:
        pass


def apply_docx(doc, mark):
    """给 Word 打页眉水印。

    **只动页眉，不碰页脚**：页脚已经被 `_page_number()` 占用来放 "- 1 -"，
    往第一个页脚段落里塞水印会把页码覆盖掉——公文没有页码是硬伤
    （GB/T 9704 要求页码），而且这个错在屏幕上不明显，打印出来才发现。

    为什么不做"斜向大字铺满整页"的那种水印：
    那需要在页眉里插 VML `v:shape` + `textpath`，手写 XML 一旦不合规，
    Word 会直接报"文件已损坏、无法打开"——而导出件是给街道报送用的，
    打不开等于交付失败。页眉段落虽然朴素，但百分百能打开。
    """
    try:
        for section in doc.sections:
            hdr = section.header
            p = hdr.paragraphs[0] if hdr.paragraphs else hdr.add_paragraph()
            p.text = mark
            p.alignment = 2                     # 右对齐，不挡正文
            for run in p.runs:
                run.font.size = _pt(8)
                run.font.color.rgb = _rgb("808080")
    except Exception:
        pass


def _pt(n):
    from docx.shared import Pt
    return Pt(n)


def _rgb(hexstr):
    from docx.shared import RGBColor
    return RGBColor.from_string(hexstr)


def overlay_css(mark):
    """内部站页面的**平铺**水印 CSS。

    做法是把一段 SVG 当背景图重复平铺，而不是往页面上放一行居中的大字：
      · 单行居中只在屏幕正中有一句，截图裁一角就没了
      · 平铺之后无论截哪一块都带水印，"提高追溯概率"才落到实处

    三个必须的细节：
      pointer-events:none → 不挡点击。挡了就是"页面坏了"，用户只会报障
      user-select:none    → 别让人顺手选中复制走这句水印
      z-index 在弹窗之下 → 压在内容上但不压弹层，否则弹窗文字变灰

    SVG 里的文字必须走 XML 转义再 URL 编码，否则姓名里出现 `&`、`<`，
    整条 CSS 会被浏览器判为非法而**静默丢弃**——水印不显示，而且不报错。
    """
    from urllib.parse import quote

    raw = (mark or "").replace("\n", " ").replace("\r", " ")
    safe = (raw.replace("&", "&amp;").replace("<", "&lt;")
               .replace(">", "&gt;").replace('"', "&quot;"))
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='320' height='190'>"
        "<text x='160' y='95' font-size='15' fill='rgba(110,118,130,0.13)'"
        " font-family='system-ui,sans-serif' text-anchor='middle'"
        " transform='rotate(-22 160 95)'>" + safe + "</text></svg>")
    url = "data:image/svg+xml;charset=utf-8," + quote(svg, safe="")
    return ("body::after{content:'';position:fixed;inset:0;pointer-events:none;"
            "user-select:none;z-index:900;background-image:url(\"" + url + "\");"
            "background-repeat:repeat;background-position:0 0}")
