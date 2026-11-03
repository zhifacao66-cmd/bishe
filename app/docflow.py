"""公文流转：状态迁移 + 文号生成 + 密级与要素校验的唯一入口

为什么单独一个模块，而不是塞进 `app/api/docs.py`：
  "改状态"和"留痕"必须是同一件事。分散在端点里写，迟早有人只改
  `notice.doc_status = "signed"` 忘了写 doc_log——而这样不会报错，
  只会在半年后追责时发现"这份文谁签的"查不到。
  和工单那边 `orderflow.push()` 是同一个理由（docs/26 红线 3）。

发文状态机（`doc_status`）：

    draft ──check──> checked ──sign──> signed ──seal──> sealed
      │                 │                                  │
      │                 └──────────sign（函可跳核稿）──────┘
      └──reject─────────<──────────────────────────────────┘
                                              sealed ──send──> sent ──archive──> archived

**收文不走这台状态机**。收文的 `recv_status`（dispatched→received）已经够用，
它的"办结"是填 `handle_result`，不是状态迁移——把两者硬塞进同一套状态，
会出现"签收了但没办"和"办完了没签收"这种在真实公文里不存在的组合。
"""
from datetime import date, datetime

from app.extensions import db
from app.models import DocLog

# 密级标识（不是"定密"）
#
# 《保守国家秘密法》的三级密级只能由**有定密权的机关**确定，居委会没有定密权。
# 所以这份清单里，"秘密/机密"只用于**继承上级来文**，社区自己产生材料时
# 只能标前三档。这条边界如果不在代码里约束，做出来的功能在业务上是错的——
# 一个社区给自己写的通知标"机密"，是硬伤。
SECRET_LEVELS = ("", "公开", "内部", "内部·敏感", "秘密", "机密")
INHERITED_LEVELS = ("秘密", "机密")

# 文种 → 行文方向。选错文种打印出来就是硬伤（平行文用"通知"是常识性错误）
DOC_TYPES = {
    "通知": "下行",
    "通报": "下行",
    "决定": "下行",
    "公告": "下行",
    "纪要": "下行",
    "请示": "上行",
    "报告": "上行",
    "函": "平行",
}

# 上行文必须签发给具体机关，且必须有签发人（GB/T 9704 规定上行文标注签发人）
UPWARD = ("请示", "报告")

ACTIONS = {
    "check": ("checked", "核稿"),
    "sign": ("signed", "签发"),
    "seal": ("sealed", "用印"),
    "send": ("sent", "分发"),
    "archive": ("archived", "归档"),
    "reject": ("draft", "退回拟稿"),
}

# 动作 → 允许的起始状态。None 不出现：新建即 draft，由 API 层直接落库。
ALLOWED_FROM = {
    "check": {"draft"},
    "sign": {"draft", "checked"},   # 函可以不核稿直接签发
    "seal": {"signed"},
    "send": {"sealed"},
    "archive": {"sent"},
    "reject": {"checked", "signed"},
}

LABEL = {k: v[1] for k, v in ACTIONS.items()}
# 收文办结不是状态迁移，但一样要留痕，所以它只进"展示用"的标签表。
# 不放进 ACTIONS：ACTIONS 同时驱动 ALLOWED_FROM 状态机，加进去会让
# `push(n, "handle")` 变成合法调用，把收文硬塞进发文状态机。
LABEL["handle"] = "办结"

# 状态 → 中文。注意 draft 不在 ACTIONS 里（拟稿是创建，不是迁移），
# 所以不能只靠 LABEL，否则草稿会显示成"未拟稿"——那是两回事。
STATUS_LABEL = {
    "": "未拟稿",
    "draft": "草稿",
    "checked": "已核稿",
    "signed": "已签发",
    "sealed": "已用印",
    "sent": "已分发",
    "archived": "已归档",
}

# 用印前的必填项。缺一项就不该进入 sealed——盖了章再发现没编号，
# 只能作废重来，纸质件还能撕，电子件留下的编号是删不掉的。
SEAL_REQUIRED = ("doc_no", "secret_level")


def make_doc_no(notice) -> str:
    """生成发文字号：`社〔2026〕12号`

    `doc_seq` 落库而不是每次从 `doc_no` 字符串里正则反解：
      · 反解要处理"社〔2026〕12号"和"社发〔2026〕12 号"两种写法，脆；
      · 手工补录的历史文号可能压根不规范，反解会算错下一个号。
    年份按 `doc_year` 取，跨年自动从 1 开始——这是公文编号的硬规则，
    不能因为"去年用到 87 号"就让今年从 88 接着排。
    """
    from flask import current_app
    from app.models import Notice

    abbr = current_app.config.get("DOC_ABBR") or "社"
    year = notice.doc_year or date.today().year
    notice.doc_year = year
    if not notice.doc_seq:
        last = (db.session.query(db.func.max(Notice.doc_seq))
                .filter(Notice.doc_year == year, Notice.direction == "out")
                .scalar())
        notice.doc_seq = int(last or 0) + 1
    return f"{abbr}〔{year}〕{notice.doc_seq}号"


