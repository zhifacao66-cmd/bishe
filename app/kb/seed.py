"""知识库文档导入：把 data/kb 下的 .md/.txt 读进 kb_doc 表（幂等）

维护方式：改文件 → 重启服务 → 自动增量入库（按标题去重，不覆盖已改内容）。
生产环境可替换为本社区现行文件，文件名前缀数字仅用于排序。
"""
import os
import re

from app.extensions import db
from app.models import KbDoc

_CATEGORY_RULES = [
    ("模板", "表格模板"), ("填报", "表格模板"), ("口径", "表格模板"),
    ("规范", "工作规范"), ("流程", "工作规范"), ("指引", "工作规范"),
    ("指南", "办事指南"), ("办理", "办事指南"), ("须知", "办事指南"),
]


def guess_category(title: str) -> str:
    for kw, cat in _CATEGORY_RULES:
        if kw in title:
            return cat
    return "政策文件"


def import_dir(app) -> int:
    folder = app.config.get("KB_DIR", "")
    if not folder or not os.path.isdir(folder):
        return 0
    added = 0
    for fn in sorted(os.listdir(folder)):
        if not fn.lower().endswith((".md", ".txt")):
            continue
        path = os.path.join(folder, fn)
        try:
            raw = open(path, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        m = re.search(r"^#\s+(.+)$", raw, re.M)
        title = (m.group(1).strip() if m
                 else re.sub(r"^\d+[-_\s]*", "", os.path.splitext(fn)[0]))
        if KbDoc.query.filter_by(title=title).first():
            continue
        src = ""
        ms = re.search(r"^>\s*来源[:：]\s*(.+)$", raw, re.M)
        if ms:
            src = ms.group(1).strip()
        db.session.add(KbDoc(title=title, category=guess_category(title),
                             source=src, content=raw, created_by="system"))
        added += 1
    if added:
        db.session.commit()
    return added


def init_kb_docs(app) -> int:
    with app.app_context():
        return import_dir(app)
