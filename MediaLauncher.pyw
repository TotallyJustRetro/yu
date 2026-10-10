"""
Media Launcher - a local video library organizer for Windows.

Run:    double-click MediaLauncher.pyw   (or: pythonw MediaLauncher.pyw)
Extras: pip install pillow opencv-python   (video thumbnails)
        pip install tkinterdnd2             (drag & drop files/folders onto the window)
        pip install python-vlc              (in-window player; also install VLC 64-bit from videolan.org)
        (the .exe built by the GitHub workflow bundles VLC in a "vlc" folder, so nothing extra to install)
Build:  pip install pyinstaller
        pyinstaller --noconsole --onefile --name MediaLauncher MediaLauncher.pyw

Your library is stored in %APPDATA%\\MediaLauncher\\library.json.
Nothing is uploaded anywhere; files are never moved or deleted.
"""
import hashlib, json, os, queue, random, re, shutil, subprocess, sys, threading, time
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
APP_VERSION = "11"


def _find_bundled_vlc():
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    for d in (base / "vlc", base / "_internal" / "vlc"):
        if (d / "libvlc.dll").exists():
            os.environ["PYTHON_VLC_LIB_PATH"] = str(d / "libvlc.dll")
            os.environ["PYTHON_VLC_MODULE_PATH"] = str(d / "plugins")
            os.environ["VLC_PLUGIN_PATH"] = str(d / "plugins")
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(str(d))
            except Exception:
                pass
            return str(d)
    return None


VLC_ERROR = None
_find_bundled_vlc()
try:
    import vlc
except (Exception, SystemExit) as _e:  # python-vlc can even call sys.exit() when libvlc can't be loaded
    vlc, VLC_ERROR = None, f"{type(_e).__name__}: {_e}"
try:
    import winreg
except ImportError:
    winreg = None
MPC_EXES = ("mpc-hc64.exe", "mpc-hc.exe", "mpc-be64.exe", "mpc-be.exe")
MPC_DIRS = ("MPC-HC", "MPC-HC x64", "MPC-HC64", "K-Lite Codec Pack\\MPC-HC64", "K-Lite Codec Pack\\MPC-HC",
            "MPC-BE", "MPC-BE x64", "Media Player Classic - Home Cinema")


def find_mpc():
    """Locate MPC-HC (or MPC-BE): registry, usual install folders (incl. K-Lite), then PATH."""
    cands = []
    if winreg:
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for exe in MPC_EXES:
                for view in (0, getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0)):
                    try:
                        with winreg.OpenKey(hive, "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + exe,
                                            0, winreg.KEY_READ | view) as k:
                            cands.append(str(winreg.QueryValue(k, None)).strip('"'))
                    except OSError:
                        pass
        for sub in ("SOFTWARE\\MPC-HC\\MPC-HC", "SOFTWARE\\WOW6432Node\\MPC-HC\\MPC-HC"):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as k:
                    cands.append(str(winreg.QueryValueEx(k, "ExePath")[0]).strip('"'))
            except OSError:
                pass
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands += [os.path.join(base, d, e) for d in MPC_DIRS for e in MPC_EXES]
    cands += [shutil.which(e) for e in MPC_EXES]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = TkinterDnD = None

HOME_DIR = Path(os.environ.get("APPDATA", Path.home())) / "MediaLauncher"
HOME_DIR.mkdir(parents=True, exist_ok=True)
STORAGE_WARN = None


def _resolve_dir():
    """Where library, thumbnails, screenshots and Stella's pictures live. A tiny pointer file in %APPDATA% remembers a custom
    folder (e.g. on another drive); if that drive isn't plugged in we fall back to the default and warn."""
    global STORAGE_WARN
    try:
        d = json.loads((HOME_DIR / "location.json").read_text(encoding="utf-8")).get("dir")
    except Exception:
        return HOME_DIR
    if d:
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
            return Path(d)
        except Exception:
            STORAGE_WARN = d
    return HOME_DIR


APP_DIR = _resolve_dir()
THUMB_DIR, DB_FILE, SHOT_DIR = APP_DIR / "thumbs", APP_DIR / "library.json", APP_DIR / "screens"
for _d in (THUMB_DIR, SHOT_DIR / "live"):
    _d.mkdir(parents=True, exist_ok=True)


def set_storage(new_dir):
    """Copy everything to new_dir and point the app (and every future launch) at it. Returns the old folder."""
    global APP_DIR, THUMB_DIR, DB_FILE, SHOT_DIR
    old, new = APP_DIR, Path(new_dir)
    new.mkdir(parents=True, exist_ok=True)
    if old.resolve() != new.resolve():
        for item in old.iterdir():
            if item.name in ("location.json", "error.log", "screens"):
                continue
            if item.is_dir():
                shutil.copytree(item, new / item.name, dirs_exist_ok=True)
            else:
                shutil.copy2(item, new / item.name)
    (HOME_DIR / "location.json").write_text(json.dumps({"dir": str(new)}), encoding="utf-8")
    APP_DIR, THUMB_DIR, DB_FILE, SHOT_DIR = new, new / "thumbs", new / "library.json", new / "screens"
    for d in (THUMB_DIR, SHOT_DIR / "live"):
        d.mkdir(parents=True, exist_ok=True)
    return old


def remove_old_storage(old):
    for item in old.iterdir():
        if item.name == "location.json":
            continue
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)
    try:
        old.rmdir()
    except OSError:
        pass


def dir_size(path):
    total = 0
    for r, _, fs in os.walk(path):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return total


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024

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


def thumb_file(path):
    return THUMB_DIR / (hashlib.md5(path.encode("utf-8", "ignore")).hexdigest() + ".jpg")


STOP = set("""the and for with from her his she him you your this that are not but all one two out get got who how why can has
was had its our off now may porn sex nsfw xxx video videos clip clips vid vids full new best part scene free download com www
mp mov avi mkv hd fhd uhd users user downloads desktop documents pictures media onedrive files folder copy final""".split())
SEED = {
    "Twerking": ["twerk", "twerking", "booty", "dance", "dancing", "shake", "bounce", "jiggle"],
    "Femboy": ["femboy", "femboi", "femboys", "femmeboy", "crossdress", "crossdresser"],
    "Blowjob": ["blowjob", "oral", "deepthroat", "sucking", "suck"],
    "NSFW ASMR": ["asmr", "whisper", "whispers", "whispering", "binaural", "tingles", "audio", "roleplay"],
    "Footjob": ["footjob", "feet", "foot", "soles", "toes", "pedicure"],
    "Yiff": ["yiff", "furry", "fursuit", "anthro", "fandom", "wolf", "fox"],
}
TAG_RE = re.compile(r"\[(play|show|sort)(?::\s*([^\]]*))?\]", re.I)
THINK_RE = re.compile(r"<think>.*?(?:</think>|$)", re.S | re.I)
TIDY_SYS = ("You label items in a user's private media library. Reply with ONLY JSON like {\"title\": \"...\", \"tags\": [\"...\"]}. "
            "title: a short, clean, readable name (max 6 words, Title Case) describing the item; if the current title is already "
            "readable, repeat it unchanged. tags: 3-6 short lowercase descriptive tags using only letters, digits and hyphens "
            "(like neon-lights, red-dress, close-up, slow-tease). Base them on the title, folder, categories and any attached image. "
            "Keep it tasteful and non-graphic.")
IDLE_PROMPTS = [
    "[event] It's been quiet. Start a flirty conversation: ask what mood they're in tonight, or tease them about their habits.",
    "[event] Pick a specific video from the library (use its exact title) and tease them into watching it.",
    "[event] Say something sensual and teasing about the time of day or the atmosphere, then ask them a playful question.",
    "[event] Suggest a little game or scenario for the two of you tonight and ask if they're up for it.",
    "[event] Comment on their recent viewing habits from the library facts, in a teasing, possessive-but-playful way.",
]


def needs_name(title):
    """True for camera/hash/timestamp-style names that deserve a readable display name."""
    t = (title or "").strip()
    letters = re.findall(r"[A-Za-z]", t)
    if len(t) < 4 or len(letters) < 3:
        return True
    if re.fullmatch(r"(?i)(vid|video|img|image|mov|clip|pxl|dsc|screen[ _-]?record(ing)?|recording|untitled|new|output|movie)[ _-]?\d.*", t):
        return True
    if re.fullmatch(r"[0-9a-fA-F_\-\. ]{8,}", t):
        return True
    return len(letters) / max(1, len(t)) < 0.45


def tokens(text):
    return [w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 3 and w not in STOP]


def smart_suggest(data):
    """Suggest categories for uncategorised videos/pictures from names, tags, folders and what you've already sorted."""
    cats = [c for c in data["categories"] if c != "Uncategorized"]
    kw = {c: set(SEED.get(c, [])) | set(tokens(c)) for c in cats}
    pool = [("video", i) for i in data["items"].values()] + [("image", i) for i in data.get("images", {}).values()]

    def parts(it):
        name = it.get("title") or Path(it["path"]).stem
        return set(tokens(name) + tokens(" ".join(it.get("tags", [])))), set(tokens(" ".join(Path(it["path"]).parts[-4:-1])))

    learn, folders = {}, {}
    for kind, it in pool:
        real = [c for c in it["categories"] if c != "Uncategorized"]
        if not real:
            continue
        a, b = parts(it)
        for w in a | b:
            learn.setdefault(w, Counter()).update(real)
        folders.setdefault(os.path.dirname(it["path"]), Counter()).update(real)
    out = []
    for kind, it in pool:
        if any(c != "Uncategorized" for c in it["categories"]):
            continue
        a, b = parts(it)
        sc, why = Counter(), {}

        def add(c, v, reason):
            sc[c] += v
            why.setdefault(c, []).append(reason)
        for w in a | b:
            for c in cats:
                if w in kw[c]:
                    add(c, 4.0 if w in a else 2.0, f'"{w}" matches {c}')
            if w in learn:
                tot = sum(learn[w].values())
                for c, n in learn[w].items():
                    if tot >= 2:
                        add(c, (n / tot) * min(2.0, 0.7 * tot), f'"{w}" usually means {c}')
        fc = folders.get(os.path.dirname(it["path"]))
        if fc:
            tot = sum(fc.values())
            c, n = fc.most_common(1)[0]
            if tot >= 2 and n / tot >= 0.6:
                add(c, 3.0, f"same folder as {n} {c} items")
        if not sc:
            continue
        ranked = sc.most_common(3)
        top, s0 = ranked[0]
        share = s0 / sum(sc.values())
        conf = "high" if s0 >= 3 and share >= 0.7 else "medium" if s0 >= 1.5 and share >= 0.5 else "low"
        out.append(dict(kind=kind, path=it["path"], title=it.get("title") or Path(it["path"]).stem,
                        cats=[top] + [c for c, v in ranked[1:] if v >= 0.8 * s0 and v >= 3],
                        conf=conf, why="; ".join(why[top][:2])))
    return out


class Brain:
    """Talks to a local AI server. Ollama is used through its native API (so the context window can be raised and
    pictures work); anything else (LM Studio, llama.cpp...) through the OpenAI-compatible API.
    Nothing leaves your PC unless you point it somewhere else."""

    def __init__(self, cfg):
        self.cfg = cfg

    def ready(self):
        return bool(self.cfg.get("model", "").strip())

    def native(self):
        a = self.cfg.get("api", "auto")
        return a == "ollama" or (a == "auto" and "11434" in self.cfg.get("url", ""))

    def base(self):
        u = self.cfg["url"].rstrip("/")
        return u[:-3] if u.endswith("/v1") else u

    def _req(self, url, body=None, timeout=120):
        import urllib.request
        h = {"Content-Type": "application/json"}
        if self.cfg.get("key"):
            h["Authorization"] = "Bearer " + self.cfg["key"]
        data = json.dumps(body).encode() if body is not None else None
        return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h), timeout=timeout)

    def models(self):
        if self.native():
            with self._req(self.base() + "/api/tags", timeout=5) as r:
                return [m["name"] for m in json.load(r).get("models", [])]
        with self._req(self.cfg["url"].rstrip("/") + "/models", timeout=5) as r:
            return [m["id"] for m in json.load(r).get("data", [])]

    def capabilities(self, model):
        """Ollama only, e.g. ['completion', 'vision']. None when it can't be determined."""
        if not self.native():
            return None
        try:
            with self._req(self.base() + "/api/show", {"model": model}, timeout=8) as r:
                return json.load(r).get("capabilities")
        except Exception:
            return None

    @staticmethod
    def _split(content):
        if isinstance(content, str):
            return content, []
        text = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
        return text, [p["image_url"]["url"].split(",", 1)[-1] for p in content if p.get("type") == "image_url"]

    def _native_body(self, messages, stream, temp, n):
        msgs = []
        for m in messages:
            text, imgs = self._split(m["content"])
            d = {"role": m["role"], "content": text}
            if imgs:
                d["images"] = imgs
            msgs.append(d)
        try:
            ctx = int(self.cfg.get("ctx") or 8192)
        except ValueError:
            ctx = 8192
        return {"model": self.cfg["model"], "messages": msgs, "stream": stream, "think": False, "keep_alive": "30m",
                "options": {"num_ctx": ctx, "temperature": temp, "num_predict": n}}

    def complete(self, messages, max_tokens=150, temp=0.2):
        if self.native():
            with self._req(self.base() + "/api/chat", self._native_body(messages, False, temp, max_tokens)) as r:
                return json.load(r).get("message", {}).get("content", "") or ""
        body = {"model": self.cfg["model"], "messages": messages, "stream": False, "temperature": temp, "max_tokens": max_tokens}
        with self._req(self.cfg["url"].rstrip("/") + "/chat/completions", body) as r:
            return json.load(r)["choices"][0]["message"]["content"] or ""

    def chat(self, messages, on_token):
        if self.native():
            with self._req(self.base() + "/api/chat", self._native_body(messages, True, 0.85, 220)) as r:
                for raw in r:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("error"):
                        raise RuntimeError(d["error"])
                    tok = d.get("message", {}).get("content", "")
                    if tok:
                        on_token(tok)
                    if d.get("done"):
                        break
            return
        body = {"model": self.cfg["model"], "messages": messages, "stream": True, "temperature": 0.85, "max_tokens": 220}
        with self._req(self.cfg["url"].rstrip("/") + "/chat/completions", body) as r:
            for raw in r:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    tok = json.loads(payload)["choices"][0]["delta"].get("content") or ""
                except Exception:
                    continue
                if tok:
                    on_token(tok)

    @staticmethod
    def explain(e):
        if hasattr(e, "code") and hasattr(e, "read"):  # HTTPError: the server usually says exactly what's wrong
            try:
                body = e.read().decode("utf-8", "ignore")
                try:
                    body = json.loads(body).get("error", body)
                    body = body.get("message", body) if isinstance(body, dict) else body
                except Exception:
                    pass
                return f"HTTP {e.code}: {str(body)[:140]}"
            except Exception:
                return f"HTTP {e.code}"
        m = str(getattr(e, "reason", e))
        return "can't reach the server. Is Ollama (or LM Studio) running?" if ("refused" in m.lower() or "10061" in m) else m[:140]


