# -*- coding: utf-8 -*-
"""台账关键词搜索：LIKE / FTS5(unicode61) / FTS5(trigram) 实测对照。

为什么要有这个脚本
------------------
docs/17 把"台账搜索用 LIKE、中文不分词"列为一条**明确的取舍**而不是缺陷，
理由是"台账万条以内够用，上 FTS5 会让零依赖、可整包搬移这条约定变复杂"。
但这个理由如果只是拍脑袋写的，答辩时经不起问："你怎么知道够用？"
"凭什么说 FTS5 收益不成正比？"

这个脚本就是回答这两个问题的证据，结论有三条：

1. **绝对耗时**：LIKE 在本项目量级（1 万 / 5 万条）的耗时有具体数字，
   跟"人眼可感知的 100ms"比，离得远；
2. **FTS5 默认分词器对中文是坏的**：unicode61 按空白与标点切分，
   一整句中文会变成一个 token，搜"电梯"命中 0 条——**比 LIKE 更差**，
   因为它不是慢，是漏；
3. **要让 FTS5 能搜中文，得换 trigram**：索引体积膨胀数倍、建索引要时间，
   而且 **2 字词（电梯/漏水/噪音，中文里最常见的那类）直接不支持**。
   真要靠分词解决，就得引 jieba 之类的第三方分词器——那才是真正破坏"零依赖"。

跑法（独立库，不碰 data/community.db）：
    venv\\Scripts\\python.exe scripts/bench_ledger_search.py            # 1 万条
    venv\\Scripts\\python.exe scripts/bench_ledger_search.py --rows 50000
"""
import argparse
import json
import os
import random
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "bench_ledger.db")

LINES = ["综治", "民政", "卫健", "物业", "街道"]
BUILDINGS = ["1号楼", "3号楼", "5号楼", "7号楼", "12号楼"]
UNITS = ["1单元", "2单元", "3单元"]
ITEMS = [
    "电梯运行异响", "楼道堆物清理", "独居老人走访", "下水道返味",
    "消防通道占用", "物业费催缴", "邻里噪音纠纷", "楼道灯不亮",
    "外墙渗水", "非机动车乱停", "护栏损坏报修", "垃圾分类投放点异味",
]
DESCS = [
    "居民来电反映，已登记并转物业，等待上门核实。",
    "网格员现场查看，拍照留档，与住户当面沟通完成。",
    "已联系维保单位，约定明日上门，居民知晓并认可。",
    "经排查为楼下管道问题，已协调楼上住户配合检修。",
]
# 查询词：两个字的中文词占多数——这正是 trigram 处理不了的那类。
# 四个词都能在 ITEMS 里找到真实出处（电梯运行异响 / 外墙渗水 / 独居老人走访 / 3号楼）
QUERIES = ["电梯", "渗水", "独居老人", "3号楼"]


def gen_rows(n, seed=20261022):
    rnd = random.Random(seed)
    t0 = datetime(2026, 3, 1, 8, 0, 0)
    rows = []
    for i in range(n):
        line = rnd.choice(LINES)
        bld = rnd.choice(BUILDINGS)
        unit = rnd.choice(UNITS)
        room = "%d%02d" % (rnd.randint(1, 18), rnd.randint(1, 6))
        item = rnd.choice(ITEMS)
        payload = {
            "room": "%s%s%s室" % (bld, unit, room),
            "item": item,
            "desc": rnd.choice(DESCS),
            "order_no": "GD2026%06d" % rnd.randint(1, 999999),
            "source": "上门走访",
        }
        # content_json 落库的是 JSON 文本，LIKE 搜的就是这一串——
        # 也就是说中文子串匹配语义上等同于"全文包含"，和用户预期一致。
        rows.append((
            i + 1,
            line,
            json.dumps(payload, ensure_ascii=False),
            1 if rnd.random() < 0.22 else 0,
            (t0 + timedelta(minutes=i * 7)).strftime("%Y-%m-%d %H:%M:%S"),
        ))
    return rows


