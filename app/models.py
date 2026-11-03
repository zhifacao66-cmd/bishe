"""数据库模型：对应《总体设计》第五节"""
from datetime import datetime

from sqlalchemy import event

from app.extensions import db


class FdeRecord(db.Model):
    """FDE 首录原始记录"""
    __tablename__ = "fde_record"
    id = db.Column(db.Integer, primary_key=True)
    raw_text = db.Column(db.Text, nullable=False)
    source = db.Column(db.String(16), default="text")  # text/voice/image
    creator = db.Column(db.String(32), default="")
    created_at = db.Column(db.DateTime, default=datetime.now)
    items = db.relationship("WorkItem", backref="fde", lazy=True)


class WorkItem(db.Model):
    """业务项（拆解后的最小业务单元）"""
    __tablename__ = "work_item"
    id = db.Column(db.Integer, primary_key=True)
    fde_id = db.Column(db.Integer, db.ForeignKey("fde_record.id"))
    type = db.Column(db.String(32))          # 报修/人口变动/走访/调解...
    subject = db.Column(db.String(128))      # 事项摘要
    fields_json = db.Column(db.Text)         # 结构化字段(JSON)
    target_lines = db.Column(db.String(64))  # 去向业务线：综治/民政/物业...
    status = db.Column(db.String(16), default="pending")  # pending/done
    created_at = db.Column(db.DateTime, default=datetime.now)


class FieldSecurity(db.Model):
    """敏感字段标记：level 1明文/2脱敏/3加密"""
    __tablename__ = "field_security"
    id = db.Column(db.Integer, primary_key=True)
    work_item_id = db.Column(db.Integer, db.ForeignKey("work_item.id"))
    field = db.Column(db.String(64))
    level = db.Column(db.Integer, default=2)


class WorkOrder(db.Model):
    """物业工单（内容已脱敏）"""
    __tablename__ = "work_order"
    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(32), unique=True)
    work_item_id = db.Column(db.Integer, db.ForeignKey("work_item.id"))
    property_co = db.Column(db.String(64))
    content = db.Column(db.Text)
    sla_hours = db.Column(db.Integer, default=24)
    sla_level = db.Column(db.String(8), default="")   # 紧急/较急/一般（依知识库分级）
    status = db.Column(db.String(16), default="open")  # open/doing/done
    feedback = db.Column(db.Text)
    # 处理人：能追责的只有"人"。
    # `property_co` 是单位，一个物业公司有好几个维修工，光有单位等于只知道
    # "物业办的"，出了事找不到具体是谁。这里存姓名快照 + 账号 id：
    # 姓名快照保住"当时是谁"，账号 id 保住"改名/停用之后还能对上同一个人"。
    assignee = db.Column(db.String(32))
    assignee_id = db.Column(db.Integer)
    assigned_at = db.Column(db.DateTime)   # 接单时刻，处理时长从这里算
    done_at = db.Column(db.DateTime)       # 办结时刻

    # ---------------------------------------------------------------- 争议与裁决
    #
    # 三方治理里，"办完"和"办对"是两件事。物业点了办结，居民可能不认，
    # 居委会要判的是**这笔钱谁出、这件事谁干**，不是一个满意度星级。
    #
    # 为什么裁决字段挂在工单上而不是居民诉求上：
    #   一张诉求会拆成多个业务项、派给不同单位（物业修灯、民政核低保）。
    #   挂在诉求上，就只能整单判一次，等于"因为灯没修好，低保复核也不算数"。
    #   争议永远针对**具体某张单**，所以落在这里。
    dispute_type = db.Column(db.String(16), default="")   # 责任归属/费用承担/质量不达标/超期未办
    verdict = db.Column(db.String(16), default="")        # 裁决结论，见 app/arbitration.py:VERDICTS
    verdict_basis = db.Column(db.String(256), default="") # 依据（合同条款/维修资金办法），必须写
    verdict_by = db.Column(db.String(32), default="")
    verdict_at = db.Column(db.DateTime)
    # 居民确认：'' 未表态 / ok 已解决 / disputed 不服
    # 注意它和 `status` 是两条独立的线——工单可以 done 而居民 disputed，
    # 这正是需要仲裁的状态，不能因为 done 就把 disputed 盖掉。
    confirm_status = db.Column(db.String(16), default="")
    confirm_at = db.Column(db.DateTime)
    confirm_note = db.Column(db.String(512), default="")

    created_at = db.Column(db.DateTime, default=datetime.now)


