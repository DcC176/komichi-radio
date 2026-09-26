@echo off
setlocal
cd /d "%~dp0.."
set PORT=8901
echo.
echo   Komichi Radio - Official Player Verification Panel
echo   ------------------------------------------------
echo   Serving from : %CD%
echo   Open at      : http://127.0.0.1:%PORT%/proto-official/
echo.
echo   Close this window or press Ctrl+C to stop the server.
echo.
start "" "http://127.0.0.1:%PORT%/proto-official/"
python -m http.server %PORT% --bind 127.0.0.1