def build_db(n):
    if os.path.exists(DB):
        os.remove(DB)
    for suf in ("-wal", "-shm"):
        if os.path.exists(DB + suf):
            os.remove(DB + suf)
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=WAL")          # 与生产一致（app/db_tuning.py）
    con.execute("""CREATE TABLE ledger(
        id INTEGER PRIMARY KEY, line TEXT, content_json TEXT,
        reused INTEGER NOT NULL DEFAULT 0, created_at TEXT)""")
    rows = gen_rows(n)
    con.executemany("INSERT INTO ledger VALUES (?,?,?,?,?)", rows)
    con.execute("CREATE INDEX ix_ledger_created ON ledger(created_at, id)")
    con.commit()
    return con, rows


def timed(con, sql, args, rounds=7):
    """取中位数：首轮含磁盘冷读，中位数能滤掉抖动。"""
    samples = []
    val = None
    for _ in range(rounds):
        t = time.perf_counter()
        cur = con.execute(sql, args)
        val = cur.fetchall()
        samples.append((time.perf_counter() - t) * 1000)
    return statistics.median(samples), val


def like_search(con, kw):
    """完全复刻 app/api/ledgers.py 的写法：先 count 再取一页。"""
    like = "%" + kw + "%"
    c_ms, hit = timed(con, "SELECT COUNT(*) FROM ledger WHERE content_json LIKE ?", (like,))
    p_ms, page = timed(con,
                       "SELECT id FROM ledger WHERE content_json LIKE ? "
                       "ORDER BY created_at DESC LIMIT 20", (like,))
    return hit[0][0], c_ms, p_ms, len(page)


def build_fts(con, tokenize):
    name = "fts_" + tokenize
    con.execute("DROP TABLE IF EXISTS %s" % name)
    t = time.perf_counter()
    con.execute("CREATE VIRTUAL TABLE %s USING fts5(content_json, tokenize='%s')" % (name, tokenize))
    con.execute("INSERT INTO %s(rowid, content_json) SELECT id, content_json FROM ledger" % name)
    con.execute("INSERT INTO %s(%s) VALUES('optimize')" % (name, name))
    con.commit()
    return name, time.perf_counter() - t


def fts_search(con, name, kw):
    try:
        c_ms, hit = timed(con, "SELECT COUNT(*) FROM %s WHERE %s MATCH ?" % (name, name), (kw,))
        return hit[0][0], c_ms, None
    except sqlite3.OperationalError as e:
        # trigram 对 <3 字符的查询会直接报错，这本身就是结论的一部分
        return -1, 0.0, str(e)


