#!/usr/bin/env python3
import re
import random
import shutil
import subprocess
import wave
import array
import json
from pathlib import Path
from datetime import datetime
import sys

# ==============================
# GLOBAL FLAGS
# ==============================

ENABLE_GLOBAL_TLK_DEDUP = True
ENABLE_NARRATION_STITCH = True

# Clean up any .D / .TRA files we decompile in GAME_DIR
CLEANUP_DLG_SOURCES = True

# If True: never touch lines that already have a soundref in dialog.tlk
# If False: treat everything as eligible for (re)voicing, even if TLK has soundrefs
RESPECT_EXISTING_VO = True

# ==============================
# STATIC CONFIG (GAME / TOOLS)
# ==============================

GAME_DIR = Path(r"/mnt/d/00PlaneScapeMod")

# Root for all generated Auto-VO mods (keeps GAME_DIR tidy)
AUTOVO_ROOT = GAME_DIR / "autovo"

# Base dir for all reference voices:
#   /home/administrator/voices/<something>_refs
REF_BASE_DIR = Path(r"/home/administrator/voices")

# Narrator voice stays fixed:
NARRATOR_REF_AUDIO_DIR = REF_BASE_DIR / "narrator_refs"

PROMPT_TEXT_FALLBACK = (
    "Hey, chief. You okay? You playing corpse or you putting the blinds on the Dusties? "
    "I thought you was a deader for sure."
)

VOXCMD = "voxcpm"

INFERENCE_STEPS = 15
USE_NORMALIZE = True
USE_DENOISE = True

CFG_MIN = 1.7
CFG_MAX = 1.7
BASELINE_CFG = 1.8
SEED_GROUP_SIZE = 20

WEIDU_EXE = GAME_DIR / "weidu.exe"
WEIDU_LANG = "en_us"
FORCE_REEXTRACT = False
FORCE_RETRAIFY_TLK = False
ASK_ON_EXISTING = True

# Tiny line-level fades to kill pops at boundaries (ms)
FADE_IN_MS = 10
FADE_OUT_MS = 10

# ==============================
# PHONETIC FIXES (TTS-ONLY)
# ==============================

PHONETIC_FIXES = [
    ("TOO", "too"),
    ("DEAD", "dead"),
    ("morte", "mort"),
    ("WHO", "who"),
    ("Pharod", "Fah-rod"),
    ("Ysgard", "izgard"),
    ("DOES", "does"),
    ("ye", "ya"),
    ("MOST", "most"),
]

# ==============================
# DLG-SPECIFIC GLOBALS (SET AT RUNTIME)
# ==============================

DLG_NAME: str | None = None
DLG_BASENAME: str | None = None
VOICE_PREFIX: str | None = None

REF_AUDIO_DIR: Path | None = None

MOD_ID: str | None = None
MOD_DIR: Path | None = None
SOUNDS_DIR: Path | None = None
INPUT_TXT: Path | None = None
TMP_OUT_DIR: Path | None = None
LOG_PATH: Path | None = None
TLK_TRA_FILE: Path | None = None

_SOUNDREF_CACHE: dict[int, str | None] = {}
_DECOMPILED_CREATED: set[str] = set()

# ==============================
# VIEWER SCRIPT TEMPLATE
# ==============================

VIEWER_SCRIPT_TEMPLATE = r'''#!/usr/bin/env python3
import json
import os
import sys
import pathlib
import platform
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

HERE = pathlib.Path(__file__).resolve().parent
META_PATH = HERE / "vo_lines.json"

def load_metadata():
    if not META_PATH.is_file():
        messagebox.showerror("Error", f"Metadata file not found: {META_PATH}")
        sys.exit(1)
    with META_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)

def play_wav(path):
    path = str(path)
    if not os.path.isfile(path):
        messagebox.showerror("Playback error", f"WAV not found: {path}")
        return
    system = platform.system()
    if system == "Windows":
        try:
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
        except Exception as e:
            messagebox.showerror("Playback error", str(e))
            return
    else:
        for cmd in (["aplay", path], ["paplay", path], ["ffplay", "-nodisp", "-autoexit", path]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except FileNotFoundError:
                continue
        messagebox.showerror("Playback error", "No suitable audio player found (tried aplay, paplay, ffplay).")

def main():
    try:
        data = load_metadata()
    except Exception as e:
        messagebox.showerror("Error", f"Failed to load metadata: {e}")
        return

    entries = data.get("entries", [])
    root = tk.Tk()
    root.title(f"Auto-VO Preview - {data.get('dlg_basename', '')}")

    main_frame = ttk.Frame(root, padding=10)
    main_frame.grid(row=0, column=0, sticky="nsew")
    root.rowconfigure(0, weight=1)
    root.columnconfigure(0, weight=1)

    list_frame = ttk.Frame(main_frame)
    list_frame.grid(row=0, column=0, sticky="nsew")
    main_frame.rowconfigure(0, weight=1)
    main_frame.columnconfigure(0, weight=1)

    listbox = tk.Listbox(list_frame, height=20, exportselection=False)
    scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
    listbox.configure(yscrollcommand=scrollbar.set)
    listbox.grid(row=0, column=0, sticky="nsew")
    scrollbar.grid(row=0, column=1, sticky="ns")
    list_frame.rowconfigure(0, weight=1)
    list_frame.columnconfigure(0, weight=1)

    detail = tk.Text(main_frame, width=80, height=8, wrap="word")
    detail.grid(row=1, column=0, sticky="ew", pady=(8, 0))

    btn_frame = ttk.Frame(main_frame)
    btn_frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))
    btn_frame.columnconfigure(0, weight=0)
    btn_frame.columnconfigure(1, weight=1)

    def populate():
        listbox.delete(0, tk.END)
        for idx, entry in enumerate(entries):
            txt = (entry.get("text") or "").replace("\n", " ")
            if len(txt) > 80:
                txt = txt[:77] + "..."
            label = f"{idx+1:03d} | {entry.get('resref')} | strref {entry.get('strref')} | {txt}"
            listbox.insert(tk.END, label)

    def on_select(event=None):
        sel = listbox.curselection()
        if not sel:
            return
        entry = entries[sel[0]]
        text = entry.get("text") or ""
        detail.delete("1.0", tk.END)
        detail.insert("1.0", text)

    def on_play():
        sel = listbox.curselection()
        if not sel:
            messagebox.showinfo("No selection", "Select a line to play.")
            return
        entry = entries[sel[0]]
        wav_rel = entry.get("wav")
        if not wav_rel:
            messagebox.showerror("Playback error", "No WAV path in metadata.")
            return
        wav_path = HERE / wav_rel
        play_wav(wav_path)

    listbox.bind("<<ListboxSelect>>", on_select)
    listbox.bind("<Double-Button-1>", lambda e: on_play())

    play_btn = ttk.Button(btn_frame, text="Play", command=on_play)
    play_btn.grid(row=0, column=0, sticky="w")

    populate()
    root.mainloop()

if __name__ == "__main__":
    main()
'''

# ==============================
# RUNTIME DLG SETUP
# ==============================

