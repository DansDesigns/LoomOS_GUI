#!/usr/bin/env python3
"""
LoomOS File Browser — built-in app
Full-featured file manager with grid/list view, background chooser,
transparency controls, and all standard file operations.
Drop in ~/.loomos_apps/ alongside loomos_app_sdk.py
"""

LOOMOS_APP = {
    "name":             "File Browser",
    "description":      "File manager",
    "icon":             "📁",
    "keywords":         ["files", "file browser", "folder", "file manager", "browser", "explorer"],
    "singleton":        True,
    "ipc_port":         47853,
    "accepts_keys":     True,
    "accepts_dictation":False,
}

import pygame, os, sys, json, socket, threading, time, math, shutil, zipfile, stat
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, simpledialog, messagebox

# ── IPC ───────────────────────────────────────────────────────────────────────

PORT      = LOOMOS_APP["ipc_port"]
SOCK_PATH = "/tmp/loomos_gui.sock"
GUI_PORT  = 47842

def _send_gui(msg: dict):
    try:
        if os.path.exists(SOCK_PATH):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(SOCK_PATH)
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("127.0.0.1", GUI_PORT))
        s.sendall((json.dumps(msg) + "\n").encode())
        s.close()
    except Exception:
        pass

_ipc_queue = []
_ipc_lock  = threading.Lock()

def _ipc_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(5); srv.settimeout(1.0)
    while True:
        try:
            conn, _ = srv.accept()
            data = b""
            while True:
                c = conn.recv(4096)
                if not c: break
                data += c
            conn.close()
            for line in data.decode().splitlines():
                line = line.strip()
                if line:
                    with _ipc_lock:
                        _ipc_queue.append(json.loads(line))
        except socket.timeout:
            pass
        except Exception:
            pass

threading.Thread(target=_ipc_server, daemon=True).start()

# ── Colours (matching music player palette) ───────────────────────────────────

DARK_BG    = (  8, 12, 20)
PANEL_BG   = ( 10, 16, 28)
BLUE_MID   = ( 30, 80,130)
BLUE_LITE  = ( 80,160,210)
BLUE_DARK  = ( 15, 35, 65)
TEXT_BRIGHT= (230,240,255)
TEXT_DIM   = (120,130,150)
TEXT_MID   = (180,190,200)
GREEN_LITE = ( 80,200, 80)
ORANGE_MID = (210,110, 30)
ORANGE_LITE= (230,140, 50)
RED_LITE   = (220, 60, 60)
YELLOW     = (230,200, 50)
WHITE      = (255,255,255)

BAR_H   = 38   # top bar height
BOT_H   = 38   # bottom bar height
TOOL_H  = 48   # toolbar height
PATH_H  = 42   # breadcrumb bar height

# ── File type icons ───────────────────────────────────────────────────────────

FOLDER_ICO = "📁"
FILE_ICONS = {
    # audio
    ".mp3":"🎵", ".ogg":"🎵", ".wav":"🎵", ".flac":"🎵", ".m4a":"🎵", ".opus":"🎵",
    # video
    ".mp4":"🎬", ".mkv":"🎬", ".avi":"🎬", ".mov":"🎬", ".webm":"🎬",
    # image
    ".jpg":"🖼", ".jpeg":"🖼", ".png":"🖼", ".gif":"🖼", ".bmp":"🖼", ".webp":"🖼", ".svg":"🖼",
    # docs
    ".pdf":"📄", ".txt":"📝", ".md":"📝", ".doc":"📝", ".docx":"📝",
    ".xls":"📊", ".xlsx":"📊", ".csv":"📊",
    ".ppt":"📊", ".pptx":"📊",
    # code
    ".py":"🐍", ".js":"📜", ".ts":"📜", ".html":"🌐", ".css":"🎨",
    ".c":"📜", ".cpp":"📜", ".h":"📜", ".rs":"📜", ".go":"📜", ".java":"📜",
    # archives
    ".zip":"📦", ".rar":"📦", ".tar":"📦", ".gz":"📦", ".7z":"📦", ".bz2":"📦",
    # executables
    ".sh":"⚙", ".bash":"⚙", ".exe":"⚙", ".bin":"⚙",
}

ARCHIVE_EXT = {".zip", ".rar", ".tar", ".gz", ".7z", ".bz2", ".tar.gz", ".tar.bz2"}

# Preferences file — persists background path and other settings
PREFS_FILE = os.path.join(os.path.expanduser("~"), ".loomos_filebrowser.json")


