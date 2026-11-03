# -*- coding: utf-8 -*-
"""论文用示意图：架构图 / 时序图 / Agent 协作图 / 减负成效图

    python scripts/plot_thesis.py
    # 输出 docs/img/fig1_*.png ~ fig4_*.png

与 plot_bench.py 同一套配色与字号，便于论文里并列排版。
图中文字直接取自系统实现（Agent 名、权限点数、表名），不做美化性虚构。

排版约束：每个文字块都按框宽自动收敛字号（CJK 记 1 em、ASCII 记 0.55 em），
避免"框小字大"的溢出——matplotlib 不会自动换行，这是手绘图最常见的翻车点。
"""
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG = os.path.join(BASE, "docs", "img")
os.makedirs(IMG, exist_ok=True)

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.dpi"] = 200
plt.rcParams["savefig.dpi"] = 200

C_BLUE, C_RED, C_GREEN, C_ORANGE = "#2f6feb", "#d9534f", "#2e9e5b", "#e08b1a"
C_TEXT, C_MUTED, C_LINE = "#1f2328", "#57606a", "#c9d1d9"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def new_canvas(w, h):
    fig, ax = plt.subplots(figsize=(w, h))
    # 让坐标轴铺满整张画布：否则默认 subplot 只占约 77% 宽度，
    # 按 figsize 换算的数据单位会偏大，文字必然溢出框（第一版就栽在这）。
    ax.set_position([0, 0, 1, 1])
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")
    return fig, ax


def em_units(s: str) -> float:
    """估算文字宽度：CJK 记 1 em，ASCII 记 0.55 em"""
    return sum(1.0 if ord(c) > 127 else 0.60 for c in s)


def fit_fs(text, width_units, fig_w, base=9.5, pad_pt=8.0):
    """按可用宽度反算字号，保证文字不出框"""
    avail = width_units / 100 * fig_w * 72 - pad_pt
    longest = max((em_units(line) for line in str(text).split("\n")), default=1)
    return max(5.6, min(base, avail / max(longest, 1)))


def rbox(ax, x, y, w, h, text="", fc="#ffffff", ec=C_LINE, lw=1.0, fs=None,
         fig_w=9.6, color=C_TEXT, weight="normal", ha="center", va="center",
         ls="-", zorder=2, alpha=1.0):
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                                boxstyle="round,pad=0,rounding_size=1.1",
                                facecolor=fc, edgecolor=ec, linewidth=lw,
                                linestyle=ls, alpha=alpha, zorder=zorder))
    if text:
        size = fs if fs else fit_fs(text, w * 0.94, fig_w)
        tx = x + w / 2 if ha == "center" else x + 1.3
        ax.text(tx, y + h / 2, text, ha=ha, va=va, fontsize=size,
                color=color, fontweight=weight, linespacing=1.55, zorder=zorder + 1)


def arrow(ax, p1, p2, color=C_MUTED, lw=1.15, style="-|>", ls="-", rad=0.0, z=1):
    ax.add_patch(FancyArrowPatch(p1, p2, arrowstyle=style, mutation_scale=11,
                                color=color, linewidth=lw, linestyle=ls,
                                connectionstyle=f"arc3,rad={rad}", zorder=z,
                                shrinkA=1.5, shrinkB=1.5))


def step_badge(ax, x, y, n, color):
    """步骤序号：实心圆 + 白字，避免 ⑪⑫ 在雅黑下缺字"""
    ax.scatter([x], [y], s=105 if n < 10 else 125, c=color, edgecolors="white",
               linewidths=1.0, zorder=5)
    ax.text(x, y - 0.05, str(n), ha="center", va="center", fontsize=6.6,
            color="white", fontweight="bold", zorder=6)