class WorkOrderLog(db.Model):
    """工单流转记录（只追加，不改历史）

    为什么不把上面那三个字段换成"在工单上多堆几列"就算完：
    一张单会换手——交接班、请假转派、两人分头办。字段只能记住"最后一个"，
    而追责要的恰恰是"每一步是谁、什么时候、把单子推到了哪一步"。
    所以按事件追加，历史行永不 update，删工单也不删记录。

    `order_no` 冗余存一份：查流转时不必 join 工单表，工单被归档后照样能查。
    """
    __tablename__ = "work_order_log"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("work_order.id"), index=True)
    order_no = db.Column(db.String(32))
    action = db.Column(db.String(16))       # dispatch/accept/transfer/finish
    from_status = db.Column(db.String(16))
    to_status = db.Column(db.String(16))
    actor = db.Column(db.String(32))        # 操作人姓名（给页面看）
    actor_id = db.Column(db.Integer)        # 账号 id（改名/停用后仍可对上）
    actor_org = db.Column(db.String(64))
    detail = db.Column(db.Text)             # 反馈内容 / 转派说明
    created_at = db.Column(db.DateTime, default=datetime.now, index=True)


class Ledger(db.Model):
    """台账"""
    __tablename__ = "ledger"
    id = db.Column(db.Integer, primary_key=True)
    work_item_id = db.Column(db.Integer, db.ForeignKey("work_item.id"))
    line = db.Column(db.String(32))  # 综治/民政/卫健...
    content_json = db.Column(db.Text)
    # 供关键词检索的纯文本：content_json 里**所有叶子值**拼成的空格分隔串（不含键名）。
    #
    # 为什么不直接 LIKE content_json：那是 JSON 序列化后的文本，键名也参与匹配。
    # 台账的 JSON 里有 "room_key"、"type"、"source" 这些键，于是搜一个下划线
    # 或搜 "type" 会把**整本台账**都捞出来——有结果、有分页、看不出异常，
    # 用户只会以为"库里全都相关"。更隐蔽的是搜 "_"：修复前命中的是键名里的
    # 下划线（room_key），跟居民写的内容毫无关系。
    # 落成一列纯文本后，LIKE 的语义回到"内容里含这个词"，与用户预期一致。
    search_text = db.Column(db.Text)
    # 是否复用了历史档案（免重复采集）。
    # 这个标记原本只写在 content_json 里，统计时得把整张表读出来反序列化，
    # 而且报表和台账页各扫一遍——两处口径迟早会漂。落成真实列后可以一条
    # COUNT 出结果，页面和报表用同一个来源。
    reused = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)


@event.listens_for(Ledger, "before_insert")
@event.listens_for(Ledger, "before_update")
def _sync_ledger_search_text(mapper, conn, target):
    """写入/更新台账时自动重建 search_text。

    放在 ORM 事件里而不是调用方，是为了不重演"同一份事实写两处、迟早会漂"
    那类问题：全站建 Ledger 的地方有业务代码、迁移脚本、演示数据、11 套回归，
    靠人工在每个 `Ledger(...)` 里补一个参数，迟早漏一处，而漏掉的表现是
    "这条台账搜不到"——有结果、不报错，最难发现的那种。
    事件是唯一入口：只要经过 ORM，就一定同步。
    """
    if target.content_json:
        from app.search import content_text       # 延迟导入：search 依赖 models
        target.search_text = content_text(target.content_json)