def _load_prefs() -> dict:
    try:
        with open(PREFS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_prefs(data: dict):
    try:
        with open(PREFS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def file_icon(path: str) -> str:
    if os.path.isdir(path):
        return FOLDER_ICO
    ext = os.path.splitext(path)[1].lower()
    return FILE_ICONS.get(ext, "📄")


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def format_mtime(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ── Popup / overlay helpers ───────────────────────────────────────────────────

class PopupMenu:
    """Simple drop-down or context menu."""
    def __init__(self, items: list, x: int, y: int, font, w: int = 180):
        self.items   = items   # list of (label, callback) or None for separator
        self.x       = x
        self.y       = y
        self.font    = font
        self.w       = w
        self.item_h  = font.get_linesize() + 10
        self.hovered = -1
        sep_count    = sum(1 for i in items if i is None)
        self.h       = len(items) * self.item_h + sep_count * 4

    def rect(self):
        return pygame.Rect(self.x, self.y, self.w, self.h)

    def draw(self, surface, alpha: int = 230):
        bg = pygame.Surface((self.w, self.h), pygame.SRCALPHA)
        bg.fill((*PANEL_BG, alpha))
        pygame.draw.rect(bg, (*BLUE_MID, 255), bg.get_rect(), 1, border_radius=6)
        surface.blit(bg, (self.x, self.y))
        y = self.y
        for i, item in enumerate(self.items):
            if item is None:
                pygame.draw.line(surface, BLUE_DARK,
                                 (self.x + 8, y + 6), (self.x + self.w - 8, y + 6))
                y += 10
                continue
            label, _ = item
            if i == self.hovered:
                hl = pygame.Surface((self.w - 4, self.item_h), pygame.SRCALPHA)
                hl.fill((*BLUE_MID, 160))
                surface.blit(hl, (self.x + 2, y))
            col = TEXT_BRIGHT if i == self.hovered else TEXT_MID
            ls  = self.font.render(label, True, col)
            surface.blit(ls, (self.x + 12, y + self.item_h // 2 - ls.get_height() // 2))
            y += self.item_h

    def handle_mouse(self, pos):
        if not self.rect().collidepoint(pos):
            self.hovered = -1
            return False
        ry = pos[1] - self.y
        idx = 0
        y   = 0
        for i, item in enumerate(self.items):
            if item is None:
                y += 10
                continue
            if y <= ry < y + self.item_h:
                self.hovered = i
                return False
            y += self.item_h
            idx += 1
        self.hovered = -1
        return False

    def handle_click(self, pos) -> bool:
        """Returns True if click was inside menu (consumed)."""
        if not self.rect().collidepoint(pos):
            return False
        y = pos[1] - self.y
        cy = 0
        for item in self.items:
            if item is None:
                cy += 10
                continue
            if cy <= y < cy + self.item_h:
                _, cb = item
                if cb:
                    cb()
                return True
            cy += self.item_h
        return True


class TransparencyPopup:
    """Popup with sliders for bg overlay opacity and panel opacity."""
    W_POP = 300
    H_POP = 160

    def __init__(self, x: int, y: int, fonts: dict,
                 bg_alpha: int, panel_alpha: int,
                 on_bg: callable, on_panel: callable):
        self.x       = x
        self.y       = y
        self.fonts   = fonts
        self.bg_a    = bg_alpha     # 0-255
        self.panel_a = panel_alpha  # 0-255
        self.on_bg   = on_bg
        self.on_panel = on_panel
        self._drag   = None   # 'bg' | 'panel'

    def _slider_rects(self):
        sx = self.x + 20
        sw = self.W_POP - 40
        r_bg    = pygame.Rect(sx, self.y + 55,  sw, 8)
        r_panel = pygame.Rect(sx, self.y + 115, sw, 8)
        return r_bg, r_panel

    def draw(self, surface, font_sm, font_md):
        bg = pygame.Surface((self.W_POP, self.H_POP), pygame.SRCALPHA)
        bg.fill((*PANEL_BG, 245))
        pygame.draw.rect(bg, (*BLUE_MID, 255), bg.get_rect(), 1, border_radius=8)
        surface.blit(bg, (self.x, self.y))

        title = font_md.render("Transparency", True, TEXT_BRIGHT)
        surface.blit(title, (self.x + self.W_POP//2 - title.get_width()//2, self.y + 10))

        r_bg, r_panel = self._slider_rects()
        for label, rect, val in (
            ("Background overlay", r_bg,    self.bg_a),
            ("Panel opacity",      r_panel, self.panel_a),
        ):
            lbl_s = font_sm.render(label + f"  {int(val/255*100)}%", True, TEXT_MID)
            surface.blit(lbl_s, (rect.x, rect.y - lbl_s.get_height() - 2))
            pygame.draw.rect(surface, BLUE_DARK, rect, border_radius=4)
            fill = pygame.Rect(rect.x, rect.y, int(rect.w * val / 255), rect.h)
            pygame.draw.rect(surface, BLUE_LITE, fill, border_radius=4)
            # thumb
            tx = rect.x + int(rect.w * val / 255)
            pygame.draw.circle(surface, WHITE, (tx, rect.centery), 7)

    def handle_mousedown(self, pos):
        r_bg, r_panel = self._slider_rects()
        hit_bg    = pygame.Rect(r_bg.x - 8, r_bg.y - 8,    r_bg.w + 16,    r_bg.h + 16)
        hit_panel = pygame.Rect(r_panel.x - 8, r_panel.y - 8, r_panel.w + 16, r_panel.h + 16)
        if hit_bg.collidepoint(pos):
            self._drag = 'bg'; self._update_slider(pos)
        elif hit_panel.collidepoint(pos):
            self._drag = 'panel'; self._update_slider(pos)

    def handle_mousemove(self, pos):
        if self._drag:
            self._update_slider(pos)

    def handle_mouseup(self):
        self._drag = None

    def _update_slider(self, pos):
        r_bg, r_panel = self._slider_rects()
        rect = r_bg if self._drag == 'bg' else r_panel
        t = max(0.0, min(1.0, (pos[0] - rect.x) / rect.w))
        val = int(t * 255)
        if self._drag == 'bg':
            self.bg_a = val
            self.on_bg(val)
        else:
            self.panel_a = val
            self.on_panel(val)

    def contains(self, pos) -> bool:
        return pygame.Rect(self.x, self.y, self.W_POP, self.H_POP).collidepoint(pos)


class InfoOverlay:
    """Shows file/folder info in a popup."""
    def __init__(self, path: str, font_md, font_sm):
        self.path    = path
        self.font_md = font_md
        self.font_sm = font_sm
        self._lines  = self._build_lines()

    def _build_lines(self):
        p = self.path
        lines = [os.path.basename(p) or p]
        lines.append(f"Path: {p}")
        try:
            st = os.stat(p)
            lines.append(f"Size: {human_size(st.st_size)}")
            lines.append(f"Modified: {format_mtime(st.st_mtime)}")
            lines.append(f"Mode: {stat.filemode(st.st_mode)}")
            if os.path.isdir(p):
                try:
                    n = len(os.listdir(p))
                    lines.append(f"Items: {n}")
                except Exception:
                    pass
        except Exception as e:
            lines.append(f"Error: {e}")
        return lines

    def draw(self, surface, cx, cy):
        w  = 440
        lh = self.font_sm.get_linesize() + 4
        h  = len(self._lines) * lh + 30
        x  = cx - w // 2
        y  = cy - h // 2
        bg = pygame.Surface((w, h), pygame.SRCALPHA)
        bg.fill((*PANEL_BG, 245))
        pygame.draw.rect(bg, (*BLUE_MID, 255), bg.get_rect(), 1, border_radius=8)
        surface.blit(bg, (x, y))
        title = self.font_md.render("File Info", True, TEXT_BRIGHT)
        surface.blit(title, (x + w//2 - title.get_width()//2, y + 6))
        for i, line in enumerate(self._lines):
            col  = TEXT_BRIGHT if i == 0 else TEXT_MID
            s    = self.font_sm.render(line, True, col)
            surface.blit(s, (x + 14, y + 28 + i * lh))

    def rect_bounds(self, cx, cy):
        w = 440
        lh = self.font_sm.get_linesize() + 4
        h = len(self._lines) * lh + 30
        return pygame.Rect(cx - w//2, cy - h//2, w, h)


# ── FileBrowser ───────────────────────────────────────────────────────────────

class FileBrowser:
    VIEW_LIST = "list"
    VIEW_GRID = "grid"

    GRID_COLS   = 5
    GRID_ITEM_W = 150
    GRID_ITEM_H = 140

    def __init__(self, W: int, H: int, fonts: dict):
        self.W = W
        self.H = H
        self.fonts = fonts

        self.font_lg  = fonts.get("lg_b")   or pygame.font.Font(None, 28)
        self.font_md  = fonts.get("md")      or pygame.font.Font(None, 22)
        self.font_sm  = fonts.get("sm")      or pygame.font.Font(None, 20)
        # Icons MUST use a unicode-capable font regardless of what the host passes in.
        # Comfortaa and similar app fonts don't include emoji/symbol glyphs, so we
        # always prefer the dedicated unicode fonts loaded by main().
        self.font_ico = fonts.get("ico")     or fonts.get("ctrl_lg") or pygame.font.Font(None, 28)
        self.font_ism = fonts.get("ico_sm")  or fonts.get("ctrl_md") or pygame.font.Font(None, 22)

        # Navigation state
        self.cwd      = os.path.expanduser("~")
        self.entries  = []       # list of absolute paths
        self.selected = set()    # selected absolute paths
        self.scroll   = 0
        self._load_dir(self.cwd)

        # Clipboard
        self._clip_paths  = []
        self._clip_mode   = None   # 'copy' | 'cut'

        # View
        self.view_mode    = self.VIEW_LIST

        # Background
        self._bg_image    = None
        self._bg_path     = None
        self._bg_overlay_alpha  = 170   # overlay on top of bg image
        self._panel_alpha       = 220   # panel/bar alpha

        # Load persisted preferences (background image path, alpha values)
        _prefs = _load_prefs()
        saved_bg = _prefs.get("bg_path")
        if saved_bg and os.path.isfile(saved_bg):
            surf = self._load_image_scaled(saved_bg)
            if surf:
                self._bg_image = surf
                self._bg_path  = saved_bg
        self._bg_overlay_alpha = _prefs.get("bg_overlay_alpha", 170)
        self._panel_alpha      = _prefs.get("panel_alpha", 220)

        # Overlays / popups
        self._ctx_menu        = None   # PopupMenu
        self._trans_popup     = None   # TransparencyPopup
        self._info_overlay    = None   # InfoOverlay
        self._show_trans      = False

        # Rename in-place (not used — uses tkinter dialog instead)
        self.status_msg   = f"{len(self.entries)} items"
        self._hover_path  = None

        # Double-click tracking
        self._last_click_path  = None
        self._last_click_time  = 0.0

    # ── Directory loading ─────────────────────────────────────────────────────

    def _load_dir(self, path: str):
        try:
            names = os.listdir(path)
        except PermissionError:
            self.status_msg = f"Permission denied: {path}"
            return
        dirs  = sorted([n for n in names if os.path.isdir(os.path.join(path, n))
                        and not n.startswith(".")], key=str.lower)
        files = sorted([n for n in names if not os.path.isdir(os.path.join(path, n))
                        and not n.startswith(".")], key=str.lower)
        self.entries  = [os.path.join(path, n) for n in dirs + files]
        self.scroll   = 0
        self.selected = set()
        self.cwd      = path
        self.status_msg = f"{len(self.entries)} items"

    def _navigate(self, path: str):
        if os.path.isdir(path):
            self._load_dir(path)
        else:
            self._open_file(path)

    def _open_file(self, path: str):
        try:
            if sys.platform.startswith("linux"):
                os.system(f'xdg-open "{path}" &')
            elif sys.platform == "darwin":
                os.system(f'open "{path}" &')
            else:
                os.startfile(path)
            self.status_msg = f"Opened: {os.path.basename(path)}"
        except Exception as e:
            self.status_msg = f"Cannot open: {e}"

    # ── File operations ───────────────────────────────────────────────────────

    def op_new_folder(self):
        def _do():
            root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
            name = simpledialog.askstring("New Folder", "Folder name:", parent=root)
            root.destroy()
            if name:
                p = os.path.join(self.cwd, name)
                try:
                    os.makedirs(p)
                    self._load_dir(self.cwd)
                    self.status_msg = f"Created: {name}"
                except Exception as e:
                    self.status_msg = f"Error: {e}"
        threading.Thread(target=_do, daemon=True).start()

    def op_new_file(self):
        def _do():
            root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
            name = simpledialog.askstring("New File", "File name:", parent=root)
            root.destroy()
            if name:
                p = os.path.join(self.cwd, name)
                try:
                    open(p, "a").close()
                    self._load_dir(self.cwd)
                    self.status_msg = f"Created: {name}"
                except Exception as e:
                    self.status_msg = f"Error: {e}"
        threading.Thread(target=_do, daemon=True).start()

    def op_open(self):
        target = list(self.selected)
        if not target and self.entries:
            return
        for p in target:
            self._navigate(p)

    def op_rename(self):
        if not self.selected:
            self.status_msg = "Select a file to rename"
            return
        path = next(iter(self.selected))
        def _do():
            root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
            old  = os.path.basename(path)
            new  = simpledialog.askstring("Rename", "New name:", initialvalue=old, parent=root)
            root.destroy()
            if new and new != old:
                dest = os.path.join(os.path.dirname(path), new)
                try:
                    os.rename(path, dest)
                    self._load_dir(self.cwd)
                    self.status_msg = f"Renamed → {new}"
                except Exception as e:
                    self.status_msg = f"Rename failed: {e}"
        threading.Thread(target=_do, daemon=True).start()

    def op_copy(self):
        if not self.selected:
            self.status_msg = "Nothing selected"
            return
        self._clip_paths = list(self.selected)
        self._clip_mode  = "copy"
        self.status_msg  = f"Copied {len(self._clip_paths)} item(s)"

    def op_cut(self):
        if not self.selected:
            self.status_msg = "Nothing selected"
            return
        self._clip_paths = list(self.selected)
        self._clip_mode  = "cut"
        self.status_msg  = f"Cut {len(self._clip_paths)} item(s)"

    def op_paste(self):
        if not self._clip_paths:
            self.status_msg = "Clipboard is empty"
            return
        def _do():
            errs = []
            for src in self._clip_paths:
                name = os.path.basename(src)
                dest = os.path.join(self.cwd, name)
                # avoid overwriting itself
                if os.path.abspath(src) == os.path.abspath(dest):
                    base, ext = os.path.splitext(name)
                    dest = os.path.join(self.cwd, f"{base}_copy{ext}")
                try:
                    if os.path.isdir(src):
                        shutil.copytree(src, dest)
                        if self._clip_mode == "cut":
                            shutil.rmtree(src)
                    else:
                        shutil.copy2(src, dest)
                        if self._clip_mode == "cut":
                            os.remove(src)
                except Exception as e:
                    errs.append(str(e))
            if self._clip_mode == "cut":
                self._clip_paths = []
                self._clip_mode  = None
            self._load_dir(self.cwd)
            self.status_msg = f"Pasted {len(self._clip_paths or []) or 'items'}" \
                              + (f"  ({len(errs)} errors)" if errs else "")
        threading.Thread(target=_do, daemon=True).start()

    def op_move(self):
        """Move selected files to a chosen destination folder."""
        if not self.selected:
            self.status_msg = "Nothing selected"
            return
        def _do():
            root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
            dest_dir = filedialog.askdirectory(title="Move to…", parent=root)
            root.destroy()
            if not dest_dir:
                return
            errs = []
            for src in list(self.selected):
                try:
                    shutil.move(src, os.path.join(dest_dir, os.path.basename(src)))
                except Exception as e:
                    errs.append(str(e))
            self._load_dir(self.cwd)
            self.status_msg = "Moved" + (f"  ({len(errs)} errors)" if errs else "")
        threading.Thread(target=_do, daemon=True).start()

    def op_delete(self):
        if not self.selected:
            self.status_msg = "Nothing selected"
            return
        names = ", ".join(os.path.basename(p) for p in list(self.selected)[:3])
        if len(self.selected) > 3:
            names += f" and {len(self.selected)-3} more"
        def _do():
            root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
            ok = messagebox.askyesno(
                "Delete", f"Permanently delete:\n{names}?", parent=root)
            root.destroy()
            if ok:
                errs = []
                for p in list(self.selected):
                    try:
                        if os.path.isdir(p):
                            shutil.rmtree(p)
                        else:
                            os.remove(p)
                    except Exception as e:
                        errs.append(str(e))
                self._load_dir(self.cwd)
                self.status_msg = "Deleted" + (f"  ({len(errs)} errors)" if errs else "")
        threading.Thread(target=_do, daemon=True).start()

    def op_info(self):
        path = next(iter(self.selected)) if self.selected else self.cwd
        self._info_overlay = InfoOverlay(path, self.font_md, self.font_sm)

    def op_extract(self):
        if not self.selected:
            self.status_msg = "Select an archive to extract"
            return
        path = next(iter(self.selected))
        ext  = os.path.splitext(path)[1].lower()
        if ext not in ARCHIVE_EXT and not path.lower().endswith(".tar.gz") \
                and not path.lower().endswith(".tar.bz2"):
            self.status_msg = "Not a supported archive"
            return
        def _do():
            dest = os.path.splitext(path)[0]
            os.makedirs(dest, exist_ok=True)
            try:
                if path.lower().endswith(".zip"):
                    with zipfile.ZipFile(path, "r") as z:
                        z.extractall(dest)
                elif path.lower().endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar")):
                    import tarfile
                    with tarfile.open(path) as t:
                        t.extractall(dest)
                elif path.lower().endswith(".gz") and not path.lower().endswith(".tar.gz"):
                    import gzip
                    out = os.path.join(dest, os.path.basename(path)[:-3])
                    with gzip.open(path, "rb") as gz, open(out, "wb") as f:
                        shutil.copyfileobj(gz, f)
                elif path.lower().endswith((".rar",)):
                    # Try unrar / python-rarfile
                    try:
                        import rarfile
                        with rarfile.RarFile(path) as r:
                            r.extractall(dest)
                    except ImportError:
                        os.system(f'unrar x "{path}" "{dest}" &')
                elif path.lower().endswith(".7z"):
                    os.system(f'7z x "{path}" -o"{dest}" &')
                self._load_dir(self.cwd)
                self.status_msg = f"Extracted → {os.path.basename(dest)}"
            except Exception as e:
                self.status_msg = f"Extract error: {e}"
        threading.Thread(target=_do, daemon=True).start()

    # ── Background / appearance helpers ──────────────────────────────────────

    def open_bg_image(self):
        def _pick():
            root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
            path = filedialog.askopenfilename(
                title="Select Background Image",
                filetypes=[("Images", "*.jpg *.jpeg *.png *.bmp *.gif *.webp"),
                           ("All files", "*.*")])
            root.destroy()
            if path:
                surf = self._load_image_scaled(path)
                if surf:
                    self._bg_image = surf
                    self._bg_path  = path
                    self.status_msg = f"BG: {os.path.basename(path)}"
                    _save_prefs({"bg_path": path,
                                 "bg_overlay_alpha": self._bg_overlay_alpha,
                                 "panel_alpha": self._panel_alpha})
                else:
                    self.status_msg = "Could not load image"
        threading.Thread(target=_pick, daemon=True).start()

    def clear_bg_image(self):
        self._bg_image = None
        self._bg_path  = None
        self.status_msg = "Background cleared"
        _save_prefs({"bg_path": None,
                     "bg_overlay_alpha": self._bg_overlay_alpha,
                     "panel_alpha": self._panel_alpha})

    def _load_image_scaled(self, path: str):
        try:
            raw   = pygame.image.load(path).convert()
            iw, ih = raw.get_size()
            scale = max(self.W / iw, self.H / ih)
            nw, nh = int(iw * scale), int(ih * scale)
            scaled = pygame.transform.smoothscale(raw, (nw, nh))
            surf   = pygame.Surface((self.W, self.H))
            surf.blit(scaled, ((self.W - nw) // 2, (self.H - nh) // 2))
            return surf
        except Exception:
            return None

    def toggle_transparency_popup(self, anchor_x: int, anchor_y: int):
        if self._show_trans:
            self._show_trans  = False
            self._trans_popup = None
        else:
            self._show_trans  = True
            self._trans_popup = TransparencyPopup(
                anchor_x, anchor_y + 4,
                self.fonts,
                self._bg_overlay_alpha, self._panel_alpha,
                on_bg    = lambda v: setattr(self, "_bg_overlay_alpha", v),
                on_panel = lambda v: setattr(self, "_panel_alpha", v),
            )

    # ── Layout helpers ────────────────────────────────────────────────────────

    def _content_rect(self) -> pygame.Rect:
        y = BAR_H + TOOL_H + PATH_H
        h = self.H - y - BOT_H
        return pygame.Rect(0, y, self.W, h)

    # ── Toolbar buttons ───────────────────────────────────────────────────────

    # Each button: (label, callback, [is_active_fn])
    def _toolbar_buttons(self):
        sel = bool(self.selected)
        clip = bool(self._clip_paths)
        has_sel = bool(self.selected)
        first_sel = next(iter(self.selected), None)
        is_archive = first_sel and os.path.splitext(first_sel)[1].lower() in ARCHIVE_EXT

        return [
            # Navigation
            ("Up",       self._cmd_go_up,      None),
            ("Root",     self._cmd_go_root,    None),
            None,
            # View controls
            ("List",     self._cmd_view_list,  lambda: self.view_mode == self.VIEW_LIST),
            ("Grid",     self._cmd_view_grid,  lambda: self.view_mode == self.VIEW_GRID),
            None,
            # Appearance
            ("BG Image", self.open_bg_image,   None),
            ("Clear BG", self.clear_bg_image,  None),
            ("Alpha",    self._cmd_trans,       lambda: self._show_trans),
            None,
            # File ops
            ("New Folder", self.op_new_folder, None),
            ("New File",   self.op_new_file,   None),
            ("Open",       self.op_open,       None),
            ("Rename",     self.op_rename,     None),
            ("Copy",       self.op_copy,       None),
            ("Cut",        self.op_cut,        None),
            ("Paste",      self.op_paste,      None),
            ("Move",       self.op_move,       None),
            ("Delete",     self.op_delete,     None),
            ("Info",       self.op_info,       None),
            ("Extract",    self.op_extract,    None),
        ]

    def _cmd_go_up(self):
        parent = os.path.dirname(self.cwd)
        if parent != self.cwd:
            self._navigate(parent)

    def _cmd_go_root(self):
        """Navigate to the filesystem root (/ on Linux/macOS, drive root on Windows)."""
        if sys.platform == "win32":
            # Go to "This PC" — list drive letters
            self._load_root_windows()
        else:
            self._navigate("/")

    def _load_root_windows(self):
        """Populate entries with all available drive letters on Windows."""
        import string
        drives = []
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                drives.append(drive)
        self.entries  = drives
        self.scroll   = 0
        self.selected = set()
        self.cwd      = "This PC"
        self.status_msg = f"{len(drives)} drives"

    def _cmd_view_list(self):
        self.view_mode = self.VIEW_LIST
    def _cmd_view_grid(self):
        self.view_mode = self.VIEW_GRID
    def _cmd_trans(self):
        # called from click handler with anchor coords
        pass   # handled in handle_click

    # ── Input handling ────────────────────────────────────────────────────────

    def handle_key(self, key: int, mods: int):
        ctrl  = bool(mods & pygame.KMOD_CTRL)
        shift = bool(mods & pygame.KMOD_SHIFT)

        # Close popups first
        if key == pygame.K_ESCAPE:
            if self._info_overlay:
                self._info_overlay = None; return
            if self._show_trans:
                self._show_trans   = False
                self._trans_popup  = None; return
            if self._ctx_menu:
                self._ctx_menu = None; return
            self.selected.clear(); return

        if ctrl:
            if key == pygame.K_c:  self.op_copy()
            elif key == pygame.K_x: self.op_cut()
            elif key == pygame.K_v: self.op_paste()
            elif key == pygame.K_a: self.selected = set(self.entries)
            elif key == pygame.K_z: self.selected.clear()
            return

        if key == pygame.K_RETURN:
            if self.selected:
                for p in list(self.selected):
                    self._navigate(p)
        elif key == pygame.K_BACKSPACE:
            parent = os.path.dirname(self.cwd)
            if parent != self.cwd:
                self._navigate(parent)
        elif key == pygame.K_F2:
            self.op_rename()
        elif key == pygame.K_DELETE:
            self.op_delete()
        elif key == pygame.K_UP:
            self.scroll = max(0, self.scroll - 1)
        elif key == pygame.K_DOWN:
            self.scroll += 1

    def handle_click(self, pos):
        # ── Close popups if clicking outside ─────────────────────────────────
        if self._info_overlay:
            self._info_overlay = None; return
        if self._ctx_menu:
            consumed = self._ctx_menu.handle_click(pos)
            self._ctx_menu = None; return

        if self._show_trans and self._trans_popup:
            if self._trans_popup.contains(pos):
                self._trans_popup.handle_mousedown(pos)
                return
            else:
                self._show_trans = False; self._trans_popup = None

        # ── Toolbar ───────────────────────────────────────────────────────────
        tool_y = BAR_H
        if tool_y <= pos[1] < tool_y + TOOL_H:
            self._handle_toolbar_click(pos)
            return

        # ── Breadcrumb ────────────────────────────────────────────────────────
        bread_y = BAR_H + TOOL_H
        if bread_y <= pos[1] < bread_y + PATH_H:
            self._handle_breadcrumb_click(pos)
            return

        # ── Content area ──────────────────────────────────────────────────────
        cr = self._content_rect()
        if cr.collidepoint(pos):
            path = self._path_at(pos)
            if path:
                now = time.time()
                mods = pygame.key.get_mods()
                ctrl = bool(mods & pygame.KMOD_CTRL)
                if ctrl:
                    if path in self.selected:
                        self.selected.discard(path)
                    else:
                        self.selected.add(path)
                else:
                    # Double-click to open
                    if (path == self._last_click_path and
                            now - self._last_click_time < 0.45):
                        self._navigate(path)
                        self._last_click_path = None
                        return
                    self.selected = {path}
                    self._last_click_path = path
                    self._last_click_time = now

    def handle_rightclick(self, pos):
        cr = self._content_rect()
        if not cr.collidepoint(pos):
            return
        path = self._path_at(pos)
        if path and path not in self.selected:
            self.selected = {path}
        items = [
            ("▶ Open",         self.op_open),
            ("✏ Rename",        self.op_rename),
            None,
            ("⎘ Copy",          self.op_copy),
            ("✂ Cut",           self.op_cut),
            ("⎘ Paste here",    self.op_paste),
            ("⇥ Move to…",      self.op_move),
            None,
            ("✕ Delete",        self.op_delete),
            None,
            ("📦 Extract",       self.op_extract),
            ("ℹ Info",           self.op_info),
        ]
        # Clamp to screen
        mx = min(pos[0], self.W - 196)
        my = min(pos[1], self.H - 350)
        self._ctx_menu = PopupMenu(items, mx, my, self.font_sm)

    def handle_scroll(self, dy: int):
        self.scroll = max(0, self.scroll - dy * 2)

    def handle_mousemotion(self, pos):
        if self._show_trans and self._trans_popup:
            if pygame.mouse.get_pressed()[0]:
                self._trans_popup.handle_mousemove(pos)
        cr = self._content_rect()
        if cr.collidepoint(pos):
            self._hover_path = self._path_at(pos)
        else:
            self._hover_path = None
        if self._ctx_menu:
            self._ctx_menu.handle_mouse(pos)

    def handle_mouseup(self):
        if self._trans_popup:
            self._trans_popup.handle_mouseup()

    # ── Toolbar click ─────────────────────────────────────────────────────────

    def _handle_toolbar_click(self, pos):
        btns   = self._toolbar_buttons()
        x      = 8
        btn_h  = TOOL_H - 8
        btn_y  = BAR_H + 4
        for btn in btns:
            if btn is None:
                x += 10; continue
            label, cb, active_fn = btn
            w = self.font_sm.size(label)[0] + 16
            rect = pygame.Rect(x, btn_y, w, btn_h)
            if rect.collidepoint(pos):
                if label.startswith("◑"):
                    # Special: toggle transparency popup anchored to button
                    if self._show_trans:
                        self._show_trans = False
                        self._trans_popup = None
                    else:
                        self._show_trans  = True
                        def _on_bg(v, b=self):
                            b._bg_overlay_alpha = v
                            _save_prefs({"bg_path": b._bg_path,
                                         "bg_overlay_alpha": v,
                                         "panel_alpha": b._panel_alpha})
                        def _on_panel(v, b=self):
                            b._panel_alpha = v
                            _save_prefs({"bg_path": b._bg_path,
                                         "bg_overlay_alpha": b._bg_overlay_alpha,
                                         "panel_alpha": v})
                        self._trans_popup = TransparencyPopup(
                            rect.x, rect.bottom + 2,
                            self.fonts,
                            self._bg_overlay_alpha, self._panel_alpha,
                            on_bg    = _on_bg,
                            on_panel = _on_panel,
                        )
                else:
                    cb()
                return
            x += w + 4

    # ── Breadcrumb click ──────────────────────────────────────────────────────

    def _handle_breadcrumb_click(self, pos):
        parts   = Path(self.cwd).parts
        x       = 14
        bread_y = BAR_H + TOOL_H
        for i, part in enumerate(parts):
            sep = "  ›" if i < len(parts) - 1 else ""
            w = self.font_md.size(part + sep)[0] + 6
            rect = pygame.Rect(x, bread_y, w, PATH_H)
            if rect.collidepoint(pos):
                dest = str(Path(*parts[:i+1]))
                if dest != self.cwd:
                    self._navigate(dest)
                return
            x += w

    # ── Item hit-testing ──────────────────────────────────────────────────────

    def _path_at(self, pos) -> str | None:
        cr = self._content_rect()
        if self.view_mode == self.VIEW_LIST:
            item_h = self.font_md.get_linesize() + 10
            n_vis  = max(1, cr.height // item_h)
            ry     = pos[1] - cr.y
            idx    = self.scroll + ry // item_h
            if 0 <= idx < len(self.entries):
                return self.entries[idx]
        else:  # grid
            cols   = max(1, cr.width // self.GRID_ITEM_W)
            col    = (pos[0]) // self.GRID_ITEM_W
            row    = (pos[1] - cr.y) // self.GRID_ITEM_H
            idx    = self.scroll + row * cols + col
            if 0 <= idx < len(self.entries):
                return self.entries[idx]
        return None

    # ── Drawing ───────────────────────────────────────────────────────────────

    def draw(self, surface: pygame.Surface):
        surface.fill(DARK_BG)

        # Background image
        if self._bg_image:
            if self._bg_image.get_size() != (self.W, self.H):
                self._bg_image = pygame.transform.smoothscale(
                    self._bg_image, (self.W, self.H))
            surface.blit(self._bg_image, (0, 0))
            overlay = pygame.Surface((self.W, self.H), pygame.SRCALPHA)
            overlay.fill((0, 0, 0, self._bg_overlay_alpha))
            surface.blit(overlay, (0, 0))

        self._draw_top_bar(surface)
        self._draw_toolbar(surface)
        self._draw_breadcrumb(surface)
        self._draw_content(surface)
        self._draw_bottom_bar(surface)

        # Overlays (rendered on top of everything)
        if self._ctx_menu:
            self._ctx_menu.draw(surface, self._panel_alpha)
        if self._show_trans and self._trans_popup:
            self._trans_popup.draw(surface, self.font_sm, self.font_md)
        if self._info_overlay:
            self._info_overlay.draw(surface, self.W // 2, self.H // 2)

    def _draw_top_bar(self, surface):
        bar = pygame.Surface((self.W, BAR_H), pygame.SRCALPHA)
        bar.fill((10, 14, 24, self._panel_alpha))
        surface.blit(bar, (0, 0))
        # Use unicode font for the emoji, then Comfortaa for the rest
        title = self.font_ico.render("📁", True, TEXT_BRIGHT)
        surface.blit(title, (12, (BAR_H - title.get_height()) // 2))
        label = self.font_md.render(" LoomOS File Browser", True, TEXT_BRIGHT)
        surface.blit(label, (12 + title.get_width(), (BAR_H - label.get_height()) // 2))
        hint = self.font_sm.render(
            "Enter open  Backspace up  Del delete  Ctrl+C/X/V  Ctrl+A select all  F2 rename  RClick menu",
            True, TEXT_DIM)
        surface.blit(hint, (self.W - hint.get_width() - 12,
                             (BAR_H - hint.get_height()) // 2))

    def _draw_toolbar(self, surface):
        bar = pygame.Surface((self.W, TOOL_H), pygame.SRCALPHA)
        bar.fill((12, 18, 32, self._panel_alpha))
        surface.blit(bar, (0, BAR_H))

        btns  = self._toolbar_buttons()
        x     = 8
        btn_h = TOOL_H - 8
        btn_y = BAR_H + 4

        for btn in btns:
            if btn is None:
                # separator
                sep_x = x + 3
                pygame.draw.line(surface, BLUE_DARK,
                                 (sep_x, btn_y + 4), (sep_x, btn_y + btn_h - 4))
                x += 10
                continue
            label, cb, active_fn = btn
            w = self.font_sm.size(label)[0] + 16
            rect = pygame.Rect(x, btn_y, w, btn_h)
            active = active_fn() if active_fn else False
            bg_col = (*BLUE_MID, 200) if active else (*BLUE_DARK, 180)
            bs = pygame.Surface((w, btn_h), pygame.SRCALPHA)
            bs.fill(bg_col)
            pygame.draw.rect(bs, (*BLUE_MID, 220), bs.get_rect(), 1, border_radius=5)
            surface.blit(bs, (rect.x, rect.y))
            ls = self.font_sm.render(label, True, TEXT_BRIGHT)
            surface.blit(ls, (rect.x + w // 2 - ls.get_width() // 2,
                               rect.y + btn_h // 2 - ls.get_height() // 2))
            x += w + 4

    def _draw_breadcrumb(self, surface):
        by = BAR_H + TOOL_H
        bar = pygame.Surface((self.W, PATH_H), pygame.SRCALPHA)
        bar.fill((8, 12, 22, self._panel_alpha))
        surface.blit(bar, (0, by))

        parts = Path(self.cwd).parts
        x = 14
        for i, part in enumerate(parts):
            sep = "  ›" if i < len(parts) - 1 else ""
            col = TEXT_MID if i < len(parts) - 1 else TEXT_BRIGHT
            txt = self.font_md.render(part + sep, True, col)
            ty  = by + PATH_H // 2 - txt.get_height() // 2
            surface.blit(txt, (x, ty))
            x += txt.get_width() + 6
            if x > self.W - 20:
                break

    def _draw_content(self, surface):
        cr  = self._content_rect()
        # Panel background
        pb = pygame.Surface((cr.width, cr.height), pygame.SRCALPHA)
        pb.fill((*DARK_BG, self._panel_alpha))
        surface.blit(pb, cr.topleft)

        if self.view_mode == self.VIEW_LIST:
            self._draw_list(surface, cr)
        else:
            self._draw_grid(surface, cr)

    def _draw_list(self, surface, cr: pygame.Rect):
        item_h = self.font_md.get_linesize() + 10
        n_vis  = max(1, cr.height // item_h)
        # Clamp scroll
        self.scroll = max(0, min(self.scroll, max(0, len(self.entries) - n_vis)))

        for i in range(n_vis):
            idx = self.scroll + i
            if idx >= len(self.entries): break
            path = self.entries[idx]
            name = os.path.basename(path)
            y    = cr.y + i * item_h

            is_sel   = path in self.selected
            is_hover = path == self._hover_path
            is_dir   = os.path.isdir(path)

            # Highlight strip
            if is_sel:
                hl = pygame.Surface((cr.width, item_h), pygame.SRCALPHA)
                hl.fill((*BLUE_MID, 120))
                surface.blit(hl, (cr.x, y))
            elif is_hover:
                hl = pygame.Surface((cr.width, item_h), pygame.SRCALPHA)
                hl.fill((*BLUE_DARK, 100))
                surface.blit(hl, (cr.x, y))

            # Icon — always use unicode-capable font
            ico = self.font_ico.render(file_icon(path), True, TEXT_BRIGHT)
            surface.blit(ico, (cr.x + 6, y + item_h // 2 - ico.get_height() // 2))

            # Name
            col = ORANGE_LITE if is_dir else (TEXT_BRIGHT if is_sel else TEXT_MID)
            ns  = self.font_md.render(name[:60], True, col)
            surface.blit(ns, (cr.x + 56, y + item_h // 2 - ns.get_height() // 2))

            # Size / date (right-aligned)
            try:
                st = os.stat(path)
                meta = f"{human_size(st.st_size):>10}   {format_mtime(st.st_mtime)}"
            except Exception:
                meta = ""
            ms = self.font_sm.render(meta, True, TEXT_DIM)
            surface.blit(ms, (cr.right - ms.get_width() - 10,
                               y + item_h // 2 - ms.get_height() // 2))

            # Divider
            if i > 0:
                pygame.draw.line(surface, (*BLUE_DARK, 80),
                                 (cr.x, y), (cr.right, y))

    def _draw_grid(self, surface, cr: pygame.Rect):
        cols   = max(1, cr.width // self.GRID_ITEM_W)
        n_rows = math.ceil(len(self.entries) / cols) if self.entries else 1
        # Clamp scroll (in rows)
        vis_rows = max(1, cr.height // self.GRID_ITEM_H)
        self.scroll = max(0, min(self.scroll, max(0, n_rows - vis_rows) * cols))

        start_row = self.scroll // cols if cols else 0
        for row in range(vis_rows + 1):
            for col in range(cols):
                idx = (start_row + row) * cols + col
                if idx >= len(self.entries): break
                path   = self.entries[idx]
                name   = os.path.basename(path)
                is_sel = path in self.selected
                is_hov = path == self._hover_path
                is_dir = os.path.isdir(path)

                gx = cr.x + col * self.GRID_ITEM_W
                gy = cr.y + row * self.GRID_ITEM_H

                # Cell bg
                if is_sel:
                    cell = pygame.Surface((self.GRID_ITEM_W - 4, self.GRID_ITEM_H - 4),
                                          pygame.SRCALPHA)
                    cell.fill((*BLUE_MID, 120))
                    pygame.draw.rect(cell, (*BLUE_LITE, 200), cell.get_rect(), 1, border_radius=6)
                    surface.blit(cell, (gx + 2, gy + 2))
                elif is_hov:
                    cell = pygame.Surface((self.GRID_ITEM_W - 4, self.GRID_ITEM_H - 4),
                                          pygame.SRCALPHA)
                    cell.fill((*BLUE_DARK, 100))
                    pygame.draw.rect(cell, (*BLUE_MID, 160), cell.get_rect(), 1, border_radius=6)
                    surface.blit(cell, (gx + 2, gy + 2))

                # Icon (large) — rendered with dedicated unicode font so emoji always show
                ico = self.font_ico.render(file_icon(path), True, TEXT_BRIGHT)
                ix  = gx + self.GRID_ITEM_W // 2 - ico.get_width() // 2
                iy  = gy + 18
                surface.blit(ico, (ix, iy))

                # Name — truncated, centred, using regular font
                col_txt = ORANGE_LITE if is_dir else TEXT_MID
                max_w   = self.GRID_ITEM_W - 12
                ns      = self.font_sm.render(name[:20], True, col_txt)
                if ns.get_width() > max_w:
                    ns = self.font_sm.render(name[:16] + "…", True, col_txt)
                nx = gx + self.GRID_ITEM_W // 2 - ns.get_width() // 2
                surface.blit(ns, (nx, gy + self.GRID_ITEM_H - 26))

    def _draw_bottom_bar(self, surface):
        by  = self.H - BOT_H
        bar = pygame.Surface((self.W, BOT_H), pygame.SRCALPHA)
        bar.fill((10, 14, 24, self._panel_alpha))
        surface.blit(bar, (0, by))

        n_sel = len(self.selected)
        left  = f"{len(self.entries)} items" + (f"  ·  {n_sel} selected" if n_sel else "")
        if self._clip_paths:
            left += f"  ·  {len(self._clip_paths)} in clipboard ({self._clip_mode})"
        ls = self.font_sm.render(left, True, TEXT_DIM)
        surface.blit(ls, (12, by + (BOT_H - ls.get_height()) // 2))

        ss = self.font_sm.render(self.status_msg[:80], True, TEXT_MID)
        surface.blit(ss, (self.W // 2 - ss.get_width() // 2,
                           by + (BOT_H - ss.get_height()) // 2))

        view_lbl = f"View: {'List' if self.view_mode == self.VIEW_LIST else 'Grid'}"
        vs = self.font_sm.render(view_lbl, True, TEXT_DIM)
        surface.blit(vs, (self.W - vs.get_width() - 12,
                           by + (BOT_H - vs.get_height()) // 2))


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    pygame.init()

    info   = pygame.display.Info()
    W, H   = info.current_w, info.current_h
    screen = pygame.display.set_mode((W, H), pygame.NOFRAME)
    pygame.display.set_caption("LoomOS File Browser")
    clock  = pygame.time.Clock()

    def _load_font(name, size):
        try:
            p = pygame.font.match_font(name)
            if p: return pygame.font.Font(p, size)
        except Exception:
            pass
        return pygame.font.Font(None, size)

    def _load_icon_font(size):
        """
        Load a font that reliably renders the emoji/symbol codepoints used as
        file-type icons.  We bypass pygame.font.match_font() entirely and probe
        known absolute file paths, because match_font does fuzzy name matching
        and frequently returns a font with no glyph coverage for these chars.

        Priority:
          Windows -> Segoe UI Emoji  (ships with every modern Windows install)
                  -> Segoe UI Symbol (older Windows fallback)
          Linux   -> Noto Color Emoji / Noto Emoji
                  -> DejaVu Sans  (broad coverage, on virtually every distro)
          macOS   -> Apple Color Emoji
        """
        import sys as _sys
        win_paths = [
            r"C:\Windows\Fonts\seguiemj.ttf",   # Segoe UI Emoji
            r"C:\Windows\Fonts\seguisym.ttf",   # Segoe UI Symbol
            r"C:\Windows\Fonts\segoeui.ttf",    # Segoe UI (partial)
        ]
        nix_paths = [
            "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
            "/usr/share/fonts/noto/NotoColorEmoji.ttf",
            "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf",
            "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
            "/usr/share/fonts/noto/NotoEmoji-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
        ]
        mac_paths = [
            "/System/Library/Fonts/Apple Color Emoji.ttc",
            "/System/Library/Fonts/Supplemental/Arial Unicode MS.ttf",
        ]
        if _sys.platform == "win32":
            candidates = win_paths + nix_paths + mac_paths
        elif _sys.platform == "darwin":
            candidates = mac_paths + nix_paths + win_paths
        else:
            candidates = nix_paths + win_paths + mac_paths

        for path in candidates:
            if os.path.isfile(path):
                try:
                    return pygame.font.Font(path, size)
                except Exception:
                    pass
        return pygame.font.Font(None, size)

    fonts = {
        "lg_b":   _load_font("Comfortaa", 26),
        "md":     _load_font("Comfortaa", 22),
        "sm":     _load_font("Comfortaa", 19),
        # Icon fonts are loaded via direct file paths, completely independent
        # of the SDK/app font stack, to guarantee emoji glyph coverage.
        "ico":    _load_icon_font(32),   # grid view large icon
        "ico_sm": _load_icon_font(22),   # list view + toolbar icons
    }

    # ── Startup diagnostic — log which icon font was resolved ───────────────
    def _font_path(f):
        try:    return getattr(f, "path", None) or str(f)
        except: return "unknown"
    print(f"[FileBrowser] icon font (32pt): {_font_path(fonts['ico'])}")
    print(f"[FileBrowser] icon font (22pt): {_font_path(fonts['ico_sm'])}")

    browser = FileBrowser(W, H, fonts)

    _send_gui({"type": "app_ready", "manifest": LOOMOS_APP, "pid": os.getpid()})
    _send_gui({"type": "app_command_log", "text": "File Browser ready"})

    running = True
    while running:
        clock.tick(30)
        browser.W = W; browser.H = H

        # IPC
        with _ipc_lock:
            msgs = list(_ipc_queue); _ipc_queue.clear()
        for msg in msgs:
            t = msg.get("type", "")
            if t == "quit":
                running = False
            elif t == "voice_command":
                cmd = msg.get("text", "").lower().strip()
                if cmd in ("go up", "up", "back", "parent"):
                    browser._navigate(os.path.dirname(browser.cwd))
                elif cmd in ("home",):
                    browser._navigate(os.path.expanduser("~"))
                elif cmd in ("list view", "list"):
                    browser.view_mode = FileBrowser.VIEW_LIST
                elif cmd in ("grid view", "grid"):
                    browser.view_mode = FileBrowser.VIEW_GRID
                elif cmd.startswith("go to "):
                    dest = os.path.expanduser(cmd[6:].strip())
                    if os.path.isdir(dest):
                        browser._navigate(dest)
                elif cmd in ("new folder",):
                    browser.op_new_folder()
                elif cmd in ("delete", "delete selected"):
                    browser.op_delete()
                elif cmd in ("copy",):
                    browser.op_copy()
                elif cmd in ("paste",):
                    browser.op_paste()
                elif cmd in ("rename",):
                    browser.op_rename()
                elif cmd in ("select all",):
                    browser.selected = set(browser.entries)

        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.VIDEORESIZE:
                W, H   = ev.w, ev.h
                screen = pygame.display.set_mode((W, H), pygame.RESIZABLE)
            elif ev.type == pygame.KEYDOWN:
                mods = pygame.key.get_mods()
                ctrl = bool(mods & pygame.KMOD_CTRL)
                if ctrl and ev.key == pygame.K_q:
                    running = False
                else:
                    browser.handle_key(ev.key, mods)
            elif ev.type == pygame.MOUSEBUTTONDOWN:
                if ev.button == 1:
                    browser.handle_click(ev.pos)
                elif ev.button == 3:
                    browser.handle_rightclick(ev.pos)
            elif ev.type == pygame.MOUSEBUTTONUP:
                if ev.button == 1:
                    browser.handle_mouseup()
            elif ev.type == pygame.MOUSEMOTION:
                browser.handle_mousemotion(ev.pos)
            elif ev.type == pygame.MOUSEWHEEL:
                browser.handle_scroll(ev.y)

        browser.draw(screen)
        pygame.display.flip()

    _send_gui({"type": "app_closed", "name": LOOMOS_APP["name"]})
    pygame.quit()

if __name__ == "__main__":
    main()
