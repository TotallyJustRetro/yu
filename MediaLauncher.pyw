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
import hashlib, json, os, queue, random, re, shutil, subprocess, threading, time
from collections import Counter, OrderedDict
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from pathlib import Path

try:
    from PIL import Image, ImageOps, ImageTk
except ImportError:
    Image = ImageOps = ImageTk = None
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
IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
THUMB_PX = 320
TILE_W, TILE_H, PAGE = 240, 135, 40

BG, PANEL, CARD = "#0a0206", "#14040b", "#210913"
FG, MUTED, ACCENT = "#fff0f6", "#c08aa3", "#ff1f7a"
CRIMSON, EDGE, THUMB_BG, NEON = "#b30047", "#4a1226", "#12040a", "#ff5fae"
CAT_COLORS = {"Twerking": "#ff7a59", "Femboy": "#ff6ec7", "Blowjob": "#ff2d55", "NSFW ASMR": "#b57bff",
              "Footjob": "#ff8fb8", "Yiff": "#ffb03a", "Uncategorized": "#ff1f7a"}
TAGLINES = [
    "Lights low. Volume up. 😈", "Been a bad boy tonight? 😏", "Pick your poison. 💋", "No judgement in here. 😘",
    "What are we in the mood for? 🔥", "Your dirty little secret, neatly organized. 🙈", "Behave? Never. 😈",
    "Come closer... the good stuff is one click away. 😏", "Take your time. I'm not going anywhere. 💋",
    "You've been so good all day. You deserve this. 🔥", "Lock the door. Silence the phone. Press play. 😈",
    "Sit back, relax, and let me spoil you. 💋", "You have excellent taste. Dangerous, even. 😏",
    "Guilty pleasures are still pleasures. 😘", "Wanting things is allowed. Go get it. 🔥",
    "Slow down. Savor it. 💋", "Tonight's agenda: absolutely nothing respectable. 😈",
    "Good boys get rewarded. Bad boys get rewarded more. 😏", "Don't think. Just indulge. 🔥",
    "Tease yourself a little. Browse before you press play. 😘", "You're allowed to be greedy tonight. 💋",
    "Everything here was chosen by you. Own it. 😈", "Your fantasies, your rules. 🔥",
    "Whisper-quiet? Or full volume? Your call. 😏", "You look good tonight. Yes, you. 💋",
    "Patience is sexy. So is a full library. 😏", "Be bad. Be brave. Be back tomorrow. 😘",
    "Nobody's watching but me. 🙈", "Let's make some questionable decisions. 😈",
    "Confidence looks good on you. So does that grin. 😏", "Open a folder. Open your mind. Open another folder. 😏",
    "Warm up with something sweet, then go wild. 🔥", "I won't tell if you don't. 🤫",
    "Tonight, you answer to no one. 💋", "Naughty list? You're the founder. 😈",
    "Take what you want. You've earned it. 🔥", "Slow scroll. Deep breath. Big smile. 😘",
    "Be selfish tonight. 💋", "Tease. Browse. Indulge. Repeat. 😈", "Dim the lights, turn up the heat. 🔥",
    "You're one click from a very good evening. 😏", "Temptation is just a thumbnail away. 😈",
    "Is it hot in here, or is it just your library? 🔥", "You've got that look again. I like it. 😘",
    "Pleasure is not a crime. It's a hobby. 😏", "Your secrets are safe in here. All of them. 🙈",
    "Nothing to see here... except everything. 😈", "Good things come to those who press play. 💋",
    "Hello, trouble. 😈", "Mmm, back already? Insatiable. 😏", "You deserve a little indulgence. 💋",
    "Mood: dangerously well-behaved... for now. 😈", "Be a good host to your own desires. 💋",
    "Feeling bold? So am I. 🔥", "Today's affirmation: I am allowed to enjoy myself. 😘",
    "Today's affirmation: I am irresistible. And I know it. 😏", "Today's affirmation: no guilt, only pleasure. 💋",
    "Today's affirmation: I take what I want, politely. 😈", "You are wanted. You are worthy. You are in the mood. 🔥",
]


def mix(a, b, t):
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in zip(ca, cb))


def tint(color, t):
    return mix("#0a0206", color, t)


def color_of(cat):
    return CAT_COLORS.get(cat, ACCENT)


