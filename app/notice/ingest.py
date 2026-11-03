"""公告入库：扫描 data/notices/ → 解析 → 编排 → 发布

「单独一个文件夹放上级单位的文件，自动编排自动发布」就落在这里。

流程（每个文件独立成败，一个坏文件不影响其他）：
    文件 → parse_file → 去重（内容哈希）→ 置信判决 → 入库（published / pending）
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from flask import current_app

from app.extensions import db
from app.models import AuditLog, Notice
from app.notice.parse import SUPPORTED, parse_file

# 层级判定与发文接口共用一张表（app/auth.py 的 SUPERIOR_LEVELS），
# 避免"页面按一套口径算、入库按另一套口径算"的错位
from app.auth import is_superior


def notice_dir() -> Path:
    d = Path(current_app.config["NOTICE_DIR"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def _audit(action: str, detail: str, actor: str = "system"):
    db.session.add(AuditLog(actor=actor, agent="notice", action=action,
                            input_digest="", output_digest=detail[:250]))


def scan_and_import(actor: str = "system", publish: bool = True,
                    force: bool = False) -> dict:
    """扫描并导入目录下的所有支持格式文件

    publish=True  字段解析完整就直接发布（上级文件属于"应当公开"的信息）
    publish=False 只入库为待确认，由社区人工核对后再发
    force=True    内容哈希已存在也重新解析覆盖正文（改过文件重发时用）
    """
    d = notice_dir()
    result = {"imported": [], "updated": [], "skipped": [], "pending": [], "failed": []}

    files = sorted(p for p in d.iterdir()
                   if p.is_file() and p.suffix.lower() in SUPPORTED
                   and not p.name.startswith((".", "~$")))
    if not files:
        return result

    for path in files:
        try:
            data = parse_file(path)
        except Exception as e:                      # 解析器自身异常也要兜住
            result["failed"].append({"file": path.name, "why": f"{type(e).__name__}: {e}"})
            continue

        old = Notice.query.filter_by(source_hash=data["source_hash"]).first()
        if old and not force:
            # 内容哈希相同 = 同一份文件没动过，直接跳过
            result["skipped"].append(path.name)
            continue
        if not old:
            # 走到这里说明内容变了（source_hash 必然不同）。
            # 光按哈希找不回原记录，会把"上级改了同一份文件"变成多出一条重复公告，
            # 所以再按文件名认一次：同目录同名即视为同一份公告的更新。
            old = (Notice.query.filter_by(source_file=path.name)
                   .order_by(Notice.id.desc()).first())

        note = "；".join(dict.fromkeys(data["note"]))   # 去重保序
        if old:                                          # 同一份文件被修改后重扫
            old.title = data["title"] or old.title
            old.issuer = data["issuer"] or old.issuer
            old.doc_no = data["doc_no"] or old.doc_no
            old.issued_at = data["issued_at"] or old.issued_at
            old.body_md = data["body_md"] or old.body_md
            old.summary = data["summary"] or old.summary
            old.source_hash = data["source_hash"]
            old.parse_note = note
            if publish and data["complete"]:
                old.status = "published"
                old.published_at = old.published_at or datetime.now()
            db.session.commit()
            result["updated"].append(path.name)
            continue

        n = Notice(
            title=data["title"] or path.stem,
            issuer=data["issuer"],
            doc_no=data["doc_no"],
            issued_at=data["issued_at"],
            category=data["category"],
            level=data["level"],
            scope=data["scope"],
            body_md=data["body_md"],
            summary=data["summary"],
            source_file=path.name,
            source_hash=data["source_hash"],
            parse_note=note,
            pinned=data["pinned"],
            created_by=actor,
            # 上级层级的文件进收文流程（社区工作台出现待签收）；社区自己放的不需要收文
            # 判定走 SUPERIOR_LEVELS 而不是 == "街道"，以后调层级只改一处
            kind="superior" if is_superior(data["level"]) else "original",
            recv_status="dispatched" if is_superior(data["level"]) else "",
            status="published" if (publish and data["complete"]) else "pending",
        )
        if n.status == "published":
            n.published_at = datetime.now()
        db.session.add(n)
        db.session.commit()

        if n.status == "published":
            result["imported"].append({"file": path.name, "id": n.id, "title": n.title})
            _audit("publish", f"自动发布公告《{n.title}》（来源 {path.name}）", actor)
        else:
            result["pending"].append({"file": path.name, "id": n.id, "note": note})
            _audit("pending", f"公告待确认《{n.title}》（{note}）", actor)
        db.session.commit()

    return result


def publish(notice_id: int, actor: str) -> tuple[bool, str]:
    n = db.session.get(Notice, notice_id)
    if not n:
        return False, "公告不存在"
    missing = [k for k, v in (("标题", n.title), ("发文单位", n.issuer)) if not v]
    if missing:
        return False, f"缺少{'、'.join(missing)}，不能发布"
    if n.status == "published":
        # 幂等：已经发布过的不能再盖一次 published_at。
        # 之前这里无条件刷新时间戳，重复点一次"发布"就把老文件顶到列表最前面，
        # 居民以为刚发的，其实是上个月的——发稿时间是证据，不能被误触改写。
        return True, f"《{n.title}》已于 {n.published_at:%Y-%m-%d %H:%M} 发布，无需重复操作"
    n.status = "published"
    n.published_at = datetime.now()
    _audit("publish", f"人工确认发布《{n.title}》", actor)
    db.session.commit()
    return True, "已发布"


def take_offline(notice_id: int, actor: str) -> tuple[bool, str]:
    n = db.session.get(Notice, notice_id)
    if not n:
        return False, "公告不存在"
    if n.status == "offline":
        return True, f"《{n.title}》已处于下架状态"
    n.status = "offline"
    # 下架后置顶失去意义，但 pinned/pinned_at 保留原值，便于重新上架后恢复排序
    _audit("offline", f"下架公告《{n.title}》", actor)
    db.session.commit()
    return True, "已下架"


def _exclude_docs(q):
    """公告口径一律不含发文

    发文（direction=out）和公告共用 notice 表，但它们是两回事：
      · 公告是**给居民看的**，有 scope、有发布状态；
      · 公文是**给机关看的**，有文号、签发人、密级，根本不进居民端。
    不过滤的话，一份"关于申请增补消防设施的请示"会出现在通知公告列表里，
    社区一点"确认发布"就推到了居民端——请示里往往写着经费数额和未决事项，
    这不是显示问题，是泄密。

    用 `is_(None) or != out` 而不是直接 `!= out`：老库 direction 可能为 NULL，
    而 SQL 里 `NULL != 'out'` 是 NULL，会把这批行整批筛掉（公告全消失）。
    """
    from sqlalchemy import or_
    return q.filter(or_(Notice.direction.is_(None), Notice.direction != "out"))


def staff_query(role: str, base=None):
    """**内部账号**的可见范围（列表 / 详情 / 导出共用这一个口径）

    和下面 `visible_query`（居民端）不是一回事，别混：

      · 居民端只该看"已发布 + 面向所有人"的，看不见草稿，也看不见内部件。
      · 社区是属地发布方，它要确认、要下架，所以草稿、已下架、各范围都得看得见。
      · 街道是监督方，按"全区几件没签"看，同样不过滤。
      · 物业只有 notice.view：它既不是发布方也不是监督方，只该看到
        **已发布、且 scope 允许它看**的那部分。

    早先内部接口直接用 `Notice.query`，于是物业账号按 id 直取就能读到
    scope=community 的社区内部公告，连正文带附件导出（docx）都能拿走。
    列表里也照样列出来——页面上不显示只是"没渲染"，接口早就把内容发出去了。
    """
    q = _exclude_docs(Notice.query if base is None else base)
    if role in ("community", "street", "admin"):
        return q
    # scope 收窄走 ABAC 同一张规则表（`app/policy.py:scope_query`）。
    # 以前这里是 `Notice.scope.in_(["all", role])` 的第二份实现，
    # 与 `can_view` 里的 `(n.scope or "all") in ("all", role)` 就已经有细微差异了——
    # 两处口径不一致的后果是"列表里有、点进去 404"，最难排查的一类问题。
    from app.policy import scope_query
    q, _ = scope_query("notice", q, None, role_override=role)
    return q.filter(Notice.status == "published")


def can_view(n, role: str) -> bool:
    """单个对象能不能看。与 `staff_query` 必须同一口径：

    两处口径一旦不一致，就会出现"列表里没有、按 id 却能打开"或反过来
    "列表里有、点进去 404"，两种都很难排查。
    """
    if (n.direction or "in") == "out":
        return False          # 公文不进公告体系，任何角色都不该从这里看到
    if role in ("community", "street", "admin"):
        return True
    # 与列表侧同源：同一个 scope 归一（空串当 "all"）、同一张规则表。
    from app.policy import RoleShield, decide
    return decide(RoleShield(role), "read", "notice",
                  scope=(n.scope or "all"), status=(n.status or "")).allow


def visible_query(role: str = "resident"):
    """可见范围：已发布 + 与该角色相关的 scope

    scope=all 对所有角色可见；community/property 只给对应角色和街道看。
    """
    q = _exclude_docs(Notice.query.filter(Notice.status == "published"))
    if role in ("resident", "", None):
        return q.filter(Notice.scope.in_(["all"]))
    if role == "street":
        return q
    return q.filter(Notice.scope.in_(["all", role]))