def setup_dlg(dlg_name_raw: str):
    """
    Initialize all DLG-specific globals from a user-provided DLG name.
    """
    global DLG_NAME, DLG_BASENAME, VOICE_PREFIX
    global REF_AUDIO_DIR, MOD_ID, MOD_DIR, SOUNDS_DIR, INPUT_TXT, TMP_OUT_DIR, LOG_PATH, TLK_TRA_FILE

    dlg = dlg_name_raw.strip()
    if not dlg:
        raise SystemExit("No DLG name provided; aborting.")

    dlg = dlg.upper()
    if dlg.endswith(".DLG"):
        dlg = dlg[:-4]

    DLG_NAME = dlg
    DLG_BASENAME = dlg

    # Infer voice prefix (DMORTE -> MORTE, else use full name)
    vp = DLG_BASENAME
    if vp.startswith("D") and len(vp) > 1 and vp[1].isalpha():
        vp = vp[1:]
    VOICE_PREFIX = vp

    # Voice ref dir selection
    dlg_folder = DLG_BASENAME.lower() + "_refs"
    voice_folder = VOICE_PREFIX.lower() + "_refs"

    dlg_path = REF_BASE_DIR / dlg_folder
    voice_path = REF_BASE_DIR / voice_folder

    candidates = [dlg_path]
    if voice_path != dlg_path:
        candidates.append(voice_path)

    chosen = None
    for p in candidates:
        if p.exists():
            chosen = p
            print(f"[DEBUG] Using existing voice ref dir: {p}")
            break
    if chosen is None:
        chosen = dlg_path
        print(f"[DEBUG] Voice ref dir does not exist yet, will use: {chosen}")
        print("        Create (wav,txt) seed pairs there before running VoxCPM.")

    REF_AUDIO_DIR = chosen

    # All generated content for this DLG lives under GAME_DIR/autovo/<mod_subdir>
    mod_subdir = f"autovo_{DLG_BASENAME.lower()}"
    MOD_ID = f"autovo/{mod_subdir}"
    MOD_DIR = AUTOVO_ROOT / mod_subdir
    SOUNDS_DIR = MOD_DIR / "sounds"
    INPUT_TXT = MOD_DIR / f"{mod_subdir}_input.txt"
    TMP_OUT_DIR = MOD_DIR / "tmp_batch"
    LOG_PATH = MOD_DIR / f"{mod_subdir}_run.log"
    TLK_TRA_FILE = MOD_DIR / "dialog_full.tra"

    print(f"[DEBUG] DLG_BASENAME={DLG_BASENAME}, VOICE_PREFIX={VOICE_PREFIX}")
    print(f"[DEBUG] MOD_ID={MOD_ID}")
    print(f"[DEBUG] MOD_DIR={MOD_DIR}")
    print(f"[DEBUG] SOUNDS_DIR={SOUNDS_DIR}")
    print(f"[DEBUG] TLK_TRA_FILE={TLK_TRA_FILE}")


# ==============================
# HELPERS
# ==============================

# Cache of all DLG resource names discovered via WeiDU --list-files
_DLG_RESOURCE_CACHE: list[str] | None = None


def list_all_dlg_resources_via_weidu() -> list[str]:
    """
    Use WeiDU --list-files to enumerate all resources known to CHITIN.KEY,
    then extract the .DLG resource names.

    This does NOT require any .DLG files to exist on disk; WeiDU reads
    from BIFFs, which is what we want for base + variant discovery.
    """
    global _DLG_RESOURCE_CACHE
    if _DLG_RESOURCE_CACHE is not None:
        return _DLG_RESOURCE_CACHE

    if not WEIDU_EXE:
        raise SystemExit("WEIDU_EXE not configured; cannot list DLG resources.")

    cmd = [
        str(WEIDU_EXE),
        "--list-files",
    ]

    print(f"[DEBUG] Running WeiDU to list all resources (for DLG discovery):")
    print("       " + " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(GAME_DIR),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise SystemExit(f"WeiDU not found at {WEIDU_EXE}")

    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0 and not out.strip():
        raise SystemExit(
            f"WeiDU --list-files failed with exit code {proc.returncode} "
            f"and no output; cannot discover DLG resources."
        )

    names: set[str] = set()
    for line in out.splitlines():
        # Be generous: grab any token ending in .DLG
        m = re.search(r"\b([A-Za-z0-9_]+)\.DLG\b", line, flags=re.IGNORECASE)
        if not m:
            continue
        resname = m.group(1).upper()
        names.add(resname)

    if not names:
        print("[WARN] WeiDU --list-files produced no .DLG entries; "
              "variant discovery will fall back to base name only.")

    _DLG_RESOURCE_CACHE = sorted(names)
    print(f"[DEBUG] WeiDU DLG resources discovered: {len(_DLG_RESOURCE_CACHE)}")
    return _DLG_RESOURCE_CACHE


def find_dlg_variants(base_name: str) -> list[str]:
    """
    Use WeiDU-discovered DLG resources to find base + variant dialog names.

    Example for base_name='DMORTE':
      DMORTE, DMORTE1, DMORTEN, ...

    Example for base_name='DILQUIX':
      DILQUIX, DILQUIXN, DILQUIXT, ...

    Rules:
      - resource name must start with base_name (uppercased)
      - suffix (after base) must be:
          "" (the base itself), or
          exactly 1 char of [A-Z0-9]
    """
    base = base_name.upper()
    all_dlg = list_all_dlg_resources_via_weidu()

    variants: set[str] = set()
    for name in all_dlg:
        if not name.startswith(base):
            continue
        suffix = name[len(base):]  # may be ""
        if not suffix:
            variants.add(name)
            continue
        # Only accept a single trailing letter/number as a variant
        if len(suffix) == 1 and re.fullmatch(r"[A-Z0-9]", suffix):
            variants.add(name)

    if not variants:
        variants = {base}

    variants_list = sorted(variants)
    print(f"[DEBUG] DLG variants detected for base '{base}': {', '.join(variants_list)}")
    return variants_list


def list_dlg_basenames():
    """
    Return a sorted list of all .DLG basenames found in GAME_DIR.
    """
    basenames = []
    for path in GAME_DIR.iterdir():
        if path.is_file() and path.suffix.lower() == ".dlg":
            basenames.append(path.stem.upper())
    basenames.sort()
    return basenames


def load_text(path: Path, encoding: str = "cp1252") -> str:
    return path.read_text(encoding=encoding, errors="replace")


def save_text(path: Path, text: str, encoding: str = "cp1252") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding, errors="replace")


def append_log(line: str):
    MOD_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def init_run_log():
    MOD_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("w", encoding="utf-8") as f:
        f.write(f"Auto-VO run for {DLG_BASENAME} at {datetime.now().isoformat()}\n")


def ensure_base_dialog_backup_and_restore():
    dialog_tlk = GAME_DIR / "lang" / WEIDU_LANG / "dialog.tlk"
    if not dialog_tlk.is_file():
        raise SystemExit(f"dialog.tlk not found at {dialog_tlk}")

    backup_dir = MOD_DIR / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_path = backup_dir / f"dialog_{timestamp}.tlk"
    shutil.copy2(dialog_tlk, snapshot_path)
    print(f"[DEBUG] Snapshot of current dialog.tlk saved to {snapshot_path}")

    base_backup = backup_dir / "dialog_base.tlk"
    if not base_backup.exists():
        shutil.copy2(dialog_tlk, base_backup)
        print(f"[DEBUG] Created baseline dialog.tlk backup at {base_backup}")
    else:
        shutil.copy2(base_backup, dialog_tlk)
        print(f"[DEBUG] Restored dialog.tlk from baseline backup {base_backup}")


def normalize_text_for_match(text: str) -> str:
    t = text.replace("\r\n", "\n")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def normalize_dashes_for_tts(text: str) -> str:
    """
    Normalize "floating" or dangling dashes to commas for TTS, while
    leaving true intra-word hyphens alone (e.g. 'meat-hook').

    Examples:
      "journal - though"  -> "journal, though"
      " -- "              -> ", "
    """
    # word <space> dash(es) <space> word  -> comma between words
    text = re.sub(r'(?<=\w)\s+[-–—]+\s+(?=\w)', ', ', text)

    # standalone dash tokens not touching word chars -> comma
    text = re.sub(r'(?<!\w)\s*[-–—]+\s*(?!\w)', ', ', text)

    return text


