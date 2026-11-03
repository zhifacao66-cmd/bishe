"""回归：模型调用的韧性（瞬态重试 / 失败留痕 / 冷加载抑制）

为什么单独立一套
----------------
线上出现过一次「模型服务暂不可用，已降级为原文摘录」。事后查审计：
同一句话 16:11:30 降级、16:12:17 重问就正常——**是瞬时故障，不是模型坏了**。
但当时系统里没有任何地方能回答"为什么失败"：进程内统计只记 ok/ms，
容器一重启连失败计数都清零了。于是三个问题同时存在：

  1. 一次失败就降级，用户看到的是"模型不可用"，实际重试一次就好；
  2. 失败不记原因，超时、连不上、502、排队超限在统计里长得一模一样；
  3. 没设 keep_alive，Ollama 空闲 5 分钟卸载权重，低频使用场景下
     几乎每次提问都是冷加载，既慢又最容易在加载期间超时。

本套守的就是这三条。断言方式：用一个可编程的假模型服务，
让它"第 N 次请求返回什么"可控，然后检查客户端的重试次数、统计内容、
请求体字段——都不需要真实模型，跑得很快。
"""
import json
import os
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# 本机地址必须绕代理，否则 requests 会把 127.0.0.1 交给系统代理
os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

# ---------------------------------------------------------------- 库隔离
DB = BASE / "data" / "smoke_resilience.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()
os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["NOTICE_AUTO_IMPORT"] = "0"


# ---------------------------------------------------------------- 可编程假模型
class Scripted(BaseHTTPRequestHandler):
    """按 script 逐次应答；script 用完后一律回成功。

    script 每项是 (状态码, 响应体)。用它模拟：502、超时、401、坏 JSON、
    以及"第一次失败第二次成功"这种线上真实形态。
    """
    script: list = []
    hits: list = []
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _reply(self, code, obj, delay=0.0):
        import time
        if delay:
            time.sleep(delay)
        body = json.dumps(obj, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                      # 客户端已超时放弃，正常现象

    def _serve(self, code, payload):
        if code == "slow":
            self._reply(200, {"message": {"role": "assistant", "content": "慢"}},
                        delay=float(payload))
            return
        if isinstance(payload, str):        # 原文返回，用来模拟网关错误页
            raw_body = payload.encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(raw_body)))
                self.end_headers()
                self.wfile.write(raw_body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        self._reply(code, payload)

    def do_GET(self):
        Scripted.hits.append(("GET", self.path, None))
        code, payload = Scripted.script.pop(0) if Scripted.script else (
            200, {"models": [{"name": "qwen3:8b"}]})
        self._serve(code, payload)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        Scripted.hits.append(("POST", self.path, body))
        code, payload = Scripted.script.pop(0) if Scripted.script else (
            200, {"message": {"role": "assistant", "content": "ok"}})
        self._serve(code, payload)

    @classmethod
    def reset(cls, script=None):
        cls.script = list(script or [])
        cls.hits = []


srv = ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
FAKE = f"http://127.0.0.1:{srv.server_address[1]}"
threading.Thread(target=srv.serve_forever, daemon=True).start()

# ---------------------------------------------------------------- 应用
from unittest import mock                              # noqa: E402

from app import create_app                             # noqa: E402
from app.llm import channel as chmod                   # noqa: E402
from app.llm import client as llm                      # noqa: E402
from app.llm.client import LLMUnavailable, chat        # noqa: E402

app = create_app("dev")

PASS = FAIL = 0
FAILED = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name}   ← {detail}")


def _patch_channel(remote: bool, timeout: float = 5.0, keep: str = "30m"):
    """把通道钉死成测试配置：remote 决定走不走重试分支"""
    real = chmod.resolve

    def fake():
        d = real()
        d.update(base_url=FAKE, remote=remote, timeout=timeout,
                 model="qwen3:8b", token="", keep_alive=keep,
                 label="测试通道", display="http://测试")
        return d

    return mock.patch.object(llm.channel, "resolve", fake)


def _stats():
    return llm.stats()


# ================================================================ 1 瞬态重试
print("=" * 66)
print("① 瞬态失败自动重试（线上那次降级的真实形态）")
print("=" * 66)

