"""健康检查与运行态探针

用途：压测/长稳脚本采样、部署存活探针、演示时自检。
刻意**不返回任何业务数据**（人数、户数、审计内容），只暴露依赖状态与资源用量，
避免健康接口成为保密场景下的侧信道。
"""
import os
import threading
import time

from flask import Blueprint, current_app, jsonify
from sqlalchemy import text

from app.api import fail, ok
from app.auth import current_user, login_required, perm_required, user_perms
from app.extensions import db
from app.llm import client as llm_client
from app.llm.client import describe
from app.llm.gate import gate

bp = Blueprint("health", __name__)

_START = time.time()
_llm_cache = {"ts": 0.0, "info": {"ok": False, "error": "未探活", "ms": 0}}

try:
    import psutil
    _PROC = psutil.Process(os.getpid())
except Exception:               # psutil 缺失时降级，不影响主流程
    psutil, _PROC = None, None


def _llm_probe(ttl: float | None = None) -> dict:
    """模型探活带缓存，并**保留失败原因**。

    本机通道 5s 就够（一次 localhost RTT）；远程通道给 20s——
    线上每次探活都是一次跨公网往返，压测时长稳采样会把隧道打满。

    缓存的是整个结果而不只是布尔值：线上曾经出现健康页显示"在线"、
    实际每次调用都降级的情况，根因是探活只留了 True/False，
    "连上了但推理失败"和"根本没连上"在页面上长得一样。
    """
    try:
        remote = describe()["remote"]
    except Exception:
        remote = False
    if ttl is None:
        ttl = 20.0 if remote else 5.0
    now = time.time()
    if now - _llm_cache["ts"] > ttl:
        _llm_cache["info"] = llm_client.probe()
        _llm_cache["ts"] = now
    return _llm_cache["info"]


def _llm_alive(ttl: float | None = None) -> bool:
    return bool(_llm_probe(ttl).get("ok"))


# —— 侧栏状态灯的「只读快照」 ——
#
# 为什么不能直接用 _llm_probe()：它过期就**同步**探一次。远程通道 TLS 握手
# 实测要 9~26 秒，把这个放进页面渲染路径等于让每个页面都可能卡半分钟。
# 侧栏是个"随时挂着"的装饰件，它的正确设计是：读缓存、立刻返回、
# 顺手在后台补一次探活——状态晚 20 秒是能接受的，页面卡 20 秒不行。
_cache_guard = {"busy": False}
_guard = threading.Lock()

_PROBE_TTL_REMOTE, _PROBE_TTL_LOCAL = 20.0, 5.0

_ERR_TEXT = {
    "connect": "通道连不上",
    "timeout": "探活超时",
    "http_401": "令牌不匹配",
    "http_5xx": "网关异常",
    "bad_response": "返回无法解析",
    "http_403": "网关拒绝访问",
    "http_404": "网关路径不存在",
}


def _spawn_probe() -> None:
    """后台补探一次；已经有探活在飞就不再起第二个。

    不加这个标志，20 秒轮询 × 多标签页会把隧道打成排队——同一时刻
    十几个 25 秒超时的探活排在一起，结果全是 timeout，
    于是状态灯红给你看，而模型其实好好的。

    **必须把 app 带进线程**：probe() 会经 channel.resolve() 读 current_app.config，
    没有应用上下文会抛 RuntimeError。这个异常如果只 catch 不处理，
    表现就是"计时器永远不过期、灯永远是检测中"——看着像前端坏了。
    """
    with _guard:
        if _cache_guard["busy"]:
            return
        _cache_guard["busy"] = True
    app = current_app._get_current_object()

    def _run():
        try:
            with app.app_context():
                _llm_cache["info"] = llm_client.probe()
                _llm_cache["ts"] = time.time()
        except Exception as e:                  # noqa: BLE001  探活工具自己不许炸
            _llm_cache["info"] = {"ok": False, "ms": 0, "error": "probe_error",
                                  "detail": f"{type(e).__name__}: {e}"[:160]}
            _llm_cache["ts"] = time.time()
        finally:
            _cache_guard["busy"] = False

    threading.Thread(target=_run, name="llm-probe", daemon=True).start()


