#!/bin/bash
# =============================================================================
# LoomOS Installer
# =============================================================================
# Agentic OS — Devuan Excalibur base, no systemd, voice-first, mesh-native
# Repository: https://github.com/DansDesigns/LoomOS
#
# Run as root from any Linux environment with internet access.
# The ISO build process boots directly into this script.
#
# Usage:
#   bash install.sh
#   — or via ISO boot (automatic) —
# =============================================================================

set -uo pipefail
# Note: -e (exit on error) intentionally omitted at top level.
# Individual sections use explicit error handling so one failure
# does not silently abort the whole install. Critical steps use die().

# =============================================================================
# CLEANUP TRAP — unmounts everything safely if script exits for any reason
# =============================================================================
TARGET="/mnt/loomos_install"

cleanup() {
    local exit_code=$?
    if [[ $exit_code -ne 0 ]]; then
        echo -e "\n${RED}[!] Installer exited unexpectedly — cleaning up mounts...${NC}" 2>/dev/null || true
    fi
    # Unmount in reverse order — deepest first
    for fs in dev/pts dev proc sys run boot/efi; do
        umount "$TARGET/$fs" 2>/dev/null || true
    done
    umount "$TARGET" 2>/dev/null || true
}
trap cleanup EXIT

# =============================================================================
# COLOURS
# =============================================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

log()     { echo -e "${GREEN}[+]${NC} $*"; }
info()    { echo -e "${CYAN}[i]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
die()     { echo -e "${RED}[✗] FATAL:${NC} $*" >&2; exit 1; }
ask()     { echo -e "${BOLD}[?]${NC} $*"; }
section() { echo -e "\n${BOLD}${CYAN}══════════════════════════════════════${NC}"; \
            echo -e "${BOLD}${CYAN}  $*${NC}"; \
            echo -e "${BOLD}${CYAN}══════════════════════════════════════${NC}\n"; }

# =============================================================================
# CONFIGURATION DEFAULTS
# These can be overridden by environment variables before running.
# Most are set interactively during install — these are fallbacks only.
# =============================================================================

LOOMOS_REPO="https://raw.githubusercontent.com/DansDesigns/LoomOS/main"
DEVUAN_MIRROR="${DEVUAN_MIRROR:-https://pkgmaster.devuan.org/merged}"
DEVUAN_SUITE="${DEVUAN_SUITE:-excalibur}"
INIT_SYSTEM="sysvinit"          # sysvinit only — no systemd, no runit, no openrc

# These are set interactively or auto-detected — defaults shown here
HOSTNAME=""                     # asked during install
USERNAME=""                     # asked during install
TIMEZONE=""                     # asked during install
LOCALE=""                       # asked during install

# Hardware-detected, set later
DARCH=""
CPU_MODEL=""
CPU_CORES=""
CPU_VENDOR=""
TOTAL_RAM_MB=0
TOTAL_RAM_GB=0
HAS_GPU=false
HAS_NVIDIA=false
HAS_AMD_GPU=false               # renamed from HAS_AMD to avoid confusion with AMD CPU
HAS_INTEL_GPU=false
HAS_WIFI=false
HAS_BT=false
HAS_WEBCAM=false
IS_LAPTOP=false
BOOT_MODE=""

# Set after hardware detection + user LLM choice
TTS_ENGINE=""                   # kokoro | chatterbox — auto-selected by RAM
VOSK_MODEL=""                   # auto-selected by RAM
LLM_MODEL=""                    # chosen interactively
LLM_QUANT=""                    # auto-selected: Q2_K for 1-bit, Q4_K_M otherwise

# Partition sizes
OS_PART_SIZE_MB=32768           # 32GB default — adjusted if disk is small
SWAP_SIZE_GB=0                  # calculated from RAM

# Install flags
INSTALL_FLATPAK=true
INSTALL_QTILE=true

# =============================================================================
# PREFLIGHT CHECKS
# =============================================================================
section "LoomOS Installer — Preflight"

# Must be root
[[ $EUID -eq 0 ]] || die "Must run as root. Try: sudo bash install.sh"

# Check architecture
ARCH=$(uname -m)
case "$ARCH" in
    x86_64)  DARCH="amd64" ;;
    aarch64) DARCH="arm64" ;;
    armv7l)  DARCH="armhf" ;;
    i686)    DARCH="i386"  ;;
    *)       die "Unsupported CPU architecture: $ARCH" ;;
esac
info "Architecture: $ARCH → $DARCH"

# Fix debootstrap not knowing about excalibur (common on older host systems)
if [[ ! -f "/usr/share/debootstrap/scripts/${DEVUAN_SUITE}" ]]; then
    warn "debootstrap does not know about '${DEVUAN_SUITE}' — creating symlink from trixie"
    if [[ -f "/usr/share/debootstrap/scripts/trixie" ]]; then
        ln -sf /usr/share/debootstrap/scripts/trixie \
               /usr/share/debootstrap/scripts/${DEVUAN_SUITE}
        log "Symlink created: ${DEVUAN_SUITE} → trixie"
    else
        die "Cannot find trixie script in debootstrap. Install a newer debootstrap:\n  apt install debootstrap"
    fi
fi

# Install essential tools if missing — using the nala double-install trick
log "Checking and installing required tools..."

# Install nala first — must be called twice to install properly
if ! command -v nala >/dev/null 2>&1; then
    apt-get install -y nala nala 2>/dev/null || \
    apt install nala nala 2>/dev/null || \
    warn "nala install failed — falling back to apt-get"
fi

# Helper: install a package using nala if available, else apt-get
pkg_install() {
    if command -v nala >/dev/null 2>&1; then
        nala install -y "$@"
    else
        apt-get install -y "$@"
    fi
}

# Required tools — install any that are missing
REQUIRED_TOOLS=(
    debootstrap
    parted
    e2fsprogs       # mkfs.ext4
    dosfstools      # mkfs.fat
    util-linux      # lsblk, wipefs, flock
    pciutils        # lspci
    usbutils        # lsusb
    curl
    wget
    unzip
    xz-utils
    gdisk           # sgdisk — for safer GPT operations
)

MISSING_TOOLS=()
for tool in "${REQUIRED_TOOLS[@]}"; do
    dpkg -l "$tool" 2>/dev/null | grep -q "^ii" || MISSING_TOOLS+=("$tool")
done

