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
    """按街道下发模板导出。文件名带日期，社区每月报一次。

    用自研 `app/xlsx.py` 而非 openpyxl：线上托管环境的依赖不能确定，
    openpyxl 体量大、又不在 requirements 里，缺了会让导出直接 500
    （2026-11-03 线上就因此全灭）。自研写入器零依赖，格式坑已处理过。

    导出必须走 ABAC 的 R6 那一档：底册是「人」不是「事」，导一次就是几百条
    姓名 / 证件掩码 / 门牌 / 残疾等级离开系统。只靠 `person.view` 不够——
    那管的是"能不能看列表"，而文件一旦下载就脱离了页面，事后只能靠
    水印（截图外传能追到人）和审计（谁在什么时候导了哪张表）找回来。
    报表与公文导出早就是这三件套，底册原来漏了，等于最敏感的那类数据没有留痕。
    """
    import io
    from datetime import datetime

    from app.exportlog import log_export
    from app.policy import decide
    from app.watermark import apply_xlsx
    from app.watermark import text as wm_text
    from app.xlsx import Workbook, styles

    u = current_user()
    kind = request.args.get("kind", "")
    spec, header, rows = export_rows(u, kind)
    if spec is None:
        return fail("未知的导出类型：%s" % kind), 400

    # 底册含个人信息，按「内部」级（2）参与判定：角色天花板够不够、
    # 是否要水印、是否要留痕，全部由 policy 的 R6 一处决定，
    # 不在这个函数里另写一套阈值。
    d = decide(u, "export", "person", secret=2)
    if not d.allow:
        return fail(d.reason), 403
    mark = wm_text(u, "导出底册") if d.watermark else ""

    wb = Workbook()
    ncol = len(header)
    sh = wb.add("Sheet1")

    # 大标题行合并（下发模板都是这样：第 1 行整行合并写表名）
    last_col = _col_name(ncol)
    sh.merge(f"A1:{last_col}1")
    sh.cell(1, 1, spec["title"], styles.title)

    # 表头从 spec 指定的行开始（残疾人/防溺水是第 2 行，留守儿童是第 3 行——
    # 后者上面还有一行空白，原表如此，照抄才和街道的模板对得上）
    hrow = spec["header_row"]
    for i, name in enumerate(header, start=1):
        sh.cell(hrow, i, name, styles.head)
    for r, row in enumerate(rows, start=hrow + 1):
        for i, v in enumerate(row, start=1):
            sh.cell(r, i, v)

    # 水印必须在 to_bytes 之前写入（页眉 + 数据区末行两道，见 watermark.py）
    if mark:
        apply_xlsx(sh, mark)

    buf = io.BytesIO(wb.to_bytes())
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d")
    fname = "%s_%s.xlsx" % (spec["title"], stamp)
    # 审计只记表名与条数，不记姓名：这条轨迹本身也可能被导出
    if d.log:
        log_export("person", f"导出底册 {spec['title']}（{len(rows)} 条）")
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument"
                              ".spreadsheetml.sheet")


def _col_name(n: int) -> str:
    """列号 → Excel 列名（1→A, 26→Z, 27→AA）。"""
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s