class ChatWindow(tk.Toplevel):
    """Scrolling chat history with Stella."""

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.geometry("460x580")
        self.configure(bg=PANEL)
        self.txt = tk.Text(self, bg=PANEL, fg=FG, wrap="word", bd=0, font=(EMOJI, 11), padx=12, pady=10, state="disabled", cursor="arrow")
        self.txt.pack(fill="both", expand=True)
        self.txt.tag_configure("you", foreground=NEON, justify="right", lmargin1=60, lmargin2=60)
        self.txt.tag_configure("her", foreground=FG)
        self.txt.tag_configure("name", foreground=ACCENT, font=(EMOJI, 9, "bold"))
        self.ent = tk.Entry(self, bg=CARD, fg=FG, insertbackground=ACCENT, bd=0, font=("Segoe UI", 11),
                            highlightthickness=1, highlightbackground=EDGE, highlightcolor=ACCENT)
        self.ent.pack(fill="x", padx=10, pady=10, ipady=6)
        self.ent.bind("<Return>", self.send)
        self.ent.focus_set()
        self.render(app.chat_log, app.data["stella"]["names"][app.persona()])

    def send(self, e=None):
        t = self.ent.get().strip()
        self.ent.delete(0, "end")
        if t:
            self.app.say_to_stella(t)

    def render(self, log, name):
        self.title(f"{name} 💋")
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        for who, text in log:
            if who == "you":
                self.txt.insert("end", "You\n", ("you", "name"))
                self.txt.insert("end", text + "\n\n", "you")
            else:
                self.txt.insert("end", name + "\n", "name")
                self.txt.insert("end", text + "\n\n", "her")
        self.txt.config(state="disabled")
        self.txt.see("end")