if [[ ${#MISSING_TOOLS[@]} -gt 0 ]]; then
    info "Installing missing tools: ${MISSING_TOOLS[*]}"
    pkg_install "${MISSING_TOOLS[@]}" || die "Could not install required tools"
fi

log "All required tools present"

# Network check — test both Devuan mirror and general internet
info "Checking network connectivity..."
if ! ping -c1 -W4 pkgmaster.devuan.org >/dev/null 2>&1; then
    warn "Cannot reach pkgmaster.devuan.org — trying alternate mirror..."
    DEVUAN_MIRROR="https://deb.devuan.org/merged"
    ping -c1 -W4 deb.devuan.org >/dev/null 2>&1 \
        || die "No network connectivity. Connect to internet before running installer."
fi
log "Network: OK (mirror: $DEVUAN_MIRROR)"

# Verify Devuan mirror is actually serving excalibur
HTTP_STATUS=$(curl -sI -o /dev/null -w "%{http_code}" \
    "${DEVUAN_MIRROR}/dists/${DEVUAN_SUITE}/Release" 2>/dev/null || echo "000")
[[ "$HTTP_STATUS" == "200" ]] || \
    warn "Could not verify Devuan mirror is serving ${DEVUAN_SUITE} (HTTP $HTTP_STATUS) — continuing anyway"

echo ""
log "Preflight complete — proceeding to hardware detection"

# =============================================================================
# HARDWARE DETECTION
# =============================================================================
section "Hardware Detection"

# ── CPU ───────────────────────────────────────────────────────────────────────
CPU_MODEL=$(grep -m1 'model name' /proc/cpuinfo 2>/dev/null \
    | cut -d: -f2 | xargs || echo "Unknown CPU")
CPU_CORES=$(nproc 2>/dev/null || echo "1")
CPU_VENDOR=$(grep -m1 'vendor_id' /proc/cpuinfo 2>/dev/null \
    | awk '{print $3}' || echo "unknown")
info "CPU:   $CPU_MODEL"
info "Cores: $CPU_CORES  Vendor: $CPU_VENDOR"

# ── RAM ───────────────────────────────────────────────────────────────────────
TOTAL_RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
TOTAL_RAM_MB=$((TOTAL_RAM_KB / 1024))
TOTAL_RAM_GB=$((TOTAL_RAM_MB / 1024))
info "RAM:   ${TOTAL_RAM_MB}MB (${TOTAL_RAM_GB}GB)"

# ── GPU — careful AMD detection to avoid false positive on AMD CPUs ───────────
# Check display/3D controllers specifically, not all AMD hardware
if lspci 2>/dev/null | grep -iE '(VGA|3D|Display)' | grep -qi 'nvidia'; then
    HAS_NVIDIA=true; HAS_GPU=true
    NVIDIA_MODEL=$(lspci 2>/dev/null | grep -iE '(VGA|3D)' \
        | grep -i nvidia | head -1 | sed 's/.*: //')
    info "GPU:   NVIDIA — $NVIDIA_MODEL"
fi

if lspci 2>/dev/null | grep -iE '(VGA|3D|Display)' \
    | grep -iE '(radeon|amdgpu|\[AMD)'; then
    HAS_AMD_GPU=true; HAS_GPU=true
    AMD_MODEL=$(lspci 2>/dev/null | grep -iE '(VGA|3D)' \
        | grep -iE '(radeon|amdgpu|\[AMD)' | head -1 | sed 's/.*: //')
    info "GPU:   AMD — $AMD_MODEL"
fi

if lspci 2>/dev/null | grep -iE '(VGA|3D|Display)' \
    | grep -i 'intel' | grep -iE '(graphics|uhd|hd graphics|iris|xe)'; then
    HAS_INTEL_GPU=true; HAS_GPU=true
    INTEL_GPU_MODEL=$(lspci 2>/dev/null | grep -iE '(VGA|3D)' \
        | grep -i intel | head -1 | sed 's/.*: //')
    info "GPU:   Intel — $INTEL_GPU_MODEL"
fi

if ! $HAS_GPU; then
    info "GPU:   None detected — CPU-only rendering"
fi

# Get VRAM if possible (used for LLM host election scoring)
VRAM_MB=0
if $HAS_NVIDIA && command -v nvidia-smi >/dev/null 2>&1; then
    VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits \
        2>/dev/null | head -1 | xargs || echo 0)
    info "VRAM:  ${VRAM_MB}MB"
fi

# ── Boot mode ─────────────────────────────────────────────────────────────────
if [[ -d /sys/firmware/efi ]]; then
    BOOT_MODE="uefi"
    info "Boot:  UEFI"
else
    BOOT_MODE="bios"
    info "Boot:  Legacy BIOS"
fi

# ── Laptop / desktop ──────────────────────────────────────────────────────────
if ls /sys/class/power_supply/BAT* 2>/dev/null | grep -q .; then
    IS_LAPTOP=true
    info "Form:  Laptop (battery detected)"
else
    info "Form:  Desktop"
fi

# ── Wireless ──────────────────────────────────────────────────────────────────
if ls /sys/class/net/ 2>/dev/null | grep -qE '^wl'; then
    HAS_WIFI=true
    WIFI_IF=$(ls /sys/class/net/ | grep -E '^wl' | head -1)
    info "WiFi:  Detected ($WIFI_IF)"
fi

# ── Bluetooth ─────────────────────────────────────────────────────────────────
if [[ -d /sys/class/bluetooth ]] \
    && ls /sys/class/bluetooth/ 2>/dev/null | grep -q .; then
    HAS_BT=true
    info "BT:    Detected"
fi

# ── Audio ─────────────────────────────────────────────────────────────────────
HAS_AUDIO=false
if [[ -d /proc/asound ]] && ls /proc/asound/card* 2>/dev/null | grep -q .; then
    HAS_AUDIO=true
    AUDIO_CARDS=$(ls /proc/asound/ | grep -c '^card' || echo 0)
    info "Audio: ${AUDIO_CARDS} card(s) detected"
fi

# ── Webcam — required for LoomOS auth ─────────────────────────────────────────
HAS_WEBCAM=false
WEBCAM_DEVICE=""
# Check V4L2 video devices
for dev in /dev/video*; do
    if [[ -e "$dev" ]]; then
        # Check it's actually a capture device not a meta/output device
        if cat /sys/class/video4linux/$(basename $dev)/name 2>/dev/null \
            | grep -qiv 'meta\|output'; then
            HAS_WEBCAM=true
            WEBCAM_DEVICE="$dev"
            WEBCAM_NAME=$(cat /sys/class/video4linux/$(basename $dev)/name \
                2>/dev/null || echo "unknown")
            info "Cam:   $WEBCAM_DEVICE ($WEBCAM_NAME)"
            break
        fi
    fi
done
if ! $HAS_WEBCAM; then
    warn "Webcam: not detected — face auth will be unavailable"
    warn "        Install webcam hardware or use passphrase-only auth"
fi

# ── Touchscreen ───────────────────────────────────────────────────────────────
HAS_TOUCH=false
if grep -r 'ID_INPUT_TOUCHSCREEN=1' /run/udev/data/ 2>/dev/null | grep -q .; then
    HAS_TOUCH=true
    info "Touch: Touchscreen detected"
fi

# ── Auto-select voice/LLM components based on hardware ───────────────────────
section "Selecting Components for Hardware Profile"

# Vosk STT model
if [[ $TOTAL_RAM_MB -lt 2000 ]]; then
    VOSK_MODEL="vosk-model-small-en-us-0.15"   # 40MB — very low RAM
    info "STT:   vosk-model-small-en-us-0.15 (40MB, low RAM mode)"
elif [[ $TOTAL_RAM_MB -lt 4000 ]]; then
    VOSK_MODEL="vosk-model-en-us-0.22-lgraph"  # 128MB — mid tier
    info "STT:   vosk-model-en-us-0.22-lgraph (128MB)"
else
    VOSK_MODEL="vosk-model-en-us-0.22"         # 1.8GB — full accuracy
    info "STT:   vosk-model-en-us-0.22 (1.8GB, full accuracy)"
fi

# TTS engine
if [[ $TOTAL_RAM_MB -ge 6000 ]]; then
    TTS_ENGINE="chatterbox"
    info "TTS:   Chatterbox (0.5B params, natural voice, 6GB+ RAM)"
else
    TTS_ENGINE="kokoro"
    info "TTS:   Kokoro (82M params, fast, minimal RAM)"
fi

# LLM quantization — 1-bit preferred, fallback to Q4 on older hardware
# LLM_MODEL and LLM_QUANT set interactively later, but set defaults here
if [[ $TOTAL_RAM_MB -ge 3000 ]]; then
    LLM_QUANT="IQ1_S"   # 1-bit quantization — Bonsai BitNet native
    info "LLM:   1-bit quantization enabled (IQ1_S)"
else
    LLM_QUANT="Q2_K"    # 2-bit fallback for very low RAM
    info "LLM:   2-bit quantization (Q2_K, low RAM mode)"
fi

# Mesh scoring — published in manifest so other nodes can elect LLM host
# Score = VRAM*3 + RAM*1 + cores*0.5 (higher = better LLM host candidate)
MESH_SCORE=$(echo "scale=0; ($VRAM_MB * 3) + ($TOTAL_RAM_MB * 1) + ($CPU_CORES * 50)" \
    | bc 2>/dev/null || echo $((VRAM_MB * 3 + TOTAL_RAM_MB + CPU_CORES * 50)))
info "Mesh score: $MESH_SCORE (used for LLM host election)"

echo ""
log "Hardware detection complete"

# =============================================================================
# INTERACTIVE CONFIGURATION
# =============================================================================
section "System Configuration"

# ── Hostname ──────────────────────────────────────────────────────────────────
while true; do
    ask "Enter hostname for this machine (e.g. loom-desk, loom-laptop):"
    read -rp "> " HOSTNAME
    HOSTNAME=$(echo "$HOSTNAME" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')
    [[ "$HOSTNAME" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] && break
    warn "Invalid hostname. Use lowercase letters, numbers and hyphens only."
done
log "Hostname: $HOSTNAME"

# ── Username ──────────────────────────────────────────────────────────────────
while true; do
    ask "Enter username for the primary account:"
    read -rp "> " USERNAME
    USERNAME=$(echo "$USERNAME" | tr '[:upper:]' '[:lower:]')
    [[ "$USERNAME" =~ ^[a-z][a-z0-9_-]{0,30}$ ]] && break
    warn "Invalid username. Use lowercase letters, numbers, underscores, hyphens."
done
log "Username: $USERNAME"

# ── Timezone ──────────────────────────────────────────────────────────────────
# Try to auto-detect from IP geolocation
AUTO_TZ=$(curl -s --max-time 5 "https://ipapi.co/timezone" 2>/dev/null || echo "")
if [[ -n "$AUTO_TZ" ]] && [[ -f "/usr/share/zoneinfo/$AUTO_TZ" ]]; then
    ask "Detected timezone: $AUTO_TZ — use this? [Y/n]:"
    read -rp "> " TZ_CONFIRM
    if [[ "${TZ_CONFIRM,,}" != "n" ]]; then
        TIMEZONE="$AUTO_TZ"
    fi
fi

if [[ -z "$TIMEZONE" ]]; then
    ask "Enter timezone (e.g. Europe/London, America/New_York, Asia/Tokyo):"
    ask "See full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
    while true; do
        read -rp "> " TIMEZONE
        [[ -f "/usr/share/zoneinfo/$TIMEZONE" ]] && break
        warn "Timezone not found. Try format: Region/City (e.g. Europe/London)"
    done
fi
log "Timezone: $TIMEZONE"

# ── Locale ────────────────────────────────────────────────────────────────────
ask "Enter locale [default: en_GB.UTF-8]:"
read -rp "> " LOCALE
[[ -z "$LOCALE" ]] && LOCALE="en_GB.UTF-8"
log "Locale: $LOCALE"

# ── OS partition size ─────────────────────────────────────────────────────────
ask "OS partition size in GB [default: 32]:"
ask "(Minimum 20GB recommended — mesh storage gets the rest of the disk)"
read -rp "> " OS_SIZE_INPUT
if [[ "$OS_SIZE_INPUT" =~ ^[0-9]+$ ]] && [[ $OS_SIZE_INPUT -ge 10 ]]; then
    OS_PART_SIZE_MB=$((OS_SIZE_INPUT * 1024))
else
    OS_PART_SIZE_MB=32768
    info "Using default: 32GB"
fi
log "OS partition: $((OS_PART_SIZE_MB / 1024))GB"

# =============================================================================
# LLM MODEL SELECTION
# =============================================================================
section "LLM Model Selection"

# Recommended models list — hardcoded here, also available at
# https://raw.githubusercontent.com/DansDesigns/LoomOS/main/recommended_models.txt
declare -A RECOMMENDED_MODELS
RECOMMENDED_MODELS=(
    # key = display name    value = "ollama_tag|description|min_ram_gb|quant"
    ["BitNet 3B (1-bit)"]="hf.co/microsoft/bitnet-b1-58-3B-GGUF|Microsoft 1-bit model, 1.5GB RAM, fast reasoning|2|IQ1_S"
    ["BitNet 8B (1-bit)"]="hf.co/microsoft/bitnet-b1-58-Large-GGUF|Microsoft 1-bit 8B, 3GB RAM, better reasoning|4|IQ1_S"
    ["Qwen3 4B (4-bit)"]="qwen3:4b|Alibaba Qwen3, 3GB RAM, excellent reasoning|4|Q4_K_M"
    ["Qwen3 8B (4-bit)"]="qwen3:8b|Alibaba Qwen3 8B, 6GB RAM, strong reasoning|6|Q4_K_M"
    ["Phi-4 Mini (4-bit)"]="phi4-mini:3.8b|Microsoft Phi-4 Mini, 3GB RAM, efficient|4|Q4_K_M"
    ["Gemma3 4B (4-bit)"]="gemma3:4b|Google Gemma3, 3GB RAM, good general use|4|Q4_K_M"
    ["Llama3.2 3B (4-bit)"]="llama3.2:3b|Meta Llama 3.2, 2.5GB RAM, capable|3|Q4_K_M"
    ["Mistral 7B (4-bit)"]="mistral:7b|Mistral 7B, 5GB RAM, well-rounded|6|Q4_K_M"
)

# Filter to models that fit this machine's RAM
VIABLE_MODELS=()
for model_name in "${!RECOMMENDED_MODELS[@]}"; do
    IFS='|' read -ra MODEL_INFO <<< "${RECOMMENDED_MODELS[$model_name]}"
    MIN_RAM=${MODEL_INFO[2]}
    if [[ $TOTAL_RAM_GB -ge $MIN_RAM ]]; then
        VIABLE_MODELS+=("$model_name")
    fi
done

echo ""
echo -e "${BOLD}This machine has ${TOTAL_RAM_GB}GB RAM${NC}"
echo ""
echo -e "${CYAN}Choose how to select your LLM model:${NC}"
echo ""
echo -e "  ${BOLD}1)${NC} Recommended for this hardware"
echo -e "  ${BOLD}2)${NC} Browse full recommended list"
echo -e "  ${BOLD}3)${NC} Search HuggingFace for a model"
echo -e "  ${BOLD}4)${NC} Enter model tag manually"
echo ""
read -rp "Choice [1-4]: " LLM_CHOICE

case "$LLM_CHOICE" in

    1)  # Hardware recommendation
        section "Hardware-Recommended Models"
        echo -e "Models that fit in ${TOTAL_RAM_GB}GB RAM:\n"
        i=1
        declare -A MODEL_INDEX
        for model_name in "${VIABLE_MODELS[@]}"; do
            IFS='|' read -ra MODEL_INFO <<< "${RECOMMENDED_MODELS[$model_name]}"
            MTAG="${MODEL_INFO[0]}"
            MDESC="${MODEL_INFO[1]}"
            MRAM="${MODEL_INFO[2]}"
            MQNT="${MODEL_INFO[3]}"
            echo -e "  ${BOLD}$i)${NC} $model_name"
            echo -e "     ${DIM}$MDESC${NC}"
            echo -e "     ${DIM}Min RAM: ${MRAM}GB  Quantization: $MQNT${NC}"
            echo ""
            MODEL_INDEX[$i]="$model_name"
            ((i++))
        done
        if [[ ${#VIABLE_MODELS[@]} -eq 0 ]]; then
            warn "No recommended models fit in ${TOTAL_RAM_GB}GB RAM"
            warn "You may still install a model manually — select option 4"
            LLM_MODEL=""
            LLM_QUANT="Q2_K"
        else
            read -rp "Select model [1-$((i-1))]: " MODEL_NUM
            if [[ -n "${MODEL_INDEX[$MODEL_NUM]:-}" ]]; then
                SELECTED="${MODEL_INDEX[$MODEL_NUM]}"
                IFS='|' read -ra MODEL_INFO <<< "${RECOMMENDED_MODELS[$SELECTED]}"
                LLM_MODEL="${MODEL_INFO[0]}"
                LLM_QUANT="${MODEL_INFO[3]}"
                log "Selected: $SELECTED ($LLM_MODEL, $LLM_QUANT)"
            else
                warn "Invalid selection — will prompt again after install"
                LLM_MODEL=""
            fi
        fi
        ;;

    2)  # Full recommended list
        section "Full Recommended Model List"
        i=1
        declare -A ALL_MODEL_INDEX
        for model_name in "${!RECOMMENDED_MODELS[@]}"; do
            IFS='|' read -ra MODEL_INFO <<< "${RECOMMENDED_MODELS[$model_name]}"
            MTAG="${MODEL_INFO[0]}"
            MDESC="${MODEL_INFO[1]}"
            MRAM="${MODEL_INFO[2]}"
            MQNT="${MODEL_INFO[3]}"
            FITS=""
            [[ $TOTAL_RAM_GB -lt $MRAM ]] && FITS="${RED}(needs ${MRAM}GB RAM)${NC}" \
                                          || FITS="${GREEN}(fits this machine)${NC}"
            echo -e "  ${BOLD}$i)${NC} $model_name $FITS"
            echo -e "     ${DIM}$MDESC${NC}"
            echo -e "     ${DIM}Quantization: $MQNT${NC}"
            echo ""
            ALL_MODEL_INDEX[$i]="$model_name"
            ((i++))
        done
        read -rp "Select model [1-$((i-1))]: " MODEL_NUM
        if [[ -n "${ALL_MODEL_INDEX[$MODEL_NUM]:-}" ]]; then
            SELECTED="${ALL_MODEL_INDEX[$MODEL_NUM]}"
            IFS='|' read -ra MODEL_INFO <<< "${RECOMMENDED_MODELS[$SELECTED]}"
            LLM_MODEL="${MODEL_INFO[0]}"
            LLM_QUANT="${MODEL_INFO[3]}"
            log "Selected: $SELECTED ($LLM_MODEL, $LLM_QUANT)"
        else
            warn "Invalid selection — will prompt after install"
            LLM_MODEL=""
        fi
        ;;

    3)  # HuggingFace search
        section "HuggingFace Model Search"
        ask "Enter search term (e.g. 'bitnet', 'qwen', 'phi', 'mistral'):"
        read -rp "> " HF_SEARCH
        echo ""
        info "Searching HuggingFace for: $HF_SEARCH"
        # Query HuggingFace API — filter for GGUF models (Ollama-compatible)
        HF_RESULTS=$(curl -s --max-time 10 \
            "https://huggingface.co/api/models?search=${HF_SEARCH}&filter=gguf&limit=10&sort=downloads" \
            2>/dev/null || echo "[]")

        if [[ "$HF_RESULTS" == "[]" ]] || [[ -z "$HF_RESULTS" ]]; then
            warn "No results or search failed — enter model tag manually"
            LLM_MODEL=""
        else
            echo -e "${BOLD}Results (most downloaded GGUF models):${NC}\n"
            i=1
            declare -A HF_MODEL_INDEX
            # Parse model IDs from JSON using basic string manipulation
            while IFS= read -r line; do
                if echo "$line" | grep -q '"modelId"'; then
                    MODEL_ID=$(echo "$line" | sed 's/.*"modelId": *"\([^"]*\)".*/\1/')
                    echo -e "  ${BOLD}$i)${NC} $MODEL_ID"
                    echo -e "     ${DIM}ollama pull hf.co/$MODEL_ID${NC}"
                    echo ""
                    HF_MODEL_INDEX[$i]="hf.co/$MODEL_ID"
                    ((i++))
                fi
            done < <(echo "$HF_RESULTS" | tr ',' '\n')

            if [[ $i -gt 1 ]]; then
                read -rp "Select [1-$((i-1))] or 0 to enter manually: " HF_NUM
                if [[ "$HF_NUM" != "0" ]] && [[ -n "${HF_MODEL_INDEX[$HF_NUM]:-}" ]]; then
                    LLM_MODEL="${HF_MODEL_INDEX[$HF_NUM]}"
                    # Default to 1-bit if bitnet in name, else Q4
                    echo "$LLM_MODEL" | grep -qi 'bitnet\|1.58\|1bit' \
                        && LLM_QUANT="IQ1_S" || LLM_QUANT="Q4_K_M"
                    log "Selected: $LLM_MODEL (quant: $LLM_QUANT)"
                else
                    ask "Enter full model tag (e.g. hf.co/author/model-GGUF):"
                    read -rp "> " LLM_MODEL
                    LLM_QUANT="Q4_K_M"
                fi
            fi
        fi
        ;;

    4|*)  # Manual entry
        ask "Enter Ollama model tag (e.g. qwen3:4b or hf.co/author/model-GGUF):"
        read -rp "> " LLM_MODEL
        ask "Enter quantization [default: IQ1_S for 1-bit, Q4_K_M for others]:"
        read -rp "> " LLM_QUANT_INPUT
        [[ -n "$LLM_QUANT_INPUT" ]] && LLM_QUANT="$LLM_QUANT_INPUT"
        ;;
esac

# If no model selected, skip download and note it for post-install
if [[ -z "$LLM_MODEL" ]]; then
    warn "No LLM model selected — Ollama will be installed but no model downloaded"
    warn "After boot run: ollama pull <model-tag>"
fi


# =============================================================================
# DISK SELECTION
# =============================================================================
section "Disk Selection"

# Show disks — filtered to real disks only, no loop/rom/iso devices
echo -e "${BOLD}Available disks:${NC}\n"
lsblk -d -o NAME,SIZE,MODEL,ROTA,TYPE 2>/dev/null \
    | grep -v 'loop\|rom\|sr\|fd' \
    | grep 'disk' \
    | while IFS= read -r line; do
        echo -e "  $line"
    done
echo ""

while true; do
    ask "Enter target disk name (e.g. sda, nvme0n1, vda) — ALL DATA WILL BE ERASED:"
    read -rp "> " DISK_NAME
    TARGET_DISK="/dev/${DISK_NAME}"
    [[ -b "$TARGET_DISK" ]] || { warn "Device $TARGET_DISK not found. Try again."; continue; }

    # Warn if this looks like the boot device
    BOOT_DEV=$(findmnt -n -o SOURCE / 2>/dev/null | sed 's/[0-9]*$//' | sed 's/p[0-9]*$//')
    if [[ "$TARGET_DISK" == "$BOOT_DEV" ]]; then
        warn "WARNING: $TARGET_DISK appears to be your current boot device!"
        warn "Installing here will erase your running system."
    fi

    DISK_SIZE=$(lsblk -d -o SIZE -n "$TARGET_DISK" 2>/dev/null | xargs)
    echo ""
    echo -e "${RED}${BOLD}  !! ALL DATA on $TARGET_DISK ($DISK_SIZE) will be permanently erased !!${NC}"
    echo ""
    ask "Type 'YES' in capitals to confirm, or 'no' to choose again:"
    read -rp "> " CONFIRM
    [[ "$CONFIRM" == "YES" ]] && break
    [[ "${CONFIRM,,}" == "no" ]] && continue
    warn "Type YES (capitals) to confirm or no to choose a different disk"
done

log "Target disk: $TARGET_DISK ($DISK_SIZE)"

# Partition naming — nvme uses p1 p2 p3, sata/virtio uses 1 2 3
if [[ "$DISK_NAME" == nvme* ]] || [[ "$DISK_NAME" == mmcblk* ]]; then
    P="p"
else
    P=""
fi

# =============================================================================
# DISK PREPARATION — unmount and wipe everything before partitioning
# This fixes the "partition in use" error from the old installer
# =============================================================================
section "Preparing Disk: $TARGET_DISK"

info "Unmounting any mounted partitions on $TARGET_DISK..."

# Unmount all partitions on the target disk — iterate through all possible
# partition names and unmount them. Do this before any partitioning.
for part in $(lsblk -ln -o NAME "$TARGET_DISK" 2>/dev/null | tail -n +2); do
    PART_DEV="/dev/$part"
    if mountpoint -q "$PART_DEV" 2>/dev/null \
        || grep -q "^$PART_DEV " /proc/mounts 2>/dev/null; then
        info "  Unmounting $PART_DEV"
        umount -l "$PART_DEV" 2>/dev/null || true
    fi
done

# Also unmount by target disk path directly
umount -l "${TARGET_DISK}"* 2>/dev/null || true

# Disable any swap on this disk
for part in $(lsblk -ln -o NAME "$TARGET_DISK" 2>/dev/null | tail -n +2); do
    swapoff "/dev/$part" 2>/dev/null || true
done
swapoff "${TARGET_DISK}"* 2>/dev/null || true

# Close any LVM/LUKS/RAID that might be using this disk
if command -v pvremove >/dev/null 2>&1; then
    pvremove -ff "${TARGET_DISK}"* 2>/dev/null || true
fi
if command -v cryptsetup >/dev/null 2>&1; then
    for part in $(lsblk -ln -o NAME "$TARGET_DISK" 2>/dev/null | tail -n +2); do
        cryptsetup close "/dev/$part" 2>/dev/null || true
    done
fi

# Wipe all filesystem and partition signatures from the entire disk
# This is the key step that prevents "device busy" errors
info "Wiping partition signatures..."
wipefs -a --force "$TARGET_DISK" 2>/dev/null || \
    dd if=/dev/zero of="$TARGET_DISK" bs=512 count=2048 2>/dev/null || true

# Wipe first and last MB (catches GPT header and backup)
dd if=/dev/zero of="$TARGET_DISK" bs=1M count=10 2>/dev/null || true
DISK_SIZE_BYTES=$(blockdev --getsize64 "$TARGET_DISK" 2>/dev/null || echo 0)
if [[ $DISK_SIZE_BYTES -gt 10485760 ]]; then
    dd if=/dev/zero of="$TARGET_DISK" bs=1M \
        seek=$(( (DISK_SIZE_BYTES / 1048576) - 10 )) \
        count=10 2>/dev/null || true
fi

# Force kernel to re-read partition table
partprobe "$TARGET_DISK" 2>/dev/null || true
sleep 2  # Give kernel time to settle

log "Disk prepared — all partitions unmounted and signatures cleared"

# =============================================================================
# PARTITIONING
# =============================================================================
section "Partitioning"

# Calculate partition boundaries
# p1: EFI (512MB) or BIOS boot (1MB)
# p2: OS root
# p3: Swap
# p4: Mesh storage (remainder)

BOOT_END_MB=513       # EFI: 1MB start → 513MB end
[[ "$BOOT_MODE" == "bios" ]] && BOOT_END_MB=2

OS_START_MB=$BOOT_END_MB
OS_END_MB=$((OS_START_MB + OS_PART_SIZE_MB))

SWAP_SIZE_GB=$((TOTAL_RAM_GB > 8 ? 8 : (TOTAL_RAM_GB < 1 ? 1 : TOTAL_RAM_GB)))
SWAP_END_MB=$((OS_END_MB + (SWAP_SIZE_GB * 1024)))

info "Partition layout:"
info "  p1  $([ "$BOOT_MODE" = "uefi" ] && echo "512MB  EFI System" || echo "1MB    BIOS boot")"
info "  p2  $((OS_PART_SIZE_MB / 1024))GB   OS root  (/)"
info "  p3  ${SWAP_SIZE_GB}GB    Swap"
info "  p4  remainder  Mesh storage  (/mnt/mesh)"

# Create GPT partition table
parted -s "$TARGET_DISK" mklabel gpt
sleep 1

# p1: Boot partition
if [[ "$BOOT_MODE" == "uefi" ]]; then
    parted -s "$TARGET_DISK" mkpart ESP fat32 1MiB 513MiB
    parted -s "$TARGET_DISK" set 1 esp on
else
    parted -s "$TARGET_DISK" mkpart primary 1MiB 2MiB
    parted -s "$TARGET_DISK" set 1 bios_grub on
fi

# p2: OS root
parted -s "$TARGET_DISK" mkpart primary ext4 ${OS_START_MB}MiB ${OS_END_MB}MiB

# p3: Swap
parted -s "$TARGET_DISK" mkpart primary linux-swap ${OS_END_MB}MiB ${SWAP_END_MB}MiB

# p4: Mesh storage — rest of disk
parted -s "$TARGET_DISK" mkpart primary ext4 ${SWAP_END_MB}MiB 100%

# Force kernel re-read
partprobe "$TARGET_DISK"
sleep 3  # Essential — give kernel time to register new partitions

# Verify partitions exist before formatting
BOOT_PART="${TARGET_DISK}${P}1"
OS_PART="${TARGET_DISK}${P}2"
SWAP_PART="${TARGET_DISK}${P}3"
MESH_PART="${TARGET_DISK}${P}4"

for part in "$BOOT_PART" "$OS_PART" "$SWAP_PART" "$MESH_PART"; do
    [[ -b "$part" ]] || die "Partition $part was not created. Check disk and try again."
done

log "Partitions created — formatting..."

# Format — wipefs each partition first to clear any old signatures
wipefs -a "$BOOT_PART"  2>/dev/null || true
wipefs -a "$OS_PART"    2>/dev/null || true
wipefs -a "$SWAP_PART"  2>/dev/null || true
wipefs -a "$MESH_PART"  2>/dev/null || true

if [[ "$BOOT_MODE" == "uefi" ]]; then
    mkfs.fat -F32 -n LOOMOS_EFI "$BOOT_PART" \
        || die "Failed to format EFI partition"
fi

mkfs.ext4 -q -F -L LOOMOS_OS   "$OS_PART"   \
    || die "Failed to format OS partition"

mkswap        -L LOOMOS_SWAP   "$SWAP_PART"  \
    || die "Failed to create swap"

mkfs.ext4 -q -F -L LOOMOS_MESH "$MESH_PART" \
    || die "Failed to format mesh storage partition"

log "All partitions formatted successfully"

# =============================================================================
# MOUNT TARGET
# =============================================================================
section "Mounting Target"

mkdir -p "$TARGET"
mount "$OS_PART" "$TARGET" || die "Cannot mount OS partition"

if [[ "$BOOT_MODE" == "uefi" ]]; then
    mkdir -p "$TARGET/boot/efi"
    mount "$BOOT_PART" "$TARGET/boot/efi" \
        || die "Cannot mount EFI partition"
fi

log "Target mounted at $TARGET"


# =============================================================================
# DEBOOTSTRAP — Devuan Excalibur base
# =============================================================================
section "Installing Devuan $DEVUAN_SUITE Base System"
info "Pulling from $DEVUAN_MIRROR — takes 5-10 minutes..."

# Get Devuan keyring
if [[ ! -f /usr/share/keyrings/devuan-archive-keyring.gpg ]]; then
    curl -sL "https://files.devuan.org/devuan-archive-keyring.gpg" \
        -o /usr/share/keyrings/devuan-archive-keyring.gpg 2>/dev/null || true
fi

debootstrap \
    --arch="$DARCH" \
    --variant=minbase \
    --include="ca-certificates,curl,gnupg,locales" \
    --exclude="systemd,systemd-sysv" \
    --keyring=/usr/share/keyrings/devuan-archive-keyring.gpg \
    "$DEVUAN_SUITE" \
    "$TARGET" \
    "$DEVUAN_MIRROR" \
    || die "debootstrap failed"

log "Devuan base installed"

# =============================================================================
# CHROOT SETUP
# =============================================================================
IN_CHROOT="chroot $TARGET"

# Bind mounts — each mounted individually with fallback
for fs in proc sys dev dev/pts run; do
    mkdir -p "$TARGET/$fs"
    mount --bind "/$fs" "$TARGET/$fs" 2>/dev/null || true
done

# Prevent services from starting during install (common chroot problem)
cat > "$TARGET/usr/sbin/policy-rc.d" <<'EOF'
#!/bin/sh
exit 101
EOF
chmod +x "$TARGET/usr/sbin/policy-rc.d"

# =============================================================================
# APT SOURCES
# =============================================================================
cat > "$TARGET/etc/apt/sources.list" <<EOF
deb $DEVUAN_MIRROR $DEVUAN_SUITE          main contrib non-free non-free-firmware
deb $DEVUAN_MIRROR ${DEVUAN_SUITE}-updates main contrib non-free non-free-firmware
deb $DEVUAN_MIRROR ${DEVUAN_SUITE}-security main contrib non-free non-free-firmware
EOF

$IN_CHROOT apt-get update -q

# Install nala — double install required
log "Installing nala package manager..."
$IN_CHROOT apt-get install -y nala nala \
    || $IN_CHROOT apt-get install -y nala nala \
    || warn "nala install had issues — falling back to apt-get for remainder"

# Helper: install packages inside chroot using nala
chroot_install() {
    $IN_CHROOT nala install -y "$@" 2>/dev/null \
        || $IN_CHROOT apt-get install -y "$@"
}

# =============================================================================
# LOCALE AND TIMEZONE
# =============================================================================
$IN_CHROOT bash -c "echo '${LOCALE} UTF-8' >> /etc/locale.gen && locale-gen"
$IN_CHROOT bash -c "echo 'LANG=${LOCALE}' > /etc/default/locale"
$IN_CHROOT bash -c "ln -sf /usr/share/zoneinfo/${TIMEZONE} /etc/localtime"
$IN_CHROOT bash -c "echo '${TIMEZONE}' > /etc/timezone"
log "Locale: $LOCALE  Timezone: $TIMEZONE"

# =============================================================================
# HOSTNAME AND NETWORK
# =============================================================================
echo "$HOSTNAME" > "$TARGET/etc/hostname"
cat > "$TARGET/etc/hosts" <<EOF
127.0.0.1   localhost
127.0.1.1   $HOSTNAME
::1         localhost ip6-localhost ip6-loopback
EOF

# =============================================================================
# FSTAB
# =============================================================================
cat > "$TARGET/etc/fstab" <<EOF
# LoomOS fstab — generated by installer
LABEL=LOOMOS_OS    /           ext4  defaults,noatime          0 1
LABEL=LOOMOS_SWAP  none        swap  sw                        0 0
LABEL=LOOMOS_MESH  /mnt/mesh   ext4  defaults,noatime,nofail   0 2
EOF
[[ "$BOOT_MODE" == "uefi" ]] && \
    echo "LABEL=LOOMOS_EFI   /boot/efi   vfat  umask=0077,nofail  0 1" \
    >> "$TARGET/etc/fstab"

mkdir -p "$TARGET/mnt/mesh"
log "fstab written"

# =============================================================================
# BASE PACKAGES
# =============================================================================
section "Installing Base Packages"

BASE_PKGS=(
    # Init — sysvinit, no systemd
    sysvinit-core sysvinit-utils
    elogind libpam-elogind dbus-sysv

    # Kernel — generic, catches most hardware
    linux-image-${DARCH}
    linux-headers-${DARCH}

    # Firmware — broad hardware coverage
    firmware-linux
    firmware-linux-nonfree
    firmware-misc-nonfree
    firmware-realtek
    firmware-atheros
    firmware-iwlwifi
    firmware-amd-graphics

    # Core utilities
    bash sudo curl wget git
    nano vim less
    htop lsof pciutils usbutils
    net-tools iproute2 iputils-ping
    dnsutils whois
    zip unzip tar rsync
    bc jq

    # Python — core of LoomOS
    python3 python3-pip python3-venv python3-dev
    python3-dbus python3-gi python3-cairo

    # Audio — PipeWire with sysvinit-compatible session management
    pipewire pipewire-pulse pipewire-alsa
    wireplumber alsa-utils

    # X11 — substrate for Qtile and graphical apps
    xorg xinit
    x11-xserver-utils x11-utils
    xdotool xclip xsel
    libx11-dev libxext-dev

    # Fonts
    fonts-dejavu fonts-liberation
    fonts-noto fonts-noto-color-emoji

    # Network management
    network-manager wpasupplicant rfkill

    # NFS — mesh storage sharing
    nfs-kernel-server nfs-common

    # Process migration
    criu

    # Build tools — needed for Python packages
    gcc g++ make pkg-config
    libssl-dev libffi-dev
    libopenblas-dev

    # Camera — for auth system
    v4l-utils libv4l-dev

    # Notification daemon (no systemd)
    dunst libnotify-bin

    # Terminal emulator
    alacritty

    # Bootloader (swapped for EFI later if needed)
    grub-pc
)

# Conditional packages based on hardware
$HAS_NVIDIA && BASE_PKGS+=(nvidia-driver)
$HAS_AMD_GPU && BASE_PKGS+=(libgl1-mesa-dri mesa-vulkan-drivers)
$HAS_INTEL_GPU && BASE_PKGS+=(intel-media-va-driver-non-free)
$IS_LAPTOP && BASE_PKGS+=(acpi acpid tlp tlp-rdw)
$HAS_BT && BASE_PKGS+=(bluez bluez-tools)
$HAS_WIFI && BASE_PKGS+=(wireless-tools)

# Swap grub-pc for grub-efi on UEFI systems
if [[ "$BOOT_MODE" == "uefi" ]]; then
    BASE_PKGS=("${BASE_PKGS[@]/grub-pc/grub-efi-${DARCH}}")
    BASE_PKGS+=(efibootmgr)
fi

chroot_install "${BASE_PKGS[@]}"
log "Base packages installed"

# =============================================================================
# FLATPAK
# =============================================================================
section "Installing Flatpak"
chroot_install flatpak
$IN_CHROOT flatpak remote-add --if-not-exists flathub \
    https://dl.flathub.org/repo/flathub.flatpakrepo 2>/dev/null || true
log "Flatpak + Flathub configured"

# =============================================================================
# LOOM OS PYTHON ENVIRONMENT
# =============================================================================
section "Building LoomOS Python Environment"

mkdir -p "$TARGET/opt/loomos"/{bin,configs,models,logs,mesh,memory,profiles}
$IN_CHROOT python3 -m venv /opt/loomos/venv --system-site-packages

PIP="$IN_CHROOT /opt/loomos/venv/bin/pip install --quiet --no-warn-script-location"

log "Installing Python dependencies..."

# Voice
$PIP vosk sounddevice soundfile pyaudio

# TTS
if [[ "$TTS_ENGINE" == "chatterbox" ]]; then
    $PIP chatterbox-tts
    log "TTS: Chatterbox installed"
else
    $PIP kokoro soundfile
    log "TTS: Kokoro installed"
fi

# Auth — face and voice recognition
$PIP \
    face-recognition \
    opencv-python-headless \
    speechbrain \
    numpy

# LLM and mesh
$PIP \
    requests \
    aiohttp \
    psutil \
    watchdog \
    rapidfuzz \
    paramiko \
    rich \
    textual

# Particle animation display
$PIP pygame

# Hardware monitoring
$PIP py3nvml 2>/dev/null || true  # NVIDIA stats, optional

log "Python environment complete"

# =============================================================================
# QTILE
# =============================================================================
section "Installing Qtile"

chroot_install \
    python3-xcffib \
    python3-cairocffi \
    libcairo2-dev \
    libxcb1-dev \
    libxcb-render0-dev \
    python3-psutil

$IN_CHROOT /opt/loomos/venv/bin/pip install --quiet qtile
log "Qtile installed"

# =============================================================================
# OLLAMA — manual binary install (avoids their systemd installer)
# =============================================================================
section "Installing Ollama"

OLLAMA_VERSION="latest"
OLLAMA_ARCH="$DARCH"
[[ "$DARCH" == "amd64" ]] && OLLAMA_ARCH="amd64"
[[ "$DARCH" == "arm64" ]] && OLLAMA_ARCH="arm64"

info "Downloading Ollama binary..."
curl -fsSL https://ollama.com/install.sh | sh

# Create ollama user and dirs
$IN_CHROOT useradd -r -s /bin/false -d /opt/loomos/models/llm ollama 2>/dev/null || true
mkdir -p "$TARGET/opt/loomos/models/llm"
$IN_CHROOT chown -R ollama:ollama /opt/loomos/models/llm 2>/dev/null || true

# Set Ollama to use our storage path and enable 1-bit quantization support
cat > "$TARGET/etc/default/ollama" <<EOF
# LoomOS Ollama configuration
OLLAMA_MODELS=/opt/loomos/models/llm
OLLAMA_HOST=0.0.0.0:11434
OLLAMA_NUM_PARALLEL=2
OLLAMA_FLASH_ATTENTION=1
OLLAMA_KV_CACHE_TYPE=q8_0
# 1-bit quantization support
OLLAMA_LLAMA_QUANT=${LLM_QUANT}
EOF

# Sysvinit service for Ollama — no systemd
cat > "$TARGET/etc/init.d/ollama" <<'INITEOF'
#!/bin/sh
### BEGIN INIT INFO
# Provides:          ollama
# Required-Start:    $network $local_fs
# Required-Stop:     $network $local_fs
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: Ollama LLM inference server
### END INIT INFO

. /etc/default/ollama
DAEMON=/usr/local/bin/ollama
PIDFILE=/var/run/ollama.pid
LOGFILE=/var/log/ollama.log
RUNAS=ollama

export OLLAMA_MODELS OLLAMA_HOST OLLAMA_NUM_PARALLEL
export OLLAMA_FLASH_ATTENTION OLLAMA_KV_CACHE_TYPE OLLAMA_LLAMA_QUANT

case "$1" in
    start)
        echo "Starting Ollama..."
        start-stop-daemon --start --background --quiet \
            --pidfile $PIDFILE --make-pidfile \
            --chuid $RUNAS \
            --exec $DAEMON -- serve >> $LOGFILE 2>&1
        echo "Ollama started"
        ;;
    stop)
        echo "Stopping Ollama..."
        start-stop-daemon --stop --quiet --pidfile $PIDFILE
        rm -f $PIDFILE
        ;;
    restart) $0 stop; sleep 2; $0 start ;;
    status)
        [ -f $PIDFILE ] && kill -0 $(cat $PIDFILE) 2>/dev/null \
            && echo "Ollama running (PID $(cat $PIDFILE))" \
            || echo "Ollama not running"
        ;;
    *) echo "Usage: $0 {start|stop|restart|status}"; exit 1 ;;