class Report(db.Model):
    """报表（街道上报用，脱敏汇总）

    一个 (period, line) 只保留**一条**记录，重新生成 = 覆盖并升版本。
    早先是直接 insert，于是同一个月份被点几次就有几份报表堆在列表里，
    导出件还各带一个不同的编号——归档件因此失去"一期一份"的证据效力。

    被覆盖的旧版本不丢，落到 `report_revision`（只追加）。两件事因此同时成立：
      · 列表里一期只有一份（归档件唯一，导出编号唯一）
      · 历史上每一版都还在（能回滚、能举证"当时报上去的是什么数"）
    `version` 单调递增，回滚也**不重号**——回滚是"用旧内容生成新一版"，
    所以回滚后版本号继续往上走，`rollback_from` 记着它抄的是第几版。
    """
    __tablename__ = "report"
    id = db.Column(db.Integer, primary_key=True)
    period = db.Column(db.String(16))   # 如 2026-10
    line = db.Column(db.String(32), default="街道")
    version = db.Column(db.Integer, default=1, nullable=False)  # 第几版（重新生成 +1）
    data_json = db.Column(db.Text)
    generated_at = db.Column(db.DateTime, default=datetime.now)
    # 本版内容来自哪一版（只在对某版回滚后非空）。不覆盖版本号，
    # 否则"第 2 版"会在库里指向两份不同内容，归档编号就失去意义了。
    rollback_from = db.Column(db.Integer)


class ReportRevision(db.Model):
    """报表历史版本（只追加，不修改）

    每次覆盖或回滚，把**被替掉的那一版**整份挪进来。报表是口径证据：
    数字后来被改过，必须能回答"改之前报的是什么"。只留 version + audit_log
    不够——审计里存的是摘要（前 250 字），拿它还原不出一份完整的月报。
    """
    __tablename__ = "report_revision"
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.Integer, index=True)   # 指向 report.id（不设外键，避免删报表时被历史挡住）
    period = db.Column(db.String(16))
    line = db.Column(db.String(32))
    version = db.Column(db.Integer)                 # 这一版自己的版本号
    data_json = db.Column(db.Text)                  # 整份快照，原样保存
    generated_at = db.Column(db.DateTime)           # 当初生成时刻（不是归档时刻）
    archived_at = db.Column(db.DateTime, default=datetime.now)
    reason = db.Column(db.String(32))               # regenerate / rollback
    actor = db.Column(db.String(32), default="")    # 谁触发的覆盖或回滚


class Task(db.Model):
    """任务（街道下发，督办对象）
    街道 → 社区 / 物业 的任务派发，是"上下级业务关系"的落地载体。
    """
    __tablename__ = "task"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(128))
    assignee_role = db.Column(db.String(32))  # community/property/street
    assignee_org = db.Column(db.String(64))   # 受派组织（幸福里社区 / 幸福里物业）
    detail = db.Column(db.Text)               # 任务说明（下发时一并交代口径）
    deadline = db.Column(db.DateTime)
    status = db.Column(db.String(16), default="open")     # open/done
    creator = db.Column(db.String(32), default="")        # 下发人
    created_at = db.Column(db.DateTime, default=datetime.now)
    closed_at = db.Column(db.DateTime)


class AuditLog(db.Model):
    """审计日志（只追加）"""
    __tablename__ = "audit_log"
    id = db.Column(db.Integer, primary_key=True)
    actor = db.Column(db.String(32))   # 操作者或 Agent 名
    agent = db.Column(db.String(32))
    action = db.Column(db.String(64))
    input_digest = db.Column(db.String(256))
    output_digest = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.now)


class KbDoc(db.Model):
    """知识库文档（政策文件 / 办事指南 / 表格模板 / 工作规范）"""
    __tablename__ = "kb_doc"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(128), unique=True)
    category = db.Column(db.String(32), default="政策文件")
    source = db.Column(db.String(128), default="")   # 文件来源/文号
    content = db.Column(db.Text, nullable=False)
    tags = db.Column(db.String(128), default="")
    created_by = db.Column(db.String(32), default="")
    created_at = db.Column(db.DateTime, default=datetime.now)