LINES = {
    "girl": {
        "greet": ["Well, well... look who's back. Missed me? 😘", "Hey you. Lock the door, yeah? 😏",
                  "Ooh, I've been waiting. Pick something good for me 💋", "Back for more? Good. I like a regular. 😈"],
        "play": ["Ooh, '{t}'. Interesting choice... 😏", "'{t}', huh? You really do have taste 💋",
                 "Playing '{t}'. Try not to let me distract you... too much 😈", "Mmm, '{t}'. I approve. 🔥"],
        "repeat": ["That's play #{n} for '{t}'. Obsessed much? 😏", "'{t}' AGAIN? I'm starting to think you're attached 😘",
                   "{n} plays on '{t}'. I'm not jealous. (I'm a little jealous.) 😒"],
        "late": ["It's late... and yet here you are. Naughty. 😈", "Past midnight? Someone can't sleep... 😏",
                 "The best bad decisions happen after dark 💋"],
        "fav": ["'{t}' goes on the Hot list. Noted... and a little scandalized 🔥", "Ooh, a favorite! I'll remember that about you 😏"],
        "added": ["{n} new videos? You've been busy 😏", "Fresh stash! {n} new ones. Look at you go 😈"],
        "dupes": ["Skipped {d} doubles. Recycled fantasies, hm? 😅"],
        "idle": ["Still here? Good. Don't go anywhere 😘", "Staring at the screen... or at me? 😏",
                 "Hydrate. Stretch. Then come right back to me 💋"],
        "gallery": ["Ooh, the picture gallery. Let me see what you've got 👀", "Scroll slow. I want to see everything 😏"],
        "images": ["{n} new pictures! Let me guess... all for 'research'? 😏"],
        "empty": ["Your library's empty! Add a folder, babe. I'm bored 😘"],
        "affirm": ["You deserve every guilty pleasure on this screen 💋", "Wanting things is allowed. Treat yourself 😈",
                   "You look good tonight, and you know it 🔥", "Be bad. Be brave. Be back tomorrow 😘",
                   "Nobody's judging you in here. Not even me. Much. 😏"],
        "chat": ["Mmm, tell me more... or just press play 😏", "You're cute when you type. Keep going 💋",
                 "I'm all ears. And attitude 😈", "Is that all you've got? I expected more from you 😘"],
    },
    "boy": {
        "greet": ["Hiii~ missed me? 🎀", "Oh! You're back. I was getting lonely~ 💕", "Hey cutie. What are we watching tonight? 😏",
                  "Ready to be naughty? I am 🎀"],
        "play": ["'{t}'? Ooh, nice pick~ 🎀", "Mmm, '{t}'. You have such good taste, cutie 💕",
                 "'{t}' it is. I'm watching you watch 😏", "Ooh, '{t}'... I'm blushing 🥺"],
        "repeat": ["'{t}' for the {n}th time?! Okay okay, I see you~ 😳", "{n} plays?! Obsessed~ 🎀", "'{t}' again? I'm getting jealous 🥺"],
        "late": ["Up this late? Naughty naughty~ 🌙", "Midnight vibes... my favorite~ 💕", "Can't sleep? Same~ 😏"],
        "fav": ["'{t}' on the Hot list! Noted~ 🔥🎀", "A favorite! You're so predictable... and I love it 😘"],
        "added": ["{n} new videos?! Look at you go~ 😏", "Ooh, fresh stash! {n} new ones 🎀"],
        "dupes": ["Skipped {d} doubles~ Recycling, hm? 😅"],
        "idle": ["Still here~ Don't leave me 🥺", "You're staring. Not that I mind 😏", "Water break, cutie. Then back to me 💕"],
        "gallery": ["Ooh, pictures! Show me, show me~ 👀", "Scroll slow, I want to see everything 😏"],
        "images": ["{n} new pictures! For 'research', obviously 😏"],
        "empty": ["Your library's empty! Add a folder, cutie. I'm bored 🥺"],
        "affirm": ["You deserve every guilty pleasure on this screen 💕", "Wanting things is allowed. Treat yourself~ 😈",
                   "You look good tonight, and you know it 🔥", "Be bad. Be brave. Be back tomorrow 😘",
                   "Nobody's judging. Not even me~ 🎀"],
        "chat": ["Mmm, tell me more~ or just press play 😏", "You're cute when you type 💕", "I'm all ears~ and attitude 😈",
                 "Is that all? I expected more from you 😘"],
    },
}
CATLINES = {
    "girl": {
        "Twerking": ["Someone's got rhythm. Can't blame you 🍑", "Ooh, the booty category. Classic. 😏"],
        "Femboy": ["Femboy night? Great taste 🎀", "Cute AND naughty? Say less 😘"],
        "Blowjob": ["Straight to the point, huh? No foreplay with me, rude 😏", "Bold. I respect the directness 😈"],
        "NSFW ASMR": ["Headphones on? Smart. Whispers hit different 🎧", "Turn it down, the neighbors will hear 😏"],
        "Footjob": ["Ooh, someone has a type. No judgement 😏", "Feet, huh? Everyone's got a thing 🦶💋"],
        "Yiff": ["Furry hours! Wag that tail 🐺", "Fandom night. I'm here for it 😈"],
        "_": ["{c}, huh? Interesting choice 😏", "Mmm, {c}. You surprise me sometimes 💋"],
    },
    "boy": {
        "Twerking": ["Ooh, rhythm! I'm taking notes~ 🍑", "The booty category again? Classic you 😏"],
        "Femboy": ["Femboy night? Hehe, I'm flattered~ 🎀", "Cute AND naughty? You know my type 😘"],
        "Blowjob": ["Straight to the point, huh? Bold~ 😳", "No small talk tonight, I see 😈"],
        "NSFW ASMR": ["Headphones on? Good call. Whispers hit different 🎧", "Turn it down, cutie, the neighbors~ 😏"],
        "Footjob": ["Ooh, someone has a type~ no judgement 😏", "Feet, huh? Everyone's got a thing 🦶💕"],
        "Yiff": ["Furry hours! Wag wag~ 🐺", "Fandom night! I'm so here for it 😈"],
        "_": ["{c}, huh? Interesting choice~ 😏", "Mmm, {c}. You're full of surprises 💕"],
    },
}
KEYWORDS = {"4k": "4K? Fancy. You like it crisp 😏", "hd": "HD only. High standards, I like that 😘",
            "pov": "POV, huh? You like to feel involved 😈", "compilation": "A compilation. Efficient. I like that 💋",
            "cosplay": "Cosplay! The details matter 🎀", "asmr": "Headphones on, right? 🎧",
            "solo": "Solo night? Nothing wrong with that 😏"}
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
        self.title("Naughty Player \U0001F608")
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


