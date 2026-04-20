#!/usr/bin/env python3
"""
LoomOS Speech Centre — Main GUI v0.15.4

Changes from v0.14:
  NEW: MEDIA mode — a universal media player built directly into the central
       circle.  Behaves as a first-class InputMode alongside COMMAND and
       CONVERSATION.
         • Audio (mp3/ogg/wav/flac/m4a/opus) via pygame.mixer
         • Video (mp4/avi/mkv/webm) rendered inside the circle via cv2
         • Album art / box-art shown inside the circle when available
         • Animated equaliser bars inside (audio) or overlaid (video)
         • Full transport controls arc-rendered below the circle
         • Speech detection lowers media volume and overlays the wave circle
         • Voice commands: play/pause/stop/next/previous/shuffle/repeat/
           volume up|down|set, open media folder, open video folder
         • CTRL+P or pill-tap cycles to/from MEDIA mode
         • IPC: {"type":"set_mode","value":"media"}

  PATCH: Artist/song voice search in MEDIA mode
         • "play <artist>" — if no exact song match, asks "Which song?"
         • "play <song>"   — plays directly and confirms via TTS
         • "play anything by <artist>" — shuffles all artist tracks, auto-starts

  PATCH2: Broader home-directory scan + lower artist match threshold
         • MEDIA_SEARCH_DIRS now includes ~ so all subdirs are found
         • _scan_media depth-limited walk with dedup, cap raised to 2000
         • Artist threshold lowered to 0.45 to handle Vosk transcription drift
"""

import pygame
import pygame.gfxdraw
import os, sys, math, time, random, argparse, json, platform
import threading, socket, hashlib, hmac, subprocess
import urllib.request, urllib.error
import numpy as np

try:
    import tkinter as tk
    from tkinter import filedialog
    TK_OK = True
except ImportError:
    TK_OK = False
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from pathlib import Path

# ── Optional imports ──────────────────────────────────────────────────────────
try:
    import psutil
    PSUTIL_OK = True
except ImportError:
    PSUTIL_OK = False
    print("WARNING: psutil not installed — pip install psutil")

try:
    from vosk import Model, KaldiRecognizer
    import sounddevice as sd
    VOSK_OK = True
except ImportError:
    VOSK_OK = False
    print("WARNING: vosk/sounddevice not installed — STT disabled.")

try:
    import pyttsx3
    PYTTSX3_OK = True
except ImportError:
    PYTTSX3_OK = False
    print("WARNING: pyttsx3 not installed — TTS disabled.  pip install pyttsx3")

try:
    import cv2
    CV2_OK = True
except ImportError:
    CV2_OK = False


# ── OS Detection + IPC Configuration ─────────────────────────────────────────

def _detect_ipc_mode():
    _IS_WINDOWS = platform.system() == "Windows"
    _unix_available = hasattr(socket, "AF_UNIX") and not _IS_WINDOWS
    if _unix_available:
        return {"mode":"unix","socket_path":"/tmp/loomos_gui.sock","host":None,"port":None}
    else:
        return {"mode":"tcp","socket_path":None,"host":"127.0.0.1","port":47842}

IPC_CFG = _detect_ipc_mode()
print(f"[IPC] mode={IPC_CFG['mode']}"
      + (f" path={IPC_CFG['socket_path']}" if IPC_CFG['mode'] == 'unix'
         else f" {IPC_CFG['host']}:{IPC_CFG['port']}"))

# ============================
version_no = "0.15.4"
# ============================

# ── App system constants ──────────────────────────────────────────────────────

APPS_DIR      = Path.home() / ".loomos_apps"
APP_SDK_PATH  = Path(__file__).parent / "loomos_app_sdk.py"
APP_PORT_BASE = 47843
APP_PORT_MAX  = 47899

APP_OPEN_CMDS   = ["open", "launch", "start", "run", "show"]
APP_CLOSE_CMDS  = ["close", "quit", "exit", "kill", "stop"]
APP_DRAWER_CMDS = ["show apps", "open apps", "app drawer", "list apps",
                   "show app launcher"]
APP_RESCAN_CMDS = ["scan apps", "refresh apps", "reload apps"]


# ── Persistent settings ───────────────────────────────────────────────────────

SETTINGS_FILE = os.path.expanduser("~/.loomos_settings.json")

_SETTINGS_DEFAULTS = {
    "panel_alpha":   0.82,
    "bar_alpha":     0.12,
    "mic_threshold": 0.10,
    "ui_font":       "",
    "tts_rate":      175,
    "tts_volume":    1.0,
}

def load_settings() -> dict:
    s = dict(_SETTINGS_DEFAULTS)
    if os.path.exists(SETTINGS_FILE):
        try:
            stored = json.load(open(SETTINGS_FILE))
            for k in _SETTINGS_DEFAULTS:
                if k in stored:
                    s[k] = str(stored[k]) if k == "ui_font" else float(stored[k])
            if "ui_alpha" in stored and "panel_alpha" not in stored:
                old = float(stored["ui_alpha"])
                s["panel_alpha"] = s["bar_alpha"] = old
        except Exception as e:
            print(f"[Settings] Load error: {e}")
    return s

def save_settings(s: dict):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(s, f, indent=2)
        os.chmod(SETTINGS_FILE, 0o600)
    except Exception as e:
        print(f"[Settings] Save error: {e}")


# ── Vocabulary ────────────────────────────────────────────────────────────────

NATO_WORDS = [
    "ALPHA","BETA","CHARLIE","DELTA","ECHO","FOXTROT",
    "GOLF","HOTEL","INDIGO","JULIET","KILO","LIMA",
    "MIKE","NOVEMBER","OSCAR","PAPA","QUEBEC","ROMEO",
    "SIERRA","TANGO","UNIFORM","VICTOR","WHISKEY","XRAY",
    "YANKIE","ZULU",
]
NATO_SET = set(NATO_WORDS)

SPOKEN_NUMBERS = {
    "zero":0,"one":1,"two":2,"three":3,"four":4,
    "five":5,"six":6,"seven":7,"eight":8,"nine":9,
}
PANEL_COMMANDS: dict = {
    "open settings":("right",True),"show settings":("right",True),
    "close settings":("right",False),"hide settings":("right",False),
    "toggle settings":("right",None),"open model":("left",True),
    "show model":("left",True),"close model":("left",False),
    "hide model":("left",False),"toggle model":("left",None),
    "open system":("right",True),"show system":("right",True),
    "close system":("right",False),"hide system":("right",False),
    "toggle system":("right",None),"open info":("left",True),
    "show info":("left",True),"close info":("left",False),
    "hide info":("left",False),"toggle info":("left",None),
}
WALLPAPER_COMMANDS = [
    "set wallpaper","change wallpaper","change background",
    "set background","browse wallpapers","show wallpapers"]

MUTE_COMMANDS   = ["stop listening","mute microphone","mute mic","disable listening"]
UNMUTE_COMMANDS = ["start listening","unmute microphone","unmute mic","enable listening"]
LOGOUT_COMMANDS = ["logout","log out","sign out","log me out","sign me out","pause session","lock screen","lockscreen"]
CONV_MODE_COMMANDS   = ["conversation mode","conversational mode","chat mode","switch to chat"]
CMD_MODE_COMMANDS    = ["command mode","control mode","switch to command","switch to control"]
TOGGLE_MODE_COMMANDS = ["toggle mode","switch mode","change mode"]
MEDIA_MODE_COMMANDS  = ["media mode","media player","music mode","player mode",
                         "open player","open media","launch player","switch to media"]

MEDIA_PLAY_CMDS    = ["play","play music","play media","resume","resume media","resume music","resume playback"]
MEDIA_PAUSE_CMDS   = ["pause playback","pause music","pause media"]
MEDIA_STOP_CMDS    = ["stop music","stop media","stop playback","stop playing"]
MEDIA_NEXT_CMDS    = ["next","next track","next song","skip","skip track"]
MEDIA_PREV_CMDS    = ["previous","previous track","previous song","last track","last song","go back"]
MEDIA_SHUFFLE_CMDS = ["shuffle","shuffle on","shuffle off","toggle shuffle","shuffle music"]
MEDIA_REPEAT_CMDS  = ["repeat","repeat on","repeat off","toggle repeat","loop","loop on","loop off"]
MEDIA_VOL_UP_CMDS  = ["volume up","louder","turn it up","increase volume","turn up"]
MEDIA_VOL_DN_CMDS  = ["volume down","quieter","turn it down","decrease volume","turn down"]
MEDIA_FOLDER_CMDS  = ["open music folder","open music","select music folder","select music",
                        "select folder","open media folder","change folder","change music folder","change music"]

# ── Artist/song search command prefixes ──────────────────────────────────────
MEDIA_PLAY_ARTIST_CMDS = [
    "play anything by ", "play everything by ", "shuffle everything by ",
    "shuffle anything by ", "play all by ", "play all songs by ",
]

# Help panel voice triggers
HELP_OPEN_COMMANDS  = ["show help","open help","show commands","open commands",
                        "help","commands","list commands","what can i say"]
HELP_CLOSE_COMMANDS = ["close help","hide help","close commands","hide commands"]

# Acknowledgement phrases — one is chosen at random for each recognised command
ACK_PHRASES = ["working", "processing", "understood", "accessing"]

IMG_EXT = {".jpg",".jpeg",".png",".bmp",".gif",".webp"}
AUDIO_EXT = {".mp3",".ogg",".wav",".flac",".m4a",".opus"}
VIDEO_EXT = {".mp4",".avi",".mkv",".webm",".mov",".m4v"}

WALLPAPER_FILE  = os.path.expanduser("~/.loomos_wallpaper")
PASSPHRASE_FILE = os.path.expanduser("~/.loomos_passphrase")
SAMPLE_RATE     = 16000
OLLAMA_BASE     = "http://localhost:11434"
CIRCLE_ALPHA    = 64

APP_MINIMISE_CMDS  = ["minimise", "minimize", "hide", "shrink"]
APP_MAXIMISE_CMDS  = ["maximise", "maximize", "fullscreen", "full screen"]
APP_RESTORE_CMDS   = ["restore"]

# ── Slider range constants ─────────────────────────────────────────────────────
PANEL_ALPHA_MIN  = 0.0
PANEL_ALPHA_MAX  = 1.0
BAR_ALPHA_MIN    = 0.0
BAR_ALPHA_MAX    = 1.0
MIC_THRESH_MIN   = 0.005
MIC_THRESH_MAX   = 0.30


# ── Colours ───────────────────────────────────────────────────────────────────

BLACK       = (  0,   0,   0)
WHITE       = (255, 255, 255)
DARK_BG     = (  8,  12,  20)
ORANGE_BG   = (180,  90,  20)
ORANGE_MID  = (210, 110,  30)
ORANGE_LITE = (230, 140,  50)
BLUE_DARK   = ( 15,  35,  65)
BLUE_MID    = ( 30,  80, 130)
BLUE_LITE   = ( 80, 160, 210)
BLUE_AVATAR = ( 60, 140, 190)
GREEN_DARK  = ( 10,  40,  10)
GREEN_MID   = ( 30, 100,  30)
GREEN_LITE  = ( 80, 200,  80)
RED_DARK    = ( 60,   8,   8)
RED_MID     = (160,  20,  20)
RED_LITE    = (220,  60,  60)
ENROL_DARK  = ( 40,  20,  60)
ENROL_MID   = (100,  40, 160)
ENROL_LITE  = (180, 100, 255)
TEXT_DIM    = (120, 130, 150)
TEXT_MID_C  = (180, 190, 200)
TEXT_BRIGHT = (230, 240, 255)
TEXT_BOLD   = (255, 255, 255)
CLICK_HOVER = ( 60, 180, 200)
WAVEFORM_WHITE = (255, 255, 255)
MUTE_COL    = (120,  80,  80)
CONV_DARK   = ( 60,  30,   5)
CONV_MID    = (160,  80,  15)
CONV_LITE   = (230, 140,  40)
CONV_BAR_BG = ( 20,  12,   4)
CMD_TAB_DARK  = (  5,  40,  30)
CMD_TAB_MID   = ( 20, 110,  80)
CMD_TAB_LITE  = ( 60, 200, 140)

MEDIA_DARK    = (  8,  28,  12)
MEDIA_MID     = ( 28, 110,  45)
MEDIA_LITE    = ( 70, 210, 110)
MEDIA_ACCENT  = (120, 255, 160)
MEDIA_BAR_BG  = ( 10,  24,  14)

APP_DRAWER_BG    = (  5,  12,  22)
APP_TILE_BG      = ( 12,  24,  40)
APP_TILE_BDR     = ( 20,  70, 100)
APP_TILE_HOV     = ( 20,  55,  85)
APP_TILE_RUN_BDR = ( 40, 180, 120)
APP_ICON_COL     = ( 80, 200, 160)
APP_NAME_COL     = (210, 230, 250)
APP_DESC_COL     = ( 90, 110, 135)
APP_RUN_DOT      = ( 40, 200, 130)

BAR_HEIGHT = 36
PANEL_W    = 230

CHAT_YOU_COL  = (100, 200, 255)
CHAT_AI_COL   = (200, 160,  60)
CHAT_DIM_COL  = ( 90,  95, 110)
CMD_LOG_TIME_COL = ( 70,  80,  95)
CMD_LOG_TEXT_COL = (120, 210, 160)

TTS_HALO_OUTER  = ( 80, 160, 210)
TTS_HALO_MID    = ( 30,  80, 130)
TTS_HALO_INNER  = ( 15,  35,  65)
TTS_HALO_FRINGE = (120, 190, 240)

HELP_ACCENT  = ( 80, 200, 200)
HELP_HDR_COL = (160, 220, 240)
HELP_KEY_COL = ( 60, 180, 180)
HELP_VAL_COL = (180, 195, 215)
HELP_CAT_COL = (100, 170, 200)
HELP_DIVIDER = ( 25,  50,  80)

# ── Prompt Store ──────────────────────────────────────────────────────────
PROMPTS_DIR = Path.home() / "prompts"

class PromptStore:
    """Loads .txt prompt files from ~/prompts/ and tracks the active one."""
    def __init__(self):
        PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
        self._prompts: dict = {}
        self.active_name: Optional[str] = None
        self.active_text: Optional[str] = None
        self.reload()

    def reload(self):
        found = {}
        for f in sorted(PROMPTS_DIR.glob("*.txt")):
            try:
                text = f.read_text(encoding="utf-8").strip()
                if text:
                    found[f.stem] = text
            except Exception as e:
                print(f"[PromptStore] {f.name}: {e}")
        self._prompts = found
        print(f"[PromptStore] {len(found)} prompts loaded from {PROMPTS_DIR}")
        # keep active selection valid after reload
        if self.active_name and self.active_name not in self._prompts:
            self.active_name = None
            self.active_text = None

    @property
    def names(self) -> list:
        return list(self._prompts.keys())

    def select(self, name: Optional[str]):
        if name is None:
            self.active_name = None
            self.active_text = None
        else:
            self.active_name = name
            self.active_text = self._prompts.get(name)

    def fuzzy_find(self, query: str) -> Optional[str]:
        q = query.lower().strip()
        for n in self.names:
            if n.lower() == q:                return n
        for n in self.names:
            if n.lower().startswith(q):       return n
        for n in self.names:
            if q in n.lower():                return n
        q_words = set(q.split())
        best_n, best_s = None, 0
        for n in self.names:
            score = len(q_words & set(n.lower().split()))
            if score > best_s:
                best_s, best_n = score, n
        return best_n if best_s > 0 else None

# ── Mode / State enums ────────────────────────────────────────────────────────

class InputMode(Enum):
    COMMAND      = "CMD"
    CONVERSATION = "CONV"
    MEDIA        = "MEDIA"

class UIState(Enum):
    IDLE           = auto()
    ENROL_1        = auto()
    ENROL_2        = auto()
    ENROL_MISMATCH = auto()
    LOGIN          = auto()
    LOGIN_SUCCESS  = auto()
    LOGIN_ERROR    = auto()
    ACTIVE         = auto()
    SUSPENDED      = auto()


# ── Passphrase helpers ────────────────────────────────────────────────────────

def _hash_pp(tokens):
    return hashlib.sha256(" ".join(str(t) for t in tokens).upper().encode()).hexdigest()

def save_passphrase(tokens):
    with open(PASSPHRASE_FILE,"w") as f:
        json.dump({"hash":_hash_pp(tokens),"length":len(tokens)},f)
    os.chmod(PASSPHRASE_FILE, 0o600)

def verify_passphrase(tokens) -> bool:
    if not os.path.exists(PASSPHRASE_FILE): return False
    try:
        stored = json.load(open(PASSPHRASE_FILE))
        return hmac.compare_digest(stored["hash"], _hash_pp(tokens))
    except Exception: return False

def passphrase_enrolled() -> bool:
    return os.path.exists(PASSPHRASE_FILE)


# ── System font scanner ───────────────────────────────────────────────────────

def get_system_fonts() -> List[str]:
    return sorted(set(f for f in pygame.font.get_fonts() if f and len(f) > 1))


# ── Font loader ───────────────────────────────────────────────────────────────

_FONT_REG  = ["Segoeuiemoji"]
_FONT_BOLD  = ["Segoeuiemoji-Bold"]

def _ffind(paths):
    for p in paths:
        if p and os.path.isfile(p): return p
    return None

def load_fonts(font_name: str = "") -> dict:
    rp = bp = None
    if font_name:
        sp = pygame.font.match_font(font_name)
        if sp and os.path.isfile(sp):
            rp = bp = sp
            print(f"[Font] {font_name} -> {os.path.basename(sp)}")
    if rp is None:
        rp = _ffind(_FONT_REG); bp = _ffind(_FONT_BOLD) or rp; rp = rp or bp
        print(f"[Font] {os.path.basename(rp) if rp else 'pygame default'}")
    def mk(path, size):
        return pygame.font.Font(path, size) if path else pygame.font.Font(None, size)
    return {
        "sm":mk(rp,14),"md":mk(rp,18),"lg":mk(rp,24),
        "xl":mk(rp,34),"xxl":mk(rp,50),
        "sm_b":mk(bp,13),"md_b":mk(bp,18),"lg_b":mk(bp,24),"xl_b":mk(bp,34),
        "mono_sm":mk(rp,18),"mono_md":mk(rp,19),"mono_lg":mk(bp,24),
        "mono_xl":mk(bp,34),"mono_xxl":mk(bp,50),
    }


# ── Drawing helpers ───────────────────────────────────────────────────────────

def draw_circle_alpha(surface, colour_rgba, centre, radius):
    if radius <= 0: return
    s = pygame.Surface((radius*2, radius*2), pygame.SRCALPHA)
    pygame.draw.circle(s, colour_rgba, (radius, radius), radius)
    surface.blit(s, (centre[0]-radius, centre[1]-radius))

def draw_rounded_rect_alpha(surface, rect, fill_rgba, border_rgba=None,
                             border_w=0, radius=10):
    w, h = rect.width, rect.height
    if w <= 0 or h <= 0: return
    tmp = pygame.Surface((w, h), pygame.SRCALPHA)
    tmp.fill((0,0,0,0))
    r = tmp.get_rect()
    kw = ({"border_top_left_radius":radius[0],"border_top_right_radius":radius[1],
            "border_bottom_left_radius":radius[2],"border_bottom_right_radius":radius[3]}
          if isinstance(radius, tuple) else {"border_radius":radius})
    pygame.draw.rect(tmp, fill_rgba, r, **kw)
    if border_rgba and border_w > 0:
        pygame.draw.rect(tmp, border_rgba, r, border_w, **kw)
    surface.blit(tmp, rect.topleft)

def lerp_col(a, b, t):
    return tuple(int(a[i]+(b[i]-a[i])*t) for i in range(3))

def blend_col(base, tgt, speed, dt):
    return lerp_col(base, tgt, min(1.0, speed*dt))

def truncate_text(font, text: str, max_w: int) -> str:
    if font.size(text)[0] <= max_w: return text
    while text and font.size(text+"...")[0] > max_w:
        text = text[:-1]
    return text + "..."


# ── Help Panel ────────────────────────────────────────────────────────────────

_HELP_VOICE_SECTIONS = [
    ("PANELS & UI", [
        ("show/open model info",       "Open left panel"),
        ("close/hide model info",      "Close left panel"),
        ("show/open settings",         "Open right panel"),
        ("close/hide settings",        "Close right panel"),
        ("show help/commands",   "Open this help panel"),
        ("close help/commands", "Close this help panel"),
        ("show apps / app drawer",      "Open app launcher"),
        ("rescan apps",                 "Refresh app list"),
    ]),
    ("MICROPHONE & MODE", [
        ("stop listening",              "Mute microphone"),
        ("conversation mode",           "Switch to conversation mode"),
        ("command mode",                "Switch to command mode"),
    ]),
    ("SYSTEM", [
        ("set wallpaper / browse wallpapers", "Open wallpaper browser"),
        ("logout / log-out / sign out",       "Return to login screen"),
        ("clear",                             "Clears the Speech Circle"),
    ]),
    ("CONVERSATION MODE", [
        ("<anything>",                  "Speak freely - sends to LLM (if loaded)"),
        ("ESC (popup)",                 "Dismiss dictation popup"),
        ("ENTER (popup)",               "Submit dictation immediately"),
    ]),
    ("MEDIA MODE", [
        ("play <song name>",            "Play a specific song"),
        ("play <artist>, <track name>", "Search by artist, then ask for song"),
        ("play anything by <artist>",   "Shuffle all tracks by artist"),
        ("play / pause / stop",         "Playback controls"),
        ("next / previous",             "Skip tracks"),
        ("volume up/down",     "Adjust volume"),
        ("open/select folder",          "Browse for media folder"),
    ]),
    ("APPS", [
        ("open <app name>",             "Launch an app by keyword"),
        ("close <app name>",            "Close a running app"),
        ("minimise / hide <app name>",  "Minimise app window"),
        ("maximise <app name>",         "Maximise app window"),
        ("restore <app name>",          "Restore app window"),
    ]),
]

_HELP_KEY_SECTIONS = [
    ("FUNCTION KEYS", [
        ("F1",          "Toggle left panel (Model Info)"),
        ("F2",          "Toggle right panel (Settings)"),
        ("F3",          "Toggle app drawer"),
        ("CTRL+H",      "Toggle this help panel"),
    ]),
    ("SYSTEM SHORTCUTS", [
        ("CTRL+TAB",    "Toggle conversation / command mode"),
        ("CTRL+P",      "Toggle media mode"),
        ("CTRL+M",      "Toggle mic mute"),
        ("CTRL+W",      "Open wallpaper browser"),
        ("CTRL+L",      "Logout / return to login"),
        ("ESC",         "Quit application"),
    ]),
    ("LOGIN / ENROLMENT", [
        ("ENTER",       "Submit staged passphrase"),
        ("BACKSPACE",   "Remove last staged word"),
        ("DELETE",      "Clear all staged words"),
        ("C",           "Toggle click-input mode"),
    ]),
    ("DEBUG / DEV", [
        ("CTRL+S",      "Force login success state"),
        ("CTRL+E",      "Force login error state"),
        ("CTRL+A",      "Force active state"),
        ("CTRL+N",      "Reset passphrase (new enrolment)"),
        ("CTRL+C",      "Toggle click-input on login screen"),
    ]),
]