def next_free_no() -> str:
    """取一个当前没被占用的文号。

    为什么不直接在 `make_doc_no` 里查重：那函数是"给某份文定号"，
    而这里是"要一个空号"。两件事分开，调用方（分发时补号）才能循环重试。
    """
    from flask import current_app
    from app.models import Notice

    abbr = current_app.config.get("DOC_ABBR") or "社"
    year = date.today().year
    last = (db.session.query(db.func.max(Notice.doc_seq))
            .filter(Notice.doc_year == year, Notice.direction == "out")
            .scalar()) or 0
    for i in range(int(last) + 1, int(last) + 50):
        cand = f"{abbr}〔{year}〕{i}号"
        if not db.session.query(Notice.id).filter(Notice.doc_no == cand).first():
            return cand
    raise ValueError("本年度文号已排满，请检查 doc_seq 是否异常")


def check_ready(notice, action: str) -> str:
    """动作前置校验，返回空串表示可以走；否则返回给人看的原因

    这些校验放这里而不是 API 层：将来加"批量签发"或某个 Agent 直接推状态，
    少写一句就绕过了。校验和状态机放在同一个文件，抄漏的概率最低。
    """
    if action == "sign":
        if not (notice.signer or "").strip():
            return "签发前必须填写签发人"
        if notice.doc_type in UPWARD and not (notice.to_org or "").strip():
            return "上行文必须填写主送机关"
    if action == "seal":
        for f in SEAL_REQUIRED:
            if not (getattr(notice, f) or "").strip():
                return f"用印前必须补全：{'文号' if f == 'doc_no' else '密级标识'}"
    if action == "send":
        if not (notice.to_org or "").strip():
            return "分发前必须填写主送机关"
    return ""


def push(notice, action: str, actor: str = "", actor_id=None, note: str = "") -> DocLog:
    """记一步流转，并把发文推到该动作的目标状态。

    不 commit —— 交给调用方和自己那批写入一起提交。
    起始状态不合法时抛 ValueError（用户点了不该点的按钮，API 层转 409）。
    """
    if action not in ACTIONS:
        raise ValueError(f"未知发文动作：{action}")
    cur = notice.doc_status or ""
    if cur not in ALLOWED_FROM[action]:
        raise ValueError(
            f"当前状态「{cur or '未拟稿'}」不允许「{LABEL[action]}」"
            f"（允许：{'、'.join(x or '未拟稿' for x in sorted(ALLOWED_FROM[action]))}）")
    reason = check_ready(notice, action)
    if reason:
        raise ValueError(reason)

    to_status, _ = ACTIONS[action]
    log = DocLog(
        notice_id=notice.id,
        doc_no=notice.doc_no or "",
        action=action,
        from_status=cur,
        to_status=to_status,
        actor=(actor or "")[:32],
        actor_id=actor_id,
        note=(note or "")[:2000],
    )
    notice.doc_status = to_status
    if action == "seal":
        notice.seal_by = (actor or "")[:32]
        notice.sealed_at = datetime.now()
    if action == "archive":
        notice.archived_at = datetime.now()
    db.session.add(log)
    return log


def handle_inbound(notice, result: str, actor: str = "", actor_id=None) -> DocLog:
    """登记收文办理结果（办结）。

    为什么不复用 `push()`：收文不走发文状态机。`recv_status` 只到 `received`
    就够用了（dispatched→received），办结是**填 handle_result**，不是状态迁移。
    把两者硬塞进一套状态，会出现"签收了但没办结"和"办结但没签收"这种在真实
    公文里不存在的中间态（见本模块开头）。

    但"谁办的、什么时候办完的、办成了什么"必须和状态变更一样留痕——所以这里
    写一条 DocLog(action="handle")，与 push() 同一个表、同一个口径。
    不 commit，交给调用方（和它那批写入一起提交）。

    handled_at 只记**首次**办结时间：补正/改结果不该把周报里"本周办结"的数字
    挪到下周去。每次提交都留一条日志，"改过几次"从日志里看得见。
    """
    text = (result or "").strip()
    if len(text) < 5:
        raise ValueError("办理结果至少 5 个字，请写清做了什么、什么结果")
    if len(text) > 500:
        raise ValueError("办理结果过长（≤500 字），细节请附在后续材料里")

    first = notice.handled_at is None
    notice.handler = (actor or "")[:32]
    notice.handle_result = text[:500]
    if first:
        notice.handled_at = datetime.now()
    log = DocLog(
        notice_id=notice.id,
        doc_no=notice.doc_no or "",
        action="handle",
        # 收文没有 doc_status，这两个字段留空；填 recv_status 会让人误以为
        # 收文也在这台状态机上走
        from_status=notice.doc_status or "",
        to_status=notice.doc_status or "",
        actor=(actor or "")[:32],
        actor_id=actor_id,
        note=text[:2000],
    )
    db.session.add(log)
    return log


def direction_of(doc_type: str) -> str:
    """文种 → 行文方向，未知文种当内部件，不猜成"下行\""""
    return DOC_TYPES.get(doc_type, "内部")
