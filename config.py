import os
import secrets
import sys

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# 公开的默认密钥，只允许在开发环境使用。
# Flask 的 session 是「签名 cookie」（uid/role 都存在客户端），
# 密钥一旦是可猜的常量，任何拿到源码的人都能离线签一个 role=street 的 cookie 直接登进来，
# 等于完全绕过登录。
#
# 这里只放「候选值」，真正的裁决在 create_app 里做（见 resolve_secret_key）：
# 如果写成类属性 _secret_key()，密钥会在模块导入那一刻定死，
# 之后再改环境变量、再 create_app 都不会生效，测试和多实例场景都会失灵。
_INSECURE_DEFAULT = "dev-secret-key-change-me"


def resolve_secret_key(debug: bool) -> str:
    """生产环境没有显式配置 SECRET_KEY 时的兜底：用实例级密钥文件，绝不回落公开默认值

    为什么落到文件而不是每次随机：随机密钥会让**每次重启都把所有登录踢下线**，
    演示时表现为"打开就掉线"。文件只在首次生成，之后一直复用，
    既是私有的，又能跨重启保持一致。
    """
    if debug:
        return _INSECURE_DEFAULT

    path = os.path.join(BASE_DIR, ".secret_key")
    try:
        if os.path.exists(path):
            k = open(path, encoding="utf-8").read().strip()
            if k:
                return k
        k = secrets.token_urlsafe(48)
        # 仅属主可读（Windows 上 chmod 语义有限，但不影响功能）
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(k)
        print(f"[config] 未设置 SECRET_KEY，已生成实例密钥 {path}（请勿提交到版本库）",
              file=sys.stderr, flush=True)
        return k
    except Exception as e:
        # 磁盘只读等极端情况：退回进程级随机密钥，保证能起来（多进程会互不认）
        k = secrets.token_urlsafe(48)
        print(f"[config] 密钥文件不可用（{type(e).__name__}），改用进程级随机密钥；"
              "重启后登录状态将失效", file=sys.stderr, flush=True)
        return k