with app.app_context():
    # 第一次 502（隧道边缘节点抖动），第二次成功
    Scripted.reset([(502, "<html>bad gateway</html>")])
    with _patch_channel(remote=True):
        out = chat("测试瞬态恢复")
    check("第 1 次 502、第 2 次成功 → 调用方拿到结果", out == "ok", out)
    check("确实发了 2 次请求", len(Scripted.hits) == 2, len(Scripted.hits))
    s = _stats()
    check("重试成功只记 1 次成功、0 次失败", s["ok"] == 1 and s["fail"] == 0,
          (s["ok"], s["fail"]))
    check("重试成功后 last_error 为空", not s["last_error"]["kind"], s["last_error"])

with app.app_context():
    # 连接被直接掐断（隧道进程消失的形态）
    Scripted.reset([(503, "")])
    with _patch_channel(remote=True):
        out = chat("连接抖动")
    check("503 → 重试后仍能成功", out == "ok", out)
    check("503 同样重试了一次", len(Scripted.hits) == 2, len(Scripted.hits))

# ================================================================ 2 不该重试
print()
print("=" * 66)
print("② 不该重试的错误（重试只会让用户多等）")
print("=" * 66)

with app.app_context():
    Scripted.reset([(401, {"error": "unauthorized"})])
    with _patch_channel(remote=True):
        try:
            chat("令牌错")
            raised = False
        except LLMUnavailable:
            raised = True
    check("401 抛 LLMUnavailable", raised)
    check("401 不重试（只发 1 次）", len(Scripted.hits) == 1, len(Scripted.hits))
    s = _stats()
    check("401 记为 http_401", s["last_error"]["kind"] == "http_401",
          s["last_error"])
    check("失败计数 +1", s["fail"] == 1, s["fail"])

with app.app_context():
    # 排队超限：GPU 已过载，再塞一次只会拖慢别人。
    # 注意必须从 config 注入超时——chat() 内部会用 LLM_QUEUE_TIMEOUT 重新 configure，
    # 直接改 gate.queue_timeout 会被它覆盖回 40 秒（第一版就是这么写错的，
    # 结果测试真等了 40 秒，还把下一条断言的 last_error 污染成 queue）。
    Scripted.reset([])
    old_qto = app.config.get("LLM_QUEUE_TIMEOUT")
    app.config["LLM_QUEUE_TIMEOUT"] = 0.05
    with _patch_channel(remote=True):
        held = llm.gate._sem.acquire(timeout=0)      # 占住唯一槽位，模拟已有推理在进行
        try:
            try:
                chat("排队")
                raised = False
            except LLMUnavailable:
                raised = True
        finally:
            if held:
                llm.gate._sem.release()
            app.config["LLM_QUEUE_TIMEOUT"] = old_qto
            llm.gate.configure(1, 40)
    check("排队超时抛 LLMUnavailable", raised)
    check("排队超时不重试（一次请求都没打到模型）", len(Scripted.hits) == 0,
          len(Scripted.hits))
    s = _stats()
    check("排队超时记为 queue", s["last_error"]["kind"] == "queue", s["last_error"])

with app.app_context():
    # 返回体不是 chat 结构（比如网关自己的错误页）。
    # 必须连续两次都坏：只坏一次的话重试会拿到正常响应并成功，
    # 那测的就不是"坏返回体如何失败"，而是"重试如何救命"了。
    Scripted.reset([(200, "<html>oops</html>"), (200, "<html>oops</html>")])
    with _patch_channel(remote=True):
        try:
            chat("坏返回体")
            raised = False
        except LLMUnavailable:
            raised = True
    check("返回体不是 JSON → 抛 LLMUnavailable", raised)
    check("坏返回体重试一次（共 2 次）", len(Scripted.hits) == 2, len(Scripted.hits))
    check("记为 bad_response", _stats()["last_error"]["kind"] == "bad_response",
          _stats()["last_error"])

# ================================================================ 3 僵死槽位
print()
print("=" * 66)
print("②-2 僵死槽位回收（一次网络卡顿不能让整个模型通道停摆）")
print("=" * 66)

import time as _time                                      # noqa: E402


def _fake_hold(g, age: float):
    """模拟一个卡在网络层、还没交还槽位的调用"""
    g._sem.acquire(timeout=0)
    with g._stat_lock:
        g._seq += 1
        tok = g._seq
        g._active[tok] = _time.monotonic() - age
        g.inflight += 1
    return tok


def _hard_reset(g):
    """把闸门恢复成干净状态。

    不能只调 reset()：它清统计但**不动信号量**。测试里 fake 出来的僵死占位
    如果没被 reclaim，sem 会一直是 0，后面所有真实调用都卡在 acquire 上——
    第一版就因为漏了这步，整套测试挂到超时被 kill。
    """
    import threading as _th
    g.reset()
    g._sem = _th.BoundedSemaphore(1)
    g.limit = 1
    g.configure(1, 40)


