@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
title 模型服务 -^> 阿里云 ECS 47.108.235.113

REM ==============================================================
REM  一条命令把本机 GPU 推理接给云服务器 47.108.235.113
REM
REM  依次做四件事：
REM    1) 起 Ollama 11434，并确保它加载的是 F:/ollama-models
REM       （开机自启的 Ollama 往往没带 OLLAMA_MODELS，去 C 盘找，
REM        结果是 /api/tags 空 → 全模型 404 → 通道"验证没通过"）
REM    2) 起网关 11435（校验 X-LLM-Token，不让 11434 裸奔上公网）
REM    3) 申请 Cloudflare 隧道，拿到本次的公网地址
REM    4) 把新地址推到服务器 C:/app/llm.json 并核验 /api/health
REM
REM  用法：
REM    启动模型服务-47.108.235.113.bat              通道 + 推送
REM    启动模型服务-47.108.235.113.bat --no-push    只起通道，不碰服务器
REM
REM  窗口必须一直开着。关掉 = 服务器立刻降级为规则模式
REM  （业务不报错，只是拆解走规则桩，与论文消融基线一致）
REM
REM  本文件是 GBK 编码 + CRLF 行尾，换编码保存会乱码或解析错
REM ==============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

if not defined LLM_MODEL set "LLM_MODEL=qwen3:8b"
if not defined OLLAMA_MODELS if exist "F:/ollama-models/manifests" set "OLLAMA_MODELS=F:/ollama-models"
set "OLLAMA_URL=http://127.0.0.1:11434"
REM 这行是兜底：从 Git Bash 或精简环境调起时 LOCALAPPDATA 可能没导出，
REM 少了它后面找 ollama.exe 会拼出 \Programs\Ollama\ollama.exe 这种路径。
if not defined LOCALAPPDATA set "LOCALAPPDATA=%USERPROFILE%\AppData\Local"
set "ECS_HOST=47.108.235.113"

REM 控制台是 936，Python 必须按 GBK 输出，否则中文全是乱码
set "PYTHONUTF8=0"
set "PYTHONIOENCODING=gbk:replace"

REM ---------- 找 Python：.venv 优先，其次本项目用的 workbuddy 环境 ----------
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
if not defined PY (
    echo [错误] 找不到 Python 3，请安装或创建 .venv 后重试。
    pause
    exit /b 1
)

echo ============================================================
echo   本机模型服务  --^>  阿里云 ECS %ECS_HOST%
echo ------------------------------------------------------------
echo   Python   : !PY!
echo   模型     : %LLM_MODEL%
echo   模型目录 : %OLLAMA_MODELS%
echo ------------------------------------------------------------
echo   这个窗口要一直开着。关掉 = 服务器降级为规则模式。
echo ============================================================
echo.

REM ---------- 第 1 步：Ollama ----------
call :ensure_ollama
if errorlevel 1 (
    echo.
    echo [中止] Ollama 没起来，隧道通了也是空转。
    pause
    exit /b 1
)

REM ---------- 第 2~4 步：网关 + 隧道 + 推送 ----------
set "PUSH=--push-ecs"
if /i "%~1"=="--no-push" set "PUSH="
echo [2/4] 起网关与隧道并验证通道（首次约 30~60 秒）…
echo.
!PY! scripts\start_llm_channel.py !PUSH!
set "RC=!errorlevel!"

echo.
if "!RC!"=="0" (
    echo [结束] 通道已关闭，服务器已降级为规则模式。
) else (
    echo [结束] 通道异常退出，退出码 !RC!。
    echo        若是「连续 5 次没拿到隧道地址」，直接重跑本脚本即可（握手抖动）。
)
pause
exit /b !RC!

REM ==============================================================
REM  子过程：确保 Ollama 在线，且加载的模型目录是对的
REM ==============================================================
:ensure_ollama
if not defined OLLAMA_MODELS (
    echo [1/4] [警告] 没找到 F:/ollama-models/manifests，Ollama 会用 C 盘默认目录。
    echo        若模型报 not found，检查模型是否在 F:/ollama-models。
)

curl -s -m 2 --noproxy "*" "%OLLAMA_URL%/api/version" >nul 2>&1
if errorlevel 1 goto :oe_offline

REM 在线还不够：开机自启的 Ollama 常常没带 OLLAMA_MODELS，模型列表是空的
curl -s -m 5 --noproxy "*" "%OLLAMA_URL%/api/tags" | findstr /i /c:"%LLM_MODEL%" >nul
if not errorlevel 1 goto :oe_ready

echo [1/4] Ollama 在线，但模型列表里没有 %LLM_MODEL%
echo        原因：它启动时没带 OLLAMA_MODELS，去 C 盘默认目录找模型了。
echo        正在重启 Ollama 并指定 %OLLAMA_MODELS% …
taskkill /f /im "ollama app.exe" >nul 2>&1
taskkill /f /im "ollama.exe" >nul 2>&1
ping -n 4 127.0.0.1 >nul
goto :oe_launch

:oe_offline
echo [1/4] Ollama 未运行，正在启动（模型目录 %OLLAMA_MODELS%）…

:oe_launch
set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if exist "!OLLAMA_EXE!" goto :oe_start
for /f "delims=" %%i in ('where ollama 2^>nul') do if not exist "!OLLAMA_EXE!" set "OLLAMA_EXE=%%i"
if not exist "!OLLAMA_EXE!" (
    echo [1/4] 未安装 Ollama：https://ollama.com/download
    exit /b 1
)
:oe_start
start "Ollama 模型服务" /min "!OLLAMA_EXE!" serve

set /a _w=0
:oe_wait
ping -n 2 127.0.0.1 >nul
set /a _w+=1
curl -s -m 1 --noproxy "*" "%OLLAMA_URL%/api/version" >nul 2>&1
if not errorlevel 1 goto :oe_verify
if !_w! lss 30 goto :oe_wait
echo [1/4] 等了 30 秒 Ollama 仍未就绪，检查显存是否被其他程序占满。
exit /b 1

:oe_verify
curl -s -m 5 --noproxy "*" "%OLLAMA_URL%/api/tags" | findstr /i /c:"%LLM_MODEL%" >nul
if not errorlevel 1 goto :oe_ready
echo [1/4] [警告] 模型 %LLM_MODEL% 仍未加载，先执行：ollama pull %LLM_MODEL%
echo        通道仍会启动，但服务器发起推理时会失败。
exit /b 0

:oe_ready
echo [1/4] Ollama 已就绪，模型 %LLM_MODEL% 可用
exit /b 0
