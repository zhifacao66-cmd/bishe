"""回归：模型通道（本机直连 / 远程隧道 / 令牌网关 / 降级）

只守一件事：**"线上到底有没有在跑模型"这句话，必须能从系统里验证出来。**

为什么单独立一套：
部署环境没有显卡，模型要靠隧道回到开发者本机。这条链路有三处最容易坏，
而且坏了都不报错——只是安静地降级成规则引擎，页面一切正常：
  1. 远程地址必须走系统代理（沙箱只能经代理出网），本机地址必须绕代理
     （否则 requests 把 127.0.0.1 交给代理，回 404）；
  2. 隧道对端必须校验令牌，否则等于把显卡裸奔到公网；
  3. 冷启动要等权重加载，超时值设小了第一次调用必失败。
本套覆盖：
  1. 通道裁决：本机/远程判定、掩码展示、配置来源优先级、远程超时下限
  2. 调用：请求体字段（model/format/think/options）与令牌头确实发出
  3. 网关：无令牌 401、错令牌 401、白名单外 404、正确令牌转发成功
  4. 降级：不可达时抛 LLMUnavailable，并计入失败统计（业务层据此走规则）
  5. 观测：/api/health 暴露通道信息且不回显令牌；/api/health/llm-test 权限与结果
  6. 页面：/system 仅管理员可见
"""
import http.client
import json
import os
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_llm.db"
for suf in ("", "-wal", "-shm"):
    p = pathlib.Path(str(DB) + suf)
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------- 假 Ollama
class FakeOllama(BaseHTTPRequestHandler):
    """记录收到的每个请求，用来断言请求体与令牌头"""

    seen = []
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        FakeOllama.seen.append({"path": self.path, "body": None,
                                "token": self.headers.get("X-LLM-Token")})
        self._send({"models": [{"name": "qwen3:8b"}]})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        FakeOllama.seen.append({"path": self.path, "body": body,
                                "token": self.headers.get("X-LLM-Token")})
        self._send({"message": {"role": "assistant", "content": "楼道声控灯维修"}})


fake_srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
FAKE = f"http://127.0.0.1:{fake_srv.server_address[1]}"
threading.Thread(target=fake_srv.serve_forever, daemon=True).start()

# ---------------------------------------------------------------- 网关
sys.path.insert(0, str(BASE / "scripts"))
import llm_gateway as gw                                  # noqa: E402

GW_TOKEN = "test-token-abcdefghijklmnop"
gw.Handler.token = GW_TOKEN
gw.Handler.upstream_host = "127.0.0.1"
gw.Handler.upstream_port = fake_srv.server_address[1]
gw_srv = ThreadingHTTPServer(("127.0.0.1", 0), gw.Handler)
GW = f"http://127.0.0.1:{gw_srv.server_address[1]}"
threading.Thread(target=gw_srv.serve_forever, daemon=True).start()


def raw_post(base, path, payload, token=None):
    """裸 HTTP 调用：绕过 requests，免得系统代理把 127.0.0.1 也接管了"""
    u = base.split("://", 1)[1]
    host, port = u.split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=10)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-LLM-Token"] = token
    c.request("POST", path, body=json.dumps(payload).encode(), headers=headers)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, data


# ---------------------------------------------------------------- 应用
os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["LLM_BASE_URL"] = FAKE          # 本机通道，指向假 Ollama
os.environ["NOTICE_AUTO_IMPORT"] = "0"
os.environ.pop("LLM_TOKEN", None)

from app import create_app                                # noqa: E402
from app.extensions import db                             # noqa: E402
from app.llm import channel, client                       # noqa: E402

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


def login(c, u, p):
    return c.post("/login", data={"username": u, "password": p}, follow_redirects=False)


print("=" * 66)
print("模型通道回归")
print("=" * 66)

