"""
loomos_app_sdk.py — LoomOS App SDK  v0.2

Drop this file in ~/.loomos_apps/ alongside your app scripts.

    from loomos_app_sdk import LoomApp

    APP = LoomApp(LOOMOS_APP)
    APP.ready()          # tell the GUI you're open

    @APP.on_command
    def handle(cmd: str): ...          # voice command forwarded from STT

    @APP.on_key
    def key(k: int, ch: str, mods: int): ...  # raw keypress (if accepts_keys)

    @APP.on_dictation
    def dictated(text: str): ...       # confirmed spoken text (if accepts_dictation)

    APP.run()            # blocks until quit

──────────────────────────────────────────────────────────────────────────────

Manifest format  (LOOMOS_APP dict at top of your app file)
──────────────────────────────────────────────────────────
LOOMOS_APP = {
    # ── Required ──────────────────────────────────────────────────────────
    "name":        "My App",      # shown in app drawer
    "description": "Does stuff",  # subtitle on the drawer tile
    "icon":        "🔧",           # single emoji shown on the tile
    "keywords": ["my app", "app"],# voice trigger words (lowercase)
    "ipc_port":  47852,           # unique port, range 47843-47899

    # ── Optional ──────────────────────────────────────────────────────────
    "version":          "1.0",
    "singleton":        True,     # prevent duplicate launches (default True)
    "accepts_keys":     False,    # set True to receive raw key_input messages
    "accepts_dictation":False,    # set True to receive dictation_text messages
                                  # (dictation is routed HERE instead of the LLM
                                  #  while this app is focused)
}

IPC messages the SDK sends TO the GUI
──────────────────────────────────────
  app_ready        — app started; registers it in the drawer
  app_closed       — app is about to exit
  app_status       — update the bottom-bar status text
  app_chat         — post a line into the AI conversation log
  app_command_log  — add an entry to the command log tab
  tts_speak        — ask the GUI's TTS to speak a string

IPC messages the GUI sends TO the app
──────────────────────────────────────
  voice_command  — raw STT text forwarded from the GUI (always sent)
  key_input      — raw keypress  { key, unicode, mods }
                   only sent when manifest["accepts_keys"] is True
  dictation_text — confirmed spoken text { text }
                   only sent when manifest["accepts_dictation"] is True
                   (bypasses the LLM pipeline while this app is focused)
  focus          — this app became foreground
  blur           — another app took focus
  quit           — GUI is shutting down; please exit cleanly

Pygame helper
─────────────
Apps that run their own pygame window can use pump_ipc() inside their
main loop instead of APP.run():

    while running:
        for ev in pygame.event.get(): ...
        APP.pump_ipc()   # process any queued IPC messages non-blocking
        screen.fill(...)
        pygame.display.flip()
        clock.tick(30)
"""

import json
import os
import platform
import socket
import sys
import threading
import time
from typing import Callable, Optional


# ── GUI IPC config (mirrors loomos_gui.py logic) ──────────────────────────────

def _gui_ipc_cfg() -> dict:
    is_win  = platform.system() == "Windows"
    unix_ok = hasattr(socket, "AF_UNIX") and not is_win
    if unix_ok:
        return {"mode": "unix", "path": "/tmp/loomos_gui.sock"}
    return {"mode": "tcp", "host": "127.0.0.1", "port": 47842}


def _send_to_gui_raw(msg: dict) -> bool:
    """Send one JSON line to the main GUI. Returns True on success."""
    cfg = _gui_ipc_cfg()
    try:
        if cfg["mode"] == "unix":
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(cfg["path"])
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((cfg["host"], cfg["port"]))
        s.sendall((json.dumps(msg) + "\n").encode())
        s.close()
        return True
    except Exception as e:
        print(f"[SDK] send_to_gui failed: {e}", file=sys.stderr)
        return False


# ── Standalone helpers (usable without LoomApp) ────────────────────────────────

def send_chat(text: str, role: str = "ai") -> bool:
    """Post a line into the GUI conversation log."""
    return _send_to_gui_raw({"type": "app_chat", "role": role, "text": text})


def speak(text: str) -> bool:
    """Ask the GUI's TTS engine to speak *text*."""
    return _send_to_gui_raw({"type": "tts_speak", "text": text})


def log_command(text: str) -> bool:
    """Append an entry to the GUI command-log tab."""
    return _send_to_gui_raw({"type": "app_command_log", "text": text})


# ── LoomApp ────────────────────────────────────────────────────────────────────

