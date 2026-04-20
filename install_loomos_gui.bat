@echo off
setlocal disableDelayedExpansion
cd /d "C:\LoomOS"
rmdir /s /q .venv
python -m venv .venv
call ".venv\Scripts\activate.bat"
pip install -r requirements.txt
python loomos_gui.py
pause