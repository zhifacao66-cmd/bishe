"""ABAC 访问决策点：一份规则表，一个入口

## 为什么要这个文件

改造之前，"这条数据该不该让他看"被手写在七个地方：

  app/api/orders.py:_scoped()            `u.role == "property" → property_co == u.org`
  app/api/tasks.py:_scoped()             `u.role in ("community","property") → assignee_*`
  app/notice/ingest.py:staff_query()     `scope in ("all", role)`
  app/notice/ingest.py:can_view()        同上，另一份实现
  app/api/docs.py:196                    密级 in ("内部·敏感","秘密","机密") → 403
  app/views.py:208                       同上，另一份实现（页面层）
  app/api/evidence.py                    `property_co != u.org → 403`

它们现在能对上，靠的是每次改都记得把七处一起改。这不叫设计，叫运气——
`docs.py` 和 `views.py` 那份重复已经证明了：接口挡住了、模板还在渲染，
等于没修（这是本轮之前真实发生过的事）。这个文件把它们收敛成一个 `decide()`。

## 为什么是「两层并存」而不是「替代 RBAC」

  RBAC 回答：**能不能进这个功能**（`PERMS` 表，管到页面/接口粒度）
  ABAC 回答：**进来之后这一条能不能看**（本文件，管到行/字段粒度）

说"用 ABAC 替代 RBAC"是不懂装懂：权限点管不住"同一张列表里哪几行该隐藏"
（有 `order.view` 的人里，物业不该看到别家单位的单），
属性规则也管不住"这个人有没有资格进这个页面"（属性再多也不构成功能授权）。
所以本文件**不动** `auth.PERMS`，只在它判定通过之后接手。

## 四类属性（缺一类就只能叫「带条件的 RBAC」）

  主体 subject：role / org / perms / status
  资源 resource：kind / secret（密级 1-5）/ scope / owner_org
  动作 action：read / list / export / write / judge
  环境 env：channel（internal / portal）/ export_fmt

## 为什么返回 Decision 而不是 bool

ABAC 的价值一半在"拒"，一半在"拒了能说清为什么"。`reason` 有三个用处：
  ① 403 的 body 里给人看（"密级超出你的可见上限"，而不是干巴巴的"无权限"）
  ② 审计日志留痕（谁、在什么属性组合下、被哪条规则挡的）
  ③ 答辩现场解释规则——`/security/policy` 页面上直接试算
"""
from dataclasses import dataclass, field

from app.extensions import db
from app.models import Notice, Task

# ---------------------------------------------------------------- 密级

# 库里存的是中文字符串（Notice.secret_level），这里映射成可比较的整数。
# 空串 = 未标密级。**未标不等于公开**：老数据大多是空串，按 2（内部）处理
# 才是保守方向；把未标当公开，等于让"忘了标"变成"对全网公开"。
SECRET_NUM = {
    "": 2,
    "公开": 1,
    "内部": 2,
    "内部·敏感": 3,
    "秘密": 4,
    "机密": 5,
}
SECRET_LABEL = {1: "公开", 2: "内部", 3: "内部·敏感", 4: "秘密", 5: "机密"}

# 角色可见密级上限。
# property = 2 与改造前完全一致（docs.py 挡 3/4/5）；不收紧是为了不改变已有行为。
# 收紧到 1（连"内部"都看不到）会让物业看不到物业费调价通知，那不叫保密，叫瘫痪。
ROLE_SECRET_CEILING = {
    "resident": 1,
    "property": 2,
    "community": 5,
    "street": 5,
    "admin": 5,
}

# 上位角色：跨单位横向可见的唯一依据
OVERSEER_ROLES = ("street", "admin")


def secret_num(level):
    """中文字符串密级 → 整数。认不出的按"机密"处理。

    认不出就往上兜，不能往下兜：新增一个密级标识（比如"内部·敏感（含居民信息）"）
    而忘了往 SECRET_NUM 里加，往下兜会让它变成"公开"直接漏出去。
    """
    s = (level or "").strip()
    if s in SECRET_NUM:
        return SECRET_NUM[s]
    return 5