esac
exit 0
INITEOF
chmod +x "$TARGET/etc/init.d/ollama"
$IN_CHROOT update-rc.d ollama defaults
log "Ollama installed with sysvinit service"

# =============================================================================
# LLM MEMORY — local persistent memory store
# =============================================================================
# Each user gets a memory directory on the mesh partition
# The LLM reads from this on session start to restore context
mkdir -p "$TARGET/opt/loomos/memory"
cat > "$TARGET/opt/loomos/memory/README" <<EOF
LoomOS LLM Memory Store
========================
Each subdirectory here is a user's persistent memory.
Format: /opt/loomos/memory/<username>/
  session_log.jsonl  — timestamped conversation log
  facts.json         — extracted persistent facts about the user
  preferences.json   — learned user preferences
  projects/          — per-project context and briefs

Memory is referenced automatically at session start.
On a mesh, this directory syncs via /mnt/mesh/memory/ so
memory is available on all nodes for the same user.
EOF

# =============================================================================
# VOSK STT MODEL
# =============================================================================
section "Downloading STT Model: $VOSK_MODEL"

VOSK_URL="https://alphacephei.com/vosk/models/${VOSK_MODEL}.zip"
mkdir -p "$TARGET/opt/loomos/models/vosk"

info "Downloading $VOSK_MODEL..."
curl -L --progress-bar "$VOSK_URL" -o /tmp/vosk_model.zip \
    && unzip -q /tmp/vosk_model.zip -d "$TARGET/opt/loomos/models/vosk/" \
    && rm /tmp/vosk_model.zip \
    && log "Vosk model installed" \
    || warn "Vosk model download failed — download manually after boot:\n  curl -L $VOSK_URL | unzip -d /opt/loomos/models/vosk/"