def apply_phonetic_fixes(text: str) -> str:
    result = text
    for src, repl in PHONETIC_FIXES:
        if not src:
            continue
        pattern = r"\b" + re.escape(src) + r"\b"
        result = re.sub(pattern, repl, result, flags=re.IGNORECASE)
    return result


def clean_for_tts(text: str) -> str:
    """
    Character TTS cleaner.
    """
    t = text.strip()

    quote_segments = re.findall(r'"([^"]+)"', t)
    if quote_segments:
        t = " ".join(quote_segments)
    else:
        if t.startswith('"') and t.endswith('"'):
            t = t[1:-1].strip()

    t = re.sub(r"\*(.*?)\*", r"\1", t)
    t = normalize_dashes_for_tts(t)
    t = t.replace("\r\n", " ").replace("\n", " ")
    t = re.sub(r"\s+", " ", t).strip()
    t = apply_phonetic_fixes(t)
    return t


def clean_segment_for_tts(text: str) -> str:
    """
    Segment cleaner for narration/character sub-spans in mixed or narrator-only lines.
    """
    t = text.strip()

    # Strip note prefixes like ^NNOTE:
    t = re.sub(r"^\^NNOTE:\s*", "", t)
    t = re.sub(r"^\^[A-Za-z0-9_\-]+:?\s*", "", t)

    # Strip engine tags like <BANDAGES2> etc.
    t = re.sub(r"<[^>]+>", "", t)

    t = re.sub(r"\*(.*?)\*", r"\1", t)
    t = normalize_dashes_for_tts(t)
    t = t.replace("\r\n", " ").replace("\n", " ")
    t = re.sub(r"\s+", " ", t).strip()

    if not t:
        return ""
    if re.fullmatch(r"[.\-–—…\s]+", t):
        return ""

    t = apply_phonetic_fixes(t)
    return t


def apply_fade_in_out(wav_path: Path, fade_in_ms: int = FADE_IN_MS, fade_out_ms: int = FADE_OUT_MS):
    if fade_in_ms <= 0 and fade_out_ms <= 0:
        return
    if not wav_path.is_file():
        return

    with wave.open(str(wav_path), "rb") as w:
        params = w.getparams()
        nchannels = params.nchannels
        sampwidth = params.sampwidth
        framerate = params.framerate
        nframes = params.nframes
        comptype = params.comptype
        compname = params.compname

        if sampwidth != 2 or comptype != "NONE":
            print(f"[DEBUG] Skipping fade on {wav_path.name}: unsupported format "
                  f"(sampwidth={sampwidth}, comptype={comptype})")
            return

        raw = w.readframes(nframes)

    samples = array.array("h")
    samples.frombytes(raw)

    total_frames = nframes
    if total_frames == 0:
        return

    fade_in_frames = int(framerate * fade_in_ms / 1000)
    fade_out_frames = int(framerate * fade_out_ms / 1000)

    half = total_frames // 2
    if fade_in_frames > half:
        fade_in_frames = half
    if fade_out_frames > half:
        fade_out_frames = half

    for i in range(fade_in_frames):
        factor = i / float(fade_in_frames) if fade_in_frames > 0 else 1.0
        for c in range(nchannels):
            idx = i * nchannels + c
            if idx >= len(samples):
                break
            samples[idx] = int(samples[idx] * factor)

    for i in range(fade_out_frames):
        factor = (fade_out_frames - i) / float(fade_out_frames) if fade_out_frames > 0 else 1.0
        frame_index = total_frames - fade_out_frames + i
        if frame_index < 0:
            continue
        for c in range(nchannels):
            idx = frame_index * nchannels + c
            if idx >= len(samples):
                break
            samples[idx] = int(samples[idx] * factor)

    with wave.open(str(wav_path), "wb") as out:
        out.setnchannels(nchannels)
        out.setsampwidth(sampwidth)
        out.setframerate(framerate)
        out.setcomptype(comptype, compname)
        out.writeframes(samples.tobytes())


# ==============================
# SEEDS
# ==============================

def load_seeds():
    if REF_AUDIO_DIR is None:
        raise SystemExit("REF_AUDIO_DIR not initialized (call setup_dlg first).")

    seeds = []

    if REF_AUDIO_DIR.is_file():
        seeds.append({
            "key": REF_AUDIO_DIR.stem,
            "wav": REF_AUDIO_DIR,
            "text": PROMPT_TEXT_FALLBACK,
        })
        print(f"[DEBUG] Seed bank: single file {REF_AUDIO_DIR}")
        return seeds

    if not REF_AUDIO_DIR.is_dir():
        raise SystemExit(f"REF_AUDIO_DIR not found or not a directory: {REF_AUDIO_DIR}")

    for wav in REF_AUDIO_DIR.iterdir():
        if not wav.is_file() or wav.suffix.lower() != ".wav":
            continue
        txt = wav.with_suffix(".txt")
        if not txt.is_file():
            continue
        transcript = txt.read_text(encoding="utf-8", errors="replace").strip()
        if not transcript:
            raise SystemExit(f"Transcript file is empty: {txt}")
        seeds.append({
            "key": wav.stem,
            "wav": wav,
            "text": transcript,
        })

    if not seeds:
        raise SystemExit(
            f"No (wav, txt) pairs found in REF_AUDIO_DIR: {REF_AUDIO_DIR}"
        )

    print(f"[DEBUG] Seed bank: {len(seeds)} seeds loaded from {REF_AUDIO_DIR}")
    return seeds


def pick_baseline_seed(seeds):
    baseline = min(seeds, key=lambda s: s["wav"].name.lower())
    print(f"[DEBUG] Baseline seed selected: {baseline['wav'].name} (key={baseline['key']})")
    return baseline


def load_narrator_seed():
    if not ENABLE_NARRATION_STITCH:
        return None

    if not NARRATOR_REF_AUDIO_DIR.exists():
        print(f"[DEBUG] Narrator refs dir {NARRATOR_REF_AUDIO_DIR} not found; narration stitching disabled.")
        return None

    if NARRATOR_REF_AUDIO_DIR.is_file():
        wav = NARRATOR_REF_AUDIO_DIR
        txt = wav.with_suffix(".txt")
        if txt.is_file():
            transcript = txt.read_text(encoding="utf-8", errors="replace").strip() or "Narrator voice reference."
        else:
            transcript = "Narrator voice reference."
        print(f"[DEBUG] Narrator seed (single file): {wav}")
        return {"wav": wav, "text": transcript}

    for wav in sorted(NARRATOR_REF_AUDIO_DIR.iterdir()):
        if not wav.is_file() or wav.suffix.lower() != ".wav":
            continue
        txt = wav.with_suffix(".txt")
        if not txt.is_file():
            continue
        transcript = txt.read_text(encoding="utf-8", errors="replace").strip() or "Narrator voice reference."
        print(f"[DEBUG] Narrator seed selected: {wav}")
        return {"wav": wav, "text": transcript}

    print(f"[DEBUG] No (wav, txt) pairs found in {NARRATOR_REF_AUDIO_DIR}; narration stitching disabled.")
    return None


# ==============================
# TLK SOUNDREF CHECK
# ==============================

def get_soundref_for_strref(strref: int) -> str | None:
    if strref in _SOUNDREF_CACHE:
        return _SOUNDREF_CACHE[strref]

    cmd = [
        str(WEIDU_EXE),
        "--use-lang", WEIDU_LANG,
        "--string", str(strref),
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(GAME_DIR),
        capture_output=True,
        text=True
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"~.*?~\s*\[([^\]]+)\]", out, re.DOTALL)
    if not m:
        _SOUNDREF_CACHE[strref] = None
        return None
    sound = m.group(1).strip()
    if not sound:
        sound = None
    _SOUNDREF_CACHE[strref] = sound
    return sound


