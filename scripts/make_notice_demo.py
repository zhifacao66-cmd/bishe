"""生成一份 .docx 版上级文件，用于演示「Word 文件丢进目录也能自动编排」

Word 里没有结构化元信息，解析器只能按公文排布猜：
首行标题 → 红头单位 → 文号 → 正文 → 落款 → 日期。
本脚本故意做成最普通的 Word 文档（不加样式、不写字号），
就是为了验证"格式不规范时能认出多少"。
"""
from pathlib import Path

from docx import Document

OUT = Path(__file__).resolve().parent.parent / "data" / "notices" / \
    "2026-10-12-关于开展独居老人入户走访的通知.docx"

TITLE = "关于开展独居老人入户走访的通知"
PARAS = [
    "各社区居委会、各网格员：",
    "为及时掌握辖区独居老人生活状况，防范季节性安全风险，决定在 10 月开展独居老人入户走访。现将有关事项通知如下。",
    "一、走访对象",
    "辖区范围内 60 周岁以上、单独居住或无子女日常照料的居民，以社区人口台账为准。",
    "二、走访内容",
    "1. 了解身体状况、用药与就医需求；",
    "2. 检查用电用气安全，重点看燃气软管、电热毯、取暖设备；",
    "3. 核对紧急联系人电话是否有效；",
    "4. 宣传高龄津贴、居家养老服务等政策。",
    "三、工作要求",
    "走访要见人见面，不得仅电话代替。发现重大风险的，当日报告社区并联系家属。",
    "请各社区于 10 月 30 日前完成走访，将走访记录录入台账。",
    "幸福里社区居民委员会",
    "2026年10月12日",
]


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_paragraph(TITLE)
    for p in PARAS:
        doc.add_paragraph(p)
    # 附带一张表格，验证 docx 表格能否转成发布版可见的表格
    table = doc.add_table(rows=3, cols=3)
    rows = [["社区", "走访户数", "完成时限"],
            ["幸福里社区", "32", "10月30日"],
            ["和平社区", "18", "10月30日"]]
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            table.cell(r, c).text = val
    doc.save(str(OUT))
    print("已生成", OUT)


if __name__ == "__main__":
    main()
