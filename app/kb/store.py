"""RAG 检索层：字符二元组 BM25（默认）+ 可选 Ollama 稠密向量混合

为什么默认不直接上向量库：
1. 本机 8GB 显存已被 qwen3:8b 占去 6.7GB，再加载 bge-m3 这类 embedding 模型会挤爆显存
   或把生成模型挤到 CPU，整体体验反而更差；
2. 社区场景的检索对象是政策文件、办事指南，属于「术语精确匹配」任务——
   “高龄津贴”“居住证”“群租”这类专有名词，BM25 的命中率不弱于小参数量 embedding，
   而且完全确定性、可解释（能指出命中了哪几个词），便于答辩演示与排错；
3. 仍然保留 KB_EMBED_MODEL 开关：换机器/显存富裕时设为 bge-m3 即切混合检索，代码不用改。

中文分词采用「字符二元组」而非 jieba：零额外依赖、零下载，
对政策文本（用词规范、少口语）效果稳定。
"""
import math
import re
import threading
from collections import Counter

from app.extensions import db
from app.models import KbDoc

# ---------------------------------------------------------------- 分词与切片

_SPLIT = re.compile(r"[\s\u3000，。、；：？！“”‘’（）《》〈〉【】\[\],.;:?!\"'()<>/\\|~`@#$%^&*+=_-]+")


def tokenize(text: str) -> list[str]:
    """英文/数字按词，中文按字符二元组。"""
    tokens = []
    for seg in _SPLIT.split((text or "").lower()):
        if not seg:
            continue
        if seg.isascii():
            tokens.append(seg)
        else:
            if len(seg) == 1:
                tokens.append(seg)
            else:
                tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return tokens


def _split_long(heading: str, body: str, size: int) -> list[dict]:
    """长段落按句号切分，避免一个切片横跨多个主题。"""
    if len(body) <= size * 1.4:
        pieces = [body]
    else:
        pieces, buf = [], ""
        for sent in re.split(r"(?<=[。；！？])", body):
            if not sent:
                continue
            if len(buf) + len(sent) > size and buf:
                pieces.append(buf)
                buf = sent
            else:
                buf += sent
        if buf:
            pieces.append(buf)
    prefix = f"{heading}｜" if heading else ""
    return [{"text": prefix + p.strip(), "heading": heading}
            for p in pieces if p.strip()]


def split_chunks(content: str, size: int = 420) -> list[dict]:
    """按标题分节 + 同节段落聚合切片，保留标题作为上下文前缀。

    跳过 `>` 引用块（来源、演示说明）：这些行没有业务信息，却会因标题词命中
    而挤掉真正有用的条款片段。
    """
    blocks, buf, heading = [], [], ""

    def flush():
        nonlocal buf
        text = "\n".join(buf).strip()
        if text:
            blocks.append((heading, text))
        buf = []

    for raw in (content or "").splitlines():
        s = raw.strip()
        if s.startswith("#"):
            flush()
            heading = s.lstrip("#").strip()
            continue
        if not s or s.startswith(">"):
            flush()
            continue
        buf.append(s)
    flush()

    chunks, cur_head, cur = [], None, []

    def emit():
        nonlocal cur
        if cur:
            chunks.extend(_split_long(cur_head, "\n".join(cur), size))
            cur = []

    for h, text in blocks:
        if h != cur_head:
            emit()
            cur_head = h
        cur.append(text)
        if sum(len(x) for x in cur) >= size * 0.6:
            emit()
    emit()
    return chunks


# ---------------------------------------------------------------- 稠密向量（可选）

def _embed(texts: list[str], model: str):
    """调用 Ollama /api/embed。失败返回 None，由调用方回落纯 BM25。"""
    if not model:
        return None
    try:
        import requests
        from flask import current_app
        base = current_app.config.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        # 与 llm/client.py 同理：本地地址不能走系统代理，否则 POST 会被代理回 404
        s = requests.Session()
        s.trust_env = False
        r = s.post(base + "/api/embed",
                   json={"model": model, "input": texts}, timeout=25)
        if r.status_code != 200:
            return None
        return r.json().get("embeddings")
    except Exception:
        return None


def _cosine(a, b) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return num / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------- 索引