def llm_badge() -> dict:
    """侧栏状态灯的展示模型。页面首屏直接渲染它，接口轮询也复用它。

    单一来源的意义：首屏说"运行中"、两秒后轮询说"不可用"，是最难查的一类 bug。
    """
    try:
        ch = describe()
        remote = bool(ch["remote"])
        model = ch["model"]
        label = ch.get("label") or ""
    except Exception:                            # noqa: BLE001
        ch, remote, model, label = {}, False, "", ""

    ttl = _PROBE_TTL_REMOTE if remote else _PROBE_TTL_LOCAL
    probed = _llm_cache["ts"] > 0
    age = round(time.time() - _llm_cache["ts"], 1) if probed else None
    if not probed or age > ttl:
        _spawn_probe()

    info = _llm_cache["info"] or {}
    ok = bool(info.get("ok"))

    # 「数据不出内网」这句话在大屏上是要被评委追问的，所以它必须跟着通道走：
    # 本机直连才成立；经隧道回源时数据确实出了本机，只能承诺"只回自己那台机器"。
    where = "数据不出内网" if not remote else "加密隧道回本机 GPU"
    if not probed:
        state, title, sub = "idle", "模型状态检测中", f"{model} · 等待首次探活"
    elif ok:
        state, title, sub = "ok", "本地大模型运行中", f"{model} · {where}"
    else:
        why = _ERR_TEXT.get(info.get("error", ""), info.get("error") or "未探活")
        state, title, sub = "bad", "模型未连接 · 已降级", f"{why} · 走规则路径"

    tip = []
    if label:
        tip.append(f"通道：{label}")
    if ok and info.get("ms"):
        tip.append(f"探活 {info['ms']}ms")
    if info.get("detail"):
        tip.append(str(info["detail"])[:120])
    if age is not None:
        tip.append(f"{int(age)}s 前探活")
    return {"state": state, "title": title, "sub": sub, "ok": ok,
            "state_age_s": age, "error": info.get("error", ""),
            "detail": info.get("detail", ""), "ms": info.get("ms", 0),
            "channel": ch.get("channel", ""),
            "remote": remote, "model": model, "tip": "；".join(tip)}


def _boot_info() -> dict:
    """启动期是否自愈过 / 是否降级启动。

    为什么要出现在健康页：托管沙箱只看得到 stderr 尾巴，完整 traceback 常被
    截在中间，真正那句理由（file is not a database / unable to open …）
    正好被切掉。落一份到 data/boot_error.log 再从这里读出来，
    排障就不用靠猜。
    """
    uri = current_app.config.get("SQLALCHEMY_DATABASE_URI") or ""
    if not uri.startswith("sqlite"):
        return {}
    folder = os.path.dirname(uri.split("sqlite:///", 1)[-1])
    log = os.path.join(folder, "boot_error.log")
    out = {"ok": True, "error": ""}
    try:
        if os.path.exists(log):
            with open(log, encoding="utf-8", errors="replace") as fp:
                lines = [ln.strip() for ln in fp if ln.strip()]
            if lines:
                out.update(ok=False, error=lines[-1][:200],
                           count=len(lines))
    except Exception:                                  # noqa: BLE001
        pass
    return out


