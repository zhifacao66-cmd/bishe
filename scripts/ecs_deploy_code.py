"""把本机代码更新到阿里云 ECS（只换代码，绝不动服务器上的 data\\）。

用法：
    python scripts/ecs_deploy_code.py            # 更新并核验
    python scripts/ecs_deploy_code.py --check    # 只对比版本，不动服务器
    python scripts/ecs_deploy_code.py --rollback # 回滚到上一次备份

为什么要有这个脚本，而不是"手工 scp 几个文件"：
  1. 这台机器的链路丢包严重（22 端口 TCP connect 约 50% 成功，且连续快速重连会被拒），
     一次调用里连两次基本必失败第二段。所以：**一次 bash 调用只建一个连接**，
     所有远端动作合并成一个 .bat，一次下发。
  2. 中途断在半路是最坏的中间态（代码换了一半、服务停了）。所以先传包、再交换、
     最后重启，而且交换前一定先备份目录。
  3. Windows 的 .bat 有两个必须遵守的规矩，踩过才知道：
       · 行尾必须是 CRLF（只有 LF 时 cmd 会把 if/for 多行结构错解，
         报"2>&1 此时不应有"这类看不懂的错，且前面的行已经执行了）；
       · 不能有中文注释（UTF-8 中文在 rem 行里会被当命令，报"不是内部或外部命令"）。
     所以脚本里现生成 .bat，写的时候就是 ASCII + CRLF。

为什么不顺手同步数据库：演示/比赛期间服务器是写入端，push 会把它抹掉
（见 docs/27 §6.2）。数据流动是人来决定的事，代码更新不是。
"""
import argparse
import os
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = "Administrator@47.108.235.113"
KEY = os.path.join(os.path.expanduser("~"), ".ssh", "ecs_community_agent")
BASE = "http://47.108.235.113"
TMP = os.path.join(ROOT, "data", "_ecsdeploy")
STAMP = time.strftime("%Y%m%d%H%M%S")

# 只更新这三项：app/ 全量、run.py、config.py。
# 不碰 main.py / start_server.bat / llm.json / .secret_key / data/ / snapshot_apply.py，
# 那些是服务器侧的运行环境与数据，属于"本机不该覆盖"的东西。
UPLOAD = ["app", "run.py", "config.py"]
KEEP_REMOTE = ("main.py", "start_server.bat", "sync_once.bat", "llm.json",
               ".secret_key", "data", "logs", "snapshot_apply.py", "fix_orphans.py")

