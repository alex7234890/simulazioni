@echo off
title MEV Insurance Simulation
cls
echo.
echo  +============================================================+
echo  ^|        MEV INSURANCE  --  SIMULATION LAUNCHER             ^|
echo  +============================================================+
echo.
echo   [A]  ORCHESTRATORE  (raccomandato - avvio guidato completo)
echo   [B]  MANUALE        (avvia componenti separati in finestre)
echo.
set /p scelta="  Scelta [A/B, default=A]: "

if /i "%scelta%"=="B" goto manual
goto orchestrator

:: ─────────────────────────────────────────────────────────────
:orchestrator
echo.
echo  [>>] Avvio Orchestratore...
python scripts/orchestrator.py
if %errorlevel% neq 0 (
    echo.
    echo  [!!] Orchestratore terminato con errore (codice %errorlevel%)
    pause
)
goto end

:: ─────────────────────────────────────────────────────────────
:manual
echo.
echo  [>>] Avvio componenti in finestre separate...
echo.

:: 1. Nodo Hardhat
start "HARDHAT NODE" cmd /k "npx hardhat node"
timeout /t 4 /nobreak >nul

:: 2. Deploy contratti
start "DEPLOY" cmd /k "npx hardhat run scripts/deploy_all.js --network localhost && pause"
timeout /t 2 /nobreak >nul

:: 3. Oracle Daemon (commit-reveal automatico)
start "ORACLE DAEMON" cmd /k "python scripts/oraclec.py"

:: 4. MEV Bot
start "MEV BOT" cmd /k "python scripts/mev_bot.py --account 1"

:: 5. Trader (interattivo)
start "TRADER" cmd /k "python -i scripts/trader.py --account 4"

:: 6. Monitor + report economico (Ctrl+C per report)
start "MONITOR" cmd /k "python scripts/monitor.py"

echo.
echo  [OK] Componenti avviate. Per gestione oracle usa l'Orchestratore [A].
echo.

:end