# ---------------------------------------------------------------- 1. 通道裁决
print("\n[1] 通道裁决")
with app.app_context():
    ch = channel.resolve()
    check("本机地址判定为 local", ch["remote"] is False, ch)
    check("配置来源为环境变量", ch["source"] == "env", ch["source"])
    check("本机地址原样展示", ch["display"] == FAKE, ch["display"])
    check("本机通道超时取配置值", ch["timeout"] == app.config["LLM_TIMEOUT"], ch["timeout"])

    os.environ["LLM_BASE_URL"] = "https://demo-llm.trycloudflare.com"
    r = channel.resolve()
    check("公网地址判定为 remote", r["remote"] is True, r)
    check("远程超时抬到 ≥90s（冷启动要加载权重）", r["timeout"] >= 90, r["timeout"])
    check("隧道子域做掩码", r["display"].startswith("https://demo…"),
          r["display"])
    check("掩码不含完整子域", "trycloudflare" in r["display"] and "demo-llm." not in r["display"],
          r["display"])

    # 掩码不能把本地地址也打码——本地地址本来就没必要藏
    check("本地地址不打码", channel.mask_url("http://127.0.0.1:11434") == "http://127.0.0.1:11434",
          channel.mask_url("http://127.0.0.1:11434"))

    os.environ["LLM_BASE_URL"] = FAKE
    check("环境变量切回后立即生效", channel.resolve()["remote"] is False)

# ---------------------------------------------------------------- 2. 调用
print("\n[2] 调用与请求体")
FakeOllama.seen.clear()
with app.app_context():
    out = client.chat("把这句话压缩成工单标题", system="只输出标题",
                      schema={"type": "object", "properties": {"t": {"type": "string"}}},
                      think=False)
    check("返回模型输出", out == "楼道声控灯维修", out)

    req = FakeOllama.seen[-1]
    check("打到 /api/chat", req["path"] == "/api/chat", req["path"])
    body = req["body"]
    check("带 model", body.get("model") == app.config["LLM_MODEL"], body.get("model"))
    check("带 system 消息", body["messages"][0]["role"] == "system", body["messages"][0])
    check("结构化输出传了 schema", isinstance(body.get("format"), dict), body.get("format"))
    check("think 显式关闭", body.get("think") is False, body.get("think"))
    check("带 num_ctx", "num_ctx" in body.get("options", {}), body.get("options"))
    check("本机通道不带令牌头", req["token"] is None, req["token"])

    desc = client.describe()
    check("describe 报 local", desc["channel"] == "local", desc)
    check("describe 不回显令牌", "token" not in json.dumps(desc).lower()
          or desc["token_set"] is False, desc)

# ---------------------------------------------------------------- 3. 网关
print("\n[3] 令牌网关")
st, _ = raw_post(GW, "/api/chat", {"model": "qwen3:8b", "messages": []})
check("无令牌 → 401", st == 401, st)
st, _ = raw_post(GW, "/api/chat", {"model": "qwen3:8b", "messages": []}, token="wrong")
check("错令牌 → 401", st == 401, st)
st, _ = raw_post(GW, "/api/v1/chat/completions", {"model": "x"}, token=GW_TOKEN)
check("白名单外路径 → 404", st == 404, st)

FakeOllama.seen.clear()
st, data = raw_post(GW, "/api/chat",
                    {"model": "qwen3:8b", "messages": [{"role": "user", "content": "hi"}]},
                    token=GW_TOKEN)
check("正确令牌 → 200", st == 200, st)
check("网关透传了上游响应", "楼道声控灯维修".encode() in data, data[:80])
check("网关确实转发到上游", len(FakeOllama.seen) == 1, len(FakeOllama.seen))

# 应用侧经网关调用：证明令牌链路端到端通
os.environ["LLM_BASE_URL"] = GW
os.environ["LLM_TOKEN"] = GW_TOKEN
FakeOllama.seen.clear()
with app.app_context():
    out = client.chat("经网关调用")
    check("经网关调用成功", out == "楼道声控灯维修", out)
    check("网关消费令牌后不再透传给上游（凭据只在本机一跳内有效）",
          FakeOllama.seen[-1]["token"] is None, FakeOllama.seen[-1]["token"])
    d = client.describe()
    check("配置了令牌时 token_set=true", d["token_set"] is True, d)