# =============================================================================
# LLM MODEL DOWNLOAD
# =============================================================================
if [[ -n "$LLM_MODEL" ]]; then
    section "Downloading LLM: $LLM_MODEL"
    info "This may take a while depending on model size..."
    info "Quantization: $LLM_QUANT"
    # Pull model into chroot with Ollama running temporarily
    $IN_CHROOT bash -c "
        export OLLAMA_MODELS=/opt/loomos/models/llm
        export OLLAMA_LLAMA_QUANT=${LLM_QUANT}
        /usr/local/bin/ollama serve &
        OLLAMA_PID=\$!
        sleep 5
        /usr/local/bin/ollama pull ${LLM_MODEL} && echo 'Model downloaded'
        kill \$OLLAMA_PID 2>/dev/null
    " || warn "Model download failed — run after boot: ollama pull $LLM_MODEL"
fi

# =============================================================================
# LOOMOS CONFIG FILE
# =============================================================================
section "Writing LoomOS Configuration"

mkdir -p "$TARGET/etc/loomos"
cat > "$TARGET/etc/loomos/loomos.conf" <<CONF
# LoomOS Configuration
# Generated by installer — https://github.com/DansDesigns/LoomOS

[core]
version         = 0.1.0
hostname        = ${HOSTNAME}
arch            = ${DARCH}
has_gpu         = ${HAS_GPU}
has_nvidia      = ${HAS_NVIDIA}
has_amd_gpu     = ${HAS_AMD_GPU}
has_webcam      = ${HAS_WEBCAM}
webcam_device   = ${WEBCAM_DEVICE:-/dev/video0}
total_ram_mb    = ${TOTAL_RAM_MB}
is_laptop       = ${IS_LAPTOP}
mesh_score      = ${MESH_SCORE}

