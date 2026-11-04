"""一键安全与流程体检 —— 把散在各处的检查收成一条命令。

为什么要有这个文件
------------------
在此之前，"上线前该看什么"散在 6 个脚本里：`_review_probe.py`（越权）、
`smoke_security.py`（会话/限流）、`smoke_security_hard.py`（导出/XSS 回显）、
`_audit_xss.py`（模板转义）、`check_integrity.py`（16 项数据自洽）、
外加若干 SQL 手查。问题不是这些检查不存在，而是**没有一个人会在改动后
主动跑六遍**——于是"检查过"变成"上次检查过"。

这个脚本做三件事：
  1. 静态项自己算（不依赖服务，不改数据，秒级出结果）
  2. 需要真实环境的项（数据自洽、线上探活）用子进程/只读 HTTP 代跑
  3. 每项都给**判定口径**，避免"存疑 65 处"这种没人看得完的报告

判定分级
--------
  PASS  该做的做了，且能给出证据
  WARN  已知边界或有条件的风险（附原因与代价，不装看不见）
  FAIL  真问题，必须有动作
  SKIP  本次没条件查（比如没连上目标），不是通过

退出码：有 FAIL 返回 1，否则 0。可以直接挂到提交前钩子或 CI。

用法
----
    python scripts/audit_all.py                  # 本机全量（静态 + 数据 + 本机在线）
    python scripts/audit_all.py --offline        # 只静态 + 数据，不碰网络
    python scripts/audit_all.py --target ecs     # 对 ECS 做只读线上体检
    python scripts/audit_all.py --json           # 机器可读（给 CI / 存档）
    python scripts/audit_all.py --only A,C       # 只跑指定组
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
ICON = {PASS: "✓", WARN: "!", FAIL: "✗", SKIP: "-"}

RESULTS: list[dict] = []


def add(group: str, name: str, status: str, detail: str = "", basis: str = ""):
    RESULTS.append({"group": group, "name": name, "status": status,
                    "detail": detail, "basis": basis})


# ---------------------------------------------------------------- A 凭据与密钥

# 必须被 .gitignore 挡住的东西。少一条就等于把钥匙跟代码一起发出去。
CRED_FILES = [".secret_key", ".llm_gateway_token", "llm.publish.json",
              "llm.json", "data/community.db"]
# 源码里出现即视为硬编码（赋值右侧是字符串字面量）
CRED_RE = re.compile(
    r"""(?ix)
    \b(password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key)\b
    \s*=\s*
    (?!\s*(?:os\.environ|os\.getenv|config\.get|request\.|getenv|form\.|args\.|
            json\.|data\.|cfg\.|_cfg|None|"",?\s*$|'',?\s*$))
    (["'])([^"'\n]{6,})\3
    """
)
# 文档里可以出现的东西：演示账号明文（这是给评委看的），不算泄漏
DOC_ALLOW = re.compile(r"admin123|community123|property123|123456|"
                       r"wang123|li123|<你的|xxx|XMM2LKNQOR")


def _ignored(paths: list[str]) -> list[str]:
    """用 git 自己的规则判断，而不是拿文件名去 .gitignore 里搜字符串。

    `.gitignore` 里写的是 `data/*.db`、`*.key` 这类通配，字符串匹配会把
    "已忽略" 误判成 "未忽略"（第一版就踩了：llm.json 与 community.db 报 FAIL）。
    规则只有 git 自己解释得准。
    """
    if not paths:
        return []
    try:
        out = subprocess.run(["git", "check-ignore", "--no-index"] + paths, cwd=ROOT,
                             capture_output=True, text=True, timeout=30,
                             encoding="utf-8", errors="replace")
        ignored = {ln.strip().replace("\\", "/") for ln in out.stdout.splitlines() if ln.strip()}
        return [p for p in paths if p.replace("\\", "/") not in ignored]
    except Exception:                                             # noqa: BLE001
        gi = (ROOT / ".gitignore").read_text(encoding="utf-8") if (ROOT / ".gitignore").exists() else ""
        return [p for p in paths if Path(p).name not in gi and p not in gi]


def group_a():
    if not (ROOT / ".gitignore").exists():
        add("A", "A1 凭据文件被版本库忽略", FAIL, "没有 .gitignore", "缺这个文件=密钥会跟着提交")
    else:
        missing = _ignored(CRED_FILES)
        add("A", "A1 凭据文件被版本库忽略", PASS if not missing else FAIL,
            f"{len(CRED_FILES)} 项全部覆盖" if not missing else "未忽略：" + "、".join(missing),
            "git check-ignore 判定，通配规则也认")

    # 已跟踪文件里是否混进了凭据（.gitignore 只影响未跟踪文件，
    # 早期误提交过一次就会一直留在历史里，这条是最后一道兜底）
    tracked_bad = []
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True, timeout=30, encoding="utf-8", errors="replace")
        tracked = out.stdout.splitlines()
        for f in tracked:
            base = f.rsplit("/", 1)[-1]
            if base in (".secret_key", ".llm_gateway_token", "llm.publish.json", "llm.json") \
                    or base.startswith("community.db"):
                tracked_bad.append(f)
        add("A", "A2 已提交文件里没有凭据", PASS if not tracked_bad else FAIL,
            f"{len(tracked)} 个已跟踪文件，凭据 0 个" if not tracked_bad
            else "已入库：" + "、".join(tracked_bad[:5]),
            "git ls-files 交叉核对")
    except Exception as e:                                        # noqa: BLE001
        add("A", "A2 已提交文件里没有凭据", SKIP, f"{type(e).__name__}", "非 git 仓库或 git 不可用")

    # 源码硬编码：只看真正的字符串字面量，配置读取与本机回环地址不算
    hits = []
    for p in list((ROOT / "app").rglob("*.py")) + [ROOT / "config.py", ROOT / "run.py"]:
        if "__pycache__" in str(p):
            continue
        for i, ln in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            s = ln.strip()
            if s.startswith("#") or "environ" in s or "getenv" in s or "config.get" in s:
                continue
            m = CRED_RE.search(ln)
            if m and len(m.group(4)) >= 8:
                hits.append(f"{p.relative_to(ROOT)}:{i}")
    add("A", "A3 源码无硬编码密钥", PASS if not hits else FAIL,
        f"扫描 {len(list((ROOT / 'app').rglob('*.py')))} 个 py，0 处硬编码" if not hits
        else "命中：" + "、".join(hits[:6]),
        "排除 os.environ / config.get / 回环地址")

    # 文档与模板里不能出现真实令牌（演示密码是允许的）
    leaked = []
    tok_file = ROOT / ".llm_gateway_token"
    if tok_file.exists():
        real = tok_file.read_text(encoding="utf-8").strip()
        if len(real) >= 16:
            for p in list((ROOT / "docs").rglob("*.md")) + list((ROOT / "app" / "templates").rglob("*.html")):
                try:
                    if real in p.read_text(encoding="utf-8", errors="replace"):
                        leaked.append(str(p.relative_to(ROOT)))
                except OSError:
                    pass
    add("A", "A4 真实令牌未写进文档/模板", PASS if not leaked else FAIL,
        "0 处" if not leaked else "出现在：" + "、".join(leaked[:4]),
        "拿 .llm_gateway_token 原文做子串匹配")

    # 密钥强度：短于 32 字符的 Flask SECRET_KEY 可被离线爆破
    sk = ROOT / ".secret_key"
    if sk.exists():
        val = sk.read_text(encoding="utf-8", errors="replace").strip()
        add("A", "A5 SECRET_KEY 强度", PASS if len(val) >= 32 else FAIL,
            f"{len(val)} 字符", "≥32 字符（Flask 会话签名的最小合理长度）")
    else:
        add("A", "A5 SECRET_KEY 强度", SKIP, "本机未落盘（dev 自动生成）", "")


# ---------------------------------------------------------------- B 会话与传输

def group_b():
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DATABASE_URL", "sqlite:///" + str(ROOT / "data" / "community.db"))
    try:
        from config import DevConfig, ProdConfig
    except Exception as e:                                        # noqa: BLE001
        add("B", "B0 读取配置", SKIP, f"{type(e).__name__}: {e}", "")
        return

    add("B", "B1 Cookie HttpOnly", PASS if DevConfig.SESSION_COOKIE_HTTPONLY else FAIL,
        str(DevConfig.SESSION_COOKIE_HTTPONLY), "JS 读不到会话 Cookie，XSS 拿不走身份")
    ss = DevConfig.SESSION_COOKIE_SAMESITE
    add("B", "B2 Cookie SameSite", PASS if ss in ("Lax", "Strict") else FAIL, str(ss),
        "本项目不用 CSRF token，SameSite 是主要防线")
    # Secure 位在 HTTP 下必须为 0，否则浏览器根本不回传 Cookie，登录会假死。
    # 这是"环境限制"而不是"配置错误"，所以报 WARN 并写清前提。
    secure = DevConfig.SESSION_COOKIE_SECURE
    add("B", "B3 Cookie Secure 位", WARN if not secure else PASS,
        "未开启（HTTP 环境）" if not secure else "已开启",
        "上 HTTPS 后必须设 SESSION_COOKIE_SECURE=1，否则 Cookie 明文过网")
    add("B", "B4 生产不启用 DEBUG", PASS if not ProdConfig.DEBUG else FAIL,
        f"prod DEBUG={ProdConfig.DEBUG}", "DEBUG 会在异常页暴露源码与变量")
    add("B", "B5 生产密钥不回落默认值", PASS if ProdConfig.SECRET_KEY is None else FAIL,
        "未配置时走实例密钥文件" if ProdConfig.SECRET_KEY is None else "存在硬编码默认值",
        "公开默认密钥=任何人可伪造会话")


# ---------------------------------------------------------------- C 权限

GUARDS = {"perm_required", "login_required"}
# 这些路由本来就该公开：登录/登出/探活/居民端（凭手机号+查询码，无系统账号）。
# 白名单必须**「文件:函数」精确匹配**：`report`/`notices`/`contacts`/`health`/`me`
# 在 api/ 与页面层是同名函数，按裸函数名放行等于给同名路由开后门。
PUBLIC_OK = {
    ("app/views.py", "login"), ("app/views.py", "logout"),
    ("app/views.py", "health"), ("app/views.py", "notices"),
    ("app/admin.py", "register"),
    ("app/api/admin.py", "me"),
    ("app/api/health.py", "health"), ("app/api/health.py", "ping"),
    ("app/api/health.py", "dbwrite"),
    ("app/api/portal.py", "report"), ("app/api/portal.py", "query_ticket"),
    ("app/api/portal.py", "my_tickets"), ("app/api/portal.py", "ticket_detail"),
    ("app/api/portal.py", "notices"), ("app/api/portal.py", "contacts"),
    ("app/api/portal.py", "roles"),
    ("app/portal.py", "report"), ("app/portal.py", "query"),
    ("app/portal.py", "notices"), ("app/portal.py", "contacts"),
}


def _routes():
    """AST 解析所有路由函数：名字、文件、行号、是否有装饰器守卫、函数体是否有内联守卫

    必须扫 `app/**/*.py` 全量：早先只扫 `app/api/*.py` + views.py + admin.py，
    漏掉了 `app/portal.py`（居民端 9 条）与 `@bp.patch`（3 条改状态的接口）——
    134 条路由里只复核了 122 条，等于给"裸路由 0"一个假的确定性。
    """
    rows = []
    for p in sorted((ROOT / "app").rglob("*.py")):
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        body_lines = src.splitlines()
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            decos = []
            for d in node.decorator_list:
                f = d.func if isinstance(d, ast.Call) else d
                decos.append((getattr(f, "attr", None) or getattr(f, "id", ""), d))
            names = [n for n, _ in decos]
            if not any(n in ("get", "post", "route", "put", "delete", "patch") for n in names):
                continue
            perms = []
            for n, d in decos:
                if n == "perm_required" and isinstance(d, ast.Call) and d.args:
                    a = d.args[0]
                    if isinstance(a, ast.Constant):
                        perms.append(a.value)
            seg = "\n".join(body_lines[node.lineno - 1: getattr(node, "end_lineno", node.lineno)])
            inline = ("current_user()" in seg or "APP_TEST" in seg
                      or "session.get" in seg or "has_perm(" in seg)
            rows.append({
                "file": str(p.relative_to(ROOT)), "func": node.name, "line": node.lineno,
                "guarded": any(n in GUARDS for n in names), "perms": perms,
                "inline": inline,
            })
    return rows


def group_c():
    rows = _routes()
    def _pub(r):
        return (r["file"].replace("\\", "/"), r["func"]) in PUBLIC_OK
    naked = [r for r in rows if not r["guarded"] and not r["inline"] and not _pub(r)]
    add("C", "C1 路由权限覆盖", PASS if not naked else FAIL,
        f"{len(rows)} 个路由，装饰器守卫 {sum(1 for r in rows if r['guarded'])}，"
        f"内联守卫 {sum(1 for r in rows if not r['guarded'] and r['inline'])}，裸路由 {len(naked)}"
        + ("" if not naked else "：" + "、".join(f"{r['file']}:{r['func']}" for r in naked[:5])),
        "AST 查装饰器与函数体内联 current_user()/APP_TEST 判断，两者任一即算有守卫")

    # 声明的权限点与实际使用的权限点双向对账：
    # 声明了没人用 → 死权限；用了没声明 → 永远满足不了，接口恒 403
    try:
        from app.auth import ROLE_PERMS
    except Exception as e:                                        # noqa: BLE001
        add("C", "C2 权限点双向对账", SKIP, f"{type(e).__name__}: {e}", "")
        return
    declared = set()
    for perms in ROLE_PERMS.values():
        declared |= set(perms)
    used = {p for r in rows for p in r["perms"]}
    dead = sorted(declared - used)
    undeclared = sorted(used - declared)
    if undeclared:
        add("C", "C2 权限点双向对账", FAIL,
            f"路由用了但 ROLE_PERMS 未声明：{'、'.join(undeclared[:6])}",
            "未声明=任何角色都没有，接口恒 403")
    elif dead:
        add("C", "C2 权限点双向对账", WARN,
            f"{len(dead)} 个权限点已声明但无路由使用：{'、'.join(dead[:6])}",
            "通常是页面内控件级权限（如按钮显隐），需人工确认是否故意")
    else:
        add("C", "C2 权限点双向对账", PASS, f"{len(declared)} 个权限点全部有路由对应")

    # 防自锁三条：管理员不能把自己弄没
    src = (ROOT / "app" / "api" / "admin.py").read_text(encoding="utf-8", errors="replace")
    checks = [("最后一个管理员", "最后一个"),
              ("不能停用自己", "self" if "uid == u.id" in src or "uid == u.id" in src else "自己")]
    last_admin = "最后一个" in src
    self_guard = ("u.id" in src and ("自己" in src or "uid == u.id" in src))
    add("C", "C3 防管理员自锁", PASS if (last_admin and self_guard) else FAIL,
        f"最后管理员保护={'有' if last_admin else '无'}，"
        f"停用自己不拦={'是' if self_guard else '否'}",
        "三条约束：不停用自己 / 不取消自己 admin / 不失去最后一个管理员")

    # -------------------------------------------------- 导出三件套
    # 能不能导（ABAC decide）/ 水印 / 留痕，缺一不可。
    #
    # 这不是锦上添花：文件一旦下载就脱离了页面，之后没有任何访问日志能追。
    # 水印让"截图外传"能追到人（文案含姓名·单位·时间·动作），
    # 审计让"谁在什么时候导了哪张表"能查回来。
    #
    # 底册导出曾经只挂了 `@perm_required("person.view")`，三样全缺——
    # 那意味着 300 个居民的姓名 / 证件掩码 / 门牌 / 残疾等级被谁导走，
    # 事后查不到。最敏感的「人」反而比「报表」留痕更少。
    exports, lack = 0, []
    for p in sorted((ROOT / "app").rglob("*.py")):
        src = p.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines()
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            rule = None
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and d.args:
                    a = d.args[0]
                    if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                            and "export" in a.value):
                        rule = a.value
            if rule is None:
                continue
            exports += 1
            seg = "\n".join(lines[node.lineno - 1:
                                  getattr(node, "end_lineno", node.lineno)])
            miss = []
            if "decide(" not in seg:
                miss.append("ABAC判定")
            if "wm_text(" not in seg:
                miss.append("水印")
            if "log_export(" not in seg:
                miss.append("审计")
            if miss:
                lack.append(f"{p.relative_to(ROOT)}:{node.name} 缺 " + "/".join(miss))
    add("C", "C4 导出三件套", PASS if not lack else FAIL,
        (f"{exports} 个导出端点均具备 ABAC + 水印 + 审计" if not lack
         else f"{len(lack)}/{exports} 个端点缺件：" + "；".join(lack[:4])),
        "按 URL 含 export 定位端点，函数体内须同时出现 decide( / wm_text( / log_export(")

    # -------------------------------------------------- 按 id 取公告必判范围
    # `can_view` 只写在详情里时，别的按 id 取公告的路径（取草稿 / 生成落地件 /
    # 签收 / 补全 / 置顶 / 办文页 / 居民端详情）各自只判 `if not n`。
    # 结果 `direction=out` 的本级发文虽然在详情页被拒，却能从
    # `GET /api/notices/<id>/draft` 拿到完整正文、还能被 localize 成落地件，
    # 办文页与居民端同样能打开。**同一个"能不能看"的规则散在多处，
    # 每新增一个端点就漏一次**，所以固化成规则。
    miss_guard = []
    for p in sorted((ROOT / "app").rglob("*.py")):
        src = p.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines()
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            seg = "\n".join(lines[node.lineno - 1:
                                  getattr(node, "end_lineno", node.lineno)])
            if "db.session.get(Notice" not in seg:
                continue
            if any(k in seg for k in ("_load_visible(", "can_view(", "direction")):
                continue
            miss_guard.append(f"{p.relative_to(ROOT)}:{node.name}")
    add("C", "C5 取公告必判范围", PASS if not miss_guard else FAIL,
        ("所有按 id 取公告的函数都有范围判断" if not miss_guard
         else f"{len(miss_guard)} 处只判了「存不存在」：" + "、".join(miss_guard[:5])),
        "函数体内出现 _load_visible( / can_view( / direction 任一即算判过；"
        "漏了会让 direction=out 的公文从侧路读到正文")


# ---------------------------------------------------------------- D 前端

SINK_RE = re.compile(r"(innerHTML\s*=|insertAdjacentHTML|outerHTML\s*=|document\.write)")
USER_INPUT = {
    "name", "title", "content", "text", "raw_text", "feedback", "remark", "summary",
    "phone", "id_card", "address", "assignee", "detail", "raw", "reason", "note",
    "tag", "q", "username", "nick", "desc", "comment", "msg", "message", "label",
    "target", "creator", "receiver", "handled_by", "handler", "body", "subject",
    "source", "keyword", "search_text", "err", "error", "hint", "tip", "suggestion", "output",
}
SAFE_FIELD = {
    "id", "uid", "oid", "pid", "count", "ms", "chars", "chunks", "score", "hours",
    "version", "size", "page", "pages", "total", "idx", "n", "i", "level", "chunk_id",
    "doc_id", "order_id", "item_id", "event_count", "perm_count", "saved_hours",
    "uptime_s", "latency_ms", "status", "sla_level", "from_status", "to_status",
    "action", "role", "kind", "scope", "overdue_hours", "left_hours", "sla_hours",
    "fail", "ok", "skipped", "passed", "failed",
}


def _split_exprs(text: str) -> list[str]:
    out, i = [], 0
    while True:
        i = text.find("${", i)
        if i < 0:
            return out
        depth, j = 1, i + 2
        while j < len(text) and depth:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            j += 1
        out.append(text[i + 2:j - 1].strip())
        i = j


def _stmt_at(lines: list[str], idx: int) -> str:
    buf = lines[idx]
    d = buf.count("(") - buf.count(")") + buf.count("{") - buf.count("}")
    k = idx
    while d > 0 and k + 1 < len(lines) and k - idx < 40:
        k += 1
        buf += "\n" + lines[k]
        d += lines[k].count("(") - lines[k].count(")")
        d += lines[k].count("{") - lines[k].count("}")
    return buf


def _ternary_literal(e: str) -> bool:
    """三元表达式且两个分支都是字符串字面量 → 条件部分不进 HTML，安全。

    这是上一版留 65 处"存疑"的主要来源：`(S.q || S.line) ? '没有…' : '暂无…'`
    里的 S.q 只参与判断，输出的是固定中文。判成风险会让人反复复核 3 遍。
    """
    m = re.match(r"^\((.*?)\)\s*\?(.*)$", e, re.S) or re.match(r"^[^?]+\?(.*)$", e, re.S)
    if not e or "?" not in e:
        return False
    body = e[e.index("?") + 1:]
    # 找配对的三元冒号
    depth, k = 0, 0
    while k < len(body):
        ch = body[k]
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
        elif ch == ":" and depth == 0:
            break
        k += 1
    if k >= len(body):
        return False
    yes, no = body[:k].strip(), body[k + 1:].strip()
    lit = lambda s: bool(re.fullmatch(r"""(['"]).*?\1""", s, re.S))
    return lit(yes) and lit(no)


def group_d():
    tpl = ROOT / "app" / "templates"
    stat = {"esc": 0, "safe_field": 0, "ternary": 0, "ident": 0, "risk": 0}
    risks = []
    for p in sorted(tpl.glob("*.html")):
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, ln in enumerate(lines):
            if not SINK_RE.search(ln):
                continue
            for e in _split_exprs(_stmt_at(lines, i)):
                if not e:
                    continue
                if "esc(" in e or "escAttr(" in e:
                    stat["esc"] += 1
                    continue
                if _ternary_literal(e):
                    stat["ternary"] += 1
                    continue
                fields = set(re.findall(r"[A-Za-z_$][\w$]*\.([A-Za-z_$][\w$]*)", e))
                fields |= set(re.findall(r"""\[['"]([\w_]+)['"]\]""", e))
                if fields and fields <= SAFE_FIELD:
                    stat["safe_field"] += 1
                elif fields & USER_INPUT:
                    stat["risk"] += 1
                    risks.append((p.name, i + 1, e.replace("\n", " ")[:70],
                                  sorted(fields & USER_INPUT)))
                elif re.fullmatch(r"[\d.\s+\-*/%()]+", e) or re.fullmatch(r"[A-Za-z_$][\w$]*", e):
                    stat["ident"] += 1
                else:
                    stat["ident"] += 1
    total = sum(stat.values())
    if risks:
        add("D", "D1 模板输出转义", FAIL,
            f"{stat['risk']} 处用户字段未转义：" +
            "；".join(f"{r[0]}:{r[1]} {','.join(r[3])}" for r in risks[:4]),
            "按字段语义判定：用户字段必须 esc()，数字/枚举/三元字面量不算风险")
    else:
        add("D", "D1 模板输出转义", PASS,
            f"插值 {total} 处：esc 包裹 {stat['esc']}、数字/枚举 {stat['safe_field']}、"
            f"三元字面量 {stat['ternary']}、纯标识符 {stat['ident']}，真风险 0",
            "按字段语义判定：用户字段必须 esc()，数字/枚举/三元字面量不算风险")

    # 页面重复定义 esc() 会因函数声明提升覆盖 base.html 的强版本（踩过）
    dup = []
    for p in tpl.glob("*.html"):
        if p.name == "base.html":
            continue
        s = p.read_text(encoding="utf-8", errors="replace")
        if re.search(r"function\s+esc\s*\(", s) or re.search(r"\besc\s*=\s*function", s):
            dup.append(p.name)
    add("D", "D2 页面不重复定义 esc()", PASS if not dup else FAIL,
        "仅 base.html 定义" if not dup else "重复定义：" + "、".join(dup),
        "函数声明提升会静默覆盖强版本，转义形同虚设")

    # 裸 fetch 绕开统一通道层：没有超时归一、没有 .netbar 提示
    allow = {"base.html", "pending.html"}
    bare = []
    for p in tpl.glob("*.html"):
        if p.name in allow:
            continue
        for i, ln in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if re.search(r"(?<![\w.])fetch\s*\(", ln) and "window.api" not in ln:
                bare.append(f"{p.name}:{i}")
    add("D", "D3 前端请求走统一入口", PASS if not bare else WARN,
        "0 处裸 fetch" if not bare else f"{len(bare)} 处裸 fetch：" + "、".join(bare[:5]),
        "统一入口才有 60s 超时、非 JSON 归一、通道失败提示")

    # api() 返回的是 fetch 的原生 Promise，没有 jQuery 的 .fail()。
    # 写成 api(...).then(...).fail(...) 会在"链式构造"阶段就抛 TypeError，
    # 既拿不到错误提示，同一函数里它后面的语句也全部不执行
    # （trace 页因此永远不加载运行列表、risk 页热点表永远停在"加载中"）。
    bad_fail = []
    for p in sorted(tpl.rglob("*.html")):
        for i, ln in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if re.search(r"\)\s*\.fail\s*\(", ln):
                bad_fail.append(f"{p.name}:{i}")
    add("D", "D4 不用原生 Promise 上没有的 .fail", PASS if not bad_fail else FAIL,
        "0 处 .fail" if not bad_fail else f"{len(bad_fail)} 处：" + "、".join(bad_fail[:5]),
        "api() 是原生 Promise，.fail 不存在；要 .catch，且业务失败( code!==0 )由调用方判")

    # getElementById 指向不存在的 id = 空指针，表现就是"按钮点了没反应"
    miss_id = []
    for p in sorted(tpl.rglob("*.html")):
        s = p.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"""getElementById\(\s*['"]([\w.-]+)['"]\s*\)""", s):
            name = m.group(1)
            if not re.search(rf"""id\s*=\s*['"\\]*{re.escape(name)}['"\\]""", s):
                miss_id.append(f"{p.name}→{name}")
    add("D", "D5 getElementById 目标存在", PASS if not miss_id else FAIL,
        "全部能找到对应元素" if not miss_id else f"{len(miss_id)} 处找不到：" + "、".join(miss_id[:6]),
        "元素改名后残留旧 id 会空指针，页面表现为点了没反应")

    # 拉列表失败时必须把失败写进表格本体。只弹顶部提示条不够：
    # 表格里要么还是上一次的数据（用户当成最新的），要么是"暂无记录"
    # （把故障说成"没有数据"）——两种都让故障看起来像正常。
    #
    # 判据按行取窗口：文件级"有一个 try 就算过"太宽——页面上别的按钮有 try，
    # 加载函数照样裸着（system.html 就是这样，153/179 行的 try 与 load() 无关）。
    no_guard = []
    for p in sorted(tpl.rglob("*.html")):
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, ln in enumerate(lines):
            if "await api(" not in ln:
                continue
            near = "\n".join(lines[max(0, i - 12):i + 13])
            if not re.search(r"\btry\s*\{|\.catch\s*\(|\bcatch\s*\(", near):
                no_guard.append(f"{p.name}:{i + 1}")
    no_guard = list(dict.fromkeys(no_guard))
    if no_guard:
        add("D", "D6 请求失败有兜底", FAIL,
            f"{len(no_guard)} 处 await api 没有 try/catch："
            + "、".join(no_guard[:6]),
            "没有兜底时：通道失败会停在「加载中」或继续显示旧数据，"
            "业务失败会伪装成「暂无记录」")
    else:
        add("D", "D6 请求失败有兜底", PASS, "每处 await api 都有 try/catch")


# ---------------------------------------------------------------- E 数据与流程

def group_e():
    db = ROOT / "data" / "community.db"
    if not db.exists():
        add("E", "E1 数据层自洽", SKIP, "没有 community.db", "")
        add("E", "E2 工单流转合法性", SKIP, "没有 community.db", "")
        add("E", "E3 事件链完整", SKIP, "没有 community.db", "")
        return
    # E1 直接复用既有实现，不重写一遍口径（重写就会分叉）
    try:
        out = subprocess.run([PY, "-X", "utf8", str(ROOT / "scripts" / "check_integrity.py")],
                             cwd=ROOT, capture_output=True, text=True, timeout=180,
                             encoding="utf-8", errors="replace")
        body = out.stdout + out.stderr
        ok_n = body.count("[OK")
        bad = [ln.strip() for ln in body.splitlines() if "[FAIL" in ln or "[X" in ln]
        add("E", "E1 数据层自洽（16 项）", PASS if out.returncode == 0 and not bad else FAIL,
            f"{ok_n} 项 OK" if not bad else "；".join(bad[:3]),
            "复用 scripts/check_integrity.py，不另写一套口径")
    except Exception as e:                                        # noqa: BLE001
        add("E", "E1 数据层自洽（16 项）", SKIP, f"{type(e).__name__}", "")

    import sqlite3
    con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
    try:
        # E2 状态机：动作必须在允许的起始状态上。
        # **规则从 orderflow.ALLOWED_FROM 取，不在这里再写一遍**——早先这里写死
        # 了 dispatch/accept/transfer/finish 四个动作，仲裁上线后新增的
        # uphold（维持办结，done→done）和 reject（退回重办，done→open）
        # 全被判成"非法迁移"，于是一条正常的仲裁记录让整套体检永远红着。
        # 这不是数据错，是检查口径落后于定义处（同一处定义两处维护，必然漂移）。
        from app.orderflow import ALLOWED_FROM
        rows = con.execute("""
            select id, order_no, action, from_status, to_status
              from work_order_log order by id
        """).fetchall()
        bad = []
        for rid, no, action, f, to in rows:
            allowed = ALLOWED_FROM.get(action)
            if allowed is None:
                bad.append((rid, no, action, f, to))
            elif f not in allowed:
                bad.append((rid, no, action, f, to))
        n_log = len(rows)
        rule = " / ".join("%s:%s" % (k, ",".join(sorted("NULL" if x is None else x
                                                        for x in v)))
                          for k, v in sorted(ALLOWED_FROM.items()))
        add("E", "E2 工单流转合法性", PASS if not bad else FAIL,
            f"{n_log} 条流转全部落在允许的迁移上" if not bad
            else f"{len(bad)} 条非法：" + str(bad[:3]),
            "口径直接取 app/orderflow.py:ALLOWED_FROM（唯一定义处）：" + rule)

        # E3 事件链：上一步的 to 必须等于下一步的 from，断了就说明有状态被裸改过
        broken = 0
        sample = []
        for (oid,) in con.execute("select distinct order_id from work_order_log"):
            rows = con.execute("""select action, coalesce(from_status,''), to_status
                                    from work_order_log where order_id=?
                                   order by created_at, id""", (oid,)).fetchall()
            for i in range(1, len(rows)):
                if rows[i][1] and rows[i - 1][2] and rows[i][1] != rows[i - 1][2]:
                    broken += 1
                    if len(sample) < 3:
                        sample.append(f"order {oid}: {rows[i-1][0]}→{rows[i-1][2]} 接 {rows[i][0]}(from {rows[i][1]})")
        add("E", "E3 事件链完整", PASS if broken == 0 else FAIL,
            "0 处断裂" if broken == 0 else f"{broken} 处：" + "；".join(sample),
            "上一步 to_status 与下一步 from_status 必须相等")

        # E4 重复办结/重复接单：一张单只能接一次、办一次
        dup = con.execute("""select order_no, sum(action='finish'), sum(action='accept')
                               from work_order_log group by order_no
                              having sum(action='finish')>1 or sum(action='accept')>1""").fetchall()
        add("E", "E4 无重复接单/办结", PASS if not dup else FAIL,
            "0 张" if not dup else f"{len(dup)} 张：" + str(dup[:3]),
            "同一张单 accept / finish 各至多一次")
    finally:
        con.close()


# ---------------------------------------------------------------- F 线上（只读）

def _http(url: str, timeout: float = 20, headers: dict | None = None, data: bytes | None = None):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with op.open(req, timeout=timeout) as r:
        return r.status, dict(r.headers), r.read()


def group_f(target: str):
    if target in ("none", ""):
        add("F", "F0 线上体检", SKIP, "未指定目标", "")
        return
    if target == "local":
        base = "http://127.0.0.1:" + os.environ.get("APP_PORT", "5000")
    elif target == "ecs":
        base = "http://47.108.235.113"
    else:
        base = target.rstrip("/")
    try:
        st, hdrs, body = _http(base + "/api/health", timeout=25)
        d = json.loads(body.decode("utf-8", "replace")).get("data") or {}
        add("F", "F1 服务健康", PASS if d.get("status") == "ok" else WARN,
            f"status={d.get('status')}  uptime={d.get('uptime_s')}s  "
            f"db={((d.get('db') or {}).get('latency_ms'))}ms  "
            f"llm={((d.get('llm') or {}).get('available'))}",
            "降级（模型不可用）业务仍可跑，但拆解退规则桩")
    except Exception as e:                                        # noqa: BLE001
        add("F", "F1 服务健康", FAIL, f"{type(e).__name__}: {str(e)[:60]}", "目标不可达")
        # 服务不可达时 F2-F4 也做不了，但必须**显式报 SKIP**而不是静默跳过：
        # 少三项不吭声，总数会从 29 掉到 26，看报告的人不知道是"没查"还是
        # "查了没过"。SKIP 在口径里本来就不算通过，不存在报喜的问题。
        for _name in ("F2 默认密码未生效", "F3 传输加密", "F4 CSP 头"):
            add("F", _name, SKIP, "服务不可达，本项未检查", "先让目标跑起来再验")
        return

    # 默认密码：只读验证能否登录，不做任何写操作
    import http.cookiejar
    import urllib.parse
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                     urllib.request.HTTPCookieProcessor(cj))
    weak = []
    for user, pw in (("admin", "admin123"), ("admin", "123456")):
        try:
            body = urllib.parse.urlencode({"username": user, "password": pw}).encode()
            req = urllib.request.Request(base + "/login", data=body,
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            with op.open(req, timeout=20) as r:
                if any(c.name == "session" for c in cj):
                    weak.append(f"{user}/{pw}")
            cj.clear()
        except Exception:                                         # noqa: BLE001
            pass
    if target == "local" and weak:
        # 本地实例的口令本来就是演示用的 admin/admin123（docs/09 演示脚本就写这个）。
        # 把它判成 FAIL 会让这条体检对本地永远红着，久了就没人看了——
        # 所以本地出 SKIP（带理由），公网目标（ecs / 自定义 URL）仍然判 FAIL。
        add("F", "F2 默认密码未生效", SKIP,
            f"本地实例使用演示口令：{'、'.join(weak)}",
            "本项只在公网目标上判定；要上线先把口令换掉（scripts/rotate_password.py）")
    else:
        add("F", "F2 默认密码未生效", PASS if not weak else FAIL,
            "默认口令已改" if not weak else f"仍可登录：{'、'.join(weak)}",
            "公网可访问时默认口令等于把后台交出去；本项会写登录审计（成功/失败各一条），是唯一副作用")

    add("F", "F3 传输加密", PASS if base.startswith("https") else WARN, base.split(":")[0] + " 明文",
        "HTTP 下会话 Cookie 可被嗅探，SESSION_COOKIE_SECURE 只能关着")
    csp = hdrs.get("Content-Security-Policy", "")
    add("F", "F4 CSP 头", PASS if csp else WARN, (csp[:70] + "…") if csp else "未下发",
        "至少限制 frame-ancestors 与 script-src")


# ---------------------------------------------------------------- 主流程

GROUPS = {"A": ("凭据与密钥", group_a), "B": ("会话与传输", group_b),
          "C": ("权限", group_c), "D": ("前端", group_d)}


def main() -> int:
    ap = argparse.ArgumentParser(description="一键安全与流程体检")
    ap.add_argument("--target", default="local",
                    help="local（本机 5000）/ ecs / none / 任意 base URL")
    ap.add_argument("--offline", action="store_true", help="跳过所有网络检查")
    ap.add_argument("--only", default="", help="只跑指定组，逗号分隔，如 A,C")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--quiet", action="store_true", help="只打印非 PASS 项")
    args = ap.parse_args()

    want = [g.strip().upper() for g in args.only.split(",") if g.strip()] or list(GROUPS) + ["E", "F"]

    for key in want:
        if key == "E":
            group_e()
        elif key == "F":
            if args.offline:
                add("F", "F0 线上体检", SKIP, "--offline", "")
            else:
                group_f(args.target)
        elif key in GROUPS:
            try:
                GROUPS[key][1]()
            except Exception as e:                                # noqa: BLE001
                add(key, f"{key}0 组执行异常", FAIL, f"{type(e).__name__}: {e}", "")
        else:
            add("?", f"未知分组 {key}", SKIP, "", "")

    n = {s: sum(1 for r in RESULTS if r["status"] == s) for s in (PASS, WARN, FAIL, SKIP)}
    if args.json:
        print(json.dumps({"generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                          "target": args.target, "summary": n, "items": RESULTS},
                         ensure_ascii=False, indent=2))
        return 1 if n[FAIL] else 0

    gname = {"A": "凭据与密钥", "B": "会话与传输", "C": "权限", "D": "前端",
             "E": "数据与流程", "F": "线上（只读）"}
    print("=" * 78)
    print("  安全与流程体检   目标：%s   %s" % (args.target, __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")))
    print("=" * 78)
    cur = None
    for r in RESULTS:
        if args.quiet and r["status"] == PASS:
            continue
        if r["group"] != cur:
            cur = r["group"]
            print("\n[%s] %s" % (cur, gname.get(cur, cur)))
        line = "  %s %-26s %s" % (ICON[r["status"]], r["name"], r["detail"])
        print(line)
        if r["basis"] and r["status"] in (WARN, FAIL):
            print("    └ 口径：%s" % r["basis"])

    print("\n" + "-" * 78)
    print("  通过 %d ｜ 警告 %d ｜ 失败 %d ｜ 跳过 %d" % (n[PASS], n[WARN], n[FAIL], n[SKIP]))
    if n[FAIL]:
        print("  有失败项，先处理再上线。")
    elif n[WARN]:
        print("  无失败项；警告项是已知边界，确认可接受即可。")
    else:
        print("  全部通过。")
    print("-" * 78)
    return 1 if n[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
