"""上级文件解析：把 data/notices/ 下的 .md / .docx / .txt 统一成一条公告

设计原则
--------
1. **能确定的不问人，不确定的不猜**。字段解析有把握就填，没把握就留空并把
   原因写进 note，最终 status=pending 等人补。宁可让社区多点一下，也不能把
   "关于XXX的通知"错认成发文单位发出去。
2. **正文只做格式归一，不做改写**。政策的措辞不能由程序润色。
3. 解析结果一律是纯数据（dict），入库与页面都不直接依赖原始文件。
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from pathlib import Path

# 文号：幸街发〔2026〕18号 / [2026]18号 / （2026）18号
DOC_NO_RE = re.compile(
    r"([\u4e00-\u9fa5]{0,12})\s*[〔\[（(]\s*(\d{4})\s*[〕\]）)]\s*(\d+)\s*号"
)
DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
DATE_ISO_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
# 落款单位：以这些词结尾的短行
ISSUER_TAIL = re.compile(
    r"(街道办事处|人民政府|居民委员会|社区居委会|委员会|有限公司|服务公司|物业|社区)$"
)
# 文件名里带日期：2026-10-15-关于开展xxx的通知.md
NAME_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[-_]")

MD_SUFFIX = {".md", ".markdown"}
TXT_SUFFIX = {".txt"}
DOCX_SUFFIX = {".docx"}
SUPPORTED = MD_SUFFIX | TXT_SUFFIX | DOCX_SUFFIX


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ 通用工具

def _clean_lines(lines: list[str]) -> list[str]:
    """去掉行尾空白、压缩连续空行（最多保留一个空行）"""
    out: list[str] = []
    for ln in lines:
        ln = ln.replace("\u3000", "  ").rstrip()
        if not ln.strip():
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(ln)
    while out and out[-1] == "":
        out.pop()
    return out


def _to_md_paragraphs(lines: list[str]) -> str:
    """纯文本 → Markdown：段间空行，保留原文措辞不做任何改写"""
    return "\n\n".join(ln.strip() for ln in _clean_lines(lines) if ln.strip())


def _strip_signature(lines: list[str], issuer: str, issued_at: date | None) -> list[str]:
    """去掉正文末尾的落款单位与成文日期

    公文正文最后两行通常就是"XX街道办事处 / 2026年10月15日"，
    而页面底部另有独立的签名区。不去掉就会重复显示两遍落款。
    只从末尾往回最多剥 3 行，避免误删正文里恰好提到单位的句子。
    """
    out = list(lines)
    for _ in range(3):
        if not out:
            break
        last = out[-1].strip()
        if not last:
            out.pop()
            continue
        if issuer and last == issuer:
            out.pop()
            continue
        if last and re.fullmatch(DATE_RE.pattern, last.replace(" ", "")):
            out.pop()
            continue
        if issued_at and last.replace(" ", "") in (
            issued_at.strftime("%Y年%m月%d日"), issued_at.strftime("%Y-%m-%d")):
            out.pop()
            continue
        break
    return out


def _pick_title(paras: list[str]) -> str:
    for p in paras[:4]:
        t = p.strip().lstrip("#").strip()
        # 标题一般 6~80 字，且不以标点结尾
        if 6 <= len(t) <= 80 and not t.endswith(("。", "；", "，", ".")):
            return t
    return paras[0].strip() if paras else ""


def _find_doc_no(text: str) -> str:
    m = DOC_NO_RE.search(text)
    return re.sub(r"\s+", "", m.group(0)) if m else ""


def _find_date(text: str) -> date | None:
    for rx in (DATE_RE, DATE_ISO_RE):
        hits = rx.findall(text)
        if hits:
            y, m, d = hits[-1]          # 落款日期通常在最后
            try:
                return date(int(y), int(m), int(d))
            except ValueError:
                continue
    return None


def _find_issuer(paras: list[str], doc_no_idx: int | None) -> str:
    """三层判定：文号上一行 → 末尾落款 → 文号里的机关简称补全"""
    # 1) 文号上一行若是短机关名，就是发文单位（红头文件的常见排布）
    if doc_no_idx is not None and doc_no_idx > 0:
        prev = paras[doc_no_idx - 1].strip()
        if 4 <= len(prev) <= 40 and ISSUER_TAIL.search(prev) and not prev.startswith("关于"):
            return prev
    # 2) 末尾 6 段里找落款
    for p in reversed(paras[-6:]):
        t = p.strip()
        if 4 <= len(t) <= 40 and ISSUER_TAIL.search(t) and not t.startswith("关于"):
            return t
    # 3) 从文号前缀反推（如"幸街发"→ 无法可靠还原，放弃）
    return ""


def _summarize(body_md: str, limit: int = 120) -> str:
    text = re.sub(r"[#>*|\-`]", "", body_md)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _infer_level(issuer: str) -> str:
    if "街道" in issuer:
        return "街道"
    if "社区" in issuer or "居委会" in issuer:
        return "社区"
    if "物业" in issuer or "公司" in issuer:
        return "物业"
    return "街道"


# ------------------------------------------------------------------ Markdown

FRONT_KEYS = {
    "title": "title", "标题": "title",
    "issuer": "issuer", "发文单位": "issuer", "发文机关": "issuer",
    "doc_no": "doc_no", "文号": "doc_no", "发文字号": "doc_no",
    "issued_at": "issued_at", "成文日期": "issued_at", "日期": "issued_at",
    "category": "category", "类别": "category",
    "level": "level", "层级": "level",
    "scope": "scope", "范围": "scope",
    "pinned": "pinned", "置顶": "pinned",
}


def _split_front_matter(text: str) -> tuple[dict, str]:
    if not text.lstrip().startswith("---"):
        return {}, text
    lines = text.splitlines()
    # 找到第一个非空行必须是 ---
    start = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if start is None or lines[start].strip() != "---":
        return {}, text
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    meta: dict = {}
    for ln in lines[start + 1:end]:
        if ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        key = FRONT_KEYS.get(k.strip().lower()) or FRONT_KEYS.get(k.strip())
        if key:
            meta[key] = v.strip().strip('"').strip("'")
    return meta, "\n".join(lines[end + 1:])


def parse_markdown(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    meta, body = _split_front_matter(raw)
    lines = body.splitlines()
    note: list[str] = []

    # 首行 # 标题：作为正文标题，同时进 body 前去掉，避免页面重复显示
    h1 = ""
    for i, ln in enumerate(lines):
        if ln.strip().startswith("# "):
            h1 = ln.strip()[2:].strip()
            lines = lines[:i] + lines[i + 1:]
            break
        if ln.strip():
            break

    title = meta.get("title") or h1 or _pick_title([l for l in lines if l.strip()])
    body_md = _clean_lines(lines)
    # 先拼一次原文：下面 _find_date 要在"还没剥落款"的完整正文里找发布日期。
    # 这行必须在使用 body_text 之前——之前它被写在 issued_at 之后，
    # 只有 front matter 带 issued_at 时 or 短路才恰好绕过去，
    # 一旦上级给的 md 没写日期就会 UnboundLocalError 整个文件解析失败。
    body_text = "\n".join(body_md)

    doc_no = meta.get("doc_no") or _find_doc_no(raw)   # 文号也可能写在正文里
    issuer = meta.get("issuer") or _find_issuer(body_md, None)
    issued_at = _parse_date(meta.get("issued_at")) or _find_date(body_text) or _date_from_name(path)

    body_md = _strip_signature(body_md, issuer, issued_at)
    body_text = "\n".join(body_md)          # 剥掉落款后重算，摘要里不带落款
    if not meta.get("title"):
        note.append("标题取自正文/文件名，请核对")
    if not doc_no:
        note.append("未识别到发文字号")
    if not issuer:
        note.append("未识别到发文单位")

    return {
        "title": title,
        "issuer": issuer,
        "doc_no": doc_no,
        "issued_at": issued_at,
        "category": meta.get("category") or "通知公告",
        "level": meta.get("level") or _infer_level(issuer),
        "scope": meta.get("scope") or "all",
        "pinned": str(meta.get("pinned", "")).lower() in {"1", "true", "yes", "是"},
        "body_md": "\n".join(body_md),
        "summary": meta.get("summary") or _summarize(body_text),
        "note": note,
    }


def _parse_date(v) -> date | None:
    if not v:
        return None
    v = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日", "%Y.%m.%d"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return _find_date(v)


def _date_from_name(path: Path) -> date | None:
    m = NAME_DATE_RE.match(path.name)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


# ------------------------------------------------------------------ 纯文本

def parse_txt(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    if raw.count("\ufffd") > 20:                       # 多半是 GBK 存的老文件
        try:
            raw = path.read_text(encoding="gbk", errors="replace")
        except Exception:
            pass
    lines = [l for l in raw.splitlines() if l.strip()]
    title = _pick_title(lines)
    # 去掉标题行本身
    body_lines = [l for l in lines if l.strip().lstrip("#").strip() != title]
    issuer = _find_issuer(lines, None)
    doc_no = _find_doc_no(raw)
    issued = _find_date(raw) or _date_from_name(path)
    body_lines = _strip_signature(body_lines, issuer, issued)
    note = []
    if not issuer:
        note.append("纯文本无元信息，未识别到发文单位")
    if not doc_no:
        note.append("未识别到发文字号")
    return {
        "title": title,
        "issuer": issuer,
        "doc_no": doc_no,
        "issued_at": issued,
        "category": "通知公告",
        "level": _infer_level(issuer),
        "scope": "all",
        "pinned": False,
        "body_md": _to_md_paragraphs(body_lines),
        "summary": _summarize(_to_md_paragraphs(body_lines)),
        "note": note,
    }


# ------------------------------------------------------------------ Word

def _docx_table_to_md(table) -> str:
    rows = []
    for row in table.rows:
        cells = [c.text.strip().replace("\n", " ").replace("|", "/") for c in row.cells]
        rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "|" + "|".join(["---"] * width) + "|"]
    for r in rows[1:]:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def parse_docx(path: Path) -> dict:
    """Word 里没有结构化元信息，只能按公文排布猜：
       首行标题 → 红头单位 → 文号 → 正文 → 落款 → 日期
    """
    try:
        from docx import Document
    except Exception:
        return _fail(path, "python-docx 未安装，无法解析 .docx（请另存为 .md）")

    try:
        doc = Document(str(path))
    except Exception as e:
        return _fail(path, f"Word 文件打不开（{type(e).__name__}），可能是 .doc 旧格式")

    paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    if not paras:
        return _fail(path, "Word 文档里没有可读文字")

    title = _pick_title(paras)
    doc_no = ""
    doc_no_idx = None
    for i, p in enumerate(paras):
        if not doc_no and DOC_NO_RE.search(p):
            doc_no = _find_doc_no(p)
            doc_no_idx = i
            break
    issuer = _find_issuer(paras, doc_no_idx)
    issued_at = _find_date("\n".join(paras)) or _date_from_name(path)

    # 正文：标题之后、去掉文号段与落款段
    skip = {title, issuer, doc_no}
    body_lines: list[str] = []
    started = False
    for p in paras:
        if not started:
            if p == title:
                started = True
            continue
        if p in skip:
            continue
        if ISSUER_TAIL.search(p) and len(p) <= 40 and p == issuer:
            continue
        body_lines.append(p)

    body_lines = _strip_signature(body_lines, issuer, issued_at)
    # 表格补在正文末尾
    tables_md = [_docx_table_to_md(t) for t in doc.tables]
    tables_md = [t for t in tables_md if t]
    body_md = _to_md_paragraphs(body_lines)
    if tables_md:
        body_md += "\n\n" + "\n\n".join(tables_md)

    note = []
    if not issuer:
        note.append("未识别到发文单位，请补全")
    if not doc_no:
        note.append("未识别到发文字号")
    if tables_md:
        note.append(f"含 {len(tables_md)} 张表格，已按行列转成表格，请核对")
    return {
        "title": title,
        "issuer": issuer,
        "doc_no": doc_no,
        "issued_at": issued_at,
        "category": "通知公告",
        "level": _infer_level(issuer),
        "scope": "all",
        "pinned": False,
        "body_md": body_md,
        "summary": _summarize(body_md),
        "note": note,
    }


def _fail(path: Path, msg: str) -> dict:
    return {
        "title": path.stem, "issuer": "", "doc_no": "", "issued_at": _date_from_name(path),
        "category": "通知公告", "level": "街道", "scope": "all", "pinned": False,
        "body_md": "", "summary": "", "note": [msg],
    }


def parse_file(path: Path) -> dict:
    suffix = path.suffix.lower()
    if suffix in MD_SUFFIX:
        data = parse_markdown(path)
    elif suffix in DOCX_SUFFIX:
        data = parse_docx(path)
    elif suffix in TXT_SUFFIX:
        data = parse_txt(path)
    else:
        data = _fail(path, f"暂不支持的格式 {suffix}")
    data["source_file"] = path.name
    data["source_hash"] = file_hash(path)
    # 置信判决：标题和发文单位缺一不可，否则进待确认
    missing = [k for k, v in (("标题", data["title"]), ("发文单位", data["issuer"])) if not v]
    data["complete"] = not missing
    if missing:
        data["note"] = data["note"] + [f"缺少{'、'.join(missing)}，需人工补全后发布"]
    return data