class KbIndex:
    def __init__(self):
        self.chunks: list[dict] = []
        self.postings: dict[str, list[tuple[int, int]]] = {}
        self.df: Counter = Counter()
        self.avgdl = 1.0
        self.embed_model = ""
        self.vectors: list[list[float]] | None = None

    # ---- 构建
    def build(self, docs, chunk_size: int = 420, embed_model: str = ""):
        self.chunks, self.postings, self.df = [], {}, Counter()
        for d in docs:
            for seq, ch in enumerate(split_chunks(d.content, chunk_size)):
                idx = len(self.chunks)
                toks = tokenize(ch["text"])
                tf = Counter(toks)
                self.chunks.append({
                    "doc_id": d.id, "title": d.title, "category": d.category,
                    "seq": seq, "heading": ch["heading"], "text": ch["text"],
                    "tokens": toks,
                    "title_tokens": set(tokenize(d.title)) | set(tokenize(ch["heading"])),
                })
                for t, c in tf.items():
                    self.postings.setdefault(t, []).append((idx, c))
                    self.df[t] += 1
        self.avgdl = (sum(len(c["tokens"]) for c in self.chunks) / len(self.chunks)) if self.chunks else 1.0

        self.embed_model = embed_model
        self.vectors = None
        if embed_model and self.chunks:
            vecs = _embed([c["text"] for c in self.chunks], embed_model)
            if vecs and len(vecs) == len(self.chunks):
                self.vectors = vecs
            else:
                self.embed_model = ""   # 模型没拉下来/服务不可用 → 静默回落
        return self

    # ---- 检索
    def search(self, query: str, top_k: int = 4) -> list[dict]:
        q = tokenize(query)
        if not q or not self.chunks:
            return []
        k1, b = 1.5, 0.75
        scores: dict[int, float] = {}
        for tok in set(q):
            df = self.df.get(tok, 0)
            if not df:
                continue
            idf = math.log(1 + (len(self.chunks) - df + 0.5) / (df + 0.5))
            for idx, tf in self.postings.get(tok, []):
                dl = len(self.chunks[idx]["tokens"])
                denom = tf + k1 * (1 - b + b * dl / max(self.avgdl, 1e-6))
                scores[idx] = scores.get(idx, 0.0) + idf * tf * (k1 + 1) / denom

        # 标题/小节名命中加成：整篇主题与“所需材料”这类小节名比零散片段更可信
        qset = set(q)
        for idx in list(scores):
            tt = self.chunks[idx]["title_tokens"]
            hit = len(qset & tt) / max(len(qset), 1)
            if hit:
                scores[idx] *= 1 + 0.8 * hit

        if not scores:
            return []

        # 可选稠密向量混合
        if self.vectors:
            qv = _embed([query], self.embed_model)
            if qv:
                max_bm = max(scores.values()) or 1.0
                for idx in scores:
                    sim = _cosine(qv[0], self.vectors[idx])
                    scores[idx] = 0.65 * (scores[idx] / max_bm) + 0.35 * max(sim, 0)
            norm = max(scores.values()) or 1.0
        else:
            norm = max(scores.values()) or 1.0

        out = []
        for idx, s in sorted(scores.items(), key=lambda kv: -kv[1])[:top_k]:
            c = self.chunks[idx]
            out.append({
                "doc_id": c["doc_id"], "title": c["title"], "category": c["category"],
                "seq": c["seq"], "heading": c["heading"],
                "score": round(min(s / norm, 1.0), 4),
                "snippet": _snippet(c["text"], q),
                "text": c["text"],
            })
        return out

    def stats(self) -> dict:
        return {
            "doc_count": len({c["doc_id"] for c in self.chunks}),
            "chunk_count": len(self.chunks),
            "avg_chunk_chars": int(sum(len(c["text"]) for c in self.chunks) / len(self.chunks)) if self.chunks else 0,
            "dense": bool(self.vectors),
            "embed_model": self.embed_model,
        }


def _snippet(text: str, q_tokens: list[str], width: int = 120) -> str:
    pos = -1
    for t in q_tokens:               # 二元组本身可直接在原文里找
        p = text.find(t)
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    if pos < 0:
        return text[:width * 2] + ("…" if len(text) > width * 2 else "")
    start = max(0, pos - width // 3)
    end = min(len(text), pos + width)
    return ("…" if start > 0 else "") + text[start:end] + ("…" if end < len(text) else "")


# ---------------------------------------------------------------- 全局索引

_INDEX: KbIndex | None = None
_LOCK = threading.Lock()


def rebuild(chunk_size: int = 420, embed_model: str = "") -> KbIndex:
    global _INDEX
    docs = KbDoc.query.order_by(KbDoc.id).all()
    idx = KbIndex().build(docs, chunk_size=chunk_size, embed_model=embed_model)
    with _LOCK:
        _INDEX = idx
    return idx


def get_index(chunk_size: int = 420, embed_model: str = "") -> KbIndex:
    global _INDEX
    with _LOCK:
        idx = _INDEX
    if idx is None:
        idx = rebuild(chunk_size=chunk_size, embed_model=embed_model)
    return idx


def init_kb(app):
    """应用启动时建索引（文档少，几十毫秒级）"""
    with app.app_context():
        rebuild(app.config.get("KB_CHUNK_SIZE", 420),
                app.config.get("KB_EMBED_MODEL", ""))