class Config:
    # 留空：密钥在 create_app 里按「环境变量 → 开发默认 / 生产随机」的顺序现算。
    # 不在这里读 os.environ，是因为类属性在模块导入那一刻就固定了，
    # 之后再改环境变量（测试、多实例、命令行覆盖）都不会生效。
    SECRET_KEY = None
    # 会话 Cookie 加固：HttpOnly 挡 XSS 偷 cookie；SameSite=Lax 让跨站 POST 不带 cookie，
    # 这是本系统在不引入 CSRF token 的前提下防 CSRF 的主要手段（详见 docs/04 已知不足）。
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
    # 反代后浏览器侧是 https 才能开；沙箱内是 http 回源，默认关，由环境变量控制
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"

    # 是否信任最近一层反向代理写入的 X-Forwarded-*。
    # 本机直连=0；ECS/沙箱在 nginx/网关后面时设 1，由 ProxyFix 只取最近一段，
    # 客户端自己塞的左侧段会被丢弃。**不要**在代码里直接读 XFF 第一段当 IP：
    # 那是客户端可伪造的，限流与取证都会失真。
    TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"

    # 登录失败限流：**账号维度**是主维度（IP 在代理后面不可靠，见 auth.py 注释）
    LOGIN_MAX_FAIL = int(os.environ.get("LOGIN_MAX_FAIL", "5"))
    LOGIN_IP_MAX_FAIL = int(os.environ.get("LOGIN_IP_MAX_FAIL", "30"))
    LOGIN_FAIL_WINDOW = int(os.environ.get("LOGIN_FAIL_WINDOW", "300"))

    # 上传体积上限。Flask 默认**不限**，而居民端是公网匿名入口——
    # 不设这条，一个 500MB 的 POST 就能把 2C2G 的内存吃干，
    # 而且 Flask 会先把整个 body 读进内存再交给视图，拦不住。
    # 5 张 × 5MB 再加表单开销，取 28MB。
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_MB", "28")) * 1024 * 1024

    # 证据附件落盘目录。跟着数据库放在 data/ 下，备份脚本一并带走；
    # 不放 app/static/ ——放那里等于把居民照片变成可直接访问的静态资源。
    EVIDENCE_DIR = os.environ.get(
        "EVIDENCE_DIR", os.path.join(BASE_DIR, "data", "evidence"))

    # 开发与演示默认 SQLite（随项目走，零配置，无需任何数据库进程）；
    # 生产切 MySQL 时，把 DATABASE_URL 设为 SQLAlchemy 的 MySQL URI 并另装驱动。
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "data", "community.db")
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # 本地大模型（Ollama）
    OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    # 显存 8GB：默认 qwen3:8b（num_ctx 压到 4096）；若爆显存可切 qwen3:4b
    LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3:8b")
    LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "60"))
    LLM_THINK = os.environ.get("LLM_THINK", "0") == "1"   # Qwen3 思考链，默认关闭
    LLM_NUM_CTX = int(os.environ.get("LLM_NUM_CTX", "4096"))
    LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.2"))

    # ---------------- 并发与稳定性 ----------------
    # SQLite：WAL 让读写不互斥；busy_timeout 让写-写冲突退避重试（默认 5s 偏短）
    SQLITE_WAL = os.environ.get("SQLITE_WAL", "1") == "1"
    SQLITE_BUSY_TIMEOUT = int(os.environ.get("SQLITE_BUSY_TIMEOUT", "20000"))
    # 本地模型并发闸门：单卡显存下模型串行推理，并发请求必须排队而不是一起挤爆显存。
    # 1 = 严格串行（8GB 显存推荐）；显存充裕可调 2。
    LLM_MAX_CONCURRENCY = int(os.environ.get("LLM_MAX_CONCURRENCY", "1"))
    # 排队等待上限：超过则降级为规则路径，宁可质量降级也不让用户等到 HTTP 超时
    LLM_QUEUE_TIMEOUT = int(os.environ.get("LLM_QUEUE_TIMEOUT", "40"))

    # RAG 知识库
    KB_DIR = os.path.join(BASE_DIR, "data", "kb")          # 放入 .md/.txt 即自动入库
    # 检索返回的条款数。注意：这一行曾被误并进上一行注释里，导致环境变量 KB_TOP_K
    # 静默失效（kb.py 用 .get(..., 4) 兜着，没人看得出配置没生效）。
    KB_TOP_K = int(os.environ.get("KB_TOP_K", "4"))
    KB_CHUNK_SIZE = int(os.environ.get("KB_CHUNK_SIZE", "420"))
    # 可选：Ollama 稠密向量模型（如 bge-m3 / nomic-embed-text）。
    # 留空则只用 BM25 稀疏检索，零额外显存；填了但模型不存在会自动回落。
    KB_EMBED_MODEL = os.environ.get("KB_EMBED_MODEL", "")

    # 通知公告：上级单位把文件丢进这个目录，自动解析发布
    # 支持 .md（带元信息头）/ .docx / .txt；解析不完整的转"待确认"，需人工补全
    NOTICE_DIR = os.path.join(BASE_DIR, "data", "notices")
    NOTICE_AUTO_IMPORT = os.environ.get("NOTICE_AUTO_IMPORT", "1") == "1"
    # 运行期间的后台扫描间隔（秒）。0 = 关闭，只在启动时扫一次。
    # 「文件丢进去就自动上线」靠的就是它，没有它就得点按钮或重启。
    NOTICE_SCAN_INTERVAL = int(os.environ.get("NOTICE_SCAN_INTERVAL", "60"))


class DevConfig(Config):
    DEBUG = True


class ProdConfig(Config):
    DEBUG = False


config_map = {"dev": DevConfig, "prod": ProdConfig}
