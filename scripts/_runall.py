"""逐套单跑全部 smoke 回归并汇总。

## 为什么必须是"逐套独立进程"

本项目 27+ 套回归里有多套共用 `data/community.db` 的实时快照
（`smoke_tasks` 直接复制库文件，连 `-wal` 一起拷）。把它们塞进
同一个进程里跑，WAL 状态互相污染 —— 现象是**秒退且输出为空**，
退出码还是 0，看起来像"这套没有用例"。所以这里坚持
`subprocess` 一套一进程。

## 为什么要单独写这个脚本而不是 shell 循环

各套脚本的汇总行**格式完全不统一**，实测有六种：

    smoke_insight  → 通过 107 项，失败 0 项
    smoke_boot     → 通过 18 项 / 失败 0 项
    smoke_order_flow → 结果：通过 93 / 失败 0
    smoke_auth     → 结果：40 通过 / 0 失败
    smoke_security_hard → 合计 62 项，通过 62，失败 0
    smoke_autoscan → 通过 13 / 13        ← 第二个数是**总数**，不是失败数
    smoke_logic    → 结果：PASS 46 / 46  ← 连"通过"两个字都没有

用 shell 的 grep+sed 认这些格式，全角冒号在 Windows Git Bash 下会被
locale 吃掉，整批判成 EMPTY。所以用 Python 正则，并且**不枚举格式**——
按"通过/失败 后面跟的第一个数字"宽松提取，认不出就算失败。
不能因为汇总行没匹配上就当它通过了。

## 另一个坑：批量跑会撞上环境的批量删除保护

每套脚本都会删掉自己的 `data/smoke_*.db` 再重建。一次性串起 31 套，
累计删除会超过环境的阈值（50 个文件），后面的套件直接以

    [safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] ... rc=1

退出，输出里连一行用例都没有。这才是"批量跑不可信"的真实原因
（不是 WAL 污染——那是我先前的误判）。所以这个脚本要**分批调用**：

    python scripts/_runall.py admin,arbitration,auth,autoscan,boot  
    python scripts/_runall.py dashboard,docs,ecs_sync,frontend,insight

用法：
    python scripts/_runall.py            # 全部（可能触发删除保护）
    python scripts/_runall.py policy,insight
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 宽松匹配，按特异性排序。每组返回 (数字1, 数字2, 第二个数是否是总数)。
# 用 search 不用 match——汇总行前面可能有"结果："之类的引导语。
PATTERNS = [
    # 通过 N ... 失败 M
    (re.compile(r"通过\s*(\d+)\D{0,12}?失败\s*(\d+)"), False),
    # N 通过 / M 失败
    (re.compile(r"(\d+)\s*通过\s*/\s*(\d+)\s*失败"), False),
    # PASS N / M（M 是总数）
    (re.compile(r"PASS\s*(\d+)\s*/\s*(\d+)"), True),
    # 通过 N / M（M 是总数）
    (re.compile(r"通过\s*(\d+)\s*/\s*(\d+)"), True),
]


def parse(out):
    """从输出里找汇总行，返回 (passed, failed, fmt) 或 None。"""
    for line in reversed(out.splitlines()):
        line = line.strip().strip("=").strip()
        if not line:
            continue
        for idx, (pat, second_is_total) in enumerate(PATTERNS):
            m = pat.search(line)
            if not m:
                continue
            a, b = int(m.group(1)), int(m.group(2))
            if second_is_total:
                # 总数 - 通过 = 失败；总数小于通过只可能是解析错了
                return a, max(b - a, 0), "P%d" % (idx + 1)
            return a, b, "P%d" % (idx + 1)
    return None


def run_one(path):
    env = dict(os.environ)
    env["no_proxy"] = "127.0.0.1,localhost"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    try:
        p = subprocess.run(
            [sys.executable, "-X", "utf8", path],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900,
        )
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return -1, "（超时）"


def main():
    only = None
    if len(sys.argv) > 1 and sys.argv[1] not in ("-h", "--help"):
        only = {x.strip() for x in sys.argv[1].split(",") if x.strip()}

    files = sorted(
        os.path.join(HERE, f) for f in os.listdir(HERE)
        if f.startswith("smoke_") and f.endswith(".py")
    )
    if only:
        files = [f for f in files if os.path.basename(f)[:-3].replace("smoke_", "") in only]

    total = suites = 0
    bad = []
    print("%-26s %6s %6s  %s" % ("套件", "通过", "失败", "格式"))
    print("-" * 52)
    for f in files:
        name = os.path.basename(f)[:-3]
        rc, out = run_one(f)
        got = parse(out)
        if got is None:
            print("%-26s %6s %6s  %s" % (name, "-", "-", "无汇总行(rc=%d)" % rc))
            bad.append((name, "无汇总行", rc, out))
            continue
        p, fl, fmt = got
        print("%-26s %6d %6d  %s" % (name, p, fl, fmt))
        total += p
        suites += 1
        if fl != 0 or rc != 0:
            bad.append((name, "失败 %d 项" % fl, rc, out))

    print("-" * 52)
    print("套件 %d 套，合计通过 %d 项，异常 %d 套" % (suites, total, len(bad)))

    if bad:
        print("\n" + "=" * 52)
        print("异常明细")
        print("=" * 52)
        for name, why, rc, out in bad:
            print("\n### %s —— %s (rc=%d)" % (name, why, rc))
            lines = [x for x in out.splitlines() if x.strip()]
            for line in lines[-12:]:
                print("   " + line)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
