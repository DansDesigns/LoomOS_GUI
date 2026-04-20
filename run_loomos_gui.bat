@echo off
setlocal disableDelayedExpansion
cd /d "C:\LoomOS"
call ".venv\Scripts\activate.bat"
python loomos_gui.py