@echo off
setlocal disableDelayedExpansion
rmdir /s /q .venv
python -m venv .venv
call ".venv\Scripts\activate.bat"
pip install -r requirements.txt
python loomos_gui.py
pause