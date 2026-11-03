@echo off
chcp 936 >nul
REM 兼容旧入口：转发到根目录启动器，只确保本地模型服务在跑
call "%~dp0..\启动系统.bat" --ollama