class HelpPanel:
    ANIM_SPEED  = 10.0
    RADIUS      = 12
    COL_PAD     = 28
    ROW_H       = 22
    CAT_GAP     = 10
    HDR_H       = 44
    BOTTOM_PAD  = 18
    SCROLL_SPD  = 3

    def __init__(self, sw: int, sh: int, fonts: dict):
        self.sw = sw
        self.sh = sh
        self.fonts = fonts
        self.open  = False
        self._t    = 0.0
        self._scroll = 0
        self._max_scroll = 0
        self._drag = False
        self._panel_rect: Optional[pygame.Rect] = None
        self._voice_rows = self._build_rows(_HELP_VOICE_SECTIONS)
        self._key_rows   = self._build_rows(_HELP_KEY_SECTIONS)
        self._content_h  = 0

    def toggle(self):    self.open = not self.open
    def set_open(self, v):
        self.open = (not self.open) if v is None else bool(v)

    def refresh_fonts(self, nf: dict):
        self.fonts = nf

    def handle_mousewheel(self, pos, dy) -> bool:
        if not self.open or self._panel_rect is None: return False
        if self._panel_rect.collidepoint(pos):
            self._scroll = max(0, min(self._max_scroll,
                                      self._scroll - dy * self.ROW_H * self.SCROLL_SPD))
            return True
        return False

    def handle_mousedown(self, pos) -> bool:
        if not self.open or self._panel_rect is None: return False
        return self._panel_rect.collidepoint(pos)

    def update(self, dt: float):
        target = 1.0 if self.open else 0.0
        self._t = max(0.0, min(1.0, self._t + (target - self._t) * self.ANIM_SPEED * dt))
        if not self.open:
            self._scroll = 0

    def draw(self, surface, panel_alpha: float = 1.0):
        if self._t < 0.005:
            self._panel_rect = None
            return

        a_eff   = self._t * panel_alpha
        ai      = int(255 * a_eff)
        f       = self.fonts
        lh      = self.ROW_H
        pad     = self.COL_PAD

        panel_w = min(self.sw - 80, 980)
        panel_x = (self.sw - panel_w) // 2

        voice_h = self._col_height(self._voice_rows, lh)
        key_h   = self._col_height(self._key_rows,   lh)
        content_h = max(voice_h, key_h)
        self._content_h = content_h

        max_panel_h = self.sh - BAR_HEIGHT * 2 - 60
        visible_h   = min(content_h + self.HDR_H + self.BOTTOM_PAD, max_panel_h)

        panel_y = BAR_HEIGHT + int((visible_h + 20) * (self._t - 1.0))
        panel_y = max(BAR_HEIGHT - visible_h - 4, panel_y)

        self._panel_rect = pygame.Rect(panel_x, panel_y, panel_w, visible_h)
        self._max_scroll = max(0, content_h - (visible_h - self.HDR_H - self.BOTTOM_PAD))

        draw_rounded_rect_alpha(surface, self._panel_rect,
            (12, 18, 30, int(255 * a_eff)),
            border_rgba=(*HELP_ACCENT, int(255 * a_eff)),
            border_w=1, radius=self.RADIUS)

        title_rect = pygame.Rect(panel_x, panel_y, panel_w, self.HDR_H)
        draw_rounded_rect_alpha(surface, title_rect,
            (18, 32, 50, int(255 * a_eff)),
            border_rgba=(*HELP_ACCENT, int(255 * a_eff)),
            border_w=0, radius=(self.RADIUS, self.RADIUS, 0, 0))

        icon_s = f["md_b"].render("?", True, (*HELP_ACCENT, ai))
        surface.blit(icon_s, (panel_x + 16, panel_y + (self.HDR_H - icon_s.get_height()) // 2))

        title_s = f["lg_b"].render("SPEECH & KEYBOARD REFERENCE", True, (*HELP_HDR_COL, ai))
        surface.blit(title_s, (panel_x + 40, panel_y + (self.HDR_H - title_s.get_height()) // 2))

        hint_s = f["sm"].render("CTRL+H  /  'close help'  /  ESC to close", True, (*TEXT_DIM, int(ai * 0.7)))
        surface.blit(hint_s, (panel_x + panel_w - hint_s.get_width() - 14,
                               panel_y + (self.HDR_H - hint_s.get_height()) // 2))

        sep_y = panel_y + self.HDR_H
        pygame.draw.line(surface, (*HELP_ACCENT, int(80 * a_eff)),
                         (panel_x + 10, sep_y), (panel_x + panel_w - 10, sep_y), 1)

        content_area = pygame.Rect(panel_x + 1, sep_y + 1,
                                   panel_w - 2,
                                   visible_h - self.HDR_H - 2)
        try:
            clip = surface.subsurface(content_area)
        except ValueError:
            return

        col_w = (panel_w - pad * 3) // 2
        col_x_left  = pad
        col_x_right = pad * 2 + col_w

        base_y = self.BOTTOM_PAD // 2 - self._scroll

        self._draw_column(clip, f, self._voice_rows, col_x_left,  base_y, col_w, ai, lh,
                           header="VOICE COMMANDS")
        self._draw_column(clip, f, self._key_rows,   col_x_right, base_y, col_w, ai, lh,
                           header="KEYBOARD SHORTCUTS")

        div_x = col_x_left + col_w + pad // 2 - 1
        div_h = content_area.height - 8
        pygame.draw.line(clip, (*HELP_DIVIDER, int(180 * a_eff)),
                         (div_x, 4), (div_x, div_h), 1)

        if self._max_scroll > 0:
            sb_h = content_area.height - 8
            tb_h = max(20, int(sb_h * (content_area.height / max(1, content_h))))
            tb_y = int((self._scroll / max(1, self._max_scroll)) * (sb_h - tb_h)) + 4
            pygame.draw.rect(clip, (*BLUE_DARK, int(ai * 0.4)),
                             (panel_w - 8, 4, 4, sb_h), border_radius=2)
            pygame.draw.rect(clip, (*HELP_ACCENT, ai),
                             (panel_w - 8, tb_y, 4, tb_h), border_radius=2)

    @staticmethod
    def _build_rows(sections):
        rows = []
        for cat, items in sections:
            rows.append(("cat", cat, ""))
            for key, desc in items:
                rows.append(("row", key, desc))
        return rows

    def _col_height(self, rows, lh):
        h = 0
        for kind, _, __ in rows:
            h += lh + (self.CAT_GAP if kind == "cat" else 0)
        return h + self.BOTTOM_PAD

    def _draw_column(self, clip, f, rows, cx, base_y, col_w, ai, lh, header=""):
        y = base_y
        key_w = col_w * 44 // 100

        hs = f["sm_b"].render(header, True, (*HELP_HDR_COL, int(ai * 0.9)))
        clip.blit(hs, (cx, y))
        y += lh + 4
        pygame.draw.line(clip, (*HELP_ACCENT, int(60 * ai / 255)),
                         (cx, y), (cx + col_w - 4, y), 1)
        y += 6

        for kind, key_text, desc_text in rows:
            if kind == "cat":
                y += self.CAT_GAP
                cat_s = f["sm_b"].render(key_text, True, (*HELP_CAT_COL, int(ai * 0.85)))
                clip.blit(cat_s, (cx, y))
                y += lh
            else:
                pill_rect = pygame.Rect(cx, y, key_w - 4, lh - 2)
                draw_rounded_rect_alpha(clip, pill_rect,
                    (*BLUE_DARK, int(ai * 0.55)),
                    border_rgba=(*HELP_KEY_COL, int(ai * 0.35)),
                    border_w=1, radius=3)
                ks = f["sm"].render(
                    truncate_text(f["sm"], key_text, key_w - 12),
                    True, (*HELP_KEY_COL, ai))
                clip.blit(ks, (cx + 5, y + (lh - 2 - ks.get_height()) // 2))

                ds = f["sm"].render(
                    truncate_text(f["sm"], desc_text, col_w - key_w - 6),
                    True, (*HELP_VAL_COL, int(ai * 0.85)))
                clip.blit(ds, (cx + key_w + 4, y + (lh - ds.get_height()) // 2))
                y += lh


# ── TTS Engine ────────────────────────────────────────────────────────────────
class TTSEngine:
    _RAMP_UP   = 0.18
    _RAMP_DOWN = 0.28
    _SUSTAIN   = 0.85
    _MIC_RELEASE_DELAY = 1.2

    def __init__(self, on_level=None, on_start=None, on_end=None):
        self.on_level  = on_level or (lambda v: None)
        self.on_start  = on_start or (lambda: None)
        self.on_end    = on_end or (lambda: None)
        self.ready     = False
        self.error     = ""
        self.speaking  = False
        self.mic_muted = False
        self._queue: List[str] = []
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._rate = 175
        self._volume = 1.0
        self._stop_flag = False
        self._level = 0.0
        self._utt_start = 0.0
        self._utt_len = 0.0

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="TTS-engine").start()
        threading.Thread(target=self._level_loop, daemon=True, name="TTS-level").start()

    def stop(self):
        self._stop_flag = True
        self._event.set()

    def speak(self, text: str):
        text = text.strip()
        if not text: return
        with self._lock:
            self._queue.append(text)
        self._event.set()

    def speak_immediate(self, text: str):
        text = text.strip()
        if not text: return
        with self._lock:
            self._queue.clear()
            self._queue.append(text)
        self._event.set()

    def set_rate(self, wpm: int):
        self._rate = wpm

    def set_volume(self, vol: float):
        self._volume = max(0.0, min(1.0, vol))

    def _tts_begin(self):
        self.speaking = True
        self.mic_muted = True
        self.on_start()

    def _tts_end(self):
        self.speaking = False
        time.sleep(self._MIC_RELEASE_DELAY)
        self.mic_muted = False
        self.on_end()

    def _run(self):
        if platform.system() == "Windows":
            self._run_windows()
        else:
            self._run_linux()

    def _run_windows(self):
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            self.ready = True
        except Exception as e:
            self.error = str(e)
            self._run_pyttsx3()
            return

        while not self._stop_flag:
            while not self._stop_flag:
                with self._lock:
                    text = self._queue.pop(0) if self._queue else None
                if text is None:
                    break

                words = len(text.split())
                self._utt_len = max(0.5, words / (self._rate / 60.0))
                self._utt_start = time.time()
                self._tts_begin()

                try:
                    speaker.Rate = int((self._rate - 175) / 25)
                    speaker.Volume = int(self._volume * 100)
                    speaker.Speak(text, 1)
                    while speaker.IsSpeaking():
                        if self._stop_flag:
                            speaker.Speak("", 2)
                            break
                        time.sleep(0.05)
                except:
                    pass

                self._tts_end()

            self._event.wait(timeout=0.1)
            self._event.clear()

    def _run_linux(self):
        espeak = None
        for cmd in ["espeak-ng", "espeak"]:
            try:
                subprocess.run([cmd, "--version"], capture_output=True, timeout=2)
                espeak = cmd
                break
            except:
                pass

        if espeak is None:
            self._run_pyttsx3()
            return

        self.ready = True

        while not self._stop_flag:
            while not self._stop_flag:
                with self._lock:
                    text = self._queue.pop(0) if self._queue else None
                if text is None:
                    break

                words = len(text.split())
                self._utt_len = max(0.5, words / (self._rate / 60.0))
                self._utt_start = time.time()
                self._tts_begin()

                try:
                    subprocess.run([
                        espeak,
                        "-s", str(self._rate),
                        "-a", str(int(self._volume * 200)),
                        text
                    ])
                except:
                    pass

                self._tts_end()

            self._event.wait(timeout=0.1)
            self._event.clear()

    def _run_pyttsx3(self):
        if not PYTTSX3_OK:
            self.error = "pyttsx3 not installed"
            return

        try:
            import pyttsx3 as _p
            engine = _p.init()
            engine.setProperty("rate", self._rate)
            engine.setProperty("volume", self._volume)
            self.ready = True
        except Exception as e:
            self.error = str(e)
            return

        while not self._stop_flag:
            while not self._stop_flag:
                with self._lock:
                    text = self._queue.pop(0) if self._queue else None
                if text is None:
                    break

                words = len(text.split())
                self._utt_len = max(0.5, words / (self._rate / 60.0))
                self._utt_start = time.time()
                self._tts_begin()

                try:
                    engine.setProperty("rate", self._rate)
                    engine.setProperty("volume", self._volume)
                    engine.say(text)
                    engine.runAndWait()
                except:
                    pass

                self._tts_end()

            self._event.wait(timeout=0.1)
            self._event.clear()

    def _level_loop(self):
        while not self._stop_flag:
            if self.speaking:
                el = time.time() - self._utt_start
                dur = self._utt_len
                ru, rd = self._RAMP_UP, self._RAMP_DOWN

                if el < ru:
                    base = (el / ru) * self._SUSTAIN
                elif el < dur - rd:
                    ph = el * 7.3
                    base = self._SUSTAIN + 0.06 * math.sin(ph) + 0.04 * math.sin(ph * 2.1)
                elif el < dur:
                    base = (1.0 - (el - (dur - rd)) / rd) * self._SUSTAIN
                else:
                    base = 0.0

                target = max(0.0, min(1.0, base))
            else:
                target = 0.0

            self._level += (target - self._level) * 0.35
            self.on_level(max(0.0, self._level))
            time.sleep(0.025)


# ── Chat History ──────────────────────────────────────────────────────────────

class ChatHistory:
    def __init__(self):
        self._turns: List[dict] = []; self._lock = threading.Lock()
    def add_user(self, text: str):
        with self._lock: self._turns.append({"role":"you","text":text.strip()})
    def start_ai(self) -> int:
        with self._lock:
            self._turns.append({"role":"ai","text":""}); return len(self._turns)-1
    def append_ai_token(self, idx: int, token: str):
        with self._lock:
            if 0 <= idx < len(self._turns): self._turns[idx]["text"] += token
    def get_turns(self) -> List[dict]:
        with self._lock: return list(self._turns)
    def clear(self):
        with self._lock: self._turns.clear()


# ── Command Log ───────────────────────────────────────────────────────────────

class CommandLog:
    MAX_ENTRIES = 200
    def __init__(self):
        self._entries: List[dict] = []; self._lock = threading.Lock()
    def add(self, text: str):
        ts = time.strftime("%H:%M:%S")
        with self._lock:
            self._entries.append({"ts":ts,"text":text})
            if len(self._entries) > self.MAX_ENTRIES:
                self._entries = self._entries[-self.MAX_ENTRIES:]
    def get_entries(self) -> List[dict]:
        with self._lock: return list(self._entries)
    def clear(self):
        with self._lock: self._entries.clear()


# ── Font Picker Dropdown ──────────────────────────────────────────────────────

class FontDropdown:
    ROW_H=26; MAX_ROWS=8; RADIUS=6; SCROLL_SPD=3

    def __init__(self, fonts, font_list, current, on_select=None):
        self.fonts=fonts; self.font_list=font_list; self.current=current
        self.on_select=on_select or (lambda n:None)
        self.open=False; self._scroll=0; self._hover_idx=-1
        self._header_rect=None; self._list_rect=None
        self._clamp_scroll_to_selection()

    def layout(self, x, y, w):
        self._header_rect=pygame.Rect(x,y,w,self.ROW_H+4)
        self._list_rect=pygame.Rect(x,y+self._header_rect.height,
                                    w,min(len(self.font_list),self.MAX_ROWS)*self.ROW_H)

    @property
    def total_height(self):
        h=self._header_rect.height if self._header_rect else self.ROW_H+4
        if self.open: h+=min(len(self.font_list),self.MAX_ROWS)*self.ROW_H
        return h

    def handle_mousedown(self, pos) -> bool:
        if self._header_rect and self._header_rect.collidepoint(pos):
            self.open=not self.open; return True
        if self.open and self._list_rect and self._list_rect.collidepoint(pos):
            idx=self._item_at(pos)
            if 0<=idx<len(self.font_list):
                self.current=self.font_list[idx]; self.on_select(self.current); self.open=False
            return True
        return False

    def handle_mousemove(self, pos):
        self._hover_idx=self._item_at(pos) if (self.open and self._list_rect
                                                and self._list_rect.collidepoint(pos)) else -1

    def handle_mousewheel(self, pos, dy) -> bool:
        if self.open and self._list_rect and self._list_rect.collidepoint(pos):
            self._scroll=max(0,min(len(self.font_list)-self.MAX_ROWS,self._scroll-dy*self.SCROLL_SPD))
            return True
        return False

    def close(self): self.open=False

    def draw(self, surface, alpha=1.0):
        if not self._header_rect: return
        a=int(255*alpha); hdr=self._header_rect
        draw_rounded_rect_alpha(surface,hdr,(20,30,48,int(a*0.92)),
            border_rgba=(*BLUE_MID,a) if self.open else (*BLUE_DARK,int(a*0.8)),
            border_w=1, radius=self.RADIUS if not self.open else (self.RADIUS,self.RADIUS,0,0))
        arrow=self.fonts["sm"].render("▴" if self.open else "▾",True,(*BLUE_LITE,a))
        surface.blit(arrow,(hdr.x+8,hdr.y+(hdr.height-arrow.get_height())//2))
        dn=self.current if self.current else "-- default font --"
        ds=self.fonts["sm"].render(truncate_text(self.fonts["sm"],dn,hdr.width-arrow.get_width()-28),
                                   True,(*TEXT_BRIGHT,a))
        surface.blit(ds,(hdr.x+arrow.get_width()+14,hdr.y+(hdr.height-ds.get_height())//2))
        if not self.open: return
        lr=self._list_rect
        draw_rounded_rect_alpha(surface,lr,(10,16,28,int(a*0.97)),
            border_rgba=(*BLUE_MID,int(a*0.7)),border_w=1,radius=(0,0,self.RADIUS,self.RADIUS))
        for i in range(min(len(self.font_list),self.MAX_ROWS)):
            idx=self._scroll+i
            if idx>=len(self.font_list): break
            name=self.font_list[idx]; iy=lr.y+i*self.ROW_H
            ir=pygame.Rect(lr.x+1,iy,lr.width-2,self.ROW_H)
            issel=(name==self.current); ishov=(idx==self._hover_idx)
            if issel: draw_rounded_rect_alpha(surface,ir,(*BLUE_MID,int(a*0.35)),radius=0)
            elif ishov: draw_rounded_rect_alpha(surface,ir,(*BLUE_DARK,int(a*0.5)),radius=0)
            col=TEXT_BOLD if issel else (TEXT_BRIGHT if ishov else TEXT_DIM)
            ls=self.fonts["sm"].render(truncate_text(self.fonts["sm"],name,lr.width-24),True,(*col,a))
            surface.blit(ls,(lr.x+10,iy+(self.ROW_H-ls.get_height())//2))
            if issel:
                tk=self.fonts["sm"].render("v",True,(*BLUE_LITE,a))
                surface.blit(tk,(lr.right-tk.get_width()-8,iy+(self.ROW_H-tk.get_height())//2))
        total=len(self.font_list)
        if total>self.MAX_ROWS:
            th=lr.height-4; tb=max(16,int(th*self.MAX_ROWS/total))
            ty=int((self._scroll/max(1,total-self.MAX_ROWS))*(th-tb))+lr.y+2
            draw_rounded_rect_alpha(surface,pygame.Rect(lr.right-6,lr.y+2,4,th),(*BLUE_DARK,int(a*0.4)),radius=2)
            draw_rounded_rect_alpha(surface,pygame.Rect(lr.right-6,ty,4,tb),(*BLUE_MID,a),radius=2)

    def _item_at(self, pos):
        if not self._list_rect: return -1
        return self._scroll+(pos[1]-self._list_rect.y)//self.ROW_H

    def _clamp_scroll_to_selection(self):
        if not self.current or self.current not in self.font_list:
            self._scroll=0; return
        self._scroll=max(0,min(self.font_list.index(self.current),len(self.font_list)-self.MAX_ROWS))


# ── Dictation Popup ───────────────────────────────────────────────────────────

class DictationPopup:
    FADE_SPD=8.0; HOLD_T=0.6

    def __init__(self, screen_w, screen_h, fonts, on_submit=None, circle_cy=None, circle_r=None):
        self.sw=screen_w; self.sh=screen_h; self.fonts=fonts
        self.on_submit=on_submit or (lambda t:None)
        self.circle_cx=screen_w//2
        self.circle_cy=circle_cy if circle_cy is not None else screen_h//2
        self.circle_r =circle_r  if circle_r  is not None else int(screen_w*0.13)
        self._state="hidden"; self._alpha=0.0
        self._text=""; self._hold_t=0.0; self._cursor_t=0.0
        self.conv_mode=False

    def update_position(self, cy, r): self.circle_cy=cy; self.circle_r=r

    @property
    def visible(self): return self._state!="hidden"

    def show(self, partial_text=""):
        if self._state in ("hidden","fading"): self._state="listening"; self._alpha=0.0
        self._text=partial_text

    def update_partial(self, text):
        if self._state=="hidden": self.show(text)
        else: self._text=text; self._state="listening"

    def finalize(self, text): self._text=text; self._state="confirming"; self._hold_t=0.0
    def dismiss(self): self._state="fading"

    def submit_now(self):
        if self._text.strip(): self.on_submit(self._text.strip())
        self._state="fading"

    def handle_key(self, key) -> bool:
        if not self.visible: return False
        if key==pygame.K_ESCAPE: self.dismiss(); return True
        if key in (pygame.K_RETURN,pygame.K_KP_ENTER): self.submit_now(); return True
        return False

    def update(self, dt):
        self._cursor_t+=dt
        if self._state=="listening":
            self._alpha=min(1.0,self._alpha+self.FADE_SPD*dt)
        elif self._state=="confirming":
            self._alpha=min(1.0,self._alpha+self.FADE_SPD*dt)
            self._hold_t+=dt
            if self._hold_t>=self.HOLD_T:
                if self._text.strip(): self.on_submit(self._text.strip())
                self._state="fading"
        elif self._state=="fading":
            self._alpha=max(0.0,self._alpha-self.FADE_SPD*dt)
            if self._alpha<=0.0: self._state="hidden"; self._text=""

    def _chord_w(self, r, dy):
        d2=r*r-dy*dy; return int(math.sqrt(max(0,d2))) if d2>0 else 0

    def _wrap_into_circle(self, font, text, r, uhh):
        words=text.split(); lines=[]; lh=font.get_linesize(); row=0; cur=""
        while words:
            dy=-uhh+int(row*lh)+lh//2; hw=self._chord_w(r,dy)-12
            if hw<20: row+=1; continue
            w=words[0]; cand=(cur+" "+w).strip() if cur else w
            if font.size(cand)[0]<=hw*2: cur=cand; words.pop(0)
            else:
                if cur: lines.append((cur,hw)); cur=""; row+=1
                else: lines.append((truncate_text(font,w,hw*2),hw)); words.pop(0); row+=1
        if cur:
            dy=-uhh+int(row*lh)+lh//2; hw=self._chord_w(r,dy)-12
            lines.append((cur,max(hw,20)))
        return lines

    def draw(self, surface):
        if self._state=="hidden" or self._alpha<0.01: return
        a=int(self._alpha*255); cx=self.circle_cx; cy=self.circle_cy; r=self.circle_r
        font=self.fonts["md_b"]; sf=self.fonts["sm"]; lh=font.get_linesize()
        bot_res=sf.get_height()+14; uhh=(r-bot_res-8-((-r+14)))//2
        bc_y=cy+(-r+14+r-bot_res-8)//2
        draw_circle_alpha(surface,(0,0,0,int(self._alpha*80)),(cx,cy),r-2)
        rl=self._wrap_into_circle(font,self._text,r,uhh) if self._text else []
        n=len(rl); th=n*lh
        if n==0: ay=cy-lh//2
        else:
            cy_=bc_y-th//2; ta=cy+(-r+14)
            fr=min(1.0,th/max(1,uhh*2)); ay=int(cy_+(ta-cy_)*fr)
        for i,(lt,_) in enumerate(rl):
            fade=max(0.0,min(1.0,(i-(n-3))/2.0))
            ls=font.render(lt,True,(*lerp_col(TEXT_DIM,TEXT_BOLD,fade),a))
            surface.blit(ls,(cx-font.size(lt)[0]//2,ay+i*lh))
        if rl and self._state=="listening":
            lt,_=rl[-1]; lx=cx-font.size(lt)[0]//2
            if int(self._cursor_t*2)%2==0:
                cx2=lx+font.size(lt)[0]+3
                draw_rounded_rect_alpha(surface,pygame.Rect(cx2,ay+(n-1)*lh+3,2,lh-6),
                    (*(CONV_LITE if self.conv_mode else BLUE_LITE),a),radius=1)
        if not rl:
            ps=font.render("Listening...",True,(*TEXT_DIM,int(a*0.6)))
            surface.blit(ps,(cx-ps.get_width()//2,cy-ps.get_height()//2))
        dy2=cy+r-bot_res+4
        if self._state=="listening":
            pulse=0.5+0.5*math.sin(self._cursor_t*6.0)
            dc=lerp_col(CONV_MID,CONV_LITE,pulse) if self.conv_mode else lerp_col(ORANGE_MID,ORANGE_LITE,pulse)
        else: dc=GREEN_LITE
        draw_circle_alpha(surface,(*dc,int(a*0.35)),(cx,dy2),10)
        draw_circle_alpha(surface,(*dc,a),(cx,dy2),5)
        slbl={"listening":"LISTENING","confirming":"PROCESSING","fading":""}.get(self._state,"")
        if slbl:
            sl=sf.render(slbl,True,(*TEXT_DIM,int(a*0.7)))
            surface.blit(sl,(cx-sl.get_width()//2,dy2+8))


# ── Wallpaper Browser ─────────────────────────────────────────────────────────

class WallpaperBrowser:
    THUMB_W=200; THUMB_H=120; THUMB_PAD=16; COLS=5
    SEARCH_DIRS=[
        os.path.expanduser("~/Pictures/wallpapers"),
        os.path.expanduser("~/Wallpapers"),
        os.path.expanduser("~/wallpapers"),
        "/usr/share/backgrounds",
        "/usr/share/wallpapers",
        "/usr/share/pixmaps/backgrounds",
    ]

    def __init__(self, sw, sh, fonts, on_select=None):
        self.sw=sw; self.sh=sh; self.fonts=fonts
        self.on_select=on_select or (lambda p:None)
        self.visible=False; self._images=[]; self._thumbs={}
        self._selected=0; self._scroll=0; self._loading=False
        self._alpha=0.0; self._fade_in=False
        tw=self.COLS*(self.THUMB_W+self.THUMB_PAD)-self.THUMB_PAD
        self._grid_x=(sw-tw)//2; self._grid_y=BAR_HEIGHT+60

    def open(self):
        if self.visible: return
        self.visible=True; self._fade_in=True; self._alpha=0.0
        self._selected=0; self._scroll=0
        if not self._images: self._start_scan()

    def close(self): self.visible=False; self._fade_in=False

    def handle_key(self, key) -> bool:
        if not self.visible: return False
        n=len(self._images)
        if key==pygame.K_ESCAPE: self.close(); return True
        if key in (pygame.K_RETURN,pygame.K_KP_ENTER):
            if self._images: self.on_select(self._images[self._selected])
            self.close(); return True
        if key==pygame.K_RIGHT and self._selected<n-1: self._selected+=1; self._clamp(); return True
        if key==pygame.K_LEFT  and self._selected>0:   self._selected-=1; self._clamp(); return True
        if key==pygame.K_DOWN: self._selected=min(n-1,self._selected+self.COLS); self._clamp(); return True
        if key==pygame.K_UP:   self._selected=max(0,self._selected-self.COLS);   self._clamp(); return True
        return False

    def handle_click(self, pos) -> bool:
        if not self.visible: return False
        for i,rect in enumerate(self._thumb_rects()):
            if rect.collidepoint(pos) and i<len(self._images):
                if self._selected==i: self.on_select(self._images[i]); self.close()
                else: self._selected=i
                return True
        return False

    def update(self, dt):
        if not self.visible: return
        self._alpha=max(0.0,min(1.0,self._alpha+(1.0 if self._fade_in else 0.0-self._alpha)*10.0*dt))
        self._load_pending_thumbs()

    def draw(self, surface):
        if not self.visible or self._alpha<0.01: return
        a=int(self._alpha*255)
        ov=pygame.Surface((self.sw,self.sh),pygame.SRCALPHA); ov.fill((5,8,15,int(a*0.92))); surface.blit(ov,(0,0))
        t=self.fonts["xl_b"].render("WALLPAPER BROWSER",True,(*TEXT_BRIGHT,a))
        surface.blit(t,(self.sw//2-t.get_width()//2,BAR_HEIGHT+16))
        h=self.fonts["sm"].render("Keys to select  *  ENTER to apply  *  ESC to close",True,(*TEXT_DIM,a))
        surface.blit(h,(self.sw//2-h.get_width()//2,BAR_HEIGHT+16+t.get_height()+4))
        if not self._images:
            ms=self.fonts["md"].render("Scanning..." if self._loading else "No images found",True,(*TEXT_DIM,a))
            surface.blit(ms,(self.sw//2-ms.get_width()//2,self.sh//2-ms.get_height()//2)); return
        for i,rect in enumerate(self._thumb_rects()):
            idx=self._scroll*self.COLS+i
            if idx>=len(self._images): break
            path=self._images[idx]; issel=(idx==self._selected)
            draw_rounded_rect_alpha(surface,rect,(*BLUE_MID,int(a*0.6)) if issel else (20,26,38,int(a*0.5)),
                border_rgba=(*BLUE_LITE,a) if issel else (*TEXT_DIM,a),border_w=2,radius=6)
            th=self._thumbs.get(path)
            if th:
                tw2,th2=th.get_size(); surface.blit(th,(rect.x+(rect.w-tw2)//2,rect.y+(rect.h-th2)//2))
            else:
                draw_rounded_rect_alpha(surface,pygame.Rect(rect.x+2,rect.y+2,rect.w-4,rect.h-4),(30,36,50,int(a*0.6)),radius=4)
                ls=self.fonts["sm"].render("loading...",True,(*TEXT_DIM,a))
                surface.blit(ls,(rect.x+rect.w//2-ls.get_width()//2,rect.y+rect.h//2-ls.get_height()//2))
            fn=os.path.basename(path); fn=fn[:18]+"..." if len(fn)>18 else fn
            fl=self.fonts["sm"].render(fn,True,(*TEXT_DIM,a))
            surface.blit(fl,(rect.x+rect.w//2-fl.get_width()//2,rect.y+rect.h-fl.get_height()-2))
        rows=math.ceil(len(self._images)/self.COLS)
        if rows>self._vrows():
            prog=self._scroll/max(1,rows-self._vrows()); bh=self.sh-self._grid_y-40
            draw_rounded_rect_alpha(surface,pygame.Rect(self.sw-8,self._grid_y+int(prog*(bh-40)),4,40),(*BLUE_MID,a),radius=2)

    def _vrows(self): return max(1,(self.sh-self._grid_y-BAR_HEIGHT-40)//(self.THUMB_H+self.THUMB_PAD))
    def _thumb_rects(self):
        rects=[]
        for row in range(self._vrows()):
            for col in range(self.COLS):
                rects.append(pygame.Rect(self._grid_x+col*(self.THUMB_W+self.THUMB_PAD),
                                         self._grid_y+row*(self.THUMB_H+self.THUMB_PAD),
                                         self.THUMB_W,self.THUMB_H))
        return rects
    def _clamp(self):
        row=self._selected//self.COLS; vr=self._vrows()
        if row<self._scroll: self._scroll=row
        elif row>=self._scroll+vr: self._scroll=row-vr+1
    def _start_scan(self):
        self._loading=True; threading.Thread(target=self._scan,daemon=True).start()
    def _scan(self):
        found=[]
        for d in self.SEARCH_DIRS:
            if not os.path.isdir(d): continue
            try:
                for root,dirs,files in os.walk(d):
                    dirs[:]=[x for x in dirs if not x.startswith(".")][:3]
                    for f in files:
                        if os.path.splitext(f)[1].lower() in IMG_EXT: found.append(os.path.join(root,f))
                        if len(found)>200: break
                    if len(found)>200: break
            except PermissionError: pass
        self._images=sorted(found); self._loading=False
    def _load_pending_thumbs(self):
        loaded=0; vr=self._vrows(); start=self._scroll*self.COLS
        for idx in range(start,min(len(self._images),start+vr*self.COLS+self.COLS)):
            if loaded>=2: break
            path=self._images[idx]
            if path not in self._thumbs:
                try:
                    img=pygame.image.load(path).convert(); tw,th=img.get_size()
                    scale=min((self.THUMB_W-4)/tw,(self.THUMB_H-20)/th)
                    self._thumbs[path]=pygame.transform.smoothscale(img,(max(1,int(tw*scale)),max(1,int(th*scale))))
                    loaded+=1
                except Exception: self._thumbs[path]=None


# ── System poller ─────────────────────────────────────────────────────────────

class SystemPoller:
    ACTIVITY_MAX_CHARS=60

    def __init__(self, hw_interval=2.0, model_interval=5.0):
        self.hw_interval=hw_interval; self.model_interval=model_interval
        self.data={
            "cpu":0.0,"ram":0.0,"gpu":0.0,"ram_used_mb":0,"ram_total_mb":0,
            "disk_pct":0.0,"battery":-1,"plugged":True,"net_up_kb":0.0,"net_dn_kb":0.0,
            "model_loaded":"None","model_activity":"Awaiting Input..","model_size_gb":0.0,"generating":False,
        }
        self._running=False; self._last_net=None; self._last_net_t=None
        self.on_token: Optional[callable]=None

    def start(self):
        self._running=True
        threading.Thread(target=self._run_hw,daemon=True).start()
        threading.Thread(target=self._run_model,daemon=True).start()

    def stop(self): self._running=False

    def _run_hw(self):
        while self._running: self._poll_hw(); time.sleep(self.hw_interval)
    def _run_model(self):
        while self._running: self._poll_model(); time.sleep(self.model_interval)

    def _poll_hw(self):
        if not PSUTIL_OK: return
        try:
            self.data["cpu"]=psutil.cpu_percent(interval=None)
            vm=psutil.virtual_memory()
            self.data["ram"]=vm.percent; self.data["ram_used_mb"]=vm.used//(1024*1024); self.data["ram_total_mb"]=vm.total//(1024*1024)
        except Exception: pass
        try: self.data["disk_pct"]=psutil.disk_usage("/").percent
        except Exception: pass
        try:
            bat=psutil.sensors_battery()
            self.data["battery"]=int(bat.percent) if bat else -1; self.data["plugged"]=bat.power_plugged if bat else True
        except Exception: self.data["battery"]=-1
        try:
            nc=psutil.net_io_counters(); now=time.time()
            if self._last_net:
                dt=max(0.001,now-self._last_net_t)
                self.data["net_up_kb"]=(nc.bytes_sent-self._last_net.bytes_sent)/dt/1024
                self.data["net_dn_kb"]=(nc.bytes_recv-self._last_net.bytes_recv)/dt/1024
            self._last_net,self._last_net_t=nc,now
        except Exception: pass
        try:
            r=subprocess.run(["nvidia-smi","--query-gpu=utilization.gpu","--format=csv,noheader,nounits"],
                             capture_output=True,text=True,timeout=2)
            if r.returncode==0: self.data["gpu"]=float(r.stdout.strip().split(",")[0])
        except Exception: self.data["gpu"]=0.0

    def _poll_model(self):
        try:
            with urllib.request.urlopen(f"{OLLAMA_BASE}/api/ps",timeout=2) as resp:
                d=json.loads(resp.read()); models=d.get("models",[])
                if models:
                    m=models[0]; self.data["model_loaded"]=m.get("name","Unknown"); self.data["model_size_gb"]=m.get("size",0)/1e9
                    if not self.data["generating"]: self.data["model_activity"]="Ready"
                else:
                    self.data["model_loaded"]="None"; self.data["model_size_gb"]=0.0
                    if not self.data["generating"]: self.data["model_activity"]="Awaiting Input.."
        except Exception: pass

    def stream_generation(self, prompt: str, system_prompt: str = ""):
        def _stream():
            self.data["generating"]=True; self.data["model_activity"]="Thinking.."; accumulated=""
            try:
                payload = {"model": self.data["model_loaded"], "prompt": prompt, "stream": True}
                if system_prompt:
                    payload["system"] = system_prompt
                body = json.dumps(payload).encode()
                req=urllib.request.Request(f"{OLLAMA_BASE}/api/generate",data=body,headers={"Content-Type":"application/json"})
                with urllib.request.urlopen(req,timeout=120) as resp:
                    for raw_line in resp:
                        if not self._running: break
                        line=raw_line.decode().strip()
                        if not line: continue
                        try: chunk=json.loads(line)
                        except json.JSONDecodeError: continue
                        token=chunk.get("response",""); accumulated+=token
                        self.data["model_activity"]=accumulated[-self.ACTIVITY_MAX_CHARS:].lstrip("\n")
                        if self.on_token: self.on_token(token)
                        if chunk.get("done",False): break
            except Exception: pass
            finally: self.data["generating"]=False; self.data["model_activity"]="Awaiting Input.."
        threading.Thread(target=_stream,daemon=True).start()


# ── Webcam feed ───────────────────────────────────────────────────────────────

class WebcamFeed:
    def __init__(self): self._frame=None; self._lock=threading.Lock(); self._running=False; self.ready=False; self.error=""
    def start(self):
        if not CV2_OK: self.error="opencv-python not installed"; return
        self._running=True; threading.Thread(target=self._run,daemon=True).start()
    def stop(self): self._running=False
    def _run(self):
        cap=cv2.VideoCapture(0)
        if not cap.isOpened(): self.error="Cannot open webcam"; return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,320); cap.set(cv2.CAP_PROP_FRAME_HEIGHT,240); self.ready=True
        consecutive_failures = 0
        while self._running:
            try:
                ret, frame = cap.read()
                if ret and frame is not None and frame.size > 0:
                    consecutive_failures = 0
                    with self._lock:
                        self._frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                else:
                    consecutive_failures += 1
                    time.sleep(0.05)
                    if consecutive_failures > 40:
                        cap.release()
                        time.sleep(1.0)
                        cap = cv2.VideoCapture(0)
                        if not cap.isOpened():
                            self.error = "Webcam lost after resume"; break
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
                        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
                        consecutive_failures = 0
            except Exception:
                time.sleep(0.1)
        cap.release()
    def get_frame(self):
        with self._lock: return self._frame.copy() if self._frame is not None else None

def _np_to_pg(arr): return pygame.surfarray.make_surface(np.ascontiguousarray(arr.transpose(1,0,2)))

def make_circular_cam(frame_rgb, diameter, border_col, bw=3):
    if frame_rgb is None: return None
    h, w = frame_rgb.shape[:2]
    if h <= 0 or w <= 0 or diameter <= 0: return None
    side = min(h, w)
    if side <= 0: return None
    sq = frame_rgb[(h-side)//2:(h+side)//2, (w-side)//2:(w+side)//2]
    if sq.shape[0] <= 0 or sq.shape[1] <= 0: return None
    try:
        raw = _np_to_pg(sq)
        scaled = pygame.transform.smoothscale(raw, (diameter, diameter))
        out = pygame.Surface((diameter, diameter), pygame.SRCALPHA)
        r = diameter // 2
        pygame.draw.circle(out, (255, 255, 255, 255), (r, r), r)
        out.blit(scaled, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
        am = pygame.Surface((diameter, diameter), pygame.SRCALPHA)
        am.fill((0, 0, 0, 255 - CIRCLE_ALPHA))
        out.blit(am, (0, 0), special_flags=pygame.BLEND_RGBA_SUB)
        pygame.draw.circle(out, (*border_col, 220), (r, r), r, bw)
        return out
    except Exception:
        return None


# ── Vosk STT ──────────────────────────────────────────────────────────────────

class VoskSTT:
    MODEL_PATHS=[
        os.environ.get("VOSK_MODEL_PATH",""),
        os.path.expanduser("~/vosk-model"),
        os.path.expanduser("~/vosk-model-small-en-us-0.15"),
        "/opt/vosk-model","./vosk-model","models/EN",
    ]
    def __init__(self,on_partial=None,on_final=None,on_level=None):
        self.on_partial=on_partial or (lambda r,s:None)
        self.on_final  =on_final   or (lambda t,r:None)
        self.on_level  =on_level   or (lambda v:None)
        self._running=False; self.ready=False; self.error=""; self.muted=False
    def start(self):
        if not VOSK_OK: self.error="vosk not installed"; return
        self._running=True; threading.Thread(target=self._run,daemon=True).start()
    def stop(self): self._running=False
    def _find(self):
        for p in self.MODEL_PATHS:
            if p and os.path.isdir(p): return p
        return None
    def _run(self):
        mp=self._find()
        if not mp: self.error="No Vosk model"; print("[VoskSTT] "+self.error); return
        try:
            import vosk; vosk.SetLogLevel(-1)
            model=vosk.Model(mp); rec=vosk.KaldiRecognizer(model,SAMPLE_RATE)
            rec.SetWords(True); self.ready=True; print(f"[VoskSTT] Ready -- {mp}")
        except Exception as e: self.error=str(e); print(f"[VoskSTT] {e}"); return
        def cb(indata,frames,ti,status):
            if not self._running: return
            rms=float(np.sqrt(np.mean(indata[:,0]**2))); self.on_level(min(1.0,rms*8.0))
            if self.muted: return
            pcm=(indata[:,0]*32767).astype(np.int16).tobytes()
            if rec.AcceptWaveform(pcm):
                raw=json.loads(rec.Result()).get("text","").strip()
                if raw: self.on_final(self._tok(raw),raw)
            else:
                raw=json.loads(rec.PartialResult()).get("partial","").strip()
                if raw: self.on_partial(raw,self._partial_sets(raw))
        try:
            with sd.InputStream(samplerate=SAMPLE_RATE,channels=1,dtype="float32",blocksize=4000,callback=cb):
                while self._running: time.sleep(0.05)
        except Exception as e: self.error=str(e); print(f"[VoskSTT] {e}")
    def _tok(self,text):
        out=[]
        for w in text.lower().split():
            if w in SPOKEN_NUMBERS: out.append(SPOKEN_NUMBERS[w])
            elif w.upper() in NATO_SET: out.append(w.upper())
        return out
    def _partial_sets(self,text):
        t=self._tok(text)
        return {x for x in t if isinstance(x,str)},{x for x in t if isinstance(x,int)}
    @staticmethod
    def panel_command(raw):
        low=raw.lower().strip()
        for phrase,action in PANEL_COMMANDS.items():
            if phrase in low: return action
        return None
    @staticmethod
    def wallpaper_command(raw): return any(c in raw.lower().strip() for c in WALLPAPER_COMMANDS)
    @staticmethod
    def mute_command(raw):
        low=raw.lower().strip()
        if any(c in low for c in MUTE_COMMANDS):   return "mute"
        if any(c in low for c in UNMUTE_COMMANDS): return "unmute"
        return None
    @staticmethod
    def logout_command(raw) -> bool: return any(p in raw.lower().strip() for p in LOGOUT_COMMANDS)
    @staticmethod
    def mode_command(raw) -> Optional[str]:
        low=raw.lower().strip()
        if any(c in low for c in CONV_MODE_COMMANDS):   return "conversation"
        if any(c in low for c in CMD_MODE_COMMANDS):    return "command"
        if any(c in low for c in TOGGLE_MODE_COMMANDS): return "toggle"
        if any(c in low for c in MEDIA_MODE_COMMANDS):  return "media"
        return None

    @staticmethod
    def media_command(raw) -> Optional[str]:
        """Detect media player transport commands. Returns command key or None."""
        low = raw.lower().strip()

        # ── "play anything by <artist>" — must check BEFORE plain "play" ─────
        for prefix in MEDIA_PLAY_ARTIST_CMDS:
            if low.startswith(prefix):
                artist = low[len(prefix):].strip()
                if artist:
                    return f"play_artist_all:{artist}"

        # ── "play <name>" — could be a song or artist; resolved in handler ───
        if low.startswith("play "):
            tail = low[len("play "):].strip()
            if tail and tail not in ("music", "media"):
                is_transport = any(low == c or low == c.rstrip() for c in MEDIA_PLAY_CMDS)
                if not is_transport:
                    return f"play_named:{tail}"

        if any(c == low or low.startswith(c) for c in MEDIA_PLAY_CMDS):   return "play"
        if any(c == low for c in MEDIA_PAUSE_CMDS):   return "pause"
        if any(c == low for c in MEDIA_STOP_CMDS):    return "stop"
        if any(c == low for c in MEDIA_NEXT_CMDS):    return "next"
        if any(c == low for c in MEDIA_PREV_CMDS):    return "prev"
        if any(c == low for c in MEDIA_SHUFFLE_CMDS): return "shuffle"
        if any(c == low for c in MEDIA_REPEAT_CMDS):  return "repeat"
        if any(c == low or c in low for c in MEDIA_VOL_UP_CMDS): return "vol_up"
        if any(c == low or c in low for c in MEDIA_VOL_DN_CMDS): return "vol_dn"
        if any(c == low for c in MEDIA_FOLDER_CMDS):  return "folder"

        for prefix in ("set volume ", "volume "):
            if low.startswith(prefix):
                tail = low[len(prefix):].strip()

                ones = {
                    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
                    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19
                }

                tens = {
                    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "max": 100,
                    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "one hundred": 100
                }

                # common STT fixes
                fixes = {
                    "forty": "forty",
                    "tree": "three",
                    "won": "one",
                    "to": "two",
                    "too": "two"
                }
                tail = fixes.get(tail, tail)

                def words_to_num(txt):
                    txt = txt.replace("-", " ")
                    parts = txt.split()

                    if txt == "hundred" or txt == "one hundred":
                        return 100

                    if len(parts) == 1:
                        if parts[0] in ones:
                            return ones[parts[0]]
                        if parts[0] in tens:
                            return tens[parts[0]]

                    if len(parts) == 2:
                        if parts[0] in tens and parts[1] in ones:
                            return tens[parts[0]] + ones[parts[1]]

                    return None

                n = words_to_num(tail)
                if n is not None:
                    return f"vol_set:{n / 100}"

                try:
                    num = float(tail)
                    return f"vol_set:{num / 100 if num > 1 else num}"
                except ValueError:
                    pass
        return None

    @staticmethod
    def help_command(raw) -> Optional[str]:
        """Returns 'open', 'close', or None."""
        low=raw.lower().strip()
        if any(c in low for c in HELP_CLOSE_COMMANDS): return "close"
        if any(c in low for c in HELP_OPEN_COMMANDS):  return "open"
        return None


# ── System status ─────────────────────────────────────────────────────────────

@dataclass
class SystemStatus:
    device_name:str="LoomOS-DEV"; mesh_connected:int=0; mesh_role:int=0
    model_loaded:str="None"; model_activity:str="Not Loaded"; model_size_gb:float=0.0
    cpu:float=0.0; gpu:float=0.0; ram:float=0.0
    ram_used_mb:int=0; ram_total_mb:int=0; disk_pct:float=0.0
    battery:int=-1; plugged:bool=True; net_up_kb:float=0.0; net_dn_kb:float=0.0
    time_str:str="00:00"; date_str:str=""
    highlighted_words:set=field(default_factory=set)
    highlighted_nums:set=field(default_factory=set)
    stt_level:float=0.0; tts_level:float=0.0
    stt_ready:bool=False; stt_muted:bool=False
    input_mode:InputMode=field(default_factory=lambda:InputMode.COMMAND)


# ── Media Player Mode ─────────────────────────────────────────────────────────

# ── PATCHED: home directory added; deduplicated scan with raised cap ──────────
MEDIA_SEARCH_DIRS = [
    os.path.expanduser("~/Music"),
    os.path.expanduser("~/music"),
    os.path.expanduser("~/Videos"),
    os.path.expanduser("~/videos"),
    os.path.expanduser("~/Movies"),
    #os.path.expanduser("~"),          # broad fallback — catches any subdir
    #"/usr/share/sounds",
]

# Directories to skip when walking from home to avoid noise / permission storms
_HOME_SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", "snap", ".local",
    ".config", ".cache", ".mozilla", ".thunderbird", ".steam",
    "proc", "sys", "dev",
}

def _scan_media(dirs=None) -> list:
    """Scan directories for audio and video files."""
    home = os.path.expanduser("~")
    found = []
    seen_real: set = set()

    for d in (dirs or MEDIA_SEARCH_DIRS):
        if not os.path.isdir(d):
            continue
        # Shallower cap when scanning from home to avoid very deep trees
        max_depth = 5 if os.path.realpath(d) == os.path.realpath(home) else 8
        try:
            for root, dirs2, files in os.walk(d, followlinks=False):
                depth = root[len(d):].count(os.sep)
                if depth >= max_depth:
                    dirs2[:] = []
                    continue
                dirs2[:] = [
                    x for x in dirs2
                    if not x.startswith(".") and x not in _HOME_SKIP_DIRS
                ]
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext in AUDIO_EXT or ext in VIDEO_EXT:
                        full = os.path.join(root, f)
                        rp   = os.path.realpath(full)
                        if rp not in seen_real:
                            seen_real.add(rp)
                            found.append(full)
                    if len(found) > 2000:
                        break
                if len(found) > 2000:
                    break
        except PermissionError:
            pass

    return sorted(found)


class MediaPlayerMode:
    """
    Universal media player that lives inside the main WaveformCircle.
    Audio plays via pygame.mixer; video frames are rendered via cv2 inside
    the circle.  Speech detection lowers volume and the waveform circle
    re-appears over the top.
    """

    DUCK_VOLUME    = 0.20
    DUCK_SPEED     = 6.0
    NUM_INNER_BARS = 24
    CTRL_BTN_W     = 52
    CTRL_BTN_H     = 32
    CTRL_RADIUS    = 8

    def __init__(self, cx: int, cy: int, circle_r: int, fonts: dict):
        self.cx = cx; self.cy = cy; self.r = circle_r
        self.fonts = fonts

        self.tracks: list   = []
        self.current_idx    = 0

        self.playing  = True
        self.paused   = False
        self.volume   = 0.7
        self.shuffle  = False
        self.repeat   = False

        self._duck_target  = 1.0
        self._duck_factor  = 1.0

        self._phase        = 0.0
        self._bars         = [0.0] * self.NUM_INNER_BARS
        self._eq_phase     = 0.0

        self._art_surf: Optional[pygame.Surface] = None
        self._art_cache: dict = {}

        self._video_cap    = None
        self._video_frame: Optional[pygame.Surface] = None
        self._video_lock   = threading.Lock()
        self._video_thread = None
        self._video_running = False
        self._video_path   = None
        self._video_fps    = 24.0
        self._video_pos    = 0.0
        self._video_duration = 0.0

        self._start_t   = 0.0
        self._position  = 0.0
        self._duration  = 0.0

        self._btn_rects: list = []
        self.music_level: float = 0.0

        self.status_msg = "Scanning for media…"

        # ── Pending artist search state ───────────────────────────────────────
        self._pending_artist: Optional[str] = None
        self._pending_artist_matches: list  = []

        threading.Thread(target=self._bg_scan, daemon=True).start()

    # ── public API ───────────────────────────────────────────────────────────

    def refresh_fonts(self, fonts: dict):
        self.fonts = fonts

    def play(self):
        if not self.tracks: return
        path = self.tracks[self.current_idx]
        ext  = os.path.splitext(path)[1].lower()

        if ext in VIDEO_EXT:
            self._play_video(path)
        else:
            self._play_audio(path)

    def pause(self):
        if self._video_cap is not None and self._video_running:
            self._video_running = False
            self.paused = True
            pygame.mixer.music.pause()
            self.status_msg = "Paused"
        elif self.playing and not self.paused:
            pygame.mixer.music.pause()
            self.paused  = True
            self._position = time.time() - self._start_t
            self.status_msg = "Paused"

    def resume(self):
        if self.paused:
            if self._video_cap is not None:
                self._video_running = True
                self._video_thread = threading.Thread(target=self._video_loop, daemon=True)
                self._video_thread.start()
            pygame.mixer.music.unpause()
            self.paused  = False
            self.playing = True
            self._start_t = time.time() - self._position
            self.status_msg = f"Playing: {self._track_name()}"

    def stop(self):
        self._stop_video()
        pygame.mixer.music.stop()
        self.playing = False; self.paused = False; self._position = 0.0
        self._video_frame = None
        self.status_msg = "Stopped"

    def next_track(self):
        if not self.tracks: return
        was = self.playing or self.paused
        self.stop()
        if self.shuffle:
            self.current_idx = random.randrange(len(self.tracks))
        else:
            self.current_idx = (self.current_idx + 1) % len(self.tracks)
        self._resolve_art()
        if was: self.play()

    def prev_track(self):
        if not self.tracks: return
        was = self.playing or self.paused
        self.stop()
        self.current_idx = (self.current_idx - 1) % len(self.tracks)
        self._resolve_art()
        if was: self.play()

    def set_volume(self, v: float):
        self.volume = max(0.0, min(1.0, v))
        self._apply_volume()

    def seek_to(self, idx: int):
        if 0 <= idx < len(self.tracks):
            was = self.playing or self.paused
            self.stop()
            self.current_idx = idx
            self._resolve_art()
            if was: self.play()
            else: self.status_msg = f"Selected: {self._track_name()}"

    def set_ducked(self, ducked: bool):
        self._duck_target = self.DUCK_VOLUME if ducked else 1.0

    def open_folder(self):
        if not TK_OK:
            self.status_msg = "tkinter not available for folder picker"
            return
        def _pick():
            try:
                root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
                folder = filedialog.askdirectory(title="Select Media Folder")
                root.destroy()
                if folder:
                    self.status_msg = f"Scanning {os.path.basename(folder)}…"
                    found = _scan_media([folder])
                    if found:
                        was = self.playing or self.paused
                        self.stop()
                        self.tracks = found; self.current_idx = 0
                        self._resolve_art()
                        self.status_msg = f"Loaded {len(found)} files from {os.path.basename(folder)}"
                        if was: self.play()
                    else:
                        self.status_msg = "No media found in that folder"
            except Exception as e:
                self.status_msg = f"Folder picker error: {e}"
        threading.Thread(target=_pick, daemon=True).start()

    # ── Fuzzy matching helpers ────────────────────────────────────────────────

    @staticmethod
    def _word_set(text: str) -> set:
        import re
        return set(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())

    @staticmethod
    def _edit_distance(a: str, b: str) -> int:
        if a == b: return 0
        if not a: return len(b)
        if not b: return len(a)
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a):
            curr = [i + 1]
            for j, cb in enumerate(b):
                curr.append(min(prev[j] + (0 if ca == cb else 1),
                                curr[j] + 1, prev[j + 1] + 1))
            prev = curr
        return prev[-1]

    @staticmethod
    def _fuzzy_word_score(q_words: set, target_words: set) -> float:
        if not q_words:
            return 0.0
        total = 0.0
        for qw in q_words:
            best = 0.0
            for tw in target_words:
                if qw == tw:
                    best = 1.0; break
                if len(qw) >= 3 and (tw.startswith(qw) or qw.startswith(tw)):
                    best = max(best, 0.8)
                if len(qw) >= 4 and qw in tw:
                    best = max(best, 0.6)
                ed = MediaPlayerMode._edit_distance(qw, tw)
                if ed == 1:
                    best = max(best, 0.85)
                elif ed == 2:
                    if len(qw) <= 6 or ed / max(len(qw), len(tw)) <= 0.30:
                        best = max(best, 0.70)
            total += best
        return total / len(q_words)

    @staticmethod
    def _path_dir_parts(path_str: str) -> list:
        normalised = path_str.replace("\\", "/")
        parts = [p for p in normalised.split("/")[:-1]
                 if p and p not in (".", "..") and len(p) > 1]
        return parts

    def _artist_score(self, query: str, path: str) -> float:
        parts = self._path_dir_parts(path)
        if not parts:
            return 0.0
        q_words = self._word_set(query)
        best = 0.0
        for part in parts:
            s = self._fuzzy_word_score(q_words, self._word_set(part))
            if s > best:
                best = s
        return best

    def _song_score(self, query: str, path: str) -> float:
        fname = os.path.splitext(os.path.basename(path))[0]
        q_words = self._word_set(query)
        f_words = self._word_set(fname)
        return self._fuzzy_word_score(q_words, f_words)

    # ── Public search API ────────────────────────────────────────────────────

    def find_tracks_by_artist(self, artist_query: str,
                               threshold: float = 0.45) -> list:  # PATCHED: was 0.55
        if not artist_query.strip() or not self.tracks:
            return []
        scored = []
        for i, path in enumerate(self.tracks):
            s = self._artist_score(artist_query, path)
            if s >= threshold:
                scored.append((s, i))
        scored.sort(key=lambda x: -x[0])
        return [i for _, i in scored]

    def find_track_by_name(self, song_query: str,
                            threshold: float = 0.55) -> Optional[int]:
        if not song_query.strip() or not self.tracks:
            return None
        best_i, best_s = None, 0.0
        for i, path in enumerate(self.tracks):
            s = self._song_score(song_query, path)
            if s > best_s:
                best_s, best_i = s, i
        return best_i if best_s >= threshold else None

    def find_best_match(self, query: str) -> dict:
        if not self.tracks:
            return {"type": "none", "artist_matches": [],
                    "song_idx": None, "artist_score": 0.0, "song_score": 0.0}

        print(f"[Media] find_best_match query='{query}'")
        print(f"[Media] Total tracks: {len(self.tracks)}")
        for i, p in enumerate(self.tracks[:5]):
            parts = self._path_dir_parts(p)
            print(f"[Media]   track[{i}] parts={parts}  file={os.path.basename(p.replace(chr(92),'/'))}")

        a_scored = []
        for i, path in enumerate(self.tracks):
            s = self._artist_score(query, path)
            a_scored.append((s, i))
        a_scored.sort(key=lambda x: -x[0])
        best_artist_score = a_scored[0][0] if a_scored else 0.0

        # PATCHED: threshold lowered to 0.45 to handle Vosk drift
        ARTIST_THRESH = 0.45
        SONG_THRESH   = 0.65

        artist_matches = [i for s, i in a_scored if s >= ARTIST_THRESH]

        print(f"[Media] Top artist scores for '{query}':")
        for s, i in a_scored[:5]:
            parts = self._path_dir_parts(self.tracks[i])
            print(f"[Media]   score={s:.2f}  parts={parts}")

        best_song_score = 0.0
        best_song_idx   = None
        for i, path in enumerate(self.tracks):
            s = self._song_score(query, path)
            if s > best_song_score:
                best_song_score, best_song_idx = s, i

        print(f"[Media] best_artist={best_artist_score:.2f}  best_song={best_song_score:.2f}")

        if best_artist_score >= ARTIST_THRESH:
            if (best_artist_score >= best_song_score or
                    best_song_score < SONG_THRESH):
                return {"type": "artist",
                        "artist_matches": artist_matches,
                        "song_idx": None,
                        "artist_score": best_artist_score,
                        "song_score": best_song_score}

        if best_song_score >= SONG_THRESH and best_song_idx is not None:
            return {"type": "song",
                    "artist_matches": artist_matches,
                    "song_idx": best_song_idx,
                    "artist_score": best_artist_score,
                    "song_score": best_song_score}

        return {"type": "none", "artist_matches": artist_matches,
                "song_idx": best_song_idx,
                "artist_score": best_artist_score,
                "song_score": best_song_score}

    def play_artist_shuffle(self, artist_query: str,
                             matches: Optional[list] = None) -> str:
        if matches is None:
            matches = self.find_tracks_by_artist(artist_query)
        if not matches:
            return f"No tracks found for {artist_query}"
        random.shuffle(matches)
        other = [i for i in range(len(self.tracks)) if i not in set(matches)]
        new_order = [self.tracks[i] for i in matches] + [self.tracks[i] for i in other]
        self.tracks = new_order
        self.current_idx = 0
        self.shuffle = True
        self._resolve_art()
        self.play()
        return f"Shuffling {len(matches)} tracks by {artist_query.title()}"

    def play_specific_track(self, song_query: str,
                             track_idx: Optional[int] = None) -> str:
        idx = track_idx if track_idx is not None else self.find_track_by_name(song_query)
        if idx is None:
            return f"Could not find {song_query}"
        self.current_idx = idx
        self._resolve_art()
        self.play()
        name = os.path.splitext(os.path.basename(self.tracks[idx]))[0]
        return f"Playing {name}"

    # ── click / key handling ─────────────────────────────────────────────────

    def handle_click(self, pos) -> bool:
        for label, rect in self._btn_rects:
            if rect.collidepoint(pos):
                self._btn_action(label)
                return True
        dx, dy = pos[0] - self.cx, pos[1] - self.cy
        if dx*dx + dy*dy <= self.r * self.r:
            if self.playing and not self.paused: self.pause()
            elif self.paused: self.resume()
            else: self.play()
            return True
        return False

    def handle_key(self, key: int, mods: int):
        if key == pygame.K_SPACE:
            if self.playing and not self.paused: self.pause()
            elif self.paused: self.resume()
            else: self.play()
        elif key == pygame.K_RIGHT:  self.next_track()
        elif key == pygame.K_LEFT:   self.prev_track()
        elif key == pygame.K_UP:     self.set_volume(self.volume + 0.15)
        elif key == pygame.K_DOWN:   self.set_volume(self.volume - 0.15)
        elif key == pygame.K_s:      self.shuffle = not self.shuffle
        elif key == pygame.K_r:      self.repeat  = not self.repeat

    # ── update / draw ────────────────────────────────────────────────────────

    def _get_music_level(self) -> float:
        if not self.playing or self.paused:
            return 0.0
        avg = sum(self._bars) / max(1, len(self._bars))
        return min(1.0, avg * 1.4)

    def update(self, dt: float, stt_active: bool):
        self._phase    += dt * 3.0
        self._eq_phase += dt * 4.0

        self._duck_factor += (self._duck_target - self._duck_factor) * self.DUCK_SPEED * dt
        self._apply_volume()

        if self.playing and not self.paused and self._video_cap is None:
            self._position = time.time() - self._start_t
            if not pygame.mixer.music.get_busy() and self.playing:
                if self.repeat: self.play()
                else:           self.next_track()

        if self.playing and not self.paused:
            for i in range(self.NUM_INNER_BARS):
                t = (0.3 + 0.4 * abs(math.sin(self._eq_phase * 0.7 + i * 0.45))
                         + 0.3 * abs(math.sin(self._eq_phase * 1.3 + i * 0.29))
                         + 0.15 * abs(math.sin(self._eq_phase * 2.1 + i * 0.61)))
                t = min(1.0, t)
                speed = 16.0 if t > self._bars[i] else 5.0
                self._bars[i] += (t - self._bars[i]) * speed * dt
        else:
            for i in range(self.NUM_INNER_BARS):
                self._bars[i] += (0.0 - self._bars[i]) * 5.0 * dt

        self.music_level = self._get_music_level()

    def draw(self, surface, wave_circle: "WaveformCircle", stt_active: bool,
             stt_level: float, tts_level: float, dt: float):
        cx, cy, r = self.cx, self.cy, self.r

        draw_circle_alpha(surface, (*MEDIA_DARK, 220), (cx, cy), r)

        frame_drawn = False
        if self._video_cap is not None or self._video_frame is not None:
            with self._video_lock:
                vf = self._video_frame
            if vf:
                self._blit_circle_image(surface, vf, cx, cy, r)
                frame_drawn = True
        if not frame_drawn and self._art_surf:
            self._blit_circle_image(surface, self._art_surf, cx, cy, r)

        self._draw_inner_eq(surface, cx, cy, r)
        pygame.draw.circle(surface, MEDIA_ACCENT, (cx, cy), r, 2)
        self._draw_info_arc(surface, cx, cy, r)
        self._draw_controls(surface, cx, cy, r)
        self._draw_progress_arc(surface, cx, cy, r)

        if stt_active and stt_level > 0.005:
            ovl = pygame.Surface((r * 2, r * 2), pygame.SRCALPHA)
            ovl.fill((0, 0, 0, 0))
            pygame.draw.circle(ovl, (*DARK_BG, 160), (r, r), r)
            surface.blit(ovl, (cx - r, cy - r))
            wave_circle.update(max(stt_level, self.music_level * 0.4), tts_level, dt)
            wave_circle.draw(surface, show_cam=False)

    # ── internals ────────────────────────────────────────────────────────────

    def _bg_scan(self):
        tracks = _scan_media()
        self.tracks = tracks
        if tracks:
            self.status_msg = f"Found {len(tracks)} media files"
            self._resolve_art()
        else:
            self.status_msg = "No media found in ~/Music or ~/Videos"

    def _track_name(self) -> str:
        if not self.tracks: return "—"
        return os.path.basename(self.tracks[self.current_idx])

    def _play_audio(self, path: str):
        self._stop_video()
        try:
            pygame.mixer.music.load(path)
            pygame.mixer.music.set_volume(self.volume * self._duck_factor)
            pygame.mixer.music.play()
            self.playing  = True
            self.paused   = False
            self._start_t = time.time()
            self._position = 0.0
            self.status_msg = f"♪ {os.path.basename(path)}"
        except Exception as e:
            self.status_msg = f"Cannot play: {e}"

    def _play_video(self, path: str):
        if not CV2_OK:
            self.status_msg = "cv2 not available — cannot play video"
            return
        self._stop_video()
        try:
            cap = cv2.VideoCapture(path)
            if not cap.isOpened():
                self.status_msg = f"Cannot open: {os.path.basename(path)}"
                return
            self._video_cap      = cap
            self._video_path     = path
            self._video_fps      = cap.get(cv2.CAP_PROP_FPS) or 24.0
            total_frames         = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            self._video_duration = total_frames / max(1, self._video_fps)
            self._video_pos      = 0.0
            self._video_running  = True
            self.playing         = True
            self.paused          = False
            self.status_msg = f"▶ {os.path.basename(path)}"
            try:
                pygame.mixer.music.load(path)
                pygame.mixer.music.set_volume(self.volume * self._duck_factor)
                pygame.mixer.music.play()
            except Exception:
                pass
            self._video_thread = threading.Thread(target=self._video_loop, daemon=True)
            self._video_thread.start()
        except Exception as e:
            self.status_msg = f"Video error: {e}"

    def _video_loop(self):
        if self._video_cap is None: return
        interval = 1.0 / max(1, self._video_fps)
        while self._video_running and self._video_cap is not None:
            t0 = time.time()
            ret, frame = self._video_cap.read()
            if not ret:
                self._video_running = False
                break
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            d = self.r * 2
            h, w = rgb.shape[:2]
            scale = min(d / w, d / h)
            nw, nh = int(w * scale), int(h * scale)
            if nw > 0 and nh > 0:
                rgb = cv2.resize(rgb, (nw, nh))
            surf = _np_to_pg(rgb)
            with self._video_lock:
                self._video_frame = surf
            self._video_pos += interval
            elapsed = time.time() - t0
            sleep = max(0.0, interval - elapsed)
            time.sleep(sleep)
        if not self._video_running:
            if self.repeat: self.play()
            else:           self.next_track()

    def _stop_video(self):
        self._video_running = False
        if self._video_cap:
            try: self._video_cap.release()
            except Exception: pass
            self._video_cap = None
        with self._video_lock:
            self._video_frame = None

    def _apply_volume(self):
        eff = self.volume * self._duck_factor
        try: pygame.mixer.music.set_volume(eff)
        except Exception: pass

    def _resolve_art(self):
        if not self.tracks: return
        folder = os.path.dirname(self.tracks[self.current_idx])
        if folder in self._art_cache:
            self._art_surf = self._art_cache[folder]; return
        candidates = ["cover.jpg","cover.jpeg","cover.png","folder.jpg","folder.jpeg",
                      "folder.png","artwork.jpg","artwork.png","front.jpg","front.png"]
        surf = None
        for name in candidates:
            p = os.path.join(folder, name)
            if os.path.isfile(p):
                surf = self._load_circle_image(p); break
        if surf is None:
            try:
                for f in sorted(os.listdir(folder)):
                    if os.path.splitext(f)[1].lower() in {".jpg",".jpeg",".png",".bmp"}:
                        surf = self._load_circle_image(os.path.join(folder, f))
                        if surf: break
            except Exception:
                pass
        self._art_cache[folder] = surf
        self._art_surf = surf

    def _load_circle_image(self, path: str) -> Optional[pygame.Surface]:
        try:
            return pygame.image.load(path).convert()
        except Exception:
            return None

    def _blit_circle_image(self, surface, img_surf, cx, cy, r):
        d = r * 2
        if d <= 0: return
        iw, ih = img_surf.get_size()
        if iw <= 0 or ih <= 0: return
        if iw != d or ih != d:
            scale = max(d / iw, d / ih)
            nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
            img_surf = pygame.transform.smoothscale(img_surf, (nw, nh))
            sw, sh = img_surf.get_size()
            if sw < d or sh < d:
                padded = pygame.Surface((max(sw, d), max(sh, d)), pygame.SRCALPHA)
                padded.fill((0, 0, 0, 0))
                padded.blit(img_surf, ((max(sw, d) - sw) // 2, (max(sh, d) - sh) // 2))
                img_surf = padded
            ox = max(0, (img_surf.get_width()  - d) // 2)
            oy = max(0, (img_surf.get_height() - d) // 2)
            cw = min(d, img_surf.get_width()  - ox)
            ch = min(d, img_surf.get_height() - oy)
            if cw <= 0 or ch <= 0: return
            try:
                img_surf = img_surf.subsurface(pygame.Rect(ox, oy, cw, ch))
            except ValueError:
                return
            if cw != d or ch != d:
                img_surf = pygame.transform.smoothscale(img_surf, (d, d))

        out = pygame.Surface((d, d), pygame.SRCALPHA)
        out.fill((0, 0, 0, 0))
        img_rgba = img_surf.convert_alpha() if img_surf.get_flags() & pygame.SRCALPHA == 0 else img_surf.copy()
        out.blit(img_rgba, (0, 0))
        mask = pygame.Surface((d, d), pygame.SRCALPHA)
        mask.fill((0, 0, 0, 255))
        pygame.draw.circle(mask, (0, 0, 0, 0), (r, r), r)
        out.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_SUB)
        dark = pygame.Surface((d, d), pygame.SRCALPHA)
        dark.fill((0, 0, 0, 0))
        pygame.draw.circle(dark, (0, 0, 0, 80), (r, r), r)
        out.blit(dark, (0, 0))
        surface.blit(out, (cx - r, cy - r))

    def _draw_inner_eq(self, surface, cx, cy, r):
        n   = self.NUM_INNER_BARS
        dpb = 360.0 / n
        max_len = int(r * 0.35)
        for i in range(n):
            ar = math.radians(i * dpb - 90.0)
            blen = int(self._bars[i] * max_len)
            if blen < 1: continue
            ox = cx + r * math.cos(ar); oy = cy + r * math.sin(ar)
            ix = cx + (r - blen) * math.cos(ar); iy = cy + (r - blen) * math.sin(ar)
            pygame.draw.line(surface,
                             lerp_col(MEDIA_MID, MEDIA_ACCENT, self._bars[i]),
                             (int(ox), int(oy)), (int(ix), int(iy)), 2)

    def _draw_info_arc(self, surface, cx, cy, r):
        y0 = cy + r + 10
        name = self._track_name()
        if len(name) > 44: name = name[:42] + "…"
        ns = self.fonts["md"].render(name, True, TEXT_BRIGHT)
        surface.blit(ns, (cx - ns.get_width()//2, y0))
        if self.playing or self.paused:
            pos_s = f"{int(self._position//60)}:{int(self._position%60):02d}"
            ps = self.fonts["sm"].render(pos_s, True, MEDIA_ACCENT)
            surface.blit(ps, (cx - ps.get_width()//2, y0 + ns.get_height() + 2))
        else:
            ss = self.fonts["sm"].render(self.status_msg[:55], True, TEXT_DIM)
            surface.blit(ss, (cx - ss.get_width()//2, y0 + ns.get_height() + 2))

    def _draw_controls(self, surface, cx, cy, r):
        labels = ["⏮", "⏸" if (self.playing and not self.paused) else "▶", "⏹", "⏭",
                  "🔀", "🔁", "⏏"]
        active_set = set()
        if self.shuffle: active_set.add("🔀")
        if self.repeat:  active_set.add("🔁")

        n    = len(labels)
        bw   = self.CTRL_BTN_W; bh = self.CTRL_BTN_H
        gap  = 6
        total_w = n * bw + (n - 1) * gap
        bx0  = cx - total_w // 2
        by   = cy + r + 46

        self._btn_rects = []
        for i, lbl in enumerate(labels):
            bx = bx0 + i * (bw + gap)
            rect = pygame.Rect(bx, by, bw, bh)
            is_active = lbl in active_set
            bg_col = (*MEDIA_MID, 210) if is_active else (*MEDIA_DARK, 200)
            bdr    = (*MEDIA_ACCENT, 220) if is_active else (*MEDIA_MID, 180)
            s = pygame.Surface((bw, bh), pygame.SRCALPHA); s.fill(bg_col)
            pygame.draw.rect(s, bdr, s.get_rect(), 1, border_radius=self.CTRL_RADIUS)
            surface.blit(s, (bx, by))
            ls = self.fonts["sm"].render(lbl, True, MEDIA_ACCENT if is_active else TEXT_BRIGHT)
            surface.blit(ls, (bx + bw//2 - ls.get_width()//2, by + bh//2 - ls.get_height()//2))
            self._btn_rects.append((lbl, rect))

        vol_y = by + bh + 8
        vol_w = min(total_w, 260)
        vol_x = cx - vol_w // 2
        pygame.draw.rect(surface, MEDIA_DARK, (vol_x, vol_y, vol_w, 4), border_radius=2)
        pygame.draw.rect(surface, MEDIA_ACCENT,
                         (vol_x, vol_y, int(vol_w * self.volume), 4), border_radius=2)
        vl = self.fonts["sm"].render(f"VOL {int(self.volume*100)}%", True, TEXT_BRIGHT)
        surface.blit(vl, (vol_x + vol_w + 8, vol_y - 4))

    def _draw_progress_arc(self, surface, cx, cy, r):
        if self._duration <= 0 and self._video_duration <= 0: return
        total = self._video_duration if self._video_cap else self._duration
        if total <= 0: return
        frac = min(1.0, self._position / total)
        rr = r + 5
        start_a = math.radians(-90)
        end_a   = math.radians(-90 + 360 * frac)
        if frac < 0.01: return
        steps = max(2, int(360 * frac))
        pts   = []
        for s in range(steps + 1):
            a = start_a + (end_a - start_a) * s / max(1, steps)
            pts.append((int(cx + rr * math.cos(a)), int(cy + rr * math.sin(a))))
        if len(pts) >= 2:
            pygame.draw.lines(surface, MEDIA_ACCENT, False, pts, 2)

    def _btn_action(self, label: str):
        if   label == "⏮":  self.prev_track()
        elif label in ("▶", "⏸"):
            if self.playing and not self.paused: self.pause()
            elif self.paused: self.resume()
            else: self.play()
        elif label == "⏹":   self.stop()
        elif label == "⏭":  self.next_track()
        elif label == "🔀": self.shuffle = not self.shuffle
        elif label == "🔁": self.repeat  = not self.repeat
        elif label == "⏏":   self.open_folder()
# ── Prompt Pill ───────────────────────────────────────────────────────────
PROMPT_PILL_VOICE_OPEN  = {"switch prompt","select prompt","load prompt",
                            "choose prompt","change prompt","pick prompt",
                            "open prompt","prompt menu","show prompts"}
PROMPT_PILL_VOICE_CLEAR = {"clear prompt","no prompt","remove prompt",
                            "reset prompt","disable prompt"}
PROMPT_PILL_VOICE_WHAT  = {"what prompt","which prompt","current prompt"}
PROMPT_PILL_LOAD_PFXS   = ["load ","use prompt ","switch to ","activate "]

class PromptPill:
    """
    A pill button rendered in the bottom bar (CONV mode only).
    Opens a dropdown overlay listing all prompts from ~/prompts/.
    """
    PILL_PAD_X  = 10
    PILL_PAD_Y  = 4
    ROW_H       = 26
    HEADER_H    = 22
    FOOTER_H    = 20
    MAX_ROWS    = 10
    DROP_W      = 260
    RADIUS      = 10
    ANIM_SPD    = 12.0

    def __init__(self, fonts: dict, store: PromptStore):
        self.fonts  = fonts
        self.store  = store
        self._open  = False
        self._t     = 0.0          # animation 0→1
        self._hover = -99
        self._pill_rect:    Optional[pygame.Rect] = None
        self._drop_rect:    Optional[pygame.Rect] = None
        self._row_rects:    list = []

    def refresh_fonts(self, nf: dict):
        self.fonts = nf

    def toggle(self):
        self._open = not self._open
        if self._open:
            self.store.reload()

    def close(self):
        self._open = False

    # ── input ─────────────────────────────────────────────────────────────

    def handle_mousedown(self, pos) -> bool:
        """Returns True if the click was consumed."""
        if self._pill_rect and self._pill_rect.collidepoint(pos):
            self.toggle(); return True
        if not self._open or self._drop_rect is None:
            return False
        if not self._drop_rect.collidepoint(pos):
            self.close(); return True     # click outside → close
        for idx, rect in enumerate(self._row_rects):
            if rect.collidepoint(pos):
                # idx 0 = "No prompt", idx 1+ = named prompts
                if idx == 0:
                    self.store.select(None)
                else:
                    name = self.store.names[idx - 1]
                    self.store.select(name)
                self.close()
                return True
        return True

    def handle_mousemove(self, pos):
        self._hover = -99
        for idx, rect in enumerate(self._row_rects):
            if rect.collidepoint(pos):
                self._hover = idx; return

    def handle_mousewheel(self, pos, dy) -> bool:
        if self._drop_rect and self._drop_rect.collidepoint(pos):
            return True
        return False

    # ── update / draw ──────────────────────────────────────────────────────

    def update(self, dt: float):
        target = 1.0 if self._open else 0.0
        self._t = max(0.0, min(1.0,
            self._t + (target - self._t) * self.ANIM_SPD * dt))

    def draw_pill(self, surface, sw: int, sh: int, bar_alpha: float = 1.0, x_left: int = 0):
        """Draw just the pill into the bottom bar. Call from draw_bottom_bar."""
        f      = self.fonts
        active = self.store.active_name is not None
        label  = self.store.active_name or "No prompt"
        if len(label) > 18: label = label[:16] + "…"

        ls  = f["sm_b"].render(label, True,
            MEDIA_ACCENT if active else TEXT_DIM)
        tag_col = (*MEDIA_LITE, 180) if active else (*TEXT_DIM, 180)
        tag = f["sm"].render("▾", True, tag_col)

        pw  = ls.get_width() + tag.get_width() + self.PILL_PAD_X * 2 + 14
        ph  = ls.get_height() + self.PILL_PAD_Y * 2
        by  = sh - BAR_HEIGHT
        px  = x_left + 10       # right-aligned, left of stats
        py  = by + (BAR_HEIGHT - ph) // 2

        pill = pygame.Rect(px, py, pw, ph)
        self._pill_rect = pill

        a   = int(255 * bar_alpha)
        bg  = (*MEDIA_DARK, int(200 * bar_alpha)) if active else (*DARK_BG, int(160 * bar_alpha))
        bdr = (*MEDIA_MID,  int(220 * bar_alpha)) if active else (*BLUE_DARK, int(160 * bar_alpha))
        draw_rounded_rect_alpha(surface, pill, bg,
            border_rgba=bdr, border_w=1, radius=self.RADIUS)

        dot = MEDIA_LITE if active else TEXT_DIM
        pygame.draw.circle(surface, (*dot, a),
            (px + self.PILL_PAD_X, py + ph // 2), 4)

        surface.blit(ls, (px + self.PILL_PAD_X + 10, py + self.PILL_PAD_Y))
        surface.blit(tag, (px + self.PILL_PAD_X + 10 + ls.get_width() + 4,
                           py + self.PILL_PAD_Y))

    def draw_dropdown(self, surface, sw: int, sh: int, bar_alpha: float = 1.0):
        """Draw the animated dropdown. Call from the main draw loop (after bars)."""
        if self._t < 0.005:
            self._drop_rect = None
            self._row_rects = []
            return

        a   = int(255 * self._t * bar_alpha)
        f   = self.fonts
        names = self.store.names
        n_rows = min(len(names) + 1, self.MAX_ROWS)  # +1 for "no prompt"
        drop_h = self.HEADER_H + n_rows * self.ROW_H + self.FOOTER_H

        if self._pill_rect:
            dx = max(10, self._pill_rect.right - self.DROP_W)
        else:
            dx = sw - self.DROP_W - 10
        # animate upward from bar
        dy_full = sh - BAR_HEIGHT - drop_h - 4
        dy = int(dy_full + (drop_h + 4) * (1.0 - self._t))

        drop = pygame.Rect(dx, dy, self.DROP_W, drop_h)
        self._drop_rect = drop

        draw_rounded_rect_alpha(surface, drop,
            (*DARK_BG, int(240 * self._t)),
            border_rgba=(*BLUE_MID, int(200 * self._t)),
            border_w=1, radius=self.RADIUS)

        # header
        hs = f["sm_b"].render("SELECT PROMPT", True, (*TEXT_DIM, a))
        surface.blit(hs, (dx + 12, dy + (self.HEADER_H - hs.get_height()) // 2))
        pygame.draw.line(surface, (*BLUE_DARK, int(180 * self._t)),
            (dx + 6, dy + self.HEADER_H - 1),
            (dx + self.DROP_W - 6, dy + self.HEADER_H - 1), 1)

        self._row_rects = []
        all_rows = [None] + names   # None = "No prompt"

        for idx, name in enumerate(all_rows):
            if idx >= self.MAX_ROWS: break
            ry   = dy + self.HEADER_H + idx * self.ROW_H
            row  = pygame.Rect(dx, ry, self.DROP_W, self.ROW_H)
            self._row_rects.append(row)

            is_active = (name is None and self.store.active_name is None) or \
                        (name == self.store.active_name)
            is_hov    = (self._hover == idx)

            if is_active:
                draw_rounded_rect_alpha(surface, row,
                    (*MEDIA_DARK, int(160 * self._t)), radius=0)
            elif is_hov:
                draw_rounded_rect_alpha(surface, row,
                    (*BLUE_DARK, int(120 * self._t)), radius=0)

            dot_col = MEDIA_LITE if is_active else (BLUE_LITE if is_hov else TEXT_DIM)
            pygame.draw.circle(surface, (*dot_col, a),
                (dx + 16, ry + self.ROW_H // 2), 3)

            label   = "No prompt (clear)" if name is None else \
                      (name if len(name) <= 28 else name[:26] + "…")
            txt_col = MEDIA_ACCENT if is_active else \
                      (TEXT_BRIGHT if is_hov else TEXT_MID_C)
            ls2 = f["sm"].render(label, True, (*txt_col, a))
            surface.blit(ls2, (dx + 28, ry + (self.ROW_H - ls2.get_height()) // 2))

            if is_active:
                tag = f["sm"].render("active", True, (*MEDIA_LITE, a))
                surface.blit(tag, (dx + self.DROP_W - tag.get_width() - 10,
                                   ry + (self.ROW_H - tag.get_height()) // 2))

        # footer hint
        fy = dy + self.HEADER_H + n_rows * self.ROW_H
        pygame.draw.line(surface, (*BLUE_DARK, int(120 * self._t)),
            (dx + 6, fy), (dx + self.DROP_W - 6, fy), 1)
        hint = f["sm"].render("'load <name>'  ·  'clear prompt'",
                               True, (*TEXT_DIM, int(160 * self._t)))
        surface.blit(hint, (dx + 8,
            fy + (self.FOOTER_H - hint.get_height()) // 2))

# ── Model Picker ──────────────────────────────────────────────────────────────
class ModelPicker:
    ROW_H    = 26
    HEADER_H = 22
    FOOTER_H = 18
    MAX_ROWS = 12
    DROP_W   = 300
    RADIUS   = 10
    ANIM_SPD = 12.0

    def __init__(self, fonts: dict):
        self.fonts   = fonts
        self._open   = False
        self._t      = 0.0
        self._hover  = -1
        self._models: list = []
        self._loading       = False
        self._label_rect: Optional[pygame.Rect] = None
        self._drop_rect:  Optional[pygame.Rect] = None
        self._row_rects:  list = []

    def refresh_fonts(self, nf: dict): self.fonts = nf

    def set_label_rect(self, rect: pygame.Rect):
        self._label_rect = rect

    def toggle(self):
        self._open = not self._open
        if self._open: self._fetch_models()

    def close(self): self._open = False

    def _fetch_models(self):
        self._loading = True; self._models = []
        def _go():
            try:
                with urllib.request.urlopen(f"{OLLAMA_BASE}/api/tags", timeout=3) as r:
                    data = json.loads(r.read())
                self._models = [m["name"] for m in data.get("models", [])]
            except Exception:
                self._models = []
            self._loading = False
        threading.Thread(target=_go, daemon=True).start()

    def _load_model(self, name: str, on_done=None):
        def _go():
            try:
                body = json.dumps({"name": name}).encode()
                req = urllib.request.Request(
                    f"{OLLAMA_BASE}/api/pull",
                    data=body, headers={"Content-Type":"application/json"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    for _ in r: pass   # drain stream
            except Exception as e:
                print(f"[ModelPicker] pull error: {e}")
            if on_done: on_done(name)
        threading.Thread(target=_go, daemon=True).start()

    def handle_mousedown(self, pos) -> bool:
        if self._label_rect and self._label_rect.collidepoint(pos):
            self.toggle(); return True
        if not self._open: return False
        if self._drop_rect and not self._drop_rect.collidepoint(pos):
            self.close(); return True
        for idx, rect in enumerate(self._row_rects):
            if rect.collidepoint(pos) and idx < len(self._models):
                return True   # handled by caller via selected_model()
        return False

    def clicked_model(self, pos) -> Optional[str]:
        """Call after handle_mousedown returns True — returns model name if a row was clicked."""
        for idx, rect in enumerate(self._row_rects):
            if rect.collidepoint(pos) and idx < len(self._models):
                name = self._models[idx]
                self.close()
                return name
        return None

    def handle_mousemove(self, pos):
        self._hover = -1
        for idx, rect in enumerate(self._row_rects):
            if rect.collidepoint(pos): self._hover = idx; return

    def update(self, dt: float):
        target = 1.0 if self._open else 0.0
        self._t = max(0.0, min(1.0, self._t + (target - self._t) * self.ANIM_SPD * dt))

    def draw(self, surface, sw: int, sh: int, bar_alpha: float = 1.0):
        if self._t < 0.005:
            self._drop_rect = None; self._row_rects = []; return

        a    = int(255 * self._t * bar_alpha)
        f    = self.fonts
        rows = (["Loading…"] if self._loading else
                self._models if self._models else ["No models found"])
        n_rows   = min(len(rows), self.MAX_ROWS)
        drop_h   = self.HEADER_H + n_rows * self.ROW_H + self.FOOTER_H

        dx = 16  # align with the MODEL label
        dy_full = sh - BAR_HEIGHT - drop_h - 4
        dy = int(dy_full + (drop_h + 4) * (1.0 - self._t))

        drop = pygame.Rect(dx, dy, self.DROP_W, drop_h)
        self._drop_rect = drop

        draw_rounded_rect_alpha(surface, drop,
            (*DARK_BG, int(240 * self._t)),
            border_rgba=(*BLUE_MID, int(200 * self._t)),
            border_w=1, radius=self.RADIUS)

        hs = f["sm_b"].render("SELECT MODEL", True, (*TEXT_DIM, a))
        surface.blit(hs, (dx + 12, dy + (self.HEADER_H - hs.get_height()) // 2))
        pygame.draw.line(surface, (*BLUE_DARK, int(180 * self._t)),
            (dx + 6, dy + self.HEADER_H - 1),
            (dx + self.DROP_W - 6, dy + self.HEADER_H - 1), 1)

        self._row_rects = []
        for idx, name in enumerate(rows[:self.MAX_ROWS]):
            ry  = dy + self.HEADER_H + idx * self.ROW_H
            row = pygame.Rect(dx, ry, self.DROP_W, self.ROW_H)
            self._row_rects.append(row)

            is_hov = (idx == self._hover) and not self._loading
            if is_hov:
                draw_rounded_rect_alpha(surface, row,
                    (*BLUE_DARK, int(120 * self._t)), radius=0)

            txt_col = TEXT_BRIGHT if is_hov else TEXT_MID_C
            if self._loading or not self._models:
                txt_col = TEXT_DIM
            ls = f["sm"].render(name, True, (*txt_col, a))
            surface.blit(ls, (dx + 16, ry + (self.ROW_H - ls.get_height()) // 2))

        fy = dy + self.HEADER_H + n_rows * self.ROW_H
        pygame.draw.line(surface, (*BLUE_DARK, int(120 * self._t)),
            (dx + 6, fy), (dx + self.DROP_W - 6, fy), 1)
        hint = f["sm"].render("click to switch model",
                               True, (*TEXT_DIM, int(160 * self._t)))
        surface.blit(hint, (dx + 8, fy + (self.FOOTER_H - hint.get_height()) // 2))


# ── Waveform circle ────────────────────────────────────────────────────────────

class WaveformCircle:
    NUM_BARS=64; BAR_MIN=2; BAR_MAX=52; BAR_WIDTH=3
    _HALO_LAYERS      = 5
    _HALO_FRINGE_PX   = 12
    _HALO_FRINGE_ALPHA = 70

    def __init__(self, cx, cy, radius):
        self.cx=cx; self.cy=cy; self.base_radius=radius
        self.bar_heights=[0.0]*self.NUM_BARS
        self.tts_radius=0.0
        self._noise=[random.random() for _ in range(self.NUM_BARS)]
        self._phase=0.0; self._cam_surf=None; self.conv_mode=False
        self._tts_smooth=0.0

    def update(self, stt: float, tts: float, dt: float):
        self._phase+=dt*2.0
        for i in range(self.NUM_BARS):
            nv=math.sin(self._phase+self._noise[i]*math.pi*2)
            target=stt*(0.5+0.5*abs(nv)) if stt>0.01 else 0.0
            speed=18.0 if target>self.bar_heights[i] else 4.0
            self.bar_heights[i]=max(0.0,min(1.0,self.bar_heights[i]+(target-self.bar_heights[i])*speed*dt))
        attack=14.0 if tts>self._tts_smooth else 3.5
        self._tts_smooth=max(0.0,min(1.0,self._tts_smooth+(tts-self._tts_smooth)*attack*dt))
        self.tts_radius=self._tts_smooth*30.0

    def set_cam(self, surf): self._cam_surf=surf

    def draw(self, surface, show_cam: bool=True):
        cx,cy,r=self.cx,self.cy,self.base_radius
        lv=self._tts_smooth
        if lv>0.01:
            fa=int(self._HALO_FRINGE_ALPHA*lv)
            for fi in range(3):
                draw_circle_alpha(surface,
                    (*lerp_col(TTS_HALO_OUTER,TTS_HALO_FRINGE,fi/2),int(fa*(0.35+0.65*fi/2))),
                    (cx,cy), r+self._HALO_FRINGE_PX-fi*4)
            draw_circle_alpha(surface,(*DARK_BG,fa),(cx,cy),r-1)
        if lv>0.01:
            inner_r=int(r*(1.0-lv))
            for layer in range(self._HALO_LAYERS):
                frac=layer/max(1,self._HALO_LAYERS-1)
                gap=max(1,(r-inner_r)//(self._HALO_LAYERS+1))
                l_inner=max(0,inner_r-layer*gap)
                l_alpha=int(110*lv*(1.0-frac*0.6))
                l_col=lerp_col(TTS_HALO_OUTER,TTS_HALO_INNER,frac)
                if r<=l_inner or l_alpha<1: continue
                tmp=pygame.Surface((r*2,r*2),pygame.SRCALPHA)
                pygame.draw.circle(tmp,(*l_col,l_alpha),(r,r),r)
                if l_inner>0: pygame.draw.circle(tmp,(0,0,0,0),(r,r),l_inner)
                surface.blit(tmp,(cx-r,cy-r))
        if show_cam and self._cam_surf: surface.blit(self._cam_surf,(cx-r,cy-r))
        else: draw_circle_alpha(surface,(*DARK_BG,CIRCLE_ALPHA),(cx,cy),r)
        bar_col=CONV_LITE if self.conv_mode else WAVEFORM_WHITE
        dpb=360.0/self.NUM_BARS
        for i in range(self.NUM_BARS):
            ar=math.radians(i*dpb-90.0)
            bh=self.BAR_MIN+self.bar_heights[i]*(self.BAR_MAX-self.BAR_MIN)
            x1=cx+r*math.cos(ar); y1=cy+r*math.sin(ar)
            x2=cx+(r+bh)*math.cos(ar); y2=cy+(r+bh)*math.sin(ar)
            pr2=math.radians(i*dpb); hw=self.BAR_WIDTH/2
            dx=hw*math.cos(pr2); dy=hw*math.sin(pr2)
            pygame.draw.polygon(surface,bar_col,
                [(x1-dx,y1-dy),(x1+dx,y1+dy),(x2+dx,y2+dy),(x2-dx,y2-dy)])


# ── Avatar circle ─────────────────────────────────────────────────────────────

def draw_avatar_circle(surface,cx,cy,outer_r,inner_r,avatar_r,
                       outer_col,mid_col,av_col,alpha=255,cam_surf=None):
    draw_circle_alpha(surface,(*outer_col,alpha),(cx,cy),outer_r)
    draw_circle_alpha(surface,(*mid_col,alpha),(cx,cy),inner_r)
    if cam_surf is not None:
        surface.blit(cam_surf,(cx-inner_r,cy-inner_r))
    else:
        draw_circle_alpha(surface,(*av_col,CIRCLE_ALPHA),(cx,cy-int(avatar_r*0.12)),int(avatar_r*0.38))
        bcy=cy+int(avatar_r*0.45); bry=int(avatar_r*0.32); aw=(avatar_r-4)*2
        if aw>0 and bry>0:
            bs=pygame.Surface((aw,bry*2),pygame.SRCALPHA)
            pygame.draw.ellipse(bs,(*av_col,CIRCLE_ALPHA),bs.get_rect())
            surface.blit(bs,(cx-avatar_r+4,bcy-bry),area=pygame.Rect(0,bry,aw,bry))


# ── Login screen ──────────────────────────────────────────────────────────────

class LoginScreen:
    LAYOUT_MARGIN_RATIO=0.21
    def __init__(self,fonts,w,h):
        self.fonts=fonts; self.w,self.h=w,h; self.state="login"; self.flash_t=0.0
        self._cur_outer=BLUE_DARK; self._cur_mid=BLUE_MID; self._cur_av=BLUE_AVATAR
        self.click_input_enabled=True; self.staged=[]; self._hover=None; self.enrol_1=None
        self._layout_r=int(w*self.LAYOUT_MARGIN_RATIO); self._build_rects()

    def _build_rects(self):
        w,h=self.w,self.h; cx,cy=w//2,h//2; lr=self._layout_r
        pairs=[(NATO_WORDS[i],NATO_WORDS[i+1]) for i in range(0,len(NATO_WORDS),2)]
        lh=34; start_y=cy-len(pairs)*lh//2; pr=cx-lr-80
        self._wrd_r={}; self._wrd_l=[]
        for idx,(w1,w2) in enumerate(pairs):
            y=start_y+idx*lh
            s1=self.fonts["md"].render(w1,True,WHITE); s2=self.fonts["md"].render(w2,True,WHITE)
            x1=pr-s1.get_width()-14; x2=pr
            self._wrd_r[w1]=pygame.Rect(x1-6,y-2,s1.get_width()+12,lh-2)
            self._wrd_r[w2]=pygame.Rect(x2-6,y-2,s2.get_width()+12,lh-2)
            self._wrd_l.append((y,w1,w2,x1,x2))
        nw,nh,gap=60,44,12; gx=cx+lr+40; gy=cy-nh-gap//2
        self._num_r={}; self._num_l=[]
        for n in range(10):
            row,col=divmod(n,5); nx=gx+col*nw; ny=gy+row*(nh+gap)
            self._num_r[n]=pygame.Rect(nx-4,ny-1,nw-4,nh-2)
            self._num_l.append((n,nx,ny))

    def set_state(self,s):
        self.state=s; self.flash_t=0.0
        if s in ("login","enrol_1","enrol_2"): self.staged.clear()

    def update(self,dt):
        self.flash_t+=dt
        cm={"login":(BLUE_DARK,BLUE_MID,BLUE_AVATAR),"enrol_1":(ENROL_DARK,ENROL_MID,ENROL_LITE),
            "enrol_2":(ENROL_DARK,ENROL_MID,ENROL_LITE),"enrol_mismatch":(RED_DARK,RED_MID,RED_LITE),
            "success":(GREEN_DARK,GREEN_MID,GREEN_LITE),"error":(RED_DARK,RED_MID,RED_LITE)}
        to,tm,ta=cm.get(self.state,(BLUE_DARK,BLUE_MID,BLUE_AVATAR))
        self._cur_outer=blend_col(self._cur_outer,to,6,dt)
        self._cur_mid  =blend_col(self._cur_mid,  tm,6,dt)
        self._cur_av   =blend_col(self._cur_av,   ta,6,dt)

    def handle_motion(self,pos): self._hover=self._hit(pos) if self.click_input_enabled else None
    def handle_click(self,pos):
        if not self.click_input_enabled: return None
        item=self._hit(pos)
        if item is None: return None
        kind,key=item; self.staged.append(key)
        return {"type":"passphrase_click","kind":kind,"value":key,"sequence":list(self.staged)}
    def pop(self):
        if self.staged: self.staged.pop()
    def clear(self): self.staged.clear()
    def _hit(self,pos):
        for w,r in self._wrd_r.items():
            if r.collidepoint(pos): return ("word",w)
        for n,r in self._num_r.items():
            if r.collidepoint(pos): return ("num",n)
        return None

    def draw(self,surface,status,cam_surf=None):
        surface.fill(BLACK); cx,cy=self.w//2,self.h//2
        if self.state in ("login","enrol_1","enrol_2","enrol_mismatch"):
            self._draw_main(surface,status,cx,cy,cam_surf)
        elif self.state=="success":
            self._draw_result(surface,cx,cy,"ACCESS GRANTED","PASSPHRASE RECOGNIZED",GREEN_LITE,cam_surf)
        else:
            self._draw_result(surface,cx,cy,"!-DENIED-!","PASSPHRASE NOT RECOGNIZED",RED_LITE,cam_surf)

    def _cam_crop(self,cam_surf,inner_r):
        if cam_surf is None or inner_r<=0: return None
        diameter=inner_r*2
        raw=pygame.transform.smoothscale(cam_surf,(diameter,diameter))
        out=pygame.Surface((diameter,diameter),pygame.SRCALPHA)
        pygame.draw.circle(out,(255,255,255,255),(inner_r,inner_r),inner_r)
        out.blit(raw,(0,0),special_flags=pygame.BLEND_RGBA_MIN)
        am=pygame.Surface((diameter,diameter),pygame.SRCALPHA); am.fill((0,0,0,255-CIRCLE_ALPHA))
        out.blit(am,(0,0),special_flags=pygame.BLEND_RGBA_SUB)
        return out

    def _draw_main(self,surface,status,cx,cy,cam_surf):
        f=self.fonts; outer_r=int(self._layout_r*0.88); inner_r=int(outer_r*0.72); avatar_r=int(inner_r*0.85)
        draw_avatar_circle(surface,cx,cy,outer_r,inner_r,avatar_r,
                           self._cur_outer,self._cur_mid,self._cur_av,
                           cam_surf=self._cam_crop(cam_surf,inner_r))
        ly=cy+self._layout_r+14
        prompts={"login":("SPEAK PASSPHRASE",TEXT_MID_C),"enrol_1":("SET PASSPHRASE -- SPEAK NOW",ENROL_LITE),
                 "enrol_2":("REPEAT TO CONFIRM",ENROL_LITE),"enrol_mismatch":("MISMATCH -- START AGAIN",RED_LITE)}
        txt,col=prompts.get(self.state,("SPEAK PASSPHRASE",TEXT_MID_C))
        lbl=f["md"].render(txt,True,col); surface.blit(lbl,(cx-lbl.get_width()//2,ly))
        ny=ly+30
        if not VOSK_OK:
            w=f["sm"].render("[ VOSK NOT INSTALLED ]",True,RED_LITE); surface.blit(w,(cx-w.get_width()//2,ny))
        elif not status.stt_ready:
            w=f["sm"].render("[ LOADING STT... ]",True,ORANGE_LITE); surface.blit(w,(cx-w.get_width()//2,ny))
        if self.click_input_enabled and self.staged:
            ss=f["sm"].render(f"STAGED: {' . '.join(str(v) for v in self.staged)}",True,BLUE_LITE)
            surface.blit(ss,(cx-ss.get_width()//2,ny))
            hs=f["sm"].render("ENTER=submit  BKSP=remove  DEL=clear",True,TEXT_DIM)
            surface.blit(hs,(cx-hs.get_width()//2,ny+22))
        ci=f["sm"].render(f"CLICK INPUT: {'ON' if self.click_input_enabled else 'OFF'}  (C to toggle)",
                          True,(80,180,100) if self.click_input_enabled else TEXT_DIM)
        surface.blit(ci,(cx-ci.get_width()//2,self.h-26))
        for (y,w1,w2,x1,x2) in self._wrd_l:
            self._draw_word(surface,status,w1,x1,y); self._draw_word(surface,status,w2,x2,y)
        for (n,nx,ny2) in self._num_l:
            self._draw_num(surface,status,n,nx,ny2)

    def _draw_word(self,surf,status,word,x,y):
        hl=word in status.highlighted_words; hv=self.click_input_enabled and self._hover==("word",word)
        st=self.click_input_enabled and word in self.staged
        if hl:   col,fnt=TEXT_BOLD,self.fonts["lg_b"]
        elif hv: col,fnt=CLICK_HOVER,self.fonts["md"]
        elif st: col,fnt=(180,220,180),self.fonts["md"]
        else:    col,fnt=TEXT_DIM,self.fonts["md"]
        s=fnt.render(word,True,col)
        if hl or hv or st:
            hs=pygame.Surface((s.get_width()+12,s.get_height()+4),pygame.SRCALPHA)
            hs.fill((255,255,255,40 if hv else 18)); surf.blit(hs,(x-6,y-2))
        surf.blit(s,(x,y))

    def _draw_num(self,surf,status,n,nx,ny):
        hl=n in status.highlighted_nums; hv=self.click_input_enabled and self._hover==("num",n)
        st=self.click_input_enabled and n in self.staged
        if hl:   col,fnt=TEXT_BOLD,self.fonts["lg_b"]
        elif hv: col,fnt=CLICK_HOVER,self.fonts["md"]
        elif st: col,fnt=(180,220,180),self.fonts["md"]
        else:    col,fnt=TEXT_DIM,self.fonts["md"]
        s=fnt.render(str(n),True,col)
        if hl or hv or st:
            hs=pygame.Surface((s.get_width()+8,s.get_height()+4),pygame.SRCALPHA)
            hs.fill((255,255,255,40 if hv else 18)); surf.blit(hs,(nx-4,ny-2))
        surf.blit(s,(nx,ny))

    def _draw_result(self,surface,cx,cy,title,subtitle,col,cam_surf):
        p=0.05*math.sin(self.flash_t*4.0); outr=int(self.w*0.175*(1+p)); innr=int(outr*0.72); avr=int(innr*0.85)
        draw_avatar_circle(surface,cx,cy,outr,innr,avr,
                           self._cur_outer,self._cur_mid,self._cur_av,
                           cam_surf=self._cam_crop(cam_surf,innr))
        ts=self.fonts["xxl"].render(title,True,col); surface.blit(ts,(cx-ts.get_width()//2,cy+outr+16))
        ss=self.fonts["md"].render(subtitle,True,col); surface.blit(ss,(cx-ss.get_width()//2,cy+outr+16+ts.get_height()+6))


# ── Side panels ───────────────────────────────────────────────────────────────

class SidePanel:
    ANIM_SPEED=8.0; RADIUS=10; SLIDER_H=24; SLIDER_PAD=14
    OUTPUT_H=320; OUTPUT_HDR_H=46; OUTPUT_LINE_H=17
    TAB_H=22; TAB_CLR_W=26; TAB_PAD=6
    FONT_PICKER_TOP_PAD=10; FONT_PICKER_BOT_PAD=8

    def __init__(self,side,w,h,fonts,poller=None,stt_ref=None,settings=None,
                 on_settings_change=None,font_list=None,on_font_select=None,
                 chat_history=None,command_log=None):
        self.side=side; self.w,self.h=w,h; self.fonts=fonts
        self.open=False; self._t=0.0
        self._poller=poller; self._stt=stt_ref
        self._tts_ref = None  # set externally after construction
        self._settings=settings or {}
        self._on_settings_change=on_settings_change or (lambda:None)
        self._chat_history=chat_history; self._command_log=command_log
        self._slider_panel_dragging=self._slider_bar_dragging=self._slider_thresh_dragging=False
        self._panel_alpha_val=float(self._settings.get("panel_alpha",0.82))
        self._bar_alpha_val  =float(self._settings.get("bar_alpha",  0.82))
        self._threshold_val  =float(self._settings.get("mic_threshold",0.02))
        self._tab_scroll={"conv":0,"cmds":0}; self._tab_autoscroll={"conv":True,"cmds":True}
        self._active_tab="conv"; self._input_mode=None
        self._info_tab = "status"  # default: "status" | "llm" | "stt" | "tts"
        self._info_tab_rects: dict = {}
        self._output_box_rect=None
        self._tab_conv_rect=self._tab_cmds_rect=None
        self._tab_conv_clr_rect=self._tab_cmds_clr_rect=None
        if side == "left":
            self.title = "Model Info"
            self._static = []  # now handled by tabs
            self._static_llm = [("Context", "4096 tok"), ("Temp", "0.7"), ("Mesh", "on")]
            self._static_stt = [("Engine", "Vosk"), ("Sample Rate", "16000"), ("Model", "vosk-model"), ("Status", "--")]
            self._static_tts = [("Engine", "espeak-ng"), ("Rate", "175 wpm"), ("Volume", "100%"), ("Backend", "--")]
        else:
            self.title="System Settings"
            self._static=[("Node Tier","2"),("NFS","/mnt/mesh"),("Auth","face+voice"),("Version",f"LoomOS {version_no}")]
        self._slider_panel_rect=self._slider_bar_rect=self._slider_thresh_rect=None
        self._slider_panel_track=self._slider_bar_track=self._slider_thresh_track=None
        self._font_dropdown=None
        if side=="right" and font_list is not None:
            self._font_dropdown=FontDropdown(fonts,font_list,self._settings.get("ui_font",""),
                                             on_select=on_font_select or (lambda n:None))

    def refresh_fonts(self,nf):
        self.fonts=nf
        if self._font_dropdown: self._font_dropdown.fonts=nf

    @property
    def panel_alpha(self): return self._panel_alpha_val
    @property
    def bar_alpha(self):   return self._bar_alpha_val
    @property
    def threshold(self):   return self._threshold_val

    def set_open(self,v): self.open=(not self.open) if v is None else v
    def toggle(self):     self.open=not self.open

    def handle_mousedown(self,pos) -> bool:
        if not self.open: return False
        if self.side=="right" and self._font_dropdown:
            if self._font_dropdown.handle_mousedown(pos): return True
        if self.side=="left":
            for key, rect in self._info_tab_rects.items():
                if rect.collidepoint(pos):
                    self._info_tab = key;
                    return True
            if self._tab_conv_clr_rect and self._tab_conv_clr_rect.collidepoint(pos):
                if self._chat_history: self._chat_history.clear()
                self._tab_scroll["conv"]=0; return True
            if self._tab_cmds_clr_rect and self._tab_cmds_clr_rect.collidepoint(pos):
                if self._command_log: self._command_log.clear()
                self._tab_scroll["cmds"]=0; return True
            if self._tab_conv_rect and self._tab_conv_rect.collidepoint(pos):
                self._active_tab="conv"; return True
            if self._tab_cmds_rect and self._tab_cmds_rect.collidepoint(pos):
                self._active_tab="cmds"; return True
        if self.side!="right": return False
        if self._slider_panel_rect and self._slider_panel_rect.collidepoint(pos):
            self._slider_panel_dragging=True; self._update_panel_slider_from_x(pos[0]); return True
        if self._slider_bar_rect and self._slider_bar_rect.collidepoint(pos):
            self._slider_bar_dragging=True; self._update_bar_slider_from_x(pos[0]); return True
        if self._slider_thresh_rect and self._slider_thresh_rect.collidepoint(pos):
            self._slider_thresh_dragging=True; self._update_thresh_slider_from_x(pos[0]); return True
        return False

    def handle_mousemove(self,pos):
        if self._font_dropdown and self.side=="right": self._font_dropdown.handle_mousemove(pos)
        if self._slider_panel_dragging:    self._update_panel_slider_from_x(pos[0])
        elif self._slider_bar_dragging:    self._update_bar_slider_from_x(pos[0])
        elif self._slider_thresh_dragging: self._update_thresh_slider_from_x(pos[0])

    def handle_mouseup(self,pos):
        if self._slider_panel_dragging or self._slider_bar_dragging or self._slider_thresh_dragging:
            self._save_settings()
        self._slider_panel_dragging=self._slider_bar_dragging=self._slider_thresh_dragging=False

    def handle_mousewheel(self,pos,dy) -> bool:
        if self.side=="right" and self._font_dropdown and self.open:
            if self._font_dropdown.handle_mousewheel(pos,dy): return True
        if (self.side=="left" and self.open and self._output_box_rect
                and self._output_box_rect.collidepoint(pos)):
            tab=self._active_tab
            self._tab_scroll[tab]=max(0,self._tab_scroll[tab]-dy*self.OUTPUT_LINE_H*2)
            self._tab_autoscroll[tab]=False; return True
        return False

    def _update_panel_slider_from_x(self,mx):
        tr=self._slider_panel_track or self._slider_panel_rect
        if not tr: return
        t=max(0.0,min(1.0,(mx-tr.x)/max(1,tr.width)))
        self._panel_alpha_val=PANEL_ALPHA_MIN+t*(PANEL_ALPHA_MAX-PANEL_ALPHA_MIN)
        self._settings["panel_alpha"]=self._panel_alpha_val
        self._on_settings_change()
    def _update_bar_slider_from_x(self,mx):
        tr=self._slider_bar_track or self._slider_bar_rect
        if not tr: return
        t=max(0.0,min(1.0,(mx-tr.x)/max(1,tr.width)))
        self._bar_alpha_val=BAR_ALPHA_MIN+t*(BAR_ALPHA_MAX-BAR_ALPHA_MIN)
        self._settings["bar_alpha"]=self._bar_alpha_val
        self._on_settings_change()
    def _update_thresh_slider_from_x(self,mx):
        tr=self._slider_thresh_track or self._slider_thresh_rect
        if not tr: return
        t=max(0.0,min(1.0,(mx-tr.x)/max(1,tr.width)))
        self._threshold_val=MIC_THRESH_MIN+t*(MIC_THRESH_MAX-MIC_THRESH_MIN)
        self._settings["mic_threshold"]=self._threshold_val
        if self._stt: self._stt._open_threshold=self._threshold_val
        self._on_settings_change()
    def _save_settings(self): save_settings(self._settings)

    def update(self,dt):
        t=1.0 if self.open else 0.0
        self._t=max(0.0,min(1.0,self._t+(t-self._t)*self.ANIM_SPEED*dt))
        if not self.open and self._font_dropdown: self._font_dropdown.close()

    def _live(self):
        if not self._poller: return []
        d=self._poller.data
        if self.side=="left":
            sz=f" {d['model_size_gb']:.1f}GB" if d["model_size_gb"]>0 else ""
            return [("Model",f"{d['model_loaded']}{sz}"),("Status",d["model_activity"][:28])]
        bat=f"{d['battery']}%{'z' if d['plugged'] else ''}" if d["battery"]>=0 else "N/A"
        return [("CPU",f"{d['cpu']:.0f}%"),("RAM",f"{d['ram']:.0f}%  {d['ram_used_mb']}/{d['ram_total_mb']}MB"),
                ("GPU",f"{d['gpu']:.0f}%" if d["gpu"]>0 else "N/A"),("Disk",f"{d['disk_pct']:.0f}%"),
                ("Battery",bat),("Net^",f"{d['net_up_kb']:.0f}KB/s"),("Netv",f"{d['net_dn_kb']:.0f}KB/s")]

    def draw(self,surface,panel_alpha=1.0):
        if self._t<0.005: return
        anim=self._t; a_eff=anim*panel_alpha; pw=PANEL_W; ph=self.h-BAR_HEIGHT*2-40
        x=(int(-pw+anim*(pw+20)) if self.side=="left" else int(self.w-anim*(pw+20)))
        x=(max(-pw,min(20,x)) if self.side=="left" else max(self.w-pw-20,x))
        y=BAR_HEIGHT+20
        draw_rounded_rect_alpha(surface,pygame.Rect(x,y,pw,ph),(15,20,30,int(255*a_eff)),
            border_rgba=(*BLUE_MID,int(255*a_eff)),border_w=1,radius=self.RADIUS)
        ts=self.fonts["md_b"].render(self.title,True,BLUE_LITE); surface.blit(ts,(x+16,y+12))
        pygame.draw.line(surface,BLUE_MID,(x+10,y+34),(x+pw-10,y+34),1)
        output_reserve=(self.OUTPUT_H+self.OUTPUT_HDR_H+14) if self.side=="left" else 0
        slider_reserve=(self.SLIDER_H+self.fonts["sm"].get_height()+6+14)*3+20
        fpr=0
        if self.side=="right" and self._font_dropdown:
            dp_x=x+self.SLIDER_PAD; dp_w=pw-self.SLIDER_PAD*2; dp_y=y+44
            self._font_dropdown.layout(dp_x,dp_y,dp_w)
            self._font_dropdown.draw(surface,alpha=a_eff)
            fpr=self.FONT_PICKER_TOP_PAD+self._font_dropdown.total_height+self.FONT_PICKER_BOT_PAD
        iy = y + 44 + fpr + (
            (self._font_dropdown.total_height - (
                self._font_dropdown._header_rect.height if self._font_dropdown._header_rect else 0))
            if (self._font_dropdown and self._font_dropdown.open and self.side == "right") else 0)
        if self.side == "left":
            self._draw_info_tabs(surface, x, y, pw, ph, a_eff)
        else:
            for lbl, val in self._live() + self._static:
                bot = y + ph - (slider_reserve)
                if iy > bot: break
                ls = self.fonts["sm"].render(lbl, True, TEXT_DIM);
                vs = self.fonts["sm"].render(str(val), True, TEXT_BRIGHT)
                surface.blit(ls, (x + 14, iy));
                surface.blit(vs, (x + pw - vs.get_width() - 14, iy));
                iy += 26
        if self.side=="right":   self._draw_sliders(surface,x,y,pw,ph,a_eff)
        elif self.side=="left":  self._draw_output_section(surface,x,y,pw,ph,a_eff,
                                     input_mode=getattr(self,"_input_mode",None))

    def _draw_info_tabs(self, surface, px, py, pw, ph, a):
        ai = int(255 * a)
        f = self.fonts
        output_reserve = self.OUTPUT_H + self.OUTPUT_HDR_H + 14

        # ── tab bar ──────────────────────────────────────────────────────────
        tab_defs = [("status", "SYS"), ("llm", "LLM"), ("stt", "STT"), ("tts", "TTS")]
        tw = (pw - 16) // 4
        tx0 = px + 8
        ty = py + 42
        th = 22
        self._info_tab_rects = {}

        for i, (key, label) in enumerate(tab_defs):
            tr = pygame.Rect(tx0 + i * (tw + 2), ty, tw, th)
            self._info_tab_rects[key] = tr
            active = (self._info_tab == key)
            bg = (*BLUE_MID, int(ai * 0.55)) if active else (*BLUE_DARK, int(ai * 0.30))
            bdr = (*BLUE_LITE, int(ai * 0.90)) if active else (*BLUE_MID, int(ai * 0.40))
            draw_rounded_rect_alpha(surface, tr, bg,
                                    border_rgba=bdr, border_w=1,
                                    radius=(6, 6, 0, 0) if active else 4)
            col = TEXT_BRIGHT if active else TEXT_DIM
            ls = f["sm_b" if active else "sm"].render(label, True, (*col, ai))
            surface.blit(ls, (tr.x + tw // 2 - ls.get_width() // 2,
                              tr.y + th // 2 - ls.get_height() // 2))

        # ── content area ─────────────────────────────────────────────────────
        content_y = ty + th + 4
        content_bot = py + ph - output_reserve - 8
        content_h = content_bot - content_y
        if content_h < 10: return

        # background
        draw_rounded_rect_alpha(surface,
                                pygame.Rect(px + 6, content_y, pw - 12, content_h),
                                (*BLUE_DARK, int(ai * 0.20)), radius=4)

        iy = content_y + 8
        PAD = 14

        def _row(lbl, val, highlight=False):
            nonlocal iy
            if iy + 20 > content_bot: return
            lc = TEXT_DIM
            vc = BLUE_LITE if highlight else TEXT_BRIGHT
            ls = f["sm"].render(lbl, True, (*lc, ai))
            vs = f["sm"].render(str(val), True, (*vc, ai))
            surface.blit(ls, (px + PAD, iy))
            surface.blit(vs, (px + pw - vs.get_width() - PAD, iy))
            iy += 20

        def _divider():
            nonlocal iy
            if iy + 6 > content_bot: return
            pygame.draw.line(surface, (*BLUE_DARK, int(ai * 0.6)),
                             (px + PAD, iy + 2), (px + pw - PAD, iy + 2), 1)
            iy += 8

        if self._info_tab == "status":
            d = self._poller.data if self._poller else {}

            # ── LLM ──────────────────────────────────────────────────────────
            name = d.get("model_loaded", "None")
            gen = d.get("generating", False)
            act = d.get("model_activity", "—")
            ls2 = f["sm_b"].render("LLM", True, (*BLUE_LITE, ai))
            surface.blit(ls2, (px + PAD, iy));
            iy += 18
            _row("Model", name[:22] if name else "None", highlight=(name != "None"))
            _row("Status", "Generating…" if gen else act[:20], highlight=gen)

            _divider()

            # ── STT ──────────────────────────────────────────────────────────
            ready = self._stt.ready if self._stt else False
            err = self._stt.error if self._stt else ""
            muted = self._stt.muted if self._stt else False
            level = round(self._settings.get("mic_threshold", 0.10), 3) if self._settings else "—"
            ls2 = f["sm_b"].render("STT", True, (*BLUE_LITE, ai))
            surface.blit(ls2, (px + PAD, iy));
            iy += 18
            _row("Engine", "Vosk")
            _row("Status",
                 "Muted" if muted else ("Ready" if ready else (err[:16] if err else "Loading…")),
                 highlight=ready and not muted)
            _row("Mic Threshold", f"{level}")

            _divider()

            # ── TTS ──────────────────────────────────────────────────────────
            rate = int(self._settings.get("tts_rate", 175)) if self._settings else 175
            vol = int(float(self._settings.get("tts_volume", 1.0)) * 100) if self._settings else 100
            backend = "SAPI5" if platform.system() == "Windows" else "espeak-ng"
            tts_speaking = False
            if hasattr(self, '_tts_ref') and self._tts_ref:
                tts_speaking = self._tts_ref.speaking
            ls2 = f["sm_b"].render("TTS", True, (*BLUE_LITE, ai))
            surface.blit(ls2, (px + PAD, iy));
            iy += 18
            _row("Engine", backend)
            _row("Rate", f"{rate} wpm")
            _row("Volume", f"{vol}%")
            _row("Status", "Speaking…" if tts_speaking else "Idle",
                 highlight=tts_speaking)

        elif self._info_tab == "llm":
            d = self._poller.data if self._poller else {}
            name = d.get("model_loaded", "None")
            sz = d.get("model_size_gb", 0.0)
            act = d.get("model_activity", "—")
            gen = d.get("generating", False)
            _row("Model", name[:22] if name else "None", highlight=(name != "None"))
            if sz > 0: _row("Size", f"{sz:.1f} GB")
            _row("Status", ("Generating…" if gen else act[:20]), highlight=gen)
            _divider()
            for lbl, val in self._static_llm:
                _row(lbl, val)



        elif self._info_tab == "stt":
            ready = self._stt.ready if self._stt else False
            err = self._stt.error if self._stt else ""
            muted = self._stt.muted if self._stt else False
            _row("Engine", "Vosk")
            _row("Sample Rate", f"{SAMPLE_RATE} Hz")
            _row("Status",
                 "Muted" if muted else ("Ready" if ready else (err[:18] if err else "Loading…")),
                 highlight=ready and not muted)
            _divider()
            _row("Model", "vosk-model")

        elif self._info_tab == "tts":
            if self._poller:
                # pull live rate/vol from settings if available
                rate = int(self._settings.get("tts_rate", 175)) if self._settings else 175
                vol = int(float(self._settings.get("tts_volume", 1.0)) * 100) if self._settings else 100
            else:
                rate, vol = 175, 100
            backend = "SAPI5" if platform.system() == "Windows" else "espeak-ng"
            _row("Engine", backend)
            _row("Rate", f"{rate} wpm")
            _row("Volume", f"{vol}%")
            _divider()
            for lbl, val in self._static_tts[3:]:
                _row(lbl, val)

    def _draw_output_section(self,surface,px,py,pw,ph,a,input_mode=None):
        ai=int(255*a); PAD=10; out_h=self.OUTPUT_H; hdr_h=self.OUTPUT_HDR_H
        tab_h=self.TAB_H; box_y=py+ph-out_h-hdr_h-8; box_x=px+PAD; box_w=pw-PAD*2
        if input_mode is not None:
            forced_tab="conv" if input_mode==InputMode.CONVERSATION else "cmds"
        else: forced_tab=self._active_tab
        self._active_tab=forced_tab
        ds=pygame.Surface((box_w,1),pygame.SRCALPHA)
        for dx in range(0,box_w,6):
            if (dx//3)%2==0: pygame.draw.rect(ds,(*BLUE_MID,int(ai*0.5)),(dx,0,3,1))
        surface.blit(ds,(box_x,box_y-6))
        clr_w=self.TAB_CLR_W; tab=forced_tab
        tdefs={"conv":("CONV",CONV_LITE,CONV_MID,CONV_DARK),"cmds":("CMDS",CMD_TAB_LITE,CMD_TAB_MID,CMD_TAB_DARK)}
        label,lite,mid,dark=tdefs[tab]
        draw_rounded_rect_alpha(surface,pygame.Rect(box_x,box_y,box_w,tab_h),
            (*dark,int(ai*0.7)),border_rgba=(*mid,int(ai*1.0)),border_w=1,radius=(self.RADIUS,self.RADIUS,0,0))
        pygame.draw.line(surface,(*lite,ai),(box_x+2,box_y+tab_h-1),(box_x+box_w-clr_w-4,box_y+tab_h-1),1)
        ls=self.fonts["sm_b"].render(label,True,(*lite,ai))
        surface.blit(ls,(box_x+self.TAB_PAD,box_y+(tab_h-ls.get_height())//2))
        clr_rect=pygame.Rect(box_x+box_w-clr_w,box_y,clr_w,tab_h)
        draw_rounded_rect_alpha(surface,clr_rect,(*RED_MID,int(ai*0.5)),border_rgba=(*RED_LITE,int(ai*0.3)),border_w=1,radius=3)
        xs=self.fonts["sm"].render("x",True,(*RED_LITE,ai))
        surface.blit(xs,(clr_rect.x+(clr_w-xs.get_width())//2,clr_rect.y+(tab_h-xs.get_height())//2))
        if tab=="conv":
            self._tab_conv_rect=pygame.Rect(box_x,box_y,box_w-clr_w,tab_h)
            self._tab_conv_clr_rect=clr_rect; self._tab_cmds_rect=self._tab_cmds_clr_rect=None
        else:
            self._tab_cmds_rect=pygame.Rect(box_x,box_y,box_w-clr_w,tab_h)
            self._tab_cmds_clr_rect=clr_rect; self._tab_conv_rect=self._tab_conv_clr_rect=None
        generating=self._poller.data.get("generating",False) if self._poller else False
        dx2=box_x+box_w-clr_w-14; dy2=box_y+tab_h//2
        if generating:
            pulse=0.5+0.5*math.sin(time.time()*6.0)
            dc=lerp_col(ORANGE_BG,ORANGE_LITE,pulse)
            draw_circle_alpha(surface,(*ORANGE_BG,int(ai*0.25)),(dx2,dy2),6)
            draw_circle_alpha(surface,(*dc,ai),(dx2,dy2),3)
        rect_y=box_y+hdr_h; box_rect=pygame.Rect(box_x,rect_y,box_w,out_h)
        self._output_box_rect=box_rect
        draw_rounded_rect_alpha(surface,box_rect,(6,10,18,int(ai*0.92)),
            border_rgba=(*BLUE_DARK,int(ai*0.8)),border_w=1,radius=(0,0,6,6))
        font=self.fonts["mono_sm"]; ipad=8; mtw=box_w-ipad*2-6
        lines_meta: List[tuple]=[]
        if self._active_tab=="conv":
            if self._chat_history:
                turns=self._chat_history.get_turns()
                if not turns: lines_meta.append(("No conversation yet.",TEXT_DIM))
                else:
                    for turn in turns:
                        role=turn["role"]; text=turn["text"]
                        pc=CHAT_YOU_COL if role=="you" else CHAT_AI_COL
                        pf="YOU: " if role=="you" else "AI:  "
                        wr=_wrap_text(font,text,mtw-font.size(pf)[0])
                        for i,ln in enumerate(wr):
                            lines_meta.append((pf+ln if i==0 else "     "+ln, pc if i==0 else TEXT_MID_C))
                        lines_meta.append(("",TEXT_DIM))
            else:
                act=self._poller.data.get("model_activity","") if self._poller else ""
                for ln in _wrap_text(font,act,mtw): lines_meta.append((ln,TEXT_BRIGHT))
        else:
            if self._command_log:
                entries=self._command_log.get_entries()
                if not entries: lines_meta.append(("No commands yet.",TEXT_DIM))
                else:
                    for entry in entries:
                        tsw=font.size(entry["ts"]+"  ")[0]
                        wr=_wrap_text(font,entry["text"],mtw-tsw)
                        for i,ln in enumerate(wr):
                            lines_meta.append((entry["ts"]+"  "+ln if i==0 else "          "+ln,
                                               CMD_LOG_TEXT_COL if i==0 else TEXT_MID_C))
        lh=self.OUTPUT_LINE_H; total_h=len(lines_meta)*lh
        if generating and self._tab_autoscroll[forced_tab] and forced_tab=="conv":
            self._tab_scroll[forced_tab]=max(0,total_h-out_h+ipad*2)
        max_sc=max(0,total_h-out_h+ipad*2)
        self._tab_scroll[forced_tab]=min(self._tab_scroll[forced_tab],max_sc)
        clip_inner=pygame.Rect(box_x+1,rect_y+1,box_w-2,out_h-2)
        try: clip_surf=surface.subsurface(clip_inner)
        except ValueError: return
        bright_start=max(0,len(lines_meta)-8); text_y=ipad-self._tab_scroll[forced_tab]
        for i,(lt,bc) in enumerate(lines_meta):
            ty=text_y+i*lh
            if ty+lh<0: continue
            if ty>out_h: break
            t_fade=max(0.0,min(1.0,(i-bright_start)/max(1,len(lines_meta)-bright_start)))
            col=lerp_col(CHAT_DIM_COL if forced_tab=="conv" else CMD_TAB_DARK,bc,t_fade)
            clip_surf.blit(font.render(lt,True,(*col,ai)),(ipad,ty))
        if generating and lines_meta and forced_tab=="conv":
            lt,_=lines_meta[-1]; lw=font.size(lt)[0]
            cty=text_y+(len(lines_meta)-1)*lh
            if 0<=cty<out_h-lh and int(time.time()*2)%2==0:
                draw_rounded_rect_alpha(clip_surf,pygame.Rect(ipad+lw+2,cty+3,2,lh-6),(*CONV_LITE,ai),radius=1)
        if total_h>out_h:
            sbw=3; sbx=box_w-sbw-3; trh=out_h-4
            tbh=max(20,int(trh*out_h/total_h)); tby=int((self._tab_scroll[forced_tab]/max(1,max_sc))*(trh-tbh))+2
            pygame.draw.rect(clip_surf,(*BLUE_DARK,int(ai*0.5)),(sbx,2,sbw,trh),border_radius=2)
            pygame.draw.rect(clip_surf,(*BLUE_MID,ai),(sbx,tby,sbw,tbh),border_radius=2)

    def _draw_sliders(self,surface,px,py,pw,ph,a):
        ai=int(255*a); tx=px+self.SLIDER_PAD; tw=pw-self.SLIDER_PAD*2; th=4; tr=7
        lh=self.fonts["sm"].get_height(); rh=lh+6+self.SLIDER_H+14; by=py+ph-rh*3-16
        def _row(lbl,val_t,norm,ry):
            ls=self.fonts["sm"].render(lbl,True,(*TEXT_DIM,ai)); surface.blit(ls,(px+self.SLIDER_PAD,ry))
            vs=self.fonts["sm"].render(val_t,True,(*BLUE_LITE,ai)); surface.blit(vs,(px+pw-vs.get_width()-self.SLIDER_PAD,ry))
            ty2=ry+lh+6; tr2=pygame.Rect(tx,ty2+(self.SLIDER_H-th)//2,tw,th)
            draw_rounded_rect_alpha(surface,tr2,(40,55,80,ai),radius=2)
            fw=max(4,int(tw*norm)); draw_rounded_rect_alpha(surface,pygame.Rect(tr2.x,tr2.y,fw,th),(*BLUE_MID,ai),radius=2)
            tcx=tr2.x+fw; tcy=tr2.centery
            draw_circle_alpha(surface,(*BLUE_LITE,ai),(tcx,tcy),tr)
            draw_circle_alpha(surface,(200,230,255,ai),(tcx,tcy),tr-3)
            hit_rect   = pygame.Rect(tx-tr, ty2, tw+tr*2, self.SLIDER_H)
            track_rect = pygame.Rect(tx,    ty2, tw,      self.SLIDER_H)
            return hit_rect, track_rect
        t1=(self._panel_alpha_val-PANEL_ALPHA_MIN)/max(1e-6,PANEL_ALPHA_MAX-PANEL_ALPHA_MIN)
        self._slider_panel_rect, self._slider_panel_track = _row("PANEL TRANSPARENCY",f"{int(t1*100)}%",t1,by)
        t2=(self._bar_alpha_val-BAR_ALPHA_MIN)/max(1e-6,BAR_ALPHA_MAX-BAR_ALPHA_MIN)
        self._slider_bar_rect,   self._slider_bar_track   = _row("BAR TRANSPARENCY",f"{int(t2*100)}%",t2,by+rh)
        t3=(self._threshold_val-MIC_THRESH_MIN)/max(1e-6,MIC_THRESH_MAX-MIC_THRESH_MIN)
        self._slider_thresh_rect, self._slider_thresh_track = _row("MIC THRESHOLD",f"{self._threshold_val:.3f}",t3,by+rh*2)


# ── Text wrap helper ──────────────────────────────────────────────────────────

def _wrap_text(font, text: str, max_w: int) -> list:
    if not text.strip(): return ["Mic Listening..."]
    lines=[]
    for para in text.split("\n"):
        words=para.split()
        if not words: lines.append(""); continue
        cur=""
        for word in words:
            test=(cur+" "+word).strip()
            if font.size(test)[0]<=max_w: cur=test
            else:
                if cur: lines.append(cur)
                cur=word
        if cur: lines.append(cur)
    return lines if lines else ["Mic Listening..."]


# ── Status bars ───────────────────────────────────────────────────────────────

_mode_pill_rect: Optional[pygame.Rect]=None

def draw_top_bar(surface,fonts,status,w,h,bar_alpha=1.0):
    bba=int(255*bar_alpha); is_conv=status.input_mode==InputMode.CONVERSATION
    bg=CONV_BAR_BG if is_conv else (8,10,18)
    bs=pygame.Surface((w,BAR_HEIGHT),pygame.SRCALPHA); bs.fill((*bg,bba)); surface.blit(bs,(0,0))
    dn=fonts["md"].render(status.device_name,True,TEXT_BRIGHT); surface.blit(dn,(16,(BAR_HEIGHT-dn.get_height())//2))
    ts=fonts["lg"].render(status.time_str,True,TEXT_BRIGHT); surface.blit(ts,(w//2-ts.get_width()//2,2))
    ds=fonts["sm"].render(status.date_str,True,TEXT_BRIGHT); surface.blit(ds,(w//2-ds.get_width()//2,BAR_HEIGHT-ds.get_height()-2))
    mc=GREEN_LITE if status.mesh_connected>0 else TEXT_BRIGHT
    mt=f"Nodes:{status.mesh_connected}" if status.mesh_connected>0 else "Nodes: NONE"
    rl={1:"TIER-1",2:"TIER-2",3:"TIER-3"}.get(status.mesh_role,"")
    ms=fonts["sm"].render(f"{mt} | {rl}" if rl else mt,True,mc)
    surface.blit(ms,(w-ms.get_width()-16,(BAR_HEIGHT-ms.get_height())//2))


def draw_bottom_bar(surface,fonts,status,poller,w,h,bar_alpha=1.0,launcher=None,prompt_pill=None,model_picker=None):
    global _mode_pill_rect
    bba=int(255*bar_alpha); by=h-BAR_HEIGHT
    is_conv=status.input_mode==InputMode.CONVERSATION
    is_media=status.input_mode==InputMode.MEDIA
    bg=CONV_BAR_BG if is_conv else (MEDIA_BAR_BG if is_media else (8,10,18))
    bs=pygame.Surface((w,BAR_HEIGHT),pygame.SRCALPHA); bs.fill((*bg,bba)); surface.blit(bs,(0,by))
    ml = fonts["sm"].render(f"MODEL: {status.model_loaded}", True, TEXT_DIM)
    ml_right = 16 + ml.get_width()
    ml_rect  = pygame.Rect(16, by, ml.get_width() + 4, BAR_HEIGHT)   # ADD
    surface.blit(ml, (16, by + (BAR_HEIGHT - ml.get_height()) // 2))
    if model_picker:                                                    # ADD
        model_picker.set_label_rect(ml_rect)
    surface.blit(ml, (16, by + (BAR_HEIGHT - ml.get_height()) // 2))
    if is_media:
        mode_str="o MEDIA"; mode_fg=MEDIA_ACCENT; mode_bg=(*MEDIA_DARK,200); mode_brd=(*MEDIA_MID,200)
    elif is_conv:
        mode_str="o CONV"; mode_fg=CONV_LITE; mode_bg=(*CONV_DARK,200); mode_brd=(*CONV_MID,200)
    else:
        mode_str="o CMD";  mode_fg=BLUE_LITE; mode_bg=(*BLUE_DARK,200); mode_brd=(*BLUE_MID,180)
    pill_lbl=fonts["sm_b"].render(mode_str,True,mode_fg)
    px2,py2=10,4; pw2=pill_lbl.get_width()+px2*2; ph2=pill_lbl.get_height()+py2*2
    pill_x=w-pw2-220; pill_y=by+(BAR_HEIGHT-ph2)//2
    pill_rect=pygame.Rect(pill_x,pill_y,pw2,ph2); _mode_pill_rect=pill_rect
    draw_rounded_rect_alpha(surface,pill_rect,mode_bg,border_rgba=mode_brd,border_w=1,radius=ph2//2)
    surface.blit(pill_lbl,(pill_x+px2,pill_y+py2))
    if status.tts_level>0.05:
        pulse=0.5+0.5*math.sin(time.time()*8.0)
        tc=lerp_col(BLUE_MID,BLUE_LITE,pulse*status.tts_level)
        tl=fonts["sm_b"].render("* TTS",True,tc)
        tpw=tl.get_width()+px2*2; tph=tl.get_height()+py2*2
        tpx=w-tpw-16; tpy=by+(BAR_HEIGHT-tph)//2
        draw_rounded_rect_alpha(surface,pygame.Rect(tpx,tpy,tpw,tph),
            (*BLUE_DARK,200),border_rgba=(*BLUE_MID,180),border_w=1,radius=tph//2)
        surface.blit(tl,(tpx+px2,tpy+py2))
    if status.stt_muted:
        _draw_mute_pill(surface,fonts,w,h,by)
    else:
        fa=launcher.focused() if launcher else None
        if fa and fa.running:
            app_s=fonts["sm_b"].render(f"APP: {fa.icon} {fa.name}",True,APP_ICON_COL)
            surface.blit(app_s,(w//2-app_s.get_width()//2,by+(BAR_HEIGHT-app_s.get_height())//2))
        else:
            activity=poller.data["model_activity"] if poller else status.model_activity
            activity=truncate_text(fonts["sm"],activity,w//2-40)
            generating=poller.data.get("generating",False) if poller else False
            act_col=CONV_LITE if (generating and is_conv) else (ORANGE_LITE if generating else TEXT_MID_C)
            ma=fonts["sm"].render(activity,True,act_col)
            surface.blit(ma,(w//2-ma.get_width()//2,by+(BAR_HEIGHT-ma.get_height())//2))
    if poller:
        d=poller.data; st=f"CPU {d['cpu']:.0f}%  RAM {d['ram']:.0f}%"
        if d["gpu"]>0: st+=f"  GPU {d['gpu']:.0f}%"
        if d["battery"]>=0: st+=f"  BAT {d['battery']}%"+("z" if d["plugged"] else "")
    else:
        st=f"CPU {status.cpu:.0f}%  RAM {status.ram:.0f}%"
    ss=fonts["sm"].render(st,True,TEXT_BRIGHT)
    surface.blit(ss,(w-ss.get_width()-16,by+(BAR_HEIGHT-ss.get_height())//2))
    if prompt_pill and status.input_mode == InputMode.CONVERSATION:
        prompt_pill.draw_pill(surface, w, h, bar_alpha, x_left=ml_right)


def _draw_mute_pill(surface,fonts,w,h,by):
    label=fonts["sm_b"].render("[MIC MUTED]",True,WHITE)
    px2,py2=14,5; pw2=label.get_width()+px2*2; ph2=label.get_height()+py2*2
    draw_rounded_rect_alpha(surface,pygame.Rect(w//2-pw2//2,by+(BAR_HEIGHT-ph2)//2,pw2,ph2),
                            (*RED_MID,210),border_rgba=(*RED_LITE,180),border_w=1,radius=ph2//2)
    surface.blit(label,(w//2-pw2//2+px2,by+(BAR_HEIGHT-ph2)//2+py2))


# ── IPC helpers ───────────────────────────────────────────────────────────────

def _make_server_socket():
    cfg=IPC_CFG
    if cfg["mode"]=="unix":
        path=cfg["socket_path"]
        if os.path.exists(path): os.unlink(path)
        srv=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); srv.bind(path)
    else:
        srv=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        srv.bind((cfg["host"],cfg["port"]))
    return srv

def _cleanup_server_socket():
    cfg=IPC_CFG
    if cfg["mode"]=="unix" and os.path.exists(cfg["socket_path"]):
        os.unlink(cfg["socket_path"])

def send_msg(msg: dict):
    cfg=IPC_CFG
    try:
        s=(socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) if cfg["mode"]=="unix"
           else socket.socket(socket.AF_INET,socket.SOCK_STREAM))
        if cfg["mode"]=="unix": s.connect(cfg["socket_path"])
        else: s.connect((cfg["host"],cfg["port"]))
        s.sendall((json.dumps(msg)+"\n").encode()); s.close()
    except Exception as e: print(f"IPC: {e}")


# ── App system ────────────────────────────────────────────────────────────────

@dataclass
class AppEntry:
    path:str; manifest:dict
    process:Optional[subprocess.Popen]=field(default=None,repr=False)
    ipc_port:int=0
    @property
    def name(self): return self.manifest.get("name",Path(self.path).stem)
    @property
    def description(self): return self.manifest.get("description","")
    @property
    def icon(self): return self.manifest.get("icon","[]")
    @property
    def keywords(self): return [k.lower() for k in self.manifest.get("keywords",[])]
    @property
    def running(self): return self.process is not None and self.process.poll() is None
    @property
    def singleton(self): return self.manifest.get("singleton",True)
    @property
    def silent_commands(self): return self.manifest.get("silent_commands",False)


class _FakePopen:
    def __init__(self,pid): self.pid=pid
    def poll(self):
        try: os.kill(self.pid,0); return None
        except OSError: return 1


def _read_manifest(path: Path) -> Optional[dict]:
    try:
        lines=[]
        with open(path,"r",errors="replace") as f:
            for i,line in enumerate(f):
                lines.append(line)
                if i>80: break
        import ast,re
        m=re.search(r'LOOMOS_APP\s*=\s*(\{.*?\})',"".join(lines),re.DOTALL)
        if m: return ast.literal_eval(m.group(1))
    except Exception as e: print(f"[AppLauncher] Manifest error in {path.name}: {e}")
    return None


class AppLauncher:
    def __init__(self):
        self._apps:Dict[str,AppEntry]={}; self._focused_name:Optional[str]=None
        self._lock=threading.Lock()
        APPS_DIR.mkdir(parents=True,exist_ok=True)
        sdk_dest=APPS_DIR/"loomos_app_sdk.py"
        if not sdk_dest.exists() and APP_SDK_PATH.exists():
            import shutil; shutil.copy(APP_SDK_PATH,sdk_dest); print(f"[AppLauncher] Installed SDK -> {sdk_dest}")

    def scan(self) -> List[AppEntry]:
        found={}
        for py in sorted(APPS_DIR.glob("*.py")):
            if py.name.startswith("_") or py.name=="loomos_app_sdk.py": continue
            mf=_read_manifest(py)
            if mf is None: continue
            nk=mf.get("name",py.stem).lower()
            with self._lock: ex=self._apps.get(nk)
            found[nk]=AppEntry(path=str(py),manifest=mf,process=ex.process if ex else None,ipc_port=mf.get("ipc_port",0))
        with self._lock: self._apps=found
        print(f"[AppLauncher] Scanned {APPS_DIR}: {len(found)} apps")
        return list(found.values())

    def apps(self):
        with self._lock: return list(self._apps.values())
    def get(self,nk):
        with self._lock: return self._apps.get(nk)

    def match(self,utterance) -> Optional[AppEntry]:
        low=utterance.lower(); best=None; bs=0
        with self._lock: entries=list(self._apps.values())
        for e in entries:
            for kw in e.keywords:
                if kw in low and len(kw)>bs: bs=len(kw); best=e
        return best

    def launch(self,entry) -> str:
        if entry.singleton and entry.running: return f"{entry.name} is already running"
        try:
            env=os.environ.copy(); env["PYTHONPATH"]=str(APPS_DIR)+os.pathsep+env.get("PYTHONPATH","")
            proc=subprocess.Popen([sys.executable,entry.path],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            entry.process=proc
            nk=entry.name.lower()
            with self._lock:
                if nk in self._apps: self._apps[nk].process=proc
            print(f"[AppLauncher] Launched {entry.name} (pid {proc.pid})")
            return f"Launched {entry.name}"
        except Exception as e: return f"Failed to launch {entry.name}: {e}"

    def close(self,entry) -> str:
        if not entry.running: return f"{entry.name} is not running"
        self._send_to_app(entry,{"type":"quit"})
        try: entry.process.wait(timeout=2.0)
        except subprocess.TimeoutExpired: entry.process.terminate()
        return f"Closed {entry.name}"

    def close_all(self):
        with self._lock: entries=list(self._apps.values())
        for e in entries:
            if e.running: self.close(e)

    def focus(self,entry):
        if self._focused_name==entry.name.lower(): return
        prev=self._focused_name
        if prev:
            with self._lock: pe=self._apps.get(prev)
            if pe: self._send_to_app(pe,{"type":"blur"})
        self._focused_name=entry.name.lower(); self._send_to_app(entry,{"type":"focus"})

    def focused(self) -> Optional[AppEntry]:
        with self._lock: return self._apps.get(self._focused_name) if self._focused_name else None

    def blur_all(self): self._focused_name=None

    def forward_command(self,text) -> tuple:
        e=self.focused()
        if e and e.running and e.ipc_port:
            return self._send_to_app(e,{"type":"voice_command","text":text}), e.silent_commands
        return False, False

    def forward_key(self,key,uc,mods) -> bool:
        e=self.focused()
        if e and e.running and e.ipc_port and e.manifest.get("accepts_keys",False):
            return self._send_to_app(e,{"type":"key_input","key":key,"unicode":uc,"mods":mods})
        return False

    def forward_dictation(self,text) -> bool:
        e=self.focused()
        if e and e.running and e.ipc_port and e.manifest.get("accepts_dictation",False):
            return self._send_to_app(e,{"type":"dictation_text","text":text})
        return False

    def _send_to_app(self,entry,msg) -> bool:
        if not entry.ipc_port: return False
        try:
            s=socket.socket(socket.AF_INET,socket.SOCK_STREAM); s.settimeout(0.5)
            s.connect(("127.0.0.1",entry.ipc_port)); s.sendall((json.dumps(msg)+"\n").encode()); s.close(); return True
        except Exception: return False

    def register_from_ipc(self,manifest,pid):
        nk=manifest.get("name","").lower()
        with self._lock:
            if nk in self._apps:
                e=self._apps[nk]; e.ipc_port=manifest.get("ipc_port",e.ipc_port)
                if e.process is None or e.process.poll() is not None: e.process=_FakePopen(pid)
            else:
                self._apps[nk]=AppEntry(path="",manifest=manifest,ipc_port=manifest.get("ipc_port",0),process=_FakePopen(pid))
        print(f"[AppLauncher] Registered: {manifest.get('name')} (pid {pid})")

    def send_window_command(self, entry, action: str) -> str:
        if not entry.running:
            return f"{entry.name} is not running"
        sent = self._send_to_app(entry, {"type": "window", "action": action})
        if sent:
            return f"{action} {entry.name}"
        return self._os_window_command(entry, action)

    def _os_window_command(self, entry, action: str) -> str:
        pid = entry.process.pid if entry.process else None
        if pid is None:
            return f"Cannot find window for {entry.name}"
        try:
            if platform.system() == "Windows":
                import ctypes, ctypes.wintypes
                EnumWindows = ctypes.windll.user32.EnumWindows
                EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool,
                                                     ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
                GetWindowThreadProcessId = ctypes.windll.user32.GetWindowThreadProcessId
                IsWindowVisible = ctypes.windll.user32.IsWindowVisible
                ShowWindow = ctypes.windll.user32.ShowWindow
                SW = {"minimise": 6, "maximize": 3, "maximise": 3,
                      "restore": 9, "close": 16}
                sw_cmd = SW.get(action, 9)
                hwnds = []

                def _cb(hwnd, _):
                    pid2 = ctypes.wintypes.DWORD()
                    GetWindowThreadProcessId(hwnd, ctypes.byref(pid2))
                    if pid2.value == pid and IsWindowVisible(hwnd):
                        hwnds.append(hwnd)
                    return True

                EnumWindows(EnumWindowsProc(_cb), 0)
                if not hwnds:
                    return f"No visible window found for {entry.name}"
                for hwnd in hwnds:
                    ShowWindow(hwnd, sw_cmd)
                return f"{action} {entry.name}"
            else:
                result = subprocess.run(
                    ["wmctrl", "-l", "-p"], capture_output=True, text=True)
                for line in result.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 3 and parts[2] == str(pid):
                        hwnd = parts[0]
                        if action in ("minimise", "minimize"):
                            subprocess.run(["xdotool", "windowminimize", hwnd])
                        elif action in ("maximise", "maximize"):
                            subprocess.run(["wmctrl", "-i", "-r", hwnd, "-b", "add,maximized_vert,maximized_horz"])
                        elif action == "restore":
                            subprocess.run(["wmctrl", "-i", "-r", hwnd, "-b", "remove,maximized_vert,maximized_horz"])
                        elif action == "close":
                            subprocess.run(["wmctrl", "-i", "-c", hwnd])
                        return f"{action} {entry.name}"
                return f"No window found for {entry.name}"
        except Exception as e:
            return f"Window command failed: {e}"


class AppDrawer:
    COLS=5; TILE_W=140; TILE_H=110; TILE_PAD=16; FADE_SPD=10.0; RADIUS=12

    def __init__(self,sw,sh,fonts,launcher,on_launch=None):
        self.sw=sw; self.sh=sh; self.fonts=fonts; self.launcher=launcher
        self.on_launch=on_launch or (lambda e:None)
        self.visible=False; self._alpha=0.0; self._hover=-1
        self._tiles:List[pygame.Rect]=[]; self._entries:List[AppEntry]=[]
        tw=self.COLS*(self.TILE_W+self.TILE_PAD)-self.TILE_PAD
        self._grid_x=(sw-tw)//2; self._grid_y=80

    def open(self): self._entries=self.launcher.scan(); self.visible=True
    def close(self): self.visible=False
    def toggle(self):
        if self.visible: self.close()
        else: self.open()

    def handle_key(self,key) -> bool:
        if not self.visible: return False
        if key==pygame.K_ESCAPE: self.close(); return True
        return False

    def handle_click(self,pos) -> bool:
        if not self.visible: return False
        for i,rect in enumerate(self._tiles):
            if rect.collidepoint(pos) and i<len(self._entries):
                self.on_launch(self._entries[i]); self.close(); return True
        self.close(); return True

    def handle_mousemove(self,pos):
        self._hover=-1
        for i,rect in enumerate(self._tiles):
            if rect.collidepoint(pos): self._hover=i; break

    def update(self,dt):
        t=1.0 if self.visible else 0.0
        self._alpha=max(0.0,min(1.0,self._alpha+(t-self._alpha)*self.FADE_SPD*dt))

    def draw(self,surface):
        if self._alpha<0.01: return
        a=int(self._alpha*255)
        ov=pygame.Surface((self.sw,self.sh),pygame.SRCALPHA); ov.fill((*APP_DRAWER_BG,int(a*0.94))); surface.blit(ov,(0,0))
        ts=self.fonts["xl_b"].render("APP LAUNCHER",True,(*APP_NAME_COL,a))
        surface.blit(ts,(self.sw//2-ts.get_width()//2,BAR_HEIGHT+14))
        hs=self.fonts["sm"].render("CLICK to launch  *  F3 or ESC to close  *  'open <app>' via voice",True,(*APP_DESC_COL,int(a*0.8)))
        surface.blit(hs,(self.sw//2-hs.get_width()//2,BAR_HEIGHT+14+ts.get_height()+4))
        if not self._entries:
            ms=self.fonts["md"].render(f"No apps found in {APPS_DIR}",True,(*TEXT_DIM,a))
            surface.blit(ms,(self.sw//2-ms.get_width()//2,self.sh//2-ms.get_height()//2)); return
        self._tiles=[]
        rows=(len(self._entries)+self.COLS-1)//self.COLS
        for row in range(rows):
            for col in range(self.COLS):
                idx=row*self.COLS+col
                if idx>=len(self._entries): break
                self._tiles.append(pygame.Rect(self._grid_x+col*(self.TILE_W+self.TILE_PAD),
                                               self._grid_y+60+row*(self.TILE_H+self.TILE_PAD),
                                               self.TILE_W,self.TILE_H))
        for i,(rect,entry) in enumerate(zip(self._tiles,self._entries)):
            ih=i==self._hover; ir=entry.running
            draw_rounded_rect_alpha(surface,rect,(*APP_TILE_HOV,int(a*0.9)) if ih else (*APP_TILE_BG,int(a*0.85)),
                border_rgba=(*APP_TILE_RUN_BDR,int(a*0.9)) if ir else (*APP_TILE_BDR,int(a*0.7)),border_w=2 if ir else 1,radius=self.RADIUS)
            ic=self.fonts["xxl"].render(entry.icon,True,(*APP_ICON_COL,a))
            surface.blit(ic,(rect.centerx-ic.get_width()//2,rect.y+10))
            ns=self.fonts["sm_b"].render(entry.name,True,(*APP_NAME_COL,a))
            surface.blit(ns,(rect.centerx-ns.get_width()//2,rect.y+62))
            desc=entry.description; mdw=self.TILE_W-8
            if self.fonts["sm"].size(desc)[0]>mdw:
                while desc and self.fonts["sm"].size(desc+"...")[0]>mdw: desc=desc[:-1]
                desc+="..."
            ds=self.fonts["sm"].render(desc,True,(*APP_DESC_COL,int(a*0.8)))
            surface.blit(ds,(rect.centerx-ds.get_width()//2,rect.y+82))
            if ir:
                draw_circle_alpha(surface,(*APP_RUN_DOT,int(a*0.3)),(rect.right-10,rect.y+10),7)
                draw_circle_alpha(surface,(*APP_RUN_DOT,a),(rect.right-10,rect.y+10),4)


# ── Main application ──────────────────────────────────────────────────────────

class LoomOSGui:
    TARGET_FPS=60; FLASH_HOLD_OK=1.8; FLASH_HOLD_ERR=1.2; FLASH_HOLD_MISMATCH=1.5
    _TTS_SENTENCE_ENDS={'.','!','?','\n'}; _TTS_MIN_CHARS=80

    def __init__(self,wallpaper_path=None):
        pygame.init(); pygame.display.set_caption("LoomOS Speech Centre")
        info=pygame.display.Info(); self.W=info.current_w; self.H=info.current_h
        self.screen=pygame.display.set_mode((self.W,self.H),pygame.FULLSCREEN|pygame.NOFRAME)
        self.clock=pygame.time.Clock(); self.running=True
        self._settings=load_settings()
        self.fonts=load_fonts(self._settings.get("ui_font",""))
        self._system_fonts=get_system_fonts(); print(f"[Fonts] {len(self._system_fonts)} found")
        self._wallpaper_path=wallpaper_path or self._load_saved_wallpaper()
        self.wallpaper=self._load_wallpaper(self._wallpaper_path)
        self._poller=SystemPoller() if PSUTIL_OK else None
        self._input_mode=InputMode.COMMAND
        self._chat=ChatHistory(); self._cmd_log=CommandLog()
        self._current_ai_idx:Optional[int]=None
        self._tts_buffer=""; self._tts_auto_muted=False
        if self._poller: self._poller.on_token=self._on_llm_token; self._poller.start()
        if passphrase_enrolled(): self.ui_state=UIState.LOGIN; init_ls="login"
        else:                     self.ui_state=UIState.ENROL_1; init_ls="enrol_1"
        self.status=SystemStatus()
        self.status.time_str=time.strftime("%H:%M"); self.status.date_str=time.strftime("%A %d %B %Y").upper()
        self.status.input_mode=self._input_mode
        self.login=LoginScreen(self.fonts,self.W,self.H); self.login.set_state(init_ls)
        _wave_r=int(self.W*0.13); _wave_cy=self.H//2
        self.wave=WaveformCircle(self.W//2,_wave_cy,_wave_r)
        self._stt=VoskSTT(on_partial=self._stt_partial,on_final=self._stt_final,on_level=self._stt_level)
        self._stt._open_threshold=self._settings["mic_threshold"]
        self._tts=TTSEngine(on_level=self._tts_level_cb,on_start=self._tts_start_cb,on_end=self._tts_end_cb)
        self._tts.set_rate(int(self._settings.get("tts_rate",175)))
        self._tts.set_volume(float(self._settings.get("tts_volume",1.0)))
        self._tts.start()
        self.panel_l=SidePanel("left",self.W,self.H,self.fonts,self._poller,stt_ref=self._stt,settings=self._settings,
                                on_settings_change=self._on_settings_change,chat_history=self._chat,command_log=self._cmd_log)
        self.panel_l._input_mode=self._input_mode
        self.panel_l._active_tab="conv" if self._input_mode==InputMode.CONVERSATION else "cmds"
        self.panel_l._tts_ref = self._tts
        self.panel_r=SidePanel("right",self.W,self.H,self.fonts,self._poller,stt_ref=self._stt,
                                settings=self._settings,on_settings_change=self._on_settings_change,
                                font_list=self._system_fonts,on_font_select=self._on_font_select)
        self.dictation=DictationPopup(self.W,self.H,self.fonts,on_submit=self._on_dictation_submit,
                                      circle_cy=_wave_cy,circle_r=_wave_r)
        self.wallpaper_browser=WallpaperBrowser(self.W,self.H,self.fonts,on_select=self._on_wallpaper_select)
        self._launcher=AppLauncher(); self._launcher.scan()
        self._app_drawer=AppDrawer(self.W,self.H,self.fonts,self._launcher,on_launch=self._launch_app)
        self.help_panel=HelpPanel(self.W,self.H,self.fonts)
        self._media=MediaPlayerMode(self.W//2, _wave_cy, _wave_r, self.fonts)
        self._media_stt_active=False

        self._flash_t=0.0; self._cam_surf=None
        self._webcam=WebcamFeed(); self._webcam.start()
        self._stt.start()
        self._prompt_store = PromptStore()
        self._prompt_pill = PromptPill(self.fonts, self._prompt_store)
        self._model_picker = ModelPicker(self.fonts)
        threading.Thread(target=self._ipc_server,daemon=True).start()

    # ── TTS callbacks ─────────────────────────────────────────────────────────
    def _tts_level_cb(self,level): self.status.tts_level=level
    def _tts_start_cb(self):
        print("[TTS] Speaking...")
        if not self._stt.muted: self._stt.muted=True; self._tts_auto_muted=True
        else: self._tts_auto_muted=False
    def _tts_end_cb(self):
        print("[TTS] Done.")
        if self._tts_auto_muted: self._stt.muted=False; self._tts_auto_muted=False
    def _flush_tts(self):
        t=self._tts_buffer.strip(); self._tts_buffer=""
        if t: self._tts.speak(t)

    # ── App helpers ───────────────────────────────────────────────────────────
    def _launch_app(self,entry):
        result=self._launcher.launch(entry); self._log_cmd(result)
        def _fl():
            time.sleep(0.5)
            if entry.running: self._launcher.focus(entry)
        threading.Thread(target=_fl,daemon=True).start()

    def _log_cmd(self,text):
        self._cmd_log.add(text)
        if self.panel_l.open: self.panel_l._active_tab="cmds"; self.panel_l._tab_autoscroll["cmds"]=True

    def _ack(self):
        self._tts.speak_immediate(random.choice(ACK_PHRASES))

    def _set_mode(self,mode):
        if self._input_mode==mode: return
        self._input_mode=mode; self.status.input_mode=mode
        self.wave.conv_mode=self.dictation.conv_mode=(mode==InputMode.CONVERSATION)
        self.panel_l._input_mode=mode
        self.panel_l._active_tab="conv" if mode==InputMode.CONVERSATION else "cmds"
        self._log_cmd(f"mode -> {mode.value}"); print(f"[Mode] -> {mode.value}")

    def _toggle_mode(self):
        cycle={InputMode.COMMAND:InputMode.CONVERSATION,
               InputMode.CONVERSATION:InputMode.MEDIA,
               InputMode.MEDIA:InputMode.COMMAND}
        self._set_mode(cycle.get(self._input_mode, InputMode.COMMAND))

    def _on_llm_token(self,token):
        if self._current_ai_idx is not None:
            self._chat.append_ai_token(self._current_ai_idx,token)
            self.panel_l._tab_autoscroll["conv"]=True
        self._tts_buffer+=token
        lc=self._tts_buffer[-1] if self._tts_buffer else ""
        if lc in self._TTS_SENTENCE_ENDS or len(self._tts_buffer)>=self._TTS_MIN_CHARS:
            self._flush_tts()

    def _on_font_select(self,font_name):
        print(f"[Font] -> '{font_name}'"); self._settings["ui_font"]=font_name; save_settings(self._settings)
        nf=load_fonts(font_name); self.fonts=nf
        self.login.fonts=nf; self.dictation.fonts=nf; self.wallpaper_browser.fonts=nf
        self.panel_l.refresh_fonts(nf); self.panel_r.refresh_fonts(nf)
        self.help_panel.refresh_fonts(nf)
        self._media.refresh_fonts(nf)
        self._prompt_pill.refresh_fonts(nf)
        self._model_picker.refresh_fonts(nf)
        self.login._build_rects()

    def _on_settings_change(self): self._stt._open_threshold=self._settings.get("mic_threshold",0.02)
    def _current_panel_alpha(self): return self._settings.get("panel_alpha",0.82)
    def _current_bar_alpha(self):   return self._settings.get("bar_alpha",0.82)

    def _load_saved_wallpaper(self):
        if os.path.exists(WALLPAPER_FILE):
            try:
                p=open(WALLPAPER_FILE).read().strip()
                if os.path.isfile(p): return p
            except Exception: pass
        return None

    def _load_wallpaper(self,path):
        if path and os.path.exists(path):
            try: img=pygame.image.load(path).convert(); return pygame.transform.scale(img,(self.W,self.H))
            except Exception as e: print(f"Wallpaper load error: {e}")
        return None

    def _on_wallpaper_select(self,path):
        self.wallpaper=self._load_wallpaper(path); self._wallpaper_path=path
        try:
            with open(WALLPAPER_FILE,"w") as f: f.write(path)
        except Exception as e: print(f"[Wallpaper] Save error: {e}")

    def _toggle_mute(self):
        self._stt.muted=not self._stt.muted; self.status.stt_muted=self._stt.muted
        self._log_cmd("mic muted" if self._stt.muted else "mic unmuted")
        if self._stt.muted and self.dictation.visible: self.dictation.dismiss()

    def _set_mute(self,muted):
        if self._stt.muted!=muted: self._toggle_mute()

    def _switch_model(self, name: str):
        if not self._poller: return
        self._poller.data["model_loaded"] = name
        self._poller.data["model_activity"] = f"Loading {name}…"
        self._log_cmd(f"model -> {name}")

        def _pull():
            try:
                body = json.dumps({"name": name}).encode()
                req = urllib.request.Request(
                    f"{OLLAMA_BASE}/api/pull", data=body,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    for _ in r: pass
                self._poller.data["model_activity"] = "Ready"
                print(f"[Model] Switched to {name}")
            except Exception as e:
                self._poller.data["model_activity"] = f"Error: {e}"
                print(f"[Model] Switch error: {e}")

        threading.Thread(target=_pull, daemon=True).start()

    def _do_logout(self):
        self._log_cmd("logout"); self._launcher.close_all()
        if self.dictation.visible: self.dictation.dismiss()
        self.panel_l.set_open(False); self.panel_r.set_open(False)
        self.help_panel.set_open(False)
        self.ui_state=UIState.LOGIN; self.login.set_state("login"); self.login.clear()

    def _do_success(self): self.ui_state=UIState.LOGIN_SUCCESS; self.login.set_state("success"); self._flash_t=0.0; self._tts.speak("Access granted")
    def _do_error(self):   self.ui_state=UIState.LOGIN_ERROR;   self.login.set_state("error");   self._flash_t=0.0; self._tts.speak("Pass phrase not recognized")

    def _on_dictation_submit(self, text):
        print(f"[Dictation] Submit: '{text}'")
        low = text.lower().strip()

        # ── prompt voice commands (CONV mode only) ────────────────────────────
        if self.status.input_mode == InputMode.CONVERSATION:
            if low in PROMPT_PILL_VOICE_OPEN:
                self._prompt_pill.toggle()
                self._log_cmd("prompt selector opened")
                self._ack();
                return
            if low in PROMPT_PILL_VOICE_CLEAR:
                self._prompt_store.select(None)
                self._tts.speak("Prompt cleared")
                self._log_cmd("prompt cleared");
                return
            if low in PROMPT_PILL_VOICE_WHAT:
                n = self._prompt_store.active_name
                self._tts.speak(f"Active prompt: {n}" if n else "No prompt active")
                return
            for pfx in PROMPT_PILL_LOAD_PFXS:
                if low.startswith(pfx):
                    query = text[len(pfx):].strip()
                    name = self._prompt_store.fuzzy_find(query)
                    if name:
                        self._prompt_store.select(name)
                        self._tts.speak(f"Loaded prompt: {name}")
                        self._log_cmd(f"prompt loaded: {name}")
                    else:
                        self._tts.speak(f"No prompt found matching {query}")
                    return

        # ── normal dictation flow (unchanged from original) ───────────────────
        if self._launcher.forward_dictation(text):
            self._log_cmd(f"-> app text: {text[:40]}");
            return
        send_msg({"type": "llm_query", "text": text})
        self._chat.add_user(text)
        if self._poller and self._poller.data["model_loaded"] != "None":
            self._current_ai_idx = self._chat.start_ai()
            self._tts_buffer = ""
            # ── inject system prompt if one is active ─────────────────────────
            system = self._prompt_store.active_text
            if system:
                self._poller.stream_generation(text, system_prompt=system)
            else:
                self._poller.stream_generation(text)
        else:
            idx = self._chat.start_ai()
            self._chat.append_ai_token(idx, "[no model loaded]")
            self._tts.speak("unrecognized command.")

    def _submit_staged(self):
        t=list(self.login.staged); self.login.clear()
        if t: self._stt_final(t," ".join(str(x) for x in t).lower())

    def _stt_partial(self,raw_text,token_sets=None):
        words,nums=token_sets if token_sets else (set(),set())
        if self.ui_state in (UIState.LOGIN,UIState.ENROL_1,UIState.ENROL_2):
            self.status.highlighted_words=words; self.status.highlighted_nums=nums
        elif self.ui_state==UIState.ACTIVE:
            if raw_text.strip(): self.dictation.update_partial(raw_text.strip())

    def _stt_final(self,tokens,raw_text):
        self.status.highlighted_words=set(); self.status.highlighted_nums=set()
        if self.dictation.visible: self.dictation.dismiss()

        if self.ui_state==UIState.ACTIVE:
            hc=VoskSTT.help_command(raw_text)
            if hc=="open":
                self.help_panel.set_open(True)
                self._log_cmd("help panel opened"); self._ack(); return
            if hc=="close":
                self.help_panel.set_open(False)
                self._log_cmd("help panel closed"); self._ack(); return

        if self.ui_state==UIState.ACTIVE:
            mc=VoskSTT.mode_command(raw_text)
            if mc=="conversation": self._set_mode(InputMode.CONVERSATION); self._ack(); return
            if mc=="command":      self._set_mode(InputMode.COMMAND);      self._ack(); return
            if mc=="toggle":       self._toggle_mode();                     self._ack(); return
            if mc=="media":        self._set_mode(InputMode.MEDIA);         self._ack(); return

        if self.ui_state==UIState.ACTIVE and self._input_mode==InputMode.MEDIA:
            mc3 = VoskSTT.media_command(raw_text)

            if self._media._pending_artist is not None and mc3 is None:
                song_query    = raw_text.lower().strip()
                artist_matches = self._media._pending_artist_matches
                best_i, best_s = None, 0.0
                q_words = self._media._word_set(song_query)
                for idx in artist_matches:
                    fname = os.path.splitext(
                        os.path.basename(self._media.tracks[idx]))[0]
                    f_words = self._media._word_set(fname)
                    score   = self._media._fuzzy_word_score(q_words, f_words)
                    if score > best_s:
                        best_s, best_i = score, idx
                self._media._pending_artist = None
                self._media._pending_artist_matches = []
                if best_i is not None and best_s >= 0.45:
                    self._media.current_idx = best_i
                    self._media._resolve_art()
                    self._media.play()
                    name = os.path.splitext(
                        os.path.basename(self._media.tracks[best_i]))[0]
                    self._tts.speak(f"Playing {name}")
                    self._log_cmd(f"play song (artist search): {name}")
                else:
                    result = self._media.play_specific_track(song_query)
                    self._tts.speak(result)
                    self._log_cmd(result)
                return

            if mc3 and mc3.startswith("play_artist_all:"):
                artist  = mc3[len("play_artist_all:"):]
                matches = self._media.find_tracks_by_artist(artist)
                result  = self._media.play_artist_shuffle(artist, matches=matches)
                self._tts.speak(result)
                self._log_cmd(result)
                return

            if mc3 and mc3.startswith("play_named:"):
                query = mc3[len("play_named:"):]
                result = self._media.find_best_match(query)
                rtype  = result["type"]
                print(f"[Media] query='{query}' type={rtype} "
                      f"a_score={result['artist_score']:.2f} "
                      f"s_score={result['song_score']:.2f}")

                if rtype == "artist":
                    matches = result["artist_matches"]
                    folder  = os.path.basename(
                        os.path.dirname(self._media.tracks[matches[0]]))
                    if len(matches) == 1:
                        msg = self._media.play_specific_track(
                            query, track_idx=matches[0])
                        self._tts.speak(msg)
                        self._log_cmd(msg)
                    else:
                        self._media._pending_artist = query
                        self._media._pending_artist_matches = matches
                        self._tts.speak(
                            f"Found {len(matches)} tracks by {folder}. "
                            f"Which song?")
                        self._log_cmd(
                            f"artist match: {folder} ({len(matches)} tracks)")

                elif rtype == "song":
                    msg = self._media.play_specific_track(
                        query, track_idx=result["song_idx"])
                    self._tts.speak(msg)
                    self._log_cmd(f"play song: {msg}")

                else:
                    self._tts.speak(f"Could not find {query}")
                    self._log_cmd(f"no match: {query}")
                return

            if mc3=="play":    self._media.play();                      self._ack(); return
            if mc3=="pause":
                if self._media.playing and not self._media.paused: self._media.pause()
                elif self._media.paused: self._media.resume()
                self._ack(); return
            if mc3=="stop":    self._media.stop();                      self._ack(); return
            if mc3=="next":    self._media.next_track();                self._ack(); return
            if mc3=="prev":    self._media.prev_track();                self._ack(); return
            if mc3=="shuffle": self._media.shuffle=not self._media.shuffle; self._ack(); return
            if mc3=="repeat":  self._media.repeat=not self._media.repeat;   self._ack(); return
            if mc3=="vol_up":  self._media.set_volume(self._media.volume+0.15); self._ack(); return
            if mc3=="vol_dn":  self._media.set_volume(self._media.volume-0.15); self._ack(); return
            if mc3=="folder":  self._media.open_folder();               self._ack(); return
            if mc3 and mc3.startswith("vol_set:"):
                try: self._media.set_volume(float(mc3.split(":")[1]))
                except Exception: pass
                self._ack(); return

        if self.ui_state==UIState.ACTIVE and self._input_mode==InputMode.CONVERSATION:
            if raw_text.strip(): self.dictation.finalize(raw_text)
            return

        if self.ui_state==UIState.ACTIVE:
            low=raw_text.lower().strip()
            if any(low==c or low.startswith(c) for c in APP_DRAWER_CMDS):
                self._app_drawer.open(); self._log_cmd("app drawer opened"); self._ack(); return

            if any(low==c for c in APP_RESCAN_CMDS):
                apps=self._launcher.scan(); self._log_cmd(f"rescanned: {len(apps)} apps"); self._ack(); return

            for trigger in APP_OPEN_CMDS:
                if low.startswith(trigger+" "):
                    e=self._launcher.match(low[len(trigger):].strip())
                    if e: self._launch_app(e); self._ack(); return

            for trigger in APP_CLOSE_CMDS:
                if low.startswith(trigger+" "):
                    e=self._launcher.match(low[len(trigger):].strip())
                    if e: self._log_cmd(self._launcher.close(e)); self._ack(); return

            for trigger in APP_MINIMISE_CMDS:
                if low.startswith(trigger + " ") or low == trigger:
                    target = low[len(trigger):].strip()
                    e = (self._launcher.match(target) if target else self._launcher.focused())
                    if e:
                        self._log_cmd(self._launcher.send_window_command(e, "minimise"))
                        self._ack(); return

            for trigger in APP_MAXIMISE_CMDS:
                if low.startswith(trigger + " ") or low == trigger:
                    target = low[len(trigger):].strip()
                    e = (self._launcher.match(target) if target else self._launcher.focused())
                    if e:
                        self._log_cmd(self._launcher.send_window_command(e, "maximise"))
                        self._ack(); return

            for trigger in APP_RESTORE_CMDS:
                if low.startswith(trigger + " ") or low == trigger:
                    target = low[len(trigger):].strip()
                    e = (self._launcher.match(target) if target else self._launcher.focused())
                    if e:
                        self._log_cmd(self._launcher.send_window_command(e, "restore"))
                        self._ack(); return

            if VoskSTT.logout_command(raw_text): self._do_logout(); self._ack(); return
            mc2=VoskSTT.mute_command(raw_text)
            if mc2=="mute":   self._set_mute(True);  self._ack(); return
            if mc2=="unmute": self._set_mute(False); self._ack(); return
            if VoskSTT.wallpaper_command(raw_text): self._log_cmd("wallpaper browser"); self.wallpaper_browser.open(); self._ack(); return
            cmd=VoskSTT.panel_command(raw_text)
            if cmd:
                side,action=cmd; panel=self.panel_l if side=="left" else self.panel_r
                panel.set_open(action); self._log_cmd(f"panel {side} {action}"); self._ack(); return
            forwarded, silent = self._launcher.forward_command(raw_text)
            if forwarded:
                self._log_cmd(f"-> app: {raw_text}")
                if not silent: self._ack()
                return
        if not tokens and not raw_text.strip(): return
        print(f"[STT] tokens={tokens}  raw='{raw_text}'")
        if self.ui_state==UIState.ENROL_1:
            self.login.enrol_1=tokens; self.ui_state=UIState.ENROL_2; self.login.set_state("enrol_2")
        elif self.ui_state==UIState.ENROL_2:
            if tokens==self.login.enrol_1: save_passphrase(tokens); self._do_success()
            else: self.login.enrol_1=None; self.ui_state=UIState.ENROL_MISMATCH; self.login.set_state("enrol_mismatch"); self._flash_t=0.0
        elif self.ui_state==UIState.LOGIN:
            if verify_passphrase(tokens): self._do_success()
            else: self._do_error()
        elif self.ui_state==UIState.ACTIVE:
            if self._input_mode!=InputMode.MEDIA:
                self.dictation.finalize(raw_text)

    def _stt_level(self,level):
        self.status.stt_level=level; self.status.stt_ready=self._stt.ready
        if not self._stt.muted:
            stt_active=(level>getattr(self._stt,"_open_threshold",0.02))
            self._media_stt_active=stt_active
            self._media.set_ducked(stt_active)
        else:
            self._media_stt_active=False
            self._media.set_ducked(False)
        if self.ui_state==UIState.ACTIVE and not self._stt.muted:
            if (not self.dictation.visible and not self.wallpaper_browser.visible
                    and level>getattr(self._stt,"_open_threshold",0.02)):
                self.dictation.show()

    def _ipc_server(self):
        srv=_make_server_socket(); srv.listen(5); srv.settimeout(1.0)
        while self.running:
            try:
                conn,_=srv.accept(); data=b""
                while True:
                    c=conn.recv(4096)
                    if not c: break
                    data+=c
                conn.close()
                for line in data.decode().splitlines():
                    line=line.strip()
                    if line: self._ipc(json.loads(line))
            except socket.timeout: pass
            except Exception: pass

    def _ipc(self,msg):
        t=msg.get("type","")
        if t=="state":
            v=msg.get("value","")
            m={"login":UIState.LOGIN,"success":UIState.LOGIN_SUCCESS,"error":UIState.LOGIN_ERROR,
               "active":UIState.ACTIVE,"suspended":UIState.SUSPENDED,"enrol":UIState.ENROL_1}
            if v in m:
                self.ui_state=m[v]
                if v in ("success","error"): self.login.set_state(v); self._flash_t=0.0
                elif v=="login": self.login.set_state("login"); self.login.clear()
                elif v=="enrol": self.login.set_state("enrol_1"); self.login.enrol_1=None; self.login.clear()
        elif t=="highlight":
            self.status.highlighted_words=set(msg.get("words",[])); self.status.highlighted_nums=set(msg.get("nums",[]))
        elif t=="audio":
            self.status.stt_level=float(msg.get("stt",0)); self.status.tts_level=float(msg.get("tts",0))
        elif t=="status":
            for k in ("cpu","ram","gpu"): setattr(self.status,k,float(msg.get(k,0)))
            for k in ("model_loaded","model_activity"):
                if k in msg: setattr(self.status,k,msg[k])
            self.status.mesh_connected=int(msg.get("mesh",0)); self.status.mesh_role=int(msg.get("role",0))
            if "battery" in msg: self.status.battery=int(msg["battery"])
        elif t=="panel":
            side=msg.get("side","left"); action=msg.get("action",None)
            (self.panel_l if side=="left" else self.panel_r).set_open(action)
            self._log_cmd(f"IPC panel {side} {'toggle' if action is None else action}")
        elif t=="help_panel":
            action=msg.get("action",None)
            self.help_panel.set_open(action)
            self._log_cmd(f"IPC help panel {'toggle' if action is None else ('open' if action else 'close')}")
        elif t=="click_input": self.login.click_input_enabled=bool(msg.get("enabled",True))
        elif t=="reset_passphrase":
            if os.path.exists(PASSPHRASE_FILE): os.unlink(PASSPHRASE_FILE)
            self.ui_state=UIState.ENROL_1; self.login.set_state("enrol_1"); self.login.enrol_1=None; self.login.clear()
            self._log_cmd("passphrase reset")
        elif t=="wallpaper":
            p=msg.get("path","")
            if p: self._on_wallpaper_select(p); self._log_cmd(f"wallpaper: {os.path.basename(p)}")
        elif t=="wallpaper_browser": self.wallpaper_browser.open(); self._log_cmd("wallpaper browser (IPC)")
        elif t=="llm_activity":
            if self._poller:
                self._poller.data["model_activity"]=msg.get("text","")
                self._poller.data["generating"]=msg.get("generating",False)
        elif t=="mute":   self._set_mute(bool(msg.get("value",True)))
        elif t=="logout": self._do_logout()
        elif t=="set_font":
            fn=msg.get("name","")
            if fn: self._on_font_select(fn); self._log_cmd(f"font -> {fn}")
        elif t=="set_mode":
            v=msg.get("value","").lower()
            if v=="conversation": self._set_mode(InputMode.CONVERSATION)
            elif v=="command":    self._set_mode(InputMode.COMMAND)
            elif v=="media":      self._set_mode(InputMode.MEDIA)
            elif v=="toggle":     self._toggle_mode()
        elif t=="media_play":    self._media.play()
        elif t=="media_pause":
            if self._media.playing and not self._media.paused: self._media.pause()
            elif self._media.paused: self._media.resume()
        elif t=="media_stop":    self._media.stop()
        elif t=="media_next":    self._media.next_track()
        elif t=="media_prev":    self._media.prev_track()
        elif t=="media_volume":
            v=msg.get("value",None)
            if v is not None: self._media.set_volume(float(v))
        elif t=="clear_chat": self._chat.clear(); self._log_cmd("chat cleared")
        elif t=="clear_commands": self._cmd_log.clear()
        elif t=="app_ready":
            mf=msg.get("manifest",{}); pid=msg.get("pid",0)
            self._launcher.register_from_ipc(mf,pid); self._log_cmd(f"app registered: {mf.get('name','?')}")
        elif t=="app_closed": self._log_cmd(f"app closed: {msg.get('name','?')}")
        elif t=="app_chat":
            role=msg.get("role","ai"); text=msg.get("text","")
            if role=="you": self._chat.add_user(text)
            else: idx=self._chat.start_ai(); self._chat.append_ai_token(idx,text)
        elif t=="app_command_log": self._log_cmd(msg.get("text",""))
        elif t=="tts_speak":
            text=msg.get("text","")
            if text: self._tts.speak(text); print(f"[TTS/IPC] {text}")
        elif t=="tts_speak_immediate":
            text=msg.get("text","")
            if text: self._tts.speak_immediate(text)
        elif t=="open_app":
            e=self._launcher.match(msg.get("name",""))
            if e: self._launch_app(e)
        elif t=="close_app":
            e=self._launcher.match(msg.get("name",""))
            if e: self._log_cmd(self._launcher.close(e))

    def _tick_flash(self,dt):
        if self.ui_state==UIState.LOGIN_SUCCESS:
            self._flash_t+=dt
            if self._flash_t>=self.FLASH_HOLD_OK: self.ui_state=UIState.ACTIVE; self.login.clear()
        elif self.ui_state==UIState.LOGIN_ERROR:
            self._flash_t+=dt
            if self._flash_t>=self.FLASH_HOLD_ERR: self.ui_state=UIState.LOGIN; self.login.set_state("login")
        elif self.ui_state==UIState.ENROL_MISMATCH:
            self._flash_t+=dt
            if self._flash_t>=self.FLASH_HOLD_MISMATCH:
                self.ui_state=UIState.ENROL_1; self.login.set_state("enrol_1"); self.login.enrol_1=None

    def _tick_poller(self):
        if not self._poller: return
        d=self._poller.data
        self.status.cpu=d["cpu"]; self.status.ram=d["ram"]; self.status.gpu=d["gpu"]
        self.status.battery=d["battery"]; self.status.model_loaded=d["model_loaded"]
        self.status.model_activity=d["model_activity"]; self.status.model_size_gb=d["model_size_gb"]

    def _tick_webcam(self):
        frame = self._webcam.get_frame()
        if frame is None:
            self._cam_surf = None; return
        r = self.wave.base_radius
        try:
            result = make_circular_cam(frame, r * 2, BLUE_MID, 3)
            if result is not None:
                self._cam_surf = result
        except Exception:
            self._cam_surf = None

    def run(self):
        prev=time.time()
        while self.running:
            now=time.time(); dt=min(now-prev,0.05); prev=now
            self.status.time_str=time.strftime("%H:%M"); self.status.date_str=time.strftime("%d %b %Y").upper()
            self._tick_flash(dt); self._tick_poller(); self._tick_webcam()
            self.wave.set_cam(self._cam_surf)
            self.dictation.update(dt); self.wallpaper_browser.update(dt)
            self._app_drawer.update(dt); self.help_panel.update(dt)

            for ev in pygame.event.get():
                if ev.type==pygame.QUIT: self.running=False
                elif ev.type==pygame.KEYDOWN:
                    k=ev.key; mods=pygame.key.get_mods(); ctrl=(mods&pygame.KMOD_CTRL)!=0
                    if (self.ui_state==UIState.ACTIVE and not ctrl
                            and not self._app_drawer.visible and not self.wallpaper_browser.visible):
                        if self._launcher.forward_key(k,ev.unicode,mods): continue
                    if self.panel_r._font_dropdown and self.panel_r._font_dropdown.open and k==pygame.K_ESCAPE:
                        self.panel_r._font_dropdown.close(); continue
                    if self._app_drawer.visible and self._app_drawer.handle_key(k): continue
                    if self.wallpaper_browser.visible and self.wallpaper_browser.handle_key(k): continue
                    if self.help_panel.open and k==pygame.K_ESCAPE:
                        self.help_panel.set_open(False); continue
                    if self.dictation.handle_key(k): continue
                    if   k==pygame.K_F1: self.panel_l.toggle()
                    elif k==pygame.K_F2: self.panel_r.toggle()
                    elif k==pygame.K_F3: self._app_drawer.toggle()
                    elif ctrl and k==pygame.K_h:
                        self.help_panel.toggle()
                        self._log_cmd(f"help panel {'opened' if self.help_panel.open else 'closed'}")
                    elif ctrl and k==pygame.K_p and self.ui_state==UIState.ACTIVE:
                        if self._input_mode==InputMode.MEDIA: self._set_mode(InputMode.COMMAND)
                        else: self._set_mode(InputMode.MEDIA)
                    elif ctrl and k==pygame.K_TAB:
                        if self.ui_state==UIState.ACTIVE: self._toggle_mode()
                    elif ctrl and k==pygame.K_m: self._toggle_mute()
                    elif ctrl and k==pygame.K_w and self.ui_state==UIState.ACTIVE: self.wallpaper_browser.open()
                    elif ctrl and k==pygame.K_l:
                        if self.ui_state==UIState.ACTIVE: self._do_logout()
                        else: self.ui_state=UIState.LOGIN; self.login.set_state("login"); self.login.clear()
                    elif ctrl and k==pygame.K_s: self._do_success()
                    elif ctrl and k==pygame.K_e: self._do_error()
                    elif ctrl and k==pygame.K_a: self.ui_state=UIState.ACTIVE
                    elif ctrl and k==pygame.K_n:
                        if os.path.exists(PASSPHRASE_FILE): os.unlink(PASSPHRASE_FILE)
                        self.ui_state=UIState.ENROL_1; self.login.set_state("enrol_1")
                        self.login.enrol_1=None; self.login.clear()
                    elif ctrl and k==pygame.K_c: self.login.click_input_enabled=not self.login.click_input_enabled
                    elif k in (pygame.K_RETURN,pygame.K_KP_ENTER):
                        if self.ui_state in (UIState.LOGIN,UIState.ENROL_1,UIState.ENROL_2): self._submit_staged()
                    elif k==pygame.K_BACKSPACE:
                        if self.ui_state in (UIState.LOGIN,UIState.ENROL_1,UIState.ENROL_2): self.login.pop()
                    elif k==pygame.K_DELETE:
                        if self.ui_state in (UIState.LOGIN,UIState.ENROL_1,UIState.ENROL_2): self.login.clear()
                    elif k==pygame.K_ESCAPE: self.running=False
                elif ev.type==pygame.MOUSEBUTTONDOWN and ev.button==1:
                    if self._app_drawer.visible: self._app_drawer.handle_click(ev.pos); continue
                    if self.wallpaper_browser.visible: self.wallpaper_browser.handle_click(ev.pos); continue
                    if self.help_panel.handle_mousedown(ev.pos): continue
                    if self._model_picker.handle_mousedown(ev.pos):
                        name = self._model_picker.clicked_model(ev.pos)
                        if name:
                            self._switch_model(name)
                        continue
                    if (self.ui_state==UIState.ACTIVE and self._input_mode==InputMode.MEDIA
                            and self._media.handle_click(ev.pos)): continue
                    if (self.status.input_mode == InputMode.CONVERSATION and
                            self._prompt_pill.handle_mousedown(ev.pos)):
                        continue
                    if _mode_pill_rect and _mode_pill_rect.collidepoint(ev.pos) and self.ui_state==UIState.ACTIVE:
                        self._toggle_mode(); continue
                    if self.panel_l.handle_mousedown(ev.pos): continue
                    if self.panel_r.handle_mousedown(ev.pos): continue
                    if self.ui_state in (UIState.LOGIN,UIState.ENROL_1,UIState.ENROL_2):
                        r=self.login.handle_click(ev.pos)
                        if r: send_msg(r)
                elif ev.type==pygame.MOUSEBUTTONUP and ev.button==1: self.panel_r.handle_mouseup(ev.pos)
                elif ev.type==pygame.MOUSEMOTION:
                    self._app_drawer.handle_mousemove(ev.pos); self.panel_r.handle_mousemove(ev.pos)
                    self._prompt_pill.handle_mousemove(ev.pos)
                    self._model_picker.handle_mousemove(ev.pos)
                    if self.ui_state in (UIState.LOGIN,UIState.ENROL_1,UIState.ENROL_2): self.login.handle_motion(ev.pos)
                elif ev.type==pygame.MOUSEWHEEL:
                    mp=pygame.mouse.get_pos()
                    if self.help_panel.handle_mousewheel(mp,ev.y): pass
                    elif self._prompt_pill.handle_mousewheel(mp, ev.y):
                        pass
                    elif not self.panel_r.handle_mousewheel(mp,ev.y) and not self.panel_l.handle_mousewheel(mp,ev.y):
                        if self.wallpaper_browser.visible:
                            vr=self.wallpaper_browser._vrows()
                            rows=math.ceil(len(self.wallpaper_browser._images)/self.wallpaper_browser.COLS)
                            self.wallpaper_browser._scroll=max(0,min(rows-vr,self.wallpaper_browser._scroll-ev.y))

            self.login.update(dt); self.wave.update(
                max(self.status.stt_level,
                    self._media.music_level if self._input_mode == InputMode.MEDIA else 0.0),
                self.status.tts_level, dt)
            self.panel_l.update(dt); self.panel_r.update(dt)
            self._prompt_pill.update(dt)
            self._model_picker.update(dt)
            if self._input_mode==InputMode.MEDIA:
                self._media.update(dt, self._media_stt_active)
            login_states=(UIState.LOGIN,UIState.LOGIN_SUCCESS,UIState.LOGIN_ERROR,
                          UIState.ENROL_1,UIState.ENROL_2,UIState.ENROL_MISMATCH)
            pa=self._current_panel_alpha(); ba=self._current_bar_alpha()
            if self.ui_state in login_states:
                self.login.draw(self.screen,self.status,cam_surf=self._cam_surf)
            else:
                if self.wallpaper: self.screen.blit(self.wallpaper,(0,0))
                else: self.screen.fill(DARK_BG)
                if self._input_mode==InputMode.MEDIA:
                    self._media.draw(
                        self.screen, self.wave,
                        self._media_stt_active,
                        self.status.stt_level, self.status.tts_level, dt)
                    if not self._media_stt_active:
                        self.wave.draw(self.screen, show_cam=False)
                else:
                    self.wave.draw(self.screen,show_cam=False)
                self.panel_l.draw(self.screen,panel_alpha=pa); self.panel_r.draw(self.screen,panel_alpha=pa)
                draw_top_bar(self.screen,self.fonts,self.status,self.W,self.H,bar_alpha=ba)
                draw_bottom_bar(self.screen,self.fonts,self.status,self._poller,self.W,self.H,bar_alpha=ba,launcher=self._launcher,prompt_pill=self._prompt_pill, model_picker=self._model_picker)
                self._model_picker.draw(self.screen, self.W, self.H, ba)
                self.help_panel.draw(self.screen,panel_alpha=pa)
                if self.ui_state==UIState.SUSPENDED:
                    ov=pygame.Surface((self.W,self.H),pygame.SRCALPHA); ov.fill((0,0,0,140)); self.screen.blit(ov,(0,0))
                    ms=self.fonts["xl"].render("SESSION SUSPENDED",True,TEXT_DIM)
                    self.screen.blit(ms,(self.W//2-ms.get_width()//2,self.H//2-ms.get_height()//2))
                self.wallpaper_browser.draw(self.screen); self._app_drawer.draw(self.screen)
                self.dictation.draw(self.screen)
                if self.status.input_mode == InputMode.CONVERSATION:
                    self._prompt_pill.draw_dropdown(self.screen, self.W, self.H, ba)
            pygame.display.flip(); self.clock.tick(self.TARGET_FPS)

        self._launcher.close_all()
        if self._poller: self._poller.stop()
        self._media.stop()
        self._stt.stop(); self._tts.stop(); self._webcam.stop()
        pygame.quit(); _cleanup_server_socket()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__=="__main__":
    ap=argparse.ArgumentParser(description="LoomOS Speech Centre v0.15")
    ap.add_argument("--wallpaper","-w",default=None)
    ap.add_argument("--windowed",action="store_true")
    ap.add_argument("--reset-passphrase",action="store_true")
    ap.add_argument("--font",default=None)
    ap.add_argument("--mode",default=None,choices=["command","conversation","media"])
    args=ap.parse_args()
    if args.reset_passphrase and os.path.exists(PASSPHRASE_FILE):
        os.unlink(PASSPHRASE_FILE); print("[CLI] Passphrase reset.")
    app=LoomOSGui(wallpaper_path=args.wallpaper)
    if args.font: app._on_font_select(args.font)
    if args.mode=="conversation": app._set_mode(InputMode.CONVERSATION)
    if args.mode=="media":        app._set_mode(InputMode.MEDIA)
    if args.windowed:
        app.screen=pygame.display.set_mode((1280,720)); app.W,app.H=1280,720
        _wave_r=int(1280*0.13); _wave_cy=360
        app.wave=WaveformCircle(640,_wave_cy,_wave_r); app.wave.conv_mode=(app._input_mode==InputMode.CONVERSATION)
        app._media=MediaPlayerMode(640,_wave_cy,_wave_r,app.fonts)
        app.login=LoginScreen(app.fonts,1280,720)
        app.login.set_state("login" if passphrase_enrolled() else "enrol_1")
        if not passphrase_enrolled(): app.ui_state=UIState.ENROL_1
        app.panel_l=SidePanel("left",1280,720,app.fonts,app._poller,settings=app._settings,
                               on_settings_change=app._on_settings_change,chat_history=app._chat,command_log=app._cmd_log)
        app.panel_l._input_mode=app._input_mode
        app.panel_l._active_tab="conv" if app._input_mode==InputMode.CONVERSATION else "cmds"
        app.panel_r=SidePanel("right",1280,720,app.fonts,app._poller,stt_ref=app._stt,settings=app._settings,
                               on_settings_change=app._on_settings_change,font_list=app._system_fonts,
                               on_font_select=app._on_font_select)
        app.dictation=DictationPopup(1280,720,app.fonts,app._on_dictation_submit,circle_cy=_wave_cy,circle_r=_wave_r)
        app.dictation.conv_mode=(app._input_mode==InputMode.CONVERSATION)
        app.wallpaper_browser=WallpaperBrowser(1280,720,app.fonts,app._on_wallpaper_select)
        app._launcher=AppLauncher(); app._launcher.scan()
        app._app_drawer=AppDrawer(1280,720,app.fonts,app._launcher,on_launch=app._launch_app)
        app.help_panel=HelpPanel(1280,720,app.fonts)
    print("\nLoomOS Speech Centre v0.15 (home-scan + threshold patch)")
    print("-"*60)
    print("F1=left panel  F2=right panel  F3=app drawer  CTRL+H=help")
    print("CTRL+TAB=cycle mode  CTRL+P=toggle media  CTRL+M=mute  CTRL+W=wallpaper")
    print("CTRL+L=logout  ESC=quit")
    print("\nMEDIA scan: ~/Music, ~/music, ~/Videos, ~/videos, ~/Movies, ~ (home)")
    print("            up to depth 5 from home, 8 from named dirs, cap 2000 files")
    print("\nMEDIA mode voice commands:")
    print("  'media mode' / 'music mode'         → enter media mode")
    print("  play / pause / stop / next / previous")
    print("  play <song name>                     → play directly, TTS confirms name")
    print("  play <artist name>                   → asks 'Which song?'")
    print("  play anything by <artist>            → shuffle all artist tracks, auto-start")
    print("  shuffle / repeat / volume up / volume down / set volume 70")
    print("  open folder / open media folder")
    print("\nMEDIA mode IPC:")
    print('  {"type":"set_mode","value":"media"}')
    print('  {"type":"media_play"}  {"type":"media_pause"}  {"type":"media_next"}')
    print('  {"type":"media_volume","value":0.8}')
    print("\nHelp panel: CTRL+H  |  'show help'  |  ESC to close")
    print(f"\nTTS: espeak-ng (Linux) / SAPI5 (Windows) / pyttsx3 (fallback)")
    print("     On Linux:  sudo apt-get install espeak espeak-ng libespeak-ng1")
    print("\nSpeech detection auto-ducks media volume during speech input.\n")
    app.run()