# ---------------------------------------------------------------- 决策对象


@dataclass
class Decision:
    """一次决策的结果。`allow=False` 时 `rule` 指出被哪条规则挡住。"""

    allow: bool
    rule: str = ""          # 命中的规则名，如 "R4 密级上限"
    reason: str = ""        # 人话解释，直接进 403 body 与审计
    mask: bool = False      # 是否需要字段级脱敏（含身份证/手机号的行）
    watermark: bool = False  # 是否必须打水印（导出动作一律 True）
    log: bool = False       # 是否必须写审计（拒绝与导出都要写）
    attrs: dict = field(default_factory=dict)   # 现场属性快照，便于排错

    def deny(self, code=403):
        return {"ok": False, "error": self.reason, "rule": self.rule}, code


def _allow(rule, reason, **kw):
    return Decision(True, rule, reason, **kw)


def _deny(rule, reason, **kw):
    return Decision(False, rule, reason, log=True, **kw)


# ---------------------------------------------------------------- 主体/资源归一


def _subject(user):
    """把 User（或 None）压成属性字典。None = 未登录，也要能走完流程。"""
    if not user:
        return {"role": "", "org": "", "status": "", "perms": set(), "name": "匿名"}
    return {
        "role": getattr(user, "role", "") or "",
        "org": getattr(user, "org", "") or "",
        "status": getattr(user, "status", "") or "",
        "perms": user.perms() if hasattr(user, "perms") else set(),
        "name": getattr(user, "name", "") or getattr(user, "username", "") or "",
    }


def _own_org(user):
    return (getattr(user, "org", "") or "").strip()


class RoleShield:
    """只有一个角色字符串时的主体替身。

    `notice/ingest.py` 的 `staff_query(role)` / `can_view(n, role)` 手上没有
    User 对象（它们被模板、待签统计、详情页子件共用，签名不能改），
    但又必须和 ABAC 走同一张规则表，于是套这个壳。

    `status` 必须是 "active"：空串会命中 R1（账号状态），
    那样所有调用方都会被判成"停用账号"，公告页会整片变空——
    而 R1 的本意是拦"真的停用的人"，不是拦"我没传 status"。
    """

    __slots__ = ("role", "org", "status", "name")

    def __init__(self, role, org=""):
        self.role = role or ""
        self.org = org or ""
        self.status = "active"
        self.name = ""

    def perms(self):
        return set()


# 规则清单（给 /security/policy 页面与文档共用）。
# 与 `decide()` 里的实现一一对应——**改规则必须同时改这里**，
# 否则页面上写的是旧规则，代码跑的是新规则，比没有说明更糟。
RULES = [
    ("R1", "账号状态",
     "停用 / 待审核的账号只有登录态，不进任何业务数据。放在最前面，"
     "因为它比「角色有没有权限」更根本——角色对了也没用。"),
    ("R2", "居民端密级", "居民端通道只出「公开」级内容，密级 ≥2 一律拒。"),
    ("R3", "跨单位", "资源归属单位与自己单位不同时，仅街道与管理员可看。"),
    ("R4", "密级上限", "资源密级高于角色可见上限时拒。"),
    ("R5", "公告范围与状态", "scope 不含本人角色、或资源未发布时拒。"),
    ("R6", "导出归属", "密级 ≥3 的文件只有归属单位与监督方能导出；"
                       "导出一律强制水印 + 审计。"),
    ("R7", "默认允许", "未命中任何限制规则时放行（规则表是黑名单式收窄）。"),
]


# ---------------------------------------------------------------- 规则表
#
# 顺序即优先级：先命中先生效。**"拒"的规则必须排在"允许"前面**，
# 否则 R7 默认允许会兜住一切，前面的规则永远不生效——这类 bug 不报错，
# 只是"看起来配了规则，实际全放开"。


