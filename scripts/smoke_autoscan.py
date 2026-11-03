"""自动化验证：文件丢进目录后**不重启、不点按钮**也能自动上线

这是「上级文件自动发布」的验收用例。它证明的不是解析器能解析 md——
那是 smoke_portal 的事——而是**后台扫描线程确实在工作**：
启动后目录为空，写入文件，等一轮间隔，库里自己长出公告。

测试用临时目录 + 独立库，间隔压到 2 秒，全程不碰 data/。
"""
import os
import pathlib
import shutil
import sys
import tempfile
import time

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

DB = BASE / "data" / "smoke_autoscan.db"
if DB.exists():
    DB.unlink()

os.environ["DATABASE_URL"] = "sqlite:///" + str(DB)
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"
# 关掉工厂里的自动导入，测试自己控制目录与线程，避免扫到 data/notices 的样例
os.environ["NOTICE_AUTO_IMPORT"] = "0"

from app import create_app                       # noqa: E402
from app.extensions import db                    # noqa: E402
from app.models import Notice                    # noqa: E402
from app.notice.watcher import start_notice_watcher  # noqa: E402

PASS = FAIL = 0
FAILED = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"  [{detail}]" if detail else ""))


COMPLETE = """---
title: 关于开展冬季消防安全检查的通知
issuer: 幸福里街道办事处
doc_no: 幸街发〔2026〕21号
issued_at: 2026-10-21
---
各社区居委会：

为做好冬季火灾防控，请各社区于本月内完成一次消防通道排查，并形成台账。
"""

INCOMPLETE = """---
title: 关于某项工作的补充说明
---
各单位：

现将有关事项补充说明如下。
"""


def wait_until(fn, timeout=8.0, step=0.2):
    """轮询等待，避免用固定 sleep 猜时长"""
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return False


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="notice_watch_"))
    try:
        app = create_app("dev")
        app.config["NOTICE_DIR"] = str(tmp)
        app.config["NOTICE_SCAN_INTERVAL"] = 2

        print("\n[1] 启动时目录为空，库中无公告")
        with app.app_context():
            check("初始公告数 0", Notice.query.count() == 0, Notice.query.count())

        print("\n[2] 启动后台扫描线程")
        stop = start_notice_watcher(app, interval=2)
        check("扫描线程已启动", stop is not None)
        check("线程名正确", any(t.name == "notice-watcher" for t in __import__("threading").enumerate()))

        print("\n[3] 运行期间丢入一份完整文件 → 应自动发布")
        (tmp / "冬季消防检查.md").write_text(COMPLETE, encoding="utf-8")
        ok = wait_until(lambda: _count(app) == 1)
        check("未重启即出现公告", ok, f"当前 {_count(app)} 条")
        with app.app_context():
            n = Notice.query.first()
            check("状态为已发布", n and n.status == "published", n.status if n else None)
            check("标题解析正确", n and "冬季消防安全检查" in n.title, n.title if n else None)
            check("发文单位解析正确", n and n.issuer == "幸福里街道办事处",
                  n.issuer if n else None)

        print("\n[4] 再丢入一份缺发文单位的文件 → 应转待确认")
        (tmp / "补充说明.md").write_text(INCOMPLETE, encoding="utf-8")
        ok = wait_until(lambda: _count(app) == 2)
        check("第二份文件已入库", ok, f"当前 {_count(app)} 条")
        with app.app_context():
            n = Notice.query.filter(Notice.title.contains("补充说明")).first()
            check("缺发文单位 → pending", n and n.status == "pending",
                  n.status if n else None)

        print("\n[5] 幂等：目录不变时反复扫描不重复入库")
        time.sleep(5)          # 让线程空转两轮以上
        with app.app_context():
            check("公告数仍为 2", Notice.query.count() == 2, Notice.query.count())

        print("\n[6] 修改文件内容 → 应更新而非新增")
        (tmp / "冬季消防检查.md").write_text(
            COMPLETE.replace("冬季火灾防控", "冬季火灾防控（修订）"), encoding="utf-8")
        ok = wait_until(lambda: _body_contains(app, "修订"))
        check("内容变更被同步", ok)
        with app.app_context():
            check("未产生重复公告", Notice.query.count() == 2, Notice.query.count())

        print("\n[7] 关闭开关后不再扫描")
        stop.set()
        time.sleep(0.3)
        (tmp / "第三份.md").write_text(COMPLETE.replace("冬季消防", "第三份"), encoding="utf-8")
        time.sleep(3)
        with app.app_context():
            check("停掉线程后不再入库", Notice.query.count() == 2, Notice.query.count())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 46}")
    print(f"通过 {PASS} / {PASS + FAIL}")
    if FAILED:
        print("失败项：")
        for x in FAILED:
            print("  -", x)
    print("=" * 46)
    return 0 if FAIL == 0 else 1


def _count(app):
    with app.app_context():
        return Notice.query.count()


def _body_contains(app, text):
    with app.app_context():
        return any(text in (n.body_md or "") for n in Notice.query.all())


if __name__ == "__main__":
    sys.exit(main())