with app.app_context():
    g = llm.gate
    _hard_reset(g)
    g.configure(1, 40, max_hold=5)
    _fake_hold(g, age=60)                       # 已经卡了 60 秒
    check("僵死时 inflight=1", g.snapshot()["inflight"] == 1, g.snapshot())
    check("能算出持有年龄", g.snapshot()["hold_age_s"] >= 55,
          g.snapshot()["hold_age_s"])
    n = g.reclaim_stale()
    check("回收了 1 个僵死槽位", n == 1, n)
    check("回收后 inflight 归零", g.snapshot()["inflight"] == 0, g.snapshot())
    check("回收计数被记录", g.snapshot()["stale"] == 1, g.snapshot())
    # 额度确实回来了：新请求能立刻拿到槽位，不必排队
    t0 = _time.perf_counter()
    with g.slot():
        pass
    check("回收后新请求无需排队", _time.perf_counter() - t0 < 0.5,
          _time.perf_counter() - t0)
    _hard_reset(g)

with app.app_context():
    g = llm.gate
    _hard_reset(g)
    g.configure(1, 40, max_hold=300)
    _fake_hold(g, age=1)                        # 才 1 秒，属于正常推理
    check("未超时的不被误杀", g.reclaim_stale() == 0, "正常请求被回收了")
    check("未超时时 inflight 保持 1", g.snapshot()["inflight"] == 1, g.snapshot())
    _hard_reset(g)

# ================================================================ 4 本机不重试
print()
print("=" * 66)
print("③ 本机通道不重试（连不上就是没启动，重试只是白等）")
print("=" * 66)
with app.app_context():
    Scripted.reset([(502, "x")])
    with _patch_channel(remote=False):
        try:
            chat("本机失败")
            raised = False
        except LLMUnavailable:
            raised = True
    check("本机失败抛 LLMUnavailable", raised)
    check("本机只发 1 次，不重试", len(Scripted.hits) == 1, len(Scripted.hits))

# ================================================================ 4 失败留痕
print()
print("=" * 66)
print("④ 失败必须留下可读原因（排障不靠猜）")
print("=" * 66)

with app.app_context():
    Scripted.reset([(401, {"error": "x"})])
    with _patch_channel(remote=True):
        try:
            chat("留痕")
        except LLMUnavailable:
            pass
    s = _stats()
    rec = s["recent"][0]
    check("最近记录带 err 字段", rec.get("err") == "http_401", rec)
    check("最近记录带 detail", bool(rec.get("detail")), rec)
    check("last_error 带时间", bool(s["last_error"]["at"]), s["last_error"])
    check("last_error 带模型名", s["last_error"]["model"] == "qwen3:8b",
          s["last_error"])
    check("last_error 不记 prompt 正文",
          "留痕" not in json.dumps(s, ensure_ascii=False), "")

with app.app_context():
    d = llm.describe()
    check("自检信息含模型驻留时长", bool(d.get("keep_alive")), d)
    check("自检信息不回显令牌", "token" not in d or d.get("token_set") in (True, False))

# ================================================================ 4.5 探活归因
print()
print("=" * 66)
print("④-2 探活失败也要说原因（健康页不能只显示在线或离线）")
print("=" * 66)

with app.app_context():
    Scripted.reset([])
    with _patch_channel(remote=False):
        pr = llm.probe()
    check("探活成功带耗时", pr["ok"] and pr["ms"] >= 0, pr)
    check("探活成功时 error 为空", pr["error"] == "", pr)
    check("探活结果带通道类型", "remote" in pr, pr)

with app.app_context():
    # 令牌错：必须能区分出"连上了但被拒"，而不是笼统的"不可用"
    Scripted.reset([(401, {"error": "bad token"})])
    with _patch_channel(remote=False):
        pr = llm.probe()
    check("401 探活返回 ok=False", pr["ok"] is False, pr)
    check("401 探活归因为 http_401", pr["error"] == "http_401", pr)
    check("401 探活给出可读提示", "令牌" in pr.get("detail", ""), pr.get("detail"))