def decide(user, action, kind, secret=0, scope="all", owner_org="",
           channel="internal", status="published"):
    """单次访问决策。

    action   : read / list / export / write / judge
    kind     : order / task / notice / doc / evidence / ledger / fde
    secret   : 整数密级（调用方用 secret_num() 从中文转换）
    scope    : all / community / property（公告可见范围）
    owner_org: 资源归属单位；空串 = 不按单位收窄（居民诉求这类公共资源）
    channel  : internal（内部站，须登录）/ portal（居民端，匿名）
    status   : 资源状态（published / draft / archived）
    """
    s = _subject(user)
    attrs = {
        "role": s["role"], "org": s["org"], "status": s["status"],
        "action": action, "kind": kind, "secret": secret,
        "secret_label": SECRET_LABEL.get(secret, "?"),
        "scope": scope, "owner_org": owner_org,
        "channel": channel, "res_status": status,
    }

    # R1 账号状态：停用/待审核的账号只有登录态，不进任何业务数据。
    # 放在最前面，因为它比"角色有没有权限"更根本——角色是对的也没用。
    if channel == "internal" and s["status"] not in ("active",):
        d = _deny("R1 账号状态", f"账号状态为「{s['status'] or '未知'}」，请先通过审核或联系管理员")
        d.attrs = attrs
        return d

    # R2 居民端通道只出公开级。
    # 现在门户接口没碰密级字段，这条是**防未来**：哪天居民端要显示办件进度里
    # 的"处理意见"，那段文字可能是内部·敏感的。通道属性在这里钉死，
    # 就不用指望每个新接口作者都记得判一次。
    if channel == "portal" and secret >= 2:
        d = _deny("R2 居民端密级", "该内容不属于可对居民公开的范围")
        d.attrs = attrs
        return d

    # R3 横向越权：别家单位的数据。
    # 这是本项目最容易踩的一条——工单号可枚举（W+日期+序号），
    # 只按 id 直取就绕过列表过滤，形成 IDOR。
    org = _own_org(user)
    if owner_org and org and owner_org != org:
        if s["role"] not in OVERSEER_ROLES:
            d = _deny("R3 跨单位",
                      f"该记录归属「{owner_org}」，与你所属的「{org}」不同；"
                      f"跨单位查看仅限街道与管理员的监督口径")
            d.attrs = attrs
            return d

    # R4 密级上限
    ceiling = ROLE_SECRET_CEILING.get(s["role"], 1)
    if secret > ceiling:
        d = _deny("R4 密级上限",
                  f"该内容密级为「{SECRET_LABEL.get(secret, '?')}」，"
                  f"你的角色「{s['role'] or '未登录'}」可见上限为"
                  f"「{SECRET_LABEL.get(ceiling, '公开')}」")
        d.attrs = attrs
        return d

    # R5 公告范围：scope 是资源属性，与角色求交。
    # 「all + 已发布」是所有登录用户的公共交集；draft 只有作者本人能看
    # （作者校验在调用方，因为那是"人"的属性不是"角色"的属性）。
    if kind in ("notice", "doc"):
        if status != "published" and action in ("read", "list", "export"):
            d = _deny("R5 资源状态", "该文件尚未发布，不能对当前账号开放")
            d.attrs = attrs
            return d
        if (scope or "all") not in ("all", s["role"]):
            d = _deny("R5 公告范围",
                      f"该文件的可见范围是「{scope}」，不包含你的角色")
            d.attrs = attrs
            return d

    # R6 导出：比"看"更严重的一档。
    # 文件一旦下载就脱离系统，拦不住二次传播。这里做两件事：
    #   ① 密级 ≥3 的导出件，只给归属单位本人与监督方
    #   ② 所有导出强制水印 + 强制审计（水印里带姓名，截图传出去能追到人）
    # 注意：**不给导出单独设密级上限**。"能看不能导"的结果是用户截屏，
    # 反而失去了水印与审计这两条线索——不如让它导，但让痕迹跟着走。
    if action == "export":
        if secret >= 3 and owner_org and org != owner_org and s["role"] not in OVERSEER_ROLES:
            d = _deny("R6 导出归属", "敏感文件的导出仅限归属单位与监督方")
            d.attrs = attrs
            return d
        d = _allow("R6 导出", "允许导出，已强制加水印并记审计",
                   watermark=True, log=True, mask=secret >= 2)
        d.attrs = attrs
        return d

    # R7 默认允许
    d = _allow("R7 默认", "未命中限制规则")
    d.attrs = attrs
    return d


