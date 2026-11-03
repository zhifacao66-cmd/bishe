"""统计简历要用的事实：代码规模 / Agent 数 / 接口数 / 测试套。
口径全部写进输出，避免"看着差不多"。
"""
import ast
import io
import json
import os
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def py_lines(p: Path):
    """返回 (物理行, 有效行)。有效行 = 非空、非纯注释、非纯 docstring。
    用 tokenize 精确剔除注释与字符串语句，比 `not line.startswith('#')` 可信。
    """
    src = p.read_text(encoding="utf-8", errors="replace")
    total = len(src.splitlines())
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except Exception:
        return total, sum(1 for ln in src.splitlines() if ln.strip())
    drop = set()          # 要剔除的行号
    for t in toks:
        if t.type == tokenize.COMMENT:
            for ln in range(t.start[0], t.end[0] + 1):
                drop.add(ln)
    # 用 AST 找 docstring / 纯字符串表达式语句
    try:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                for ln in range(node.lineno, node.end_lineno + 1):
                    drop.add(ln)
    except Exception:
        pass
    eff = 0
    for i, ln in enumerate(src.splitlines(), 1):
        if i in drop or not ln.strip():
            continue
        eff += 1
    return total, eff


def walk(suffix, roots, exclude_dirs=()):
    out = []
    for r in roots:
        d = ROOT / r
        if not d.exists():
            continue
        for p in d.rglob(f"*{suffix}"):
            parts = set(p.parts)
            if "__pycache__" in parts or parts & set(exclude_dirs):
                continue
            out.append(p)
    return sorted(out)


def code_size():
    groups = {}

    def add(name, files):
        t = e = 0
        for p in files:
            txt = p.read_text(encoding="utf-8", errors="replace")
            a, b = py_lines(p) if p.suffix == ".py" else (
                len(txt.splitlines()),
                sum(1 for ln in txt.splitlines()
                    if ln.strip() and not ln.strip().startswith(("//", "/*", "*")))
            )
            t += a
            e += b
        groups[name] = {"files": len(files), "lines": t, "effective": e}

    app_py = walk(".py", ["app", "config.py", "run.py"] if False else ["app"])
    add("app_py", app_py)
    add("run_config", [ROOT / "run.py", ROOT / "config.py"])
    add("scripts_py", walk(".py", ["scripts"]))
    add("html", walk(".html", ["app/templates"]))
    # 排除 static/vendor：那是 Tabler/ECharts，不是自己写的，算进去等于虚报规模
    add("own_js_css", [x for x in walk(".js", ["app/static"]) + walk(".css", ["app/static"])
                       if "vendor" not in x.parts])
    groups["TOTAL_APP"] = {
        "files": sum(g["files"] for k, g in groups.items() if k != "scripts_py"),
        "lines": sum(g["lines"] for k, g in groups.items() if k != "scripts_py"),
        "effective": sum(g["effective"] for k, g in groups.items() if k != "scripts_py"),
    }
    return groups


def agents():
    """继承 BaseAgent 的类，以及各 Agent 的 agent_name / 说明。"""
    base = ROOT / "app/agents/base.py"
    names = {}
    for p in walk(".py", ["app/agents"]):
        src = p.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for b in node.bases:
                    bn = b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")
                    if bn == "BaseAgent":
                        names[node.name] = p.name
    return names


def routes():
    from app import create_app
    os.environ.setdefault("FLASK_ENV", "dev")
    app = create_app("dev")
    rules = []
    for r in app.url_map.iter_rules():
        if r.endpoint == "static":
            continue
        methods = sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS"))
        rules.append({"rule": str(r.rule), "methods": methods, "endpoint": r.endpoint})
    return rules


if __name__ == "__main__":
    res = {}
    res["size"] = code_size()
    res["agents"] = agents()
    try:
        r = routes()
        res["routes_total"] = len(r)
        res["routes_api"] = len([x for x in r if x["rule"].startswith("/api/")])
        res["routes_page"] = len([x for x in r if not x["rule"].startswith("/api/")])
        res["routes_handlers"] = len({x["endpoint"] for x in r})
        res["methods"] = {}
        for x in r:
            for m in x["methods"]:
                res["methods"][m] = res["methods"].get(m, 0) + 1
        res["api_list"] = [f'{"/".join(x["methods"])} {x["rule"]}' for x in r]
    except Exception as e:
        res["routes_error"] = repr(e)[:400]
    smokes = sorted(p.name for p in (ROOT / "scripts").glob("smoke_*.py"))
    res["smoke_suites"] = len(smokes)
    res["smoke_files"] = smokes
    print(json.dumps(res, ensure_ascii=False, indent=1))