class Notice(db.Model):
    """通知公告，同时承担「收文」与「发文」两端的载体

    两个维度必须分开看，混在一起会讲不清：

    **发布状态 status**（居民端能不能看见）
      pending   字段不全或尚未确认，等社区补全/确认后再发布
      published 已发布，居民端与内部端可见
      offline   已下架（仍保留记录，便于追溯"曾经发过什么"）

    **收文状态 recv_status**（社区办没办这个文）
      ""          不涉及收文（社区自己原创的通知、居民端发布的落地件）
      dispatched  街道已投递，社区还没签收  ← 社区工作台的"待办"
      received    社区已签收，留了签收人与时间

    这两件事解耦是有意的：上级原文本来就该公开，公开不必等签收；
    而签收证明的是"社区知道要办"，属于内部留痕，不该影响居民看不看得见。

    kind 区分文档来源：
      superior  上级来文（街道/区局下发，社区是收文方）
      local     社区落地件（依据某份上级来文生成的本社区执行通知）
      original  社区原创（无上级依据，直接发布）
    """
    __tablename__ = "notice"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    issuer = db.Column(db.String(64), default="")      # 发文单位（上级单位名称）
    doc_no = db.Column(db.String(64), default="")      # 发文字号，如 幸街发〔2026〕18号
    issued_at = db.Column(db.Date)                     # 成文日期
    category = db.Column(db.String(32), default="通知公告")
    level = db.Column(db.String(16), default="街道")    # 来源层级：街道/社区/物业
    scope = db.Column(db.String(32), default="all")    # 发布范围 all/community/property
    body_md = db.Column(db.Text, default="")           # 正文（Markdown）
    summary = db.Column(db.String(256), default="")    # 摘要，列表页与推送用
    status = db.Column(db.String(16), default="pending")
    source_file = db.Column(db.String(160), default="")  # 来源文件名
    source_hash = db.Column(db.String(64), default="")   # 文件内容哈希，去重
    parse_note = db.Column(db.String(256), default="")   # 解析说明/待确认项
    pinned = db.Column(db.Boolean, default=False)      # 置顶
    # 置顶时间：多条同时置顶时按它排先后（后置顶的在上），
    # 只靠布尔值的话几条置顶会退化成"按发稿时间排"，后置顶的反而排后面
    pinned_at = db.Column(db.DateTime)
    created_by = db.Column(db.String(32), default="system")
    created_at = db.Column(db.DateTime, default=datetime.now)
    published_at = db.Column(db.DateTime)

    # ---- 收文流转（本轮新增）
    kind = db.Column(db.String(16), default="superior")   # superior/local/original
    parent_id = db.Column(db.Integer)                     # 落地件 → 上级原文 id
    receiver_org = db.Column(db.String(64), default="")   # 收文单位，空=全体社区
    recv_status = db.Column(db.String(16), default="")    # ""/dispatched/received
    received_by = db.Column(db.String(32), default="")
    received_at = db.Column(db.DateTime)
    deadline = db.Column(db.Date)                         # 上级要求完成时限

    # ---- 公文属性（P0：把「通知公告」扩成能承载完整公文）
    #
    # direction 与 kind 不能合并：
    #   kind 说的是"这份文件从哪来"（上级下发 / 本社区落地 / 原创）
    #   direction 说的是"这份文件往哪去"（收文 / 发文 / 内部留存）
    # 一份 direction=out 的请示，kind 可以是 original（社区自己拟的）。
    direction = db.Column(db.String(8), default="in")       # in/out/inner
    doc_type = db.Column(db.String(16), default="通知")     # 通知/请示/报告/函/纪要/通报/决定/公告
    doc_status = db.Column(db.String(16), default="")       # 发文状态，见 app/docflow.py
    # 密级标识，不是"定密"：居委会无法定定密权，只能继承上级来文的密级，
    # 对自己产生的材料标"公开/内部/内部·敏感"。取值见 app/docflow.py:SECRET_LEVELS
    secret_level = db.Column(db.String(16), default="")
    secret_until = db.Column(db.Date)                       # 保密期限
    urgency = db.Column(db.String(8), default="平件")        # 平件/急件/特急
    to_org = db.Column(db.String(128), default="")          # 主送机关
    cc_org = db.Column(db.String(128), default="")          # 抄送机关
    signer = db.Column(db.String(32), default="")           # 签发人（上行文必填）
    handler = db.Column(db.String(32), default="")          # 承办人（收文）
    handled_at = db.Column(db.DateTime)
    handle_result = db.Column(db.Text, default="")          # 办理结果
    attachments = db.Column(db.Text, default="")            # 附件清单 JSON 数组
    seal_by = db.Column(db.String(32), default="")          # 用印登记
    sealed_at = db.Column(db.DateTime)
    archived_at = db.Column(db.DateTime)
    doc_year = db.Column(db.Integer)                        # 文号年份
    doc_seq = db.Column(db.Integer)                         # 文号序号（同年代字内递增）


