# -*- coding: utf-8 -*-
"""把压测/长稳的 JSON 结果画成论文用图（PNG, 200dpi）

    python scripts/plot_bench.py
    # 输出 docs/img/fig7_*.png

四张图：
    fig7_1_concurrency  并发-延迟-吞吐（单卡模型串行时的饱和曲线）
    fig7_2_layer        框架层 vs 模型层 的 P95 量级对比
    fig7_3_baseline     加固前后（WAL / 闸门）对比
    fig7_4_soak         长稳时间序列（RSS、P95、显存）
"""
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG = os.path.join(BASE, "docs", "img")
DATA = os.path.join(BASE, "data")
os.makedirs(IMG, exist_ok=True)

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.dpi"] = 200
plt.rcParams["savefig.dpi"] = 200
plt.rcParams["axes.edgecolor"] = "#c9d1d9"
plt.rcParams["axes.labelcolor"] = "#1f2328"
plt.rcParams["text.color"] = "#1f2328"
plt.rcParams["xtick.color"] = "#57606a"
plt.rcParams["ytick.color"] = "#57606a"
plt.rcParams["axes.grid"] = True
plt.rcParams["grid.color"] = "#e6e8eb"
plt.rcParams["grid.linewidth"] = 0.8

C_BLUE, C_RED, C_GREEN, C_ORANGE = "#2f6feb", "#d9534f", "#2e9e5b", "#e08b1a"


def load(name):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        print(f"  [跳过] 缺少 {p}")
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def title(ax, t, sub=""):
    ax.set_title(t, fontsize=13, fontweight="bold", pad=26 if sub else 8, loc="left")
    if sub:
        ax.annotate(sub, xy=(0, 1.0), xycoords="axes fraction",
                    xytext=(0, 9), textcoords="offset points",
                    fontsize=9, color="#57606a", ha="left", va="bottom")


def fig_concurrency(rep):
    rows = rep["modes"]["full"]["rows"]
    lv = [r["level"] for r in rows]
    p50 = [r["lat_p50"] for r in rows]
    p95 = [r["lat_p95"] for r in rows]
    qps = [r["qps"] for r in rows]

    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    ax.bar([str(x) for x in lv], qps, width=0.5, color="#dbe7ff",
           edgecolor=C_BLUE, linewidth=1.0, label="吞吐 QPS（左轴）", zorder=1)
    ax.set_ylabel("吞吐量（请求/秒）", color=C_BLUE)
    ax.tick_params(axis="y", labelcolor=C_BLUE)
    ax.set_ylim(0, max(qps) * 1.9 if max(qps) else 1)

    ax2 = ax.twinx()
    ax2.plot([str(x) for x in lv], p50, "o-", color=C_BLUE, lw=2, ms=5, label="P50 延迟")
    ax2.plot([str(x) for x in lv], p95, "s--", color=C_RED, lw=2, ms=5, label="P95 延迟")
    ax2.set_ylabel("端到端延迟（秒）")
    ax2.set_ylim(0, max(p95) * 1.25)
    ax2.grid(False)

    for x, y in zip([str(x) for x in lv], p95):
        ax2.annotate(f"{y:.1f}s", (x, y), textcoords="offset points", xytext=(0, 8),
                     ha="center", fontsize=8, color=C_RED)

    ax.set_xlabel("并发客户端数")
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=8.5, frameon=False)
    title(ax, "并发升高只增加排队，不增加吞吐",
          "单卡 8GB 显存、模型串行推理（LLM_MAX_CONCURRENCY=1）")
    fig.tight_layout()
    p = os.path.join(IMG, "fig7_1_concurrency.png")
    fig.savefig(p, bbox_inches="tight")
    plt.close(fig)
    print("  ✓", p)