# ==============================
# WEIDU: DLG / TRA
# ==============================

def ensure_dlg_decompiled_for(dlg_basename: str):
    """
    Ensure <basename>.D / <basename>.TRA exist.
    Track which basenames we actually decompiled so we can clean them.
    """
    d_path = GAME_DIR / f"{dlg_basename}.D"
    tra_path = GAME_DIR / f"{dlg_basename}.TRA"
    need = FORCE_REEXTRACT or not (d_path.is_file() and tra_path.is_file())
    if not need:
        print(f"[DEBUG] .D and .TRA already present for {dlg_basename}, skipping WeiDU DLG decompile.")
        return d_path, tra_path

    if not WEIDU_EXE:
        raise SystemExit("WEIDU_EXE not configured.")

    weidu_cmd = [
        str(WEIDU_EXE),
        "--trans",
        "--transref",
        "--use-lang", WEIDU_LANG,
        f"{dlg_basename}.DLG",
    ]

    print(f"[DEBUG] Running WeiDU to decompile {dlg_basename}.DLG:")
    print("       " + " ".join(weidu_cmd))

    try:
        subprocess.run(weidu_cmd, cwd=str(GAME_DIR), check=True)
    except FileNotFoundError:
        raise SystemExit(
            f"WeiDU not found: {WEIDU_EXE}"
        )
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"WeiDU failed with exit code {e.returncode} while processing {dlg_basename}.DLG")

    if not d_path.is_file() or not tra_path.is_file():
        raise SystemExit(f"WeiDU ran but {d_path} or {tra_path} is missing.")

    _DECOMPILED_CREATED.add(dlg_basename.upper())
    return d_path, tra_path


def cleanup_decompiled_sources():
    """
    Remove any .D / .TRA files we ourselves decompiled this run.
    """
    if not CLEANUP_DLG_SOURCES:
        return

    if not _DECOMPILED_CREATED:
        return

    for basename in sorted(_DECOMPILED_CREATED):
        for ext in (".D", ".TRA"):
            path = GAME_DIR / f"{basename}{ext}"
            if path.exists():
                try:
                    path.unlink()
                    print(f"[DEBUG] Cleaned decompiled {path.name}")
                except OSError as e:
                    print(f"[WARN] Failed to remove {path}: {e}")


def find_numbered_dlg_variants(base_name: str):
    """
    Return all DLG basenames that appear to be variants of base_name.

    Strategy (robust, not clever):
      - Normalize base_name to upper case.
      - Scan GAME_DIR for any files with extension .DLG / .D / .TRA.
      - If the stem (filename without extension) CONTAINS the base_name
        substring (case-insensitive), treat that stem as a variant.
      - Always include the exact base_name itself.
    """
    base = base_name.upper()
    variants = set()
    variants.add(base)

    debug_candidates = []

    for path in GAME_DIR.iterdir():
        if not path.is_file():
            continue

        ext = path.suffix.lower()
        if ext not in (".dlg", ".d", ".tra"):
            continue

        stem = path.stem.upper()
        debug_candidates.append(stem)

        if base in stem:
            variants.add(stem)

    variants_list = sorted(variants)
    print(f"[DEBUG] DLG variants detected for base '{base}': {', '.join(variants_list)}")

    # Optional extra debug: uncomment if you want to see all candidate stems we considered
    # print(f"[DEBUG] DLG candidate stems in GAME_DIR: {', '.join(sorted(set(debug_candidates)))}")

    return variants_list





# ==============================
# TLK TRAIFY / DEDUP
# ==============================

def ensure_tlk_traified():
    if TLK_TRA_FILE.is_file() and not FORCE_RETRAIFY_TLK:
        print(f"[DEBUG] Using existing TLK dump at {TLK_TRA_FILE}")
        return

    dialog_tlk_rel = f"lang\\{WEIDU_LANG}\\dialog.tlk"
    dialog_tlk_abs = GAME_DIR / "lang" / WEIDU_LANG / "dialog.tlk"
    if not dialog_tlk_abs.is_file():
        raise SystemExit(f"dialog.tlk not found at {dialog_tlk_abs}")

    if not WEIDU_EXE:
        raise SystemExit("WEIDU_EXE not configured for TLK traify.")

    # Write TLK dump into GAME_DIR/autovo/<mod_subdir>/dialog_full.tra
    out_rel = f"{MOD_ID}/dialog_full.tra"

    cmd = [
        str(WEIDU_EXE),
        "--traify-tlk", dialog_tlk_rel,
        "--out", out_rel,
    ]

    print(f"[DEBUG] Traifying TLK via WeiDU:")
    print("       " + " ".join(cmd))

    try:
        subprocess.run(cmd, cwd=str(GAME_DIR), check=True)
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"WeiDU --traify-tlk failed with exit code {e.returncode}")

    if not TLK_TRA_FILE.is_file():
        raise SystemExit(f"Expected TLK dump at {TLK_TRA_FILE} but it was not created.")

    print(f"[DEBUG] TLK dump written to {TLK_TRA_FILE}")


def parse_tlk_tra(tlk_tra_path: Path):
    content = load_text(tlk_tra_path)
    pattern = re.compile(
        r"@(\d+)\s*=\s*~(.*?)~",
        re.DOTALL,
    )
    strref_to_text = {}
    textkey_to_strrefs = {}

    for m in pattern.finditer(content):
        strref = int(m.group(1))
        text = m.group(2)
        strref_to_text[strref] = text
        key = normalize_text_for_match(text)
        textkey_to_strrefs.setdefault(key, []).append(strref)

    print(f"[DEBUG] parse_tlk_tra: parsed {len(strref_to_text)} TLK entries from dialog_full.tra")
    return strref_to_text, textkey_to_strrefs


def expand_duplicates(voiced_lines, strref_to_text, textkey_to_strrefs):
    if not voiced_lines:
        return voiced_lines

    by_strref = {line["strref"]: line for line in voiced_lines}
    extra = []

    for line in list(voiced_lines):
        base_strref = line["strref"]
        base_text = strref_to_text.get(base_strref)
        if not base_text:
            continue

        key = normalize_text_for_match(base_text)
        dup_refs = textkey_to_strrefs.get(key, [])
        if not dup_refs:
            continue

        for sr in dup_refs:
            if sr in by_strref:
                continue

            soundref = get_soundref_for_strref(sr)
            if soundref:
                print(f"[DEBUG] Duplicate strref {sr} already has sound [{soundref}], skipping duplicate-VO")
                continue

            dup_text = strref_to_text.get(sr, line["text"])
            clone = {
                "tra_id": None,
                "strref": sr,
                "text": dup_text,
                "tts_text": line["tts_text"],
                "resref": line["resref"],
            }
            extra.append(clone)
            by_strref[sr] = clone

    if extra:
        print(f"[DEBUG] Global TLK dedup: added {len(extra)} duplicate strref(s) via text match.")
    return voiced_lines + extra


# ==============================
# PARSE .D / .TRA INTO LINES
# ==============================

