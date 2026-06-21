"""Natív mappa- és fájlválasztók — nem a Streamlit folyamatban futtatott Tk (macOS crash elkerülése)."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _escape_applescript_string(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _ask_directory_macos(title: str) -> str | None:
    """Finder-stílusú mappaválasztó — nincs Tk a Streamlitben."""
    prompt = _escape_applescript_string(title)
    script = f'return POSIX path of (choose folder with prompt "{prompt}")'
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except FileNotFoundError:
        return None
    if r.returncode != 0:
        return ""
    out = (r.stdout or "").strip()
    return out if out else ""


def _ask_directory_zenity(title: str) -> str | None:
    exe = shutil.which("zenity")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "--file-selection", "--directory", f"--title={title}"],
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except OSError:
        return None
    if r.returncode != 0:
        return ""
    out = (r.stdout or "").strip()
    return out if out else ""


def _ask_directory_subprocess_tk(title: str) -> str | None:
    """Tk csak külön Python-folyamatban (saját főszál), így nem üti össze a Streamlitet."""
    title_repr = repr(title)
    code = f"""import os
os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")
import tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
root.update_idletasks()
try:
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    p = filedialog.askdirectory(title={title_repr})
finally:
    root.destroy()
print(p if p else "", end="")
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix="_folder_pick.py", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(code)
        tmppath = tf.name

    creationflags = 0
    if platform.system() == "Windows":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        r = subprocess.run(
            [sys.executable, tmppath],
            capture_output=True,
            text=True,
            timeout=3600,
            creationflags=creationflags,
        )
    except OSError:
        return None
    finally:
        Path(tmppath).unlink(missing_ok=True)

    if r.returncode != 0:
        err = (r.stderr or "") + (r.stdout or "")
        if "no display" in err.lower() or "couldn't connect to display" in err.lower():
            return None
        return ""
    return (r.stdout or "").strip()


def _ask_open_files_macos(title: str) -> str | None:
    """Több kép kiválasztása (Finder). Vissza: POSIX utak, soronként; üres = mégse; None = hiba."""
    prompt = _escape_applescript_string(title)
    script = f'''
try
    set theFiles to choose file with prompt "{prompt}" with multiple selections allowed
on error number -128
    return ""
end try
if (class of theFiles) is list then
    set fileList to theFiles
else
    set fileList to {{theFiles}}
end if
set outLines to {{}}
repeat with f in fileList
    set end of outLines to (POSIX path of f)
end repeat
set AppleScript's text item delimiters to linefeed
set outText to outLines as string
set AppleScript's text item delimiters to ""
return outText
'''
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except FileNotFoundError:
        return None
    if r.returncode != 0:
        return ""
    return (r.stdout or "").rstrip("\n")


def _ask_open_files_zenity(title: str) -> str | None:
    exe = shutil.which("zenity")
    if not exe:
        return None
    filt = "Images | *.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.JPG *.JPEG *.PNG"
    try:
        r = subprocess.run(
            [
                exe,
                "--file-selection",
                "--multiple",
                "--separator=|",
                f"--title={title}",
                f"--file-filter={filt}",
            ],
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except OSError:
        return None
    if r.returncode != 0:
        return ""
    out = (r.stdout or "").strip()
    return out


def _ask_open_files_subprocess_tk(title: str) -> str | None:
    """Tk külön folyamatban: askopenfilenames — több fájl, pipe-ként vissza."""
    title_repr = repr(title)
    code = f"""import os
os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")
import tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
root.update_idletasks()
paths = ()
try:
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass
    paths = filedialog.askopenfilenames(
        title={title_repr},
        filetypes=[
            ("Képek", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff"),
            ("Minden fájl", "*.*"),
        ],
    )
finally:
    root.destroy()
print("|".join(paths), end="")
"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix="_open_pick.py", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(code)
        tmppath = tf.name

    creationflags = 0
    if platform.system() == "Windows":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        r = subprocess.run(
            [sys.executable, tmppath],
            capture_output=True,
            text=True,
            timeout=3600,
            creationflags=creationflags,
        )
    except OSError:
        return None
    finally:
        Path(tmppath).unlink(missing_ok=True)

    if r.returncode != 0:
        err = (r.stderr or "") + (r.stdout or "")
        if "no display" in err.lower() or "couldn't connect to display" in err.lower():
            return None
        return ""
    return (r.stdout or "").strip()


def ask_open_image_paths(title: str = "Határoló képek kiválasztása") -> list[str] | None:
    """
    Natív fájlválasztó — több kép egyszerre (Streamlit-biztos: nem in-process Tk a macOS-en).

    Vissza:
        - nem üres ``list[str]``: abszolút útvonalak
        - ``[]``: a felhasználó megszakította / nem választott
        - ``None``: egyik backend sem futtatható
    """
    system = platform.system()
    raw: str | None = None

    if system == "Darwin":
        raw = _ask_open_files_macos(title)
        if raw is not None:
            if raw == "":
                return []
            return [ln.strip() for ln in raw.splitlines() if ln.strip()]

    if system == "Linux":
        raw = _ask_open_files_zenity(title)
        if raw is not None:
            if raw == "":
                return []
            return [p.strip() for p in raw.split("|") if p.strip()]

    raw = _ask_open_files_subprocess_tk(title)
    if raw is None:
        return None
    if raw == "":
        return []
    return [p.strip() for p in raw.split("|") if p.strip()]


def ask_directory(title: str = "Mappa kiválasztása") -> str | None:
    """
    Megnyit egy rendszer tallózót (Streamlit-biztos: nem in-process Tk a macOS-en).

    Visszatérés:
        - nem üres str: kiválasztott mappa abszolút útvonala
        - \"\": a felhasználó megszakította / nem választott
        - None: egyik backend sem futtatható (headless / hiányzó eszköz)
    """
    system = platform.system()

    if system == "Darwin":
        result = _ask_directory_macos(title)
        if result is not None:
            return result

    if system == "Linux":
        result = _ask_directory_zenity(title)
        if result is not None:
            return result

    return _ask_directory_subprocess_tk(title)
