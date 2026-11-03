"""回归：数据库坏了，服务也得先起来

线上真事故：发布沙箱的 data/ 是持久卷，部署只换代码不换库。卷上的 SQLite
被强杀成 `file is not a database` 之后，`tune_sqlite()` 里那次 engine.connect()
是第一个撞上去的地方 → create_app 抛异常 → 进程没起来 → 平台只报
"服务没有在指定端口上响应"，分享链接直接打不开。

这是**最坏的一种失败**：修库的入口（/api/health/db-check、.reset_db 标记）
全在应用里，应用起不来就一个都用不上，只能改代码重发。

所以它必须满足两条：
  ① 有种子库就自己换掉，换完照常跑；
  ② 换不成就降级启动——页面打得开、健康检查能看、日志有一行结论。

每个用例都起**独立子进程**跑：config.py 在类体里读 DATABASE_URL，
同一个进程里改环境变量是改不动它的（第一版就是这么假绿的），
必须换进程，这恰好也等于线上的"一次部署"。
"""
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

SEED_SRC = BASE / "data" / "community.db"
WORK = BASE / "data" / "_boottest"
PASS = FAIL = 0
FAILED = []

CHILD = r"""
import json, os, sys
from app import create_app
app = create_app("dev")
out = {"booted": True}
with app.test_client() as c:
    r = c.get("/api/health")
    d = r.get_json()["data"]
    out["health_http"] = r.status_code
    out["db_ok"] = d["db"]["ok"]
    lp = c.get("/login")
    out["login_http"] = lp.status_code
    c.post("/login", data={"username": "wang", "password": "123456"},
           follow_redirects=False)
    out["index_http"] = c.get("/").status_code
    # boot 字段只在登录后返回（匿名不给部署形态），所以要放在登录之后再取
    out["boot_flag"] = c.get("/api/health").get_json()["data"].get("boot", {})
print("__RESULT__" + json.dumps(out, ensure_ascii=False))
"""


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  [{detail}]" if detail else ""))


def run(db_path):
    """起个子进程跑 create_app，返回 (退出码, 结果字典, 原始输出)"""
    env = dict(os.environ)
    env["DATABASE_URL"] = "sqlite:///" + str(db_path)
    env["NOTICE_AUTO_IMPORT"] = "0"
    env.pop("LLM_BASE_URL", None)
    p = subprocess.run([sys.executable, "-c", CHILD], cwd=str(BASE), env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    res = {}
    for line in (p.stdout or "").splitlines():
        if line.startswith("__RESULT__"):
            import json
            res = json.loads(line[len("__RESULT__"):])
    return p.returncode, res, (p.stdout or "") + (p.stderr or "")


def fresh(case, seed=True, corrupt=True):
    d = WORK / case
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True, exist_ok=True)
    db = d / "community.db"
    if seed:
        shutil.copy2(SEED_SRC, d / "community.db.seed")
    if corrupt:
        db.write_bytes(b"garbage-" + b"x" * 80)
    return d, db


def q(path, sql):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchone()
    finally:
        con.close()


# ---------------------------------------------------------------- 1 自愈
print("\n[1] 坏库 + 有种子库 → 自己换掉并照常启动")
d, db = fresh("heal")
rc, res, log = run(db)
check("进程退出码 0（起得来）", rc == 0, log[-300:])
check("库文件被换成可读的", q(db, "PRAGMA quick_check")[0] == "ok")
check("留了 .bak 备份", (d / "community.db.bak").exists())
check("健康检查 db.ok", res.get("db_ok") is True, res)
check("登录页 200", res.get("login_http") == 200, res)
check("自愈后业务可用（首录台）", res.get("index_http") == 200, res)
check("健康页记得这次自愈",
      res.get("boot_flag", {}).get("ok") is False
      and "种子库" in res.get("boot_flag", {}).get("error", ""),
      res.get("boot_flag"))

# ---------------------------------------------------------------- 2 降级
print("\n[2] 坏库 + 没有种子库 → 降级启动，不许抛")
d, db = fresh("degraded", seed=False)
rc, res, log = run(db)
check("进程仍然起来了", rc == 0, log[-300:])
check("健康检查返回 200", res.get("health_http") == 200, res)
check("并且如实说 db 不通", res.get("db_ok") is False, res)
bo = (d / "boot_error.log")
check("落了一行可读结论",
      bo.exists() and "not a database" in bo.read_text(encoding="utf-8",
                                                       errors="replace"))

# ---------------------------------------------------------------- 3 不误伤
print("\n[3] 好库不许被动")
d = WORK / "healthy"
if d.exists():
    shutil.rmtree(d)
d.mkdir(parents=True, exist_ok=True)
db = d / "community.db"
shutil.copy2(SEED_SRC, db)
before = db.read_bytes()
rc, res, log = run(db)
check("正常启动", rc == 0 and res.get("db_ok") is True, log[-200:])
check("库还能用且表没少", q(db, "PRAGMA quick_check")[0] == "ok"
      and q(db, "select count(*) from user")[0] == q(SEED_SRC, "select count(*) from user")[0])
check("没有生成 .bak", not (d / "community.db.bak").exists())
check("没有写 boot_error.log", not (d / "boot_error.log").exists())

# ---------------------------------------------------------------- 4 重置标记
print("\n[4] .reset_db 标记仍然有效（换库要显式、且幂等）")
d, db = fresh("flag", corrupt=False)
(d / ".reset_db").write_text("reset-test", encoding="utf-8")
con = sqlite3.connect(db)          # 在库里留一条痕迹，验证确实换了库
con.execute("CREATE TABLE IF NOT EXISTS _mark(x)")
con.execute("INSERT INTO _mark VALUES ('old')")
con.commit()
con.close()
rc, res, log = run(db)
check("执行过标记写入 .done",
      (d / ".reset_db.done").exists()
      and (d / ".reset_db.done").read_text(encoding="utf-8").strip() == "reset-test",
      log[-200:])
check("旧库被换掉（种子库里没有 _mark 表）",
      q(db, "SELECT name FROM sqlite_master WHERE name='_mark'") is None)
run(db)                            # 再部署一次
check("重复部署不会二次重置",
      q(db, "SELECT name FROM sqlite_master WHERE name='_mark'") is None
      and (d / ".reset_db.done").read_text(encoding="utf-8").strip() == "reset-test")

print("\n" + "=" * 66)
print(f"通过 {PASS} 项 / 失败 {FAIL} 项")
if FAILED:
    print("失败项：")
    for x in FAILED:
        print("  -", x)
print("=" * 66)

shutil.rmtree(WORK, ignore_errors=True)

sys.exit(1 if FAIL else 0)
