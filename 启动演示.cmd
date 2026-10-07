@echo off
chcp 65001 >nul
rem ============================================================
rem  BetterRAG 演示启动器 —— 双击即可全屏演示（离线可跑）
rem ============================================================
setlocal
set "HTML=%~dp0BetterRAG-demo.html"
set "URL=file:///%HTML:\=/%"

if not exist "%HTML%" (
  echo [!] 未找到 BetterRAG-demo.html
  echo     请先运行： python engine\build_demo.py
  pause
  exit /b 1
)

set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "CHROME86=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
set "EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

if exist "%CHROME%"   ( start "" "%CHROME%"   --start-fullscreen --window-size=1920,1080 --app="%URL%" & goto :eof )
if exist "%CHROME86%" ( start "" "%CHROME86%" --start-fullscreen --window-size=1920,1080 --app="%URL%" & goto :eof )
if exist "%EDGE%"     ( start "" "%EDGE%"     --start-fullscreen --window-size=1920,1080 --app="%URL%" & goto :eof )

start "" "%HTML%"
