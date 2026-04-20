#!/usr/bin/env python3
"""
LoomOS Text Editor — built-in app  v0.2
Lightweight: re-uses system pygame fonts, no extra imports beyond stdlib.
Receives key_input and dictation_text from loomos_gui via IPC.

Changes from v0.1:
  - Save / CTRL+S / spoken "save" all open a Save-As dialog every time
    (no silent auto-naming).  On re-save of an already-named file
    CTRL+SHIFT+S saves in-place; plain CTRL+S / button still prompts.
  - Save-As dialog: keyboard entry for filename, folder-shortcut buttons
    (Home, Documents, Desktop, Downloads), live path preview,
    ENTER to confirm, ESC cancel.
"""

LOOMOS_APP = {
    "name":             "Text Editor",
    "description":      "Simple text editor",
    "icon":             "📝",
    "keywords":         ["text editor", "editor", "notepad", "write", "text"],
    "singleton":        True,
    "ipc_port":         47850,
    "accepts_keys":     True,
    "accepts_dictation":True,
}

import pygame, os, sys, json, socket, threading, time
from pathlib import Path

# ── IPC ──────────────────────────────────────────────────────────────────────

PORT      = LOOMOS_APP["ipc_port"]
GUI_PORT  = 47842
SOCK_PATH = "/tmp/loomos_gui.sock"

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

# ── Colours ───────────────────────────────────────────────────────────────────

DARK_BG     = (  8, 12, 20)
BLUE_MID    = ( 30, 80,130)
BLUE_LITE   = ( 80,160,210)
BLUE_DARK   = ( 15, 35, 65)
TEXT_BRIGHT = (230,240,255)
TEXT_DIM    = (120,130,150)
TEXT_MID    = (180,190,200)
GREEN_LITE  = ( 80,200, 80)
ORANGE_LITE = (230,140, 50)
RED_LITE    = (220, 60, 60)
RED_MID     = (160, 20, 20)
DIALOG_BDR  = ( 40, 90,150)
BAR_H       = 62   # taller bar: hint row + button row


# ── Save-As Dialog ────────────────────────────────────────────────────────────

class SaveAsDialog:
    """
    Pygame-rendered modal Save-As dialog.

    ENTER/Save button confirms.  ESC/Cancel button dismisses.
    Left column: folder-shortcut buttons (Home, Documents, Desktop, Downloads).
    Right column: editable filename field + live full-path preview.
    """

    FOLDER_SHORTCUTS = [
        ("🏠 Home",       lambda: str(Path.home())),
        ("📄 Documents",  lambda: str(Path.home() / "Documents")),
        ("🖥  Desktop",    lambda: str(Path.home() / "Desktop")),
        ("💾 Downloads",  lambda: str(Path.home() / "Downloads")),
    ]

    def __init__(self, fonts: dict):
        self.fonts   = fonts
        self.font    = fonts.get("md")   or pygame.font.Font(None, 18)
        self.font_sm = fonts.get("md")   or pygame.font.Font(None, 16)
        self.font_lg = fonts.get("lg_b") or fonts.get("md") or pygame.font.Font(None, 22)

        self.visible    = False
        self._folder    = str(Path.home())
        self._name      = "untitled.txt"
        self._cursor    = len(self._name)
        self._error     = ""
        self._on_save   = None
        self._blink_t   = 0.0

        # Hit-rects set each draw call
        self._folder_rects = []   # [(pygame.Rect, folder_path_str), ...]
        self._confirm_rect = None
        self._cancel_rect  = None

    # ── Public ────────────────────────────────────────────────────────────────

    def open(self, current_path, on_save):
        if current_path:
            self._folder = str(Path(current_path).parent)
            self._name   = Path(current_path).name
        else:
            self._folder = str(Path.home())
            self._name   = f"document_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        self._cursor  = len(self._name)
        self._error   = ""
        self._on_save = on_save
        self.visible  = True

    def close(self):
        self.visible  = False
        self._on_save = None

    # ── Input ─────────────────────────────────────────────────────────────────

    def handle_key(self, key: int, unicode_char: str, mods: int) -> bool:
        if not self.visible:
            return False

        if key == pygame.K_ESCAPE:
            self.close(); return True
        if key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self._confirm(); return True
        if key == pygame.K_BACKSPACE:
            if self._cursor > 0:
                self._name   = self._name[:self._cursor-1] + self._name[self._cursor:]
                self._cursor -= 1
            return True
        if key == pygame.K_DELETE:
            if self._cursor < len(self._name):
                self._name = self._name[:self._cursor] + self._name[self._cursor+1:]
            return True
        if key == pygame.K_LEFT:
            self._cursor = max(0, self._cursor - 1); return True
        if key == pygame.K_RIGHT:
            self._cursor = min(len(self._name), self._cursor + 1); return True
        if key == pygame.K_HOME:
            self._cursor = 0; return True
        if key == pygame.K_END:
            self._cursor = len(self._name); return True
        # Printable chars — block path separators
        if unicode_char and unicode_char.isprintable() and unicode_char not in "/\\":
            self._name   = self._name[:self._cursor] + unicode_char + self._name[self._cursor:]
            self._cursor += 1
            self._error  = ""
            return True
        return True  # consume everything while open

    def handle_click(self, pos) -> bool:
        if not self.visible:
            return False
        for rect, folder in self._folder_rects:
            if rect.collidepoint(pos):
                self._folder = folder; self._error = ""; return True
        if self._confirm_rect and self._confirm_rect.collidepoint(pos):
            self._confirm(); return True
        if self._cancel_rect  and self._cancel_rect.collidepoint(pos):
            self.close();    return True
        return True  # block click-through

    def _confirm(self):
        name = self._name.strip()
        if not name:
            self._error = "Filename cannot be empty."; return
        if not Path(name).suffix:
            name += ".txt"; self._name = name; self._cursor = len(name)
        try:
            Path(self._folder).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._error = f"Cannot create folder: {e}"; return
        full = str(Path(self._folder) / name)
        cb   = self._on_save
        self.close()
        if cb: cb(full)

    def update(self, dt: float):
        self._blink_t += dt

    # ── Draw ─────────────────────────────────────────────────────────────────

    def draw(self, surface):
        if not self.visible:
            return
        SW, SH = surface.get_size()

        # Background dim
        dim = pygame.Surface((SW, SH), pygame.SRCALPHA)
        dim.fill((0, 0, 0, 160))
        surface.blit(dim, (0, 0))

        # Dialog box
        DW = min(680, SW - 60)
        DH = min(360, SH - 80)
        DX = (SW - DW) // 2
        DY = (SH - DH) // 2
        dlg = pygame.Surface((DW, DH), pygame.SRCALPHA)
        dlg.fill((6, 10, 18, 250))
        pygame.draw.rect(dlg, (*DIALOG_BDR, 220), dlg.get_rect(), 2, border_radius=10)
        surface.blit(dlg, (DX, DY))

        PAD = 16
        y   = PAD

        # Title
        ts = self.font_lg.render("Save File As", True, TEXT_BRIGHT)
        surface.blit(ts, (DX + PAD, DY + y))
        y += ts.get_height() + 10
        pygame.draw.line(surface, DIALOG_BDR,
                         (DX + PAD, DY + y), (DX + DW - PAD, DY + y), 1)
        y += 10

        # ── Left column: folder shortcuts ─────────────────────────────────────
        COL_W   = 160
        BTN_H   = 28
        BTN_GAP = 6
        fx, fy  = DX + PAD, DY + y

        lbl = self.font_sm.render("Save to:", True, TEXT_DIM)
        surface.blit(lbl, (fx, fy)); fy += lbl.get_height() + 4

        self._folder_rects = []
        for label, folder_fn in self.FOLDER_SHORTCUTS:
            fp       = folder_fn()
            active   = (self._folder == fp)
            bg       = (*BLUE_MID,  200) if active else (*BLUE_DARK, 180)
            bdr      = (*BLUE_LITE, 220) if active else (*BLUE_MID,  160)
            rect     = pygame.Rect(fx, fy, COL_W, BTN_H)
            pygame.draw.rect(surface, bg,  rect, border_radius=5)
            pygame.draw.rect(surface, bdr, rect, width=1, border_radius=5)
            ls = self.font_sm.render(label, True, TEXT_BRIGHT)
            surface.blit(ls, (fx + 8, fy + (BTN_H - ls.get_height()) // 2))
            self._folder_rects.append((rect, fp))
            fy += BTN_H + BTN_GAP

        # ── Right column: folder path + filename input ─────────────────────
        RX = DX + PAD + COL_W + 16
        RW = DW - COL_W - PAD * 2 - 16
        ry = DY + y

        # Current folder
        fl = self.font_sm.render("Folder:", True, TEXT_DIM)
        surface.blit(fl, (RX, ry)); ry += fl.get_height() + 2

        folder_str = self._folder
        while self.font_sm.size(folder_str)[0] > RW - 4 and len(folder_str) > 4:
            folder_str = "…" + folder_str[4:]
        fp_s = self.font_sm.render(folder_str, True, ORANGE_LITE)
        surface.blit(fp_s, (RX, ry)); ry += fp_s.get_height() + 14

        # Filename label
        fnl = self.font_sm.render("Filename:", True, TEXT_DIM)
        surface.blit(fnl, (RX, ry)); ry += fnl.get_height() + 4

        # Input box
        INPUT_H    = 32
        input_rect = pygame.Rect(RX, ry, RW, INPUT_H)
        pygame.draw.rect(surface, (*BLUE_DARK, 220), input_rect, border_radius=5)
        pygame.draw.rect(surface, (*BLUE_LITE, 200), input_rect, width=1, border_radius=5)

        before = self._name[:self._cursor]
        after  = self._name[self._cursor:]
        bw     = self.font.size(before)[0]
        scroll_x = max(0, bw - (RW - 16))

        try:
            clip = surface.subsurface(pygame.Rect(RX+6, ry+2, RW-12, INPUT_H-4))
            ty   = (INPUT_H - 4 - self.font.get_height()) // 2
            clip.blit(self.font.render(before, True, TEXT_BRIGHT), (-scroll_x, ty))
            clip.blit(self.font.render(after,  True, TEXT_MID),
                      (bw - scroll_x, ty))
        except ValueError:
            pass

        # Blinking cursor bar
        if int(self._blink_t * 2) % 2 == 0:
            cx = RX + 6 + bw - scroll_x
            pygame.draw.rect(surface, BLUE_LITE, (cx, ry + 5, 2, INPUT_H - 10))

        ry += INPUT_H + 6

        # Full path preview
        full = str(Path(self._folder) / (self._name.strip() or "…"))
        while self.font_sm.size(full)[0] > RW - 4 and len(full) > 4:
            full = "…" + full[4:]
        pv = self.font_sm.render(full, True, TEXT_DIM)
        surface.blit(pv, (RX, ry)); ry += pv.get_height() + 6

        # Error
        if self._error:
            es = self.font_sm.render(self._error, True, RED_LITE)
            surface.blit(es, (RX, ry))

        # ── Buttons (bottom of dialog) ────────────────────────────────────────
        BTN_W  = 100; BTN_H2 = 30; BTN_Y = DY + DH - BTN_H2 - PAD
        gap    = 10
        canc_r = pygame.Rect(DX + DW - PAD - BTN_W*2 - gap, BTN_Y, BTN_W, BTN_H2)
        conf_r = pygame.Rect(DX + DW - PAD - BTN_W,          BTN_Y, BTN_W, BTN_H2)
        self._cancel_rect  = canc_r
        self._confirm_rect = conf_r

        pygame.draw.rect(surface, (*RED_MID,  180), canc_r, border_radius=6)
        pygame.draw.rect(surface, (*RED_LITE, 160), canc_r, width=1, border_radius=6)
        cl = self.font.render("Cancel", True, TEXT_BRIGHT)
        surface.blit(cl, (canc_r.x + (BTN_W - cl.get_width())//2,
                           canc_r.y + (BTN_H2 - cl.get_height())//2))

        pygame.draw.rect(surface, (*BLUE_MID,  220), conf_r, border_radius=6)
        pygame.draw.rect(surface, (*BLUE_LITE, 200), conf_r, width=1, border_radius=6)
        sl = self.font.render("Save", True, TEXT_BRIGHT)
        surface.blit(sl, (conf_r.x + (BTN_W - sl.get_width())//2,
                           conf_r.y + (BTN_H2 - sl.get_height())//2))

        # Keyboard hint
        hs = self.font_sm.render(
            "ENTER = save   ESC = cancel   ← → move cursor   BKSP = delete",
            True, TEXT_DIM)
        surface.blit(hs, (DX + (DW - hs.get_width())//2, BTN_Y - hs.get_height() - 6))


# ── Text Editor ───────────────────────────────────────────────────────────────

class TextEditor:
    def __init__(self, W, H, fonts):
        self.W = W; self.H = H; self.fonts = fonts
        self.lines      = [""]
        self.cursor_row = 0
        self.cursor_col = 0
        self.scroll_row = 0
        self.filepath   = None
        self.modified   = False
        self.status_msg = ("Ready  |  CTRL+S save as  CTRL+SHIFT+S overwrite  "
                           "CTRL+O open  CTRL+N new  CTRL+Q quit")
        self.status_t   = 0.0
        self._tab_size  = 4
        self.font       = fonts.get("mono_sm") or fonts.get("md") or pygame.font.Font(None, 18)
        self.line_h     = self.font.get_linesize() + 1
        self.margin_x   = 54
        self.margin_top = BAR_H + 4
        self._visible_lines = max(1, (H - self.margin_top - BAR_H - 8) // self.line_h)
        self._bar_buttons   = []
        self._dragging      = False
        self._drag_last     = (0, 0)
        self.save_dialog    = SaveAsDialog(fonts)

    # ── Cursor helpers ────────────────────────────────────────────────────────

    def _clamp(self):
        self.cursor_row = max(0, min(self.cursor_row, len(self.lines)-1))
        self.cursor_col = max(0, min(self.cursor_col, len(self.lines[self.cursor_row])))

    def _scroll_to_cursor(self):
        if self.cursor_row < self.scroll_row:
            self.scroll_row = self.cursor_row
        elif self.cursor_row >= self.scroll_row + self._visible_lines:
            self.scroll_row = self.cursor_row - self._visible_lines + 1

    # ── Edit ops ──────────────────────────────────────────────────────────────

    def insert_char(self, ch: str):
        row, col = self.cursor_row, self.cursor_col
        self.lines[row] = self.lines[row][:col] + ch + self.lines[row][col:]
        self.cursor_col += len(ch); self.modified = True

    def insert_text(self, text: str):
        for i, part in enumerate(text.split("\n")):
            if i > 0: self._enter()
            for ch in part: self.insert_char(ch)
        self.modified = True

    def _enter(self):
        row, col = self.cursor_row, self.cursor_col
        rest = self.lines[row][col:]
        self.lines[row] = self.lines[row][:col]
        self.lines.insert(row+1, rest)
        self.cursor_row += 1; self.cursor_col = 0; self.modified = True

    def _backspace(self):
        row, col = self.cursor_row, self.cursor_col
        if col > 0:
            self.lines[row] = self.lines[row][:col-1] + self.lines[row][col:]
            self.cursor_col -= 1
        elif row > 0:
            prev_len = len(self.lines[row-1])
            self.lines[row-1] += self.lines[row]; del self.lines[row]
            self.cursor_row -= 1; self.cursor_col = prev_len
        self.modified = True

    def _delete_char(self):
        row, col = self.cursor_row, self.cursor_col
        if col < len(self.lines[row]):
            self.lines[row] = self.lines[row][:col] + self.lines[row][col+1:]
        elif row < len(self.lines)-1:
            self.lines[row] += self.lines[row+1]; del self.lines[row+1]
        self.modified = True

    # ── Save ─────────────────────────────────────────────────────────────────

    def request_save_as(self):
        """Always prompts — used by CTRL+S, buttons, and voice 'save'."""
        self.save_dialog.open(self.filepath, self._do_save)

    def request_overwrite(self):
        """CTRL+SHIFT+S — writes in-place if path known, else prompts."""
        if self.filepath:
            self._do_save(self.filepath)
        else:
            self.save_dialog.open(None, self._do_save)

    def _do_save(self, full_path: str):
        try:
            Path(full_path).write_text("\n".join(self.lines))
            self.filepath = full_path; self.modified = False
            self.set_status(f"Saved: {os.path.basename(full_path)}")
            _send_gui({"type": "app_command_log",
                       "text": f"Saved: {os.path.basename(full_path)}"})
        except Exception as e:
            self.set_status(f"Save error: {e}")

    # ── Open / New ────────────────────────────────────────────────────────────

    def open(self, path: str):
        try:
            text = Path(path).read_text(errors="replace")
            self.lines = text.split("\n") or [""]
            self.cursor_row = 0; self.cursor_col = 0; self.scroll_row = 0
            self.filepath = path; self.modified = False
            self.set_status(f"Opened: {os.path.basename(path)}")
        except Exception as e:
            self.set_status(f"Error opening: {e}")

    def open_recent(self):
        candidates = sorted(Path.home().glob("*.txt"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates: self.open(str(candidates[0]))
        else:          self.set_status("No .txt files found in home dir")

    def new_file(self):
        self.lines = [""]; self.cursor_row = 0; self.cursor_col = 0
        self.scroll_row = 0; self.filepath = None; self.modified = False
        self.set_status("New file")

    def set_status(self, msg: str):
        self.status_msg = msg; self.status_t = 3.0

    # ── Key handling ─────────────────────────────────────────────────────────

    def handle_key(self, key: int, unicode_char: str, mods: int) -> bool:
        # Dialog eats everything while open
        if self.save_dialog.visible:
            return self.save_dialog.handle_key(key, unicode_char, mods)

        ctrl  = bool(mods & pygame.KMOD_CTRL)
        shift = bool(mods & pygame.KMOD_SHIFT)

        if ctrl:
            if key == pygame.K_s:
                if shift: self.request_overwrite()
                else:     self.request_save_as()
                return True
            if key == pygame.K_o: self.open_recent(); return True
            if key == pygame.K_n: self.new_file();    return True
            if key == pygame.K_q: return False        # signal quit
            if key == pygame.K_a:
                self.cursor_row = len(self.lines)-1
                self.cursor_col = len(self.lines[-1]); return True
        else:
            if key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self._enter(); self._clamp(); self._scroll_to_cursor(); return True
            if key == pygame.K_BACKSPACE:
                self._backspace(); self._clamp(); self._scroll_to_cursor(); return True
            if key == pygame.K_DELETE:
                self._delete_char(); self._clamp(); self._scroll_to_cursor(); return True
            if key == pygame.K_TAB:
                self.insert_char(" " * self._tab_size); self._scroll_to_cursor(); return True
            if key == pygame.K_UP:
                self.cursor_row = max(0, self.cursor_row-1)
                self._clamp(); self._scroll_to_cursor(); return True
            if key == pygame.K_DOWN:
                self.cursor_row = min(len(self.lines)-1, self.cursor_row+1)
                self._clamp(); self._scroll_to_cursor(); return True
            if key == pygame.K_LEFT:
                if self.cursor_col > 0:   self.cursor_col -= 1
                elif self.cursor_row > 0:
                    self.cursor_row -= 1
                    self.cursor_col = len(self.lines[self.cursor_row])
                self._scroll_to_cursor(); return True
            if key == pygame.K_RIGHT:
                if self.cursor_col < len(self.lines[self.cursor_row]):
                    self.cursor_col += 1
                elif self.cursor_row < len(self.lines)-1:
                    self.cursor_row += 1; self.cursor_col = 0
                self._scroll_to_cursor(); return True
            if key == pygame.K_HOME: self.cursor_col = 0; return True
            if key == pygame.K_END:
                self.cursor_col = len(self.lines[self.cursor_row]); return True
            if key == pygame.K_PAGEUP:
                self.cursor_row = max(0, self.cursor_row - self._visible_lines)
                self._clamp(); self._scroll_to_cursor(); return True
            if key == pygame.K_PAGEDOWN:
                self.cursor_row = min(len(self.lines)-1,
                                      self.cursor_row + self._visible_lines)
                self._clamp(); self._scroll_to_cursor(); return True
            if unicode_char and unicode_char.isprintable():
                self.insert_char(unicode_char); self._scroll_to_cursor(); return True
        return True

    # ── Draw ─────────────────────────────────────────────────────────────────

    def draw(self, surface):
        surface.fill(DARK_BG)
        self._draw_top_bar(surface)
        self._draw_gutter_and_text(surface)
        self._draw_bottom_bar(surface)
        self.save_dialog.draw(surface)   # overlays when visible

    def _draw_top_bar(self, surface):
        bar = pygame.Surface((self.W, BAR_H), pygame.SRCALPHA)
        bar.fill((10, 14, 24, 230)); surface.blit(bar, (0, 0))

        fn = os.path.basename(self.filepath) if self.filepath else "Untitled"
        if self.modified: fn += " ●"
        ts = self.font.render(f"📝 {fn}", True, TEXT_BRIGHT)
        surface.blit(ts, (12, 8))

        # Top-right shortcut hints
        hint = self.font.render(
            "CTRL+S save as   CTRL+SHIFT+S overwrite   CTRL+O open   CTRL+N new   CTRL+Q quit",
            True, TEXT_DIM)
        surface.blit(hint, (self.W - hint.get_width() - 10, 6))

        # Bottom of bar: action buttons
        btn_defs  = [("Save As", "saveas"), ("Overwrite", "overwrite"),
                     ("Open",    "open"),   ("New",       "new")]
        btn_h, btn_pad, btn_gap = 20, 8, 6
        btn_surfs = [(self.font.render(lbl, True, TEXT_BRIGHT), act) for lbl, act in btn_defs]
        total_w   = (sum(s.get_width() + btn_pad*2 for s, _ in btn_surfs)
                     + btn_gap * (len(btn_surfs)-1))
        bx = self.W - total_w - 10
        by = BAR_H - btn_h - 6
        self._bar_buttons = []
        for s, act in btn_surfs:
            bw   = s.get_width() + btn_pad*2
            rect = pygame.Rect(bx, by, bw, btn_h)
            pygame.draw.rect(surface, BLUE_DARK, rect, border_radius=4)
            pygame.draw.rect(surface, BLUE_MID,  rect, width=1, border_radius=4)
            surface.blit(s, (bx + btn_pad, by + (btn_h - s.get_height())//2))
            self._bar_buttons.append({"rect": rect, "action": act})
            bx += bw + btn_gap

    def handle_bar_click(self, pos) -> bool:
        if self.save_dialog.visible:
            return self.save_dialog.handle_click(pos)
        for btn in self._bar_buttons:
            if btn["rect"].collidepoint(pos):
                act = btn["action"]
                if   act == "saveas":    self.request_save_as()
                elif act == "overwrite": self.request_overwrite()
                elif act == "open":      self.open_recent()
                elif act == "new":       self.new_file()
                return True
        return False

    def bar_rect(self):
        return pygame.Rect(0, 0, self.W, BAR_H)

    def _draw_gutter_and_text(self, surface):
        font = self.font; lh = self.line_h
        mxt  = self.margin_top; mxx = self.margin_x
        pygame.draw.rect(surface, BLUE_DARK, (0, mxt, mxx-4, self.H - mxt - BAR_H))
        pygame.draw.line(surface, BLUE_MID,  (mxx-4, mxt), (mxx-4, self.H - BAR_H), 1)

        for i in range(self._visible_lines):
            row = self.scroll_row + i
            if row >= len(self.lines): break
            y      = mxt + i * lh
            is_cur = (row == self.cursor_row)

            ns = font.render(str(row+1), True, TEXT_BRIGHT if is_cur else TEXT_DIM)
            surface.blit(ns, (mxx - ns.get_width() - 8, y + 1))

            if is_cur:
                hl = pygame.Surface((self.W - mxx, lh), pygame.SRCALPHA)
                hl.fill((*BLUE_DARK, 120)); surface.blit(hl, (mxx, y))

            surface.blit(font.render(self.lines[row], True, TEXT_BRIGHT), (mxx + 4, y + 1))

            if is_cur:
                cx = mxx + 4 + font.size(self.lines[row][:self.cursor_col])[0]
                if int(time.time() * 2) % 2 == 0:
                    pygame.draw.rect(surface, BLUE_LITE, (cx, y+2, 2, lh-4))

    def _draw_bottom_bar(self, surface):
        by  = self.H - BAR_H
        bar = pygame.Surface((self.W, BAR_H), pygame.SRCALPHA)
        bar.fill((10, 14, 24, 230)); surface.blit(bar, (0, by))

        ps = self.font.render(
            f"Ln {self.cursor_row+1}  Col {self.cursor_col+1}  |  {len(self.lines)} lines",
            True, TEXT_DIM)
        surface.blit(ps, (12, by + (BAR_H - ps.get_height())//2))

        if self.status_t > 0:
            col = (GREEN_LITE
                   if ("aved" in self.status_msg or "pened" in self.status_msg)
                   else TEXT_MID)
            ss = self.font.render(self.status_msg, True, col)
            surface.blit(ss, (self.W//2 - ss.get_width()//2,
                               by + (BAR_H - ss.get_height())//2))


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    pygame.init()
    W, H   = 900, 600
    screen = pygame.display.set_mode((W, H), pygame.NOFRAME | pygame.RESIZABLE)
    pygame.display.set_caption("LoomOS Text Editor")
    clock  = pygame.time.Clock()

    def _load_font(name, size):
        try:
            p = pygame.font.match_font(name)
            if p: return pygame.font.Font(p, size)
        except Exception:
            pass
        return pygame.font.Font(None, size)

    fonts = {
        "mono_sm": _load_font("Comfortaa", 16),
        "md":      _load_font("Comfortaa", 18),
        "lg_b":    _load_font("Comfortaa", 22),
        "sm":      _load_font("Comfortaa", 13),
    }

    editor = TextEditor(W, H, fonts)
    _send_gui({"type": "app_ready", "manifest": LOOMOS_APP, "pid": os.getpid()})
    _send_gui({"type": "app_command_log", "text": "Text Editor ready"})

    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        editor.open(sys.argv[1])

    running = True
    while running:
        dt = clock.tick(30) / 1000.0
        if editor.status_t > 0: editor.status_t -= dt
        editor.save_dialog.update(dt)

        # ── IPC from main GUI ─────────────────────────────────────────────────
        with _ipc_lock:
            msgs = list(_ipc_queue); _ipc_queue.clear()
        for msg in msgs:
            t = msg.get("type", "")
            if t == "quit":
                running = False
            elif t == "key_input":
                if not editor.handle_key(msg.get("key", 0),
                                         msg.get("unicode", ""),
                                         msg.get("mods",    0)):
                    running = False
            elif t == "dictation_text":
                text = msg.get("text", "")
                if text and not editor.save_dialog.visible:
                    editor.insert_text(text + " ")
                    editor.set_status(
                        f"Dictated: {text[:30]}{'…' if len(text)>30 else ''}")
            elif t == "voice_command":
                cmd = msg.get("text", "").lower().strip()
                if cmd in ("save", "save file", "save as"):
                    editor.request_save_as()
                elif cmd in ("save here", "overwrite", "quick save"):
                    editor.request_overwrite()
                elif cmd in ("new", "new file"):
                    editor.new_file()
                elif cmd.startswith("open "):
                    editor.open(cmd[5:].strip())

        # ── Local pygame events ───────────────────────────────────────────────
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.VIDEORESIZE:
                W, H   = ev.w, ev.h
                screen = pygame.display.set_mode((W, H), pygame.NOFRAME | pygame.RESIZABLE)
                editor.W = W; editor.H = H
                editor._visible_lines = max(
                    1, (H - editor.margin_top - BAR_H - 8) // editor.line_h)
            elif ev.type == pygame.KEYDOWN:
                if not editor.handle_key(ev.key, ev.unicode, pygame.key.get_mods()):
                    running = False
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                pos = ev.pos
                if editor.save_dialog.visible:
                    editor.save_dialog.handle_click(pos)
                elif editor.bar_rect().collidepoint(pos):
                    if not editor.handle_bar_click(pos):
                        editor._dragging = True
            elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
                editor._dragging = False
            elif ev.type == pygame.MOUSEMOTION:
                if editor._dragging:
                    if (hasattr(pygame.display, "get_window_position") and
                            hasattr(pygame.display, "set_window_position")):
                        wx, wy = pygame.display.get_window_position()
                        dx, dy = ev.rel
                        pygame.display.set_window_position((wx + dx, wy + dy))
            elif ev.type == pygame.MOUSEWHEEL:
                if not editor.save_dialog.visible:
                    editor.scroll_row = max(0, min(
                        len(editor.lines) - 1,
                        editor.scroll_row - ev.y * 3))

        editor.draw(screen)
        pygame.display.flip()

    _send_gui({"type": "app_closed", "name": LOOMOS_APP["name"]})
    pygame.quit()


if __name__ == "__main__":
    main()