def can_see(user, action, kind, **kw):
    """只要 bool 的简写。给条件渲染、`if not ...: abort(403)` 这类场景用。"""
    return decide(user, action, kind, **kw).allow


def readable_secret_levels(role):
    """该角色可读的密级**字符串**集合，供列表 SQL filter 用。

    为什么不能把 `decide()` 直接塞进 SQL：密级在库里是中文串，
    `secret > ceiling` 这种比较在 SQL 里做不了（中文字符串比大小是字节序，
    "内部" > "公开" 恰好成立但"内部·敏感" vs "秘密" 就乱了）。
    所以列表侧必须翻译成 `IN (...)`，而这个翻译必须与 `decide()` 用**同一个**
    `ROLE_SECRET_CEILING`——两处各写一份上下限，就是本项目反复出事的那种漏。
    """
    ceiling = ROLE_SECRET_CEILING.get(role, 1)
    return [k for k, v in SECRET_NUM.items() if v <= ceiling]


# ---------------------------------------------------------------- 行级收窄
#
# 决策是"单条能不能看"，列表要的是"哪几行能看"。两者必须同源：
# 卡片数字、列表条数、导出内容三处口径打架，是这个项目里最顽固的一类 bug
# （见 docs/14 统计口径）。所以列表一律走这里，不再各自写 filter。


def scope_query(kind, base, user, action="list", role_override=""):
    """按 ABAC 规则收窄查询。**只收窄，不放宽**。

    返回 (query, decision)。decision 恒为 allow=True——
    被规则挡住的情况在外层已经返回 403 了，走到这里说明有读权限，
    剩下的问题是"读哪几行"。

    `role_override` 给"手上只有角色字符串、没有 User 对象"的调用方用
    （`notice/ingest.py:staff_query(role)` 就是这样：它被模板、待签统计、
    详情页子件三处共用，签名不能动）。没有这个参数，那三处就会继续
    各写一份 `scope.in_(["all", role])`，与本文件漂移。
    """
    s = _subject(user)
    role, org = s["role"], s["org"]
    if role_override:
        role = role_override

    if kind in ("order", "evidence"):
        # 物业只看自己单位的单。白名单写法：受派方只有 property 一个角色，
        # 写成 `role != "street"` 会把 admin 也收窄成 property_co == ""，
        # 管理员工单页恒空（这个坑真踩过，见 docs/26 红线 1）。
        if role == "property" and org:
            base = base.filter_by(property_co=org)

    elif kind == "task":
        # 非街道角色只给派给自己的任务。单位为空的任务是"发给全体的"，
        # 所以 assignee_org == org 之外还要放行 NULL 与空串——
        # 漏了这两条，补列前建的老任务会从受派方列表里凭空消失。
        if role in ("community", "property"):
            base = base.filter(Task.assignee_role == role)
            if org:
                base = base.filter(db.or_(Task.assignee_org == org,
                                          Task.assignee_org.is_(None),
                                          Task.assignee_org == ""))

    elif kind in ("notice", "doc"):
        # 已发布 + scope 允许它看。draft 不在这里排除的理由：
        # 发文台的核稿人必须能看到别人的草稿，作者/核稿校验在 API 层做
        # （那是"人"的属性）。所以列表收窄只做 scope，状态由调用方传 filter。
        # `["all", role]` 里的 role 可能为空串（未登录/居民端），
        # 保留空串是为了与改造前 `scope.in_(["all", role])` 逐字一致——
        # 老数据里 scope 为空的公告当初是能被这类角色看到的，
        # 顺手"修正"成只认 "all" 会让它们凭空消失。
        base = base.filter(Notice.scope.in_(["all", role]))

    return base, _allow("scope_query", "按主体属性收窄")
