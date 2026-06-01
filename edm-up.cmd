@echo off
REM Windows entry point — defers to WSL Ubuntu where Postgres + Python live.
REM Usage: double-click or run `edm-up` from a Windows shell.

setlocal
wsl -d Ubuntu -- bash -lc "cd '/mnt/c/Users/Asmit Dash/OneDrive/Desktop/codezzz/Claude/edm' && bash scripts/edm-up.sh"
endlocal