def find_dlg_variants_from_d_source(base_name: str, main_d_path: Path):
    """
    Inspect the decompiled .D source for the base dialog and infer any
    sibling DLG names that look like variants with letter/number suffixes.

    Example:
      base_name = "DILQUIX"
      main_d_path contains tokens DILQUIX, DILQUIXN, DILQUIXT
      -> we return ["DILQUIX", "DILQUIXN", "DILQUIXT"]

    Strategy:
      - Tokenize all ALL-CAPS identifiers in the .D file.
      - Keep tokens that start with the base_name in upper-case.
      - Accept short (1–3 char) [A–Z0–9] suffixes as “variant” names.
    """
    base = base_name.upper()
    text = load_text(main_d_path)
    upper = text.upper()

    variants = set()
    variants.add(base)

    # All-caps-ish tokens from the .D script
    for m in re.finditer(r"\b([A-Z][A-Z0-9_]*)\b", upper):
        token = m.group(1)
        if not token.startswith(base):
            continue
        if token == base:
            continue

        suffix = token[len(base):]
        # We only consider short, simple suffixes like "N", "T", "2", "A1", etc.
        if not suffix:
            continue
        if not (1 <= len(suffix) <= 3):
            continue
        if not re.fullmatch(r"[A-Z0-9]+", suffix):
            continue

        variants.add(token)

    variants_list = sorted(variants)
    print(f"[DEBUG] DLG variants detected from {main_d_path.name}: {', '.join(variants_list)}")
    return variants_list


def parse_dlg_d(d_path: Path):
    content = load_text(d_path)
    pattern = re.compile(r"\bSAY\s+@(\d+)", re.IGNORECASE)
    say_ids = {int(m.group(1)) for m in pattern.finditer(content)}
    print(f"[DEBUG] parse_dlg_d: found {len(say_ids)} SAY @N references in {d_path.name}")
    return say_ids


def parse_tra(tra_path: Path):
    content = load_text(tra_path)
    pattern = re.compile(
        r"@(\d+)\s*=\s*#(\d+)\s*/\*\s*~(.*?)~.*?\*/",
        re.DOTALL,
    )
    tra_map = {}
    for m in pattern.finditer(content):
        tra_id = int(m.group(1))
        strref = int(m.group(2))
        text = m.group(3)
        tra_map[tra_id] = {"tra_id": tra_id, "text": text, "strref": strref}
    print(f"[DEBUG] parse_tra: parsed {len(tra_map)} @N entries from {tra_path.name}")
    return tra_map


def build_resref(strref: int) -> str:
    base = VOICE_PREFIX.upper()
    if len(base) < 2:
        base = (base + "XX")[:2]
    else:
        base = base[:2]
    return f"{base}{strref:06d}"


def build_lines(d_path: Path, tra_path: Path):
    say_ids = parse_dlg_d(d_path)
    tra_map = parse_tra(tra_path)

    lines = []
    missing_tra = 0
    skipped_original_vo = 0

    for tra_id in sorted(say_ids):
        entry = tra_map.get(tra_id)
        if not entry:
            missing_tra += 1
            continue

        strref = entry["strref"]

        baseline_sound = get_soundref_for_strref(strref)
        if baseline_sound:
            if RESPECT_EXISTING_VO:
                skipped_original_vo += 1
                if skipped_original_vo <= 10:
                    print(
                        f"[DEBUG] Skipping strref {strref} in {d_path.name}: "
                        f"existing soundref [{baseline_sound}]"
                    )
                continue
            else:
                print(
                    f"[DEBUG] Overriding existing VO for strref {strref} in {d_path.name}: "
                    f"existing soundref [{baseline_sound}]"
                )

        text = entry["text"]
        tts_text = clean_for_tts(text)
        # Skip obvious junk/sentinel nodes
        norm = normalize_text_for_match(tts_text).upper()
        if norm == "NULL NODE":
            print(f"[DEBUG] Skipping sentinel TLK text for strref {strref}: {tts_text!r}")
            continue
        resref = build_resref(strref)

        lines.append(
            {
                "tra_id": tra_id,
                "strref": strref,
                "text": text,
                "tts_text": tts_text,
                "resref": resref,
            }
        )

    print(f"[DEBUG] build_lines: {len(lines)} lines with SAY @N + strref from {d_path.name}")
    if missing_tra:
        print(f"[WARN] Missing {missing_tra} @N entries in {tra_path.name} for SAY @N references")
    if skipped_original_vo and RESPECT_EXISTING_VO:
        print(f"[DEBUG] Skipped {skipped_original_vo} line(s) in {d_path.name} that already had baseline VO.")
    return lines


# ==============================
# INTERACTIVE REGEN
# ==============================

def describe_line(line):
    return f"[strref {line['strref']}] {line['text']}"


def plan_generation(lines, seeds):
    keep_lines = []
    regen_lines = []
    global_keep_all_existing = False

    if not SOUNDS_DIR.exists():
        print("[DEBUG] No sounds dir yet, all lines marked for generation.")
        regen_lines.extend(lines)
        return keep_lines, regen_lines, global_keep_all_existing

    skip_interactive_existing = False
    asked_global_choice = False

    for line in lines:
        wav_path = SOUNDS_DIR / f"{line['resref']}.wav"
        if wav_path.is_file():
            if not ASK_ON_EXISTING:
                keep_lines.append(line)
                continue

            if not asked_global_choice:
                print("\nExisting audio found for:")
                print(describe_line(line))
                ans = input("Keep all existing audio and skip per-line prompts? [Y/n]: ").strip().lower()
                asked_global_choice = True
                if ans in ("", "y", "yes"):
                    skip_interactive_existing = True
                    global_keep_all_existing = True
                    print("[DEBUG] Global choice: keep all existing audio, no per-line questions.")
                else:
                    skip_interactive_existing = False
                    print("[DEBUG] Global choice: interactive prompts for existing audio.")

            if skip_interactive_existing:
                keep_lines.append(line)
            else:
                ans = input("Keep this clip? [Y=keep / n=regenerate / s=skip line]: ").strip().lower()
                if ans in ("", "y", "yes"):
                    keep_lines.append(line)
                elif ans == "s":
                    pass
                else:
                    regen_lines.append(line)
        else:
            regen_lines.append(line)

    return keep_lines, regen_lines, global_keep_all_existing


def targeted_regen_by_word(lines, keep_lines, regen_lines):
    if not lines:
        return

    while True:
        s = input(
            "\nRegenerate all lines whose text contains a word/substring? "
            "Enter text (blank to continue): "
        ).strip()
        if not s:
            return

        needle = s.lower()
        matched = []
        for line in lines:
            if needle in line["text"].lower():
                if line not in regen_lines:
                    regen_lines.append(line)
                if line in keep_lines:
                    keep_lines.remove(line)
                matched.append(line)

        print(f"[DEBUG] Marked {len(matched)} line(s) for regeneration containing '{s}'.")
        if matched:
            print("[DEBUG] Matched strrefs:")
            for line in matched:
                snippet = normalize_text_for_match(line["text"])
                if len(snippet) > 80:
                    snippet = snippet[:77] + "..."
                print(f"  - {line['strref']}: {snippet}")

            cfg_override = None
            steps_override = None

            cfg_in = input(
                "\nStatic CFG value for these matched lines? "
                "(blank = use normal random CFG range): "
            ).strip()
            if cfg_in:
                try:
                    cfg_override = float(cfg_in)
                except ValueError:
                    print("[WARN] Invalid CFG value, ignoring override.")

            steps_in = input(
                "Static inference steps for these matched lines? "
                f"(blank = use global default {INFERENCE_STEPS}): "
            ).strip()
            if steps_in:
                try:
                    steps_override = int(steps_in)
                except ValueError:
                    print("[WARN] Invalid steps value, ignoring override.")

            if cfg_override is not None or steps_override is not None:
                for line in matched:
                    if cfg_override is not None:
                        line["cfg_override"] = cfg_override
                    if steps_override is not None:
                        line["steps_override"] = steps_override
                print("[DEBUG] Applied per-line CFG/steps overrides to matched lines.")

        while True:
            ans = input(
                "Proceed with current matches, or search another word/phrase? "
                "[P=proceed / S=search again]: "
            ).strip().lower()
            if ans in ("", "p", "y", "yes"):
                return
            if ans in ("s", "n", "no"):
                break
            print("Please enter 'P' to proceed or 'S' to search again.")