class OrgContact(db.Model):
    """三级管理者信息公开：居民端"找谁办事"用

    这张表就是居民端"查看三级管理者信息"的数据源——
    公开的是单位、职责、电话、地址、服务时间，不含任何个人敏感信息。
    """
    __tablename__ = "org_contact"
    id = db.Column(db.Integer, primary_key=True)
    level = db.Column(db.String(16), default="community")  # street/community/property
    name = db.Column(db.String(64), nullable=False)        # 单位名称
    duty = db.Column(db.String(256), default="")           # 主要职责
    phone = db.Column(db.String(32), default="")           # 对外公开电话（非个人手机）
    address = db.Column(db.String(128), default="")
    service_hours = db.Column(db.String(64), default="工作日 9:00-17:00")
    sort = db.Column(db.Integer, default=0)
    active = db.Column(db.Boolean, default=True)


class ResidentTicket(db.Model):
    """居民提交凭据：免注册身份的最小实现

    设计取舍：不建"居民账号表"，只发一张"查询码"。
    居民不需要记密码，社区也拿不到居民的任何账号信息；
    手机号只用于联系核实，页面展示时一律打码。
    """
    __tablename__ = "resident_ticket"
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(8), unique=True, nullable=False)  # 6 位查询码
    phone = db.Column(db.String(16), nullable=False)             # 用于双因子查询
    name = db.Column(db.String(32), default="")
    address = db.Column(db.String(128), default="")
    raw_text = db.Column(db.Text, nullable=False)
    fde_id = db.Column(db.Integer, db.ForeignKey("fde_record.id"))
    item_count = db.Column(db.Integer, default=0)     # 拆出几个业务项
    line_summary = db.Column(db.String(128), default="")  # 涉及业务线，如 "物业/民政"
    status = db.Column(db.String(16), default="open")  # open/doing/done
    # 证据附件：JSON 数组 [{name, sha256, size, mime, stored}]
    # 只存哈希与随机文件名，不存原文件名——原名可能叫"3栋201身份证.jpg"，
    # 落盘后一旦目录被列，等于把门牌和证件类型一起送出去。
    attachments = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now)


class EvidenceChain(db.Model):
    """证据哈希链（只追加，永不改历史行）

    做的是"**可验证未被篡改**"，不是"不可篡改"——这个区别必须说清：
      · 能防：应用层有人改了诉求原文、改了办结反馈、删了一条流转记录。
        任何一处改动都会让该条 hash 与后续 prev_hash 对不上，`verify()` 立刻报断点。
      · 不能防：直接拿 sqlite 文件重算整条链。所以真要用在争议上，
        链头哈希必须**外锚**——每天把链头写进审计、同步给街道留底（见 app/arbitration.py:anchor）。
    把这点讲明白，比声称"区块链防篡改"更可信，也才经得住追问。

    为什么自己写而不是上区块链：单机 SQLite、每天几十条记录，
    引入链平台等于给 2C2G 的服务器加一个必须常驻的外部依赖，
    可用性反而下降；而这里要的只是"事后能证明没改过"，链式哈希就够了。
    """
    __tablename__ = "evidence_chain"
    id = db.Column(db.Integer, primary_key=True)
    # 全局单调序号。**不能靠 id 排序**：SQLite 的 AUTOINCREMENT 在删除后会复用
    # rowid，而链的顺序一旦乱了，验证就会在最无辜的地方失败。
    seq = db.Column(db.Integer, unique=True, nullable=False, index=True)
    target_type = db.Column(db.String(16), nullable=False)  # ticket/order
    target_id = db.Column(db.Integer, nullable=False, index=True)
    action = db.Column(db.String(24), nullable=False)       # submit/attach/finish/reject/...
    payload = db.Column(db.Text, default="")                # 规范化 JSON（字段快照）
    prev_hash = db.Column(db.String(64), default="")        # 创世条为空串
    hash = db.Column(db.String(64), nullable=False)
    actor = db.Column(db.String(32), default="")
    created_at = db.Column(db.DateTime, default=datetime.now)


