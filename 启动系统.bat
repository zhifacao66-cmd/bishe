@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
title 社区"极简减负"智能体系统 · 启动器

REM ==============================================================
REM  社区"极简减负"智能体系统  ——  一键启动
REM
REM  双击本文件即可；也支持命令行参数（便于脚本化）：
REM     启动系统.bat            打开菜单
REM     启动系统.bat --app      直接启动系统（不显示菜单）
REM     启动系统.bat --check    只做环境自检
REM     启动系统.bat --seed     重置演示数据（不再二次确认）
REM     启动系统.bat --ollama   只启动本地模型服务
REM     启动系统.bat --stop     停止 Web 服务
REM
REM  注意：本文件必须是 GBK(ANSI) 编码。存成 UTF-8 时，chcp 切换会让
REM        cmd 按字节重读文件，中文行会被截断成乱码命令（实测踩过）。
REM ==============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

if not defined LLM_MODEL set "LLM_MODEL=qwen3:8b"
if not defined OLLAMA_BASE_URL set "OLLAMA_BASE_URL=http://127.0.0.1:11434"
set "OLLAMA_URL=%OLLAMA_BASE_URL%"
set "PORT=%APP_PORT%"
if not defined PORT set "PORT=5000"

REM 本窗口是 936 代码页；显式固定 Python 输出编码，
REM 否则外部环境若有 PYTHONUTF8=1，自检报告会变成乱码。
set "PYTHONUTF8=0"
set "PYTHONIOENCODING=gbk:replace"

set "ACTION=menu"
if /i "%~1"=="--app"    set "ACTION=app"
if /i "%~1"=="--check"  set "ACTION=check"
if /i "%~1"=="--seed"   set "ACTION=seed"
if /i "%~1"=="--ollama" set "ACTION=ollama"
if /i "%~1"=="--stop"   set "ACTION=stop"
if /i "%~1"=="--help"   set "ACTION=help"
if /i "%~1"=="-h"       set "ACTION=help"

REM 命令行调用不暂停、不回到菜单；双击（menu）才用交互模式
set "NOPAUSE="
set "RETURN=menu"
if not "%ACTION%"=="menu" (
    set "NOPAUSE=1"
    set "RETURN=bye"
)

call :findpy
if errorlevel 1 goto :no_python

goto :%ACTION%

REM ==============================================================
REM  菜单
REM ==============================================================
:menu
cls
call :banner
echo   [1] 启动系统       本地模型 + Web 界面，就绪后自动打开浏览器
echo   [2] 环境自检       不启动，逐项体检并给出修复建议
echo   [3] 重置演示数据   清空业务数据，重新灌入 40 条首录（约 10 分钟）
echo   [4] 停止服务       结束占用 %PORT% 端口的进程
echo   [0] 退出
echo.
choice /c 12340 /n /t 15 /d 1 /m "  请选择 [1-4，0=退出]（15 秒无操作默认启动）："
set "SEL=%errorlevel%"
if "%SEL%"=="1" goto :app
if "%SEL%"=="2" goto :check
if "%SEL%"=="3" goto :seed
if "%SEL%"=="4" goto :stop
goto :bye

REM ==============================================================
REM  [1] 启动系统
REM ==============================================================
:app
cls
call :banner
echo   [1/4] 检查是否已有实例在运行 ...
curl -s -m 2 --noproxy "*" "http://127.0.0.1:%PORT%/api/health" >nul 2>&1
if not errorlevel 1 goto :already_running
echo         端口 %PORT% 空闲

echo   [2/4] 检查本地模型服务 ...
call :ensure_ollama

echo   [3/4] 检查依赖 ...
call :ensure_deps

echo   [4/4] 启动 Web 服务 ...
echo.
echo   ------------------------------------------------------------
if not exist "%ROOT%\data\community.db" echo   首次启动：数据库将自动创建，演示前建议先执行菜单 [3]
echo   服务地址 : http://127.0.0.1:%PORT%
echo   演示账号 : wang / wuye / jiedao      密码 123456
echo   就绪后自动打开浏览器；关闭本窗口即停止服务。
echo   ------------------------------------------------------------
echo.

start "" /b "%PY%" "%ROOT%\scripts\open_when_ready.py" "http://127.0.0.1:%PORT%" --timeout 150
"%PY%" run.py

echo.
echo   服务已停止。
call :pause_if
goto :%RETURN%

:already_running
echo         系统已在运行，直接打开浏览器
start "" "http://127.0.0.1:%PORT%"
echo.
call :pause_if
goto :%RETURN%

REM ==============================================================
REM  [2] 环境自检
REM ==============================================================
:check
cls
call :banner
"%PY%" "%ROOT%\scripts\preflight.py"
echo.
call :pause_if
goto :%RETURN%

REM ==============================================================
REM  [3] 重置演示数据
REM ==============================================================
:seed
cls
call :banner
echo   重置会用真实业务链路（拆解 -^> 派单 -^> 填报）重灌 40 条口语化首录，
echo   并把时间回填到过去 45 天，构造工单三态、办结反馈与多月报表。
echo   全程调用本地模型，约 8~15 分钟，中途请勿关闭窗口。
echo.
if defined NOPAUSE goto :seed_go
choice /c YN /n /t 20 /d N /m "  确认清空现有业务数据并重建？[Y/N]（20 秒默认 N）："
if errorlevel 2 goto :%RETURN%

:seed_go
echo.
call :ensure_ollama
echo.
"%PY%" "%ROOT%\scripts\seed_demo.py" --reset
echo.
echo   完成。回到菜单选 [1] 启动系统即可查看效果。
call :pause_if
goto :%RETURN%