# ==============================
# VOXCPM BATCH / SINGLE
# ==============================

def build_chunks_for_regen(regen_lines, seeds):
    if not regen_lines:
        return {}

    keys = [s["key"] for s in seeds]
    if not keys:
        raise SystemExit("No seeds loaded.")

    chunks = {}
    for idx, line in enumerate(regen_lines):
        group_index = idx // SEED_GROUP_SIZE
        seed_key = keys[group_index % len(keys)]
        line["seed_key"] = seed_key

        cfg_value = line.get("cfg_override")
        if cfg_value is None:
            cfg_value = random.uniform(CFG_MIN, CFG_MAX)

        steps_value = line.get("steps_override")
        if steps_value is None:
            steps_value = INFERENCE_STEPS

        chunk_key = (seed_key, cfg_value, steps_value)
        if chunk_key not in chunks:
            chunks[chunk_key] = []
        chunks[chunk_key].append(line)

    return chunks


def run_voxcpm_batch(chunks, seeds_by_key):
    TMP_OUT_DIR.mkdir(parents=True, exist_ok=True)

    for (seed_key, cfg_value, steps_value), chunk in chunks.items():
        for old in TMP_OUT_DIR.glob("*.wav"):
            try:
                old.unlink()
            except FileNotFoundError:
                pass

        print(f"[DEBUG] Running VoxCPM batch for seed '{seed_key}' with cfg={cfg_value:.3f}, "
              f"steps={steps_value} on {len(chunk)} line(s).")

        seed = seeds_by_key.get(seed_key)
        if not seed:
            raise SystemExit(f"No seed data found for key '{seed_key}'")

        prompt_audio = seed["wav"]
        prompt_text = seed["text"]

        lines_text = [line["tts_text"] for line in chunk]
        text = "\n".join(lines_text)
        save_text(INPUT_TXT, text, encoding="utf-8")

        cmd = [
            VOXCMD,
            "--input", str(INPUT_TXT),
            "--output-dir", str(TMP_OUT_DIR),
            "--prompt-audio", str(prompt_audio),
            "--prompt-text", prompt_text,
            "--cfg-value", f"{cfg_value:.3f}",
            "--inference-timesteps", str(steps_value),
        ]
        if USE_NORMALIZE:
            cmd.append("--normalize")
        if USE_DENOISE:
            cmd.append("--denoise")

        print(f"[DEBUG] VoxCPM: {' '.join(cmd)}")
        append_log(f"[VOXCPM] chunk seed={seed_key}, cfg={cfg_value:.3f}, "
                   f"steps={steps_value}, lines={len(chunk)}")

        subprocess.run(cmd, check=True)

        wavs = sorted(TMP_OUT_DIR.glob("*.wav"))
        if len(wavs) != len(chunk):
            raise SystemExit(
                f"VoxCPM batch output mismatch for seed '{seed_key}' "
                f"(cfg={cfg_value:.3f}, steps={steps_value}): "
                f"expected {len(chunk)} wavs, got {len(wavs)}"
            )

        for src, line in zip(wavs, chunk):
            target = SOUNDS_DIR / f"{line['resref']}.wav"
            target.parent.mkdir(parents=True, exist_ok=True)
            src.replace(target)
            apply_fade_in_out(target)
            append_log(f"[GEN] {target.name} <- seed={seed_key}, cfg={cfg_value:.3f}, "
                       f"steps={steps_value}, strref={line['strref']}")
        print(f"[DEBUG]     Wrote {len(chunk)} wav(s) into {SOUNDS_DIR} for seed '{seed_key}'")


def run_voxcpm_single(text: str, seed: dict, out_path: Path, cfg_value: float | None = None):
    if cfg_value is None:
        cfg_value = BASELINE_CFG

    TMP_OUT_DIR.mkdir(parents=True, exist_ok=True)

    prompt_audio = seed["wav"]
    prompt_text = seed["text"]

    cmd = [
        VOXCMD,
        "--text", text,
        "--output", str(out_path),
        "--prompt-audio", str(prompt_audio),
        "--prompt-text", prompt_text,
        "--cfg-value", f"{cfg_value:.3f}",
        "--inference-timesteps", str(INFERENCE_STEPS),
    ]
    if USE_NORMALIZE:
        cmd.append("--normalize")
    if USE_DENOISE:
        cmd.append("--denoise")

    print(f"[DEBUG] VoxCPM single: {' '.join(cmd)}")
    append_log(f"[VOXCPM_SINGLE] {out_path.name} <- seed={prompt_audio}, cfg={cfg_value:.3f}")
    subprocess.run(cmd, check=True)


def synthesize_baseline(lines, baseline_seed, seeds_by_key):
    if not lines:
        return
    seed_key = baseline_seed["key"]
    chunks = {(seed_key, BASELINE_CFG, INFERENCE_STEPS): list(lines)}
    run_voxcpm_batch(chunks, seeds_by_key)


def synthesize_lines_batch(regen_lines, seeds, seeds_by_key):
    chunks = build_chunks_for_regen(regen_lines, seeds)
    if not chunks:
        return
    run_voxcpm_batch(chunks, seeds_by_key)


# ==============================
# NARRATION SPLIT / CLASSIFY
# ==============================

def split_narrator_and_dialog(full_text: str):
    """
    Split a TLK line into segments tagged as "character" or "narrator"
    using quote state.
    """
    segments = []
    current = []
    in_quote = False

    for ch in full_text:
        if ch == '"':
            seg_text = "".join(current)
            if seg_text.strip():
                role = "character" if in_quote else "narrator"
                segments.append((role, seg_text))
            current = []
            in_quote = not in_quote
        else:
            current.append(ch)

    if current:
        seg_text = "".join(current)
        if seg_text.strip():
            role = "character" if in_quote else "narrator"
            segments.append((role, seg_text))

    return segments


def classify_narrator_only_lines(regen_lines):
    """
    Split regen_lines into:
      - narr_only: lines with narrator text and NO character quotes.
      - char_only: lines with character quotes and no narrator text, or lines with no quotes.
      - mixed: lines that contain both narrator and character segments.
    """
    narr_only = []
    char_only = []
    mixed = []

    for line in regen_lines:
        segments = split_narrator_and_dialog(line["text"])
        if not segments:
            # No quotes detected; treat as character-only by default.
            char_only.append(line)
            continue

        has_narr = any(role == "narrator" and seg.strip() for role, seg in segments)
        has_char = any(role == "character" and seg.strip() for role, seg in segments)

        if has_narr and not has_char:
            narr_only.append(line)
        elif has_char and not has_narr:
            char_only.append(line)
        elif has_narr and has_char:
            mixed.append(line)
        else:
            char_only.append(line)

    print(
        f"[DEBUG] Narrator-only regen lines: {len(narr_only)}; "
        f"character-only regen lines: {len(char_only)}; "
        f"mixed narrator/character regen lines: {len(mixed)}"
    )
    return narr_only, char_only, mixed