# ---------------------------------------------------------------- 4. 降级
print("\n[4] 不可达时降级")
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9"
before = client.stats()["fail"]
with app.app_context():
    raised = False
    try:
        client.chat("这次一定失败")
    except client.LLMUnavailable:
        raised = True
    check("抛 LLMUnavailable（业务层据此走规则）", raised)
    check("失败计数 +1", client.stats()["fail"] == before + 1, client.stats()["fail"])
    check("失败也进最近记录", client.stats()["recent"][0]["ok"] is False,
          client.stats()["recent"][0])
    check("不可达时 is_available=false", client.is_available() is False)

# ---------------------------------------------------------------- 5. 接口
print("\n[5] 观测接口与权限")
os.environ["LLM_BASE_URL"] = FAKE
c = app.test_client()
# 匿名只给存活状态：通道地址 / 令牌是否配置 / 进程号属于攻击面情报，
# 不该对公网匿名开放。详情要登录后才给（见 app/api/health.py 的分档）
anon = c.get("/api/health").get_json()
check("健康接口匿名可访问（存活探针要用）", anon["code"] == 0, anon)
check("匿名不返回通道地址", "channel" not in (anon["data"].get("llm") or {}),
      anon["data"].get("llm"))
check("匿名不返回进程号", "pid" not in anon["data"], list(anon["data"]))
check("匿名仍能看出模型在线与否", "available" in anon["data"]["llm"],
      anon["data"]["llm"])

login(c, "wang", "123456")
j = c.get("/api/health").get_json()
check("登录后返回完整健康数据", "llm" in j["data"] and "stats" in j["data"]["llm"],
      list(j["data"]))
ch = j["data"]["llm"]["channel"]
check("健康接口暴露通道类型", ch["channel"] == "local", ch)
check("健康接口暴露模型", j["data"]["llm"]["model"] == app.config["LLM_MODEL"],
      j["data"]["llm"]["model"])
check("健康接口含调用统计", "calls" in j["data"]["llm"]["stats"], j["data"]["llm"]["stats"])
check("健康接口不泄露令牌原文", GW_TOKEN not in json.dumps(j), "响应里出现了令牌")
c.get("/logout")

r = c.post("/api/health/llm-test")
check("未登录自检 → 401", r.status_code == 401, r.status_code)

login(c, "wang", "123456")
r = c.post("/api/health/llm-test")
check("社区账号自检 → 403", r.status_code == 403, r.status_code)

c2 = app.test_client()
login(c2, "admin", "admin123")
r = c2.post("/api/health/llm-test")
check("管理员自检 → 200", r.status_code == 200, r.status_code)
d = (r.get_json() or {}).get("data") or {}
check("自检真的拿到了模型输出", d.get("ok") is True and d.get("output"),
      d)
check("自检返回耗时", isinstance(d.get("elapsed_ms"), int), d.get("elapsed_ms"))

# ---------------------------------------------------------------- 6. 页面
print("\n[6] 系统状态页")
check("管理员可打开 /system", c2.get("/system").status_code == 200)
check("社区账号 /system → 403", c.get("/system").status_code == 403)
check("管理员侧边栏有入口", "系统状态" in c2.get("/").get_data(as_text=True))
check("社区账号侧边栏无入口", "系统状态" not in c.get("/").get_data(as_text=True))

# ---------------------------------------------------------------- 收尾
fake_srv.shutdown()
gw_srv.shutdown()

print("\n" + "=" * 66)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
if FAILED:
    print("失败项：")
    for x in FAILED:
        print("  -", x)
print("=" * 66)
sys.exit(1 if FAIL else 0)