class ChainAnchor(db.Model):
    """链头锚点：把"某一刻的链头哈希"钉在业务表之外

    只存链头没用——改库的人可以连链头一起改。所以每次锚定都把
    `(seq, hash, 当时总条数)` 写一份到这里，并且**同步写一条 audit_log**。
    审计日志另有导出与备份路径（scripts/backup_db.py 每天 02:00），
    两条线同时被改的概率远低于改一条。
    """
    __tablename__ = "chain_anchor"
    id = db.Column(db.Integer, primary_key=True)
    seq = db.Column(db.Integer, default=0)
    head_hash = db.Column(db.String(64), default="")
    total = db.Column(db.Integer, default=0)
    note = db.Column(db.String(128), default="")
    created_at = db.Column(db.DateTime, default=datetime.now)


class User(db.Model):
    """账号：角色给默认权限，管理员可按人增减（perms_json）

    三级权限来源，优先级从高到低：
      1. role == "admin"            → 全部权限点（不可被降权，否则会把管理员自己锁在门外）
      2. status != "active"         → 无权限（待审核/停用的账号只有登录态，进不了任何业务页）
      3. perms_json 非 NULL         → 以用户级勾选为准（这就是"给新员工点权限"的落地处）
      4. 否则                       → 跟随 ROLE_PERMS[role]

    perms_json 用 NULL / "[]" 区分两种语义，这个区别很关键：
      NULL  = 从没单独配过，跟随角色默认（老账号批量升级后都落在这里）
      "[]"  = 管理员明确一个都不给，等于"先挂号、不给活"
    """
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32))      # 登录名（唯一，代码层保证）
    name = db.Column(db.String(32))          # 显示名，如“王社工”
    role = db.Column(db.String(16))          # community/street/property/admin
    org = db.Column(db.String(64))            # 所属社区/物业公司/街道
    password_hash = db.Column(db.String(128))
    status = db.Column(db.String(16), default="active")   # pending/active/disabled
    perms_json = db.Column(db.Text)          # 用户级权限点 JSON 数组，NULL=跟随角色
    phone = db.Column(db.String(16), default="")          # 仅用于管理员联系核实，不对外展示
    created_by = db.Column(db.String(32), default="")     # 自助注册 / 管理员创建
    review_note = db.Column(db.String(128), default="")   # 审核备注（拒绝原因等）
    last_login_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.now)

    def __repr__(self):
        return f"<User {self.username}:{self.role}:{self.status}>"


class DocLog(db.Model):
    """公文流转记录（只追加，不改历史）

    和 work_order_log 一个模式：状态变更与留痕在同一个入口完成
    （app/docflow.py:push），想漏都漏不掉。

    为什么不复用 audit_log：
      审计记的是"谁对系统做了什么"（含登录、越权、导出），是安全视角；
      流转记的是"这份公文走到哪一步了"，是业务视角。两者查询口径、
      保留期限、可见角色都不同——审计只有街道和 admin 能看，
      而承办人必须能看见自己经手的文走过哪些环节。
    """
    __tablename__ = "doc_log"
    id = db.Column(db.Integer, primary_key=True)
    notice_id = db.Column(db.Integer, nullable=False, index=True)
    doc_no = db.Column(db.String(64), default="")
    action = db.Column(db.String(16), nullable=False)
    from_status = db.Column(db.String(16))
    to_status = db.Column(db.String(16))
    actor = db.Column(db.String(32), default="")
    actor_id = db.Column(db.Integer)
    note = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.now)


class Weekly(db.Model):
    """工作周报

    body_md 是"给人看的稿"，data_json 是"给机器核的数"。
    两者必须同源生成：正文里的"本周办结 12 件"和 data_json.counts 里的 12
    出自同一次取数，否则点开明细会少两件（docs/14 的教训）。

    status：
      draft 草稿，可改正文
      final 已签发，正文冻结；要改只能出 revision+1 的新行（历史不覆盖）
    """
    __tablename__ = "weekly"
    id = db.Column(db.Integer, primary_key=True)
    period = db.Column(db.String(16), nullable=False)   # 2026-W43
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    data_json = db.Column(db.Text, default="")          # 取数快照（含明细 id）
    body_md = db.Column(db.Text, default="")
    status = db.Column(db.String(8), default="draft")
    editor = db.Column(db.String(32), default="")       # 最后改稿人
    signed_by = db.Column(db.String(32), default="")
    signed_at = db.Column(db.DateTime)
    revision = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, default=datetime.now)