def fig_layer(rep):
    full = {r["level"]: r for r in rep["modes"]["full"]["rows"]}
    light = {r["level"]: r for r in rep["modes"]["light"]["rows"]}
    levels = sorted(set(full) & set(light))
    if not levels:
        return
    x = range(len(levels))
    f = [full[l]["lat_p95"] * 1000 for l in levels]
    li = [light[l]["lat_p95"] * 1000 for l in levels]

    fig, ax = plt.subplots(figsize=(7.2, 3.9))
    w = 0.36
    ax.bar([i - w / 2 for i in x], f, w, color=C_RED, alpha=0.85,
           label="FDE 全链路（含本地模型）")
    ax.bar([i + w / 2 for i in x], li, w, color=C_GREEN, alpha=0.85,
           label="轻接口（框架 + SQLite）")
    ax.set_yscale("log")
    ax.set_ylim(bottom=max(5, min(li) * 0.5), top=max(f) * 4)
    ax.set_xticks(list(x))
    ax.set_xticklabels([f"{l} 并发" for l in levels])
    ax.set_ylabel("P95 延迟（毫秒，对数轴）")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    for i, (a, b) in enumerate(zip(f, li)):
        ax.annotate(f"{a:,.0f}", (i - w / 2, a), textcoords="offset points",
                    xytext=(0, 4), ha="center", fontsize=7.5, color=C_RED)
        ax.annotate(f"{b:,.0f}", (i + w / 2, b), textcoords="offset points",
                    xytext=(0, 4), ha="center", fontsize=7.5, color=C_GREEN)
    ax.legend(fontsize=8.5, frameon=False, loc="upper left", ncol=1)
    title(ax, "瓶颈在模型，不在 Web 框架",
          "同并发下两者相差 2 个数量级：框架层毫秒级，模型层秒级")
    fig.tight_layout()
    p = os.path.join(IMG, "fig7_2_layer.png")
    fig.savefig(p, bbox_inches="tight")
    plt.close(fig)
    print("  ✓", p)


def fig_baseline(opt, base):
    if not opt or not base:
        return
    o = {r["level"]: r for r in opt["modes"]["light"]["rows"]}
    b = {r["level"]: r for r in base["modes"]["light"]["rows"]}
    levels = sorted(set(o) & set(b))
    if not levels:
        return
    x = range(len(levels))
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.4, 3.8))

    w = 0.36
    ax1.bar([i - w / 2 for i in x], [b[l]["lat_p95"] * 1000 for l in levels], w,
            color=C_RED, alpha=0.85, label="未加固（rollback journal）")
    ax1.bar([i + w / 2 for i in x], [o[l]["lat_p95"] * 1000 for l in levels], w,
            color=C_GREEN, alpha=0.85, label="加固后（WAL + busy_timeout）")
    ax1.set_xticks(list(x))
    ax1.set_xticklabels([f"{l} 并发" for l in levels])
    ax1.set_ylabel("P95 延迟（毫秒）")
    for i, l in enumerate(levels):
        ax1.annotate(f"{b[l]['lat_p95']*1000:.0f}", (i - w / 2, b[l]["lat_p95"] * 1000),
                     textcoords="offset points", xytext=(0, 3), ha="center",
                     fontsize=7, color=C_RED)
        ax1.annotate(f"{o[l]['lat_p95']*1000:.0f}", (i + w / 2, o[l]["lat_p95"] * 1000),
                     textcoords="offset points", xytext=(0, 3), ha="center",
                     fontsize=7, color=C_GREEN)
    ax1.set_ylim(0, max(b[l]["lat_p95"] * 1000 for l in levels) * 1.22)
    ax1.legend(fontsize=7.5, frameon=False, loc="upper left")
    ax1.set_title("读写混合负载 P95 延迟", fontsize=11, fontweight="bold", loc="left")

    ax2.bar([i - w / 2 for i in x], [b[l]["qps"] for l in levels], w,
            color=C_RED, alpha=0.85, label="未加固")
    ax2.bar([i + w / 2 for i in x], [o[l]["qps"] for l in levels], w,
            color=C_GREEN, alpha=0.85, label="加固后")
    ax2.set_xticks(list(x))
    ax2.set_xticklabels([f"{l} 并发" for l in levels])
    ax2.set_ylabel("吞吐量（请求/秒）")
    for i, l in enumerate(levels):
        gain = (o[l]["qps"] / b[l]["qps"] - 1) * 100 if b[l]["qps"] else 0
        ax2.annotate(f"+{gain:.0f}%", (i, max(o[l]["qps"], b[l]["qps"])),
                     textcoords="offset points", xytext=(0, 4), ha="center",
                     fontsize=7.5, color=C_GREEN if gain > 0 else "#57606a")
    ax2.set_ylim(0, max(o[l]["qps"] for l in levels) * 1.22)
    ax2.legend(fontsize=7.5, frameon=False, loc="upper left")
    ax2.set_title("吞吐量（越高越好）", fontsize=11, fontweight="bold", loc="left")
    fig.suptitle("SQLite 并发加固前后对比", fontsize=13, fontweight="bold", x=0.01, ha="left")
    fig.tight_layout()
    p = os.path.join(IMG, "fig7_3_baseline.png")
    fig.savefig(p, bbox_inches="tight")
    plt.close(fig)
    print("  ✓", p)


