#!/bin/bash
# ========================================== 
# ========== LoomOS GUI Installer ========== 
# ========================================== 
set -e

echo "=============================="
echo "    LoomOS GUI Installer"
echo "=============================="
echo""
echo "-------------------------------"
echo "Installing system packages..."
echo "-------------------------------"
sudo apt install nala nala -y
#sudo nala update
sudo nala install -y \
    git alacrity htop python3-venv \
    python3 python3-pip python3-tk \
    espeak espeak-ng libespeak-ng1

echo""
echo "-------------------------------"
echo "Creating virtual environment..."
echo "-------------------------------"

if [ ! -d "$HOME/LoomOS_GUI/.venv" ]; then
    python3 -m venv "$HOME/LoomOS_GUI/.venv"
fi

source "$HOME/LoomOS_GUI/.venv/bin/activate"

echo""
echo "-------------------------------"
echo "Installing Python dependencies..."
echo "-------------------------------"
pip3 install --upgrade pip
pip3 install pyttsx3 vosk numpy pygame psutil opencv-python pynput qtile qtile-extras mypy

pip3 install -r requirements.txt

echo""
echo "=== Installation Complete ==="
echo""

python loomos_gui.py