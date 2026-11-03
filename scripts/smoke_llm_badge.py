"""回归：侧栏模型状态灯必须是真的

起因是有人盯着左下角问了一句"这真的是连上了吗，怎么一直是这样的"。
答案是**没有连**——那行是写死的 HTML，绿点和"本地大模型运行中"跟模型
毫无关系，把 Ollama 卸了照样绿。这类"假指示灯"比没有更危险：
模型降级时它替模型报平安，值班的人会先去怀疑自己操作错了。

这套断言钉住 5 件事：
  ① 未登录拿不到（通道类型/模型名/失败原因不对外）；
  ② 模型不通时必须 bad，页面里**不能**出现"本地大模型运行中"；
  ③ 模型通了才是 ok，且本机通道说"数据不出内网"；
  ④ 走隧道时改说"加密隧道回本机 GPU"——那句话是要被评委追问的，
     不能在本机直连和跨公网回源两种形态下都说同一句；
  ⑤ 轮询接口和页面渲染都不许同步探活（远程通道 TLS 握手 9~26s，
     放进渲染路径就是每个页面卡半分钟）。

模型不通的用例指向 127.0.0.1:9；通的用例才读 _llm_cache，
所以整套不依赖 GPU，也不依赖本机有没有装模型。
"""
import os
import pathlib
import sys
import time

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_llm_badge.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["NOTICE_AUTO_IMPORT"] = "0"
# 故意指一个必然连不上的端口：本套要测的就是"不通时灯会不会红"
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
os.environ.pop("LLM_BASE_URL", None)
os.environ.pop("LLM_TOKEN", None)

from app import create_app                                      # noqa: E402
from app import auth as auth_mod                                # noqa: E402
from app.api import health as health_mod                        # noqa: E402

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
        print(f"  ✗ {name}" + (f"  [{detail}]" if detail else ""))


def login(c, u="admin", p="admin123"):
    auth_mod._LOGIN_FAIL.clear()
    c.get("/logout")
    return c.post("/login", data={"username": u, "password": p},
                  follow_redirects=False)


def wait_ok(c, timeout=8.0):
    """等后台探活落地（状态灯设计成异步，所以测试也得等）"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        d = c.get("/api/health/llm").get_json()["data"]
        if d["state"] != "idle":
            return d
        time.sleep(0.15)
    return c.get("/api/health/llm").get_json()["data"]


def badge_html(c):
    html = c.get("/").get_data(as_text=True)
    i = html.find('id="llmBadge"')
    return html[max(0, i - 200):i + 600] if i >= 0 else ""


def set_cache(state, ms=12, err=""):
    """直接改探活缓存：避免测试真的去打网络，也不受本机有无模型影响"""
    health_mod._llm_cache["info"] = {"ok": state == "ok", "ms": ms, "error": err,
                                     "detail": "" if state == "ok" else "模拟失败"}
    health_mod._llm_cache["ts"] = time.time()


# ---------------------------------------------------------------- 1 鉴权
print("\n[1] 状态接口只给登录用户")
with app.test_client() as c:
    r = c.get("/api/health/llm")
    check("未登录 401", r.status_code == 401, r.status_code)
    check("未登录不回泄漏通道信息",
          "qwen" not in (r.get_data(as_text=True) or ""))

# ---------------------------------------------------------------- 2 不通必红
print("\n[2] 模型连不上时必须是红的")
with app.test_client() as c:
    login(c)
    d = wait_ok(c)
    check("状态是 bad", d["state"] == "bad", d)
    check("文案说降级", "降级" in d["title"], d["title"])
    check("说明走规则路径", "规则" in d["sub"], d["sub"])
    check("带上失败原因", bool(d["error"]), d)
    html = badge_html(c)
    check("页面里 dot 带 bad 类", 'class="dot bad"' in html, html[:160])
    check("页面**不再**写死「本地大模型运行中」", "本地大模型运行中" not in html)
    check("data-state 与状态一致", 'data-state="bad"' in html, html[:160])

# ---------------------------------------------------------------- 3 通了才绿
print("\n[3] 探活通过才是绿的")
with app.test_client() as c:
    login(c)
    set_cache("ok")
    d = c.get("/api/health/llm").get_json()["data"]
    check("状态是 ok", d["state"] == "ok", d)
    check("文案是运行中", d["title"] == "本地大模型运行中", d["title"])
    check("本机通道承诺数据不出内网", "数据不出内网" in d["sub"], d["sub"])
    html = badge_html(c)
    check("首屏渲染就是绿的（不是等 JS 才绿）",
          'class="dot ok"' in html and "本地大模型运行中" in html, html[:160])

# ---------------------------------------------------------------- 4 隧道口径
print("\n[4] 走隧道时不能再说「数据不出内网」")
with app.test_client() as c:
    login(c)
    # 换地址必须改 env：channel.resolve() 的优先级是 env > llm.json > config，
    # 只改 config 会被前面设过的 OLLAMA_BASE_URL 盖掉（第一版就栽在这）
    saved = os.environ.get("OLLAMA_BASE_URL")
    os.environ["OLLAMA_BASE_URL"] = "https://demo.trycloudflare.example"
    os.environ["LLM_TOKEN"] = "test-token"
    try:
        set_cache("ok")
        d = c.get("/api/health/llm").get_json()["data"]
        check("识别为远程通道", d["remote"] is True, d)
        check("改说加密隧道回本机", "隧道" in d["sub"], d["sub"])
        check("远程不承诺数据不出内网", "数据不出内网" not in d["sub"], d["sub"])
    finally:
        os.environ["OLLAMA_BASE_URL"] = saved or "http://127.0.0.1:9"
        set_cache("bad", err="connect")

# ---------------------------------------------------------------- 5 不许阻塞
print("\n[5] 轮询与渲染都不许同步探活")
with app.test_client() as c:
    login(c)
    # 让缓存过期，并指向一个"黑洞"地址：同步探活会卡到超时
    health_mod._llm_cache["ts"] = 0.0
    app.config["OLLAMA_BASE_URL"] = "http://10.255.255.1:11434"
    t0 = time.perf_counter()
    d = c.get("/api/health/llm").get_json()["data"]
    api_ms = (time.perf_counter() - t0) * 1000
    check(f"接口只读缓存（{api_ms:.0f}ms < 300ms）", api_ms < 300, f"{api_ms:.0f}ms")
    check("缓存未就绪时是 idle 而不是假装 ok", d["state"] == "idle", d)

    t0 = time.perf_counter()
    c.get("/")
    page_ms = (time.perf_counter() - t0) * 1000
    check(f"页面渲染不被探活拖住（{page_ms:.0f}ms < 800ms）", page_ms < 800,
          f"{page_ms:.0f}ms")
    app.config["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"

print("\n" + "=" * 66)
print(f"通过 {PASS} 项 / 失败 {FAIL} 项")
if FAILED:
    print("失败项：")
    for x in FAILED:
        print("  -", x)
print("=" * 66)

for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass

sys.exit(1 if FAIL else 0)