def fig_soak(rep):
    res = rep.get("result") or rep
    s = res.get("samples") or []
    # 丢掉末尾不完整窗口：最后一个采样点可能只覆盖几秒，
    # 里面恰好落了一次 full 请求，P95 会从 20ms 跳到 150ms，把整条曲线压平。
    if s:
        ns = [x.get("win_n") or 0 for x in s]
        med = sorted(n for n in ns if n)[len([n for n in ns if n]) // 2] if any(ns) else 0
        s = [x for x in s if (x.get("win_n") or 0) >= med * 0.35]
    if len(s) < 3:
        return
    s = s[:-1]          # 末尾窗口常被停机动作污染（worker 退出、锁释放），不具代表性
    t = [x["t_min"] for x in s]
    rss = [x.get("rss_mb") or 0 for x in s]
    p95 = [x.get("win_p95_ms") or 0 for x in s]
    gpu = [x.get("gpu_mem_mb") or 0 for x in s]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(7.6, 5.6), sharex=True,
                                   gridspec_kw={"height_ratios": [1.1, 1]})

    ax1.plot(t, p95, "o-", color=C_BLUE, lw=1.6, ms=3, label="窗口 P95 延迟")
    ax1.set_ylabel("P95 延迟（毫秒）", color=C_BLUE)
    ax1.tick_params(axis="y", labelcolor=C_BLUE)
    ax1.set_ylim(0, max(max(p95) * 1.6, 30))
    ax1.axhline(sum(p95) / len(p95), color=C_BLUE, lw=0.9, ls="--", alpha=0.45)
    ax1.annotate(f"均值 {sum(p95)/len(p95):.0f}ms", (t[-1], sum(p95) / len(p95)),
                 xytext=(-4, 6), textcoords="offset points", ha="right",
                 fontsize=8, color=C_BLUE)
    ax1.legend(fontsize=8.5, frameon=False, loc="upper left")
    ax1.set_title("长稳曲线：延迟、内存与显存均无单调恶化",
                  fontsize=13, fontweight="bold", pad=24, loc="left")
    ax1.annotate(f"{res.get('minutes', 0):.0f} 分钟 / {res.get('requests', 0)} 次请求 / "
                 f"RSS 斜率 {res.get('rss_slope_mb_per_h', 0):+.1f} MB/h / "
                 f"漂移 {res.get('p95_drift_pct', 0):+.1f}%",
                 xy=(0, 1.0), xycoords="axes fraction", xytext=(0, 9),
                 textcoords="offset points", fontsize=9, color="#57606a",
                 ha="left", va="bottom")

    ax2.plot(t, rss, "o-", color=C_ORANGE, lw=1.6, ms=3, label="Flask 进程 RSS")
    ax2.set_ylabel("Flask RSS（MB）", color=C_ORANGE)
    ax2.tick_params(axis="y", labelcolor=C_ORANGE)
    lo, hi = min(rss), max(rss)
    ax2.set_ylim(lo - max(2, (hi - lo) * 0.6), hi + max(2, (hi - lo) * 0.6))
    ax2.set_xlabel("运行时间（分钟）")

    ax3 = ax2.twinx()
    if any(gpu):
        ax3.plot(t, gpu, ":", color=C_GREEN, lw=1.4, label="GPU 显存")
        ax3.set_ylabel("GPU 显存（MB）", color=C_GREEN)
        ax3.tick_params(axis="y", labelcolor=C_GREEN)
        ax3.grid(False)
    h2, l2 = ax2.get_legend_handles_labels()
    h3, l3 = ax3.get_legend_handles_labels()
    ax2.legend(h2 + h3, l2 + l3, fontsize=8.5, frameon=False, loc="upper left", ncol=2)

    fig.tight_layout()
    p = os.path.join(IMG, "fig7_4_soak.png")
    fig.savefig(p, bbox_inches="tight")
    plt.close(fig)
    print("  ✓", p)


def main():
    print("生成图表 ->", IMG)
    opt = load("conc_optimized.json")
    base = load("conc_baseline.json")
    soak = load("soak_report.json")
    if opt:
        fig_concurrency(opt)
        fig_layer(opt)
    if opt and base:
        fig_baseline(opt, base)
    if soak:
        fig_soak(soak)


if __name__ == "__main__":
    sys.exit(main())