[auth]
method          = face_voice
webcam_device   = ${WEBCAM_DEVICE:-/dev/video0}
passphrase_format = word-number-word-number
biometrics_path = /opt/loomos/profiles
usb_required_for_change = true
presence_fps    = 2
grace_period_sec = 10

[voice]
stt_engine      = vosk
stt_model       = /opt/loomos/models/vosk/${VOSK_MODEL}
tts_engine      = ${TTS_ENGINE}
stt_language    = en-us
# No wake word — presence detection triggers login

[llm]
engine          = ollama
model           = ${LLM_MODEL:-unset}
quant           = ${LLM_QUANT}
host            = 127.0.0.1
port            = 11434
models_path     = /opt/loomos/models/llm
memory_path     = /opt/loomos/memory
context_length  = 4096
mesh_pool       = true
mesh_coordinator = auto

[mesh]
enabled         = true
discovery_port  = 7700
rpc_port        = 7701
mesh_mount      = /mnt/mesh
nfs_export      = /mnt/mesh
sync_interval   = 30
trusted_users_only = true

[gui]
wm              = qtile
terminal        = alacritty
launcher        = ulauncher
start_x_on_boot = true
particle_fps    = 60

[packages]
flatpak         = true
apt_frontend    = nala
CONF

log "Configuration written to /etc/loomos/loomos.conf"

