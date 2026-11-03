"""导出留痕：谁、什么时候、把哪份文件拿走了。

为什么单独一个模块
------------------
下载是最容易"查不到人"的动作——页面刷了有访问日志，文件一旦存到本地，
系统里就没有任何痕迹了。所以每次导出都要先写一条审计。

原来这个函数是 `views.py` 的私有 `_log_export`，只有报表与公文两个导出在用。
底册导出（`/api/persons/export`）当时没接上，等于 300 个居民的姓名、证件掩码、
门牌、残疾等级被谁导走了查不到——这正是 ABAC 的 R6 要拦的那类事情。
抽出来是为了让三处导出共用同一份实现，而不是各写一遍（写一遍就会漏一遍）。

写失败不能影响下载本身：审计是附加证据，不是导出的前置条件。
"""
from __future__ import annotations


def log_export(kind: str, detail: str) -> None:
    """记一条导出审计。

    kind   : 资源类型（report / notice / person），落到 audit_log.action
    detail : 人可读的一句话，会先脱敏再截断到 240 字

    整条包 try：数据库忙、字段变更都不该让用户拿不到文件。
    """
    try:
        from app.agents.base import mask_sensitive
        from app.auth import current_user
        from app.extensions import db
        from app.models import AuditLog

        u = current_user()
        db.session.add(AuditLog(actor=(u.name if u else "system")[:32],
                                agent="export", action=kind,
                                output_digest=mask_sensitive(detail)[:240]))
        db.session.commit()
    except Exception:                                    # noqa: BLE001
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:                                # noqa: BLE001
            pass
