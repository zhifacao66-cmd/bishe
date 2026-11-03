"""月报导出 Excel：把 report 表里的**冻结快照**渲染成一份可交的 xlsx

为什么只读快照、不重新查库
--------------------------
报表是"某一时刻的口径证据"。如果导出时重新统计，会出现两种难解释的情况：
  1. 页面上写 12 件，导出文件里 15 件（导出前又录了 3 条）——街道拿着两个版本对账；
  2. 去年 10 月的报表，今天导出来数字变了——归档件失去证据效力。
所以导出 = 把 `report.data_json` 排版，一列数字都不重算。要新数字就重新生成一份报表，
两份都留着，这本身就是可追溯。

表结构（6 个工作表）
--------------------
  月报概览      标题 / 报送信息 / 九项核心指标（含环比）/ 统计区间 / 月度摘要
  业务线分布    5 条业务线的事项数与占比 + 合计
  业务类型分布  7 类业务的事项数与占比 + 合计
  工单办理      三种状态分布、办结率、超期件数
  业务项明细    本月拆解出的业务项清单（脱敏摘要，不含居民姓名电话）
  填报说明      口径、脱敏声明、生成方式、责任追溯——报表被质疑时先看这一页

字体按公文习惯：标题黑体、正文宋体；表头用系统主色蓝，打印成黑白也不影响阅读
（表头字是白色加粗，转灰度后与正文仍有明显分界）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from app.xlsx import Workbook, styles as S

STATUS_TEXT = {"open": "待处理", "doing": "处理中", "done": "已完成"}

# 明细表一次最多带多少行。报表主体是汇总，明细是附件；
# 真要看全量应该导出工单台账，而不是往月报里塞几千行。
MAX_DETAIL = 300

_TITLE_COLS = 8          # 概览表用到的列数，标题与摘要按这个宽度合并


def _pct(a, b) -> float | None:
    return (a / b) if b else None


def _delta(cur, prev) -> tuple:
    """返回 (值, 样式)。上期为 0 时不编造增长率，直接写"—"。"""
    if not prev:
        return "—", S.text_c
    return (cur - prev) / prev, S.delta


def _order_of(pairs):
    """分布表：按数量倒序，同数量按名称，保证两次导出顺序一致"""
    return sorted(pairs, key=lambda kv: (-kv[1], kv[0]))


def _block(sh, row: int, title: str, sub: str = "", cols: int = _TITLE_COLS):
    """统一的表头三行：标题（合并）/ 副标题（合并）/ 空行

    cols 传本表实际用到的列数——合并到用不到的列上，打印时右侧会拖着一条
    长长的空白边框，看着像少画了内容。
    """
    last = chr(64 + cols)
    if title:
        sh.cell(row, 1, title, S.title)
        sh.merge(f"A{row}:{last}{row}")
        sh.height(row, 32)
    if sub:
        sh.cell(row + 1, 1, sub, S.sub)
        sh.merge(f"A{row + 1}:{last}{row + 1}")
    return row + 3


# ---------------------------------------------------------------- 概览

def _sheet_overview(wb, r, d):
    sh = wb.add("月报概览", widths=[26, 13, 13, 13, 12, 12, 12, 12],
                tab_color="206BC4", landscape=False)
    period = d.get("period") or r.period or ""
    org = d.get("org") or ""
    sup = " · ".join(x for x in (
        f"报送单位：{org}" if org else "",
        f"报送对象：{d.get('to') or '街道'}",
        f"报表编号：R{r.id:06d}",
    ) if x)

    row = _block(sh, 1, f"{period} 社区治理月度报表", sup)

    # ---- 核心指标表
    hdr = row
    sh.row(hdr, ["指标", "本期", "上期", "环比", "口径说明"],
           styles_=[S.head_l, S.head, S.head, S.head, S.head_l])
    sh.merge(f"E{hdr}:H{hdr}")

    orders = d.get("orders") or {}
    total = orders.get("total", 0) or 0
    done = orders.get("done", 0) or 0
    prev = d.get("prev") or {}
    lines = d.get("line_counts") or {}
    types = d.get("type_counts") or {}

    metrics = [
        ("首录事项（条）", d.get("fde_count", 0), prev.get("fde_count", 0),
         "工作者一次录入的原始记录数，未拆解"),
        ("拆解业务项（个）", d.get("item_count", 0), prev.get("item_count", 0),
         "一条首录拆成多个业务项，分别派往对应业务线"),
        ("覆盖业务线（条）", len(lines), None,
         "本月实际产生事项的业务线条数（共 5 条）"),
        ("物业工单（件）", total, prev.get("order_total", 0),
         "由业务项生成、派给物业的工单"),
        ("工单办结（件）", done, None,
         "状态为「已完成」的工单"),
        ("工单办结率", _pct(done, total), None,
         "办结数 ÷ 工单总数，未办结含待处理与处理中"),
        ("超期未办结（件）", d.get("overdue_count"), None,
         "超过 SLA 时限且未办结，按知识库分级：紧急4h/较急24h/一般72h"),
        ("复用历史档案（次）", d.get("reuse_count", 0), None,
         "命中已有台账条目而免于重复填报的次数"),
        ("敏感信息脱敏（批次）", d.get("sensitive_count", 0), None,
         "含身份证/手机号等字段，经本地模型脱敏后入库"),
    ]

    r0 = hdr + 1
    for i, (name, cur, pv, note) in enumerate(metrics):
        rr = r0 + i
        is_pct = name == "工单办结率"
        sh.cell(rr, 1, name, S.label)
        if cur is None:
            sh.cell(rr, 2, "—", S.text_c)
        elif is_pct:
            sh.cell(rr, 2, cur, S.pct)
        else:
            sh.cell(rr, 2, cur, S.num)
        if pv is None:
            sh.cell(rr, 3, "—", S.text_c)
            sh.cell(rr, 4, "—", S.text_c)
        else:
            sh.cell(rr, 3, pv, S.num)
            val, st = _delta(cur, pv)
            sh.cell(rr, 4, val, st)
        sh.cell(rr, 5, note, S.note)
        sh.merge(f"E{rr}:H{rr}")
        sh.height(rr, 22)

    # ---- 报表元信息
    mi = r0 + len(metrics) + 1
    sh.cell(mi, 1, "统计区间", S.label)
    sh.cell(mi, 2, _range_text(d), S.text)
    sh.merge(f"B{mi}:H{mi}")
    sh.cell(mi + 1, 1, "生成时间", S.label)
    sh.cell(mi + 1, 2, r.generated_at or datetime.now(), S.stamp)
    sh.merge(f"B{mi + 1}:H{mi + 1}")
    sh.cell(mi + 2, 1, "数据来源", S.label)
    sh.cell(mi + 2, 2, "社区智能体系统运行库（本地部署，未出网）", S.text)
    sh.merge(f"B{mi + 2}:H{mi + 2}")
    for x in range(3):
        sh.height(mi + x, 22)

    # ---- 摘要
    sr = mi + 4
    sh.cell(sr, 1, "月度摘要", S.label)
    sh.merge(f"A{sr}:H{sr}")
    sh.height(sr, 22)
    sh.cell(sr + 1, 1, d.get("summary") or "（无摘要）", S.text)
    sh.merge(f"A{sr + 1}:H{sr + 1}")
    # 摘要一般 150~200 字，按每行 46 字估算行高，避免只露一行
    sh.height(sr + 1, max(60, min(140, len(d.get("summary") or "") // 2)))
    return sh


def _range_text(d) -> str:
    rng = d.get("range") or []
    if len(rng) == 2:
        return f"{rng[0]} 至 {rng[1]}（闭开区间：含首日，不含次月 1 日）"
    return ""


# ---------------------------------------------------------------- 分布表

def _dist_sheet(wb, name, pairs, total, unit_label, note):
    sh = wb.add(name, widths=[6, 24, 16, 14], autofilter=None, tab_color="4299E1")
    row = _block(sh, 1, f"{name}（业务项数）", "统计范围与《月报概览》同一时间窗，口径一致", cols=4)
    hdr = row
    sh.row(hdr, ["序号", unit_label, "业务项数", "占比"],
           styles_=[S.head, S.head_l, S.head, S.head])
    i = 0
    for nm, cnt in _order_of(list(pairs.items())):
        rr = hdr + 1 + i
        sh.cell(rr, 1, i + 1, S.text_c)
        sh.cell(rr, 2, nm, S.text)
        sh.cell(rr, 3, cnt, S.num)
        sh.cell(rr, 4, _pct(cnt, total), S.pct)
        i += 1
    if not i:                                   # 本月无数据也要给一行，别留空白表
        sh.cell(hdr + 1, 1, "—", S.text_c)
        sh.cell(hdr + 1, 2, "本月无数据", S.text)
        sh.merge(f"B{hdr + 1}:D{hdr + 1}")
        i = 1
    tr = hdr + 1 + i
    sh.cell(tr, 1, "合计", S.total)
    sh.cell(tr, 2, "", S.total)
    sh.cell(tr, 3, sum(pairs.values()), S.total_num)
    sh.cell(tr, 4, 1.0 if sum(pairs.values()) else None, S.total_pct)
    sh.cell(tr + 2, 1, note, S.note)
    sh.merge(f"A{tr + 2}:D{tr + 2}")
    sh.height(tr + 2, 30)
    return sh


# ---------------------------------------------------------------- 工单

def _sheet_orders(wb, r, d):
    sh = wb.add("工单办理", widths=[18, 16, 14, 44], tab_color="E8590C")
    row = _block(sh, 1, "物业工单办理情况",
                 "工单内容在派发前已脱敏，本表只统计流转状态", cols=4)
    hdr = row
    sh.row(hdr, ["状态", "工单数（件）", "占比", "说明"],
           styles_=[S.head_l, S.head, S.head, S.head_l])

    orders = d.get("orders") or {}
    total = orders.get("total", 0) or 0
    plan = [
        ("open", "待处理", "物业已接单，尚未开始处理"),
        ("doing", "处理中", "已安排人员或施工，未反馈结果"),
        ("done", "已完成", "物业填写处理反馈并办结"),
    ]
    i = 0
    for key, label, note in plan:
        cnt = orders.get(key, 0) or 0
        rr = hdr + 1 + i
        sh.cell(rr, 1, label, S.text)
        sh.cell(rr, 2, cnt, S.num)
        sh.cell(rr, 3, _pct(cnt, total), S.pct)
        sh.cell(rr, 4, note, S.text)
        i += 1
    # 库里若有计划外的状态（历史脏数据），原样列出而不是悄悄丢掉
    for key, cnt in orders.items():
        if key in ("total", "open", "doing", "done"):
            continue
        rr = hdr + 1 + i
        sh.cell(rr, 1, STATUS_TEXT.get(key, key), S.text)
        sh.cell(rr, 2, cnt, S.num)
        sh.cell(rr, 3, _pct(cnt, total), S.pct)
        sh.cell(rr, 4, "状态值未归类，请核对", S.text)
        i += 1

    tr = hdr + 1 + i
    sh.cell(tr, 1, "合计", S.total)
    sh.cell(tr, 2, total, S.total_num)
    sh.cell(tr, 3, 1.0 if total else None, S.total_pct)
    sh.cell(tr, 4, "", S.total)

    # ---- 结论区
    done = orders.get("done", 0) or 0
    ovd = d.get("overdue_count")
    kr = tr + 2
    rows = [
        ("办结率", f"{done / total * 100:.1f}%" if total else "—",
         "办结数 ÷ 工单总数"),
        ("超期未办结", f"{ovd} 件" if ovd is not None else "见督办模块",
         "超过 SLA 时限仍未办结，需在月报说明原因"),
        ("SLA 分级", "紧急 4h / 较急 24h / 一般 72h",
         "按知识库条目分级，未命中按 72h 宽限，宁可宽限不误报"),
    ]
    for j, (k, v, note) in enumerate(rows):
        rr = kr + j
        sh.cell(rr, 1, k, S.label)
        sh.cell(rr, 2, v, S.text)
        sh.merge(f"B{rr}:C{rr}")
        sh.cell(rr, 4, note, S.note)
        sh.height(rr, 22)
    return sh


# ---------------------------------------------------------------- 明细

_TYPE_ORDER = ["报修", "人口变动", "走访", "调解", "咨询", "隐患排查", "其他"]


def _sheet_items(wb, r, d):
    rows = d.get("item_rows") or []
    trunc = d.get("item_rows_truncated") or 0
    sh = wb.add("业务项明细", widths=[6, 12, 52, 22, 17],
                freeze=(4, 0), autofilter=f"A4:E{4 + max(len(rows), 1)}",
                tab_color="2F9E44", landscape=True)
    row = _block(sh, 1, "本月业务项明细",
                 f"共 {d.get('item_count', len(rows))} 个业务项"
                 + (f"，本表列前 {len(rows)} 个" if trunc else "")
                 + "；事项摘要已脱敏，不含居民姓名与联系方式", cols=5)
    hdr = row
    sh.row(hdr, ["序号", "类型", "事项摘要", "业务线", "录入时间"],
           styles_=[S.head, S.head, S.head_l, S.head_l, S.head])

    if not rows:
        sh.cell(hdr + 1, 1, "—", S.text_c)
        sh.cell(hdr + 1, 2, "本月无业务项", S.text)
        sh.merge(f"B{hdr + 1}:E{hdr + 1}")
        return sh

    for i, it in enumerate(rows, 1):
        rr = hdr + i
        sh.cell(rr, 1, i, S.text_c)
        sh.cell(rr, 2, it.get("type") or "—", S.text)
        sh.cell(rr, 3, it.get("subject") or "", S.text)
        sh.cell(rr, 4, it.get("line") or "—", S.text)
        sh.cell(rr, 5, _as_dt(it.get("created")), S.stamp)
        sh.height(rr, 20)

    if trunc:
        nr = hdr + len(rows) + 2
        sh.cell(nr, 1, f"其余 {trunc} 个业务项未列入；完整明细请导出工单/台账台账页数据。", S.note)
        sh.merge(f"A{nr}:E{nr}")
    return sh


def _as_dt(v):
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v)[:19])
    except ValueError:
        return None


# ---------------------------------------------------------------- 说明

def _sheet_notes(wb, r, d):
    sh = wb.add("填报说明", widths=[20, 96], tab_color="868E96")
    row = _block(sh, 1, "填报说明与口径", "本页是报表的组成部分，随表报送", cols=2)
    hdr = row
    sh.row(hdr, ["项目", "内容"], styles_=[S.head_l, S.head_l])
    sh.merge(f"A{hdr}:B{hdr}")

    period = d.get("period") or r.period or ""
    lines = d.get("line_counts") or {}
    gen = r.generated_at.strftime("%Y-%m-%d %H:%M") if r.generated_at else "—"
    items = [
        ("统计口径", f"本表统计区间为 {_range_text(d) or period}，"
                     "按记录的创建时间归属月份；跨月办理的事项计入受理当月，"
                     "不在办结月重复计数。"),
        ("数据来源", "社区工作者首录 → 大模型拆解为业务项 → 派发为工单，"
                     "各环节记录本身即数据源；本表由系统按月汇总，无人工二次录入。"),
        ("脱敏与保密", "身份证号、手机号等在入库前由本地模型识别并脱敏，"
                       "审计日志只记录脱敏后的摘要；本表不含任何居民明文信息，"
                       "可对外报送。"),
        ("生成方式", f"由报表 Agent 于 {gen} 自动汇总生成，"
                     "全部推理在本地完成，数据未出社区内网；人工未做数字修改。"),
        ("事项覆盖", f"本期覆盖 {len(lines)} 条业务线、"
                     f"{len(d.get('type_counts') or {})} 类业务，"
                     f"共 {d.get('item_count', 0)} 个业务项。"),
        ("责任追溯", f"报表编号 R{r.id:06d}，与系统内审计日志一一对应；"
                     "如需核对某条数据，可按编号反查首录原文与派发记录。"),
        ("导出说明", "本文件为报表快照的排版件，数字与系统内该期报表完全一致；"
                     "若需更新数据，请重新生成报表后再次导出，历史版本保留可追溯。"),
    ]
    for j, (k, v) in enumerate(items):
        rr = hdr + 1 + j
        sh.cell(rr, 1, k, S.label)
        sh.cell(rr, 2, v, S.text)
        sh.height(rr, max(24, 16 * (len(v) // 44 + 1)))

    fr = hdr + len(items) + 3
    sh.cell(fr, 1, "报送单位（盖章）", S.label)
    sh.cell(fr, 2, "", S.text)
    sh.cell(fr + 1, 1, "负责人签字", S.label)
    sh.cell(fr + 1, 2, "", S.text)
    sh.cell(fr + 2, 1, "报送日期", S.label)
    sh.cell(fr + 2, 2, "", S.text)
    for x in range(3):
        sh.height(fr + x, 30)
    return sh


# ---------------------------------------------------------------- 入口

def build(r, mark: str = "") -> tuple[bytes, str]:
    """Report 行 → (xlsx 字节, 下载文件名)

    只读 data_json，不查库（见模块 docstring）。

    `mark` 是导出水印（姓名·单位·时间·动作），由 `app/policy.py` 的导出决策
    要求生成。为空时行为与改造前完全一致——统计与评测脚本调 build(r) 不受影响。
    """
    try:
        d = json.loads(r.data_json or "{}")
    except (TypeError, ValueError):
        d = {}

    wb = Workbook()
    _sheet_overview(wb, r, d)
    _dist_sheet(wb, "业务线分布", d.get("line_counts") or {},
                d.get("item_count", 0) or 0, "业务线",
                "业务线口径：物业 / 综治 / 民政 / 卫健 / 街道。"
                "一个业务项可能同时派往多条业务线，故本表合计可能大于业务项总数。")
    _dist_sheet(wb, "业务类型分布", d.get("type_counts") or {},
                d.get("item_count", 0) or 0, "业务类型",
                "类型口径：报修 / 人口变动 / 走访 / 调解 / 咨询 / 隐患排查 / 其他，"
                "分类由模型按枚举判定，不接受自由文本，保证可汇总。")
    _sheet_orders(wb, r, d)
    _sheet_items(wb, r, d)
    _sheet_notes(wb, r, d)

    period = (d.get("period") or r.period or "").strip() or "月报"
    org = (d.get("org") or "").strip()
    name = f"{period}{org}社区治理月报" if org else f"{period}社区治理月报"
    name = re.sub(r'[\\/:*?"<>|\r\n]', "_", name)[:60]

    if mark:
        # 水印只打在第一张（总览）上：它在打印时当封面，追责够用；
        # 每张都打会把 5 张表的页眉全占掉一行，正常阅读反而变差。
        # `wb.sheets` 是 app/xlsx.py 自研写入器的字段名（不是 openpyxl 的 worksheets）。
        from app.watermark import apply_xlsx
        if wb.sheets:
            apply_xlsx(wb.sheets[0], mark)

    return wb.to_bytes(), f"{name}.xlsx"