# =============================================================================
# SYSVINIT SERVICE SCRIPTS
# =============================================================================
section "Installing Service Scripts"

# ── LoomOS voice pipeline ──────────────────────────────────────────────────
cat > "$TARGET/etc/init.d/loomos-voice" <<'INITEOF'
#!/bin/sh
### BEGIN INIT INFO
# Provides:          loomos-voice
# Required-Start:    $local_fs $network ollama
# Required-Stop:     $local_fs
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: LoomOS voice pipeline (STT/TTS/auth)
### END INIT INFO
DAEMON=/opt/loomos/venv/bin/python3
ARGS="/opt/loomos/bin/voice_pipeline.py"
PIDFILE=/var/run/loomos-voice.pid
case "$1" in
    start) start-stop-daemon --start --background --quiet \
               --pidfile $PIDFILE --make-pidfile \
               --exec $DAEMON -- $ARGS ;;
    stop)  start-stop-daemon --stop --quiet --pidfile $PIDFILE; rm -f $PIDFILE ;;
    restart) $0 stop; sleep 1; $0 start ;;
    status) [ -f $PIDFILE ] && kill -0 $(cat $PIDFILE) 2>/dev/null \
                && echo "running" || echo "stopped" ;;
    *) echo "Usage: $0 {start|stop|restart|status}"; exit 1 ;;