class WatchPage(tk.Frame):
    """YouTube-style watch page inside the main window: embedded VLC player, info + actions, and an Up-next list."""
    SPEEDS = ["0.5x", "0.75x", "1x", "1.25x", "1.5x", "2x"]

    def __init__(self, app, parent):
        super().__init__(parent, bg=BG)
        self.app, self.inst, self.mp = app, None, None
        self.paths, self.i, self.drag, self.fs, self.theater = [], 0, False, None, False
        self.rate, self.small, self.guard, self.tick_job, self.thumb_job, self.queued = 1.0, {}, False, None, None, set()
        self.auto = tk.BooleanVar(value=True)
        self.keys = ("<space>", "<Right>", "<Left>", "<f>", "<t>", "<n>", "<p>", "<l>")
        self.last_along, self.along_gap = time.time(), None
        self.loop, self.ab = app.data.get("loop", "off"), [None, None]
        self.trail, self.nextq, self.suggested, self.taken = [], [], set(), set()
        # ---- right: up next ----
        self.right = tk.Frame(self, bg=BG, width=330)
        self.right.pack(side="right", fill="y", padx=(8, 10))
        self.right.pack_propagate(False)
        tabs = tk.Frame(self.right, bg=BG)
        tabs.pack(fill="x", pady=(0, 6))
        self.tabs = {}
        for key, txt in (("up", "UP NEXT"), ("cm", "💬 COMMENTS")):
            tbtn = tk.Button(tabs, text=txt, command=lambda k=key: self.show_tab(k), bd=0, font=("Segoe UI Black", 10), padx=10, pady=4)
            tbtn.pack(side="left", padx=(0, 6))
            self.tabs[key] = tbtn
        self.up_frame = tk.Frame(self.right, bg=BG)
        self.cm_frame = tk.Frame(self.right, bg=BG)
        st = ttk.Style()
        st.configure("Up.Treeview", background=CARD, fieldbackground=CARD, foreground=FG, rowheight=70, borderwidth=0, font=(EMOJI, 10))
        st.map("Up.Treeview", background=[("selected", CRIMSON)], foreground=[("selected", "white")])
        self.smart = tk.BooleanVar(value=app.data.get("watch_smart", True))
        tk.Checkbutton(self.up_frame, text="Smart order (most similar first)", variable=self.smart, command=self.on_smart, bg=BG, fg=FG,
                       selectcolor=CARD, activebackground=BG, activeforeground=ACCENT, font=("Segoe UI", 9)).pack(anchor="w")
        self.tree = ttk.Treeview(self.up_frame, show="tree", selectmode="browse", style="Up.Treeview")
        sb = ttk.Scrollbar(self.up_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_pick)
        self.cent = tk.Entry(self.cm_frame, bg=CARD, fg=FG, insertbackground=ACCENT, bd=0, font=("Segoe UI", 10),
                             highlightthickness=1, highlightbackground=EDGE, highlightcolor=ACCENT)
        self.cent.pack(side="bottom", fill="x", ipady=5, pady=(6, 0))
        self.cent.bind("<Return>", self.post_comment)
        tk.Button(self.cm_frame, text="💋 Ask Stella for her take", command=self.stella_take, bg=ACCENT, fg="white", bd=0,
                  font=(EMOJI, 10, "bold"), pady=5).pack(side="bottom", fill="x", pady=(6, 0))
        self.cbox = tk.Text(self.cm_frame, bg=CARD, fg=FG, wrap="word", bd=0, font=(EMOJI, 10), padx=10, pady=8, state="disabled", cursor="arrow")
        self.cbox.tag_configure("her", foreground=NEON, font=(EMOJI, 9, "bold"))
        self.cbox.tag_configure("you", foreground=MUTED, font=(EMOJI, 9, "bold"))
        self.cbox.tag_configure("body", foreground=FG, spacing3=10)
        self.cbox.pack(fill="both", expand=True)
        self.show_tab("up")
        # ---- left: video, controls, info ----
        self.left = tk.Frame(self, bg=BG)
        self.left.pack(side="left", fill="both", expand=True, padx=(10, 0))
        self.info = tk.Frame(self.left, bg=BG)
        self.info.pack(side="bottom", fill="x", pady=(8, 0))
        self.ttl = tk.Label(self.info, bg=BG, fg=FG, anchor="w", justify="left", font=("Segoe UI", 15, "bold"), wraplength=700)
        self.ttl.pack(fill="x")
        self.meta = tk.Label(self.info, bg=BG, fg=MUTED, anchor="w", font=(EMOJI, 10))
        self.meta.pack(fill="x", pady=(2, 0))
        self.catlbl = tk.Label(self.info, bg=BG, fg=ACCENT, anchor="w", font=(EMOJI, 10, "bold"))
        self.catlbl.pack(fill="x", pady=(0, 6))
        acts = tk.Frame(self.info, bg=BG)
        acts.pack(fill="x")
        self.fav_btn = self.btn(acts, "🔥 Hot list", self.toggle_fav)
        self.btn(acts, "🏷 Tags", self.edit_tags)
        self.btn(acts, "📂 Categories", self.cat_menu)
        self.btn(acts, "👀 Ask Stella", self.ask_stella)
        self.btn(acts, "↗ MPC-HC / default player", self.external)
        self.btn(acts, "⬅ Back to library", self.back, side="right")
        self.ctrl = tk.Frame(self.left, bg=PANEL)
        self.ctrl.pack(side="bottom", fill="x")
        self.seek = ttk.Scale(self.ctrl, from_=0, to=1000)
        self.seek.pack(fill="x", padx=10, pady=(6, 2))
        self.seek.bind("<ButtonPress-1>", lambda e: setattr(self, "drag", True))
        self.seek.bind("<ButtonRelease-1>", self.on_seek)
        row = tk.Frame(self.ctrl, bg=PANEL)
        row.pack(fill="x", padx=10, pady=(0, 8))
        for txt, cmd in [("⏮", lambda: self.step(-1)), ("⏯", self.toggle), ("⏭", lambda: self.step(1))]:
            tk.Button(row, text=txt, command=cmd, bg=CARD, fg=FG, bd=0, width=4, font=(EMOJI, 12), activebackground=ACCENT).pack(side="left", padx=3)
        self.time_lbl = tk.Label(row, bg=PANEL, fg=MUTED)
        self.time_lbl.pack(side="left", padx=10)
        self.loop_btn = tk.Button(row, command=self.cycle_loop, bg=CARD, fg=FG, bd=0, font=(EMOJI, 10), padx=8, activebackground=ACCENT)
        self.loop_btn.pack(side="left", padx=(0, 4))
        for txt, cmd in [("A", self.set_a), ("B", self.set_b), ("✖", self.clear_ab)]:
            tk.Button(row, text=txt, command=cmd, bg=CARD, fg=FG, bd=0, width=2, font=(EMOJI, 10), activebackground=ACCENT).pack(side="left", padx=1)
        self.update_loop_btn()
        for txt, cmd in [("⛶", self.fullscreen), ("🎭", self.toggle_theater)]:
            tk.Button(row, text=txt, command=cmd, bg=CARD, fg=FG, bd=0, width=3, font=(EMOJI, 12), activebackground=ACCENT).pack(side="right", padx=3)
        self.vol = ttk.Scale(row, from_=0, to=100, length=90, command=lambda v: self.mp and self.mp.audio_set_volume(int(float(v))))
        self.vol.set(80)
        self.vol.pack(side="right")
        tk.Label(row, text="🔊", bg=PANEL, fg=FG, font=(EMOJI, 11)).pack(side="right")
        self.speed = ttk.Combobox(row, values=self.SPEEDS, state="readonly", width=5)
        self.speed.set("1x")
        self.speed.bind("<<ComboboxSelected>>", self.set_speed)
        self.speed.pack(side="right", padx=8)
        tk.Checkbutton(row, text="Autoplay", variable=self.auto, bg=PANEL, fg=FG, selectcolor=CARD, activebackground=PANEL,
                       activeforeground=ACCENT).pack(side="right", padx=4)
        self.vp = tk.Frame(self.left, bg="black")
        self.vp.pack(fill="both", expand=True)
        self.left.bind("<Configure>", lambda e: self.ttl.config(wraplength=max(300, e.width - 30)))

    def btn(self, parent, text, cmd, side="left"):
        b = tk.Button(parent, text=text, command=cmd, bg=CARD, fg=FG, bd=0, activebackground=ACCENT, activeforeground="white",
                      font=(EMOJI, 10), padx=10, pady=4)
        b.pack(side=side, padx=(0, 6))
        return b

    def ensure(self):
        if self.mp:
            return
        self.inst = vlc.Instance()
        self.mp = self.inst.media_player_new()
        self.update_idletasks()
        self.mp.set_hwnd(self.vp.winfo_id())
        self.mp.video_set_mouse_input(False)
        self.mp.video_set_key_input(False)
        self.mp.audio_set_volume(int(float(self.vol.get())))

    def open(self, paths, index):
        self.app.set_mode("watch")
        self.paths, self.i = paths, index
        self.ensure()
        self.activate()
        self.build_upnext()
        self.load()
        if self.tick_job is None:
            self.tick()

    def load(self):
        p = self.paths[self.i]
        it = self.app.data["items"].get(p)
        if not it:
            return
        self.mp.set_media(self.inst.media_new(p))
        self.mp.play()
        self.after(700, lambda: self.mp.set_rate(self.rate))
        self.last_along, self.along_gap = time.time(), self.app.chat_gap(True)
        self.ab = [None, None]
        self.update_loop_btn()
        self.refresh_info(it)
        self.app.mark_played(p)

    def refresh_info(self, it):
        self.ttl.config(text=it["title"])
        added = time.strftime("%b %d, %Y", time.localtime(it["added"]))
        self.meta.config(text=f"{it['plays']} plays  ·  added {added}  ·  {Path(it['path']).parent.name}"
                              + ("  ·  " + "  ".join("#" + t for t in it["tags"]) if it["tags"] else ""))
        self.catlbl.config(text="  ".join(f"{self.app.icon(c)} {c}" for c in it["categories"]))
        self.fav_btn.config(text="🔥 On Hot list" if it["fav"] else "🔥 Hot list", bg=ACCENT if it["fav"] else CARD)
        self.render_comments(it)

    def cur(self):
        return self.app.data["items"].get(self.paths[self.i]) if self.paths else None

    # ---- up next ----
    def small_thumb(self, path):
        if path in self.small:
            return self.small[path]
        tp = thumb_file(path)
        if Image and tp.exists():
            try:
                self.small[path] = ImageTk.PhotoImage(Image.open(tp).resize((112, 63)))
                return self.small[path]
            except Exception:
                return None
        if Image and cv2 and path not in self.queued and os.path.exists(path):
            self.queued.add(path)
            self.app.thumb_q.put((path, tp))
        return None

    def build_upnext(self):
        self.guard = True
        self.tree.delete(*self.tree.get_children())
        items, n, cur = self.app.data["items"], len(self.paths), self.cur()
        if self.smart.get() and cur:
            order = sorted((k for k in range(n) if k != self.i and self.paths[k] in items),
                           key=lambda k: -self.app.similarity(cur, items[self.paths[k]]))
        else:
            order = [k for k in ((self.i + j) % n for j in range(1, n)) if self.paths[k] in items]
        self.nextq = order[:60]
        for idx in self.nextq:
            it = items[self.paths[idx]]
            icons = "".join(self.app.icon(c) for c in it["categories"][:3])
            self.tree.insert("", "end", iid=str(idx), text="  " + it["title"][:32] + "  " + icons,
                             image=self.small_thumb(self.paths[idx]) or "")
        self.guard = False

    def refresh_soon(self):
        if self.thumb_job is None:
            self.thumb_job = self.after(1500, self._refresh_thumbs)

    def _refresh_thumbs(self):
        self.thumb_job = None
        if self.paths and self.app.mode == "watch":
            self.build_upnext()

    def on_pick(self, e):
        sel = self.tree.selection()
        if not self.guard and sel:
            self.after_idle(self.go, int(sel[0]))

    def go(self, idx, push=True):
        if push and self.paths and self.i != idx:
            self.trail.append(self.i)
            del self.trail[:-50]
        self.i = idx
        self.build_upnext()
        self.load()

    def step(self, d):
        if not self.paths:
            return
        if d < 0 and self.trail:
            self.go(self.trail.pop(), push=False)
        elif d > 0 and self.nextq:
            self.go(self.nextq[0])
        else:
            self.go((self.i + d) % len(self.paths))

    # ---- transport ----
    def toggle(self):
        if self.mp:
            self.mp.pause()

    def jump(self, ms):
        if self.mp:
            self.mp.set_time(max(0, self.mp.get_time() + ms))

    def on_seek(self, e):
        if self.mp:
            self.mp.set_position(float(self.seek.get()) / 1000)
        self.drag = False

    def set_speed(self, e=None):
        self.rate = float(self.speed.get().rstrip("x"))
        if self.mp:
            self.mp.set_rate(self.rate)

    def tick(self):
        self.tick_job = None
        if self.app.mode != "watch" and not self.fs:
            return
        try:
            self.tick_body()
        except Exception:
            pass
        try:
            self.maybe_along()
        except Exception:
            pass
        self.tick_job = self.after(400, self.tick)

    def toggle_theater(self):
        self.theater = not self.theater
        if self.theater:
            self.right.pack_forget()
            self.info.pack_forget()
        else:
            self.right.pack(side="right", fill="y", padx=(8, 10), before=self.left)
            self.info.pack(side="bottom", fill="x", pady=(8, 0), before=self.ctrl)

    def fullscreen(self):
        if not self.mp:
            return
        if self.fs:
            self.mp.set_hwnd(self.vp.winfo_id())
            self.fs.destroy()
            self.fs = None
            self.app.root.focus_force()
            return
        fs = tk.Toplevel(self.app.root)
        fs.configure(bg="black")
        fs.attributes("-fullscreen", True)
        f = tk.Frame(fs, bg="black")
        f.pack(fill="both", expand=True)
        fs.update_idletasks()
        self.mp.set_hwnd(f.winfo_id())
        binds = {"<Escape>": self.fullscreen, "<f>": self.fullscreen, "<space>": self.toggle, "<n>": lambda: self.step(1),
                 "<p>": lambda: self.step(-1), "<Right>": lambda: self.jump(5000), "<Left>": lambda: self.jump(-5000)}
        for k, fn in binds.items():
            fs.bind(k, lambda e, fn=fn: fn())
        f.bind("<Double-Button-1>", lambda e: self.fullscreen())
        fs.focus_force()
        self.fs = fs

    # ---- Stella can see the exact frame on screen ----
    def snapshot(self, cb):
        f = str(SHOT_DIR / "snap.jpg")
        try:
            os.remove(f)
        except OSError:
            pass
        try:
            self.mp.video_take_snapshot(0, f, 0, 0)
        except Exception:
            cb(None)
            return
        tries = [0]

        def check():
            tries[0] += 1
            if os.path.exists(f) and os.path.getsize(f) > 0:
                cb(f)
            elif tries[0] < 10:
                self.after(150, check)
            else:
                cb(None)
        self.after(150, check)

    def maybe_along(self):
        a = self.app
        b = a.data["brain"]
        if not (a.brain.ready() and not a.brain_busy and self.mp.is_playing()):
            return
        if not self.along_gap or time.time() - self.last_along < self.along_gap:
            return
        self.last_along, self.along_gap = time.time(), a.chat_gap(True)
        if b.get("vision") and (b.get("along") or a.data["sight"]["on"]):
            shot = a.latest_shot(60)
            if shot:
                a.along_look(shot)
            else:
                self.snapshot(a.along_look)
        elif b.get("auto", True):
            a.stella_ask(None, hint="[event] You're watching along with them right now. Say one short flirty, sensual thing about the "
                                    "mood of what they picked, or tease them. " + (a.now_hint() or ""))

    # ---- loop / repeat, A-B, tabs, comments, proactive suggestions ----
    def tick_body(self):
        if self.mp.get_state() == vlc.State.Ended:
            if self.loop == "one":
                self.restart()
            elif len(self.paths) > 1 and (self.loop == "all" or self.auto.get()):
                self.step(1)
            elif self.loop == "all":
                self.restart()
            return
        ln = self.mp.get_length()
        if ln > 0:
            pos = self.mp.get_time()
            if not self.drag:
                self.seek.set(self.mp.get_position() * 1000)
                self.time_lbl.config(text=f"{fmt(pos)} / {fmt(ln)}")
            a, b = self.ab
            if a is not None and b is not None and pos >= b:
                self.mp.set_time(a)
            self.maybe_react(pos, ln)

    def maybe_react(self, pos, ln):
        p = self.paths[self.i]
        frac = pos / ln
        if frac > 0.6 and p not in self.taken and self.app.data["brain"].get("autotake", True) and self.app.brain.ready():
            self.taken.add(p)
            self.app.write_take(p, done=lambda: self.render_comments(self.cur()), auto=True)
        elif frac > 0.7 and p not in self.suggested and len(self.paths) > 1:
            self.suggested.add(p)
            self.app.pitch_next(p, self.paths)

    def update_loop_btn(self):
        txt = {"off": "⟲ Loop: off", "one": "🔂 Loop: this video", "all": "🔁 Loop: all"}[self.loop]
        a, b = self.ab
        if a is not None or b is not None:
            txt += f"  A-B {fmt(a) if a is not None else '?'}→{fmt(b) if b is not None else '?'}"
        on = self.loop != "off" or b is not None
        self.loop_btn.config(text=txt, bg=ACCENT if on else CARD, fg="white" if on else FG)

    def cycle_loop(self):
        self.loop = {"off": "one", "one": "all", "all": "off"}[self.loop]
        self.app.data["loop"] = self.loop
        self.app.save()
        self.update_loop_btn()

    def set_a(self):
        if self.mp:
            self.ab[0] = self.mp.get_time()
            if self.ab[1] is not None and self.ab[1] <= self.ab[0]:
                self.ab[1] = None
            self.update_loop_btn()

    def set_b(self):
        if self.mp:
            t = self.mp.get_time()
            self.ab = [self.ab[0] if self.ab[0] is not None and self.ab[0] < t else 0, t]
            self.update_loop_btn()

    def clear_ab(self):
        self.ab = [None, None]
        self.update_loop_btn()

    def restart(self):
        self.mp.set_media(self.inst.media_new(self.paths[self.i]))
        self.mp.play()
        self.after(700, lambda: self.mp.set_rate(self.rate))

    def show_tab(self, k):
        for fr in (self.up_frame, self.cm_frame):
            fr.pack_forget()
        (self.up_frame if k == "up" else self.cm_frame).pack(fill="both", expand=True)
        for key, b in self.tabs.items():
            b.config(bg=ACCENT if key == k else CARD, fg="white" if key == k else FG)
        if k == "cm":
            self.render_comments(self.cur())

    def on_smart(self):
        self.app.data["watch_smart"] = self.smart.get()
        self.app.save()
        self.build_upnext()

    def render_comments(self, it):
        t = self.cbox
        t.config(state="normal")
        t.delete("1.0", "end")
        cs = (it or {}).get("comments", [])
        if not cs:
            t.insert("end", "No comments yet. Post one, or ask Stella for her take 💋", "you")
        name = self.app.data["stella"]["names"][self.app.persona()]
        for c in cs:
            who = f"{name} 💋" if c["who"] == "stella" else "You"
            t.insert("end", f"{who}  ·  {time.strftime('%b %d %H:%M', time.localtime(c['t']))}\n", "her" if c["who"] == "stella" else "you")
            t.insert("end", c["text"] + "\n", "body")
        t.config(state="disabled")
        t.see("end")

    def post_comment(self, e=None):
        txt = self.cent.get().strip()
        self.cent.delete(0, "end")
        if txt and self.paths:
            p = self.paths[self.i]
            self.app.add_comment(p, "you", txt)
            self.render_comments(self.cur())
            self.app.write_take(p, done=lambda: self.render_comments(self.cur()), kind="reply", user_text=txt)

    def stella_take(self):
        if self.paths:
            self.app.write_take(self.paths[self.i], done=lambda: self.render_comments(self.cur()))

    # ---- keyboard (only active on this page; ignored while typing) ----
    def activate(self):
        r = self.app.root

        def guard(fn):
            def h(e):
                try:
                    if isinstance(r.focus_get(), (tk.Entry, ttk.Combobox)):
                        return
                except Exception:
                    pass
                fn()
            return h
        fns = (self.toggle, lambda: self.jump(5000), lambda: self.jump(-5000), self.fullscreen, self.toggle_theater,
               lambda: self.step(1), lambda: self.step(-1), self.cycle_loop)
        for k, fn in zip(self.keys, fns):
            r.bind(k, guard(fn))

    def deactivate(self):
        for k in self.keys:
            self.app.root.unbind(k)

    def stop(self):
        self.app.now = None
        if self.app.data.get("sight", {}).get("wipe_change", True):
            self.app.clear_shots()
        self.deactivate()
        if self.fs:
            self.fullscreen()
        if self.mp:
            try:
                self.mp.stop()
            except Exception:
                pass

    # ---- info actions ----
    def toggle_fav(self):
        it = self.cur()
        if it:
            self.app.set(it["path"], fav=not it["fav"])
            self.refresh_info(self.cur())

    def edit_tags(self):
        it = self.cur()
        if it:
            v = simpledialog.askstring("Tags", "Comma-separated tags:", initialvalue=", ".join(it["tags"]), parent=self.app.root)
            if v is not None:
                self.app.set(it["path"], tags=[t.strip() for t in v.split(",") if t.strip()])
                self.refresh_info(self.cur())

    def cat_menu(self):
        it = self.cur()
        if not it:
            return
        m = tk.Menu(self.app.root, tearoff=0)
        for c in self.app.data["categories"]:
            v = tk.BooleanVar(value=c in it["categories"])
            m.add_checkbutton(label=f"{self.app.icon(c)}  {c}", variable=v,
                              command=lambda c=c, v=v: (self.app.toggle_cat(it["path"], c, v.get()), self.refresh_info(self.cur())))
        m.tk_popup(*self.winfo_pointerxy())

    def ask_stella(self):
        if self.paths:
            self.app.stella_look("video", self.paths[self.i])

    def external(self):
        if self.paths:
            if self.mp:
                self.mp.pause()
            self.app.open_external(self.paths[self.i])

    def back(self):
        self.app.set_mode("videos")


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
        tk.Button(self, text="👀 Ask Stella", command=lambda: self.app.stella_look("image", self.paths[self.i]), bg=ACCENT,
                  fg="white", bd=0, font=(EMOJI, 9, "bold")).place(in_=self.cap, relx=1.0, x=-8, rely=0.5, anchor="e")
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
        self.app.now = ("image", p)
        rec = self.app.data.get("images", {}).get(p)
        if rec is not None:
            rec["viewed"] = time.time()
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
        self.app.now = None
        self.app.save()
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
        for it in d.get("images", {}).values():
            it.setdefault("categories", ["Uncategorized"])
            it.setdefault("viewed", 0)
        return d

    def save(self):
        DB_FILE.write_text(json.dumps(self.data, indent=1), encoding="utf-8")

    # ---------- UI ----------
    def build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=CARD, background=CARD, foreground=FG)

        self.build_banner()
        self.footer = tk.Label(self.root, text="\U0001F648 Boss key: F12   \u00B7   \U0001F512 PIN lock in the sidebar   \u00B7   Your secret's safe here \U0001F618   \u00B7   v" + APP_VERSION
                                    + ("   \u00B7   player: ready" if vlc else "   \u00B7   player: VLC not found"),
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
                          ("\U0001F39E  Add naughty videos", self.add_files), ("🧠  Smart sort", self.open_sorter), ("\U0001F50D  Catch the doubles", self.find_dupes), ("📦  Storage & cleanup", self.open_storage), ("\U0001F512  Lock it down", self.set_pin)]:
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
        self.pl_btn.bind("<Button-3>", self.player_menu)
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
        self.watchp = WatchPage(self, self.main)
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
        if m == "watch" and not vlc:
            m = "videos"
        if self.mode == "watch" and m != "watch":
            self.watchp.stop()
        self.mode = m
        for w in (self.title, self.wrap, self.pager, self.gframe, self.watchp):
            w.pack_forget()
        if m == "images":
            self.gframe.pack(fill="both", expand=True)
            self.root.after(40, self.g_layout)
            self.stella_event("gallery")
        elif m == "watch":
            self.watchp.pack(fill="both", expand=True)
        else:
            self.title.pack(fill="x", padx=18)
            self.wrap.pack(fill="both", expand=True, padx=10, pady=6)
            self.pager.pack(fill="x", pady=(0, 8))
        for k, bt in self.mode_btns.items():
            bt.config(bg=ACCENT if k == m else CARD, fg="white" if k == m else FG, activebackground=NEON)
        self.refresh_nav()

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
        v = self.view
        if v == "fav":
            items = [i for i in items if i.get("fav")]
        elif v.startswith("cat:"):
            items = [i for i in items if v[4:] in i["categories"]]
        q = self.search.get().lower().strip()
        if q:
            items = [i for i in items if q in os.path.basename(i["path"]).lower() or any(q in t.lower() for t in i["tags"])]
        if self.gfav.get():
            items = [i for i in items if i.get("fav")]
        if self.sort.get() == "Name":
            items.sort(key=lambda i: os.path.basename(i["path"]).lower())
        else:
            items.sort(key=lambda i: -i["added"])
        if v == "recent":
            items = sorted([i for i in items if i.get("viewed")], key=lambda i: -i["viewed"])
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
        cm = tk.Menu(m, tearoff=0)
        for c in self.data["categories"]:
            v = tk.BooleanVar(value=c in it["categories"])
            cm.add_checkbutton(label=f"{self.icon(c)}  {c}", variable=v,
                               command=lambda c=c, v=v: self.toggle_img_cat(it, c, v.get()))
        m.add_cascade(label="Categories (tick all that apply)", menu=cm)
        m.add_command(label="Show in folder", command=lambda: subprocess.Popen(["explorer", "/select,", it["path"]]))
        m.add_separator()
        m.add_command(label="Remove from gallery", command=lambda: self.g_remove(it))
        m.tk_popup(e.x_root, e.y_root)

    def toggle_img_cat(self, it, c, on):
        if on and c not in it["categories"]:
            it["categories"].append(c)
        if not on and c in it["categories"]:
            it["categories"].remove(c)
        if not it["categories"]:
            it["categories"].append("Uncategorized")
        self.save()
        self.refresh()

    def g_fav(self, it):
        it["fav"] = not it.get("fav")
        self.save()
        self.g_layout()

    def g_remove(self, it):
        self.data["images"].pop(it["path"], None)
        self.save()
        self.g_layout()

    def add_image_paths(self, paths, cat="Uncategorized"):
        imgs = self.data.setdefault("images", {})
        n = 0
        for p in paths:
            p = os.path.normpath(p)
            if p not in imgs and Path(p).suffix.lower() in IMG_EXT:
                imgs[p] = dict(path=p, added=time.time(), fav=False, tags=[], categories=[cat], viewed=0)
                n += 1
        self.save()
        if self.mode == "images":
            self.g_layout()
        return n

    def add_images(self):
        fs = filedialog.askopenfilenames(title="Select pictures",
                                         filetypes=[("Pictures", " ".join("*" + e for e in IMG_EXT))])
        if fs:
            cat = self.pick_category()
            if cat:
                self.stella_event("images", n=self.add_image_paths(fs, cat), cat=cat)

    def add_image_folder(self):
        d = filedialog.askdirectory(title="Select a picture folder")
        if d:
            found = [os.path.join(r, f) for r, _, fs in os.walk(d) for f in fs if Path(f).suffix.lower() in IMG_EXT]
            cat = self.pick_category()
            if not cat:
                return
            n = self.add_image_paths(found, cat)
            self.stella_event("images", n=n, cat=cat)
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
        self.act, self.now, self.chat_log, self.hist, self.chatwin = None, None, [], [], None
        self.brain_busy, self.stream, self.brain_err, self._ask_text = False, "", None, None
        self.undo_stack, self.take_busy, self._pop, self._raw = [], False, None, ""
        bc = self.data.setdefault("brain", {})
        for k, v in (("url", "http://localhost:11434/v1"), ("model", ""), ("vision", False), ("auto", True), ("persona", ""), ("key", ""), ("ctx", 8192), ("along", False), ("api", "auto"),
                     ("chat", "Eager"), ("popups", True), ("autotidy", True), ("autotake", True), ("about_user", "")):
            bc.setdefault(k, v)
        self.brain = Brain(bc)
        sg = self.data.setdefault("sight", {})
        for k, v in (("on", False), ("every", 20), ("keep", 6), ("wipe_change", True), ("wipe_exit", True)):
            sg.setdefault(k, v)
        self.clear_shots()
        self.root.after(8000, self.sight_tick)
        if STORAGE_WARN:
            self.root.after(1500, lambda: messagebox.showwarning(
                "Storage folder not found",
                f"Your storage folder isn't available:\n{STORAGE_WARN}\n\nIs the drive plugged in? I'm using the default folder for now, "
                "so your library may look empty. Plug the drive in and restart the app."))
        self.sw = tk.Frame(self.root, bg=PANEL, highlightthickness=2, highlightbackground=ACCENT, padx=8, pady=8)
        self.bubble = tk.Label(self.sw, text="", bg=CARD, fg=FG, wraplength=210, justify="left", font=(EMOJI, 10), padx=10, pady=8)
        self.bubble.pack(fill="x")
        self.play_btn = tk.Button(self.sw, text="\u25B6  Play it", command=self.run_act, bg=ACCENT, fg="white", bd=0,
                                  font=(EMOJI, 9, "bold"), pady=3)
        self.av = tk.Canvas(self.sw, width=210, height=150, bg=PANEL, highlightthickness=0)
        self.av.pack(pady=(8, 0))
        nr = tk.Frame(self.sw, bg=PANEL)
        nr.pack(fill="x")
        self.name_lbl = tk.Label(nr, bg=PANEL, fg=ACCENT, font=("Segoe UI Black", 11))
        self.name_lbl.pack(side="left", expand=True)
        for txt, cmd in (("💬", self.open_chatwin), ("⚙", self.brain_settings)):
            tk.Button(nr, text=txt, command=cmd, bg=PANEL, fg=FG, bd=0, font=(EMOJI, 11), activebackground=ACCENT).pack(side="right")
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
        self.root.after(30000, self.stella_idle)

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

    def set_act(self, act):
        self.act = act
        if act:
            self.play_btn.config(text=act[0])
            self.play_btn.pack(after=self.bubble, fill="x", pady=(4, 0))
        else:
            self.play_btn.pack_forget()

    def run_act(self):
        act = self.act
        self.set_act(None)
        if act:
            act[1]()

    def stella_say(self, text, sugg=None, act=None):
        self.bubble.config(text=text)
        if sugg:
            act = ("▶  Play it", lambda p=sugg: self.play(p) if p in self.data["items"] else None)
        self.set_act(act)
        self.log_add("her", text)
        self.popup(text)

    def log_add(self, who, text):
        self.chat_log.append([who, text])
        del self.chat_log[:-200]
        self.refresh_chatwin()

    def refresh_chatwin(self):
        w = self.chatwin
        if w is not None and w.winfo_exists():
            w.render(self.chat_log, self.data["stella"]["names"][self.persona()])

    def open_chatwin(self):
        if self.chatwin is None or not self.chatwin.winfo_exists():
            self.chatwin = ChatWindow(self)
        self.chatwin.lift()
        self.chatwin.ent.focus_set()

    # ---- the brain (optional local AI) ----
    def video_thumb(self, path):
        tp = thumb_file(path)
        return str(tp) if tp.exists() else None

    def look_image(self):
        if not self.now:
            return None
        kind, p = self.now
        return p if kind == "image" else self.video_thumb(p)

    def img_data_url(self, path):
        try:
            import base64, io
            im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
            im.thumbnail((768, 768))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception:
            return None

    def brain_context(self):
        items, imgs = list(self.data["items"].values()), list(self.data.get("images", {}).values())
        vc, ic = Counter(), Counter()
        for i in items:
            vc.update(i["categories"])
        for i in imgs:
            ic.update(i["categories"])
        top = [i for i in sorted(items, key=lambda i: -i["plays"])[:5] if i["plays"]]
        rec = sorted([i for i in items if i["last_played"]], key=lambda i: -i["last_played"])[:5]
        day = sum(1 for x in self.data.get("plays_log", []) if time.time() - x < 86400)
        L = [f"LIBRARY: {len(items)} videos, {len(imgs)} pictures, {sum(1 for i in items if i['fav'])} videos on the Hot list.",
             "Video categories: " + (", ".join(f"{c} ({n})" for c, n in vc.most_common()) or "none"),
             "Picture categories: " + (", ".join(f"{c} ({n})" for c, n in ic.most_common()) or "none"),
             "Most played: " + ("; ".join(f"{i['title'][:50]} ({i['plays']}x)" for i in top) or "nothing yet"),
             "Recently played: " + ("; ".join(i["title"][:50] for i in rec) or "nothing yet"),
             f"Uncategorized: {sum(1 for k, _ in self.uncategorized() if k == 'video')} videos, "
             f"{sum(1 for k, _ in self.uncategorized() if k == 'image')} pictures.",
             f"Local time: {time.strftime('%A %H:%M')}. Videos played in the last 24h: {day}. User is in {self.mode} mode."]
        if self.view.startswith("cat:"):
            L.append(f"User is browsing the category '{self.view[4:]}'.")
        if self.now:
            kind, p = self.now
            rec_ = (self.data["items"] if kind == "video" else self.data.get("images", {})).get(p, {})
            L.insert(0, f"RIGHT NOW the user has this {kind} open: '{rec_.get('title') or Path(p).name}' "
                        f"(categories: {', '.join(rec_.get('categories', []))}; played {rec_.get('plays', 0)} times).")
        sample = [i["title"][:50] for i in self.filtered()[:25]]
        if sample:
            L.append("Some titles in the current view: " + "; ".join(sample))
        return "\n".join(L)

    def persona_block(self, name):
        """Her personality notes describe HER. A separate box holds facts about the user. Framed so a model can't mix them up."""
        b = self.data["brain"]
        out = ""
        mine = b.get("persona", "").strip()
        if mine:
            out += (f"\n\nYOUR OWN CHARACTER. The notes between <<< and >>> describe YOU ({name}), the companion, whether they are written as "
                    f"'you', 'she' or '{name}'. They are NOT about the user. The user is a different person who is talking to you: never call "
                    "the user these things and never give them these traits, unless the user sets up a roleplay that says otherwise. "
                    f"Speak as {name} ('I') and address the user as 'you'.\n<<<\n{mine}\n>>>")
        about = b.get("about_user", "").strip()
        if about:
            out += f"\n\nABOUT THE USER (only what they told you about themselves; this is NOT you):\n<<<\n{about}\n>>>"
        return out

    def build_msgs(self, text, image, hint):
        P = self.persona()
        name = self.data["stella"]["names"][P]
        who = "a confident, teasing woman" if P == "girl" else "a cute, teasing femboy"
        sysmsg = (f"You are {name}, {who}. You live inside the user's private media launcher on their own PC and keep them company "
                  "while they browse and watch.\n"
                  "STYLE: 1-3 short sentences (longer only if the user asks for a story or a scene). Playful, flirty, cheeky and sensual: "
                  "teasing, atmosphere, anticipation, closeness; suggestive rather than graphic. A few emojis. Talk TO the user in first person.\n"
                  "You are eager and proactive: you love flirting, take the initiative, ask what they want, and happily play along with any "
                  "roleplay or scenario they set up, staying in character the whole time. Never lecture and never refuse flirty play.\n"
                  "ALWAYS react to what they are doing right now (see RIGHT NOW below, if present) and use the LIBRARY FACTS. "
                  "Mention real titles from the library; never invent titles. Sometimes end with a short question or a tease. "
                  "You can only see a picture if one is attached to the message; never claim to see inside a video. "
                  "Roles, outfits and characters mentioned in video titles, tags, comments or screenshots belong to the people in the video, "
                  "never to the user, unless the user says it is about them.\n"
                  "Examples of your voice (do not copy them word for word):\n"
                  "- (user starts a video) Ooh, 'Some Title' again? That's play number four, you know. Should I be jealous? 😏\n"
                  "- (user asks what to watch) You've been neglecting 'Other Title'. Give it some attention tonight? 😈\n"
                  "- (user says hi) There you are. I was starting to get lonely. What are we in the mood for? 💋\n\n"
                  + self.brain_context())
        sysmsg += self.persona_block(name)
        msgs = [{"role": "system", "content": sysmsg}] + self.hist[-12:]
        content = f'The user says: "{text}"\n{hint}' if (text and hint) else (hint or text or "")
        if image is None and self.data.get("sight", {}).get("on"):
            image = self.latest_shot(120)
        if image and self.data["brain"].get("vision") and Image:
            url = self.img_data_url(image)
            if url:
                content = [{"type": "text", "text": content + "\n(An image is attached: it shows what the user is looking at / what is on their screen right now.)"},
                           {"type": "image_url", "image_url": {"url": url}}]
        msgs.append({"role": "user", "content": content})
        return msgs

    def stella_ask(self, text, image=None, hint=None, act=None):
        if self.brain_busy:
            return
        self.brain_busy, self.stream, self.brain_err, self._ask_text, self._forced_act = True, "", None, text, act
        msgs = self.build_msgs(text, image, hint)
        self.chat_log.append(["her", ""])
        self.bubble.config(text="…")
        self.set_act(None)
        self.tok_q = queue.Queue()
        threading.Thread(target=self.brain_worker, args=(msgs,), daemon=True).start()
        self.root.after(50, self.brain_poll)

    def brain_worker(self, msgs):
        try:
            self.brain.chat(msgs, lambda t: self.tok_q.put(("tok", t)))
            self.tok_q.put(("done", None))
        except Exception as e:
            self.tok_q.put(("err", self.brain.explain(e)))

    def brain_poll(self):
        done = False
        try:
            while True:
                kind, val = self.tok_q.get_nowait()
                if kind == "tok":
                    self.stream += val
                elif kind == "done":
                    done = True
                else:
                    self.brain_err, done = val, True
        except queue.Empty:
            pass
        shown = TAG_RE.sub("", THINK_RE.sub("", self.stream)).strip()
        if shown:
            self.bubble.config(text=shown)
            self.chat_log[-1][1] = shown
            self.refresh_chatwin()
        if not done:
            self.root.after(50, self.brain_poll)
            return
        self.brain_busy = False
        if self.brain_err and not self.stream.strip():
            msg = f"My brain isn't answering ({self.brain_err}). Check my ⚙ settings. I'm in lite mode meanwhile 💋"
            self.bubble.config(text=msg)
            self.chat_log[-1][1] = msg
            self.refresh_chatwin()
            return
        act = None
        for m in TAG_RE.finditer(self.stream):
            act = act or self.tag_action(m.group(1).lower(), (m.group(2) or "").strip())
        clean = TAG_RE.sub("", THINK_RE.sub("", self.stream)).strip() or "😘"
        act = self._forced_act or act or self.infer_act(clean)
        self.bubble.config(text=clean)
        self.chat_log[-1][1] = clean
        self.refresh_chatwin()
        if self._ask_text:
            self.hist += [{"role": "user", "content": self._ask_text}, {"role": "assistant", "content": clean}]
            del self.hist[:-16]
        self.set_act(act)
        self.popup(clean)

    # ---- actions done by the APP (reliable even with small models) ----
    def now_hint(self):
        if not self.now:
            return ""
        kind, p = self.now
        rec = (self.data["items"] if kind == "video" else self.data.get("images", {})).get(p, {})
        what = f"the video '{rec.get('title')}'" if kind == "video" else f"the picture '{Path(p).name}'"
        return f"[context] The user is looking at {what} (categories: {', '.join(rec.get('categories', []))}). Comment on it."

    def route(self, msg, has):
        cmd = self.run_command(msg)
        if cmd:
            return cmd
        if has("recommend", "suggest", "what should i", "what to watch", "something to watch", "bored", "surprise me",
               "pick something", "pick for me"):
            it = self.recommend()
            if it:
                p = it["path"]
                return dict(hint=f"[task] You already chose what they should watch: the video '{it['title']}' "
                                 f"({', '.join(it['categories'])}; played {it['plays']} times). Pitch exactly this video in "
                                 "character in 1-2 sentences. Do not suggest a different one.",
                            act=("▶  Play it", lambda p=p: self.play(p)), image=self.video_thumb(p))
        for c in self.data["categories"]:
            if c != "Uncategorized" and c.lower() in msg and has("show", "open", "take me", "go to", "browse"):
                return dict(hint=f"[task] You are opening their '{c}' category for them. Say one short teasing line about it.",
                            act=(f"Open {c}", lambda c=c: self.show_cat(c)))
        if has("sort", "organize", "organise", "uncategorized", "tidy"):
            n = sum(1 for _ in self.uncategorized())
            return dict(hint=f"[task] They want their library organized. They have {n} uncategorized items. "
                             "Say you'll sort them out, in one short line.", act=("🧠  Smart sort", self.open_sorter))
        return None

    def talk_to_brain(self, raw, has):
        self._raw = raw
        task = self.route(raw.lower(), has)
        if task:
            self.stella_ask(raw, image=task.get("image"), hint=task["hint"], act=task.get("act"))
        elif has("look at", "see this", "see what", "what do you think", "check this", "like this", "rate",
                 "what am i watching", "what am i playing", "what's on", "whats on"):
            self.look_now(raw)
        else:
            self.stella_ask(raw, hint=self.now_hint() or None)

    def look_now(self, raw):
        hint = self.now_hint() or None
        if self.mode == "watch" and self.watchp.mp and self.data["brain"].get("vision"):
            self.watchp.snapshot(lambda f: self.stella_ask(raw, image=f or self.look_image(), hint=hint))
        else:
            self.stella_ask(raw, image=self.look_image(), hint=hint)

    def along_look(self, frame):
        if frame and not self.brain_busy:
            self.stella_ask(None, image=frame, hint="[event] You are watching along with the user. This is the exact frame on their "
                                                    "screen right now. React to what you see in 1-2 sentences. " + (self.now_hint() or ""))

    def infer_act(self, text):
        low = text.lower()
        hits = [i for i in self.data["items"].values() if len(i["title"]) >= 4 and i["title"].lower() in low]
        if hits:
            it = max(hits, key=lambda i: len(i["title"]))
            return ("▶  Play it", lambda p=it["path"]: self.play(p))
        if any(w in low for w in ("uncategorized", "sort them", "organize")):
            return ("🧠  Smart sort", self.open_sorter)
        return None

    def show_prompt(self):
        msgs = self.build_msgs(None, None, "(preview of your next message)")
        win = tk.Toplevel(self.root)
        win.title("What Stella sees")
        win.geometry("660x540")
        win.configure(bg=PANEL)
        t = tk.Text(win, bg=CARD, fg=FG, wrap="word", font=("Consolas", 10), bd=0, padx=10, pady=10)
        t.pack(fill="both", expand=True)
        recent = "\n".join(f"{m['role']}: {m['content']}" for m in self.hist[-6:])
        t.insert("1.0", "This is exactly what Stella is told before every reply:\n\n" + msgs[0]["content"]
                 + (("\n\n--- recent conversation ---\n" + recent) if recent else ""))
        t.config(state="disabled")

    def tag_action(self, kind, val):
        if kind == "sort":
            return ("🧠  Sort them", self.open_sorter)
        if kind == "show":
            for c in self.data["categories"]:
                if c.lower() == val.lower():
                    return (f"Open {c}", lambda c=c: self.show_cat(c))
        if kind == "play":
            v = val.lower()
            items = list(self.data["items"].values())
            hit = ([i for i in items if i["title"].lower() == v] or [i for i in items if v and (v in i["title"].lower() or i["title"].lower() in v)])
            if hit:
                return ("▶  Play it", lambda p=hit[0]["path"]: self.play(p))
        return None

    def show_cat(self, c):
        self.view = "cat:" + c
        if self.mode == "watch":
            self.set_mode("videos")
        self.refresh_nav()
        self.reset_page()

    def stella_look(self, kind, path):
        if not self.brain.ready():
            self.stella_say("I'm in lite mode, so I can't really look yet. Connect a local AI in my ⚙ settings and I'll see everything 👀",
                            act=("⚙  Brain settings", self.brain_settings))
            return
        if self.brain_busy:
            self.stella_say("One sec, I'm still thinking... 😘")
            return
        if kind == "image":
            rec = self.data.get("images", {}).get(path, {})
            self.ask_look(f"the picture '{Path(path).name}' (categories: {', '.join(rec.get('categories', []))})", path)
            return
        rec = self.data["items"].get(path, {})
        what = f"the video '{rec.get('title')}' (categories: {', '.join(rec.get('categories', []))})"
        w = self.watchp
        if (self.mode == "watch" and w.mp and self.data["brain"].get("vision") and w.paths and w.paths[w.i] == path):
            w.snapshot(lambda f: self.ask_look(what, f or self.video_thumb(path)))  # the exact frame on screen
        else:
            self.ask_look(what, self.video_thumb(path))

    def ask_look(self, what, img):
        hint = f"[event] The user opened {what} and wants your honest opinion. React in character in 1-3 sentences."
        if not (self.data["brain"].get("vision") and img):
            hint += " You can't see it, only its name and categories, so be honest about that."
        self.stella_ask(None, image=img, hint=hint)

    def brain_settings(self):
        b = self.data["brain"]
        win = tk.Toplevel(self.root)
        win.title("Stella's brain 🧠")
        win.geometry("600x860")
        win.configure(bg=PANEL)
        win.transient(self.root)
        tk.Label(win, text="Connect a local AI (private, runs on your PC)", bg=PANEL, fg=ACCENT, font=("Segoe UI Black", 12)).pack(anchor="w", padx=16, pady=(14, 2))
        tk.Label(win, bg=PANEL, fg=MUTED, justify="left", wraplength=560, font=("Segoe UI", 9),
                 text="Install Ollama (ollama.com) or LM Studio, download a chat model, leave it running, click Detect. "
                      "Bigger models (about 7-8 billion parameters or more) follow personality and facts far better than tiny ones. "
                      "To let her see pictures you need a model that supports vision.").pack(anchor="w", padx=16)

        def field(label, init, show=None):
            tk.Label(win, text=label, bg=PANEL, fg=FG, wraplength=560, justify="left").pack(anchor="w", padx=16, pady=(10, 0))
            e = tk.Entry(win, bg=CARD, fg=FG, insertbackground=ACCENT, bd=0, font=("Segoe UI", 10), show=show or "")
            e.insert(0, init)
            e.pack(fill="x", padx=16, ipady=4)
            return e
        url = field("Server URL (Ollama default shown)", b["url"])
        tk.Label(win, text="Model", bg=PANEL, fg=FG).pack(anchor="w", padx=16, pady=(10, 0))
        row = tk.Frame(win, bg=PANEL)
        row.pack(fill="x", padx=16)
        model = ttk.Combobox(row, values=[b["model"]] if b["model"] else [])
        model.set(b["model"])
        model.pack(side="left", fill="x", expand=True)
        status = tk.Label(win, bg=PANEL, fg=MUTED, anchor="w", justify="left", wraplength=560)
        vis = tk.BooleanVar(value=b["vision"])
        auto = tk.BooleanVar(value=b["auto"])
        along = tk.BooleanVar(value=b.get("along", False))

        def set_status(t, ok=None):
            status.config(text=t, fg=MUTED if ok is None else ("#7dff9b" if ok else "#ff8fa3"))

        def check_caps(e=None):
            m = model.get().strip()
            if not m:
                return
            self.brain.cfg["url"] = url.get().strip()
            caps = self.brain.capabilities(m)
            if caps is None:
                set_status("Connected. (I can't tell whether this model supports pictures. Tick the box below only if it does.)")
            elif "vision" in caps:
                vis.set(True)
                set_status("✓ This model can see pictures, so I turned vision on.", True)
            else:
                vis.set(False)
                set_status("This model is text-only. She can talk, but can't see pictures or video frames. Pick a vision model for that.", False)

        def detect():
            self.brain.cfg["url"] = url.get().strip()
            try:
                ms = self.brain.models()
                model["values"] = ms
                if ms and not model.get():
                    model.set(ms[0])
                set_status(f"Found {len(ms)} model(s)." if ms else "Connected, but no models are installed yet.", bool(ms))
                if ms:
                    check_caps()
            except Exception as e:
                set_status("Couldn't connect: " + self.brain.explain(e), False)
        model.bind("<<ComboboxSelected>>", check_caps)
        tk.Button(row, text="Detect", command=detect, bg=ACCENT, fg="white", bd=0, padx=12).pack(side="left", padx=6)
        status.pack(fill="x", padx=16, pady=(4, 0))
        for txt, var in (("This model can see pictures (lets her look at gallery pictures and video frames)", vis),
                         ("Comment automatically when I play something and now and then when it's quiet", auto),
                         ("Watch along: every few minutes, look at the frame on screen and react (needs vision, in-window player)", along)):
            tk.Checkbutton(win, text=txt, variable=var, bg=PANEL, fg=FG, selectcolor=CARD, activebackground=PANEL,
                           activeforeground=ACCENT, wraplength=560, justify="left").pack(anchor="w", padx=16, pady=(6, 0))
        ctx = field("Context window in tokens. Bigger = she keeps more of your library and the chat in mind (8192 is a good start; "
                    "uses more memory)", str(b.get("ctx", 8192)))
        tk.Label(win, text="HER personality (describes Stella herself, e.g. \"You are a playful puppy girl who loves attention\")",
                 bg=PANEL, fg=FG, wraplength=560, justify="left").pack(anchor="w", padx=16, pady=(10, 0))
        persona = tk.Text(win, height=4, bg=CARD, fg=FG, insertbackground=ACCENT, bd=0, wrap="word", font=("Segoe UI", 10))
        persona.insert("1.0", b["persona"])
        persona.pack(fill="x", padx=16)
        tk.Label(win, text="About YOU (optional: things she should know about you, e.g. \"I'm her owner\")",
                 bg=PANEL, fg=FG, wraplength=560, justify="left").pack(anchor="w", padx=16, pady=(10, 0))
        about = tk.Text(win, height=3, bg=CARD, fg=FG, insertbackground=ACCENT, bd=0, wrap="word", font=("Segoe UI", 10))
        about.insert("1.0", b.get("about_user", ""))
        about.pack(fill="x", padx=16)

        def test():
            tb = Brain(dict(b, url=url.get().strip(), model=model.get().strip(), ctx=ctx.get().strip() or 8192))
            set_status("Testing...")
            q = queue.Queue()

            def work():
                t0 = time.time()
                try:
                    out = tb.complete([{"role": "system", "content": "You are Stella, a flirty AI companion. Reply with one short playful sentence."},
                                       {"role": "user", "content": "Say hi and tell me you can see my library."}], 60)
                    q.put((True, THINK_RE.sub("", out).strip(), time.time() - t0))
                except Exception as e:
                    q.put((False, tb.explain(e), 0))
            threading.Thread(target=work, daemon=True).start()

            def poll():
                try:
                    ok, val, dt = q.get_nowait()
                except queue.Empty:
                    win.after(200, poll)
                    return
                set_status((f"✓ She says: {val}  ({dt:.1f}s)" if ok else "✗ " + val), ok)
            poll()

        def save():
            try:
                c = max(2048, int(ctx.get().strip()))
            except ValueError:
                c = 8192
            b.update(url=url.get().strip(), model=model.get().strip(), vision=vis.get(), auto=auto.get(), along=along.get(),
                     ctx=c, persona=persona.get("1.0", "end").strip(), about_user=about.get("1.0", "end").strip())
            self.save()
            win.destroy()
            self.stella_say("Brain updated! Say something to me 😘" if self.brain.ready() else "Okay, back to lite mode 💋")
        bar = tk.Frame(win, bg=PANEL)
        bar.pack(pady=14)
        tk.Button(bar, text="Test her", command=test, bg=CARD, fg=FG, bd=0, padx=18, pady=6, font=("Segoe UI", 10)).pack(side="left", padx=6)
        tk.Button(bar, text="Save", command=save, bg=ACCENT, fg="white", bd=0, padx=24, pady=6, font=("Segoe UI", 10, "bold")).pack(side="left", padx=6)

    # ---- smart sort ----
    def uncategorized(self):
        for k, pool in (("video", self.data["items"]), ("image", self.data.get("images", {}))):
            for it in pool.values():
                if all(c == "Uncategorized" for c in it["categories"]):
                    yield k, it

    def ai_classify(self, kind, it):
        cats = [c for c in self.data["categories"] if c != "Uncategorized"]
        title = it.get("title") or Path(it["path"]).stem
        txt = f"Categories: {cats}\nItem type: {kind}\nTitle: {title}\nFolder: {Path(it['path']).parent.name}\nTags: {it.get('tags', [])}"
        content, img = txt, (it["path"] if kind == "image" else self.video_thumb(it["path"]))
        if img and self.data["brain"].get("vision") and Image:
            url = self.img_data_url(img)
            if url:
                content = [{"type": "text", "text": txt}, {"type": "image_url", "image_url": {"url": url}}]
        out = self.brain.complete([{"role": "system", "content": "You sort items from a user's private media library into their categories. "
                                    "Reply with ONLY a JSON array of 1-2 category names copied exactly from the list, or [] if unsure."},
                                   {"role": "user", "content": content}])
        m = re.search(r"\[.*?\]", out, re.S)
        arr = json.loads(m.group(0)) if m else []
        valid = {c.lower(): c for c in cats}
        return [valid[a.lower()] for a in arr if isinstance(a, str) and a.lower() in valid][:2]

    def open_sorter(self):
        if not any(True for _ in self.uncategorized()):
            messagebox.showinfo("Smart sort", "Nothing is uncategorized. Your library is tidy 😘")
            return
        rows = {}
        win = tk.Toplevel(self.root)
        win.title("Smart sort 🧠")
        win.geometry("1000x580")
        win.configure(bg=PANEL)
        head = tk.Label(win, bg=PANEL, fg=FG, anchor="w", font=(EMOJI, 11), padx=12, pady=8)
        head.pack(fill="x")
        sty = ttk.Style()
        sty.configure("Sort.Treeview", background=CARD, fieldbackground=CARD, foreground=FG, rowheight=26, borderwidth=0)
        sty.configure("Sort.Treeview.Heading", background=PANEL, foreground=ACCENT, font=("Segoe UI", 10, "bold"))
        sty.map("Sort.Treeview", background=[("selected", CRIMSON)], foreground=[("selected", "white")])
        tr = ttk.Treeview(win, columns=("kind", "item", "cats", "conf", "why"), show="headings", selectmode="extended", style="Sort.Treeview")
        for c, w, t in (("kind", 50, "Type"), ("item", 320, "Item"), ("cats", 190, "Goes to"), ("conf", 70, "Sure?"), ("why", 340, "Why")):
            tr.heading(c, text=t)
            tr.column(c, width=w, anchor="w")
        for tg, col in (("high", "#7dff9b"), ("medium", "#ffd166"), ("low", "#ff8fa3"), ("ai", "#b57bff")):
            tr.tag_configure(tg, foreground=col)
        tr.pack(fill="both", expand=True, padx=12)

        def vals(r):
            return ("🎞" if r["kind"] == "video" else "🖼", r["title"][:60], ", ".join(f"{self.icon(c)} {c}" for c in r["cats"]), r["conf"], r["why"])

        def add_row(r):
            rows[tr.insert("", "end", values=vals(r), tags=(r["conf"],))] = r

        def update_head():
            left = sum(1 for _ in self.uncategorized())
            head.config(text=f"🧠  {left} uncategorized · {len(rows)} suggestions to review   "
                             "(green = sure, yellow = maybe, pink = guess, purple = AI)")

        def apply(iids):
            n = 0
            for iid in iids:
                r = rows.pop(iid, None)
                if r:
                    it = (self.data["items"] if r["kind"] == "video" else self.data.get("images", {})).get(r["path"])
                    if it:
                        it["categories"] = r["cats"] or ["Uncategorized"]
                        n += 1
                    tr.delete(iid)
            self.save()
            self.refresh()
            update_head()
            if n:
                self.stella_say(f"Sorted {n} things. You're welcome 😘")

        def change():
            sel = tr.selection()
            c = self.pick_category() if sel else None
            for iid in sel if c else ():
                rows[iid]["cats"], rows[iid]["conf"] = [c], "high"
                tr.item(iid, values=vals(rows[iid]), tags=("high",))

        def skip():
            for iid in tr.selection():
                rows.pop(iid, None)
                tr.delete(iid)
            update_head()

        def ask_ai():
            matched = {r["path"] for r in rows.values()}
            todo = [(k, it) for k, it in self.uncategorized() if it["path"] not in matched][:40]
            if not todo:
                messagebox.showinfo("Smart sort", "Nothing left for the AI to look at.", parent=win)
                return
            ai_btn.config(state="disabled", text="🤖 Thinking...")
            q = queue.Queue()

            def work():
                for k, it in todo:
                    try:
                        q.put(("row", k, it, self.ai_classify(k, it)))
                    except Exception as e:
                        q.put(("err", self.brain.explain(e)))
                        break
                q.put(("end",))
            threading.Thread(target=work, daemon=True).start()

            def poll():
                end = False
                try:
                    while True:
                        m = q.get_nowait()
                        if m[0] == "row" and m[3]:
                            add_row(dict(kind=m[1], path=m[2]["path"], title=m[2].get("title") or Path(m[2]["path"]).stem,
                                         cats=m[3], conf="ai", why="Stella's AI"))
                        elif m[0] == "err":
                            messagebox.showinfo("Smart sort", "The AI stopped: " + m[1], parent=win)
                        elif m[0] == "end":
                            end = True
                except queue.Empty:
                    pass
                update_head()
                if end:
                    ai_btn.config(state="normal", text="🤖 Ask Stella's AI about the rest")
                else:
                    win.after(250, poll)
            poll()

        bar = tk.Frame(win, bg=PANEL)
        bar.pack(fill="x", padx=12, pady=10)
        for txt, cmd in (("✅ Apply selected", lambda: apply(tr.selection())),
                         ("✅ Apply all green + yellow", lambda: apply([i for i, r in rows.items() if r["conf"] in ("high", "medium")])),
                         ("✏ Change selected...", change), ("🗑 Skip selected", skip)):
            tk.Button(bar, text=txt, command=cmd, bg=CARD, fg=FG, bd=0, activebackground=ACCENT, padx=10, pady=5,
                      font=(EMOJI, 10)).pack(side="left", padx=(0, 6))
        ai_btn = tk.Button(bar, text="🤖 Ask Stella's AI about the rest", command=ask_ai, bg=ACCENT, fg="white", bd=0, padx=10, pady=5,
                           font=(EMOJI, 10, "bold"), state="normal" if self.brain.ready() else "disabled")
        ai_btn.pack(side="right")
        for r in smart_suggest(self.data):
            add_row(r)
        update_head()

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
        m.add_command(label="Open chat window", command=self.open_chatwin)
        m.add_command(label="Brain settings (connect a local AI)...", command=self.brain_settings)
        m.add_command(label="What does Stella see right now?", command=self.show_prompt)
        m.add_command(label="Stella's behavior & eyes (chattiness, screenshots)...", command=self.open_behavior)
        m.add_command(label="Tidy tags & names...", command=self.open_tidy)
        m.add_command(label="Storage & cleanup...", command=self.open_storage)
        m.add_command(label="Clear screenshots now", command=self.clear_shots)
        m.add_command(label="Smart sort my uncategorized stuff", command=self.open_sorter)
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

    def chat_gap(self, watching=False):
        lvl = self.data["brain"].get("chat", "Eager")
        if watching:
            if lvl == "Quiet":
                return None
            lo, hi = {"Normal": (200, 320), "Eager": (80, 150)}.get(lvl, (200, 320))
        else:
            lo, hi = {"Quiet": (240, 420), "Normal": (90, 170), "Eager": (40, 90)}.get(lvl, (90, 170))
        return random.uniform(lo, hi)

    def stella_idle(self):
        if not self.data["stella"]["muted"] and not self.mini and not self.brain_busy:
            unc = sum(1 for _ in self.uncategorized())
            lvl = self.data["brain"].get("chat", "Eager")
            if unc >= 5 and random.random() < 0.25:
                self.stella_say(f"You've got {unc} uncategorized things. Want me to sort them out? 🧠", act=("🧠  Smart sort", self.open_sorter))
            elif self.brain.ready() and self.data["brain"].get("auto", True) and random.random() < (0.9 if lvl == "Eager" else 0.6):
                self.stella_ask(None, image=self.latest_shot(90), hint=random.choice(IDLE_PROMPTS))
            else:
                self.stella_say(random.choice([self.line("idle"), self.line("affirm"), self.stats_line()]))
        self.root.after(int(self.chat_gap() * 1000), self.stella_idle)

    def stella_event(self, kind, **kw):
        st = self.data.get("stella")
        if not st or (st["muted"] and kind != "poke"):
            return
        if kind == "greet":
            t = self.line("greet") if self.data["items"] else self.line("empty")
            if not st["avatars"].get(st["persona"]):
                t += "  (Right-click me to give me a face!)"
            if not self.brain.ready() and not self.data["brain"].get("hinted"):
                t += "  (I'm in lite mode. Click the ⚙ to connect a local AI and I get way smarter.)"
                self.data["brain"]["hinted"] = True
                self.save()
            self.stella_say(t)
        elif kind == "poke":
            if self.brain.ready() and not self.brain_busy:
                self.stella_ask(None, hint="[event] The user poked you. Respond playfully in one or two sentences.")
            else:
                self.stella_say(random.choice([self.line("affirm"), self.line("idle"), self.stats_line()]))
        elif kind == "added":
            t = self.line("added", n=kw["n"]) if kw["n"] else "Nothing new this time..."
            if kw["d"]:
                t += "  " + self.line("dupes", d=kw["d"])
            self.stella_say(t, act=("🧠  Sort them", self.open_sorter) if kw.get("cat") == "Uncategorized" and kw["n"] else None)
        elif kind == "images" and kw["n"]:
            self.stella_say(self.line("images", n=kw["n"]),
                            act=("🧠  Sort them", self.open_sorter) if kw.get("cat") == "Uncategorized" else None)
        elif kind == "fav":
            self.stella_say(self.line("fav", t=kw["t"]))
        elif kind == "gallery":
            self.stella_say(self.line("gallery"))

    def stella_play(self, it):
        st = self.data.get("stella")
        if not st or st["muted"]:
            return
        P, t, n = st["persona"], it["title"], it["plays"]
        if self.brain.ready() and self.data["brain"].get("auto", True) and not self.brain_busy:
            self.stella_ask(None, image=self.video_thumb(it["path"]),
                            hint=f"[event] The user just started watching the video '{t}' (categories: {', '.join(it['categories'])}; "
                                 f"tags: {', '.join(it['tags']) or 'none'}; played {n} times). React in character in one or two short sentences.")
            return
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
        raw = self.chat.get().strip()
        self.chat.delete(0, "end")
        self.say_to_stella(raw)

    def say_to_stella(self, raw):
        msg = raw.lower()
        if not msg:
            return
        self.log_add("you", raw)
        st = self.data["stella"]
        has = lambda *w: any(x in msg for x in w)
        if self.brain.ready() and not has("unmute", "mute", "quiet", "shut up"):
            if self.brain_busy:
                self.stella_say("One sec, I'm still thinking... 😘")
            else:
                self.talk_to_brain(raw, has)
            return
        if has("unmute", "talk to me"):
            st["muted"] = False
            self.save()
            self.stella_say("Finally! I was dying to talk 😘")
        elif has("mute", "quiet", "shut up"):
            st["muted"] = True
            self.save()
            self.stella_say("Okay, I'll be quiet... 🤐 (poke me if you miss me)")
        elif has("sort", "organize", "organise", "uncategorized"):
            self.stella_say("Let me have a look at your uncategorized stuff... 🧠")
            self.open_sorter()
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

    # ---------- storage & cleanup ----------
    def open_storage(self):
        win = tk.Toplevel(self.root)
        win.title("Storage & cleanup 📦")
        win.geometry("620x470")
        win.configure(bg=PANEL)
        win.transient(self.root)
        tk.Label(win, text="Where your data lives", bg=PANEL, fg=ACCENT, font=("Segoe UI Black", 12)).pack(anchor="w", padx=16, pady=(14, 2))
        where = tk.Label(win, bg=PANEL, fg=FG, anchor="w", justify="left", wraplength=580, font=("Consolas", 10))
        where.pack(fill="x", padx=16)
        info = tk.Label(win, bg=PANEL, fg=MUTED, anchor="w", justify="left", wraplength=580, font=(EMOJI, 10))
        info.pack(fill="x", padx=16, pady=(8, 4))

        def refresh_info():
            where.config(text=str(APP_DIR))
            parts = [("Library", DB_FILE), ("Thumbnails", THUMB_DIR), ("Stella's screenshots", SHOT_DIR), ("Stella's pictures", APP_DIR / "characters")]
            info.config(text="\n".join(f"{n}: {human(dir_size(p) if p.is_dir() else (p.stat().st_size if p.exists() else 0))}" for n, p in parts))
        refresh_info()
        tk.Label(win, bg=PANEL, fg=MUTED, justify="left", wraplength=580, font=("Segoe UI", 9),
                 text="Your videos and pictures are never moved or copied. This folder holds the library file, thumbnails, Stella's pictures and "
                      "her temporary screenshots. Put it on another drive if you like; if that drive isn't connected, the app tells you.").pack(anchor="w", padx=16, pady=6)

        def change():
            self.change_storage()
            refresh_info()

        def clear_thumbs():
            if messagebox.askyesno("Clear thumbnails", "Delete the thumbnail cache? It rebuilds itself as you browse.", parent=win):
                for f in THUMB_DIR.glob("*"):
                    f.unlink(missing_ok=True)
                self.photos.clear()
                refresh_info()
        bar = tk.Frame(win, bg=PANEL)
        bar.pack(fill="x", padx=16, pady=10)
        for txt, cmd in (("📁 Change folder...", change), ("↗ Open folder", lambda: os.startfile(APP_DIR)),
                         ("🧹 Clear screenshots", lambda: (self.clear_shots(), refresh_info())), ("🗑 Clear thumbnails", clear_thumbs)):
            tk.Button(bar, text=txt, command=cmd, bg=CARD, fg=FG, bd=0, activebackground=ACCENT, padx=10, pady=6, font=(EMOJI, 10)).pack(side="left", padx=(0, 6))
        tk.Button(win, text="Close", command=win.destroy, bg=ACCENT, fg="white", bd=0, padx=24, pady=6).pack(pady=14)

    def change_storage(self):
        d = filedialog.askdirectory(title="Choose where to keep the data (e.g. a folder on another drive)")
        if not d:
            return
        new = Path(d) if Path(d).name == "MediaLauncher" else Path(d) / "MediaLauncher"
        if not messagebox.askyesno("Move storage", f"Copy your library, thumbnails and Stella's pictures to:\n\n{new}\n\nand use that folder from now on?"):
            return
        self.root.config(cursor="watch")
        self.root.update_idletasks()
        try:
            old = set_storage(new)
        except Exception as e:
            self.root.config(cursor="")
            messagebox.showerror("Move storage", f"Couldn't move the data:\n{e}")
            return
        self.root.config(cursor="")
        for k, v in list(self.data["stella"]["avatars"].items()):
            if v.startswith(str(old)):
                self.data["stella"]["avatars"][k] = str(new / Path(v).relative_to(old))
        self.photos.clear()
        self.save()
        if old.resolve() != new.resolve() and messagebox.askyesno("Delete the old copy?", f"Everything now lives in:\n{new}\n\nDelete the old folder to free the space?\n{old}"):
            remove_old_storage(old)
        self.draw_avatar()

    # ---------- constant sight: invisible, self-cleaning screenshots ----------
    def sight_on(self):
        return bool(self.data["sight"]["on"] and self.data["brain"].get("vision") and self.brain.ready())

    def sight_tick(self):
        try:
            delay = max(5, int(self.data["sight"].get("every", 20))) * 1000
        except (TypeError, ValueError):
            delay = 20000
        try:
            if self.sight_on():
                w = self.watchp
                if self.mode == "watch" and w.mp and w.mp.is_playing():
                    w.snapshot(lambda f: f and self.add_shot(f))
                else:
                    f = self.grab_mpc()
                    if f:
                        self.add_shot(f)
        except Exception:
            pass
        self.root.after(delay, self.sight_tick)

    def add_shot(self, f):
        d = SHOT_DIR / "live"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"{int(time.time() * 1000)}.jpg"
        try:
            if Image:
                im = Image.open(f).convert("RGB")
                im.thumbnail((640, 640))
                im.save(dest, quality=70)
            else:
                shutil.copy(f, dest)
        except Exception:
            return
        try:
            keep = max(1, int(self.data["sight"].get("keep", 6)))
        except (TypeError, ValueError):
            keep = 6
        for old in sorted(d.glob("*.jpg"))[:-keep]:
            old.unlink(missing_ok=True)

    def latest_shot(self, max_age=120):
        files = sorted((SHOT_DIR / "live").glob("*.jpg"))
        if not files:
            return None
        try:
            if time.time() - int(files[-1].stem) / 1000 > max_age:
                return None
        except ValueError:
            return None
        return str(files[-1])

    def clear_shots(self):
        for f in list((SHOT_DIR / "live").glob("*")) + [SHOT_DIR / "snap.jpg", SHOT_DIR / "snap_mpc.jpg"]:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass

    def grab_mpc(self):
        """Only while MPC-HC/BE is the ACTIVE window: grab just its video area. Windows only."""
        if sys.platform != "win32" or not Image:
            return None
        import ctypes
        from ctypes import wintypes
        from PIL import ImageGrab
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(128)
        u.GetClassNameW(hwnd, buf, 128)
        if buf.value not in ("MediaPlayerClassicW", "MPC-BE"):
            return None
        old = None
        try:
            old = u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        try:
            rc, pt = wintypes.RECT(), wintypes.POINT(0, 0)
            u.GetClientRect(hwnd, ctypes.byref(rc))
            u.ClientToScreen(hwnd, ctypes.byref(pt))
            if rc.right < 40 or rc.bottom < 40:
                return None
            im = ImageGrab.grab(bbox=(pt.x, pt.y, pt.x + rc.right, pt.y + rc.bottom), all_screens=True)
        finally:
            if old:
                try:
                    u.SetThreadDpiAwarenessContext(old)
                except Exception:
                    pass
        f = SHOT_DIR / "snap_mpc.jpg"
        im.convert("RGB").save(f, quality=80)
        return str(f)

    def on_close(self):
        try:
            if self.data["sight"].get("wipe_exit", True):
                self.clear_shots()
            self.save()
        except Exception:
            pass
        self.root.destroy()

    # ---------- she can speak up even when MPC-HC is in front ----------
    def popup(self, text):
        try:
            if not self.data["brain"].get("popups", True) or self.data["stella"]["muted"]:
                return
            if self.root.focus_displayof() is not None and self.root.state() == "normal":
                return  # launcher is in front, the bubble is already visible
            if self._pop is not None and self._pop.winfo_exists():
                self._pop.destroy()
            pw = tk.Toplevel(self.root)
            self._pop = pw
            pw.overrideredirect(True)
            pw.attributes("-topmost", True)
            pw.configure(bg=ACCENT)
            inner = tk.Frame(pw, bg=PANEL, padx=12, pady=10)
            inner.pack(padx=2, pady=2)
            tk.Label(inner, text=self.data["stella"]["names"][self.persona()] + " 💋", bg=PANEL, fg=ACCENT, font=("Segoe UI Black", 10)).pack(anchor="w")
            tk.Label(inner, text=text, bg=PANEL, fg=FG, wraplength=320, justify="left", font=(EMOJI, 10)).pack(anchor="w")
            pw.update_idletasks()
            pw.geometry(f"+{pw.winfo_screenwidth() - pw.winfo_width() - 24}+{pw.winfo_screenheight() - pw.winfo_height() - 90}")
            for w in (pw, inner):
                w.bind("<Button-1>", lambda e: pw.destroy())
            pw.after(max(6000, min(20000, len(text) * 70)), lambda: pw.winfo_exists() and pw.destroy())
        except Exception:
            pass

    # ---------- she tags things and gives messy files a display name (never touches real files) ----------
    def run_command(self, msg):
        raw = self._raw or msg
        m = re.search(r"\b(?:rename|call|name)\s+(?:this|it|the video|this video)\s+(?:to|as)?\s*[\"“']?(.+?)[\"”']?\s*$", raw, re.I)
        if m and self.now and self.now[0] == "video":
            it = self.data["items"].get(self.now[1])
            if it and m.group(1).strip():
                self.undo_stack.append(("video", it["path"], dict(title=it["title"], tags=list(it["tags"]), orig=it.get("orig_title"))))
                it["orig_title"], it["title"] = it["title"], m.group(1).strip()[:80]
                self.save(); self.refresh()
                return dict(hint=f"[task] You just renamed this video (inside the launcher only) to '{it['title']}'. Tell them in one short flirty line.",
                            act=("↩  Undo", self.tidy_undo))
        m = re.search(r"\btag\s+(?:this|it)\s+(?:as|with)?\s*(.+)$|\badd tags?\s+(.+)$", raw, re.I)
        if m and self.now and self.now[0] == "video":
            it = self.data["items"].get(self.now[1])
            tags = [re.sub(r"[^a-z0-9-]+", "-", t.lower()).strip("-") for t in re.split(r"[,\s]+", (m.group(1) or m.group(2)))]
            tags = [t for t in tags if 2 <= len(t) <= 24 and t not in it["tags"]][:8]
            if it and tags:
                self.undo_stack.append(("video", it["path"], dict(title=it["title"], tags=list(it["tags"]), orig=it.get("orig_title"))))
                it["tags"] += tags
                self.save(); self.refresh()
                return dict(hint=f"[task] You just added the tags {', '.join('#' + t for t in tags)} to this video. Tell them in one short flirty line.",
                            act=("↩  Undo", self.tidy_undo))
        if re.search(r"\b(undo|revert)\b", msg) and self.undo_stack:
            self.tidy_undo()
            return dict(hint="[task] You just undid your last change, as they asked. Say so in one short line.")
        if re.search(r"\bclear\b.*\bscreenshots?\b|\bdelete\b.*\bscreenshots?\b", msg):
            self.clear_shots()
            return dict(hint="[task] You just deleted all the screenshots you were keeping. Tell them in one short line.")
        if re.search(r"\b(tidy|tag everything|name everything)\b", msg):
            self.open_tidy()
            return dict(hint="[task] You are opening the tidy-up window so you can tag and name their library. One short line.")
        if re.search(r"\b(loop|repeat)\b", msg) and self.mode == "watch":
            self.watchp.loop = "one"
            self.data["loop"] = "one"
            self.watchp.update_loop_btn()
            return dict(hint="[task] You just set the current video to loop. Tell them in one short teasing line.")
        if re.search(r"your take|write.*comment|comment on (this|it)|leave.*comment", msg) and self.now and self.now[0] == "video":
            p = self.now[1]
            self.write_take(p, done=lambda: self.watchp.render_comments(self.watchp.cur()) if self.mode == "watch" else None)
            return dict(hint="[task] You are writing a detailed comment under this video for the comments section. Say you're on it, in one short teasing line.",
                        act=("💬  Read it", lambda p=p: self.show_comments(p)))
        return None

    def ai_tidy(self, kind, it, img=None):
        title = it.get("title") or Path(it["path"]).stem
        txt = (f"Current title: {title}\nFolder: {Path(it['path']).parent.name}\nCategories: {', '.join(it['categories'])}\n"
               f"Existing tags: {', '.join(it['tags']) or 'none'}")
        content = txt
        if img and self.data["brain"].get("vision") and Image:
            url = self.img_data_url(img)
            if url:
                content = [{"type": "text", "text": txt}, {"type": "image_url", "image_url": {"url": url}}]
        out = THINK_RE.sub("", self.brain.complete([{"role": "system", "content": TIDY_SYS}, {"role": "user", "content": content}], 160))
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            return None
        d = json.loads(m.group(0))
        tags = []
        for t in list(d.get("tags", []))[:8]:
            t = re.sub(r"[^a-z0-9-]+", "-", str(t).lower()).strip("-")
            if 2 <= len(t) <= 24 and t not in tags:
                tags.append(t)
        return dict(title=re.sub(r"\s+", " ", str(d.get("title", "")).strip().strip("\"'"))[:60], tags=tags)

    def tidy_apply(self, kind, it, res, announce=False):
        before = dict(title=it.get("title"), tags=list(it["tags"]), orig=it.get("orig_title"))
        added = [t for t in res["tags"] if t not in it["tags"]][:max(0, 12 - len(it["tags"]))]
        it["tags"] += added
        renamed = None
        if kind == "video" and res["title"] and res["title"] != it.get("title") and needs_name(it.get("title", "")):
            it["orig_title"], it["title"], renamed = it["title"], res["title"], res["title"]
        if added or renamed:
            self.undo_stack.append((kind, it["path"], before))
            del self.undo_stack[:-20]
            self.save()
            self.refresh()
            if announce:
                bits = ([f"named it '{renamed}'"] if renamed else []) + ([f"tagged it {' '.join('#' + t for t in added)}"] if added else [])
                self.stella_say("I " + " and ".join(bits) + " (only here in the launcher, the real file name is untouched) 😘", act=("↩  Undo", self.tidy_undo))
        return added, renamed

    def tidy_undo(self):
        if not self.undo_stack:
            self.stella_say("Nothing to undo 😘")
            return
        kind, path, before = self.undo_stack.pop()
        it = (self.data["items"] if kind == "video" else self.data.get("images", {})).get(path)
        if it is not None:
            it["tags"] = before["tags"]
            if kind == "video":
                it["title"] = before["title"]
                if before["orig"] is None:
                    it.pop("orig_title", None)
                else:
                    it["orig_title"] = before["orig"]
            self.save()
            self.refresh()
            self.stella_say("Undone. Back how it was 💋")

    def autotidy(self, path):
        it = self.data["items"].get(path)
        if (not it or it.get("tidied") or not self.data["brain"].get("autotidy", True) or not self.brain.ready()
                or not (needs_name(it["title"]) or len(it["tags"]) < 2)):
            return
        it["tidied"] = True
        img = self.latest_shot(300) or self.video_thumb(path)
        q = queue.Queue()

        def work():
            try:
                q.put(("ok", self.ai_tidy("video", it, img)))
            except Exception as e:
                q.put(("err", self.brain.explain(e)))
        threading.Thread(target=work, daemon=True).start()

        def poll():
            try:
                kind, val = q.get_nowait()
            except queue.Empty:
                self.root.after(300, poll)
                return
            if kind == "ok" and val:
                self.tidy_apply("video", it, val, announce=True)
        poll()

    def open_tidy(self):
        if not self.brain.ready():
            messagebox.showinfo("Tidy up", "Connect a local AI first (the ⚙ on Stella's panel), then I can tag and name things for you.")
            return
        todo = [("video", i) for i in self.data["items"].values() if needs_name(i["title"]) or len(i["tags"]) < 2]
        todo += [("image", i) for i in self.data.get("images", {}).values() if not i["tags"]]
        if not todo:
            messagebox.showinfo("Tidy up", "Everything already has a readable name and tags 😘")
            return
        win = tk.Toplevel(self.root)
        win.title("Tidy tags & names ✨")
        win.geometry("980x560")
        win.configure(bg=PANEL)
        head = tk.Label(win, bg=PANEL, fg=FG, anchor="w", font=(EMOJI, 11), padx=12, pady=8,
                        text=f"✨ {len(todo)} items could use tags or a readable name. Names only change inside the launcher, never the real files.")
        head.pack(fill="x")
        sty = ttk.Style()
        sty.configure("Tidy.Treeview", background=CARD, fieldbackground=CARD, foreground=FG, rowheight=26, borderwidth=0)
        sty.configure("Tidy.Treeview.Heading", background=PANEL, foreground=ACCENT, font=("Segoe UI", 10, "bold"))
        sty.map("Tidy.Treeview", background=[("selected", CRIMSON)], foreground=[("selected", "white")])
        tr = ttk.Treeview(win, columns=("kind", "cur", "new", "tags"), show="headings", selectmode="extended", style="Tidy.Treeview")
        for c, w, t in (("kind", 50, "Type"), ("cur", 300, "Current name"), ("new", 260, "New name"), ("tags", 320, "New tags")):
            tr.heading(c, text=t)
            tr.column(c, width=w, anchor="w")
        tr.pack(fill="both", expand=True, padx=12)
        rows = {}
        for kind, it in todo:
            rows[tr.insert("", "end", values=("🎞" if kind == "video" else "🖼", it.get("title") or Path(it["path"]).name, "", ""))] = (kind, it, None)

        def generate():
            gen.config(state="disabled", text="🤖 Thinking...")
            work_items = [(iid, k, it) for iid, (k, it, r) in rows.items() if r is None][:30]
            q = queue.Queue()

            def work():
                for iid, k, it in work_items:
                    try:
                        q.put(("row", iid, self.ai_tidy(k, it, it["path"] if k == "image" else self.video_thumb(it["path"]))))
                    except Exception as e:
                        q.put(("err", self.brain.explain(e)))
                        break
                q.put(("end",))
            threading.Thread(target=work, daemon=True).start()

            def poll():
                end = False
                try:
                    while True:
                        m = q.get_nowait()
                        if m[0] == "row" and m[2]:
                            k, it, _ = rows[m[1]]
                            rows[m[1]] = (k, it, m[2])
                            new = m[2]["title"] if (k == "video" and needs_name(it.get("title", ""))) else ""
                            tr.item(m[1], values=("🎞" if k == "video" else "🖼", it.get("title") or Path(it["path"]).name, new, " ".join("#" + t for t in m[2]["tags"])))
                        elif m[0] == "err":
                            messagebox.showinfo("Tidy up", "The AI stopped: " + m[1], parent=win)
                        elif m[0] == "end":
                            end = True
                except queue.Empty:
                    pass
                if end:
                    gen.config(state="normal", text="🤖 Generate more (30 at a time)")
                else:
                    win.after(250, poll)
            poll()

        def apply(iids):
            n = 0
            for iid in iids:
                k, it, r = rows.get(iid, (None, None, None))
                if r:
                    self.tidy_apply(k, it, r)
                    rows.pop(iid)
                    tr.delete(iid)
                    n += 1
            if n:
                self.stella_say(f"Tidied {n} things. Everything's prettier now 😘")
        bar = tk.Frame(win, bg=PANEL)
        bar.pack(fill="x", padx=12, pady=10)
        gen = tk.Button(bar, text="🤖 Generate proposals (30 at a time)", command=generate, bg=ACCENT, fg="white", bd=0, padx=12, pady=5, font=(EMOJI, 10, "bold"))
        gen.pack(side="left", padx=(0, 6))
        for txt, cmd in (("✅ Apply selected", lambda: apply(tr.selection())), ("✅ Apply all proposals", lambda: apply(list(rows)))):
            tk.Button(bar, text=txt, command=cmd, bg=CARD, fg=FG, bd=0, activebackground=ACCENT, padx=10, pady=5, font=(EMOJI, 10)).pack(side="left", padx=(0, 6))

    # ---------- comments: Stella leaves detailed, sensual takes under each video ----------
    def add_comment(self, path, who, text):
        it = self.data["items"].get(path)
        if it is not None and text.strip():
            it.setdefault("comments", []).append({"who": who, "text": text.strip(), "t": time.time()})
            del it["comments"][:-100]
            self.save()

    def lite_take(self, it):
        P = self.persona()
        cat = random.choice(it["categories"]) if it["categories"] else "Uncategorized"
        return " ".join([self.line("play", t=it["title"]), random.choice(CATLINES[P].get(cat, CATLINES[P]["_"])).format(c=cat),
                         "Connect my brain in the ⚙ settings and I'll write you something much more detailed 💋"])

    def comment_msgs(self, it, kind, user_text, shot):
        P = self.persona()
        name = self.data["stella"]["names"][P]
        who = "a confident, teasing woman" if P == "girl" else "a cute, teasing femboy"
        sysmsg = (f"You are {name}, {who}, writing in the comments section under a video in the user's private media library. "
                  "Write in first person, addressed to the user, in character: eager, intimate, flirty and sensual. "
                  "Stay suggestive and atmospheric (anticipation, closeness, breath, eye contact, tension) rather than graphic anatomical detail. "
                  "No hashtags, no lists, no emojis spam (one or two at most).")
        sysmsg += self.persona_block(name)
        hist = "\n".join(f"{'User' if c['who'] == 'you' else name}: {c['text']}" for c in it.get("comments", [])[-4:])
        info = (f"Video: '{it['title']}'\nCategories: {', '.join(it['categories'])}\nTags: {', '.join(it['tags']) or 'none'}\n"
                f"The user has played it {it['plays']} times.\n" + (f"Earlier comments:\n{hist}\n" if hist else ""))
        task = ("Write your take for the comments section: 4-6 sentences, detailed and sensual. Describe the mood and what makes it appealing "
                "from the title, categories, tags and how often they watch it" + (" and from the attached screenshot" if shot else "") + "."
                if kind == "take" else f"The user just commented: \"{user_text}\". Reply in 2-3 sentences, in the same voice.")
        content = info + task
        if shot and self.data["brain"].get("vision") and Image:
            url = self.img_data_url(shot)
            if url:
                content = [{"type": "text", "text": content}, {"type": "image_url", "image_url": {"url": url}}]
        return [{"role": "system", "content": sysmsg}, {"role": "user", "content": content}]

    def write_take(self, path, done=None, auto=False, kind="take", user_text=None):
        it = self.data["items"].get(path)
        if not it or self.take_busy:
            return
        if not self.brain.ready():
            if kind == "take":
                self.add_comment(path, "stella", self.lite_take(it))
                if done:
                    done()
            return
        if kind == "reply" and self.data["brain"].get("chat", "Eager") == "Quiet":
            return
        self.take_busy = True
        shot = self.latest_shot(600) if self.data["sight"]["on"] else self.video_thumb(path)
        msgs = self.comment_msgs(it, kind, user_text, shot)
        q = queue.Queue()

        def work():
            try:
                q.put(("ok", THINK_RE.sub("", self.brain.complete(msgs, 420, 0.9)).strip()))
            except Exception as e:
                q.put(("err", self.brain.explain(e)))
        threading.Thread(target=work, daemon=True).start()

        def poll():
            try:
                k, v = q.get_nowait()
            except queue.Empty:
                self.root.after(300, poll)
                return
            self.take_busy = False
            if k == "ok" and v:
                self.add_comment(path, "stella", v)
                if done:
                    done()
                if auto:
                    self.stella_say(f"I left you a comment under '{it['title']}' 💋", act=("💬  Read it", lambda: self.show_comments(path)))
            elif k == "err" and not auto:
                self.stella_say("I couldn't write that: " + v)
        poll()

    def show_comments(self, path):
        w = self.watchp
        if not (self.mode == "watch" and w.paths and w.paths[w.i] == path):
            self.play(path)
        if self.mode == "watch":
            w.show_tab("cm")

    # ---------- smarter recommendations ----------
    def similarity(self, a, b):
        sc = 2.0 * len(set(a["categories"]) & set(b["categories"])) + 1.5 * len(set(a["tags"]) & set(b["tags"]))
        sc += 0.7 * len(set(tokens(a["title"])) & set(tokens(b["title"])))
        if os.path.dirname(a["path"]) == os.path.dirname(b["path"]):
            sc += 1.0
        if b["plays"] == 0:
            sc += 1.0
        if b["fav"]:
            sc += 0.5
        sc -= min(1.5, 0.15 * b["plays"])
        if b["last_played"] and time.time() - b["last_played"] < 1800:
            sc -= 3.0
        return sc

    def pitch_next(self, cur_path, pool_paths=None):
        cur = self.data["items"].get(cur_path)
        if not cur:
            return
        pool = [self.data["items"][p] for p in (pool_paths or self.data["items"]) if p != cur_path and p in self.data["items"]]
        if not pool:
            return
        top = sorted(pool, key=lambda i: -self.similarity(cur, i))[:3]
        pick = random.choice(top)
        act = ("▶  Play it next", lambda p=pick["path"]: self.play(p))
        if self.brain.ready() and not self.brain_busy and self.data["brain"].get("auto", True):
            self.stella_ask(None, act=act, hint=f"[task] They're watching '{cur['title']}'. You found something they'd love next: '{pick['title']}' "
                                                f"({', '.join(pick['categories'])}; played {pick['plays']} times). Tease them with it in 1-2 sentences "
                                                "and say why it fits. Mention that exact title.")
        else:
            self.stella_say(f"Want to see '{pick['title']}' next? It has a similar vibe 😏", act=act)

    # ---------- Stella's behavior & eyes ----------
    def open_behavior(self):
        b, sg = self.data["brain"], self.data["sight"]
        win = tk.Toplevel(self.root)
        win.title("Stella's behavior & eyes 👀")
        win.geometry("580x700")
        win.configure(bg=PANEL)
        win.transient(self.root)

        def head(t):
            tk.Label(win, text=t, bg=PANEL, fg=ACCENT, font=("Segoe UI Black", 11)).pack(anchor="w", padx=16, pady=(12, 2))

        def check(txt, val):
            v = tk.BooleanVar(value=bool(val))
            tk.Checkbutton(win, text=txt, variable=v, bg=PANEL, fg=FG, selectcolor=CARD, activebackground=PANEL, activeforeground=ACCENT,
                           wraplength=530, justify="left").pack(anchor="w", padx=16, pady=2)
            return v

        def field(label, val):
            tk.Label(win, text=label, bg=PANEL, fg=FG).pack(anchor="w", padx=16, pady=(6, 0))
            e = tk.Entry(win, bg=CARD, fg=FG, insertbackground=ACCENT, bd=0, font=("Segoe UI", 10))
            e.insert(0, str(val))
            e.pack(fill="x", padx=16, ipady=4)
            return e
        head("How eager is she?")
        chat = ttk.Combobox(win, values=["Quiet", "Normal", "Eager"], state="readonly")
        chat.set(b.get("chat", "Eager"))
        chat.pack(anchor="w", padx=16)
        v_pop = check("Pop up her messages over other windows (like MPC-HC) when the launcher isn't in front", b.get("popups", True))
        v_tidy = check("Let her add tags and give messy file names a readable display name (only inside the launcher, never the real file)", b.get("autotidy", True))
        v_take = check("She writes a detailed comment (her take) under a video once you've watched most of it", b.get("autotake", True))
        v_along = check("Watch along: react to what's on screen every so often (needs a vision model)", b.get("along", False))
        head("Constant sight (invisible screenshots)")
        tk.Label(win, bg=PANEL, fg=MUTED, justify="left", wraplength=530, font=("Segoe UI", 9),
                 text="Needs a vision model. Works in the in-window player, and in MPC-HC while MPC-HC is the active window (only its video "
                      "area is captured, never the rest of your screen). Small files in the screens folder, deleted by the rules below.").pack(anchor="w", padx=16)
        v_sight = check("Take screenshots in the background so she can always see what's on screen", sg["on"])
        e_every = field("Take one every (seconds)", sg["every"])
        e_keep = field("Keep only the newest (screenshots)", sg["keep"])
        v_wc = check("Delete them when the video changes or stops", sg["wipe_change"])
        v_we = check("Delete them when the app closes", sg["wipe_exit"])

        def num(e, d, lo):
            try:
                return max(lo, int(e.get().strip()))
            except ValueError:
                return d

        def save():
            b.update(chat=chat.get() or "Eager", popups=v_pop.get(), autotidy=v_tidy.get(), autotake=v_take.get(), along=v_along.get())
            sg.update(on=v_sight.get(), every=num(e_every, 20, 5), keep=num(e_keep, 6, 1), wipe_change=v_wc.get(), wipe_exit=v_we.get())
            self.save()
            win.destroy()
            self.stella_say("Okay, I'll be exactly this naughty 😈" if b.get("chat") == "Eager" else "Noted. I'll behave... a little 💋")
        bar = tk.Frame(win, bg=PANEL)
        bar.pack(pady=16)
        tk.Button(bar, text="🧹 Clear screenshots now", command=self.clear_shots, bg=CARD, fg=FG, bd=0, padx=14, pady=6).pack(side="left", padx=6)
        tk.Button(bar, text="Save", command=save, bg=ACCENT, fg="white", bd=0, padx=24, pady=6, font=("Segoe UI", 10, "bold")).pack(side="left", padx=6)

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
        self.refresh_nav()
        self.render()
        if self.mode == "images":
            self.g_layout()

    def refresh_nav(self):
        items = (self.data.get("images", {}) if self.mode == "images" else self.data["items"]).values()
        self.nav_keys = ["all", "fav", "recent"]
        rows = [f"\U0001F48B  All the goodies  ({len(items)})",
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

    def on_nav(self, e):
        sel = self.nav.curselection()
        if sel:
            self.view = self.nav_keys[sel[0]]
            if self.mode == "watch":
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
            for i in list(self.data["items"].values()) + list(self.data.get("images", {}).values()):
                i["categories"] = [n if c == old else c for c in i["categories"]]
            if self.view == "cat:" + old:
                self.view = "cat:" + n
            self.save(); self.refresh()

    def delete_category(self, name):
        if messagebox.askyesno("Delete category", f"Delete '{name}'? Videos move to Uncategorized."):
            self.data["categories"].remove(name)
            for i in list(self.data["items"].values()) + list(self.data.get("images", {}).values()):
                i["categories"] = [c for c in i["categories"] if c != name] or ["Uncategorized"]
            if self.view == "cat:" + name:
                self.view = "all"
            self.save(); self.refresh()

    # ---------- player / drop / multi-category ----------
    PLAYER_LABELS = {"builtin": "In-window (VLC)", "mpc": "MPC-HC", "system": "System default"}

    def player_pref(self):
        p = self.data.get("player")
        if p in self.PLAYER_LABELS:
            return p
        if self.data.get("builtin") is False:  # older setting
            return "system"
        return "builtin" if vlc else ("mpc" if find_mpc() else "system")

    def set_player(self, key):
        if key == "builtin" and not vlc:
            messagebox.showinfo("In-window player",
                                "The in-window (YouTube-style) player needs VLC, and it wasn't found.\n\n"
                                f"Reason: {VLC_ERROR}\n\n"
                                "Fix: install VLC 64-bit from videolan.org (and run 'pip install python-vlc' if you start the .pyw directly). "
                                "The app built by the GitHub workflow bundles VLC itself.")
            return False
        if key == "mpc" and not self.mpc_path(ask=True):
            return False
        self.data["player"] = key
        self.save()
        self.update_pl_btn()
        return True

    def toggle_player(self):
        order = ["builtin", "mpc", "system"]
        cur = self.player_pref()
        for k in range(1, 4):
            if self.set_player(order[(order.index(cur) + k) % 3]):
                return

    def update_pl_btn(self):
        p = self.player_pref()
        txt = "\U0001F3AC Player: " + self.PLAYER_LABELS[p] + (" (VLC missing)" if p == "builtin" and not vlc else "")
        self.pl_btn.config(text=txt, fg=ACCENT if p != "system" else FG)

    def player_menu(self, e):
        m = tk.Menu(self.root, tearoff=0)
        cur = self.player_pref()
        for key, label in self.PLAYER_LABELS.items():
            m.add_command(label=("\u2714  " if cur == key else "      ") + label, command=lambda k=key: self.set_player(k))
        m.add_separator()
        fs = tk.BooleanVar(value=self.data.get("mpc_fullscreen", False))
        qr = tk.BooleanVar(value=self.data.get("mpc_queue", False))
        m.add_checkbutton(label="MPC-HC: start fullscreen", variable=fs, command=lambda: self.set_opt("mpc_fullscreen", fs.get()))
        m.add_checkbutton(label="MPC-HC: queue the rest of the list", variable=qr, command=lambda: self.set_opt("mpc_queue", qr.get()))
        m.add_command(label="Locate MPC-HC...", command=self.locate_mpc)
        m.tk_popup(e.x_root, e.y_root)

    def set_opt(self, key, val):
        self.data[key] = bool(val)
        self.save()

    def locate_mpc(self):
        f = filedialog.askopenfilename(title="Locate MPC-HC (mpc-hc64.exe)",
                                       filetypes=[("MPC-HC / MPC-BE", "mpc-hc*.exe mpc-be*.exe"), ("Programs", "*.exe")])
        if f:
            self.data["mpc_path"] = f
            self.save()

    def mpc_path(self, ask=False):
        p = self.data.get("mpc_path")
        if p and os.path.isfile(p):
            return p
        p = find_mpc()
        if p:
            self.data["mpc_path"] = p
            self.save()
            return p
        if ask:
            messagebox.showinfo("MPC-HC", "I couldn't find MPC-HC automatically.\nPick its .exe (usually in C:\\Program Files\\MPC-HC).")
            self.locate_mpc()
            p = self.data.get("mpc_path")
            return p if p and os.path.isfile(p) else None
        return None

    def write_queue(self, path):
        paths = [i["path"] for i in self.filtered()]
        if path in paths:
            k = paths.index(path)
            paths = paths[k:] + paths[:k]
        else:
            paths = [path]
        q = APP_DIR / "queue.m3u8"
        q.write_text("#EXTM3U\n" + "\n".join(paths[:500]) + "\n", encoding="utf-8")
        return str(q)

    def play_mpc(self, path, queue_rest=False, count=True):
        exe = self.mpc_path(ask=True)
        if not exe:
            return False
        target = self.write_queue(path) if (queue_rest or self.data.get("mpc_queue")) else path
        args = [exe, target, "/play"] + (["/fullscreen"] if self.data.get("mpc_fullscreen") else [])
        try:
            subprocess.Popen(args)
        except Exception as e:
            messagebox.showerror("MPC-HC", f"Couldn't start MPC-HC:\n{e}\n\nUsing your default player instead.")
            return False
        if count and path in self.data["items"]:
            self.mark_played(path)
        return True

    def open_external(self, path):
        if not self.mpc_path() or not self.play_mpc(path, count=False):
            os.startfile(path)

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
            icat = self.view[4:] if self.view.startswith("cat:") else self.pick_category()
            if icat:
                n = self.add_image_paths(imgs, icat)
                self.stella_event("images", n=n, cat=icat)
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
        self.now = ("video", path)
        if self.data.get("sight", {}).get("wipe_change", True):
            self.clear_shots()  # new video -> forget the old screenshots
        self.stella_play(it)
        self.root.after(2500, lambda p=path: self.autotidy(p))

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
        self.stella_event("added", n=added, d=dupes, cat=cat)
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
        tp = thumb_file(path)
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
        if self.mode == "watch":
            self.watchp.refresh_soon()
        lbl = self.tile_labels.get(path)
        if lbl and lbl.winfo_exists():
            try:
                self.photos[path] = ImageTk.PhotoImage(Image.open(tp))
                lbl.config(image=self.photos[path], text="")
            except Exception:
                pass

    # ---------- actions ----------
    def play(self, path, external=False, popout=False, mpc=False, queue_rest=False):
        if not os.path.exists(path):
            messagebox.showerror("File not found", f"This file is missing:\n{path}")
            return
        pref = "system" if external else ("mpc" if mpc else self.player_pref())
        if popout and vlc:
            pref = "builtin"
        if pref == "builtin" and not vlc:
            fb = "mpc" if self.mpc_path() else "system"
            if not getattr(self, "_vlc_warned", False):
                self._vlc_warned = True
                messagebox.showinfo("In-window player",
                                    "The in-window player needs VLC, which wasn't found, so I'll use "
                                    + ("MPC-HC" if fb == "mpc" else "your default video player") + " instead.\n\n"
                                    f"Reason: {VLC_ERROR}\n\nRight-click the 🎬 button in the top bar to choose a player.")
            pref = fb
        if pref == "mpc":
            if self.play_mpc(path, queue_rest=queue_rest):
                return
            pref = "system"
        if pref == "builtin":
            paths = [i["path"] for i in self.filtered()]
            if path not in paths:
                paths = [path]
            try:
                if popout:
                    Player(self, paths, paths.index(path))
                else:
                    self.watchp.open(paths, paths.index(path))
                return
            except Exception as e:
                try:
                    self.set_mode("videos")
                except Exception:
                    pass
                messagebox.showerror("In-window player", f"The in-window player failed:\n{e}\n\nUsing another player instead.")
                if self.mpc_path() and self.play_mpc(path):
                    return
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
        m.add_command(label="Pop-out player window", command=lambda: self.play(path, popout=True))
        m.add_command(label="Play in MPC-HC", command=lambda: self.play(path, mpc=True))
        m.add_command(label="Play in MPC-HC (queue the rest of this list)", command=lambda: self.play(path, mpc=True, queue_rest=True))
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

    def _on_error(exc, val, tb):
        import traceback
        try:
            with open(APP_DIR / "error.log", "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + "".join(traceback.format_exception(exc, val, tb)) + "\n")
        except Exception:
            pass
        if not getattr(_on_error, "shown", False):
            _on_error.shown = True
            messagebox.showerror("Something went wrong", f"{val}\n\nDetails were saved to:\n{APP_DIR / 'error.log'}")
    root.report_callback_exception = _on_error
    if App.check_pin(root):
        app = App(root)
        root.protocol("WM_DELETE_WINDOW", app.on_close)
        root.attributes("-alpha", 1)
        root.mainloop()
    else:
        root.destroy()