class ImageViewer(tk.Toplevel):
    """Full-window picture viewer: arrows browse, wheel zooms, drag pans, Space = slideshow, F = fullscreen."""

    def __init__(self, app, paths, index):
        super().__init__(app.root)
        self.app, self.paths, self.i = app, paths, index
        self.zoom, self.full, self.show, self.photo, self.im, self._job = 1.0, False, False, None, None, None
        self.title("Naughty Gallery 😈")
        self.geometry("1100x760")
        self.configure(bg="black")
        self.c = tk.Canvas(self, bg="black", highlightthickness=0)
        self.c.pack(fill="both", expand=True)
        self.cap = tk.Label(self, bg="#12020a", fg=MUTED, anchor="w", font=(EMOJI, 10), padx=12, pady=5)
        self.cap.pack(fill="x")
        self.bind("<Right>", lambda e: self.step(1))
        self.bind("<Left>", lambda e: self.step(-1))
        self.bind("<space>", lambda e: self.toggle_show())
        self.bind("<f>", lambda e: self.fullscreen())
        self.bind("<Escape>", lambda e: self.fullscreen() if self.full else self.close())
        self.bind("<plus>", lambda e: self.set_zoom(self.zoom * 1.25))
        self.bind("<equal>", lambda e: self.set_zoom(self.zoom * 1.25))
        self.bind("<minus>", lambda e: self.set_zoom(self.zoom / 1.25))
        self.bind("<0>", lambda e: self.set_zoom(1.0))
        self.c.bind("<MouseWheel>", lambda e: self.set_zoom(self.zoom * (1.15 if e.delta > 0 else 1 / 1.15)))
        self.c.bind("<ButtonPress-1>", lambda e: self.c.scan_mark(e.x, e.y))
        self.c.bind("<B1-Motion>", lambda e: self.c.scan_dragto(e.x, e.y, gain=1))
        self.c.bind("<Double-Button-1>", lambda e: self.set_zoom(1.0 if self.zoom > 1 else 2.5))
        self.c.bind("<Configure>", lambda e: self.redraw_soon())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.load()
        self.focus_force()

    def load(self):
        p = self.paths[self.i]
        try:
            im = ImageOps.exif_transpose(Image.open(p))
            self.im = im.convert("RGB")
            info = f"{self.im.width}x{self.im.height}"
        except Exception:
            self.im, info = None, "can't open this file"
        self.zoom = 1.0
        self.cap.config(text=f"{self.i + 1} / {len(self.paths)}   ·   {Path(p).name}   ·   {info}"
                             f"   ·   ← → browse   wheel zoom   Space slideshow {'(ON)' if self.show else ''}   F fullscreen")
        self.draw()

    def redraw_soon(self):
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(40, self.draw)

    def set_zoom(self, z):
        self.zoom = max(1.0, min(10.0, z))
        self.redraw_soon()

    def draw(self):
        self._job = None
        if not self.winfo_exists():
            return
        self.c.delete("all")
        cw, ch = self.c.winfo_width(), self.c.winfo_height()
        if self.im is None or cw < 20 or ch < 20:
            return
        w, h = self.im.size
        scale = min(cw / w, ch / h, 2.0) * self.zoom
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        im = self.im.resize((nw, nh), Image.LANCZOS if self.zoom == 1.0 else Image.BILINEAR)
        self.photo = ImageTk.PhotoImage(im)
        self.c.create_image(cw // 2, ch // 2, image=self.photo, anchor="center")

    def step(self, d):
        self.i = (self.i + d) % len(self.paths)
        self.load()

    def toggle_show(self):
        self.show = not self.show
        self.load()
        if self.show:
            self.after(3500, self.tick)

    def tick(self):
        if self.show and self.winfo_exists():
            self.step(1)
            self.after(3500, self.tick)

    def fullscreen(self):
        self.full = not self.full
        self.attributes("-fullscreen", self.full)
        if self.full:
            self.cap.pack_forget()
        else:
            self.cap.pack(fill="x")

    def close(self):
        self.show = False
        self.destroy()


class App:
    def __init__(self, root):
        self.root = root
        root.title("Naughty Room \U0001F608")
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

        self.build_banner()
        self.footer = tk.Label(self.root, text="\U0001F648 Boss key: F12   \u00B7   \U0001F512 PIN lock in the sidebar   \u00B7   Your secret's safe here \U0001F618",
                               bg="#12020a", fg=MUTED, anchor="w", font=(EMOJI, 9), padx=12, pady=4)
        self.footer.pack(side="bottom", fill="x")
        side = tk.Frame(self.root, bg=PANEL, width=240)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="WHAT'S THE MOOD? \U0001F60F", bg=PANEL, fg=ACCENT, font=("Segoe UI Black", 11)).pack(anchor="w", padx=14, pady=(14, 2))
        tk.Frame(side, height=2, bg=ACCENT).pack(fill="x", padx=14, pady=(2, 10))
        self.nav = tk.Listbox(side, bg=PANEL, fg=FG, bd=0, highlightthickness=0, activestyle="none",
                              selectbackground=ACCENT, selectforeground="white", font=(EMOJI, 12))
        self.nav.pack(fill="both", expand=True, padx=8)
        self.nav.bind("<<ListboxSelect>>", self.on_nav)
        self.nav.bind("<Button-3>", self.nav_menu)
        for text, cmd in [("\U0001F48B  New mood", self.add_category), ("\U0001F4C1  Add a stash (folder)", self.add_folder),
                          ("\U0001F39E  Add naughty videos", self.add_files), ("\U0001F50D  Catch the doubles", self.find_dupes), ("\U0001F512  Lock it down", self.set_pin)]:
            tk.Button(side, text=text, command=cmd, bg=CARD, fg=FG, bd=0, activebackground=ACCENT,
                      activeforeground="white", font=(EMOJI, 10), pady=6).pack(fill="x", padx=10, pady=3)
        tk.Frame(side, height=8, bg=PANEL).pack()

        self.main = main = tk.Frame(self.root, bg=BG)
        main.pack(side="left", fill="both", expand=True)
        bar = tk.Frame(main, bg=BG)
        bar.pack(fill="x", padx=16, pady=12)
        self.mode, self.mode_btns = "videos", {}
        for key, txt in (("videos", "🎞 Videos"), ("images", "🖼 Gallery")):
            mb = tk.Button(bar, text=txt, command=lambda k=key: self.set_mode(k), bd=0, font=(EMOJI, 10, "bold"), padx=12, pady=5)
            mb.pack(side="left", padx=(0, 6))
            self.mode_btns[key] = mb
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *a: (self.toggle_ph(), self.reset_page()))
        e = tk.Entry(bar, textvariable=self.search, bg=CARD, fg=FG, insertbackground=ACCENT, bd=0, font=("Segoe UI", 12),
                     highlightthickness=1, highlightbackground=EDGE, highlightcolor=ACCENT)
        e.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 10))
        self.ph = tk.Label(e, text="Search your stash... titles, tags, moods", bg=CARD, fg=MUTED,
                           font=("Segoe UI", 11, "italic"), cursor="xterm")
        self.ph.place(x=10, rely=0.5, anchor="w")
        self.ph.bind("<Button-1>", lambda ev: e.focus_set())
        self.sort = ttk.Combobox(bar, values=["Newest", "Name", "Most played"], state="readonly", width=12)
        self.sort.set("Newest")
        self.sort.bind("<<ComboboxSelected>>", lambda e: self.reset_page())
        self.sort.pack(side="left", padx=4)
        tk.Button(bar, text="\U0001F608 Surprise me", command=self.play_random, bg=ACCENT, fg="white", bd=0,
                  font=("Segoe UI", 10, "bold"), padx=14, pady=5).pack(side="left", padx=4)
        self.pl_btn = tk.Button(bar, command=self.toggle_player, bg=CARD, fg=FG, bd=0, font=(EMOJI, 10), padx=10, pady=5)
        self.pl_btn.pack(side="left", padx=4)
        self.update_pl_btn()

        self.title = tk.Label(main, text="", bg=BG, fg=MUTED, font=("Segoe UI", 10), anchor="w")
        self.title.pack(fill="x", padx=18)

        self.wrap = wrap = tk.Frame(main, bg=BG)
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
        self.root.bind_all("<MouseWheel>", self.on_wheel)

        self.pager = pager = tk.Frame(main, bg=BG)
        pager.pack(fill="x", pady=(0, 8))
        self.prev_b = tk.Button(pager, text="< Prev", command=lambda: self.turn(-1), bg=CARD, fg=FG, bd=0, padx=12)
        self.next_b = tk.Button(pager, text="Next >", command=lambda: self.turn(1), bg=CARD, fg=FG, bd=0, padx=12)
        self.page_lbl = tk.Label(pager, bg=BG, fg=MUTED)
        self.prev_b.pack(side="left", padx=16)
        self.next_b.pack(side="right", padx=16)
        self.page_lbl.pack()
        self._resize_job = None
        self.build_gallery()
        self.build_stella()
        self.set_mode("videos")
        if TkinterDnD:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self.on_drop)

    def on_resize(self, e):
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(150, self.render)

    # ---------- modes / wheel ----------
    def set_mode(self, m):
        if m == "images" and not Image:
            messagebox.showinfo("Gallery", "The gallery needs Pillow:\n\npip install pillow")
            m = "videos"
        self.mode = m
        if m == "images":
            for w in (self.title, self.wrap, self.pager):
                w.pack_forget()
            self.gframe.pack(fill="both", expand=True)
            self.root.after(40, self.g_layout)
            self.stella_event("gallery")
        else:
            self.gframe.pack_forget()
            self.title.pack(fill="x", padx=18)
            self.wrap.pack(fill="both", expand=True, padx=10, pady=6)
            self.pager.pack(fill="x", pady=(0, 8))
        for k, b in self.mode_btns.items():
            b.config(bg=ACCENT if k == m else CARD, fg="white" if k == m else FG, activebackground=NEON)

    def on_wheel(self, e):
        try:
            if e.widget.winfo_toplevel() is not self.root:
                return
        except Exception:
            return
        if self.mode == "images":
            if str(e.widget).startswith(str(self.gframe)):
                self.g_wheel(e)
        else:
            self.canvas.yview_scroll(-1 * (e.delta // 120), "units")

    # ---------- image gallery (virtualised, smooth) ----------
    def build_gallery(self):
        self.gframe = tk.Frame(self.main, bg=BG)
        self.gitems, self.gshown, self.gphotos = [], {}, OrderedDict()
        self.gpending, self.gwant, self.gq_in, self.gq_out = set(), set(), queue.Queue(), queue.Queue()
        self.gcols, self.gcell, self.gsize, self.goff, self.gtotal, self.gsr = 1, 208, 200, 8, 0, 1
        self.g_anim, self.g_target, self._gjob = False, 0.0, None
        tb = tk.Frame(self.gframe, bg=BG)
        tb.pack(fill="x", padx=16, pady=(0, 6))
        for txt, cmd in (("🖼  Add images", self.add_images), ("📁  Add picture folder", self.add_image_folder)):
            tk.Button(tb, text=txt, command=cmd, bg=CARD, fg=FG, bd=0, activebackground=ACCENT, activeforeground="white",
                      font=(EMOJI, 10), padx=12, pady=5).pack(side="left", padx=(0, 8))
        self.gfav = tk.BooleanVar(value=False)
        tk.Checkbutton(tb, text="🔥 Hot only", variable=self.gfav, command=self.g_layout, bg=BG, fg=FG, selectcolor=CARD,
                       activebackground=BG, activeforeground=ACCENT, font=(EMOJI, 10)).pack(side="left", padx=8)
        tk.Label(tb, text="Size", bg=BG, fg=MUTED).pack(side="left", padx=(10, 4))
        self.gscale = ttk.Scale(tb, from_=120, to=360, length=150, command=self.g_size)
        self.gscale.set(self.data.get("gsize", 200))
        self.gscale.pack(side="left")
        self.gcount = tk.Label(tb, bg=BG, fg=MUTED)
        self.gcount.pack(side="right")
        box = tk.Frame(self.gframe, bg=BG)
        box.pack(fill="both", expand=True, padx=10)
        self.gc = tk.Canvas(box, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.g_yview)
        self.gc.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.gc.pack(side="left", fill="both", expand=True)
        self.gc.bind("<Configure>", lambda e: self.g_size(None))
        self.gc.bind("<Motion>", self.g_motion)
        self.gc.bind("<Leave>", lambda e: self.gc.delete("hov"))
        self.gc.bind("<Button-1>", self.g_click)
        self.gc.bind("<Button-3>", self.g_menu)
        if Image:
            for _ in range(2):
                threading.Thread(target=self.g_worker, daemon=True).start()
            self.root.after(25, self.g_poll)

    def g_size(self, v):
        if self._gjob:
            self.root.after_cancel(self._gjob)
        self._gjob = self.root.after(120, self.g_resized)

    def g_resized(self):
        self._gjob = None
        self.data["gsize"] = int(float(self.gscale.get()))
        self.save()
        if self.mode == "images":
            self.g_layout()

    def g_filtered(self):
        items = list(self.data.get("images", {}).values())
        q = self.search.get().lower().strip()
        if q:
            items = [i for i in items if q in os.path.basename(i["path"]).lower() or any(q in t.lower() for t in i["tags"])]
        if self.gfav.get():
            items = [i for i in items if i.get("fav")]
        if self.sort.get() == "Name":
            items.sort(key=lambda i: os.path.basename(i["path"]).lower())
        else:
            items.sort(key=lambda i: -i["added"])
        return items

    def g_layout(self):
        self.gitems = self.g_filtered()
        W = max(self.gc.winfo_width(), 300)
        self.gsize = int(float(self.gscale.get()))
        self.gcell = self.gsize + 8
        self.gcols = max(1, (W - 8) // self.gcell)
        self.goff = max(8, (W - self.gcols * self.gcell + 8) // 2)
        rows = -(-len(self.gitems) // self.gcols)
        self.gtotal = rows * self.gcell + 16
        self.gsr = max(self.gtotal, self.gc.winfo_height(), 1)
        self.gc.configure(scrollregion=(0, 0, W, self.gsr))
        self.gc.delete("all")
        self.gshown = {}
        self.gcount.config(text=f"{len(self.gitems)} pictures")
        if not self.gitems:
            self.gc.create_text(W // 2, 120, text="No pictures yet. Drag some in, or hit Add images 😏", fill=MUTED,
                                font=("Segoe UI", 13, "italic"))
        self.g_update()

    def g_update(self):
        n = len(self.gitems)
        if not n:
            return
        y0 = self.gc.canvasy(0)
        y1 = y0 + self.gc.winfo_height()
        lo = max(0, int(y0 // self.gcell) - 1) * self.gcols
        hi = min(n, (int(y1 // self.gcell) + 2) * self.gcols)
        for idx in [i for i in self.gshown if i < lo or i >= hi]:
            for cid in self.gshown.pop(idx)["ids"]:
                self.gc.delete(cid)
        self.gwant = {(self.gitems[i]["path"], self.gsize) for i in range(lo, hi)}
        for idx in range(lo, hi):
            if idx not in self.gshown:
                self.g_draw(idx)

    def g_draw(self, idx):
        it, sz = self.gitems[idx], self.gsize
        x = self.goff + (idx % self.gcols) * self.gcell
        y = 8 + (idx // self.gcols) * self.gcell
        ids = [self.gc.create_rectangle(x, y, x + sz, y + sz, fill=tint(ACCENT, 0.12), outline=EDGE)]
        d = {"ids": ids, "img": None, "x": x, "y": y}
        key = (it["path"], sz)
        ph = self.gphotos.get(key)
        if ph:
            self.gphotos.move_to_end(key)
            d["img"] = self.gc.create_image(x, y, image=ph, anchor="nw")
            ids.append(d["img"])
        elif key not in self.gpending:
            self.gpending.add(key)
            self.gq_in.put(key)
        if it.get("fav"):
            ids.append(self.gc.create_text(x + sz - 6, y + 6, text="🔥", anchor="ne", font=(EMOJI, 13), tags="fav"))
        self.gshown[idx] = d

    def g_worker(self):
        while True:
            key = self.gq_in.get()
            im = None
            if key in self.gwant:
                try:
                    im = self.g_thumb(*key)
                except Exception:
                    im = None
            self.gq_out.put((key, im))

    def g_thumb(self, path, size):
        tp = THUMB_DIR / ("i" + hashlib.md5((path + str(os.path.getmtime(path))).encode()).hexdigest() + ".jpg")
        if tp.exists():
            im = Image.open(tp)
            im.load()
        else:
            im = Image.open(path)
            try:
                im.draft("RGB", (THUMB_PX * 2, THUMB_PX * 2))
            except Exception:
                pass
            im = ImageOps.fit(ImageOps.exif_transpose(im).convert("RGB"), (THUMB_PX, THUMB_PX), Image.LANCZOS)
            im.save(tp, quality=85)
        return im if size == THUMB_PX else im.resize((size, size), Image.BILINEAR)

    def g_poll(self):
        for _ in range(8):
            try:
                key, im = self.gq_out.get_nowait()
            except queue.Empty:
                break
            self.gpending.discard(key)
            if im is None:
                continue
            self.gphotos[key] = ImageTk.PhotoImage(im)
            while len(self.gphotos) > 500:
                self.gphotos.popitem(last=False)
            self.g_attach(key)
        self.root.after(25, self.g_poll)

    def g_attach(self, key):
        path, size = key
        if size != self.gsize:
            return
        for idx, d in self.gshown.items():
            if d["img"] is None and idx < len(self.gitems) and self.gitems[idx]["path"] == path:
                d["img"] = self.gc.create_image(d["x"], d["y"], image=self.gphotos[key], anchor="nw")
                d["ids"].append(d["img"])
        self.gc.tag_raise("fav")
        self.gc.tag_raise("hov")

    def g_yview(self, *args):
        self.gc.yview(*args)
        self.g_target = self.gc.canvasy(0)
        self.g_update()

    def g_wheel(self, e):
        if not self.g_anim:
            self.g_target = self.gc.canvasy(0)
        top = max(0, self.gsr - self.gc.winfo_height())
        self.g_target = min(max(0, self.g_target - e.delta * 1.3), top)
        if not self.g_anim:
            self.g_anim = True
            self.g_step()

    def g_step(self):
        cur = self.gc.canvasy(0)
        d = self.g_target - cur
        if abs(d) < 1:
            self.g_anim = False
            return
        self.gc.yview_moveto((cur + d * 0.22) / self.gsr)
        self.g_update()
        self.root.after(14, self.g_step)

    def g_idx_at(self, e):
        x, y = self.gc.canvasx(e.x), self.gc.canvasy(e.y)
        col, row = int((x - self.goff) // self.gcell), int((y - 8) // self.gcell)
        if 0 <= col < self.gcols and row >= 0:
            idx = row * self.gcols + col
            if idx < len(self.gitems) and (x - self.goff) % self.gcell <= self.gsize:
                return idx
        return None

    def g_motion(self, e):
        self.gc.delete("hov")
        idx = self.g_idx_at(e)
        self.gc.config(cursor="hand2" if idx is not None else "")
        if idx is not None and idx in self.gshown:
            d = self.gshown[idx]
            self.gc.create_rectangle(d["x"] - 2, d["y"] - 2, d["x"] + self.gsize + 2, d["y"] + self.gsize + 2,
                                     outline=NEON, width=3, tags="hov")

    def g_click(self, e):
        idx = self.g_idx_at(e)
        if idx is not None:
            ImageViewer(self, [i["path"] for i in self.gitems], idx)
            if random.random() < 0.3:
                self.stella_event("gallery")

    def g_menu(self, e):
        idx = self.g_idx_at(e)
        if idx is None:
            return
        it = self.gitems[idx]
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Open", command=lambda: ImageViewer(self, [i["path"] for i in self.gitems], idx))
        m.add_command(label="Remove from Hot list" if it.get("fav") else "Add to Hot list 🔥", command=lambda: self.g_fav(it))
        m.add_command(label="Show in folder", command=lambda: subprocess.Popen(["explorer", "/select,", it["path"]]))
        m.add_separator()
        m.add_command(label="Remove from gallery", command=lambda: self.g_remove(it))
        m.tk_popup(e.x_root, e.y_root)

    def g_fav(self, it):
        it["fav"] = not it.get("fav")
        self.save()
        self.g_layout()

    def g_remove(self, it):
        self.data["images"].pop(it["path"], None)
        self.save()
        self.g_layout()

    def add_image_paths(self, paths):
        imgs = self.data.setdefault("images", {})
        n = 0
        for p in paths:
            p = os.path.normpath(p)
            if p not in imgs and Path(p).suffix.lower() in IMG_EXT:
                imgs[p] = dict(path=p, added=time.time(), fav=False, tags=[])
                n += 1
        self.save()
        if self.mode == "images":
            self.g_layout()
        return n

    def add_images(self):
        fs = filedialog.askopenfilenames(title="Select pictures",
                                         filetypes=[("Pictures", " ".join("*" + e for e in IMG_EXT))])
        if fs:
            self.stella_event("images", n=self.add_image_paths(fs))

    def add_image_folder(self):
        d = filedialog.askdirectory(title="Select a picture folder")
        if d:
            found = [os.path.join(r, f) for r, _, fs in os.walk(d) for f in fs if Path(f).suffix.lower() in IMG_EXT]
            n = self.add_image_paths(found)
            self.stella_event("images", n=n)
            messagebox.showinfo("Picture folder", f"Added {n} new pictures.")

    # ---------- Stella: offline companion (reads titles, tags, categories, play counts; never leaves your PC) ----------
    def persona(self):
        return self.data["stella"]["persona"]

    def line(self, key, **kw):
        return random.choice(LINES[self.persona()][key]).format(**kw)

    def build_stella(self):
        st = self.data.setdefault("stella", {})
        st.setdefault("persona", "girl")
        st.setdefault("names", {"girl": "Stella", "boy": "Stel"})
        st.setdefault("avatars", {})
        st.setdefault("muted", False)
        self.sugg, self.mini = None, False
        self.sw = tk.Frame(self.root, bg=PANEL, highlightthickness=2, highlightbackground=ACCENT, padx=8, pady=8)
        self.bubble = tk.Label(self.sw, text="", bg=CARD, fg=FG, wraplength=210, justify="left", font=(EMOJI, 10), padx=10, pady=8)
        self.bubble.pack(fill="x")
        self.play_btn = tk.Button(self.sw, text="\u25B6  Play it", command=self.play_sugg, bg=ACCENT, fg="white", bd=0,
                                  font=(EMOJI, 9, "bold"), pady=3)
        self.av = tk.Canvas(self.sw, width=210, height=150, bg=PANEL, highlightthickness=0)
        self.av.pack(pady=(8, 0))
        self.name_lbl = tk.Label(self.sw, bg=PANEL, fg=ACCENT, font=("Segoe UI Black", 11))
        self.name_lbl.pack()
        self.chat = tk.Entry(self.sw, bg=CARD, fg=FG, insertbackground=ACCENT, bd=0, font=("Segoe UI", 10),
                             highlightthickness=1, highlightbackground=EDGE, highlightcolor=ACCENT)
        self.chat.pack(fill="x", pady=(6, 0), ipady=4)
        self.chat.bind("<Return>", self.stella_chat)
        self.av.bind("<Button-1>", lambda e: self.stella_event("poke"))
        self.av.bind("<Button-3>", self.stella_menu)
        self.sbtn = tk.Button(self.root, text="💋", command=self.stella_mini, bg=ACCENT, fg="white", bd=0, font=(EMOJI, 16), padx=8)
        self.sw.place(relx=1.0, rely=1.0, x=-14, y=-38, anchor="se")
        self.draw_avatar()
        self.root.after(1600, lambda: self.stella_event("greet"))
        self.root.after(120000, self.stella_idle)

    def draw_avatar(self):
        st = self.data["stella"]
        P = st["persona"]
        c = self.av
        c.delete("all")
        self.name_lbl.config(text=st["names"][P] + (" 🎀" if P == "boy" else " 💋"))
        path = st["avatars"].get(P)
        self.av_photo = None
        if path and os.path.exists(path) and Image:
            try:
                im = Image.open(path).convert("RGBA")
                im.thumbnail((210, 150))
                self.av_photo = ImageTk.PhotoImage(im)
                c.create_image(105, 75, image=self.av_photo)
                return
            except Exception:
                pass
        col = "#ff6ec7" if P == "boy" else ACCENT
        c.create_oval(55, 8, 155, 108, fill=tint(col, 0.35), outline=col, width=3)
        c.create_text(105, 58, text="🎀" if P == "boy" else "💋", font=(EMOJI, 44), fill=col)
        c.create_text(105, 132, text="right-click me → Change picture", font=("Segoe UI", 8, "italic"), fill=MUTED)

    def stella_say(self, text, sugg=None):
        self.bubble.config(text=text)
        self.sugg = sugg
        if sugg:
            self.play_btn.pack(after=self.bubble, fill="x", pady=(4, 0))
        else:
            self.play_btn.pack_forget()

    def play_sugg(self):
        p, self.sugg = self.sugg, None
        self.play_btn.pack_forget()
        if p and p in self.data["items"]:
            self.play(p)

    def stella_mini(self):
        self.mini = not self.mini
        if self.mini:
            self.sw.place_forget()
            self.sbtn.place(relx=1.0, rely=1.0, x=-14, y=-38, anchor="se")
        else:
            self.sbtn.place_forget()
            self.sw.place(relx=1.0, rely=1.0, x=-14, y=-38, anchor="se")

    def stella_menu(self, e):
        st = self.data["stella"]
        other = "boy" if st["persona"] == "girl" else "girl"
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label=f"Switch to {st['names'][other]}", command=self.stella_switch)
        m.add_command(label="Rename", command=self.stella_rename)
        m.add_command(label="Change picture...", command=self.stella_pic)
        m.add_command(label="Unmute chatter" if st["muted"] else "Mute chatter", command=self.stella_mute)
        m.add_command(label="Minimize", command=self.stella_mini)
        m.tk_popup(e.x_root, e.y_root)

    def stella_switch(self):
        st = self.data["stella"]
        st["persona"] = "boy" if st["persona"] == "girl" else "girl"
        self.save()
        self.draw_avatar()
        self.stella_say(self.line("greet"))

    def stella_rename(self):
        st = self.data["stella"]
        n = simpledialog.askstring("Rename", "New name:", initialvalue=st["names"][st["persona"]], parent=self.root)
        if n and n.strip():
            st["names"][st["persona"]] = n.strip()
            self.save()
            self.draw_avatar()

    def stella_pic(self):
        f = filedialog.askopenfilename(title="Pick a picture (PNG with transparency looks best)",
                                       filetypes=[("Pictures", " ".join("*" + e for e in IMG_EXT))])
        if not f:
            return
        st = self.data["stella"]
        dest_dir = APP_DIR / "characters"
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / (st["persona"] + Path(f).suffix.lower())
        shutil.copy(f, dest)
        st["avatars"][st["persona"]] = str(dest)
        self.save()
        self.draw_avatar()
        self.stella_say("Ooh, a new look. Do I look good? 😘")

    def stella_mute(self):
        st = self.data["stella"]
        st["muted"] = not st["muted"]
        self.save()
        self.stella_say("Okay, I'll be quiet... 🤐 (poke me if you miss me)" if st["muted"] else "I'm back! Did you miss me? 😘")

    def top_cat(self):
        c = Counter()
        for i in self.data["items"].values():
            for k in i["categories"]:
                c[k] += 1 + i["plays"]
        return c.most_common(1)[0][0] if c else None

    def recommend(self):
        items = list(self.data["items"].values())
        if not items:
            return None
        pool = [i for i in items if i["plays"] == 0] or [i for i in items if i["fav"]] or items
        top = self.top_cat()
        return random.choice([i for i in pool if top in i["categories"]] or pool)

    def stats(self):
        items = list(self.data["items"].values())
        day = sum(1 for x in self.data.get("plays_log", []) if time.time() - x < 86400)
        return dict(n=len(items), imgs=len(self.data.get("images", {})), hot=sum(1 for i in items if i["fav"]),
                    never=sum(1 for i in items if i["plays"] == 0), day=day, top=self.top_cat(),
                    topn=sum(1 for i in items if self.top_cat() in i["categories"]))

    def stats_line(self):
        s = self.stats()
        if not s["n"]:
            return self.line("empty")
        opts = [f"Your #1 mood is {s['top']} ({s['topn']} videos). I'm not judging... much 😏"]
        if s["never"]:
            opts.append(f"{s['never']} videos you've never even clicked. Neglected much? 💋")
        if s["hot"]:
            opts.append(f"{s['hot']} on your Hot list. Greedy 🔥")
        if s["day"] >= 3:
            opts.append(f"{s['day']} videos in the last 24 hours. Busy, busy 😈")
        if s["imgs"]:
            opts.append(f"{s['imgs']} pictures in your gallery. Scroll away 👀")
        return random.choice(opts)

    def stella_idle(self):
        if not self.data["stella"]["muted"] and not self.mini:
            self.stella_say(random.choice([self.line("idle"), self.line("affirm"), self.stats_line()]))
        self.root.after(random.randint(90000, 180000), self.stella_idle)

    def stella_event(self, kind, **kw):
        st = self.data.get("stella")
        if not st or (st["muted"] and kind != "poke"):
            return
        if kind == "greet":
            t = self.line("greet") if self.data["items"] else self.line("empty")
            if not st["avatars"].get(st["persona"]):
                t += "  (Right-click me to give me a face!)"
            self.stella_say(t)
        elif kind == "poke":
            self.stella_say(random.choice([self.line("affirm"), self.line("idle"), self.stats_line()]))
        elif kind == "added":
            t = self.line("added", n=kw["n"]) if kw["n"] else "Nothing new this time..."
            if kw["d"]:
                t += "  " + self.line("dupes", d=kw["d"])
            self.stella_say(t)
        elif kind == "images" and kw["n"]:
            self.stella_say(self.line("images", n=kw["n"]))
        elif kind == "fav":
            self.stella_say(self.line("fav", t=kw["t"]))
        elif kind == "gallery":
            self.stella_say(self.line("gallery"))

    def stella_play(self, it):
        st = self.data.get("stella")
        if not st or st["muted"]:
            return
        P, t, n = st["persona"], it["title"], it["plays"]
        opts = [self.line("repeat", t=t, n=n)] * 2 if n >= 3 else [self.line("play", t=t)]
        cat = random.choice(it["categories"]) if it["categories"] else "Uncategorized"
        opts.append(random.choice(CATLINES[P].get(cat, CATLINES[P]["_"])).format(c=cat))
        words = set(re.findall(r"[a-z0-9]+", (t + " " + " ".join(it["tags"])).lower()))
        opts += [v for k, v in KEYWORDS.items() if k in words]
        h = time.localtime().tm_hour
        if h < 5 or h >= 23:
            opts.append(self.line("late"))
        day = sum(1 for x in self.data.get("plays_log", []) if time.time() - x < 86400)
        if day >= 6:
            opts.append(f"That's video number {day} today. Somebody's busy 😏")
        self.stella_say(random.choice(opts))

    def stella_chat(self, e=None):
        msg = self.chat.get().strip().lower()
        self.chat.delete(0, "end")
        if not msg:
            return
        st = self.data["stella"]
        has = lambda *w: any(x in msg for x in w)
        if has("unmute", "talk to me"):
            st["muted"] = False
            self.save()
            self.stella_say("Finally! I was dying to talk 😘")
        elif has("mute", "quiet", "shut up"):
            st["muted"] = True
            self.save()
            self.stella_say("Okay, I'll be quiet... 🤐 (poke me if you miss me)")
        elif has("recommend", "suggest", "what should", "bored", "surprise", "pick"):
            it = self.recommend()
            if not it:
                self.stella_say(self.line("empty"))
            else:
                why = ("You've never even played this one" if it["plays"] == 0
                       else "It's a favorite of yours" if it["fav"] else "It fits your favorite mood")
                self.stella_say(f"How about '{it['title']}'? {why}... 😏", sugg=it["path"])
        elif has("stat", "how many", "library", "collection"):
            s = self.stats()
            self.stella_say(f"{s['n']} videos, {s['imgs']} pictures, {s['hot']} on your Hot list. "
                            f"Top mood: {s['top'] or 'none yet'}. {s['day']} plays in the last 24h 😏")
        elif has("who are you", "your name", "what are you"):
            self.stella_say(f"I'm {st['names'][st['persona']]}. I live in your launcher, read your titles, tags and "
                            "play counts, and keep your secrets. I can't see inside the videos themselves though 😘")
        elif has("affirm", "compliment", "something nice", "praise"):
            self.stella_say(self.line("affirm"))
        elif has("hi", "hey", "hello", "yo"):
            self.stella_say(self.line("greet"))
        elif has("thank", "thx"):
            self.stella_say("Anytime, gorgeous 💋")
        elif has("sexy", "hot", "cute", "pretty", "love"):
            self.stella_say("Stop, you're making me blush... don't stop 😳💋")
        else:
            self.stella_say(self.line("chat"))

    # ---------- look & feel ----------
    def cat0(self, it):
        return it["categories"][0] if it["categories"] else "Uncategorized"

    def toggle_ph(self):
        if self.search.get():
            self.ph.place_forget()
        else:
            self.ph.place(x=10, rely=0.5, anchor="w")

    def hover(self, f, p, on):
        try:
            if p in self.selected:
                return
            if not on:
                w = self.root.winfo_containing(*self.root.winfo_pointerxy())
                if w is not None and (str(w) == str(f) or str(w).startswith(str(f) + ".")):
                    return
            f.config(highlightbackground=NEON if on else EDGE)
        except tk.TclError:
            pass

    def build_banner(self):
        self.tag_i, self.tag_id = random.randrange(len(TAGLINES)), None
        self.banner = tk.Canvas(self.root, height=84, bg=BG, highlightthickness=0)
        self.banner.pack(side="top", fill="x")
        self.banner.bind("<Configure>", self.draw_banner)
        self.root.after(5000, self.rotate_tag)

    def draw_banner(self, e=None):
        c = self.banner
        c.delete("all")
        w, h = max(c.winfo_width(), 900), 84
        for x in range(0, w, 4):
            t = x / w
            col = mix("#12020a", "#7a0a3a", t / 0.55) if t < 0.55 else mix("#7a0a3a", "#ff1f7a", (t - 0.55) / 0.45)
            c.create_rectangle(x, 0, x + 4, h, fill=col, outline=col)
        c.create_text(26, 32, text="\U0001F608", anchor="w", font=(EMOJI, 30), fill="#ffffff")
        for dx, dy, col in [(-2, 3, "#5a0830"), (2, 3, "#5a0830"), (0, 4, "#3a0420")]:
            c.create_text(78 + dx, 32 + dy, text="Naughty Room", anchor="w", font=("Segoe Script", 28, "bold"), fill=col)
        c.create_text(78, 32, text="Naughty Room", anchor="w", font=("Segoe Script", 28, "bold"), fill="#ffffff")
        self.tag_id = c.create_text(80, 68, text=TAGLINES[self.tag_i], anchor="w", font=("Segoe UI", 11, "italic"), fill="#ffd1e3")
        for i, ch in enumerate(["\U0001F48B", "\U0001F525", "\U0001F351", "\U0001F352", "\U0001F608"]):
            c.create_text(w - 40 - i * 52, 42, text=ch, font=(EMOJI, 26), fill="#ffd1e3")
        c.create_line(0, h - 1, w, h - 1, fill=NEON, width=2)

    def rotate_tag(self):
        self.tag_i = random.randrange(len(TAGLINES))
        try:
            self.banner.itemconfig(self.tag_id, text=TAGLINES[self.tag_i])
        except Exception:
            pass
        self.root.after(5000, self.rotate_tag)

    # ---------- sidebar ----------
    def icon(self, cat):
        return self.data.get("icons", {}).get(cat) or DEFAULT_ICONS.get(cat, FALLBACK_ICON)

    def refresh(self):
        items = self.data["items"].values()
        self.nav_keys = ["all", "fav", "recent"]
        rows = [f"\U0001F48B  All the goodies  ({len(self.data['items'])})",
                f"\U0001F525  Hot list  ({sum(1 for i in items if i['fav'])})",
                "\U0001F440  Recently watched"]
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
            if self.mode != "videos":
                self.set_mode("videos")
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
        vids, imgs = [], []
        for p in self.root.tk.splitlist(e.data):
            files = [os.path.join(r, f) for r, _, fs in os.walk(p) for f in fs] if os.path.isdir(p) else [p]
            for q in files:
                x = Path(q).suffix.lower()
                if x in VIDEO_EXT:
                    vids.append(q)
                elif x in IMG_EXT:
                    imgs.append(q)
        if imgs:
            n = self.add_image_paths(imgs)
            self.stella_event("images", n=n)
        if not vids:
            return
        cat = self.view[4:] if self.view.startswith("cat:") else self.pick_category()
        if cat:
            n, d = self.add_paths(vids, cat)
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
        log = self.data.setdefault("plays_log", [])
        log.append(time.time())
        del log[:-500]
        self.save()
        self.stella_play(it)

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
        self.stella_event("added", n=added, d=dupes)
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
        if getattr(self, "mode", "videos") == "images":
            self.g_layout()
        else:
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
                       bg=tint(color_of(self.cat0(it)), 0.28), fg=color_of(self.cat0(it)), font=(EMOJI, 32), width=TILE_W, height=TILE_H)
        lbl.pack()
        self.tile_labels[it["path"]] = lbl
        star = "\U0001F525 " if it["fav"] else ""
        t = tk.Label(f, text=star + it["title"], bg=CARD, fg=FG, anchor="w", width=32,
                     font=("Segoe UI", 10, "bold"))
        t.pack(fill="x", padx=8, pady=(6, 0))
        sub = " ".join(self.icon(c) for c in it["categories"]) + "  " + ", ".join(it["categories"]) + ("  \u00B7  " + ", ".join(it["tags"]) if it["tags"] else "")
        s = tk.Label(f, text=sub[:38], bg=CARD, fg=color_of(self.cat0(it)), anchor="w", font=(EMOJI, 9))
        s.pack(fill="x", padx=8, pady=(0, 8))
        for w in (f, lbl, t, s):
            w.bind("<Double-Button-1>", lambda e, p=it["path"]: self.play(p))
            w.bind("<Button-1>", lambda e, p=it["path"]: self.on_click(e, p))
            w.bind("<Button-3>", lambda e, p=it["path"]: self.item_menu(e, p))
            w.bind("<Enter>", lambda e, f=f, p=it["path"]: self.hover(f, p, True))
            w.bind("<Leave>", lambda e, f=f, p=it["path"]: self.root.after(40, self.hover, f, p, False))
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
        if kw.get("fav"):
            self.stella_event("fav", t=self.data["items"][path]["title"])

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