@bp.get("")
def health():
    t0 = time.perf_counter()
    db_ok, db_err = True, ""
    try:
        db.session.execute(text("SELECT 1"))
    except Exception as e:                                   # noqa: BLE001
        db_ok, db_err = False, str(e)[:120]
    db_ms = round((time.perf_counter() - t0) * 1000, 2)

    proc = {}
    if _PROC is not None:
        try:
            mi = _PROC.memory_info()
            proc = {
                "rss_mb": round(mi.rss / 1024 / 1024, 1),
                "threads": _PROC.num_threads(),
                "cpu_percent": _PROC.cpu_percent(interval=None),
            }
        except Exception:                                    # noqa: BLE001
            proc = {}

    pr = _llm_probe()
    llm_alive = bool(pr.get("ok"))
    # 探针兼维护：顺手回收卡在网络层的僵死占位。
    # 放在这里是因为健康检查是唯一"一定会被周期调用"的入口（前端轮询、
    # 压测采样、托管平台的存活探针都会打它），不需要额外起后台线程。
    gate.reclaim_stale()
    status = "ok" if (db_ok and llm_alive) else ("degraded" if db_ok else "down")
    is_sqlite = current_app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite")
    ch = describe()
    # 分两档：**匿名只给存活状态**，登录后才给部署形态。
    #
    # 为什么要分：通道地址、令牌是否配置、进程号、调用统计、db.error 原文
    # 都不是业务数据，但它们是攻击面的地图——知道模型网关的公网地址就能
    # 直接打那个端口，db.error 里常带表名和绝对路径，pid 能用来猜进程管理接口。
    # 公网匿名就能拿到这些，等于把内部结构白送；而内部人员排障确实需要它们。
    #
    # 为什么不是"只给管理员"：模型通道出问题时，社区值班的人也要能自证
    # "是模型降级了，不是我操作错了"，卡在管理员那里会让排障变慢。
    try:
        logged_in = current_user() is not None
    except Exception:                                    # noqa: BLE001
        logged_in = False

    if not logged_in:
        return ok({
            "status": status,
            "uptime_s": round(time.time() - _START, 1),
            "db": {"ok": db_ok, "latency_ms": db_ms},
            "llm": {"available": llm_alive,
                    "model": current_app.config.get("LLM_MODEL", "")},
        })

    data = {
        "status": status,
        "uptime_s": round(time.time() - _START, 1),
        "db": {"ok": db_ok, "latency_ms": db_ms, "error": db_err,
               "engine": "sqlite" if is_sqlite else "mysql"},
        # channel 说明推理跑在哪：local=本机 Ollama，remote=经隧道回本机 GPU。
        # 这两个值在论文里不是实现细节——它决定「数据有没有出内网」。
        "llm": {"available": llm_alive, "model": ch["model"], "channel": ch,
                "probe": pr,                      # 探活失败时这里带原因与耗时
                "stats": llm_client.stats(), "gate": gate.snapshot()},
        "process": proc,
        "pid": os.getpid(),
        "thread": threading.current_thread().name,
        "boot": _boot_info(),          # 启动期是否自愈/降级（空=干净启动）
    }
    return ok(data)


@bp.get("/ping")
def ping():
    """极轻量存活探针（不查库、不探模型），用于测框架自身的并发上限"""
    return jsonify({"code": 0, "msg": "ok", "data": {"t": time.time()}})


@bp.get("/llm")
@login_required
def llm_state():
    """侧栏状态灯的轮询入口：只读缓存，不做同步探活。

    要求登录：通道类型、模型名、失败原因都在里面，匿名没必要知道；
    而它服务的对象（左侧栏）本来就只出现在登录后的页面里。
    """
    return ok(llm_badge())


_test_lock = threading.Lock()
_last_test = {"ts": 0.0}