def concat_wavs(wav_paths, out_path: Path):
    if not wav_paths:
        return

    with wave.open(str(wav_paths[0]), "rb") as w0:
        params0 = w0.getparams()
        base_fmt = (params0.nchannels, params0.sampwidth, params0.framerate,
                    params0.comptype, params0.compname)
        frames = [w0.readframes(w0.getnframes())]

    for p in wav_paths[1:]:
        with wave.open(str(p), "rb") as w:
            params = w.getparams()
            fmt = (params.nchannels, params.sampwidth, params.framerate,
                   params.comptype, params.compname)
            if fmt != base_fmt:
                raise SystemExit(
                    f"WAV format mismatch when stitching narration for {out_path} "
                    f"(got {fmt}, expected {base_fmt})"
                )
            frames.append(w.readframes(w.getnframes()))

    with wave.open(str(out_path), "wb") as out:
        out.setparams(params0)
        for fr in frames:
            out.writeframes(fr)

    apply_fade_in_out(out_path)


def prepare_narration_tasks(lines_to_rebuild):
    tasks = []
    for line in lines_to_rebuild:
        text = line["text"]
        segments = split_narrator_and_dialog(text)
        if not segments:
            continue

        has_narr = any(role == "narrator" and seg.strip() for role, seg in segments)
        has_char = any(role == "character" and seg.strip() for role, seg in segments)
        if not (has_narr and has_char):
            continue

        seg_order = 0
        for role, seg_text in segments:
            cleaned = clean_segment_for_tts(seg_text)
            if not cleaned:
                continue
            tmp_name = f"stitch_{line['strref']}_{seg_order:02d}_{role[0]}.wav"
            tasks.append({
                "role": role,
                "line": line,
                "seg_order": seg_order,
                "text": cleaned,
                "tmp_name": tmp_name,
                "wav_path": None,
            })
            seg_order += 1

    return tasks


def run_voxcpm_segments_batch(tasks, seed: dict, role_label: str):
    if not tasks:
        return

    role_dir = TMP_OUT_DIR / role_label
    role_dir.mkdir(parents=True, exist_ok=True)

    for old in role_dir.glob("*.wav"):
        try:
            old.unlink()
        except FileNotFoundError:
            pass

    texts = [t["text"] for t in tasks]
    text = "\n".join(texts)
    save_text(INPUT_TXT, text, encoding="utf-8")

    cfg_value = BASELINE_CFG
    steps_value = INFERENCE_STEPS

    prompt_audio = seed["wav"]
    prompt_text = seed["text"]

    cmd = [
        VOXCMD,
        "--input", str(INPUT_TXT),
        "--output-dir", str(role_dir),
        "--prompt-audio", str(prompt_audio),
        "--prompt-text", prompt_text,
        "--cfg-value", f"{cfg_value:.3f}",
        "--inference-timesteps", str(steps_value),
    ]
    if USE_NORMALIZE:
        cmd.append("--normalize")
    if USE_DENOISE:
        cmd.append("--denoise")

    print(f"[DEBUG] VoxCPM segments batch ({role_label}): {' '.join(cmd)}")
    append_log(f"[VOXCPM_STITCH_{role_label.upper()}] segments={len(tasks)}, "
               f"cfg={cfg_value:.3f}, steps={steps_value}")
    subprocess.run(cmd, check=True)

    wavs = sorted(role_dir.glob("*.wav"))
    if len(wavs) != len(tasks):
        raise SystemExit(
            f"VoxCPM segments batch mismatch for role '{role_label}': "
            f"expected {len(tasks)} wavs, got {len(wavs)}"
        )

    for src, task in zip(wavs, tasks):
        tmp_path = role_dir / task["tmp_name"]
        src.replace(tmp_path)
        task["wav_path"] = tmp_path


def synthesize_narrator_only_lines(narr_lines, narrator_seed):
    """
    Generate pure narrator-only lines (no quoted speech) entirely
    with the narrator voice.
    """
    if not narr_lines or narrator_seed is None:
        return

    nar_dir = TMP_OUT_DIR / "narrator_only"
    nar_dir.mkdir(parents=True, exist_ok=True)

    for old in nar_dir.glob("*.wav"):
        try:
            old.unlink()
        except FileNotFoundError:
            pass

    pairs = []
    texts = []
    for line in narr_lines:
        cleaned = clean_segment_for_tts(line["text"])
        if not cleaned:
            continue
        pairs.append(line)
        texts.append(cleaned)

    if not texts:
        return

    text_blob = "\n".join(texts)
    save_text(INPUT_TXT, text_blob, encoding="utf-8")

    cfg_value = BASELINE_CFG
    steps_value = INFERENCE_STEPS

    prompt_audio = narrator_seed["wav"]
    prompt_text = narrator_seed["text"]

    cmd = [
        VOXCMD,
        "--input", str(INPUT_TXT),
        "--output-dir", str(nar_dir),
        "--prompt-audio", str(prompt_audio),
        "--prompt-text", prompt_text,
        "--cfg-value", f"{cfg_value:.3f}",
        "--inference-timesteps", str(steps_value),
    ]
    if USE_NORMALIZE:
        cmd.append("--normalize")
    if USE_DENOISE:
        cmd.append("--denoise")

    print(f"[DEBUG] VoxCPM narrator-only batch: {' '.join(cmd)}")
    append_log(f"[VOXCPM_NARRATOR_ONLY] lines={len(pairs)}, cfg={cfg_value:.3f}, steps={steps_value}")
    subprocess.run(cmd, check=True)

    wavs = sorted(nar_dir.glob("*.wav"))
    if len(wavs) != len(pairs):
        raise SystemExit(
            f"VoxCPM narrator-only mismatch: expected {len(pairs)} wavs, got {len(wavs)}"
        )

    for src, line in zip(wavs, pairs):
        target = SOUNDS_DIR / f"{line['resref']}.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        src.replace(target)
        apply_fade_in_out(target)
        append_log(f"[GEN_NARRATOR_ONLY] {target.name} <- cfg={cfg_value:.3f}, "
                   f"steps={steps_value}, strref={line['strref']}")
    print(f"[DEBUG] Narrator-only generation wrote {len(pairs)} wav(s).")


def stitch_narration(lines_to_rebuild, narrator_seed, char_seed):
    if not ENABLE_NARRATION_STITCH or narrator_seed is None or char_seed is None:
        return
    if not lines_to_rebuild:
        return

    tasks = prepare_narration_tasks(lines_to_rebuild)
    if not tasks:
        print("[DEBUG] Narration stitching: no mixed narrator/character lines found.")
        return

    char_tasks = [t for t in tasks if t["role"] == "character"]
    narr_tasks = [t for t in tasks if t["role"] == "narrator"]

    run_voxcpm_segments_batch(char_tasks, char_seed, "character")
    run_voxcpm_segments_batch(narr_tasks, narrator_seed, "narrator")

    tasks_by_line = {}
    for t in tasks:
        if t["wav_path"] is None:
            continue
        line = t["line"]
        sr = line["strref"]
        tasks_by_line.setdefault(sr, []).append(t)

    count = 0
    stitched_strrefs = []

    for line in lines_to_rebuild:
        sr = line["strref"]
        segs = tasks_by_line.get(sr)
        if not segs:
            continue
        segs_sorted = sorted(segs, key=lambda x: x["seg_order"])
        wav_paths = [t["wav_path"] for t in segs_sorted]
        out_wav = SOUNDS_DIR / f"{line['resref']}.wav"
        concat_wavs(wav_paths, out_wav)
        count += 1
        stitched_strrefs.append(sr)

    for t in tasks:
        p = t.get("wav_path")
        if p and p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    print(f"[DEBUG] Narration stitching: rebuilt {count} line(s) with mixed narrator/character audio.")
    if stitched_strrefs:
        joined = ", ".join(str(s) for s in stitched_strrefs)
        print(f"[DEBUG] Narration-stitched strrefs: {joined}")


# ==============================
# TP2 + VIEWER METADATA/HELPER
# ==============================

