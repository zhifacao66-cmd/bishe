"""人员底册接口。

四个端点：
  · /api/persons          列表（按社区/标签/风险/地址精度筛选）
  · /api/persons/stats    概览统计
  · /api/persons/match    用一段报修地址反查这户有没有重点人员
  · /api/persons/export   按街道下发模板导出 xlsx

三条设计约束（都来自真实数据的形态）：

1. **总数与列表同源**。`total` 走 `count()`，不是 `len(rows)`——
   分页后 `len(rows)` 恒等于 page_size，页面上会显示"共 20 条"。
   项目里统计口径漂移已经出过三次，不再重复。

2. **地址精度如实返回**。41.3% 的记录能到"栋+单元+门号"，其余只到组、
   街道或区。列表里必须带 `addr_precision`，否则看到"住武社区"会以为
   能直接上门，实际那 808 条只到组。

3. **match 端点只认门号级命中**。模糊到"栋"会把整栋楼几十户都算成
   "这户有重点人员"，提级就没有区分度了。
"""
from flask import Blueprint, request, send_file

from app.api import fail, ok
from app.auth import current_user, perm_required
from app.personbook import (EXPORT_SPECS, export_rows, lift_note,
                            match_by_addr, search, stats, visible_communities)

bp = Blueprint("persons", __name__)


def _int(name, default):
    """`?page=abc` 会让 `int()` 抛 ValueError 变成 500。参数脏是常态，兜住。"""
    raw = request.args.get(name, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@bp.get("")
@perm_required("person.view")
def list_persons():
    rows, total, scope = search(
        current_user(),
        keyword=request.args.get("q", ""),
        community=request.args.get("community", ""),
        tag=request.args.get("tag", ""),
        risk=request.args.get("risk", ""),
        precision=request.args.get("precision", ""),
        weight=request.args.get("weight", ""),
        page=_int("page", 1),
        size=_int("size", 20),
    )
    return ok({"items": [p.to_dict() for p in rows], "total": total,
               "scope": scope,
               "page": max(1, _int("page", 1)),
               "size": max(1, min(_int("size", 20), 100))})


@bp.get("/stats")
@perm_required("person.view")
def get_stats():
    return ok(stats(current_user()))


@bp.get("/match")
@perm_required("person.view")
def match():
    """用一段地址反查重点人员。

    返回 `hits` 为空是常态（真实报修里绝大多数户不在底册上），
    接口不把"没找到"当错误——200 + 空列表，前端自己决定怎么提示。
    """
    text = request.args.get("text", "")
    if not text.strip():
        # 必须 `fail(...), 400`：fail 的第二个参数是业务码不是 HTTP 状态码。
        # 只写 fail(...) 会被 Flask 用 200 发出去——监控里就成了"放行"。
        return fail("缺少地址 text"), 400
    persons = match_by_addr(current_user(), text)
    note = lift_note([p for p, _ in persons])
    return ok({
        "hits": [{"name": p.name_masked,
                  "tags": [t.to_dict() for t in p.tags],
                  "age": p.age, "gender": p.gender,
                  "community": p.community} for p, _ in persons],
        "count": len(persons),
        # 提级结论由后端算（personbook.lift_note），页面只显示不判断。
        # 前端自己写阈值的话，一旦命中全是权重 1，页面就会说"应提级"
        # 而系统根本不会提级——说的和做的相反，比不显示更糟。
        "top_weight": note["weight"],
        "verdict": note,
    })


@bp.get("/export")
@perm_required("person.view")
def export():
    """按街道下发模板导出。文件名带日期，社区每月报一次。"""
    import io
    from datetime import datetime

    import openpyxl
    from openpyxl.styles import Alignment, Font

    kind = request.args.get("kind", "")
    spec, header, rows = export_rows(current_user(), kind)
    if spec is None:
        return fail("未知的导出类型：%s" % kind), 400

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ncol = len(header)

    # 大标题行合并（下发模板都是这样：第 1 行整行合并写表名）
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    c = ws.cell(row=1, column=1, value=spec["title"])
    c.font = Font(bold=True, size=14)
    c.alignment = Alignment(horizontal="center", vertical="center")

    # 表头从 spec 指定的行开始（残疾人/防溺水是第 2 行，留守儿童是第 3 行——
    # 后者上面还有一行空白，原表如此，照抄才和街道的模板对得上）
    hrow = spec["header_row"]
    for i, name in enumerate(header, start=1):
        cell = ws.cell(row=hrow, column=i, value=name)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center",
                                   wrap_text=True)
    for r, row in enumerate(rows, start=hrow + 1):
        for i, v in enumerate(row, start=1):
            ws.cell(row=r, column=i, value=v)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d")
    fname = "%s_%s.xlsx" % (spec["title"], stamp)
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument"
                              ".spreadsheetml.sheet")