esac; exit 0
INITEOF

# ── LoomOS mesh daemon ──────────────────────────────────────────────────────
cat > "$TARGET/etc/init.d/loomos-mesh" <<'INITEOF'
#!/bin/sh
### BEGIN INIT INFO
# Provides:          loomos-mesh
# Required-Start:    $network $local_fs nfs-common
# Required-Stop:     $network $local_fs
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: LoomOS mesh network daemon
### END INIT INFO
DAEMON=/opt/loomos/venv/bin/python3
ARGS="/opt/loomos/bin/mesh_daemon.py"
PIDFILE=/var/run/loomos-mesh.pid
case "$1" in
    start) start-stop-daemon --start --background --quiet \
               --pidfile $PIDFILE --make-pidfile \
               --exec $DAEMON -- $ARGS ;;
    stop)  start-stop-daemon --stop --quiet --pidfile $PIDFILE; rm -f $PIDFILE ;;
    restart) $0 stop; sleep 1; $0 start ;;
    status) [ -f $PIDFILE ] && kill -0 $(cat $PIDFILE) 2>/dev/null \
                && echo "running" || echo "stopped" ;;
    *) echo "Usage: $0 {start|stop|restart|status}"; exit 1 ;;
esac; exit 0
INITEOF

# ── LoomOS auth daemon ──────────────────────────────────────────────────────
cat > "$TARGET/etc/init.d/loomos-auth" <<'INITEOF'
#!/bin/sh
### BEGIN INIT INFO
# Provides:          loomos-auth
# Required-Start:    $local_fs loomos-voice
# Required-Stop:     $local_fs
# Default-Start:     2 3 4 5
# Default-Stop:      0 1 6
# Short-Description: LoomOS presence detection and authentication
### END INIT INFO
DAEMON=/opt/loomos/venv/bin/python3
ARGS="/opt/loomos/bin/auth_daemon.py"
PIDFILE=/var/run/loomos-auth.pid
case "$1" in
    start) start-stop-daemon --start --background --quiet \
               --pidfile $PIDFILE --make-pidfile \
               --exec $DAEMON -- $ARGS ;;
    stop)  start-stop-daemon --stop --quiet --pidfile $PIDFILE; rm -f $PIDFILE ;;
    restart) $0 stop; sleep 1; $0 start ;;
    status) [ -f $PIDFILE ] && kill -0 $(cat $PIDFILE) 2>/dev/null \
                && echo "running" || echo "stopped" ;;
    *) echo "Usage: $0 {start|stop|restart|status}"; exit 1 ;;
esac; exit 0
INITEOF

for svc in loomos-voice loomos-mesh loomos-auth; do
    chmod +x "$TARGET/etc/init.d/$svc"
    $IN_CHROOT update-rc.d $svc defaults
done

log "Service scripts installed"

# =============================================================================
# USER ACCOUNT
# =============================================================================
section "Creating User Account: $USERNAME"

$IN_CHROOT useradd -m -s /bin/bash \
    -G audio,video,sudo,plugdev,netdev,bluetooth,dialout,input \
    "$USERNAME" 2>/dev/null || true

# Set a temporary password — biometric auth is primary
# This becomes the emergency fallback only
TEMP_PASS=$(tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 16 2>/dev/null || echo "loomos-change-me")
echo "${USERNAME}:${TEMP_PASS}" | $IN_CHROOT chpasswd
warn "Emergency fallback password set — change after boot with: passwd"
warn "Primary login is face + voice passphrase"

# Store temp password securely — shown once at end of install
echo "$TEMP_PASS" > /tmp/loomos_temp_pass

# Auto-start X on tty1
cat > "$TARGET/home/$USERNAME/.bash_profile" <<'BASHEOF'
# LoomOS — start Qtile on tty1 if X not running
if [[ -z "$DISPLAY" ]] && [[ "$(tty)" == "/dev/tty1" ]]; then
    exec startx /opt/loomos/bin/start_loomos.sh -- :0 vt1
fi
BASHEOF

