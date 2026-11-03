"""输出转义审计：扫描模板里所有 innerHTML / insertAdjacentHTML 的模板串，
把每个 `${...}` 表达式拎出来，判断是否经过 esc() 之类的白名单函数。

判定口径：
  · 已转义 —— 表达式以 esc(/escAttr(/num(/fmt(/renderX( 开头，或纯数字/已知常量
  · 存疑   —— 其余一律列出，人工复核（宁可多报）
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent / "app" / "templates"
SAFE_CALLS = ("esc(", "escAttr(", "num(", "fmt(", "_esc(")
# 这些是"我方常量/枚举映射"，不是接口数据
SAFE_PREFIX_TOKENS = ("color", "CLS[", "ICON[", "AGENT_CN[", "STATUS_CN[",
                      "LINE_CN[", "LV[", "TAG[", "ROLE_CN[")
SINK_RE = re.compile(r"(innerHTML\s*=|insertAdjacentHTML|outerHTML\s*=|document\.write)")


def split_exprs(text):
    """从一段 JS 里取出所有 ${...} 的内部表达式（支持嵌套花括号）"""
    out = []
    i = 0
    while True:
        i = text.find("${", i)
        if i < 0:
            break
        depth, j = 1, i + 2
        while j < len(text) and depth:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            j += 1
        out.append(text[i + 2:j - 1].strip())
        i = j
    return out


def stmt_at(lines, idx):
    """把一条可能跨多行的赋值语句拼出来：从当前行到括号/分号配平"""
    buf = lines[idx]
    depth = buf.count("(") - buf.count(")") + buf.count("{") - buf.count("}")
    k = idx
    while depth > 0 and k + 1 < len(lines) and k - idx < 40:
        k += 1
        buf += "\n" + lines[k]
        depth += lines[k].count("(") - lines[k].count(")")
        depth += lines[k].count("{") - lines[k].count("}")
    return buf


def is_safe(expr):
    if not expr:
        return True
    if expr.startswith(SAFE_CALLS):
        return True
    if expr.startswith(SAFE_PREFIX_TOKENS):
        return True
    if re.fullmatch(r"[\d\.\s+\-*/%]+", expr):          # 纯算术
        return True
    if re.fullmatch(r"[A-Za-z_$][\w$]*", expr) and expr in ("i", "n", "idx"):
        return True
    if expr in ("''", '""'):
        return True
    return False


hits, safe_n = [], 0
for p in sorted(ROOT.glob("*.html")):
    lines = p.read_text(encoding="utf-8").splitlines()
    for i, ln in enumerate(lines):
        if not SINK_RE.search(ln):
            continue
        body = stmt_at(lines, i)
        for e in split_exprs(body):
            if is_safe(e):
                safe_n += 1
            else:
                hits.append((p.name, i + 1, e.replace("\n", " ")[:110]))

print(f"已确认转义的插值 {safe_n} 处；存疑 {len(hits)} 处\n")
cur = None
for name, line, e in hits:
    if name != cur:
        print(f"--- {name}")
        cur = name
    print(f"  L{line:<4} ${{{e}}}")