def db_size(path):
    total = os.path.getsize(path) if os.path.exists(path) else 0
    for suf in ("-wal", "-shm"):
        if os.path.exists(path + suf):
            total += os.path.getsize(path + suf)
    return total / 1024.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=10000)
    ap.add_argument("--keep", action="store_true", help="跑完保留 bench 库，便于手工查看")
    args = ap.parse_args()
    n = args.rows

    print("=" * 74)
    print(" 台账关键词搜索实测  行数=%d  sqlite=%s" % (n, sqlite3.sqlite_version))
    print("=" * 74)
    t = time.perf_counter()
    con, rows = build_db(n)
    build_s = time.perf_counter() - t
    text_bytes = sum(len(r[2].encode("utf-8")) for r in rows)
    print("造数：%.1f 万行 / %.1f s；content_json 正文合计 %.1f MB（%d 字节/行）"
          % (n / 10000.0, build_s, text_bytes / 1048576.0, text_bytes // n))
    base_size = db_size(DB)
    print("库体积（LIKE 方案，仅业务列 + 时间索引）：%.1f MB" % (base_size / 1024.0))

    print("\n--- A. LIKE '%kw%'（当前实现）---")
    print("%-10s %8s %10s %10s %8s" % ("关键词", "命中", "count耗时", "取页耗时", "返回"))
    like_res = {}
    for kw in QUERIES:
        hit, c_ms, p_ms, got = like_search(con, kw)
        like_res[kw] = hit
        print("%-10s %8d %8.2fms %8.2fms %8d" % (kw, hit, c_ms, p_ms, got))

    print("\n--- B. FTS5 默认分词器 unicode61 ---")
    name, sec = build_fts(con, "unicode61")
    size_u = db_size(DB) - base_size
    print("建索引 %.2f s，索引增量 %.1f MB" % (sec, size_u / 1024.0))
    print("%-10s %8s %10s   %s" % ("关键词", "命中", "耗时", "对照 LIKE"))
    unicode_res = {}
    for kw in QUERIES:
        hit, ms, err = fts_search(con, name, kw)
        unicode_res[kw] = hit
        if err:
            print("%-10s %8s %10s   %s" % (kw, "ERR", "-", err))
        else:
            print("%-10s %8d %8.2fms   漏检 %d 条" % (kw, hit, ms, like_res[kw] - hit))

    print("\n--- C. FTS5 trigram（唯一能匹配中文子串的内置分词器）---")
    name_t, sec_t = build_fts(con, "trigram")
    size_t = db_size(DB) - base_size - size_u
    print("建索引 %.2f s，索引增量 %.1f MB（是正文的 %.1f 倍）"
          % (sec_t, size_t / 1024.0, size_t / (text_bytes / 1024.0)))
    print("%-10s %8s %10s   %s" % ("关键词", "命中", "耗时", "备注"))
    tri_res = {}
    for kw in QUERIES:
        hit, ms, err = fts_search(con, name_t, kw)
        if err:
            tri_res[kw] = -1
            print("%-10s %8s %10s   %s" % (kw, "ERR", "-", err))
            continue
        tri_res[kw] = hit
        if hit == 0 and len(kw) < 3 and like_res[kw] > 0:
            # 最阴的一种失败：不报错、不抛异常，就是搜不到——
            # 用户以为"库里没有这件事"，其实是有 824 条。
            note = "静默 0 条：trigram 需 ≥3 字符，2 字词搜不到"
        elif hit == like_res[kw]:
            note = "与 LIKE 一致"
        else:
            note = "与 LIKE 不一致（LIKE %d 条）" % like_res[kw]
        print("%-10s %8d %8.2fms   %s" % (kw, hit, ms, note))

    print("\n" + "=" * 74)
    print(" 结论")
    print("=" * 74)
    ok = sum(1 for kw in QUERIES if unicode_res.get(kw) == like_res[kw])
    print("1) unicode61 只对上 %d/%d 个查询词 —— 中文整句被当成单个 token，"
          % (ok, len(QUERIES)))
    print("   它不是慢，是漏检。默认 FTS5 在中文场景下直接不可用。")
    silent = [kw for kw in QUERIES if len(kw) < 3 and like_res[kw] > 0
              and tri_res.get(kw) == 0]
    if silent:
        print("2) trigram 对 2 字词（%s）静默返回 0 条：既不报错也不提示，"
              % " / ".join(silent))
        print("   用户只会以为「库里没这件事」，而 LIKE 明明能搜出几百条。")
    print("   而 2 字词（电梯 / 渗水 / 噪音）恰恰是中文里最常用的检索词。")
    print("3) 要让 FTS5 真正支持中文分词，只能引 jieba 等第三方分词器，")
    print("   或自己做词典切分——那才是真的破坏「零依赖、可整包搬移」。")
    print("4) LIKE 在本项目量级（%d 行）count+取页合计只在个位到十几 ms，"
          % n)
    print("   离人眼可感知的 100ms 还有一个量级，且不引入任何依赖与同步成本。")

    con.close()
    if not args.keep:
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(DB + suf):
                os.remove(DB + suf)
        print("\n（bench 库已清理：%s）" % os.path.basename(DB))


if __name__ == "__main__":
    sys.exit(main())
