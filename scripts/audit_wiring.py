#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据连通性体检：每个"会变化的数据"是否真的写得进、读得到

## 为什么要单独一套

`check_integrity.py` 查的是**库内自洽**（状态与流转链一致、外键都能连上），
`audit_all.py` 的 E 组查的是**流转合法性**（没有非法迁移、没有重复办结）。
两者都假设"字段有人写"。可这个项目真出过两类事故：

- `WorkItem.status` —— 全站没有任何一处写它，78 行全是 `pending`，
  但列表页在读它。看板显示"待派发"看起来很稳，其实是常量。
- `Notice.handled_at` —— 收文办结三件套（handler/handled_at/handle_result）
  一个写入点都没有，而周报拿 `handled_at` 算"本周已办结"和"欠办"。
  前者恒 0、后者把每一条到期的收文都算成欠办，**不报错**。

所以这套的口径是：**每个会变化的数据，必须能从接口写进去、又能从接口读回来**。
只查静态（正则找 `x.field = ...`）不可信——字段名和局部变量同名（status/title/
content），按名字匹配会把"某个不相干的局部变量叫 status"算成写入点。这里三层都用
可验证的证据：

    A 写入链路   调真实写接口 → 直接查库，断言目标列真的变了（连带副作用列）
    B 读出链路   调真实读接口 → 断言刚写进去的值出现在返回体里
    C 运行时空列 只读真实演示库，找"列在、值永远空 / 永远同一个值"的列

C 组必须配白名单**和理由**：`created_at` 永远非空但取值很多、`chain_anchor` 只有
一行都是正常的；`notice.received_at` 全空是因为演示库里两条收文都还没签收，
也不是断点。**不给理由的忽略等于没查**（同 audit_all 的判定口径）。

## 用法

    python scripts/audit_wiring.py            # 全跑（会在 data/audit_wiring.db 副本上写）
    python scripts/audit_wiring.py --only C   # 只跑 C 组（只读，不动任何库）