@bp.post("/llm-test")
@perm_required("user.manage")
def llm_test():
    """真实调用一次模型并返回耗时与原文。

    这不是探活（探活只证明端口通），而是让它干一件真实业务活——
    验收时"接上了"和"跑起来了"的差别就在这里：能看到输出、耗时和模型名。
    """
    now = time.time()
    with _test_lock:
        if now - _last_test["ts"] < 3:
            return fail("上一次自检刚结束，请几秒后再试", 429), 429
        _last_test["ts"] = now

    prompt = ("把下面这句话压缩成一条不超过 12 个字的工单标题，"
              "只输出标题本身，不要解释、不要引号：\n"
              "3栋2单元5楼的楼道声控灯坏了，晚上居民上下楼看不见，已经持续两天")
    ch = describe()
    t0 = time.perf_counter()
    try:
        text = llm_client.chat(
            prompt, system="你是社区工单助手。只输出结果本身，不寒暄、不解释。")
        return ok({
            "ok": True, "output": (text or "").strip()[:120],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
            "model": ch["model"], "channel": ch,
        })
    except llm_client.LLMUnavailable as e:
        # 模型不可用不是接口错误：这是设计好的降级路径，业务会走规则兜底。
        # 所以照样返回 200 + ok=False，让页面说清楚"现在走的是规则，不是模型"。
        return ok({"ok": False, "reason": str(e)[:200],
                   "elapsed_ms": round((time.perf_counter() - t0) * 1000),
                   "model": ch["model"], "channel": ch})


@bp.get("/net")
@perm_required("user.manage")
def net_check():
    """容器出网自检：DNS / TCP / TLS / 代理变量分开测。

    线上排障最耗时的一步是"到底是谁不通"：前置网关 60 秒掐断请求时，
    容器里既可能是 DNS 解析不到、也可能是代理不认这个域名、还可能只是 TLS 握手慢。
    三者表现都是"请求没回来"，但处置完全不同，所以拆开测给自己看。
    """
    return ok(llm_client.probe_net())


@bp.get("/db-check")
@perm_required("user.manage")
def db_check():
    """SQLite 完整性自检。

    为什么线上需要它：托管环境的 data/ 是持久卷，部署改不动库。
    一旦这个文件在写入途中被强杀成损坏，业务接口会全线 500，
    但页面只会说"内部错误"——得有一个地方能直接看到 quick_check 的结果。
    """
    import sqlite3
    uri = current_app.config.get("SQLALCHEMY_DATABASE_URI") or ""
    if not uri.startswith("sqlite"):
        return ok({"engine": "mysql", "supported": False})
    path = uri.split("sqlite:///", 1)[-1]
    out = {"engine": "sqlite", "path": os.path.basename(path), "ok": False,
           "size_mb": 0.0, "quick_check": "", "tables": {}}
    try:
        out["size_mb"] = round(os.path.getsize(path) / 1024 / 1024, 2)
    except OSError:
        pass
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("PRAGMA quick_check").fetchall()
            out["quick_check"] = "; ".join(r[0] for r in rows)[:200]
            out["ok"] = out["quick_check"].lower() == "ok"
            for (name,) in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
                try:
                    out["tables"][name] = con.execute(
                        f'SELECT count(*) FROM "{name}"').fetchone()[0]
                except Exception as e:                    # noqa: BLE001
                    out["tables"][name] = f"读取失败: {type(e).__name__}"
        finally:
            con.close()
    except Exception as e:                                # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"[:200]
    return ok(out)


@bp.post("/dbwrite")
def dbwrite():
    """压测专用写探针：只写一条审计日志，隔离出「纯数据库写并发」能力。

    仅在 APP_TEST=1（压测/长稳脚本拉起的实例）时注册可用，
    正常启动即使被猜到路径也无法使用，避免成为生产环境的写入口。
    """
    if os.environ.get("APP_TEST") != "1":
        return jsonify({"code": 404, "msg": "not found", "data": None}), 404
    from app.models import AuditLog
    try:
        row = AuditLog(actor="bench", agent="bench", action="probe",
                       input_digest="concurrency-write-probe", output_digest="ok")
        db.session.add(row)
        db.session.commit()
        return ok({"id": row.id})
    except Exception as e:                                   # noqa: BLE001
        db.session.rollback()
        # 把锁冲突翻译成可识别的错误码，压测脚本据此分类
        kind = "db_locked" if "locked" in str(e).lower() else "db_error"
        return jsonify({"code": 500, "msg": f"{kind}: {str(e)[:100]}", "data": None}), 500
