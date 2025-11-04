@echo off
start cmd /k "python C:\Users\tai80\Desktop\ogiri_duel.py"
timeout /t 3
start cmd /k "C:\Users\tai80\Downloads\ngrok-v3-stable-windows-amd64 (2)\ngrok.exe http 5000"