with app.app_context():
    # 端口无人监听：connect 类错误，与 401 必须区分开。
    # 注意先把真实 resolve 存下来——patch 之后 llm.channel.resolve 就是假函数，
    # 在假函数里再调用 resolve() 会直接递归爆栈（第一版就这么写的）。
    dead = "http://127.0.0.1:9"
    real_resolve = chmod.resolve

    def _dead_channel():
        d = real_resolve()
        d.update(base_url=dead, remote=False, timeout=1)
        return d

    with mock.patch.object(llm.channel, "resolve", _dead_channel):
        pr = llm.probe()
    check("死端口探活 ok=False", pr["ok"] is False, pr)
    check("死端口归因为 connect", pr["error"] == "connect", pr)
    check("死端口耗时被记录", pr["ms"] >= 0, pr["ms"])

with app.app_context():
    # 本机通道不出网，自检要说"跳过"而不是报错
    net = llm.probe_net()
    check("本机通道出网自检直接跳过", bool(net.get("skipped")), net)

# ================================================================ 4.6 代理
print()
print("=" * 66)
print("④-3 代理只认显式配置（平台自有变量不能猜语义）")
print("=" * 66)

with app.app_context():
    os.environ["LLM_PROXY"] = "http://10.0.0.9:3128"
    try:
        check("能读出显式配置的代理",
              llm._proxy_url()[0] == "http://10.0.0.9:3128", llm._proxy_url())
        check("记录代理来源", llm._proxy_url()[1] == "LLM_PROXY", llm._proxy_url())
        check("远程通道会带上代理",
              llm._proxies({"remote": True}) ==
              {"http": "http://10.0.0.9:3128", "https": "http://10.0.0.9:3128"},
              llm._proxies({"remote": True}))
        check("本机通道绝不走代理", llm._proxies({"remote": False}) is None,
              llm._proxies({"remote": False}))
    finally:
        os.environ.pop("LLM_PROXY", None)
    check("没配代理时返回空", llm._proxy_url()[0] == "", llm._proxy_url())

with app.app_context():
    # 这条是踩过的坑：平台注入了 SPACE_PROXY_ENDPOINT / X_IDE_AUTH_PROXY，
    # 名字像代理，值其实是内部端点（裸 IP、无端口）。把它当代理用，
    # requests 直接 ProxyError，把本来能通的直连也弄坏了。
    os.environ["SPACE_PROXY_ENDPOINT"] = "172.30.1.89"
    os.environ["X_IDE_AUTH_PROXY"] = "auth.proxy"
    try:
        check("平台自有变量不被当作代理", llm._proxy_url()[0] == "", llm._proxy_url())
        check("此时远程请求不挂任何代理", llm._proxies({"remote": True}) is None,
              llm._proxies({"remote": True}))
    finally:
        os.environ.pop("SPACE_PROXY_ENDPOINT", None)
        os.environ.pop("X_IDE_AUTH_PROXY", None)

with app.app_context():
    # 线上到公网的 TLS 握手实测 9 秒。探活超时给 6 秒 → 永远误报"降级"，
    # 而实际推理是好的。探活误报比不报更伤人，会让人去修不存在的故障。
    with _patch_channel(remote=True):
        pr = llm.probe()
    check("远程探活超时 ≥20s（TLS 握手就要 9s）", pr["timeout_s"] >= 20, pr["timeout_s"])
    with _patch_channel(remote=False):
        pr2 = llm.probe()
    check("本机探活超时仍很短", pr2["timeout_s"] <= 5, pr2["timeout_s"])

# ================================================================ 5 请求体
print()
print("=" * 66)
print("⑤ 请求体：keep_alive 必须发出（抑制冷加载）")
print("=" * 66)

with app.app_context():
    Scripted.reset([])
    with _patch_channel(remote=False, keep="30m"):
        chat("保活")
    body = Scripted.hits[0][2]
    check("请求体带 keep_alive", body.get("keep_alive") == "30m", body.get("keep_alive"))
    check("keep_alive 不是 Ollama 默认的空值", body.get("keep_alive") not in (None, "", 0))
    check("回归：stream=False 仍在", body.get("stream") is False)
    check("回归：think 显式关闭", body.get("think") is False)
    check("回归：options 里有 num_ctx", "num_ctx" in (body.get("options") or {}))

with app.app_context():
    Scripted.reset([])
    with _patch_channel(remote=False, keep="5m"):
        chat("自定义保活")
    check("keep_alive 可配置", Scripted.hits[0][2].get("keep_alive") == "5m",
          Scripted.hits[0][2].get("keep_alive"))

