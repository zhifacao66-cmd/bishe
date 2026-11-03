#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本机 ↔ 阿里云 ECS 的单向快照同步（比赛/答辩演示用）。

设计取舍先讲清楚，免得以后有人问"为什么不做双向合并"：

- 这台服务器有 SSH 和完整文件系统，**同步只需要搬一个文件**，
  不需要像线上沙箱那样额外写 /api/dbsync/* 接口 + 鉴权 + 防呆引擎（那套要 200 行）。
- 写入锚定在**本机**：本机是唯一写入端，服务器是镜像。
  一个人用就是串行写，不需要行级合并（id 分段 + 全表 updated_at + 墓碑，约 1100 行）。
- 服务器端不替换 .db 文件，而是在事务里覆盖表内容 —— 运行中的进程持有文件句柄，
  换文件在 Windows 上会失败，即使成功也会让连接池指向旧句柄。

## 用法

    python scripts/ecs_sync.py push     # 本机 → 服务器（推完立刻触发一次应用，约 2 秒生效）
    python scripts/ecs_sync.py pull     # 服务器 → 本机（赛后把现场数据拉回来写论文）
    python scripts/ecs_sync.py status   # 两端行数、最后同步时间、待应用快照

远端还有一条每分钟跑一次的兜底计划任务 `ca-sync`：漏推、断网、重启都会在一分钟内自愈。
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import snapshot_apply as sa  # noqa: E402  与服务器端是同一份文件，避免两套逻辑漂移

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DB = os.path.join(ROOT, "data", "community.db")
LOCAL_STATE = os.path.join(ROOT, "data", ".sync_applied.json")
LOCAL_LOG = os.path.join(ROOT, "data", "sync.log")

HOST = os.environ.get("ECS_HOST", "47.108.235.113")
USER = os.environ.get("ECS_USER", "Administrator")
KEY = os.environ.get("ECS_KEY", os.path.expanduser("~/.ssh/ecs_community_agent"))

REMOTE_DIR = r"C:\data-incoming"
REMOTE_INCOMING = REMOTE_DIR + r"\community.db.new"
REMOTE_PY = r"C:\Python313\python.exe"
REMOTE_APPLY = r"C:\app\snapshot_apply.py"
TASK = "ca-sync"

SSH_OPTS = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=NUL",
            "-o", "ConnectTimeout=20", "-o", "BatchMode=yes"]


def _dec(b):
    """Windows 中文控制台的输出是 GBK，Python 自己的日志是 UTF-8，都兜住"""
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("utf-8", "replace")


def ssh(cmd, timeout=180):
    p = subprocess.run(["ssh", "-i", KEY] + SSH_OPTS + ["%s@%s" % (USER, HOST), cmd],
                       capture_output=True, timeout=timeout)
    return p.returncode, _dec(p.stdout).strip(), _dec(p.stderr).strip()


def scp(local, remote, timeout=600):
    p = subprocess.run(["scp", "-i", KEY] + SSH_OPTS + [local, "%s@%s:%s" % (USER, HOST, remote)],
                       capture_output=True, timeout=timeout)
    return p.returncode, _dec(p.stderr).strip()


def _tmp(name):
    d = os.path.join(ROOT, "data", "_sync")
    os.makedirs(d, exist_ok=True)
    _prune(d)
    return os.path.join(d, "%s-%s" % (name, datetime.now().strftime("%Y%m%d-%H%M%S")))


def _prune(d, keep=4):
    """临时快照一份 456KB，不清会越攒越多；只留最近几次，排错足够了"""
    try:
        files = sorted((os.path.join(d, f) for f in os.listdir(d)), key=os.path.getmtime)
        for f in files[:-keep]:
            os.remove(f)
    except Exception:
        pass


def _local_setup():
    """让共享模块在本机把状态/日志写到项目目录，而不是服务器的 C:\\ 路径"""
    sa.DEF_STATE = LOCAL_STATE
    sa.DEF_LOG = LOCAL_LOG


# ---------------------------------------------------------------- push

def push():
    _local_setup()
    snap = _tmp("push") + ".db"
    info = sa.export_snapshot(LOCAL_DB, snap)
    sizes = ", ".join("%s=%s" % (k, info["counts"].get(k)) for k in
                      ("user", "task", "notice", "work_order", "ledger", "kb_doc", "audit_log")
                      if k in info["counts"])
    print("[1/4] 本机快照 %d KB  %s" % (info["bytes"] // 1024, sizes))

    with open(snap + ".sha", "w", encoding="utf-8") as f:
        f.write(info["sha256"] + "\n")
    # 显式推送请求：服务器端的轮询任务只认这个标记。没有它，服务器上自己产生的
    # 数据永远不会被覆盖；有了它，这一次推送一定被应用（哪怕 sha 和上次相同）。
    with open(snap + ".apply", "w", encoding="utf-8") as f:
        f.write(info["sha256"] + "\n")

    rc, err = scp(snap, REMOTE_INCOMING)
    if rc != 0:
        print("上传失败：%s" % err)
        return 1
    # 校验文件最后传：它一到，服务器就知道这份快照是完整的
    rc, err = scp(snap + ".sha", REMOTE_INCOMING + ".sha")
    if rc != 0:
        print("校验文件上传失败：%s" % err)
        return 1
    # 请求标记必须最后传：它落地的瞬间，服务器才认为"这次请真的覆盖"
    rc, err = scp(snap + ".apply", REMOTE_INCOMING + ".apply")
    if rc != 0:
        print("推送请求标记上传失败：%s" % err)
        return 1
    print("[2/4] 已上传到 %s" % REMOTE_INCOMING)

    rc, out, err = ssh(REMOTE_PY + ' ' + REMOTE_APPLY + ' --require-request --restart-task ' + TASK)
    print("[3/4] 服务器应用：")
    for line in (out or err).splitlines():
        print("      " + line)
    if rc != 0:
        print("应用失败（退出码 %d）" % rc)
        return rc

    rc, out, _ = ssh(REMOTE_PY + ' ' + REMOTE_APPLY + ' --status')
    try:
        st = json.loads(out)
    except Exception:
        st = {}
    lc = st.get("live_counts") or {}
    print("[4/4] 服务器现状：%s" % ", ".join("%s=%s" % (k, v) for k, v in lc.items()
                                             if k in ("task", "notice", "work_order", "ledger", "audit_log")))
    print("完成。")
    return 0


# ---------------------------------------------------------------- pull

def pull():
    _local_setup()
    remote_tmp = REMOTE_DIR + r"\export.db"
    rc, out, err = ssh(REMOTE_PY + ' ' + REMOTE_APPLY + ' --export ' + remote_tmp)
    if rc != 0:
        print("服务器导出失败：%s%s" % (out, err))
        return 1
    print("[1/3] 服务器已导出快照")

    got = _tmp("pull") + ".db"
    rc, err = scp("%s@%s:%s" % (USER, HOST, remote_tmp), got)
    if rc != 0:
        print("下载失败：%s" % err)
        return 1
    print("[2/3] 已下载 %d KB" % (os.path.getsize(got) // 1024))

    # 覆盖本机前先备份，出事能退回
    bak = os.path.join(ROOT, "data", "community.db.bak-%s" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    sa.export_snapshot(LOCAL_DB, bak)
    print("      本机旧库已备份：%s" % os.path.basename(bak))

    # 服务器是写入端：pull 的语义就是"让本机等于服务器"，所以强制覆盖。
    # 覆盖前已经把本机旧库备份成 community.db.bak-<ts>，出事能退回。
    rc = sa.apply_snapshot(got, LOCAL_DB, restart_task=None, force=True)
    print("[3/3] 本机应用退出码 %d" % rc)
    return rc


# ---------------------------------------------------------------- status

def status():
    _local_setup()
    c = sqlite3.connect("file:%s?mode=ro" % LOCAL_DB.replace("\\", "/"), uri=True)
    try:
        lc = sa.counts(c)
    finally:
        c.close()
    rc, out, err = ssh(REMOTE_PY + ' ' + REMOTE_APPLY + ' --status')
    try:
        st = json.loads(out)
    except Exception:
        print("取服务器状态失败：%s%s" % (out, err))
        return 1
    rc_counts = st.get("live_counts") or {}
    state = st.get("state") or {}
    keys = ["user", "task", "notice", "work_order", "ledger", "kb_doc", "audit_log"]
    print("%-12s %10s %10s   %s" % ("表", "本机", "服务器", "一致"))
    for k in keys:
        a, b = lc.get(k), rc_counts.get(k)
        print("%-12s %10s %10s   %s" % (k, a, b, "✔" if a == b else "✘ 不一致"))
    print("\n服务器最后应用：%s" % (state.get("applied_at") or "从未"))
    print("待应用快照：%s  %s" % (st.get("incoming_mtime") or "无",
                              ("%d KB" % (st.get("incoming_bytes", 0) // 1024)) if st.get("incoming_bytes") else ""))
    return 0


def main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if cmd == "push":
        return push()
    if cmd == "pull":
        return pull()
    if cmd == "status":
        return status()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