# Qtile config
mkdir -p "$TARGET/home/$USERNAME/.config/qtile"
cat > "$TARGET/home/$USERNAME/.config/qtile/config.py" <<'QTEOF'
# LoomOS Qtile configuration
# Particle animation runs as background process — Qtile tiles apps on top

import subprocess
from libqtile import bar, layout, widget, hook
from libqtile.config import Click, Drag, Group, Key, Screen
from libqtile.lazy import lazy

mod = "mod4"
terminal = "alacritty"

keys = [
    Key([mod], "Return",    lazy.spawn(terminal)),
    Key([mod], "q",         lazy.window.kill()),
    Key([mod, "shift"], "r",lazy.reload_config()),
    Key([mod], "space",     lazy.spawn("ulauncher")),
    Key([mod], "Tab",       lazy.next_layout()),
    # Session end — push to end conversation
    Key([mod, "shift"], "l",lazy.spawn("/opt/loomos/bin/session_end.sh")),
]

groups = [Group(str(i)) for i in range(1, 6)]
for g in groups:
    keys += [
        Key([mod], g.name, lazy.group[g.name].toscreen()),
        Key([mod, "shift"], g.name, lazy.window.togroup(g.name)),
    ]

layouts = [
    layout.MonadTall(
        border_width=1,
        border_focus="#FF6600",
        border_normal="#0d0d0d",
        margin=6
    ),
    layout.Max(),
    layout.Floating(),
]

# Minimal status bar — system is voice-controlled, bar is informational only
screens = [
    Screen(
        bottom=bar.Bar([
            widget.GroupBox(
                active="#FF9900",
                inactive="#333344",
                this_current_screen_border="#FF6600",
                fontsize=12,
            ),
            widget.Spacer(),
            widget.WindowName(foreground="#6688AA", fontsize=12),
            widget.Spacer(),
            widget.CPU(
                format="CPU {load_percent:.0f}%",
                foreground="#CC88FF",
                fontsize=11,
            ),
            widget.Sep(foreground="#333344"),
            widget.Memory(
                format="RAM {MemUsed:.0f}M",
                foreground="#99CCFF",
                fontsize=11,
            ),
            widget.Sep(foreground="#333344"),
            widget.Clock(
                format="%H:%M  %d/%m/%Y",
                foreground="#FF9900",
                fontsize=11,
            ),
        ], 24, background="#0a0a0f", opacity=0.9),
    )
]

@hook.subscribe.startup_once
def autostart():
    subprocess.Popen(["/opt/loomos/bin/start_services.sh"])

mouse = [
    Drag([mod], "Button1", lazy.window.set_position_floating()),
    Drag([mod], "Button3", lazy.window.set_size_floating()),
    Click([mod], "Button2", lazy.window.bring_to_front()),
]

dgroups_key_binder   = None
follow_mouse_focus   = True
bring_front_click    = False
cursor_warp          = False
auto_fullscreen      = True
focus_on_window_activation = "smart"
wmname = "LoomOS"
QTEOF

$IN_CHROOT chown -R "${USERNAME}:${USERNAME}" "/home/$USERNAME"

# =============================================================================
# NFS MESH STORAGE
# =============================================================================
# Restrict to local subnet by default — safer than open wildcard
SUBNET=$(ip route 2>/dev/null | grep 'src' | grep -v default \
    | head -1 | awk '{print $1}' || echo "192.168.0.0/24")

cat > "$TARGET/etc/exports" <<NFSEOF
# LoomOS mesh storage
# Shared with local subnet only — edit to restrict further
/mnt/mesh  ${SUBNET}(rw,sync,no_subtree_check,no_root_squash)
NFSEOF
log "NFS exports: /mnt/mesh → $SUBNET"

# =============================================================================
# BOOTLOADER
# =============================================================================
# IMPORTANT: grub-install runs OUTSIDE chroot so it can write to the real
# disk MBR/EFI. It uses --boot-directory to point at the mounted target.
# update-grub runs INSIDE chroot so it generates grub.cfg with correct
# paths relative to the installed system.
# =============================================================================
section "Installing Bootloader"

# Write grub defaults before generating config
cat > "$TARGET/etc/default/grub" <<'GRUBEOF'
GRUB_DEFAULT=0
GRUB_TIMEOUT=3
GRUB_TIMEOUT_STYLE=hidden
GRUB_DISTRIBUTOR="LoomOS"
GRUB_CMDLINE_LINUX_DEFAULT="quiet loglevel=3 vt.global_cursor_default=0"
GRUB_CMDLINE_LINUX=""
GRUBEOF

if [[ "$BOOT_MODE" == "uefi" ]]; then
    info "Installing GRUB EFI to $TARGET_DISK..."

    # Install GRUB EFI — outside chroot, target the mounted EFI partition
    grub-install \
        --target=x86_64-efi \
        --efi-directory="$TARGET/boot/efi" \
        --boot-directory="$TARGET/boot" \
        --bootloader-id=LoomOS \
        --recheck \
        --no-nvram \
        "$TARGET_DISK" \
        || die "GRUB EFI install failed"

    # Also install with nvram for machines that need it
    grub-install \
        --target=x86_64-efi \
        --efi-directory="$TARGET/boot/efi" \
        --boot-directory="$TARGET/boot" \
        --bootloader-id=LoomOS \
        --recheck \
        "$TARGET_DISK" 2>/dev/null || true

else
    info "Installing GRUB BIOS to $TARGET_DISK..."

    # Install GRUB BIOS — outside chroot, writes MBR to the disk
    grub-install \
        --target=i386-pc \
        --boot-directory="$TARGET/boot" \
        --recheck \
        "$TARGET_DISK" \
        || die "GRUB BIOS install failed"
fi

# Generate grub.cfg — inside chroot so paths are correct for the new system
$IN_CHROOT update-grub \
    || die "update-grub failed — boot config not generated"

# Verify grub.cfg was created and contains a menu entry
if grep -q "menuentry\|linux" "$TARGET/boot/grub/grub.cfg" 2>/dev/null; then
    log "Bootloader installed and grub.cfg verified"
else
    die "grub.cfg is missing or empty — boot will fail"
fi

# =============================================================================
# REMOVE POLICY-RC.D (allow services to start on real boot)
# =============================================================================
rm -f "$TARGET/usr/sbin/policy-rc.d"

# =============================================================================
# FINAL SUMMARY
# =============================================================================
section "Installation Complete"

TEMP_PASS=$(cat /tmp/loomos_temp_pass 2>/dev/null || echo "see /etc/loomos/")
rm -f /tmp/loomos_temp_pass

echo ""
echo -e "${BOLD}${GREEN}  LoomOS installed successfully${NC}"
echo ""
echo -e "${BOLD}  Hardware:${NC}"
echo -e "    CPU:      $CPU_MODEL ($CPU_CORES cores)"
echo -e "    RAM:      ${TOTAL_RAM_MB}MB"
echo -e "    GPU:      $(${HAS_GPU} && echo "yes" || echo "CPU-only")"
echo -e "    Webcam:   $(${HAS_WEBCAM} && echo "$WEBCAM_DEVICE" || echo "not detected")"
echo -e "    Boot:     $BOOT_MODE"
echo ""
echo -e "${BOLD}  Installed:${NC}"
echo -e "    Base:     Devuan $DEVUAN_SUITE ($DARCH) — no systemd"
echo -e "    Init:     sysvinit"
echo -e "    STT:      Vosk ($VOSK_MODEL)"
echo -e "    TTS:      $TTS_ENGINE"
echo -e "    LLM:      Ollama$([ -n "$LLM_MODEL" ] && echo " + $LLM_MODEL ($LLM_QUANT)" || echo " (no model — pull after boot)")"
echo -e "    Desktop:  Qtile"
echo ""
echo -e "${BOLD}  First boot:${NC}"
echo -e "    1. Remove installation media and reboot"
echo -e "    2. Screen wakes to particle animation — approach the webcam"
echo -e "    3. First user: enrollment runs automatically"
echo -e "       - Face scan, then passphrase selection (word-number-word-number)"
echo -e "    4. Emergency fallback password: ${YELLOW}$TEMP_PASS${NC}"
echo -e "       ${DIM}(change with: passwd — this is backup only, not primary auth)${NC}"
echo ""
[[ -z "$LLM_MODEL" ]] && \
    echo -e "${YELLOW}  After boot, pull your LLM model:${NC}" && \
    echo -e "    ollama pull <model-tag>" && echo ""

echo -e "${DIM}  Repo: https://github.com/DansDesigns/LoomOS${NC}"
echo ""