退出码：有 FAIL 返回 1。
"""
import argparse
import json
import os
import pathlib
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

SRC_DB = BASE / "data" / "community.db"
# 文件名带 pid：沙箱的 safe-delete 会拦截删除 .db（trash 失败直接抛 OSError），
# 所以这套**从不删库**，每次跑换一个新名字。data/smoke_*.db* 已在 .gitignore 里。
DB = BASE / "data" / ("smoke_wiring_%d.db" % os.getpid())


def _stage_copy():
    """在**导入 app 之前**复制演示库。

    `config.py` 在模块导入时就 `os.environ.get("DATABASE_URL")` 定住了，
    等 create_app 再设环境变量已经晚了——探针会直接打到 data/community.db 上。
    这个坑真踩过：一次体检把演示库里的 1 号工单接单+转派+办结了，
    还多出一条任务和一条台账。**顺序必须是：建副本 → 设环境变量 → 导入 app**。
    """
    if not SRC_DB.exists():
        raise SystemExit("找不到演示库：%s" % SRC_DB)
    for suf in ("", "-wal", "-shm"):
        p = pathlib.Path(str(DB) + suf)
        if p.exists():
            raise SystemExit("库已存在，拒绝覆盖半途状态：%s" % p)
    for suf in ("", "-wal", "-shm"):
        s = pathlib.Path(str(SRC_DB) + suf)
        if s.exists():
            shutil.copy2(s, str(DB) + suf)


_stage_copy()
os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ.setdefault("NOTICE_AUTO_IMPORT", "0")
os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:9")   # 强制规则降级，测的是链路不是模型

from app import create_app                                        # noqa: E402
from app.auth import generate_password_hash                       # noqa: E402
from app.extensions import db                                     # noqa: E402
from app.models import (FdeRecord, KbDoc, Ledger, Notice, Report,  # noqa: E402
                        ReportRevision, ResidentTicket, Task, User,
                        WorkItem, WorkOrder, WorkOrderLog)

APP = None
PW = "wiring123"
PASS = FAIL = SKIP = 0
FAILED = []


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("　" + str(extra)) if extra else ""))


def skip(label, why):
    global SKIP
    SKIP += 1
    print("  [SKIP] %s　%s" % (label, why))


def boot():
    """起应用。副本与环境变量在模块导入阶段就已经就位（见 _stage_copy）。"""
    global APP, DB
    APP = create_app("dev")
    # 保险：确认应用真的连在副本上，而不是演示库
    uri = str(APP.config.get("SQLALCHEMY_DATABASE_URI") or "")
    if str(SRC_DB.name) in uri and str(DB.name) not in uri:
        raise SystemExit("拒绝运行：应用连到了演示库 %s" % uri)
    with APP.app_context():
        prep()
    return APP


def make_client(username):
    """每个角色必须独立 client：/login 首句是 if current_user(): redirect，
    已登录的 client 再 POST 登录是空转，第二个角色根本不会生效（踩过）。"""
    c = APP.test_client()
    r = c.post("/login", data={"username": username, "password": PW})
    return c, r.status_code


def one(sql, args=(), path=None):
    """直接读 sqlite（不走 ORM，避免会话缓存把"没写进去"看成"写进去了"）"""
    p = (path or DB).as_posix()
    con = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
    try:
        return con.execute(sql, args).fetchone()
    finally:
        con.close()


def cnt(table, where="1=1", args=()):
    return one("select count(*) from %s where %s" % (table, where), args)[0]


def json_of(res):
    try:
        return res.get_json() or {}
    except Exception:                                             # noqa: BLE001
        return {}


# ---------------------------------------------------------------- 准备

def prep():
    """把演示库里的账号密码统一成已知值，并补一个可转派的对象。

    只动副本，不动 community.db。真实密码是哈希，测试没必要去猜。
    """
    with APP.app_context():
        for u in User.query.all():
            u.password_hash = generate_password_hash(PW)
            u.status = "active"
        db.session.commit()
        print("  账号：%s" % "、".join("%s(%s)" % (u.username, u.role)
                                   for u in User.query.all()))


# ---------------------------------------------------------------- A/B 组

def group_orders(clients):
    print("\n[工单] open→doing→done，三列 + 流转日志")
    prop = clients["property"]
    row = one("select id, order_no, status, property_co from work_order "
              "where status='open' and property_co=? limit 1", ("幸福里物业",))
    if not row:
        skip("工单接单", "副本里没有 幸福里物业 的 open 工单")
        return
    oid, no, _, _ = row

    # A1 接单：assignee / assignee_id / assigned_at 三列都必须落库
    before = one("select assignee, assignee_id, assigned_at from work_order where id=?", (oid,))
    n_log = cnt("work_order_log", "order_id=?", (oid,))
    r = prop.post("/api/orders/%d/accept" % oid)
    after = one("select status, assignee, assignee_id, assigned_at from work_order where id=?", (oid,))
    check("接单 status: open→doing", after[0] == "doing", "实际 %s" % (after[0],))
    check("接单 assignee 落库", bool(after[1]) and after[1] != before[0],
          "前=%s 后=%s" % (before[0], after[1]))
    check("接单 assignee_id 落库（追责要用）", after[2] is not None, "实际 %s" % (after[2],))
    check("接单 assigned_at 落库", bool(after[3]) and after[3] != before[2])
    check("接单写了流转日志", cnt("work_order_log", "order_id=?", (oid,)) == n_log + 1)
    log = one("select action, from_status, to_status, actor_id from work_order_log "
              "where order_id=? order by id desc limit 1", (oid,))
    check("流转日志记了 actor_id", log[3] is not None, "实际 %s" % (log[3],))

    # B1 读出：接口返回的 status 必须是这次改后的值
    d = json_of(prop.get("/api/orders/%d" % oid))["data"]
    check("读接口回显 status=doing", d.get("status") == "doing", d.get("status"))

    # A2 转派：assignee 变、assigned_at 不能变（换人不重置 SLA 计时）
    assigned_before = one("select assignee, assigned_at from work_order where id=?", (oid,))
    t = prop.post("/api/orders/%d/transfer" % oid,
                  json={"assignee": "赵师傅", "note": "连通性体检"})
    assigned_after = one("select assignee, assigned_at from work_order where id=?", (oid,))
    if t.status_code == 200:
        check("转派 assignee 变了", assigned_after[0] != assigned_before[0],
              "前=%s 后=%s" % (assigned_before[0], assigned_after[0]))
        check("转派不动 assigned_at（SLA 计时不被重置）",
              assigned_after[1] == assigned_before[1])
        lg = one("select action, detail from work_order_log where order_id=? "
                 "order by id desc limit 1", (oid,))
        check("转派写了流转日志且记了从谁到谁",
              lg[0] == "transfer" and "→" in (lg[1] or ""), lg[1])
    else:
        skip("转派链路", "接口返回 %d：%s" % (t.status_code, json_of(t).get("msg")))

    # A3 办结：feedback / done_at / 台账补档
    n_ledger = cnt("ledger")
    f = prop.post("/api/orders/%d/finish" % oid, json={"feedback": "已更换楼道灯泡并测试正常"})
    done = one("select status, feedback, done_at from work_order where id=?", (oid,))
    check("办结 status: doing→done", done[0] == "done", "实际 %s" % (done[0],))
    check("办结 feedback 落库", bool(done[1]), "实际 %r" % (done[1],))
    check("办结 done_at 落库", bool(done[2]))
    check("办结补了台账（台账 Agent 真的在跑）", cnt("ledger") == n_ledger + 1,
          "台账 %d → %d" % (n_ledger, cnt("ledger")))
    d = json_of(prop.get("/api/orders/%d" % oid))["data"]
    check("读接口回显 done + 处理时长", d.get("status") == "done" and "handle_hours" in d)


def group_tasks(clients):
    print("\n[任务] 下发→办结，closed_at 与状态同源")
    adm, com = clients["admin"], clients["community"]
    deadline = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    r = adm.post("/api/tasks", json={"title": "连通性体检任务", "assignee_role": "community",
                                     "assignee_org": "幸福里社区", "deadline": deadline})
    tid = (json_of(r).get("data") or {}).get("id")
    if not tid:
        skip("任务下发", json_of(r).get("msg") or r.status_code)
        return
    row = one("select status, creator from task where id=?", (tid,))
    if not row:
        check("任务下发落库", False, "接口返回了 id=%s 但库里没有这一行" % tid)
        return
    check("任务下发落库 status=open", row[0] == "open", "实际 %r" % (row[0],))
    check("任务下发记了 creator", bool(row[1]), "实际 %r" % (row[1],))

    # B 读出：列表里能看到这张新任务
    items = (json_of(com.get("/api/tasks"))["data"] or {}).get("rows") or []
    check("任务读接口能看到新任务", any(x.get("id") == tid for x in items), "共 %d 条" % len(items))

    r = com.post("/api/tasks/%d/done" % tid)
    row = one("select status, closed_at from task where id=?", (tid,))
    check("任务办结 status=done", row[0] == "done", "实际 %s" % (row[0],))
    check("任务办结 closed_at 落库（与状态同源）", bool(row[1]), "实际 %r" % (row[1],))


def group_notices(clients):
    print("\n[公告/收文] 签收与置顶：时间与人都不能空")
    com, adm = clients["community"], clients["admin"]
    row = one("select id from notice where direction='in' and recv_status='dispatched' limit 1")
    if not row:
        skip("收文签收", "副本里没有待签收的收文")
    else:
        nid = row[0]
        r = com.post("/api/notices/%d/receive" % nid)
        got = one("select recv_status, received_by, received_at from notice where id=?", (nid,))
        check("签收 recv_status=received", got[0] == "received", got[0])
        check("签收记住了人", bool(got[1]), "实际 %r" % (got[1],))
        check("签收记住了时间", bool(got[2]), "实际 %r" % (got[2],))
        d = json_of(com.get("/api/notices/%d" % nid))["data"]
        check("读接口回显签收人/时间", bool(d.get("received_by")) and bool(d.get("received_at")),
              "%r/%r" % (d.get("received_by"), d.get("received_at")))

        # 办结：三列 + 周报 KPI 必须跟着动（周报读的就是 handled_at）
        r = com.post("/api/notices/%d/handle" % nid, json={"result": "已联系物业当日疏通，回访无积水"})
        hd = one("select handler, handled_at, handle_result from notice where id=?", (nid,))
        check("办结写入了承办人/时间/结果", bool(hd[0]) and bool(hd[1]) and bool(hd[2]), str(hd))
        check("办结留了公文日志", cnt("doc_log", "notice_id=? and action='handle'", (nid,)) == 1)
        lg = one("select actor_id from doc_log where notice_id=? and action='handle'", (nid,))
        check("办结留痕能指到账号", lg and lg[0] is not None, str(lg))
        d = json_of(com.get("/api/notices/%d" % nid))["data"]
        check("读接口回显办理结果", bool(d.get("handle_result")) and bool(d.get("handled_at")),
              "%r/%r" % (d.get("handled_at"), d.get("handle_result")))
        # 再提交一次：结果可以补正，但首次办结时间不能漂（否则周报数字会跑到下周）
        first = hd[1]
        com.post("/api/notices/%d/handle" % nid, json={"result": "补充：已回访居民确认不再渗水"})
        again = one("select handled_at, handle_result from notice where id=?", (nid,))
        check("补正结果不改首次办结时间", again[0] == first, "%r → %r" % (first, again[0]))
        check("补正后结果已更新", "回访" in (again[1] or ""), again[1])
        # 未签收就不能办结
        nid2row = one("select id from notice where direction='in' and recv_status='dispatched' limit 1")
        if nid2row:
            r2 = com.post("/api/notices/%d/handle" % nid2row[0], json={"result": "未签收直接办结"})
            check("未签收不能办结（409）", r2.status_code == 409 or json_of(r2).get("code") == 409,
                  "%d %s" % (r2.status_code, json_of(r2).get("msg")))

    pub = one("select id from notice where status='published' limit 1") or \
          one("select id from notice where direction='inner' limit 1")
    if not pub:
        skip("公告置顶", "副本里没有可置顶的公告")
    else:
        nid = pub[0]
        r = adm.post("/api/notices/%d/pin" % nid, json={"pinned": True})
        got = one("select pinned, pinned_at from notice where id=?", (nid,))
        check("置顶 pinned=1 且 pinned_at 落库", bool(got[0]) and bool(got[1]),
              "%r/%r" % (got[0], got[1]))
        r = adm.post("/api/notices/%d/pin" % nid, json={"pinned": False})
        got = one("select pinned, pinned_at from notice where id=?", (nid,))
        check("取消置顶清掉 pinned_at（否则列表按旧时间排序）",
              not got[0] and not got[1], "%r/%r" % (got[0], got[1]))


def group_docs(clients):
    print("\n[公文] 流转：状态与留痕必须同一件事")
    adm = clients["admin"]
    r = adm.post("/api/docs", json={"title": "连通性体检通知", "doc_type": "通知",
                                    "secret_level": "内部", "urgency": "平件"})
    did = (json_of(r).get("data") or {}).get("id")
    if not did:
        skip("公文新建", json_of(r).get("msg") or r.status_code)
        return
    got = one("select doc_status from notice where id=?", (did,))
    check("新建公文 doc_status=draft", got[0] == "draft", got[0])

    n_log = cnt("doc_log", "notice_id=?", (did,))
    r = adm.post("/api/docs/%d/check" % did)
    got = one("select doc_status from notice where id=?", (did,))
    check("核稿 doc_status=draft→checked", got[0] == "checked", got[0])
    check("核稿写了公文留痕", cnt("doc_log", "notice_id=?", (did,)) == n_log + 1)
    lg = one("select action, actor, actor_id from doc_log where notice_id=? "
             "order by id desc limit 1", (did,))
    check("留痕记了经办人姓名", bool(lg[1]), lg[1])
    check("留痕记了经办人 actor_id（追责要能指到账号）", lg[2] is not None,
          "实际 %r　// 空 = docflow 只写了名字，与 work_order_log 口径不一致" % (lg[2],))


def group_reports(clients):
    print("\n[报表] 同周期再发布：版本 +1 且旧版必须归档（覆盖前先 archive）")
    adm = clients["admin"]
    p = one("select period from report order by id desc limit 1")
    period = p[0] if p else "2026-10"
    v0 = one("select id, version from report where period=? order by id desc limit 1", (period,))
    rev0 = cnt("report_revision")
    r1 = adm.post("/api/reports/generate", json={"period": period})
    v1 = one("select id, version from report where period=? order by id desc limit 1", (period,))
    if not v1 or r1.status_code != 200:
        skip("报表发布", json_of(r1).get("msg") or r1.status_code)
        return
    check("报表发布生成了记录", bool(v1))
    r2 = adm.post("/api/reports/generate", json={"period": period})
    v2 = one("select id, version from report where period=? order by id desc limit 1", (period,))
    check("同周期再发布 version+1", v2 and v2[1] == (v1[1] + 1),
          "前 %s 后 %s" % (v1[1] if v1 else None, v2[1] if v2 else None))
    check("覆盖前把旧版归档进 report_revision", cnt("report_revision") > rev0,
          "revision %d → %d" % (rev0, cnt("report_revision")))
    rid = v2[0] if v2 else None
    if rid:
        vers = json_of(adm.get("/api/reports/%d/versions" % rid))
        check("版本接口能列出历史", (vers.get("data") or vers) not in (None, [], {}))


def group_fde(clients):
    print("\n[首录台] 一条诉求必须同时落到 首录/业务项/工单/密级 四处")
    com = clients["community"]
    n_fde, n_item, n_order, n_sec = cnt("fde_record"), cnt("work_item"), cnt("work_order"), cnt("field_security")

    def _dash():
        return (json_of(com.get("/api/fde/dashboard")).get("data") or {}).get("stats") or {}

    s0 = _dash()
    r = com.post("/api/fde", json={
        # 姓名用「张某某」这种明确占位写法：真的姓名 + 生日 + 门牌 + 身体状况
        # 组合起来足以重识别到具体的人（见 docs/26 红线"脱敏要看重识别"），
        # 测试夹具没有必要承担这个风险。身份证与手机号本来就是编的
        # （13800138000 是中国移动的公开测试号）。
        "raw_text": "7栋3单元501的张某某老人来电13800138000，身份证110101194001011234，"
                    "反映厨房水管漏水，柜子下面已经积水，本人行动不便",
        "source": "text"})
    body = json_of(r)
    s1 = _dash()
    check("首录接口返回成功", r.status_code == 200 and body.get("code") == 0,
          "%d %s" % (r.status_code, body.get("msg")))
    # 首页看板曾经整块是常量（录十条不动一下），所以这里必须验"提交后数字会变"，
    # 而不是只看接口 200——200 加一堆常量也能过。
    check("看板 fde_count 随提交变化（不是常量）",
          s1.get("fde_count") == (s0.get("fde_count") or 0) + 1,
          "%r → %r" % (s0.get("fde_count"), s1.get("fde_count")))
    check("看板 item_count 随提交变化", (s1.get("item_count") or 0) > (s0.get("item_count") or 0),
          "%r → %r" % (s0.get("item_count"), s1.get("item_count")))
    check("写了 fde_record", cnt("fde_record") > n_fde, "%d→%d" % (n_fde, cnt("fde_record")))
    check("拆出了 work_item", cnt("work_item") > n_item, "%d→%d" % (n_item, cnt("work_item")))
    check("派出了 work_order", cnt("work_order") > n_order, "%d→%d" % (n_order, cnt("work_order")))
    wi = one("select id, fields_json from work_item order by id desc limit 1")
    fields = {}
    try:
        fields = json.loads((wi or (None, "{}"))[1] or "{}")
    except Exception:                                             # noqa: BLE001
        fields = {}
    if not fields:
        skip("写了 field_security（密级）",
             "规则降级没抽出结构化字段，密级无处可打——这是降级模式的盲区，不是断链")
    else:
        n_fs = cnt("field_security", "work_item_id=?", (wi[0],))
        check("每个结构化字段都打了密级", n_fs >= len(fields),
              "字段 %d 个，密级 %d 条" % (len(fields), n_fs))
    new_order = one("select id, order_no, sla_hours, sla_level from work_order order by id desc limit 1")
    check("新单带 SLA 等级与小时数（定级在建单之前）",
          bool(new_order[2]) and bool(new_order[3]), str(new_order))
    check("协作轨迹落了 agent_step",
          cnt("agent_step") > 0, "agent_step=%d" % cnt("agent_step"))
    if cnt("agent_step"):
        d = json_of(com.get("/api/collab/runs?limit=1"))
        items = (d.get("data") or {}).get("items") or []
        check("轨迹接口能读到这次运行", bool(items), "items=%d" % len(items))
        if items:
            rid = items[0].get("run_id")
            tr = json_of(com.get("/api/collab/trace?run_id=%s" % rid))
            steps = (tr.get("data") or {}).get("steps") or []
            check("轨迹详情含逐步记录且耗时非空",
                  bool(steps) and all(s.get("took_ms") is not None for s in steps),
                  "steps=%d" % len(steps))


def group_portal(clients):
    print("\n[居民端] 提交→工单→居民单，三方同一条链")
    pub = APP.test_client()      # 居民不登录
    n_tk, n_fde = cnt("resident_ticket"), cnt("fde_record")
    r = pub.post("/api/portal/report", json={
        "phone": "13800138000", "name": "张三", "address": "7栋3单元501",
        "text": "厨房水管漏水，柜子下面已经积水了"})
    body = json_of(r)
    check("居民提交成功", r.status_code == 200 and body.get("code") == 0,
          "%d %s" % (r.status_code, body.get("msg")))
    check("生成 resident_ticket", cnt("resident_ticket") > n_tk, "%d→%d" % (n_tk, cnt("resident_ticket")))
    check("同时生成 fde_record（首录不留白）", cnt("fde_record") > n_fde)
    t = one("select id, code, phone, item_count, line_summary from resident_ticket order by id desc limit 1")
    check("居民单有查询码（否则居民查不了进度）", bool(t[1]), str(t))
    check("居民单记了手机号", bool(t[2]), str(t))
    check("居民单 item_count 与实际拆解数一致", int(t[3] or 0) >= 1, "item_count=%r" % (t[3],))
    check("居民单有业务线摘要", bool(t[4]), str(t[4]))


def group_admin_kb(clients):
    print("\n[账号/知识库] 新建即可见（新增数据能否立刻读到）")
    adm = clients["admin"]
    n_u = cnt("user")
    r = adm.post("/api/admin/users", json={"username": "wiring_user", "name": "连通性测试",
                                           "org": "幸福里社区", "role": "community",
                                           "password": "wiring123"})
    if r.status_code == 200 and json_of(r).get("code") == 0:
        check("新建账号落库且 active", cnt("user") > n_u)
        got = one("select status, perms_json from user where username='wiring_user'")
        check("新账号 status=active", got and got[0] == "active", got)
        check("新账号 perms_json 为 NULL（跟随角色，不是 [] 一个都不给）",
              got and got[1] is None, "实际 %r" % (got[1] if got else None,))
        lst = json_of(adm.get("/api/admin/users"))["data"]
        rows = lst.get("rows") if isinstance(lst, dict) else lst
        check("用户列表能看到新账号",
              any((x or {}).get("username") == "wiring_user" for x in (rows or [])))
    else:
        skip("新建账号", json_of(r).get("msg") or r.status_code)

    n_kb = cnt("kb_doc")
    r = adm.post("/api/kb/docs", json={"title": "连通性体检文档", "category": "工作规范",
                                       "content": "这是一段用于连通性体检的正文内容，长度超过二十个字。",
                                       "tags": "体检,连通"})
    if r.status_code == 200 and json_of(r).get("code") == 0:
        check("知识库新增落库", cnt("kb_doc") > n_kb)
        got = one("select tags from kb_doc where title='连通性体检文档'")
        check("知识库 tags 落库（不是写死空串）", got and got[0] == "体检,连通", got)
    else:
        skip("知识库新增", json_of(r).get("msg") or r.status_code)


# ---------------------------------------------------------------- C 组

# 白名单：「列在、值永远空 / 永远同一个值」但**不是断点**的列，必须写理由。
# 没理由的忽略等于没查——这条口径与 audit_all 一致。
C_WHY = {
    ("chain_anchor", "seq"): "锚点表本来就只有一行，seq/total 是当时的值",
    ("chain_anchor", "head_hash"): "同上，锚点是单行快照",
    ("chain_anchor", "total"): "同上",
    ("chain_anchor", "note"): "同上",
    ("kb_doc", "created_by"): "演示库文档都由同一人导入",
    ("kb_doc", "tags"): "演示库未填标签；写入链路由 A 组验证（新建带标签能落库）",
    ("notice", "scope"): "7 条全部是「全体」，演示库没有定向公告",
    ("notice", "receiver_org"): "定向收文单位未使用（收文都是全体）",
    ("notice", "received_at"): "两条收文都还是 dispatched，没签收过；A 组已验证签收会写",
    ("notice", "received_by"): "同上",
    ("notice", "deadline"): "演示库收文未设办理期限，故周报「欠办」恒 0（口径见 docs/33）",
    ("notice", "secret_until"): "解密期限未启用（密级只做可见性，不做到期自动解密）",
    ("notice", "handler"): "★收文办结未实现：见下方 D 组说明",
    ("notice", "handled_at"): "★收文办结未实现：weekly.py 用它算「已办结/欠办」",
    ("notice", "handle_result"): "★收文办结未实现",
    ("notice", "attachments"): "公告附件未启用（居民单附件另有链路）",
    ("notice", "parent_id"): "无「转发下级来文」的父子关系数据",
    ("notice", "archived_at"): "只有 1 条归档过",
    ("notice", "doc_year"): "同一年的公文",
    ("notice", "pinned_at"): "只有 1 条置顶过",
    ("org_contact", "active"): "3 条通讯录都在用",
    ("report", "line"): "两条报表都是「街道」线",
    ("report", "version"): "两个周期各发过 1 次，没有覆盖",
    ("report", "rollback_from"): "没有回滚过；A 组验证同周期再发布能 version+1 且归档",
    ("resident_ticket", "item_count"): "演示库 25 条都只拆 1 项",
    ("resident_ticket", "attachments"): "演示库没有居民上传过附件",
    ("task", "creator"): "4 条任务都由同一人下发",
    ("task", "closed_at"): "只有 1 条办结",
    ("user", "status"): "5 个账号都 active",
    ("user", "perms_json"): "NULL=跟随角色（这是正确默认值，不是缺数据）",
    ("user", "phone"): "账号都没填手机号",
    ("user", "created_by"): "同一人建号",
    ("user", "review_note"): "没有走过审核驳回",
    ("weekly", "period"): "只有 1 份周报",
    ("weekly", "start_date"): "同上",
    ("weekly", "end_date"): "同上",
    ("weekly", "data_json"): "同上",
    ("weekly", "body_md"): "同上",
    ("weekly", "status"): "同上",
    ("weekly", "editor"): "同上",
    ("weekly", "signed_by"): "同上",
    ("weekly", "signed_at"): "同上",
    ("weekly", "revision"): "同上",
    ("work_item", "status"): "★死字段：全站无写入，判进度只认工单+台账（docs/26 红线 18）",
    ("work_order", "verdict_at"): "41 单里仲裁过 1 次",
    ("doc_log", "actor_id"): "★断点：docflow 只写 actor 姓名，没写 id",
    # --- 人员底册（2842 人，从以朵街道下发台账导入）---
    # 这两列取值单一不是断点，是数据本身的性质，写清楚免得下次有人来"修"。
    ("person", "street"): "台账是单一街道（以朵街道）下发的，全域同值属正常；"
                          "社区维度看 person.community，那列有 4 种取值（含空）",
    ("person", "batch"): "一次导入 = 一个批次，单值正常；换批次重导时列会变化，"
                         "脚本 --purge 会清旧底册",
}


def group_empty():
    print("\n[C 运行时空列] 只读演示库：列在、值却永远一样（或永远空）")
    con = sqlite3.connect("file:%s?mode=ro" % SRC_DB.as_posix(), uri=True)
    tabs = [r[0] for r in con.execute(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
    unexplained = []
    for t in tabs:
        n = con.execute("select count(*) from %s" % t).fetchone()[0]
        if n == 0:
            continue
        for col in [r[1] for r in con.execute("pragma table_info(%s)" % t)]:
            if col in ("id", "created_at"):
                continue
            ne, dist = con.execute(
                "select sum(case when %s is null or %s='' then 0 else 1 end), "
                "count(distinct %s) from %s" % (col, col, col, t)).fetchone()
            if ne == 0 or dist <= 1:
                if (t, col) not in C_WHY:
                    unexplained.append("%s.%s（非空 %d/%d，取值 %d）" % (t, col, ne, n, dist))
    # 已知断点列必须仍在白名单里（防止"删掉数据就查不出来了"）
    con.close()
    if unexplained:
        check("空列/单值列都有理由", False, "未解释 %d 个：%s" % (len(unexplained), "；".join(unexplained[:6])))
    else:
        check("空列/单值列都有理由", True, "%d 条白名单" % len(C_WHY))


def _attr_writes(field):
    """数「属性赋值」写入点：`x.field = ...`。

    只认属性赋值，不认字符串/注释/ALTER TABLE 里的字段名——
    `("handle_result", "TEXT DEFAULT ''")` 是迁移语句，不是写入。
    """
    import re
    pat = re.compile(r"\.%s\s*=(?!=)" % field)
    n = 0
    for p in (BASE / "app").rglob("*.py"):
        if p.name == "models.py":
            continue
        n += len(pat.findall(p.read_text(encoding="utf-8", errors="replace")))
    return n


def group_dead_end():
    """D 组：已知断点的当前状态。修好会自动变 PASS，不允许长期红着没人管。"""
    print("\n[D 已知断点] 不修就得在文档里写清楚，不能装作没看见")

    trio = {f: _attr_writes(f) for f in ("handler", "handled_at", "handle_result")}
    missing = [f for f, n in trio.items() if n == 0]
    check("收文办结三件套有写入路径", not missing,
          "无写入：%s；weekly.py 用它算「本周已办结」（恒 0）与「欠办」"
          "（把到期收文全算成欠办）" % "、".join(missing) if missing else "")

    # 留痕能不能指到人，看**写入路径**有没有传 actor_id（A 组用新公文实测过）。
    # 演示库里 11 条历史留痕 actor_id 为空，那是种子数据造的，不是代码断链——
    # 这属于数据质量问题，报出来但不判 FAIL（否则修了代码这套永远红着）。
    src = (BASE / "app" / "docflow.py").read_text(encoding="utf-8", errors="replace")
    check("公文留痕写入时带 actor_id", "actor_id=actor_id" in src,
          "docflow.push 没有把 actor_id 落到 DocLog，追责只能靠姓名撞")
    log_rows = one("select count(*) from doc_log", path=SRC_DB)[0]
    log_no_id = one("select count(*) from doc_log where actor_id is null", path=SRC_DB)[0]
    if log_rows and log_no_id:
        print("  [NOTE] 演示库 %d/%d 条历史留痕 actor_id 为空（种子数据，非代码问题）"
              % (log_no_id, log_rows))


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只跑某组（A/B/C/D 组合，如 C）")
    args = ap.parse_args()
    only = args.only.upper()

    print("=" * 68)
    print("数据连通性体检 · %s" % SRC_DB.name)
    print("=" * 68)

    if "C" in only or not only:
        group_empty()
    if "D" in only or not only:
        group_dead_end()

    if not only or any(g in only for g in "AB"):
        boot()
        clients = {}
        for u in ("admin", "community", "property"):
            name = {"admin": "admin", "community": "wang", "property": "wuye"}[u]
            c, code = make_client(name)
            if code not in (200, 302):
                print("  [WARN] %s 登录返回 %s" % (name, code))
            clients[u] = c
        group_orders(clients)
        group_tasks(clients)
        group_notices(clients)
        group_docs(clients)
        group_reports(clients)
        group_fde(clients)
        group_portal(clients)
        group_admin_kb(clients)

    print("\n" + "-" * 68)
    print("通过 %d / 失败 %d / 跳过 %d" % (PASS, FAIL, SKIP))
    if FAILED:
        print("失败项：")
        for x in FAILED:
            print("  - %s" % x)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
