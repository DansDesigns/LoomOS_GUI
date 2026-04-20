#!/bin/bash
# ========================================== 
# ========== LoomOS GUI Installer ========== 
# ========================================== 
set -e

echo "=== LoomOS_GUI Installer ==="

sudo apt install nala nala -y
sudo nala update -y

# ========== Install system packages ========== 
sudo nala install -y \
    git alacrity htop \
    python3 python3-pip python3-tk \
    espeak espeak-ng libespeak-ng1


# ========== Install Python packages ========== 
echo "Installing Python dependencies..."
pip3 install --upgrade pip
pip3 install \
    pyttsx3 \
    vosk \
    numpy \
    pygame \
    psutil \
    opencv-python \
    pynput


# ========== Clone LoomOS repo ========== 
echo "Cloning LoomOS repository..."
git clone https://github.com/DansDesigns/LoomOS_GUI.git "$HOME/LoomOS"

# ========== Create app directory ========== 
mkdir -p "$HOME/.loomos_apps"

# ========== Copy App folder contents ========== 
if [ -d "$HOME/LoomOS/Apps" ]; then
    echo "Installing LoomOS apps..."
    cp -r "$HOME/LoomOS/Apps/"* "$HOME/.loomos_apps/"
fi

# ========== Copy Configs folder contents ========== 
if [ -d "$HOME/LoomOS/Configs" ]; then
    echo "Installing config files..."
    cp -r "$HOME/LoomOS/Configs/"* "$HOME/.config/"
fi

# ========== END ========== 

echo "=== Installation Complete ==="
echo "Repo installed to: $HOME/LoomOS"
echo "Apps installed to: $HOME/.loomos_apps"
echo "Configs installed to: $HOME/.config"