class AgentStep(db.Model):
    """Agent 执行轨迹：一次诉求处理 = 一个 run_id，每步一行

    **为什么另开一张表而不是塞进 audit_log**
    audit_log 是合规账本：actor/agent/action + 两段 250 字的 digest，
    目的是"证明这件事发生过、发生过什么"。它刻意不存中间推理——
    存了就要考虑那段内容本身会不会泄露个人信息，审计表就成了新的泄露面。

    轨迹表要的恰好是中间过程：每个 Agent 各自给了什么结论、依据是什么、
    谁跟谁不一致、最后采纳了谁。它和 audit_log 的关系是"轨迹给答辩看，
    审计给监管看"，两者的保留期、脱敏要求、访问权限都不一样，不该同表。

    **为什么行数会涨得比业务快**
    一条诉求进来至少 4 步（脱敏 / 拆解 / 定级 / 填报），一个 item 多两步
    （风险复核 / 消解）。41 张单产生上千行是常态。所以：
      · run_id 和 fde_id 都建索引，页面按 run 取而不是全表扫；
      · 结论字段截断到 200 字（reason 400 字），不让单个 Agent 的
        长输出把表撑爆；
      · 不做级联删除——FDE 被删了轨迹仍留着，它是"当时系统怎么想的"
        的唯一记录。
    """
    __tablename__ = "agent_step"
    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.String(32), nullable=False, index=True)
    seq = db.Column(db.Integer, default=0)
    agent = db.Column(db.String(16), default="")     # security/parser/dispatch/risk/fill/resolve
    action = db.Column(db.String(24), default="")    # mask/parse/dispatch/review/resolve
    decision = db.Column(db.String(200), default="") # 这一步给出的结论
    reason = db.Column(db.String(400), default="")   # 依据（可解释性全靠它）
    topic = db.Column(db.String(24), default="")     # 参与消解的议题；空表示只是过程记录
    took_ms = db.Column(db.Integer, default=0)
    degraded = db.Column(db.Boolean, default=False)  # 是否降级到规则桩
    fde_id = db.Column(db.Integer, index=True)
    work_item_id = db.Column(db.Integer)
    order_no = db.Column(db.String(32), default="")
    created_at = db.Column(db.DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id, "run_id": self.run_id, "seq": self.seq,
            "agent": self.agent, "action": self.action,
            "decision": self.decision, "reason": self.reason,
            "topic": self.topic, "took_ms": self.took_ms,
            "degraded": bool(self.degraded),
            "fde_id": self.fde_id, "work_item_id": self.work_item_id,
            "order_no": self.order_no or "",
            "at": self.created_at.strftime("%H:%M:%S") if self.created_at else "",
            "at_full": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else "",
        }