class LoomApp:
    """
    Convenience wrapper for a LoomOS app.

    Minimal background app
    ──────────────────────
        APP = LoomApp(LOOMOS_APP)

        @APP.on_command
        def cmd(text): print("voice:", text)

        APP.ready()
        APP.run()      # blocks until GUI sends quit

    Pygame app (non-blocking)
    ─────────────────────────
        APP = LoomApp(LOOMOS_APP)
        APP.ready()

        while running:
            for ev in pygame.event.get(): ...
            APP.pump_ipc()   # handle IPC without blocking
            ...
            pygame.display.flip()
    """

    def __init__(self, manifest: dict):
        self.manifest  = manifest
        self.name      = manifest.get("name", "Unknown App")
        self.ipc_port  = manifest.get("ipc_port", 47899)

        self._command_cb:   Optional[Callable] = None
        self._key_cb:       Optional[Callable] = None
        self._dictation_cb: Optional[Callable] = None
        self._focus_cb:     Optional[Callable] = None
        self._blur_cb:      Optional[Callable] = None
        self._quit_cb:      Optional[Callable] = None

        self._running  = False
        self._queue:   list = []
        self._q_lock   = threading.Lock()
        self._srv_thread: Optional[threading.Thread] = None

    # ── Decorator API ─────────────────────────────────────────────────────────

    def on_command(self, fn: Callable):
        """Decorator — called with raw STT voice-command text."""
        self._command_cb = fn; return fn

    def on_key(self, fn: Callable):
        """Decorator — called with (key: int, unicode: str, mods: int).
        Only fires when manifest['accepts_keys'] is True."""
        self._key_cb = fn; return fn

    def on_dictation(self, fn: Callable):
        """Decorator — called with the confirmed spoken text string.
        Only fires when manifest['accepts_dictation'] is True.
        While this app is focused, dictation bypasses the LLM and comes here."""
        self._dictation_cb = fn; return fn

    def on_focus(self, fn: Callable):
        """Decorator — called when this app becomes foreground."""
        self._focus_cb = fn; return fn

    def on_blur(self, fn: Callable):
        """Decorator — called when another app takes focus."""
        self._blur_cb = fn; return fn

    def on_quit(self, fn: Callable):
        """Decorator — called when the GUI sends a quit signal."""
        self._quit_cb = fn; return fn

    # ── Outbound helpers ──────────────────────────────────────────────────────

    def ready(self):
        """Register this app with the GUI and start the IPC listener thread."""
        _send_to_gui_raw({
            "type":     "app_ready",
            "manifest": self.manifest,
            "pid":      os.getpid(),
        })
        self._running = True
        self._start_listener()

    def closed(self):
        """Notify the GUI this app is exiting."""
        _send_to_gui_raw({"type": "app_closed", "name": self.name, "pid": os.getpid()})
        self._running = False

    def status(self, text: str):
        """Update the bottom-bar activity string while this app is focused."""
        _send_to_gui_raw({"type": "app_status", "name": self.name, "text": text})

    def chat(self, text: str, role: str = "ai"):
        """Post a message to the GUI conversation log."""
        send_chat(text, role)

    def speak(self, text: str):
        """Ask the GUI TTS to speak text."""
        speak(text)

    def log(self, text: str):
        """Append to the GUI command-log tab, prefixed with app name."""
        log_command(f"[{self.name}] {text}")

    # ── Run modes ─────────────────────────────────────────────────────────────

    def run(self):
        """
        Blocking run-loop for background / non-pygame apps.
        Returns when the GUI sends quit or KeyboardInterrupt is raised.
        """
        if not self._running:
            self.ready()
        try:
            while self._running:
                self.pump_ipc()
                time.sleep(0.05)
        except KeyboardInterrupt:
            pass
        finally:
            self.closed()

    def pump_ipc(self):
        """
        Non-blocking: drain the IPC queue and dispatch any pending messages.
        Call this every frame from inside a pygame (or other) main loop.
        """
        with self._q_lock:
            msgs = list(self._queue)
            self._queue.clear()
        for msg in msgs:
            self._dispatch(msg)

    # ── Listener thread ───────────────────────────────────────────────────────

    def _start_listener(self):
        self._srv_thread = threading.Thread(
            target=self._listen_loop, daemon=True, name=f"LoomSDK-{self.name}")
        self._srv_thread.start()

    def _listen_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", self.ipc_port))
        except OSError as e:
            print(f"[SDK:{self.name}] Cannot bind port {self.ipc_port}: {e}",
                  file=sys.stderr)
            return
        srv.listen(5)
        srv.settimeout(1.0)
        print(f"[SDK:{self.name}] Listening on :{self.ipc_port}")
        while self._running:
            try:
                conn, _ = srv.accept()
                data = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                conn.close()
                for line in data.decode().splitlines():
                    line = line.strip()
                    if line:
                        with self._q_lock:
                            self._queue.append(json.loads(line))
            except socket.timeout:
                pass
            except Exception as e:
                print(f"[SDK:{self.name}] Listener error: {e}", file=sys.stderr)

    def _dispatch(self, msg: dict):
        t = msg.get("type", "")
        if t == "voice_command":
            if self._command_cb:
                self._command_cb(msg.get("text", ""))
        elif t == "key_input":
            if self._key_cb:
                self._key_cb(
                    msg.get("key", 0),
                    msg.get("unicode", ""),
                    msg.get("mods", 0),
                )
        elif t == "dictation_text":
            if self._dictation_cb:
                self._dictation_cb(msg.get("text", ""))
        elif t == "focus":
            if self._focus_cb:
                self._focus_cb()
        elif t == "blur":
            if self._blur_cb:
                self._blur_cb()
        elif t == "quit":
            self._running = False
            if self._quit_cb:
                self._quit_cb()


# ── Quick self-test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("LoomOS App SDK v0.2 — self-test")
    ok = _send_to_gui_raw({"type": "ping"})
    print(f"  GUI reachable: {ok}")
    print()
    print("Manifest flags added in v0.2:")
    print("  accepts_keys        → enables on_key() callback")
    print("  accepts_dictation   → enables on_dictation() callback")
    print("                        (routes STT away from LLM to your app)")
    print()
    print("Example text editor using SDK:")
    print("""
  LOOMOS_APP = {
      "name": "My Editor", "icon": "📝",
      "description": "Minimal text editor",
      "keywords": ["editor", "text"],
      "ipc_port": 47855,
      "accepts_keys": True,
      "accepts_dictation": True,
  }

  import pygame
  APP = LoomApp(LOOMOS_APP)
  APP.ready()

  @APP.on_dictation
  def got_text(text):
      editor.insert_text(text + " ")

  @APP.on_key
  def got_key(key, ch, mods):
      editor.handle_key(key, ch, mods)

  # inside your pygame loop:
  APP.pump_ipc()
""")