REM ==============================================================
REM  [4] 停止服务
REM ==============================================================
:stop
cls
call :banner
call :kill_web
echo.
call :pause_if
goto :%RETURN%

REM ==============================================================
REM  --ollama 只启动模型服务
REM ==============================================================
:ollama
cls
call :banner
call :ensure_ollama
echo.
echo   Ollama 在独立窗口中运行，关闭该窗口即停止模型服务。
pause
goto :%RETURN%

:help
echo.
echo   用法：启动系统.bat [--app ^| --check ^| --seed ^| --ollama ^| --stop ^| --help]
echo.
echo     --app      直接启动系统（不显示菜单）
echo     --check    只做环境自检
echo     --seed     重置演示数据
echo     --ollama   只启动本地模型服务
echo     --stop     停止 Web 服务
echo.
goto :bye

REM ==============================================================
REM  子过程
REM ==============================================================

:banner
echo.
echo   ==========================================================
echo     社区"极简减负"智能体系统   ·   本地一键启动
echo     多智能体协作 + Ollama 本地大模型  ^(%LLM_MODEL%^)
echo   ==========================================================
echo     项目目录 : %ROOT%
echo     Python   : %PY%
echo.
goto :eof

:pause_if
if not defined NOPAUSE pause
goto :eof

REM ---- 找 Python：项目 venv -> 用户级 venv -> py 启动器 -> PATH ----
:findpy
set "PY="
if exist "%ROOT%\.venv\Scripts\python.exe" set "PY=%ROOT%\.venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\bise4\Scripts\python.exe" set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\bise4\Scripts\python.exe"
if not defined PY (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
)
if not defined PY exit /b 1
exit /b 0

:no_python
echo.
echo   [错误] 没有找到 Python 3.10 及以上版本。
echo          安装地址：https://www.python.org/downloads/
echo          安装时请勾选 "Add python.exe to PATH"。
echo.
call :pause_if
goto :bye

REM ---- 确保依赖可导入 ----
:ensure_deps
"%PY%" -c "import flask, flask_sqlalchemy, requests" >nul 2>&1
if not errorlevel 1 (
    echo         依赖齐全
    goto :eof
)
echo         缺少运行库，正在安装（首次约 1 分钟）...
"%PY%" -m pip install -r "%ROOT%\requirements.txt" -q --disable-pip-version-check --timeout 30
if not errorlevel 1 goto :deps_ok
echo         默认源失败，改用清华镜像重试 ...
"%PY%" -m pip install -r "%ROOT%\requirements.txt" -q --disable-pip-version-check --timeout 30 -i https://pypi.tuna.tsinghua.edu.cn/simple
if not errorlevel 1 goto :deps_ok
echo         [警告] 依赖安装失败，请手动执行：
echo                "%PY%" -m pip install -r "%ROOT%\requirements.txt"
echo.
call :pause_if
goto :eof

:deps_ok
echo         依赖安装完成
goto :eof

REM ---- 确保 Ollama 在跑，并提示模型是否已拉取 ----
:ensure_ollama
REM 模型仓库不在默认位置时必须告诉 ollama，否则它会去 C 盘找（找不到就报 model not found）
if not defined OLLAMA_MODELS if exist "F:\ollama-models\manifests" set "OLLAMA_MODELS=F:\ollama-models"
if defined OLLAMA_MODELS echo         模型目录 !OLLAMA_MODELS!
set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if not exist "%OLLAMA_EXE%" (
    for /f "delims=" %%i in ('where ollama 2^>nul') do if not exist "!OLLAMA_EXE!" set "OLLAMA_EXE=%%i"
)

curl -s -m 2 --noproxy "*" "%OLLAMA_URL%/api/version" >nul 2>&1
if not errorlevel 1 goto :ollama_up

if not exist "%OLLAMA_EXE%" goto :ollama_absent
echo         未运行，正在启动（首次加载约 10 秒）...
start "Ollama 模型服务" /min "%OLLAMA_EXE%" serve
set /a _w=0
:ollama_wait
ping -n 2 127.0.0.1 >nul
curl -s -m 1 --noproxy "*" "%OLLAMA_URL%/api/version" >nul 2>&1
if not errorlevel 1 goto :ollama_up
set /a _w+=1
if !_w! lss 25 goto :ollama_wait
echo         [提示] 启动超时，系统将以规则模式运行（功能可用，拆解质量下降）
goto :eof

:ollama_up
echo         已就绪
if not exist "%OLLAMA_EXE%" goto :eof
"%OLLAMA_EXE%" list 2>nul | findstr /i /c:"%LLM_MODEL%" >nul
if not errorlevel 1 (
    echo         模型 %LLM_MODEL% 已拉取
    goto :eof
)
echo         [提示] 未找到模型 %LLM_MODEL%，系统将降级为规则拆解
echo                需要完整效果请执行：ollama pull %LLM_MODEL%   （约 5 GB）
goto :eof

:ollama_absent
echo         [提示] 未安装 Ollama，将以规则模式运行（拆解质量下降）
goto :eof

REM ---- 结束占用 Web 端口的进程 ----
:kill_web
set "PID="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT% .*LISTENING"') do set "PID=%%p"
if not defined PID (
    echo   没有进程在监听 %PORT% 端口，服务可能已停止。
    goto :eof
)
echo   发现进程 PID=%PID%，正在结束 ...
taskkill /F /T /PID %PID% >nul 2>&1
if errorlevel 1 (
    echo   [错误] 结束失败，请以管理员身份重新运行。
) else (
    echo   已停止。
)
goto :eof

:bye
endlocal
exit /b 0
