@echo off
REM EDM Docker deploy entry point — runs against Docker Desktop on Windows.
REM Usage: edm-deploy.cmd [up|down|logs|rebuild]
REM Default: up

setlocal
set ACTION=%1
if "%ACTION%"=="" set ACTION=up
cd /d "%~dp0"

if /I "%ACTION%"=="up" (
    if not exist .env (
        echo ERROR: .env missing. Copy .env.example to .env and fill in your keys.
        exit /b 1
    )
    docker compose -f docker-compose.bundle.yml up -d --build
    if errorlevel 1 goto END
    echo.
    echo Waiting for EDM at http://127.0.0.1:8088/api/health ...
    for /l %%i in (1,1,60) do (
        curl -fsS http://127.0.0.1:8088/api/health >nul 2>&1
        if not errorlevel 1 (
            echo EDM is up: http://127.0.0.1:8088/
            echo Open in your browser. The setup wizard will run on first visit.
            goto END
        )
        timeout /t 1 /nobreak >nul
    )
    echo Timed out waiting. App logs:
    docker compose -f docker-compose.bundle.yml logs --tail=80 app
    goto END
)

if /I "%ACTION%"=="down" (
    docker compose -f docker-compose.bundle.yml down
    goto END
)

if /I "%ACTION%"=="logs" (
    docker compose -f docker-compose.bundle.yml logs -f --tail=200
    goto END
)

if /I "%ACTION%"=="rebuild" (
    docker compose -f docker-compose.bundle.yml up -d --build --force-recreate
    goto END
)

echo Usage: %~nx0 [up^|down^|logs^|rebuild]
exit /b 2

:END
endlocal