def save(fig, name):
    p = os.path.join(IMG, name)
    fig.savefig(p, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print("  ✓", p)


# ---------------------------------------------------------------------------
# 图 1：六层架构
# ---------------------------------------------------------------------------
def fig_architecture():
    W = 9.6
    fig, ax = new_canvas(W, 6.9)

    layers = [
        ("表现层", "#e8f0fe", C_BLUE, 11.0,
         ["FDE 首录台", "物业工单", "台账中心", "报表中心", "任务看板", "审计日志", "知识库"]),
        ("接口层", "#e8f0fe", C_BLUE, 8.5,
         ["Flask 蓝图 /api/*", "统一响应封装", "登录与权限守卫", "全局异常降级"]),
        ("调度层", "#fff3e0", C_ORANGE, 8.5,
         ["Orchestrator：保密 → 拆解 → 派单 / 填报 → 台账 → 审计"]),
        ("智能体层", "#fff3e0", C_ORANGE, 11.0,
         ["保密审查", "拆解", "派单", "填报", "报表", "督办", "知识检索"]),
        ("模型数据层", "#e6f4ea", C_GREEN, 11.0,
         ["Ollama + qwen3:8b", "并发闸门 / 降级", "BM25 检索", "SQLite / MySQL", "知识库文件"]),
        ("安全治理", "#fdecea", C_RED, 9.0,
         ["RBAC 12 权限点", "三级脱敏 / 字段密级", "审计只追加", "行级数据隔离", "外网调用 0 次"]),
    ]

    ax.text(4, 97.5, "社区“极简减负”智能体系统 · 分层架构", fontsize=13,
            fontweight="bold", color=C_TEXT, ha="left", va="center")

    y = 92.0
    gap = 2.2
    spans, step = [], (92.0 - 4.0) / len(layers)
    for i, (name, fc, ec, h, items) in enumerate(layers):
        y = 92.0 - i * step - h
        ax.add_patch(Rectangle((4, y), 8.0, h, facecolor=ec, edgecolor="none",
                               alpha=0.88, zorder=2))
        ax.text(8.0, y + h / 2, name, rotation=90, ha="center", va="center",
                fontsize=min(9.2, (h - 2.0) / 100 * 6.9 * 72 / em_units(name)),
                color="white", fontweight="bold", zorder=3)
        ax.add_patch(FancyBboxPatch((12.5, y), 82.5, h,
                                    boxstyle="round,pad=0,rounding_size=1.1",
                                    facecolor=fc, edgecolor=ec, linewidth=0.9,
                                    alpha=0.55, zorder=1))
        n = len(items)
        if n == 1:
            rbox(ax, 30, y + 1.1, 56, h - 2.2, items[0], fc="white", ec=ec, fs=9.2,
                 fig_w=W, color=C_TEXT)
        else:
            pad = 1.1
            bw = (80.3 - pad * (n - 1)) / n
            for j, it in enumerate(items):
                x = 13.6 + j * (bw + pad)
                rbox(ax, x, y + 1.1, bw, h - 2.2, it, fc="white", ec=ec, fig_w=W,
                     color=C_TEXT)
        spans.append((y, y + h))

    for i in range(len(spans) - 1):
        x0 = 53.5
        arrow(ax, (x0, spans[i][0] - 0.1), (x0, spans[i + 1][1] + 0.1),
              color="#9aa4b2", lw=1.0)

    ax.text(4, 1.4, "注：层间为调用关系；“安全治理”横切各层，不是调用链中的一环。",
            fontsize=7.8, color=C_MUTED, ha="left")
    save(fig, "fig1_architecture.png")


# ---------------------------------------------------------------------------
# 图 2：FDE 端到端时序
# ---------------------------------------------------------------------------
def fig_sequence():
    W, H = 9.8, 7.4
    fig, ax = new_canvas(W, H)

    actors = ["社区工作者", "FDE 首录台\n(Orchestrator)", "保密 / 拆解\nAgent",
              "派单 / 填报\nAgent", "数据库 /\n本地模型"]
    xs = [9, 30, 50, 70, 91]
    top = 92
    for x, a in zip(xs, actors):
        rbox(ax, x - 8.0, top - 8.0, 16.0, 8.0, a, fc="#eef4ff", ec=C_BLUE,
             fig_w=W, color=C_TEXT, weight="bold", lw=1.1)
        ax.plot([x, x], [6.5, top - 8.3], ls=(0, (3, 3)), color="#cfd6de", lw=1.0, zorder=0)

    # (from, to, 文案, 颜色, 是否自调用)
    steps = [
        (0, 1, "口述一段话（姓名、电话、门牌）", C_TEXT, False),
        (1, 2, "原文（仅在内存中传递）", C_MUTED, False),
        (2, 3, "强特征优先：身份证 → 手机号\n再按字段名 + 字段值判密级", C_MUTED, False),
        (2, 4, "脱敏文本落库（commit）\n短事务：先落库，后调模型", C_GREEN, False),
        (1, 2, "脱敏文本 + 7 类业务项契约", C_MUTED, False),
        (2, 2, "qwen3:8b 约束解码（JSON Schema）· 1.5 s", C_ORANGE, True),
        (2, 4, "业务项 + 字段密级（L1/L2/L3）", C_GREEN, False),
        (2, 3, "去向含“物业”→ 派单", C_MUTED, False),
        (3, 4, "脱敏工单（SLA 24 h，不含居民信息）", C_GREEN, False),
        (2, 3, "政务线 → 填报（门牌查重，命中即复用）", C_MUTED, False),
        (3, 4, "台账（综治 / 民政 / 卫健 / 街道）", C_GREEN, False),
        (1, 0, "N 条业务项 + 密级徽章 + 政策依据", C_BLUE, True),
    ]

    y = top - 16.0
    for i, (a, b, label, color, is_ret) in enumerate(steps, 1):
        if a == b:                                            # 自调用
            x = xs[a]
            ax.add_patch(FancyArrowPatch((x + 0.8, y + 2.6), (x + 8.0, y + 2.6),
                                         arrowstyle="-", color=color, lw=1.0, zorder=2))
            ax.add_patch(FancyArrowPatch((x + 8.0, y + 2.6), (x + 8.0, y - 1.6),
                                         arrowstyle="-", color=color, lw=1.0, zorder=2))
            ax.add_patch(FancyArrowPatch((x + 8.0, y - 1.6), (x + 0.8, y - 1.6),
                                         arrowstyle="-|>", color=color, lw=1.0,
                                         mutation_scale=10, zorder=2))
            ax.text(x + 9.2, y + 0.5, label, fontsize=7.2, color=color,
                    va="center", ha="left", linespacing=1.5)
            step_badge(ax, x + 4.4, y + 2.6, i, color)
        else:
            x1, x2 = xs[a], xs[b]
            d = 1.0 if x2 > x1 else -1.0
            arrow(ax, (x1 + d * 1.0, y), (x2 - d * 1.0, y), color=color,
                  lw=1.1, ls="-" if not is_ret else "-")
            mid = (x1 + x2) / 2
            ax.text(mid, y + 1.3, label, fontsize=7.2, color=color,
                    ha="center", va="bottom", linespacing=1.5)
            step_badge(ax, x1 + d * 3.2, y, i, color)
        y -= 5.85

    ax.text(4, 2.2, "关键约束：④ 必须早于 ⑥。若把模型推理放在数据库事务内，写锁会被占住约 2 s，"
                    "并发时触发 database is locked（docs/07 第 5 节）。",
            fontsize=7.6, color=C_RED, ha="left")
    ax.text(4, 97.5, "FDE 首录端到端时序（一次录入，多方复用）", fontsize=13,
            fontweight="bold", color=C_TEXT, ha="left", va="center")
    save(fig, "fig2_fde_sequence.png")


# ---------------------------------------------------------------------------
# 图 3：Agent 协作（流程表：环节 / 输入 / 输出 / 审计）
# ---------------------------------------------------------------------------
def fig_agent_flow():
    W = 9.8
    fig, ax = new_canvas(W, 6.4)

    ax.text(4, 96.5, "Agent 协作与数据流", fontsize=13, fontweight="bold",
            color=C_TEXT, ha="left", va="center")
    ax.text(4, 91.5, "Orchestrator 按固定顺序调用，每一步的输入输出即 Agent 契约；"
                     "任一 Agent 异常都降级为规则路径，不中断主链。",
            fontsize=8.0, color=C_MUTED, ha="left", va="center")

    cols = [("环节", 4, 26), ("输入", 31.5, 24), ("输出", 57, 26), ("审计", 84.5, 11.5)]
    for name, x, w in cols:
        rbox(ax, x, 83.5, w, 6.0, name, fc="#f2f5f9", ec=C_LINE, fig_w=W,
             color=C_MUTED, weight="bold", fs=8.6, lw=0.9)

    rows = [
        ("① FDE 首录", "#eef4ff", C_BLUE,
         "口述原文（一段话）", "原始记录（入库前已脱敏）"),
        ("② 保密审查 Agent", "#fdecea", C_RED,
         "原文", "脱敏文本 + 字段密级 L1/L2/L3"),
        ("③ 拆解 Agent", "#fff3e0", C_ORANGE,
         "脱敏文本 + JSON Schema", "N 条业务项（类型 / 摘要 / 去向）"),
        ("④ 派单 / 填报 Agent", "#e6f4ea", C_GREEN,
         "业务项 + 去向业务线", "物业工单（SLA 24 h）/ 台账（复用查重）"),
        ("⑤ 报表 / 督办 Agent", "#fff3e0", C_ORANGE,
         "工单、台账、任务", "街道月报（含环比）/ 超时告警"),
    ]

    h, gap = 10.2, 2.4
    y = 78.5 - h
    for i, (name, fc, ec, inp, out) in enumerate(rows):
        rbox(ax, cols[0][1], y, cols[0][2], h, name, fc=fc, ec=ec, fig_w=W,
             color=C_TEXT, weight="bold", lw=1.1)
        rbox(ax, cols[1][1], y, cols[1][2], h, inp, fc="white", ec=C_LINE, fig_w=W,
             color=C_TEXT, fs=8.0)
        rbox(ax, cols[2][1], y, cols[2][2], h, out, fc="white", ec=C_LINE, fig_w=W,
             color=C_TEXT, fs=8.0)
        rbox(ax, cols[3][1], y, cols[3][2], h, "自动", fc="#fdf6f6", ec=C_RED,
             fig_w=W, color=C_RED, fs=8.0, lw=0.9)
        if i < len(rows) - 1:
            cx = cols[0][1] + cols[0][2] / 2
            arrow(ax, (cx, y - 0.2), (cx, y - gap + 0.2), color=ec, lw=1.1)
        y -= h + gap

    # 底部说明
    ax.add_patch(FancyBboxPatch((4, 3.5), 92, 10.5, boxstyle="round,pad=0,rounding_size=1.0",
                                facecolor="#fafbfc", edgecolor=C_LINE, lw=0.9, zorder=1))
    ax.text(5.6, 11.6, "两个关键设计", fontsize=8.8, fontweight="bold", color=C_TEXT)
    ax.text(5.6, 7.2,
            "① 审计横切：所有 Agent 继承 BaseAgent，run() 前后自动写 audit_log，落库前统一脱敏、只追加不可改；\n"
            "② 失败降级：模型不可用时退回规则拆解（返回 llm_used=false），宁可质量下降也不让业务停摆。",
            fontsize=7.6, color=C_MUTED, va="center", linespacing=1.8)
    save(fig, "fig3_agent_flow.png")


# ---------------------------------------------------------------------------
# 图 4：减负成效
# ---------------------------------------------------------------------------
def fig_benefit():
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.6, 4.1),
                                   gridspec_kw={"width_ratios": [1, 1.15]})

    # ---- 左：录入动作数 ----
    labels = ["传统方式", "本系统"]
    fde, order, ledger = [24, 24], [11, 0], [24, 0]
    x = [0, 1]
    ax1.bar(x, fde, 0.46, color=C_BLUE, label="首录（说一遍）", zorder=2)
    ax1.bar(x, order, 0.46, bottom=fde, color=C_ORANGE, label="工单派发", zorder=2)
    ax1.bar(x, ledger, 0.46, bottom=[f + o for f, o in zip(fde, order)],
            color=C_GREEN, label="台账填报", zorder=2)
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, fontsize=9.5)
    ax1.set_ylabel("录入动作次数", fontsize=9.5)
    ax1.set_ylim(0, 72)
    ax1.grid(axis="y", color="#e6e8eb", lw=0.8)
    ax1.set_axisbelow(True)
    for sp in ("top", "right"):
        ax1.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax1.spines[sp].set_color(C_LINE)
    for i, (f, o, l) in enumerate(zip(fde, order, ledger)):
        ax1.text(i, f + o + l + 2.4, f"{f + o + l}", ha="center", fontsize=12,
                 fontweight="bold", color=C_TEXT)
    for yy, v in ((12, 24), (29.5, 11), (47, 24)):
        ax1.text(0, yy, str(v), ha="center", va="center", fontsize=8.4, color="white")
    ax1.text(1, 12, "24", ha="center", va="center", fontsize=8.4, color="white")
    ax1.annotate("−59.3%", xy=(1, 60), xytext=(0.5, 64), fontsize=11,
                 fontweight="bold", color=C_RED, ha="center",
                 bbox=dict(boxstyle="round,pad=0.3", fc="#fdecea", ec=C_RED, lw=0.9))
    ax1.legend(fontsize=7.8, frameon=False, loc="upper right", handlelength=1.2)
    ax1.set_title("24 条语料：录入动作 59 → 24 次", fontsize=10.5,
                  fontweight="bold", loc="left", pad=10)

    # ---- 右：质量指标（降序） ----
    pairs = [("敏感信息召回", 100.0), ("工单 F1", 100.0), ("类型 F1", 99.0),
             ("数量准确率", 97.7), ("台账 F1", 92.3), ("漏派单率", 0.0),
             ("误派单率", 0.0), ("漏建档率", 0.0)]
    pairs.sort(key=lambda p: -p[1])
    names = [p[0] for p in pairs]
    vals = [p[1] for p in pairs]
    colors = [C_GREEN if v >= 95 else (C_ORANGE if v >= 90 else C_RED) for v in vals]
    colors = [c if v > 0 else "#9aa4b2" for c, v in zip(colors, vals)]
    ypos = list(range(len(names)))
    ax2.barh(ypos, vals, 0.58, color=colors, alpha=0.92, zorder=2)
    ax2.set_yticks(ypos)
    ax2.set_yticklabels(names, fontsize=8.6)
    ax2.invert_yaxis()
    ax2.set_xlim(0, 116)
    ax2.set_xlabel("百分比（%）", fontsize=9.5)
    ax2.axvline(100, color=C_LINE, lw=1, ls=(0, (2, 2)), zorder=1)
    ax2.grid(axis="x", color="#e6e8eb", lw=0.8)
    ax2.set_axisbelow(True)
    for sp in ("top", "right"):
        ax2.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax2.spines[sp].set_color(C_LINE)
    for y_, v in zip(ypos, vals):
        ax2.text(v + 1.8, y_, "0（无）" if v == 0 else f"{v:.1f}", va="center",
                 fontsize=8.2, color=C_MUTED if v == 0 else C_TEXT)
    ax2.set_title("质量未因减负而下降", fontsize=10.5, fontweight="bold",
                  loc="left", pad=10)

    fig.text(0.005, 0.012, "口径：基线按“期望单据”计（24 次记录 + 11 张工单 + 24 条台账），"
                           "刻意不用系统实际产出的 50 张，避免虚增；数据见 docs/06。",
             fontsize=7.4, color=C_MUTED, ha="left")
    fig.tight_layout(rect=(0, 0.045, 1, 1))
    save(fig, "fig4_benefit.png")


if __name__ == "__main__":
    print("生成论文示意图 ...")
    fig_architecture()
    fig_sequence()
    fig_agent_flow()
    fig_benefit()
