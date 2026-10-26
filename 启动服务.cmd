@echo off
chcp 65001 >nul
rem ============================================================
rem  BetterRAG 生产服务启动器 —— 双击启动（首次自动种子）
rem  服务地址： http://127.0.0.1:8787/app
rem ============================================================
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if not exist "data\betterrag.db" (
  echo [1/2] 首次启动：生成种子数据...
  python -m engine.cli seed --demo
  if errorlevel 1 (echo [!] 种子失败 & pause & exit /b 1)
)

echo [2/2] 启动服务： http://127.0.0.1:8787/app  （Ctrl+C 停止，状态自动落盘）
start "" http://127.0.0.1:8787/app
python -m engine.cli serve --port 8787
pause
