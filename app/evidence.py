"""证据哈希链：把争议相关的事实钉成一条不可悄悄修改的链

三方治理里最要命的一句话是"**你当时没这么说**"。居民说报过、物业说没收到、
居委会说已经通知过——三份记录都在各自的系统里，谁都能事后补一条。
所以凡是可能成为争议焦点的动作（提交诉求、上传照片、办结、驳回、居民确认），
都要在写业务表的同时落一条链记录。

## 能力边界（写在这里，免得答辩时说错）

**能证明**：这条链从某一条之后没有被改过、没有中间插过、没有删过某条。
**不能证明**：记录内容本身是真的；也防不住有人拿到 sqlite 文件后重算整条链。

所以：
- 链头必须**外锚**（`anchor()` 写 ChainAnchor + audit_log，审计日志另有每日备份，
  两条线同时被改的概率远低于改一条）；
- 对外一律说"**可验证未被篡改**"，不说"不可篡改"，更不说"区块链"。

## 为什么 payload 存脱敏快照而不是原文

链表的可读性来自 payload，但如果为了"举证清楚"把诉求原文原样塞进去，
就等于在 evidence_chain 里又存了一份包含手机号、身份证的明文——
绕过了 `security` Agent 的脱敏，也绕过了 `field_security` 的字段管控。
所以入库前统一过 `mask_sensitive()`：留下"说了什么"，抹掉"是谁的号码"。
原文本体仍在 `resident_ticket.raw_text`（已脱敏）里，链的作用是**证明它没被改**。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import ChainAnchor, EvidenceChain

# 链上只接受这几种动作，防止有人随手把无关操作也塞进来把链搅浑
ACTIONS = (
    "submit",    # 居民提交诉求
    "attach",    # 居民上传附件
    "dispatch",  # 派单
    "accept",    # 物业接单
    "transfer",  # 转派
    "finish",    # 物业办结
    "reject",    # 居委会驳回、退回重办
    "uphold",    # 居委会维持办结
    "confirm",   # 居民确认解决
    "dispute",   # 居民表示不服
)


def canonical(payload) -> str:
    """规范化序列化：同一份数据永远得到同一个字符串

    `sort_keys` 必须有——字典顺序一旦依赖插入顺序，同样的内容在两次
    序列化后会得到不同哈希，链会凭空"断"在最无辜的地方。
    `ensure_ascii=False` 让中文按 UTF-8 编码：转成 \\uXXXX 也能算出哈希，
    但链上内容就不可读了，排障时等于没有。
    """
    return json.dumps(payload or {}, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def _digest(prev_hash: str, target_type: str, target_id: int, action: str, body: str) -> str:
    """本条哈希 = sha256(上一条哈希 + 本条全部内容)

    把 target 与 action 一起算进去：只哈希 payload 的话，
    把 A 的附件记录和 B 的办结记录互换位置，两条的 payload 各自的哈希仍然成立，
    而语义已经完全错了。
    """
    raw = f"{prev_hash}\n{target_type}\n{target_id}\n{action}\n{body}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def head():
    """链头：返回最后一行的 (seq, hash)，空链返回 (0, "")"""
    last = db.session.query(EvidenceChain).order_by(EvidenceChain.seq.desc()).first()
    return (last.seq, last.hash) if last else (0, "")


def append(target_type: str, target_id: int, action: str, payload=None,
           actor: str = "", at=None):
    """追加一条链记录。**不 commit**，与调用方的业务写入同事务提交

    同事务的好处是：业务改动回滚了，链记录也一起消失，不会留下
    "链上有这条、业务表里查不到"的孤儿——那种孤儿在验证时会被误判成"记录被删"。

    并发：SQLite 单写入，但两个请求可能同时读到同一个 `prev_hash`。
    靠 `seq` 的唯一约束兜底，冲突时用 SAVEPOINT 重试——
    **不能用 `rollback()` 重试**，那会把调用方已经写好的业务改动一起回滚掉。
    """
    if action not in ACTIONS:
        raise ValueError(f"未登记的存证动作：{action}")
    body = canonical(payload)
    for _ in range(3):
        last = db.session.query(EvidenceChain).order_by(EvidenceChain.seq.desc()).first()
        prev = last.hash if last else ""
        seq = (last.seq if last else 0) + 1
        row = EvidenceChain(
            seq=seq, target_type=target_type, target_id=target_id, action=action,
            payload=body, prev_hash=prev,
            hash=_digest(prev, target_type, target_id, action, body),
            actor=(actor or "")[:32],
            created_at=at or datetime.now(),
        )
        try:
            with db.session.begin_nested():
                db.session.add(row)
            return row
        except IntegrityError:
            continue          # 有人抢先占了同一个 seq，重算 prev 再来
    raise RuntimeError("证据链写入冲突：连续 3 次 seq 争用失败")


def verify(target_type: str = "", target_id: int = None):
    """重算全链，返回断点

    三种断法都要分清楚，因为对应的事故完全不同：
      · `hash` 对不上 → 这一条**内容被改过**
      · `prev_hash` 对不上 → 这一条之前**被删过或插过**记录
      · `seq` 跳号 → 中间**有行被物理删除**
    只报"验证失败"而不说是哪种，等于把排查工作推给人。

    **锚点先比、链内自查后比**。顺序不能反：
    有人改了内容**并重算本条哈希**时，如果先跑链内自查，它会在下一条报
    `prev_mismatch`（前序哈希对不上），看起来像"记录被删了"，实际是"内容被改了"。
    先比锚点，才能准确说出"锚点之后链被重建过"。

    **锚点的能力边界要说清楚**：它只钉住**链头那一条**。
      · 改链头并重算哈希 → `anchor_mismatch`（锚点直接抓到）
      · 改中间某条并重算哈希 → 不会触发锚点，但会让下一条的 `prev_hash` 对不上，
        报 `prev_mismatch`。**仍然被发现**，只是诊断措辞是"被删或插"而不是"被改"。
    所以锚点不是万能的，但"篡改必然留下痕迹"这一点成立。
    要精确到"某条被改"，得每次锚都把全部行的哈希写一份——那是另一种成本，
    对这个体量（每天几十条）不划算，也没有必要：能证明"被动过"就够追责了。
    """
    rows = (db.session.query(EvidenceChain).order_by(EvidenceChain.seq.asc()).all())
    last_anchor = (db.session.query(ChainAnchor)
                   .order_by(ChainAnchor.id.desc()).first())

    # ① 锚点：链外的独立证据，优先级最高
    if last_anchor and last_anchor.seq:
        row = (db.session.query(EvidenceChain)
               .filter(EvidenceChain.seq == last_anchor.seq).first())
        if not row:
            return {"ok": False, "checked": len(rows), "broken_at": last_anchor.seq,
                    "reason": "anchor_missing",
                    "detail": f"锚点记录的第 {last_anchor.seq} 条已不存在"}
        if row.hash != last_anchor.head_hash:
            return {"ok": False, "checked": len(rows), "broken_at": last_anchor.seq,
                    "reason": "anchor_mismatch",
                    "detail": "第 %d 条的哈希与锚点时记录的哈希不一致：锚点之后链被重建过"
                              % last_anchor.seq}

    # ② 链内自查
    checked = 0
    expect_seq = 1
    prev = ""
    for r in rows:
        checked += 1
        if r.seq != expect_seq:
            return {"ok": False, "checked": checked, "broken_at": r.seq, "reason": "seq_gap",
                    "detail": f"序号应为 {expect_seq}，实际 {r.seq}：中间有记录被删除"}
        if r.prev_hash != prev:
            return {"ok": False, "checked": checked, "broken_at": r.seq, "reason": "prev_mismatch",
                    "detail": f"第 {r.seq} 条的前序哈希与上一条不符：之前有记录被删除或插入"}
        calc = _digest(r.prev_hash, r.target_type, r.target_id, r.action, r.payload or "")
        if calc != r.hash:
            return {"ok": False, "checked": checked, "broken_at": r.seq, "reason": "hash_mismatch",
                    "detail": f"第 {r.seq} 条内容与其哈希不符：这条记录被改过"}
        prev = r.hash
        expect_seq += 1
    seq_head, hash_head = (rows[-1].seq, rows[-1].hash) if rows else (0, "")

    return {"ok": True, "checked": checked, "head_seq": seq_head, "head_hash": hash_head,
            "anchored_at": last_anchor.seq if last_anchor else 0}


def anchor(note: str = "manual"):
    """把当前链头钉一份。**不 commit**（同 append）"""
    from app.models import AuditLog
    seq, h = head()
    total = db.session.query(EvidenceChain).count()
    row = ChainAnchor(seq=seq, head_hash=h, total=total, note=(note or "")[:128])
    db.session.add(row)
    # 同步写审计：审计有独立的导出与备份路径，形成第二条独立证据线。
    # actor 用 `chain` 而不是某个具体人——锚定是系统动作，不是个人操作。
    db.session.add(AuditLog(actor="chain", agent="evidence", action="anchor",
                            input_digest=f"seq={seq}",
                            output_digest=f"head={h[:32]} total={total} {note}"[:250]))
    return row


def chain_of(target_type: str, target_id: int):
    """某个对象的全部链记录（按时间正序），给详情页展示"""
    return (db.session.query(EvidenceChain)
            .filter_by(target_type=target_type, target_id=target_id)
            .order_by(EvidenceChain.seq.asc()).all())


def mask_payload(text: str, limit: int = 500) -> str:
    """链上用的脱敏文本快照

    先脱敏再截断——顺序反了会把号码截成半截，正则就认不出来了
    （`docs/26` 里踩过的同一个坑）。
    """
    from app.agents.base import mask_sensitive
    return mask_sensitive(text or "")[:limit]


# ------------------------------------------------------------------ 附件

MAX_PHOTO_BYTES = 5 * 1024 * 1024      # 单张 5MB：手机直出照片一般 2-4MB
MAX_PHOTOS = 5                         # 一次最多 5 张，够说明"哪面墙堆了什么"

# **只看扩展名是不够的**：把 .php 改名成 .jpg 一样能过扩展名检查，
# 而在某些部署方式下（比如有人手工配了静态目录直出）它会被当脚本执行。
# 所以按文件头认类型，认不出的一律拒。
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
)
# WEBP 的头是 RIFF????WEBP，前 4 字节和中间 4 字节要分别校验
_WEBP_HEAD = b"RIFF"
_WEBP_MARK = b"WEBP"


def sniff(data: bytes):
    """按内容判断图片类型，返回 (mime, ext) 或 (None, None)"""
    for head, mime, ext in _MAGIC:
        if data.startswith(head):
            return mime, ext
    if len(data) >= 12 and data[0:4] == _WEBP_HEAD and data[8:12] == _WEBP_MARK:
        return "image/webp", ".webp"
    return None, None


def save_photos(files, upload_dir):
    """保存居民上传的照片，返回 (saved, errors)

    saved 里每项 {name, sha256, size, mime, stored}：
      · `name` 是用户看到的原文件名（**只回显，不落盘**）；
      · `stored` 是随机文件名，落盘用。
    为什么不用原名落盘：居民的照片常叫「3栋201身份证.jpg」「王某某低保证.jpg」，
    一旦目录被列出（备份打包、同步脚本、误配静态目录），等于把门牌和证件类型
    一起送出去。原名只在数据库里留一份，和随机名对得上即可。

    为什么每张都算哈希：附件正是争议里最容易"被换掉"的东西——
    居民说拍的是漏水，物业说照片里没漏。哈希进链后，换图会让链断在该条。
    """
    import os
    import uuid

    saved, errors = [], []
    files = [f for f in (files or []) if f and getattr(f, "filename", "")]
    if not files:
        return saved, errors
    if len(files) > MAX_PHOTOS:
        return [], [f"最多上传 {MAX_PHOTOS} 张照片，本次收到 {len(files)} 张，请分批提交"]

    os.makedirs(upload_dir, exist_ok=True)
    for f in files:
        raw = f.read()
        if not raw:
            errors.append(f"{f.filename}：空文件")
            continue
        if len(raw) > MAX_PHOTO_BYTES:
            errors.append(f"{f.filename}：超过 5MB，请压缩后再传")
            continue
        mime, ext = sniff(raw)
        if not mime:
            # 明确说"不是图片"，而不是"格式不支持"——后者会让人以为
            # 换个扩展名就行，实际上换扩展名也过不了这一关
            errors.append(f"{f.filename}：不是有效的图片（只接受 JPG / PNG / WEBP）")
            continue
        stored = uuid.uuid4().hex + ext
        with open(os.path.join(upload_dir, stored), "wb") as fh:
            fh.write(raw)
        saved.append({
            "name": (f.filename or "")[:64],
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "mime": mime,
            "stored": stored,
        })
    return saved, errors


def load_attachments(raw: str):
    """把 ticket.attachments 里的 JSON 解成列表；坏数据不抛异常

    老库里这一列可能是 NULL、空串，或者早年手工写进去的非法 JSON。
    这里吞掉异常返回空列表，让页面照常显示——一张图挂了不能让整页白屏。
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []
