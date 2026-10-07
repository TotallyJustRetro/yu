"""
Media Launcher - a local video library organizer for Windows.

Run:    double-click MediaLauncher.pyw   (or: pythonw MediaLauncher.pyw)
Extras: pip install pillow opencv-python   (video thumbnails)
        pip install tkinterdnd2             (drag & drop files/folders onto the window)
        pip install python-vlc              (built-in player; also install VLC 64-bit from videolan.org)
Build:  pip install pyinstaller
        pyinstaller --noconsole --onefile --name MediaLauncher MediaLauncher.pyw

Your library is stored in %APPDATA%\\MediaLauncher\\library.json.
Nothing is uploaded anywhere; files are never moved or deleted.
"""
import hashlib, json, os, queue, random, subprocess, threading, time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from pathlib import Path

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = ImageTk = None
try:
    import cv2
except ImportError:
    cv2 = None
try:
    import vlc
except Exception:
    vlc = None
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = TkinterDnD = None

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "MediaLauncher"
THUMB_DIR = APP_DIR / "thumbs"
DB_FILE = APP_DIR / "library.json"
APP_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR.mkdir(parents=True, exist_ok=True)

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".flv", ".mpg", ".mpeg", ".ts"}
DEFAULT_CATS = ["Twerking", "Femboy", "Blowjob", "NSFW ASMR", "Footjob", "Yiff", "Uncategorized"]
TILE_W, TILE_H, PAGE = 240, 135, 40

BG, PANEL, CARD = "#0d0408", "#170610", "#250b18"
FG, MUTED, ACCENT = "#ffe9f1", "#b07a92", "#ff2d75"
CRIMSON, EDGE, THUMB_BG = "#9d0b3a", "#4a1226", "#12040a"
EMOJI = "Segoe UI Emoji"
DEFAULT_ICONS = {"Twerking": "\U0001F351", "Femboy": "\U0001F380", "Blowjob": "\U0001F444",
                 "NSFW ASMR": "\U0001F3A7", "Footjob": "\U0001F9B6", "Yiff": "\U0001F43A",
                 "Uncategorized": "\U0001F525"}
FALLBACK_ICON = "\U0001F48B"


def fmt(ms):
    ms = max(0, int(ms))
    return f"{ms // 60000}:{ms // 1000 % 60:02d}"


class Player(tk.Toplevel):
    """Built-in video player (needs VLC installed + python-vlc)."""

    def __init__(self, app, paths, index):
        super().__init__(app.root)
        self.app, self.paths, self.i, self.drag, self.full = app, paths, index, False, False
        self.title("Midnight Player")
        self.geometry("1000x640")
        self.configure(bg="black")
        self.inst = vlc.Instance()
        self.mp = self.inst.media_player_new()
        self.video = tk.Frame(self, bg="black")
        self.video.pack(fill="both", expand=True)
        self.bar = tk.Frame(self, bg=PANEL)
        self.bar.pack(fill="x")
        self.name = tk.Label(self.bar, bg=PANEL, fg=FG, font=(EMOJI, 10, "bold"), anchor="w")
        self.name.pack(fill="x", padx=10, pady=(6, 0))
        self.seek = ttk.Scale(self.bar, from_=0, to=1000)
        self.seek.pack(fill="x", padx=10, pady=4)
        self.seek.bind("<ButtonPress-1>", lambda e: setattr(self, "drag", True))
        self.seek.bind("<ButtonRelease-1>", self.on_seek)
        row = tk.Frame(self.bar, bg=PANEL)
        row.pack(fill="x", padx=10, pady=(0, 8))
        for txt, cmd in [("\u23EE", lambda: self.step(-1)), ("\u23EF", self.toggle),
                         ("\u23ED", lambda: self.step(1)), ("\u26F6", self.fullscreen)]:
            tk.Button(row, text=txt, command=cmd, bg=CARD, fg=FG, bd=0, width=4, font=(EMOJI, 12),
                      activebackground=ACCENT).pack(side="left", padx=3)
        self.time_lbl = tk.Label(row, bg=PANEL, fg=MUTED)
        self.time_lbl.pack(side="left", padx=10)
        vol = ttk.Scale(row, from_=0, to=100, length=110, command=lambda v: self.mp.audio_set_volume(int(float(v))))
        vol.set(80)
        vol.pack(side="right")
        tk.Label(row, text="\U0001F50A", bg=PANEL, fg=FG, font=(EMOJI, 11)).pack(side="right")
        self.mp.set_hwnd(self.video.winfo_id())
        self.mp.video_set_mouse_input(False)
        self.mp.video_set_key_input(False)
        self.bind("<space>", lambda e: self.toggle())
        self.bind("<Right>", lambda e: self.jump(5000))
        self.bind("<Left>", lambda e: self.jump(-5000))
        self.bind("<f>", lambda e: self.fullscreen())
        self.bind("<n>", lambda e: self.step(1))
        self.bind("<p>", lambda e: self.step(-1))
        self.bind("<Escape>", lambda e: self.fullscreen() if self.full else None)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.load()
        self.tick()
        self.focus_force()

    def load(self):
        p = self.paths[self.i]
        self.mp.set_media(self.inst.media_new(p))
        self.mp.play()
        self.name.config(text=Path(p).stem)
        self.app.mark_played(p)

    def step(self, d):
        self.i = (self.i + d) % len(self.paths)
        self.load()

    def toggle(self):
        self.mp.pause()

    def jump(self, ms):
        self.mp.set_time(max(0, self.mp.get_time() + ms))

    def on_seek(self, e):
        self.mp.set_position(float(self.seek.get()) / 1000)
        self.drag = False

    def fullscreen(self):
        self.full = not self.full
        self.attributes("-fullscreen", self.full)
        if self.full:
            self.bar.pack_forget()
        else:
            self.bar.pack(fill="x")

    def tick(self):
        if not self.winfo_exists():
            return
        if self.mp.get_state() == vlc.State.Ended:
            if len(self.paths) > 1:
                self.step(1)
        elif not self.drag and self.mp.get_length() > 0:
            self.seek.set(self.mp.get_position() * 1000)
            self.time_lbl.config(text=f"{fmt(self.mp.get_time())} / {fmt(self.mp.get_length())}")
        self.after(400, self.tick)

    def close(self):
        try:
            self.mp.stop()
            self.mp.release()
            self.inst.release()
        finally:
            self.destroy()
            self.app.refresh()


class App:
    def __init__(self, root):
        self.root = root
        root.title("Midnight Launcher")
        root.geometry("1200x760")
        root.configure(bg=BG)
        self.data = self.load()
        self.view, self.page, self.photos, self.tile_labels = "all", 0, {}, {}
        self.selected, self.tile_frames, self.total = set(), {}, 0
        self.thumb_q = queue.Queue()
        threading.Thread(target=self.thumb_worker, daemon=True).start()
        self.blank = tk.PhotoImage(width=TILE_W, height=TILE_H)
        self.build_ui()
        self.refresh()
        root.bind("<F12>", lambda e: root.iconify())  # boss key
        root.bind("<Control-a>", self.select_all)
        root.bind("<Escape>", lambda e: self.clear_sel())

    # ---------- storage ----------
    def load(self):
        d = {"categories": list(DEFAULT_CATS), "items": {}}
        if DB_FILE.exists():
            try:
                d = json.loads(DB_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        for it in d["items"].values():  # migrate old single-category entries
            if "categories" not in it:
                it["categories"] = [it.pop("category", "Uncategorized")]
        return d

    def save(self):
        DB_FILE.write_text(json.dumps(self.data, indent=1), encoding="utf-8")

    # ---------- UI ----------
    def build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=CARD, background=CARD, foreground=FG)

        side = tk.Frame(self.root, bg=PANEL, width=230)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="\U0001F525 MIDNIGHT", bg=PANEL, fg=ACCENT, font=("Segoe UI Black", 18)).pack(anchor="w", padx=14, pady=(16, 0))
        tk.Label(side, text="your private collection", bg=PANEL, fg=MUTED, font=("Segoe UI", 9, "italic")).pack(anchor="w", padx=16)
        tk.Frame(side, height=2, bg=ACCENT).pack(fill="x", padx=14, pady=(8, 10))
        self.nav = tk.Listbox(side, bg=PANEL, fg=FG, bd=0, highlightthickness=0, activestyle="none",
                              selectbackground=CRIMSON, selectforeground="white", font=(EMOJI, 12))
        self.nav.pack(fill="both", expand=True, padx=8)
        self.nav.bind("<<ListboxSelect>>", self.on_nav)
        self.nav.bind("<Button-3>", self.nav_menu)
        for text, cmd in [("\U0001F48B  New category", self.add_category), ("\U0001F4C1  Add folder", self.add_folder),
                          ("\U0001F39E  Add files", self.add_files), ("\U0001F50D  Find duplicates", self.find_dupes), ("\U0001F512  PIN lock", self.set_pin)]:
            tk.Button(side, text=text, command=cmd, bg=CARD, fg=FG, bd=0, activebackground=ACCENT,
                      activeforeground="white", font=(EMOJI, 10), pady=6).pack(fill="x", padx=10, pady=3)
        tk.Frame(side, height=8, bg=PANEL).pack()

        main = tk.Frame(self.root, bg=BG)
        main.pack(side="left", fill="both", expand=True)
        bar = tk.Frame(main, bg=BG)
        bar.pack(fill="x", padx=16, pady=12)
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *a: self.reset_page())
        e = tk.Entry(bar, textvariable=self.search, bg=CARD, fg=FG, insertbackground=FG, bd=0, font=("Segoe UI", 12))
        e.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 10))
        self.sort = ttk.Combobox(bar, values=["Newest", "Name", "Most played"], state="readonly", width=12)
        self.sort.set("Newest")
        self.sort.bind("<<ComboboxSelected>>", lambda e: self.reset_page())
        self.sort.pack(side="left", padx=4)
        tk.Button(bar, text="\U0001F3B2 Random", command=self.play_random, bg=ACCENT, fg="white", bd=0,
                  font=("Segoe UI", 10, "bold"), padx=14, pady=5).pack(side="left", padx=4)
        self.pl_btn = tk.Button(bar, command=self.toggle_player, bg=CARD, fg=FG, bd=0, font=(EMOJI, 10), padx=10, pady=5)
        self.pl_btn.pack(side="left", padx=4)
        self.update_pl_btn()

        self.title = tk.Label(main, text="", bg=BG, fg=MUTED, font=("Segoe UI", 10), anchor="w")
        self.title.pack(fill="x", padx=18)

        wrap = tk.Frame(main, bg=BG)
        wrap.pack(fill="both", expand=True, padx=10, pady=6)
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.grid_frame = tk.Frame(self.canvas, bg=BG)
        self.canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self.on_resize)
        self.root.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        pager = tk.Frame(main, bg=BG)
        pager.pack(fill="x", pady=(0, 8))
        self.prev_b = tk.Button(pager, text="< Prev", command=lambda: self.turn(-1), bg=CARD, fg=FG, bd=0, padx=12)
        self.next_b = tk.Button(pager, text="Next >", command=lambda: self.turn(1), bg=CARD, fg=FG, bd=0, padx=12)
        self.page_lbl = tk.Label(pager, bg=BG, fg=MUTED)
        self.prev_b.pack(side="left", padx=16)
        self.next_b.pack(side="right", padx=16)
        self.page_lbl.pack()
        self._resize_job = None
        if TkinterDnD:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self.on_drop)

    def on_resize(self, e):
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(150, self.render)

    # ---------- sidebar ----------
    def icon(self, cat):
        return self.data.get("icons", {}).get(cat) or DEFAULT_ICONS.get(cat, FALLBACK_ICON)

    def refresh(self):
        items = self.data["items"].values()
        self.nav_keys = ["all", "fav", "recent"]
        rows = [f"\U0001F48B  All videos  ({len(self.data['items'])})",
                f"\u2764  Favorites  ({sum(1 for i in items if i['fav'])})",
                "\U0001F551  Recently played"]
        for c in self.data["categories"]:
            rows.append(f"{self.icon(c)}  {c}  ({sum(1 for i in items if c in i['categories'])})")
            self.nav_keys.append("cat:" + c)
        self.nav.delete(0, "end")
        for r in rows:
            self.nav.insert("end", r)
        if self.view in self.nav_keys:
            self.nav.selection_set(self.nav_keys.index(self.view))
        self.render()

    def on_nav(self, e):
        sel = self.nav.curselection()
        if sel:
            self.view = self.nav_keys[sel[0]]
            self.reset_page()

    def nav_menu(self, e):
        idx = self.nav.nearest(e.y)
        key = self.nav_keys[idx] if idx < len(self.nav_keys) else ""
        if not key.startswith("cat:"):
            return
        name = key[4:]
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Rename", command=lambda: self.rename_category(name))
        if name != "Uncategorized":
            m.add_command(label="Delete (videos move to Uncategorized)", command=lambda: self.delete_category(name))
        m.tk_popup(e.x_root, e.y_root)

    def add_category(self):
        n = simpledialog.askstring("New category", "Category name:", parent=self.root)
        if n and n.strip() and n.strip() not in self.data["categories"]:
            n = n.strip()
            ic = simpledialog.askstring("Icon", "Paste an emoji for this category (optional):", parent=self.root)
            self.data["categories"].append(n)
            if ic and ic.strip():
                self.data.setdefault("icons", {})[n] = ic.strip()
            self.save(); self.refresh()

    def rename_category(self, old):
        n = simpledialog.askstring("Rename", "New name:", initialvalue=old, parent=self.root)
        if n and n.strip() and n.strip() not in self.data["categories"]:
            n = n.strip()
            self.data.setdefault("icons", {})[n] = self.icon(old)
            self.data["categories"][self.data["categories"].index(old)] = n
            for i in self.data["items"].values():
                i["categories"] = [n if c == old else c for c in i["categories"]]
            if self.view == "cat:" + old:
                self.view = "cat:" + n
            self.save(); self.refresh()

    def delete_category(self, name):
        if messagebox.askyesno("Delete category", f"Delete '{name}'? Videos move to Uncategorized."):
            self.data["categories"].remove(name)
            for i in self.data["items"].values():
                i["categories"] = [c for c in i["categories"] if c != name] or ["Uncategorized"]
            if self.view == "cat:" + name:
                self.view = "all"
            self.save(); self.refresh()

    # ---------- player / drop / multi-category ----------
    def toggle_player(self):
        if not vlc:
            messagebox.showinfo("Built-in player", "Install VLC (64-bit) from videolan.org, then run:\n\npip install python-vlc")
            return
        self.data["builtin"] = not self.data.get("builtin", True)
        self.save()
        self.update_pl_btn()

    def update_pl_btn(self):
        on = bool(vlc and self.data.get("builtin", True))
        self.pl_btn.config(text="\U0001F3AC Player: built-in" if on else "\U0001F3AC Player: system", fg=ACCENT if on else FG)

    def on_drop(self, e):
        paths = []
        for p in self.root.tk.splitlist(e.data):
            if os.path.isdir(p):
                paths += [os.path.join(r, f) for r, _, fs in os.walk(p) for f in fs if Path(f).suffix.lower() in VIDEO_EXT]
            elif Path(p).suffix.lower() in VIDEO_EXT:
                paths.append(p)
        if not paths:
            return
        cat = self.view[4:] if self.view.startswith("cat:") else self.pick_category()
        if cat:
            n, d = self.add_paths(paths, cat)
            messagebox.showinfo("Dropped", f"Added {n} new videos to {cat}. Skipped {d} duplicates.")

    def toggle_cat(self, path, c, on):
        cats = self.data["items"][path]["categories"]
        if on and c not in cats:
            cats.append(c)
        if not on and c in cats:
            cats.remove(c)
        if not cats:
            cats.append("Uncategorized")
        self.save()
        self.refresh()

    def mark_played(self, path):
        it = self.data["items"][path]
        it["plays"] += 1
        it["last_played"] = time.time()
        self.save()

    # ---------- multi-select ----------
    def update_title(self):
        n = len(self.selected)
        extra = f"  \u00B7  {n} selected  (right-click for bulk actions, Esc to clear)" if n else "  \u00B7  Ctrl+click to multi-select, Ctrl+A for all"
        self.title.config(text=f"{self.total} videos" + extra)

    def restyle(self):
        for p, f in self.tile_frames.items():
            f.config(highlightbackground=ACCENT if p in self.selected else EDGE)
        self.update_title()

    def on_click(self, e, path):
        if e.state & 0x4:  # Ctrl held
            self.selected ^= {path}
        else:
            self.selected = set()
        self.restyle()

    def select_all(self, e=None):
        try:
            if isinstance(self.root.focus_get(), (tk.Entry, ttk.Combobox)):
                return
        except Exception:
            pass
        self.selected = {i["path"] for i in self.filtered()}
        self.restyle()

    def clear_sel(self):
        self.selected = set()
        self.restyle()

    def bulk_menu(self, e, paths):
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label=f"{len(paths)} videos selected", state="disabled")
        add, rem = tk.Menu(m, tearoff=0), tk.Menu(m, tearoff=0)
        for c in self.data["categories"]:
            add.add_command(label=f"{self.icon(c)}  {c}", command=lambda c=c: self.bulk_cat(paths, c, True))
            rem.add_command(label=f"{self.icon(c)}  {c}", command=lambda c=c: self.bulk_cat(paths, c, False))
        m.add_cascade(label="Add to category", menu=add)
        m.add_cascade(label="Remove from category", menu=rem)
        m.add_command(label="Favorite all", command=lambda: self.bulk_fav(paths))
        m.add_separator()
        m.add_command(label="Remove all from library", command=lambda: self.bulk_remove(paths))
        m.tk_popup(e.x_root, e.y_root)

    def bulk_cat(self, paths, c, on):
        for p in paths:
            cats = self.data["items"][p]["categories"]
            if on and c not in cats:
                cats.append(c)
            if not on and c in cats:
                cats.remove(c)
            if not cats:
                cats.append("Uncategorized")
        self.save(); self.refresh()

    def bulk_fav(self, paths):
        for p in paths:
            self.data["items"][p]["fav"] = True
        self.save(); self.refresh()

    def bulk_remove(self, paths):
        if messagebox.askyesno("Remove", f"Remove {len(paths)} videos from the library? The files are not deleted."):
            for p in paths:
                self.data["items"].pop(p, None)
            self.selected = set()
            self.save(); self.refresh()

    # ---------- duplicates ----------
    @staticmethod
    def quick_hash(p, size):
        h = hashlib.md5()
        try:
            with open(p, "rb") as f:
                for off in (0, max(0, size // 2 - 524288), max(0, size - 1048576)):
                    f.seek(off)
                    h.update(f.read(1048576))
        except OSError:
            return p
        return h.hexdigest()

    def find_dupes(self):
        self.root.config(cursor="watch")
        self.root.update_idletasks()
        by_size = {}
        for p in self.data["items"]:
            try:
                by_size.setdefault(os.path.getsize(p), []).append(p)
            except OSError:
                pass
        groups = {}
        for size, ps in by_size.items():
            if len(ps) > 1:
                for p in ps:
                    groups.setdefault((size, self.quick_hash(p, size)), []).append(p)
        groups = [g for g in groups.values() if len(g) > 1]
        self.root.config(cursor="")
        if not groups:
            messagebox.showinfo("Duplicates", "No duplicate files found.")
            return
        win = tk.Toplevel(self.root)
        win.title(f"Duplicates: {len(groups)} groups")
        win.geometry("900x480")
        win.configure(bg=PANEL)
        tk.Label(win, text="Files in the same [group] are identical. Select the ones to drop from the library (files stay on disk).",
                 bg=PANEL, fg=MUTED).pack(anchor="w", padx=12, pady=8)
        lb = tk.Listbox(win, selectmode="extended", bg=CARD, fg=FG, bd=0, highlightthickness=0,
                        selectbackground=CRIMSON, font=("Consolas", 10))
        lb.pack(fill="both", expand=True, padx=12)
        rows = []
        for gi, g in enumerate(groups, 1):
            for p in g:
                lb.insert("end", f"[{gi}]  {p}")
                rows.append(p)

        def sel():
            return [i for i in lb.curselection()]

        def show():
            for i in sel()[:1]:
                subprocess.Popen(["explorer", "/select,", rows[i]])

        def drop():
            idx = sel()
            for i in reversed(idx):
                self.data["items"].pop(rows[i], None)
                lb.delete(i)
                rows.pop(i)
            self.save(); self.refresh()

        bar = tk.Frame(win, bg=PANEL)
        bar.pack(fill="x", padx=12, pady=10)
        tk.Button(bar, text="Show in folder", command=show, bg=CARD, fg=FG, bd=0, padx=12, pady=5).pack(side="left")
        tk.Button(bar, text="Remove selected from library", command=drop, bg=ACCENT, fg="white", bd=0,
                  padx=12, pady=5).pack(side="right")

    # ---------- PIN lock ----------
    @staticmethod
    def pin_hash(p):
        return hashlib.sha256(("midnight" + p).encode()).hexdigest()

    @staticmethod
    def check_pin(root):
        try:
            h = json.loads(DB_FILE.read_text(encoding="utf-8")).get("pin")
        except Exception:
            h = None
        if not h:
            return True
        for _ in range(3):
            s = simpledialog.askstring("Locked", "Enter PIN:", show="*", parent=root)
            if s is None:
                return False
            if App.pin_hash(s) == h:
                return True
        return False

    def set_pin(self):
        s = simpledialog.askstring("PIN lock", "New PIN (leave empty to remove):", show="*", parent=self.root)
        if s is None:
            return
        if s:
            self.data["pin"] = self.pin_hash(s)
        else:
            self.data.pop("pin", None)
        self.save()
        messagebox.showinfo("PIN lock", "PIN set. It's asked at startup." if s else "PIN removed.")

    # ---------- adding media ----------
    def pick_category(self):
        win = tk.Toplevel(self.root)
        win.title("Choose category")
        win.configure(bg=PANEL)
        win.transient(self.root)
        win.grab_set()
        tk.Label(win, text="Add to category:", bg=PANEL, fg=FG).pack(padx=20, pady=(16, 6))
        cb = ttk.Combobox(win, values=self.data["categories"], state="readonly", width=28)
        cur = self.view[4:] if self.view.startswith("cat:") else "Uncategorized"
        cb.set(cur)
        cb.pack(padx=20)
        result = {"v": None}

        def ok():
            result["v"] = cb.get()
            win.destroy()
        tk.Button(win, text="OK", command=ok, bg=ACCENT, fg="white", bd=0, padx=20, pady=4).pack(pady=14)
        self.root.wait_window(win)
        return result["v"]

    def add_paths(self, paths, cat):
        seen = {}
        for q in self.data["items"]:
            try:
                seen[(os.path.getsize(q), os.path.basename(q).lower())] = q
            except OSError:
                pass
        added = dupes = 0
        for p in paths:
            p = os.path.normpath(p)
            if p in self.data["items"]:
                continue
            try:
                key = (os.path.getsize(p), os.path.basename(p).lower())
            except OSError:
                continue
            if key in seen:  # same name + size already in library
                dupes += 1
                continue
            seen[key] = p
            self.data["items"][p] = dict(path=p, title=Path(p).stem, categories=[cat], tags=[], fav=False,
                                         added=time.time(), last_played=0, plays=0)
            added += 1
        self.save(); self.refresh()
        return added, dupes

    def add_folder(self):
        d = filedialog.askdirectory(title="Select a folder to scan")
        if not d:
            return
        cat = self.pick_category()
        if not cat:
            return
        found = [os.path.join(r, f) for r, _, fs in os.walk(d) for f in fs if Path(f).suffix.lower() in VIDEO_EXT]
        n, d = self.add_paths(found, cat)
        messagebox.showinfo("Folder scanned", f"Added {n} new videos.\nSkipped {d} duplicates (same name and size) and {len(found) - n - d} already in the library.")

    def add_files(self):
        fs = filedialog.askopenfilenames(title="Select videos",
                                         filetypes=[("Video files", " ".join("*" + e for e in VIDEO_EXT))])
        if fs:
            cat = self.pick_category()
            if cat:
                self.add_paths(fs, cat)

    # ---------- filtering / rendering ----------
    def filtered(self):
        items = list(self.data["items"].values())
        v = self.view
        if v == "fav":
            items = [i for i in items if i["fav"]]
        elif v == "recent":
            items = sorted([i for i in items if i["last_played"]], key=lambda i: -i["last_played"])
            return self.apply_search(items)
        elif v.startswith("cat:"):
            items = [i for i in items if v[4:] in i["categories"]]
        items = self.apply_search(items)
        s = self.sort.get()
        if s == "Name":
            items.sort(key=lambda i: i["title"].lower())
        elif s == "Most played":
            items.sort(key=lambda i: -i["plays"])
        else:
            items.sort(key=lambda i: -i["added"])
        return items

    def apply_search(self, items):
        q = self.search.get().lower().strip()
        if not q:
            return items
        return [i for i in items if q in i["title"].lower() or any(q in c.lower() for c in i["categories"])
                or any(q in t.lower() for t in i["tags"])]

    def reset_page(self):
        self.page = 0
        self.render()

    def turn(self, d):
        self.page += d
        self.render()
        self.canvas.yview_moveto(0)

    def render(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        self.tile_labels.clear()
        self.tile_frames.clear()
        items = self.filtered()
        pages = max(1, -(-len(items) // PAGE))
        self.page = max(0, min(self.page, pages - 1))
        chunk = items[self.page * PAGE:(self.page + 1) * PAGE]
        self.total = len(items)
        self.update_title()
        self.page_lbl.config(text=f"Page {self.page + 1} / {pages}")
        self.prev_b.config(state="normal" if self.page > 0 else "disabled")
        self.next_b.config(state="normal" if self.page < pages - 1 else "disabled")
        cols = max(1, self.canvas.winfo_width() // (TILE_W + 20))
        if not chunk:
            tk.Label(self.grid_frame, text="Nothing here yet. Use + Add folder to import videos.",
                     bg=BG, fg=MUTED, font=("Segoe UI", 12)).grid(padx=30, pady=40)
        for n, it in enumerate(chunk):
            self.make_tile(it).grid(row=n // cols, column=n % cols, padx=8, pady=8)

    def make_tile(self, it):
        f = tk.Frame(self.grid_frame, bg=CARD, highlightthickness=2,
                     highlightbackground=ACCENT if it["path"] in self.selected else EDGE)
        self.tile_frames[it["path"]] = f
        img = self.get_photo(it["path"])
        lbl = tk.Label(f, image=img or self.blank, text="" if img else "".join(self.icon(c) for c in it["categories"][:3]), compound="center",
                       bg=THUMB_BG, fg=ACCENT, font=(EMOJI, 32), width=TILE_W, height=TILE_H)
        lbl.pack()
        self.tile_labels[it["path"]] = lbl
        star = "\u2764 " if it["fav"] else ""
        t = tk.Label(f, text=star + it["title"], bg=CARD, fg=FG, anchor="w", width=32,
                     font=("Segoe UI", 10, "bold"))
        t.pack(fill="x", padx=8, pady=(6, 0))
        sub = " ".join(self.icon(c) for c in it["categories"]) + "  " + ", ".join(it["categories"]) + ("  \u00B7  " + ", ".join(it["tags"]) if it["tags"] else "")
        s = tk.Label(f, text=sub[:38], bg=CARD, fg=MUTED, anchor="w", font=(EMOJI, 9))
        s.pack(fill="x", padx=8, pady=(0, 8))
        for w in (f, lbl, t, s):
            w.bind("<Double-Button-1>", lambda e, p=it["path"]: self.play(p))
            w.bind("<Button-1>", lambda e, p=it["path"]: self.on_click(e, p))
            w.bind("<Button-3>", lambda e, p=it["path"]: self.item_menu(e, p))
        return f

    # ---------- thumbnails ----------
    def get_photo(self, path):
        if path in self.photos:
            return self.photos[path]
        tp = THUMB_DIR / (str(abs(hash(path))) + ".jpg")
        if Image and tp.exists():
            try:
                self.photos[path] = ImageTk.PhotoImage(Image.open(tp).resize((TILE_W, TILE_H)))
                return self.photos[path]
            except Exception:
                pass
        if Image and cv2 and os.path.exists(path):
            self.thumb_q.put((path, tp))
        return None

    def thumb_worker(self):
        while True:
            path, tp = self.thumb_q.get()
            try:
                cap = cv2.VideoCapture(path)
                total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                cap.set(cv2.CAP_PROP_POS_FRAMES, total // 10)
                ok, frame = cap.read()
                cap.release()
                if ok:
                    h, w = frame.shape[:2]
                    s = max(TILE_W / w, TILE_H / h)
                    frame = cv2.resize(frame, (int(w * s) + 1, int(h * s) + 1))
                    y, x = (frame.shape[0] - TILE_H) // 2, (frame.shape[1] - TILE_W) // 2
                    cv2.imwrite(str(tp), frame[y:y + TILE_H, x:x + TILE_W])
                    self.root.after(0, self.thumb_done, path, tp)
            except Exception:
                pass

    def thumb_done(self, path, tp):
        lbl = self.tile_labels.get(path)
        if lbl and lbl.winfo_exists():
            try:
                self.photos[path] = ImageTk.PhotoImage(Image.open(tp))
                lbl.config(image=self.photos[path], text="")
            except Exception:
                pass

    # ---------- actions ----------
    def play(self, path, external=False):
        if not os.path.exists(path):
            messagebox.showerror("File not found", f"This file is missing:\n{path}")
            return
        if vlc and self.data.get("builtin", True) and not external:
            paths = [i["path"] for i in self.filtered()]
            if path not in paths:
                paths = [path]
            Player(self, paths, paths.index(path))
        else:
            self.mark_played(path)
            os.startfile(path)

    def play_random(self):
        items = self.filtered()
        if items:
            self.play(random.choice(items)["path"])

    def item_menu(self, e, path):
        if path in self.selected and len(self.selected) > 1:
            return self.bulk_menu(e, list(self.selected))
        it = self.data["items"][path]
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Play", command=lambda: self.play(path))
        m.add_command(label="Play in default player", command=lambda: self.play(path, external=True))
        m.add_command(label="Show in folder", command=lambda: subprocess.Popen(["explorer", "/select,", path]))
        m.add_command(label="Unfavorite" if it["fav"] else "Favorite", command=lambda: self.set(path, fav=not it["fav"]))
        cm = tk.Menu(m, tearoff=0)
        for c in self.data["categories"]:
            v = tk.BooleanVar(value=c in it["categories"])
            cm.add_checkbutton(label=f"{self.icon(c)}  {c}", variable=v,
                               command=lambda c=c, v=v: self.toggle_cat(path, c, v.get()))
        m.add_cascade(label="Categories (tick all that apply)", menu=cm)
        m.add_command(label="Edit tags", command=lambda: self.edit_tags(path))
        m.add_command(label="Rename title", command=lambda: self.rename(path))
        m.add_separator()
        m.add_command(label="Remove from library", command=lambda: self.remove(path))
        m.tk_popup(e.x_root, e.y_root)

    def set(self, path, **kw):
        self.data["items"][path].update(kw)
        self.save(); self.refresh()

    def edit_tags(self, path):
        cur = ", ".join(self.data["items"][path]["tags"])
        s = simpledialog.askstring("Tags", "Comma-separated tags:", initialvalue=cur, parent=self.root)
        if s is not None:
            self.set(path, tags=[t.strip() for t in s.split(",") if t.strip()])

    def rename(self, path):
        s = simpledialog.askstring("Rename", "Title:", initialvalue=self.data["items"][path]["title"], parent=self.root)
        if s and s.strip():
            self.set(path, title=s.strip())

    def remove(self, path):
        if messagebox.askyesno("Remove", "Remove from library? The file itself is not deleted."):
            del self.data["items"][path]
            self.save(); self.refresh()


if __name__ == "__main__":
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    root.attributes("-alpha", 0)
    if App.check_pin(root):
        App(root)
        root.attributes("-alpha", 1)
        root.mainloop()
    else:
        root.destroy()
