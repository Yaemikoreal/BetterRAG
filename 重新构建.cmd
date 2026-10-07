@echo off
chcp 65001 >nul
rem 重新生成演示（语料 → 引擎跑批 → 单文件 HTML）
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python engine\build_demo.py || (echo [!] 构建失败 & pause & exit /b 1)
node tools\checkjs.js BetterRAG-demo.html
echo.
echo [OK] 已生成 BetterRAG-demo.html —— 双击「启动演示.cmd」开演
pause