# ================================================================ 6 通道配置
print()
print("=" * 66)
print("⑥ 通道配置：keep_alive 的默认值与优先级")
print("=" * 66)

with app.app_context():
    real = chmod.resolve
    d = real()
    check("解析结果包含 keep_alive 键", "keep_alive" in d, list(d))
    check("keep_alive 有值", bool(d["keep_alive"]), d.get("keep_alive"))
    os.environ["LLM_KEEP_ALIVE"] = "2h"
    try:
        d2 = real()
        check("环境变量 LLM_KEEP_ALIVE 可覆盖", d2["keep_alive"] == "2h", d2["keep_alive"])
    finally:
        os.environ.pop("LLM_KEEP_ALIVE", None)
    check("环境变量移除后回到原值", real()["keep_alive"] == d["keep_alive"])

# ================================================================ 7 知识库降级
print()
print("=" * 66)
print("⑦ 知识库降级：文案不误导、原因可回传")
print("=" * 66)

from app.agents.knowledge import KnowledgeAgent       # noqa: E402
from app.kb.store import get_index                     # noqa: E402
from app.kb.seed import init_kb_docs                   # noqa: E402

init_kb_docs(app)
with app.app_context():
    get_index()

with app.app_context():
    # 让 chat 必然失败，观察降级输出
    with mock.patch("app.agents.knowledge.chat",
                    side_effect=LLMUnavailable("远程模型通道不可用: connect refused")):
        r = KnowledgeAgent().answer("高龄津贴怎么申领")
    check("降级时 llm_used=False", r["llm_used"] is False)
    check("降级提示用中性措辞（不写死为某一通道）",
          "模型服务暂不可用" in r["answer"], r["answer"][:40])
    check("降级明确要求人工核对", "人工核对" in r["answer"])
    check("降级回传失败原因", "connect" in r.get("llm_error", ""), r.get("llm_error"))
    check("降级仍返回检索片段", len(r["hits"]) > 0, len(r["hits"]))
    check("降级答案含原文而非编造", "高龄" in r["answer"])

with app.app_context():
    # 恢复成功后不能残留错误
    with mock.patch("app.agents.knowledge.chat", return_value="依据资料：……"):
        r2 = KnowledgeAgent().answer("高龄津贴怎么申领")
    check("成功时 llm_used=True", r2["llm_used"] is True)
    check("成功时 llm_error 为空", r2.get("llm_error") == "", r2.get("llm_error"))

# ================================================================ 8 健康接口
print()
print("=" * 66)
print("⑧ 健康接口暴露失败信息（线上取证靠它）")
print("=" * 66)

with app.test_client() as c:
    # 详情要登录态：匿名只给存活状态（通道地址/统计属于内部运维信息）
    c.post("/login", data={"username": "admin", "password": "admin123"},
           follow_redirects=False)
    j = c.get("/api/health").get_json()["data"]
    ll = j["llm"]
    check("llm.stats 含 last_error", "last_error" in ll["stats"], list(ll["stats"]))
    check("llm.gate 含拒绝计数", "rejected" in ll["gate"], list(ll["gate"]))
    check("llm.channel 含 keep_alive", "keep_alive" in ll["channel"],
          list(ll["channel"]))
    check("llm 含探活详情 probe", "probe" in ll, list(ll))
    check("probe 带错误分类字段", "error" in (ll.get("probe") or {}),
          ll.get("probe"))
    check("健康接口不泄露完整隧道地址",
          "trycloudflare" not in json.dumps(ll, ensure_ascii=False), "")

with app.test_client() as c:
    check("出网自检未登录 → 401", c.get("/api/health/net").status_code == 401)
    c.post("/login", data={"username": "admin", "password": "admin123"},
           follow_redirects=False)
    r = c.get("/api/health/net")
    check("管理员可调用出网自检", r.status_code == 200, r.status_code)
    check("出网自检不泄露代理地址原文",
          "http://" not in json.dumps(r.get_json() or {}, ensure_ascii=False), "")

# ---------------------------------------------------------------- 收尾
# 收尾清库：Windows 下 sqlite 连接未完全释放时删不掉，这是环境问题不是测试失败，
# 不能让它盖掉真正的断言结果（曾经因为收尾抛错，看不出到底过了几项）。
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    try:
        if p.exists():
            p.unlink()
    except OSError:
        pass

print()
print("=" * 66)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败项：")
    for x in FAILED:
        print("  -", x)
print("=" * 66)
sys.exit(1 if FAIL else 0)
