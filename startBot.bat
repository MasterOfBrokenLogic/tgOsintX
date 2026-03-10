@echo off
title TG to Num Bot

:loop
echo [%date% %time%] Starting bot...
python bot.py
echo [%date% %time%] Bot crashed or stopped. Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto loop