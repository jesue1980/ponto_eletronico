@echo off
cd /d C:\projetos\ponto_eletronico\whatsapp_bridge
set PORT=8080
set WHATSAPP_INSTANCE_NAME=teste-local
"C:\Users\user\AppData\Local\Programs\Python\Python314\pythonw.exe" bridge_daemon.py