def write_tp2(voiced_lines):
    MOD_DIR.mkdir(parents=True, exist_ok=True)
    (MOD_DIR / "backup").mkdir(parents=True, exist_ok=True)

    tp2_path = MOD_DIR / f"setup-autovo_{DLG_BASENAME.lower()}.tp2"

    with tp2_path.open("w", encoding="utf-8") as f:
        f.write(f'BACKUP ~{MOD_ID}/backup~\n')
        f.write('AUTHOR ~Auto-VO pipeline (VoxCPM CLI batch)~\n\n')
        f.write(f'BEGIN ~Auto-VO for {DLG_BASENAME} (VoxCPM)~\n\n')

        seen_resrefs = set()
        for line in voiced_lines:
            resref = line["resref"]
            if resref in seen_resrefs:
                continue
            seen_resrefs.add(resref)
            fname = f"{resref}.wav"
            f.write(f'COPY ~{MOD_ID}/sounds/{fname}~ ~override/{fname}~\n')
        f.write("\n")

        seen_pairs = set()
        for line in voiced_lines:
            strref = line["strref"]
            resref = line["resref"]
            pair = (strref, resref)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            text = line["text"].replace("\r\n", "\n")
            safe_text = text.replace("~", "`")

            f.write(f'STRING_SET {strref} ~{safe_text}~ [{resref}]\n')

    print(f"Wrote TP2: {tp2_path}")


def write_viewer_metadata(voiced_lines):
    """
    Write vo_lines.json with a simple list of entries:
      { strref, resref, text, wav (relative path from MOD_DIR) }
    """
    MOD_DIR.mkdir(parents=True, exist_ok=True)
    meta_path = MOD_DIR / "vo_lines.json"

    entries = []
    seen_resrefs = set()
    for line in voiced_lines:
        resref = line["resref"]
        if resref in seen_resrefs:
            continue
        seen_resrefs.add(resref)
        wav_rel = f"sounds/{resref}.wav"
        entries.append({
            "strref": line["strref"],
            "resref": resref,
            "text": line["text"],
            "wav": wav_rel,
        })

    entries.sort(key=lambda e: e["strref"])

    data = {
        "dlg_basename": DLG_BASENAME,
        "mod_id": MOD_ID,
        "entries": entries,
    }

    meta_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[DEBUG] Wrote viewer metadata: {meta_path}")


def write_viewer_script():
    """
    Drop a small Tk-based preview helper script in the mod dir:
      vo_preview.py
    """
    MOD_DIR.mkdir(parents=True, exist_ok=True)
    script_path = MOD_DIR / "vo_preview.py"
    script_path.write_text(VIEWER_SCRIPT_TEMPLATE, encoding="utf-8")
    try:
        mode = script_path.stat().st_mode
        script_path.chmod(mode | 0o111)
    except OSError:
        pass
    print(f"[DEBUG] Wrote viewer helper script: {script_path}")


# ==============================
# CORE PIPELINE (REUSABLE)
# ==============================

def run_for_dlg(dlg_input: str):
    if not dlg_input:
        raise SystemExit("No DLG name provided; aborting.")
    setup_dlg(dlg_input)

    init_run_log()
    ensure_base_dialog_backup_and_restore()

    if ENABLE_GLOBAL_TLK_DEDUP:
        ensure_tlk_traified()
        strref_to_text, textkey_to_strrefs = parse_tlk_tra(TLK_TRA_FILE)
    else:
        strref_to_text, textkey_to_strrefs = None, None

    # First, ensure the main DLG is decompiled so we can inspect its .D source.
    base_d_path, base_tra_path = ensure_dlg_decompiled_for(DLG_BASENAME)

    # Discover all base+variant dialogs using WeiDU's resource listing.
    dlg_variants = find_dlg_variants(DLG_BASENAME)

    all_lines = []
    for basename in dlg_variants:
        # This invokes WeiDU on <basename>.DLG (resource from BIFF),
        # emitting <basename>.D / <basename>.TRA into GAME_DIR if needed.
        d_path, tra_path = ensure_dlg_decompiled_for(basename)
        variant_lines = build_lines(d_path, tra_path)
        if not variant_lines:
            continue
        all_lines.extend(variant_lines)

    if not all_lines:
        cleanup_decompiled_sources()
        raise SystemExit("No SAY @N lines with strrefs (after filtering) found in any DLG variant.")

    lines = []
    seen_keys = set()
    for line in all_lines:
        key = (line["strref"], line["resref"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        lines.append(line)

    if len(dlg_variants) == 1:
        print(f"Found {len(lines)} SAY-lines in {DLG_BASENAME} needing new VO.")
    else:
        joined = ", ".join(dlg_variants)
        print(f"Found {len(lines)} SAY-lines across variants {joined} needing new VO.")

    seeds = load_seeds()
    seeds_by_key = {s["key"]: s for s in seeds}
    char_seed_for_narration = pick_baseline_seed(seeds)
    narrator_seed = load_narrator_seed()

    keep_lines, regen_lines, _ = plan_generation(lines, seeds)

    # Single targeted pass: mark by word/substring only
    targeted_regen_by_word(lines, keep_lines, regen_lines)

    # Categorize regen lines by narration / dialogue roles
    if narrator_seed is not None and ENABLE_NARRATION_STITCH:
        narr_only_regen, char_only_regen, mixed_regen = classify_narrator_only_lines(regen_lines)
    else:
        narr_only_regen = []
        char_only_regen = list(regen_lines)
        mixed_regen = []

    first_run = not SOUNDS_DIR.exists() or not any(SOUNDS_DIR.glob("*.wav"))
    if first_run:
        print("[DEBUG] No existing audio found: baseline mode enabled.")
        baseline_seed = char_seed_for_narration
        # Only synthesize pure character-only lines in the baseline batch;
        # mixed lines will be handled via stitching only.
        for line in char_only_regen:
            line["seed_key"] = baseline_seed["key"]
        synthesize_baseline(char_only_regen, baseline_seed, seeds_by_key)
    else:
        synthesize_lines_batch(char_only_regen, seeds, seeds_by_key)

    # Generate pure narrator-only lines in narrator voice
    synthesize_narrator_only_lines(narr_only_regen, narrator_seed)

    # Stitch mixed narrator+character lines
    if narrator_seed is not None and ENABLE_NARRATION_STITCH:
        stitch_narration(mixed_regen, narrator_seed, char_seed_for_narration)

    # Assemble base voiced-line set (before TLK-wide dedup)
    base_voiced = keep_lines + narr_only_regen + char_only_regen + mixed_regen

    # TLK-wide duplicate propagation (after all audio is generated)
    if ENABLE_GLOBAL_TLK_DEDUP and strref_to_text is not None:
        voiced_lines = expand_duplicates(base_voiced, strref_to_text, textkey_to_strrefs)
    else:
        voiced_lines = base_voiced

    print(f"[DEBUG] Primary voiced lines count (all variants + TLK dedup): {len(voiced_lines)}")
    if not voiced_lines:
        cleanup_decompiled_sources()
        raise SystemExit("No voiced lines found; nothing to write to TP2.")

    write_tp2(voiced_lines)
    write_viewer_metadata(voiced_lines)
    write_viewer_script()

    # Clean up any .D / .TRA we decompiled
    cleanup_decompiled_sources()



# ==============================
# CLI ENTRY
# ==============================

def main_cli():
    print("=== Infinity Engine Auto-VO (VoxCPM) ===")
    dlg_input = input("Enter DLG name for this run (e.g., DMORTE, DSOEGO, DAKKON, ANNA): ").strip()
    if not dlg_input:
        print("No DLG name provided; exiting.")
        return
    run_for_dlg(dlg_input)


if __name__ == "__main__":
    main_cli()