SSH_OPTS = ["-i", KEY, "-o", "ConnectTimeout=45", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=10"]


def run(cmd, tries=4, quiet=False):
    """跑一条本地命令；ssh/scp 失败时重试（链路抖，不是命令错）。"""
    last = ""
    for i in range(tries):
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode == 0:
            return p.stdout
        last = (p.stderr or p.stdout or "").strip()
        if "timed out" not in last and "Connection closed" not in last \
                and "Connection reset" not in last:
            break                          # 不是链路问题，别浪费重试
        if not quiet:
            print(f"  [retry {i + 1}/{tries}] {last.splitlines()[-1][:90]}")
        time.sleep(2 + i * 2)
    raise RuntimeError(last or "命令失败")


def ssh(remote_cmd, **kw):
    return run(["ssh"] + SSH_OPTS + [HOST, remote_cmd], **kw)


def scp(local, remote, **kw):
    return run(["scp"] + SSH_OPTS + [local, f"{HOST}:{remote}"], **kw)


def build_tar(path):
    """打包代码。排除 __pycache__：留着只会让服务器加载到旧字节码。"""
    if os.path.exists(path):
        os.remove(path)
    with tarfile.open(path, "w:gz") as tf:
        for item in UPLOAD:
            src = os.path.join(ROOT, item)
            if os.path.isfile(src):
                tf.add(src, arcname=item)
                continue
            for base, dirs, files in os.walk(src):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for f in files:
                    if f.endswith(".pyc"):
                        continue
                    full = os.path.join(base, f)
                    tf.add(full, arcname=os.path.relpath(full, ROOT))
    return os.path.getsize(path)


# 纯 ASCII + CRLF。原因见模块 docstring 第 3 条。
UPDATE_BAT = r"""@echo off
set LOG=C:\app\_deploy.log
set STAMP={stamp}
echo ==== BACKUP >> %LOG% 2>&1
if exist C:\app\app.bak-{stamp} rd /s /q C:\app\app.bak-{stamp}
xcopy C:\app\app C:\app\app.bak-{stamp} /E /I /Q /Y >> %LOG% 2>&1
if not exist C:\app\run.py.bak-{stamp} copy /y C:\app\run.py C:\app\run.py.bak-{stamp} >> %LOG% 2>&1
if not exist C:\app\config.py.bak-{stamp} copy /y C:\app\config.py C:\app\config.py.bak-{stamp} >> %LOG% 2>&1

echo ==== STOP >> %LOG% 2>&1
rem Kill only the process listening on :80. Do NOT taskkill /im python.exe:
rem ca-sync starts a python every minute; killing it mid-transaction is worse.
set PID80=
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":80 " ^| findstr LISTENING') do set PID80=%%p
echo kill_pid=%PID80% >> %LOG% 2>&1
if defined PID80 taskkill /f /pid %PID80% >> %LOG% 2>&1
schtasks /end /tn community-agent-run >> %LOG% 2>&1

echo ==== REPLACE >> %LOG% 2>&1
if exist C:\app\app rd /s /q C:\app\app
tar -xzf C:\app\_deploy.tar.gz -C C:\app >> %LOG% 2>&1
echo tar_exit=%ERRORLEVEL% >> %LOG% 2>&1
if exist C:\app\app\itemstate.py echo itemstate=YES >> %LOG% 2>&1
if not exist C:\app\app\itemstate.py echo itemstate=NO >> %LOG% 2>&1

echo ==== START >> %LOG% 2>&1
schtasks /run /tn community-agent-run >> %LOG% 2>&1
echo ==== DONE >> %LOG% 2>&1
"""

ROLLBACK_BAT = r"""@echo off
set LOG=C:\app\_rollback.log
echo ==== ROLLBACK {stamp} >> %LOG% 2>&1
set PID80=
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":80 " ^| findstr LISTENING') do set PID80=%%p
if defined PID80 taskkill /f /pid %PID80% >> %LOG% 2>&1
schtasks /end /tn community-agent-run >> %LOG% 2>&1
if exist C:\app\app.bak-{stamp} rd /s /q C:\app\app
if exist C:\app\app.bak-{stamp} xcopy C:\app\app.bak-{stamp} C:\app\app /E /I /Q /Y >> %LOG% 2>&1
if exist C:\app\run.py.bak-{stamp} copy /y C:\app\run.py.bak-{stamp} C:\app\run.py >> %LOG% 2>&1
if exist C:\app\config.py.bak-{stamp} copy /y C:\app\config.py.bak-{stamp} C:\app\config.py >> %LOG% 2>&1
schtasks /run /tn community-agent-run >> %LOG% 2>&1
echo ==== DONE >> %LOG% 2>&1
"""


def write_ascii_crlf(path, text):
    data = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii")
    with open(path, "wb") as fp:
        fp.write(data)
    return path


def healthy(timeout=90):
    """等服务器把 :80 重新监听起来；返回 (ok, 说明)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=15) as r:
                if r.status == 200:
                    import json
                    d = json.loads(r.read().decode())["data"]
                    return True, d
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(3)
    return False, None


def remote_versions():
    """远端版本特征：用 findstr 判断关键文件，输出只看 ASCII 标记。"""
    cmd = (r'findstr /C:"/dashboard" C:\app\app\api\fde.py >nul 2>&1 '
           r'&& echo dashboard=YES || echo dashboard=NO')
    cmd += (r' & if exist C:\app\app\itemstate.py '
            r'(echo itemstate=YES) else (echo itemstate=NO)')
    return ssh(f"cmd /c \"{cmd}\"").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比对版本，不动服务器")
    ap.add_argument("--rollback", action="store_true", help="回滚到指定时间戳的备份")
    ap.add_argument("--stamp", default="", help="回滚用的备份时间戳（--rollback 时必填）")
    args = ap.parse_args()

    if not os.path.exists(KEY):
        print(f"密钥不存在：{KEY}")
        return 2

    if args.check:
        print("[ecs] 远端版本特征：")
        print(remote_versions())
        return 0

    if args.rollback:
        if not args.stamp:
            print("--rollback 需要 --stamp（C:\\app\\app.bak-<stamp> 的时间戳）")
            return 2
        print(f"[ecs] 回滚到 {args.stamp}")
        bat = write_ascii_crlf(os.path.join(TMP, "_rollback.bat"),
                               ROLLBACK_BAT.format(stamp=args.stamp))
        scp(bat, "C:/app/_rollback.bat")
        ssh(r"call C:\app\_rollback.bat")
        ok, d = healthy()
        print("[ecs] 回滚后健康检查：", "OK" if ok else "失败", d or "")
        return 0 if ok else 1

    os.makedirs(TMP, exist_ok=True)
    print("[ecs] 1/4 打包代码（app/ + run.py + config.py，不含 data/）")
    size = build_tar(os.path.join(TMP, "_deploy.tar.gz"))
    print(f"       {size / 1024:.0f} KB")

    print("[ecs] 2/4 上传代码包与执行脚本")
    scp(os.path.join(TMP, "_deploy.tar.gz"), "C:/app/_deploy.tar.gz")
    bat = write_ascii_crlf(os.path.join(TMP, "_deploy.bat"),
                           UPDATE_BAT.format(stamp=STAMP))
    scp(bat, "C:/app/_deploy.bat")

    print("[ecs] 3/4 备份 → 停服 → 换代码 → 启动（一次连接内完成）")
    ssh(r"call C:\app\_deploy.bat")

    print("[ecs] 4/4 等 :80 重新监听")
    ok, d = healthy()
    if not ok:
        print("      服务没起来。回滚：python scripts/ecs_deploy_code.py "
              f"--rollback --stamp {STAMP}")
        return 1
    print("      health ok，llm.available =", d.get("llm", {}).get("available"))

    print("[ecs] 版本特征：")
    print("      " + remote_versions().replace("\r\n", "\n      "))
    print(f"[ecs] 完成。备份目录 C:\\app\\app.bak-{STAMP}（回滚用这个时间戳）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