class Person(db.Model):
    """人员底册（街道下发的重点人员台账）。

    为什么单独建表、不塞进 WorkItem：
    这张表是**人**，一人一条，按年更新，没有"办理"这个动作。WorkItem 是"一件事"，
    有 open→doing→done 的流转。混在一起会让工单列表从 41 条涨到 2885 条，
    而且这些记录永远停在 open —— 看板、超时告警、SLA 统计全部失真。

    脱敏口径（这条是红线，不能松）：
    - 身份证**不存明文**，只存 `id_hash`（sha256 前 32 位，用作去重键）
      与 `id_masked`（前 6 后 4，中间 8 个 *）。原表 4000+ 条真实身份证，
      一旦明文入库，备份、导出、审计日志、答辩投屏全是泄露面。
    - 姓名存 `name_masked`（张*），同事要认人时按"姓 + 楼栋 + 组"组合定位。
    - 电话存 `guardian_phone`（138****8000）。

    `addr_level` 是**如实记录定位精度**，不是补齐用的：
    实测防溺水表只有 35.6% 能到"栋+单元+门号"，53.7% 只写到"水城区"。
    界面必须显示"该条仅定位到街道"，不能让人以为每条都能精确到户。
    """
    __tablename__ = "person"
    id = db.Column(db.Integer, primary_key=True)
    # 去重键：身份证 sha256。原表内部有 164 个身份证重复（多 166 行）、
    # 残疾人表与防溺水表之间另有 15 人重叠。**只能靠身份证去重**——
    # 同名同姓在同一社区并不罕见，按姓名合并会把人并错。
    id_hash = db.Column(db.String(40), unique=True, index=True)
    name_masked = db.Column(db.String(40), default="")
    id_masked = db.Column(db.String(30), default="")
    gender = db.Column(db.String(4), default="")
    age = db.Column(db.Integer)
    # 行政区划三级：街道 / 社区 / 组。community 允许为空——
    # 空表示"台账里没写清属于哪个社区"，只有街道角色能看到，社区角色看不到。
    street = db.Column(db.String(40), default="", index=True)
    community = db.Column(db.String(40), default="", index=True)
    grp = db.Column(db.String(40), default="")          # 繁荣组 / 陈家寨组
    address = db.Column(db.String(200), default="")
    # 0=认不出 1=只有栋/组 2=栋+单元 3=栋+单元+门号
    addr_level = db.Column(db.Integer, default=0, index=True)
    addr_mid = db.Column(db.String(60), default="")     # 10栋1单元
    addr_room = db.Column(db.String(20), default="")    # 302
    # 风险等级：防溺水用 高中低；残疾人用 一级~四级。两者不同源，故为自由文本，
    # 但排序权重统一由 PersonTag.risk_weight 决定。
    risk_level = db.Column(db.String(20), default="", index=True)
    guardian_masked = db.Column(db.String(40), default="")
    guardian_phone = db.Column(db.String(20), default="")
    source = db.Column(db.String(30), default="")       # 残疾人台账 / 防溺水台账
    batch = db.Column(db.String(20), default="", index=True)   # 导入批次
    created_at = db.Column(db.DateTime, default=datetime.now)

    tags = db.relationship("PersonTag", backref="person", lazy="select",
                           cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name_masked,
            "id_masked": self.id_masked,
            "gender": self.gender,
            "age": self.age,
            "street": self.street,
            "community": self.community or "（未明确社区）",
            "grp": self.grp,
            "address": self.address,
            "addr_level": self.addr_level,
            "addr_precision": ADDR_PRECISION.get(self.addr_level, "无法定位"),
            "addr_mid": self.addr_mid,
            "addr_room": self.addr_room,
            "risk_level": self.risk_level,
            "guardian": self.guardian_masked,
            "guardian_phone": self.guardian_phone,
            "source": self.source,
            "tags": [t.to_dict() for t in self.tags],
        }


# 地址定位精度的中文说法。页面上必须如实显示，避免"看着像能精确到户"。
ADDR_PRECISION = {3: "栋+单元+门号", 2: "栋+单元", 1: "仅到楼栋/组", 0: "无法定位"}


class PersonTag(db.Model):
    """人员标签：一人可挂多个（既残疾又防溺水的实测有 15 人）。

    `risk_weight` 是提级计算的唯一依据，不在这里之外再写一遍阈值：
    1=需关注（残疾三四级、防溺水低风险） 2=重点关注（残疾二级、防溺水中风险）
    3=重点看护（残疾一级、防溺水高风险、事实无人抚养）
    """
    __tablename__ = "person_tag"
    id = db.Column(db.Integer, primary_key=True)
    person_id = db.Column(db.Integer, db.ForeignKey("person.id"), index=True)
    kind = db.Column(db.String(16), default="", index=True)   # 残疾 / 防溺水 / 留守
    detail = db.Column(db.String(60), default="")             # 肢体|三级 / 低风险|隔代监护
    risk_weight = db.Column(db.Integer, default=1, index=True)

    def to_dict(self):
        return {"kind": self.kind, "detail": self.detail,
                "weight": self.risk_weight,
                "label": f"{self.kind}·{self.detail}" if self.detail else self.kind}
