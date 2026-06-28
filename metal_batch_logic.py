"""
Fémlapos TAG / mappa rendezés: OCR a mappanévhez, határoló kép, jegyzőkönyv (PDF) + fotók almappák.
"""

from __future__ import annotations

import os
import re
import shutil
import unicodedata
import uuid
from functools import partial
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Tuple
from urllib.parse import unquote, urlparse

import cv2
import imagehash
import numpy as np
from PIL import Image, ImageOps

try:
    import pytesseract
except ImportError:  # pragma: no cover
    pytesseract = None  # type: ignore

IMAGE_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tiff",
    ".tif",
    ".heic",
    ".heif",
}
PDF_SUFFIX = ".pdf"
UNASSIGNED = "_nincs_köteg"


def _unicode_path_variants(path: Path) -> list[Path]:
    """NFC/NFD változatok — macOS APFS gyakran NFD-ben tárol fájlneveket."""
    s = str(path)
    variants = [path]
    for form in ("NFC", "NFD"):
        candidate = Path(unicodedata.normalize(form, s))
        if candidate not in variants:
            variants.append(candidate)
    return variants


def _path_exists(path: Path) -> bool:
    return any(p.exists() for p in _unicode_path_variants(path))


def _match_dir_entry(parent: Path, name: str) -> Path | None:
    """Szülőmappában keresés pontos / NFC / NFD egyezéssel."""
    target_nfc = unicodedata.normalize("NFC", name)
    target_nfd = unicodedata.normalize("NFD", name)
    try:
        for entry in parent.iterdir():
            en = entry.name
            if (
                en == name
                or unicodedata.normalize("NFC", en) == target_nfc
                or unicodedata.normalize("NFD", en) == target_nfd
            ):
                return entry
    except (OSError, PermissionError):
        pass
    return None


def _resolve_existing_path(path: Path) -> Path | None:
    """
    Létező útvonal feloldása: teljes NFC/NFD, szegmensenkénti egyezés,
    illetve „input - mappa” → „mappa” alias (gyakori bemásolási hiba).
    """
    for candidate in _unicode_path_variants(path):
        if candidate.exists():
            return candidate

    parts = path.parts
    if len(parts) < 2:
        return None

    resolved = Path(parts[0])
    for part in parts[1:]:
        target = resolved / part
        if _path_exists(target):
            resolved = next(p for p in _unicode_path_variants(target) if p.exists())
            continue

        matched = _match_dir_entry(resolved, part) if resolved.is_dir() else None
        if matched is not None:
            resolved = matched
            continue

        if " - " in part and resolved.is_dir():
            suffix = part.split(" - ", 1)[1]
            alias = resolved / suffix
            if _path_exists(alias):
                resolved = next(p for p in _unicode_path_variants(alias) if p.exists())
                continue
            matched = _match_dir_entry(resolved, suffix)
            if matched is not None:
                resolved = matched
                continue

        return None

    return resolved if resolved.exists() else None


def _suggest_alternate_folder(path: Path) -> Path | None:
    """Ha a megadott útvonal nem létezik, javasolt közeli mappa (csak hibaüzenethez)."""
    if path.exists():
        return None
    parent = path.parent
    if not parent.is_dir():
        return None
    name = path.name
    if " - " in name:
        suffix = name.split(" - ", 1)[1]
        candidate = parent / suffix
        if candidate.is_dir():
            return candidate
        matched = _match_dir_entry(parent, suffix)
        if matched is not None and matched.is_dir():
            return matched
    matched = _match_dir_entry(parent, name)
    if matched is not None and matched.is_dir():
        return matched
    return None


def normalize_user_path(raw: Path | str) -> Path:
    """
    Felhasználói útvonal tisztítása: szóköz, idézőjelek, ``~``, ``file://`` URI,
    macOS Unicode (NFC/NFD) és gyakori aliasok (pl. ``input - mappa`` → ``mappa``).
    A beírt / bemásolt / tallózott mappaútvonalakhoz használd — nem belső temp útvonalakhoz.
    """
    s = str(raw).strip()
    if not s:
        return Path(s)
    if s.lower().startswith("file://"):
        parsed = urlparse(s)
        s = unquote(parsed.path)
        if not s and parsed.netloc:
            s = unquote(f"//{parsed.netloc}{parsed.path}")
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    path = Path(s).expanduser()
    if path.exists():
        return path
    resolved = _resolve_existing_path(path)
    return resolved if resolved is not None else path


def folder_path_error_message(path: Path | str) -> str:
    """Magyar hibaüzenet, ha az útvonal nem használható forrásmappaként."""
    path = normalize_user_path(path)
    try:
        if not path.exists():
            alt = _suggest_alternate_folder(path)
            if alt is not None:
                return (
                    f"Az útvonal nem létezik: {path}\n"
                    f"Talán ezt érted: {alt}"
                )
            return f"Az útvonal nem létezik: {path}"
        if path.is_file():
            return (
                f"Fájl van megadva mappa helyett — a szülő mappát használd: {path.parent}"
            )
        if path.is_symlink():
            try:
                target = path.resolve()
                if not target.is_dir():
                    return f"A link nem mappára mutat: {path} → {target}"
            except OSError:
                return f"A link nem olvasható: {path}"
        if not path.is_dir():
            return f"A megadott útvonal nem mappa: {path}"
    except PermissionError:
        return (
            f"Nincs jogosultság az útvonal ellenőrzéséhez: {path}\n"
            "macOS: adj a Terminalnak / Cursornak **Teljes lemezhez való hozzáférést** "
            "(Rendszerbeállítások → Adatvédelem és biztonság)."
        )
    except OSError as e:
        return f"Az útvonal nem olvasható: {path} ({e.__class__.__name__}: {e})"
    return f"A megadott útvonal nem mappa: {path}"


def norm_path_key(p: Path | str) -> str:
    """Egységes útvonal-kulcs (expanduser) — skip/force és fájllista összevetéséhez."""
    return str(normalize_user_path(p))


def sort_media_paths_by_name_then_mtime(paths: list[Path]) -> list[Path]:
    """
    Képek + PDF egy listájának rendezése: **fájlnév** lexikografikusan, majd **mtime** másodlagosan
    (ugyanaz, mint ``list_sorted_media`` — előnézet, cache-rejátszás és új terv egységes sorrendje).
    """

    def sk(p: Path) -> tuple[str, float]:
        try:
            return (p.name.lower(), p.stat().st_mtime)
        except OSError:
            return (p.name.lower(), 0.0)

    return sorted(paths, key=sk)

# Határoló: ha a pHash > max_hamming, az aHash „mentés” csak addig érvényes, amíg a pHash
# nem haladja meg ezt a felső határt — különben sok téves találat (világosság hasonló, tartalom más).
DELIMITER_RESCUE_PHASH_EXTRA = 16
# aHash küszöb: max(12, max_hamming + DELIMITER_AHASH_OFFSET) — +11 a szűk (14, 0.92) párok miatt.
DELIMITER_AHASH_OFFSET = 11


def safe_folder_name(raw: str, max_len: int = 80) -> str:
    """
    Biztonságos, **korlátos** mappanév. A nyers OCR gyakran **többsoros, zajos** szöveg —
    ilyenkor csak az **első nem üres sort** használjuk (a teljes zaj sosem lesz mappanév),
    a tiltott karaktereket cseréljük, és ``max_len`` hosszra vágunk. Üres / csupa-szemét
    bemenetre stabil ``"azonosítatlan"`` a tartalék.
    """
    text = raw if isinstance(raw, str) else str(raw or "")
    # Az első nem üres sor — a többsoros OCR-zaj ne folyjon bele a mappanévbe.
    first_line = ""
    for line in text.replace("\r", "\n").split("\n"):
        if line.strip():
            first_line = line.strip()
            break
    s = re.sub(r'[<>:"/\\\\|?*]', "_", first_line)
    s = re.sub(r"\s+", "_", s)
    s = s.strip("._") or "azonosítatlan"
    return s[:max_len]


def list_sorted_media(source: Path, recursive: bool = False) -> Tuple[list[Path], Optional[str]]:
    """
    Fájlnév szerint (stabil, ha a névben van dátum/idő), majd módosítás ideje másodlagosan: képek + PDF.
    (A korábbi „előbb mtime” rendezés gyakran felborította a kamera-fájlok időrendjét, ha az mtime nem egyezett a fájlnévvel.)
    Vissza: (fájlok, hibaüzenet) — ha a mappa nem olvasható (pl. macOS jogosultság), üres lista + magyarázat.
    """
    source = normalize_user_path(source)
    if not source.is_dir():
        return [], folder_path_error_message(source)

    items: list[Path] = []

    if recursive:

        def walk_safe(root: Path) -> None:
            stack = [root]
            while stack:
                d = stack.pop()
                try:
                    for entry in d.iterdir():
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry)
                            elif entry.is_file():
                                suf = entry.suffix.lower()
                                if suf in IMAGE_SUFFIXES or suf == PDF_SUFFIX:
                                    items.append(entry)
                        except OSError:
                            continue
                except (PermissionError, OSError):
                    continue

        try:
            next(iter(source.iterdir()), None)
        except (PermissionError, OSError) as e:
            return [], (
                f"Nincs jogosultság a mappa listázásához: {source}\n"
                f"({e.__class__.__name__}: {e})\n"
                "macOS: adj a Terminalnak / Cursornak **Teljes lemezhez való hozzáférést** "
                "(Rendszerbeállítások → Adatvédelem és biztonság), vagy másolj egy olyan mappába, "
                "amit az app olvashat."
            )

        walk_safe(source)
        return sort_media_paths_by_name_then_mtime(items), None

    try:
        for p in source.iterdir():
            if not p.is_file():
                continue
            suf = p.suffix.lower()
            if suf in IMAGE_SUFFIXES or suf == PDF_SUFFIX:
                items.append(p)
    except PermissionError as e:
        return [], (
            f"Nincs jogosultság a mappa listázásához: {source}\n"
            f"(PermissionError: {e})\n"
            "macOS: adj a Terminalnak / Cursornak **Teljes lemezhez való hozzáférést** "
            "(Rendszerbeállítások → Adatvédelem és biztonság), vagy másolj egy olyan mappába, "
            "amit az app olvashat."
        )
    except OSError as e:
        return [], f"A mappa nem olvasható: {source}\n({e.__class__.__name__}: {e})"

    return sort_media_paths_by_name_then_mtime(items), None


def _read_bgr(path: Path) -> Optional[np.ndarray]:
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    return img


def _pil_to_rgb_for_hash(pil: Image.Image) -> Image.Image:
    """EXIF után: RGB + fehér háttér átlátszó rétegnél (pHash stabilabb)."""
    if pil.mode in ("RGBA", "LA"):
        if pil.mode == "LA":
            pil = pil.convert("RGBA")
        bg = Image.new("RGB", pil.size, (255, 255, 255))
        bg.paste(pil, mask=pil.split()[3])
        return bg
    if pil.mode == "P":
        if "transparency" in pil.info:
            pil = pil.convert("RGBA")
            bg = Image.new("RGB", pil.size, (255, 255, 255))
            bg.paste(pil, mask=pil.split()[3])
            return bg
        return pil.convert("RGB")
    return pil.convert("RGB")


def _crop_center_inner(pil: Image.Image, inner_ratio: float) -> Image.Image:
    """
    Középső inner_ratio × méretű téglalap (0–1). Sarokvágás / hiányzó sarok esetén
    a határoló-hash kevésbé érzékeny, ha inner_ratio < 1.0.
    """
    r = float(inner_ratio)
    r = max(0.65, min(1.0, r))
    if r >= 0.999:
        return pil
    w, h = pil.size
    if w < 2 or h < 2:
        return pil
    nw = max(1, int(round(w * r)))
    nh = max(1, int(round(h * r)))
    nw = min(nw, w)
    nh = min(nh, h)
    left = (w - nw) // 2
    top = (h - nh) // 2
    return pil.crop((left, top, left + nw, top + nh))


def phash_and_ahash(
    path: Path, *, inner_ratio: float = 1.0
) -> Optional[Tuple[imagehash.ImageHash, imagehash.ImageHash]]:
    """pHash + average hash ugyanabból a képből (egy megnyitás, EXIF + RGB)."""
    try:
        pil = Image.open(path)
        pil = ImageOps.exif_transpose(pil)
        pil = _pil_to_rgb_for_hash(pil)
        pil = _crop_center_inner(pil, inner_ratio)
        ph = imagehash.phash(pil, hash_size=8, highfreq_factor=4)
        ah = imagehash.average_hash(pil, hash_size=8)
        return ph, ah
    except Exception:
        return None


def phash_of_image(path: Path) -> Optional[imagehash.ImageHash]:
    """
    Csak pHash (pl. régi hívásokhoz). Preferáld a phash_and_ahash + is_delimiter_image párost.
    """
    pair = phash_and_ahash(path)
    return pair[0] if pair else None


def delimiter_match_hashes(
    pair: Tuple[imagehash.ImageHash, imagehash.ImageHash],
    ref_phash: imagehash.ImageHash,
    ref_ahash: imagehash.ImageHash,
    max_hamming: int,
) -> bool:
    """
    Előre kiszámolt (pHash, aHash) pár vs. referencia.

    1) pHash <= max_hamming → egyezés (erős jel).
    2) Egyébként: aHash elég közel, ÉS a pHash nem lehet túl messze (max_hamming + extra),
       különben a puszta világosság-hasonlóság sok fals határolót adna.
    """
    h, a = pair
    ph_dist = h - ref_phash
    if ph_dist <= max_hamming:
        return True
    a_thresh = min(60, max(12, max_hamming + DELIMITER_AHASH_OFFSET))
    if (a - ref_ahash) > a_thresh:
        return False
    rescue_cap = max_hamming + DELIMITER_RESCUE_PHASH_EXTRA
    return ph_dist <= rescue_cap


def is_delimiter_image(
    path: Path,
    ref_phash: imagehash.ImageHash,
    ref_ahash: imagehash.ImageHash,
    max_hamming: int,
    *,
    inner_ratio: float = 1.0,
) -> bool:
    """
    Egyezés, ha a pHash távolság <= max_hamming, VAGY (szűkített mentés) az average hash elég közel
    és a pHash nem haladja meg a max_hamming + extra felső határt.
    """
    pair = phash_and_ahash(path, inner_ratio=inner_ratio)
    if pair is None:
        return False
    return delimiter_match_hashes(pair, ref_phash, ref_ahash, max_hamming)


def _tesseract_text(gray: np.ndarray, psm: int, *, use_whitelist: bool = True) -> str:
    if pytesseract is None:
        return ""
    if use_whitelist:
        wl = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyzáéíóöőúüűÁÉÍÓÖŐÚÜŰ.-_/ "
        cfg = f"--oem 3 --psm {psm} -c tessedit_char_whitelist={wl}"
    else:
        cfg = f"--oem 3 --psm {psm}"
    try:
        return pytesseract.image_to_string(gray, config=cfg, lang="hun+eng") or ""
    except Exception:
        try:
            return pytesseract.image_to_string(gray, config=cfg, lang="eng") or ""
        except Exception:
            return ""


def _auto_ocr_preprocess_gray(gray: np.ndarray) -> np.ndarray:
    """
    Kép-statisztikából becsült „jó” szürke sáv: percentilis-alapú dinamika + opcionális offset + CLAHE.
    """
    g = gray.astype(np.float32)
    p2, p98 = np.percentile(g, (2.0, 98.0))
    if p98 <= p2 + 1e-3:
        g2 = gray.copy()
    else:
        g = (g - p2) / max(p98 - p2, 1e-3) * 255.0
        g2 = np.clip(g, 0, 255).astype(np.uint8)

    mean = float(np.mean(g2))
    std = float(np.std(g2))
    if mean < 102.0:
        beta = int(min(48, max(5, int(118.0 - mean))))
        g2 = cv2.convertScaleAbs(g2, alpha=1.0, beta=beta)
    elif mean > 168.0:
        beta = int(max(-48, min(-5, int(138.0 - mean))))
        g2 = cv2.convertScaleAbs(g2, alpha=1.0, beta=beta)

    std2 = float(np.std(g2))
    clip = float(np.clip(4.2 - std2 / 32.0, 1.7, 5.8))
    clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8))
    return clahe.apply(g2)


def ocr_metal_plate_line(
    path: Path,
    *,
    auto_preprocess: bool = True,
    brightness_beta: int = 0,
    contrast_alpha: float = 1.0,
    gamma: float = 1.0,
    clahe_clip: float = 2.0,
) -> Optional[str]:
    """
    Fémlap / adattábla szöveg becslése (Tesseract).

    auto_preprocess=True (alap): percentilis + CLAHE + több bináris variáns + élesebb célfelbontás
    (kis kép felnagyítása, nagy kép max. ~1800 px hosszabb oldal — jobb olvashatóság, mint a régi 960).
    """
    if pytesseract is None:
        return None
    bgr = _read_bgr(path)
    if bgr is None:
        return None
    h, w = bgr.shape[:2]
    long_side = float(max(h, w))
    # Kis kép: felnagyítás (a vékony felirat olvashatóbb legyen).
    if long_side < 1000.0:
        su = min(3.0, 1200.0 / long_side)
        bgr = cv2.resize(bgr, None, fx=su, fy=su, interpolation=cv2.INTER_CUBIC)
        h, w = bgr.shape[:2]
        long_side = float(max(h, w))
    # Nagy kép: lekicsinyítés (memória / sebesség); 1800 > régi 960 → kevesebb részletvesztés.
    ocr_max = 1800.0
    if long_side > ocr_max:
        sc = ocr_max / long_side
        bgr = cv2.resize(bgr, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
        h, w = bgr.shape[:2]

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    if auto_preprocess:
        gray = _auto_ocr_preprocess_gray(gray)
        g2 = gray
    else:
        ca = float(max(0.3, min(contrast_alpha, 3.0)))
        bb = int(max(-80, min(brightness_beta, 80)))
        gm = float(max(0.35, min(gamma, 2.5)))
        clip = float(max(0.5, min(clahe_clip, 8.0)))

        gray = cv2.convertScaleAbs(gray, alpha=ca, beta=bb)
        if abs(gm - 1.0) > 0.02:
            inv_gamma = 1.0 / gm
            lut = np.clip((np.arange(256, dtype=np.float32) / 255.0) ** inv_gamma * 255.0, 0, 255).astype(
                np.uint8
            )
            gray = cv2.LUT(gray, lut)

        clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8))
        g2 = clahe.apply(gray)

    th = cv2.adaptiveThreshold(g2, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 35, 11)
    th_inv = cv2.adaptiveThreshold(g2, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 35, 11)
    blur = cv2.GaussianBlur(g2, (3, 3), 0)
    _, otsu = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    blur3 = cv2.GaussianBlur(g2, (0, 0), 1.0)
    sharp = cv2.addWeighted(g2, 1.28, blur3, -0.28, 0)

    variants = [g2, sharp, th, th_inv, otsu]

    def score_line(s: str) -> tuple[int, int]:
        alnum = re.sub(r"[^0-9A-Za-záéíóöőúüűÁÉÍÓÖŐÚÜŰ._\-/]", "", " ".join(s.split()))
        core = re.sub(r"[^0-9A-Za-záéíóöőúüűÁÉÍÓÖŐÚÜŰ]", "", alnum)
        return len(alnum), len(core)

    best = ""
    best_key = (-1, -1)
    strong_exit = False

    def consider(t: str) -> None:
        nonlocal best, best_key, strong_exit
        alnum = re.sub(r"[^0-9A-Za-záéíóöőúüűÁÉÍÓÖŐÚÜŰ._\-/]", "", " ".join(t.split()))
        key = score_line(t)
        if key > best_key or (key == best_key and len(alnum) > len(best)):
            best = alnum
            best_key = key
        core_now = re.sub(r"[^0-9A-Za-záéíóöőúüűÁÉÍÓÖŐÚÜŰ]", "", alnum)
        if len(core_now) >= 10:
            best = alnum
            strong_exit = True

    for g in variants:
        for psm in (7, 6, 11, 8):
            consider(_tesseract_text(g, psm, use_whitelist=True))
            if strong_exit:
                break
        if strong_exit:
            break
        core_best = re.sub(r"[^0-9A-Za-záéíóöőúüűÁÉÍÓÖŐÚÜŰ]", "", best)
        if len(core_best) >= 10:
            break

    # Gyenge / rövid találatnál: fehérlista nélkül (speciális karakterek, más szegmentálás).
    if not strong_exit and len(re.sub(r"[^0-9A-Za-záéíóöőúüűÁÉÍÓÖŐÚÜŰ]", "", best)) < 8:
        for g in (g2, sharp, th):
            for psm in (7, 11):
                consider(_tesseract_text(g, psm, use_whitelist=False))
                if strong_exit:
                    break
            if strong_exit:
                break
    # legalább 3 „értelmes” karakter (szám/betű)
    core = re.sub(r"[^0-9A-Za-záéíóöőúüűÁÉÍÓÖŐÚÜŰ]", "", best)
    if len(core) < 3:
        return None
    return best.strip("._-/ ") or None


def unique_dest(dest_dir: Path, filename: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    if not dest.exists():
        return dest
    stem = Path(filename).stem
    suf = Path(filename).suffix
    for i in range(1, 10000):
        cand = dest_dir / f"{stem}_{i}{suf}"
        if not cand.exists():
            return cand
    return dest_dir / f"{stem}_{uuid.uuid4().hex[:8]}{suf}"


def write_uploaded_media_to_dir(
    items: Iterable[Tuple[str, bytes]], dest_dir: Path | str
) -> list[Path]:
    """
    Feltöltött ``(fájlnév, tartalom)`` párokat ír egy munkamappába (felhő / headless mód:
    nincs helyi forrásmappa). A névből csak az alap fájlnevet tartjuk meg (útvonal-rész nélkül),
    a névütközést ``unique_dest`` oldja fel — így a meglévő pipeline (``list_sorted_media`` →
    fájlnév szerinti sorrend, ``build_plan``) változatlanul használható a temp mappára.

    Vissza: a kiírt fájlok útvonalai a hívás sorrendjében.
    """
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, data in items:
        safe = Path(str(name)).name or "feltoltott_fajl"
        target = unique_dest(dest, safe)
        target.write_bytes(data if isinstance(data, (bytes, bytearray)) else bytes(data))
        written.append(target)
    return written


@dataclass
class Segment:
    """Egy TAG / mappa szegmens: határolóképek közötti blokk; OCR (vagy név nélkül fájlnév) alapú mappa, fémlap-kép, fájlok."""

    folder_key: str  # safe_folder_name(ocr_raw) — végrehajtás előtt a UI felülírhatja
    plate_image: Path  # az OCR-t adó (első sikeres) kép
    ocr_raw: str  # a Tesseract által felismert karaktersor
    photos: list[Path] = field(default_factory=list)
    pdfs: list[Path] = field(default_factory=list)
    closed_by_delimiter: Optional[Path] = None  # a szegmenst lezáró határoló kép (ha volt)


@dataclass
class OrganizePlan:
    segments: list[Segment] = field(default_factory=list)
    unassigned_images: list[Path] = field(default_factory=list)
    unassigned_pdfs: list[Path] = field(default_factory=list)
    delimiter_hits: list[Path] = field(default_factory=list)


@dataclass
class PlanScanCache:
    """
    Első ``build_plan`` után: határoló-demóció / ``non_delimiter_paths`` változásnál
    újra lehet játszani a szegmentációt **hash újraszámolás és a már tárolt OCR nélkül**.
    """

    files: list[Path]
    hash_by_path: dict[Path, Tuple[imagehash.ImageHash, imagehash.ImageHash]]
    ref_phash: imagehash.ImageHash
    ref_ahash: imagehash.ImageHash
    max_hamming: int
    inner_ratio: float
    source_str: str
    recursive: bool
    image_count: int = 0
    files_sorted_by_name_mtime: bool = False
    ocr_by_path: dict[Path, Optional[str]] = field(default_factory=dict)
    delimiter_candidates: set[str] = field(default_factory=set)


def _path_match_key(p: Path | str) -> str:
    """Egyező útvonal-kulcs skip/force és fájl összehasonlításhoz (expanduser)."""
    return str(normalize_user_path(p))


def list_delimiter_followers_preview(
    cache: PlanScanCache,
    non_delimiter_paths: Iterable[str],
    force_delimiter_paths: Iterable[str],
    *,
    following_max: int = 24,
    file_sequence: Optional[list[Path]] = None,
) -> list[tuple[Path, list[Path]]]:
    """
    A fájlsorrendben minden **érvényes** határoló képhez megadja a közvetlenül követő,
    nem határoló **képeket** (PDF kihagyva), a következő határolóig vagy ``following_max`` darabig.

    ``file_sequence``: ha megadod, ezen a listán lépked (pl. név szerint rendezett másolat a ``cache.files``-ról);
    alapértelmezés: ``cache.files`` (a tervkészítéskor használt sorrend).

    A ``non_delimiter_paths`` / ``force_delimiter_paths`` ugyanazzal a logikával érvényesül, mint
    ``_segment_media_to_plan`` (hash + kényszerített + kizárás).
    """
    skip = {_path_match_key(x) for x in non_delimiter_paths}
    force = {_path_match_key(x) for x in force_delimiter_paths}
    hmap = cache.hash_by_path
    files = list(file_sequence) if file_sequence is not None else cache.files
    baseline_delims = set(cache.delimiter_candidates)

    def is_delimiter(f: Path) -> bool:
        if f.suffix.lower() not in IMAGE_SUFFIXES:
            return False
        sf = _path_match_key(f)
        if sf in skip:
            return False
        if sf in force:
            return True
        if sf in baseline_delims:
            return True
        pair = hmap.get(f)
        if pair is None:
            return False
        return delimiter_match_hashes(pair, cache.ref_phash, cache.ref_ahash, cache.max_hamming)

    out: list[tuple[Path, list[Path]]] = []
    for i, f in enumerate(files):
        if not is_delimiter(f):
            continue
        followers: list[Path] = []
        j = i + 1
        while j < len(files) and len(followers) < following_max:
            g = files[j]
            if g.suffix.lower() not in IMAGE_SUFFIXES:
                j += 1
                continue
            if is_delimiter(g):
                break
            followers.append(g)
            j += 1
        out.append((f, followers))
    return out


def _segment_media_to_plan(
    files: list[Path],
    hash_by_path: dict[Path, Tuple[imagehash.ImageHash, imagehash.ImageHash]],
    ref_phash: imagehash.ImageHash,
    ref_ahash: imagehash.ImageHash,
    max_hamming: int,
    skip_del: set[str],
    force_del: set[str],
    ocr: Callable[[Path], Optional[str]],
    ocr_by_path: dict[Path, Optional[str]],
    *,
    use_ocr_cache: bool,
    delimiter_candidates: Optional[set[str]] = None,
    total_images: Optional[int] = None,
    progress: Optional[Callable[[float, Optional[str]], None]],
) -> OrganizePlan:
    """
    TAG/mappa szegmentáció + határoló.

    **Határoló = mindig szegmenshatár:** lezárja az aktuális blokkot; a következő nem-határoló kép
    mindig új szegmenst nyit (OCR üresen: fájlnév / „azonosítatlan”).
    ``use_ocr_cache`` igaz = csak hiányzó útvonalra hív OCR-t.
    """

    def rep(frac: float, msg: Optional[str] = None) -> None:
        if progress is not None:
            progress(max(0.0, min(1.0, frac)), msg)

    plan = OrganizePlan()
    current: Optional[Segment] = None

    n_images = int(total_images) if total_images is not None else sum(1 for x in files if x.suffix.lower() in IMAGE_SUFFIXES)
    img_done = 0

    def bump_image_progress(fname: str) -> None:
        nonlocal img_done
        img_done += 1
        if progress is not None and n_images:
            rep(0.43 + 0.56 * (img_done / n_images), f"{img_done}/{n_images} — {fname}")

    for f in files:
        suf = f.suffix.lower()

        if suf == PDF_SUFFIX:
            if current is not None:
                current.pdfs.append(f)
            else:
                plan.unassigned_pdfs.append(f)
            continue

        if suf not in IMAGE_SUFFIXES:
            continue

        pair = hash_by_path.get(f)
        sf = norm_path_key(f)
        if sf in skip_del:
            is_delimiter = False
        elif sf in force_del:
            is_delimiter = True
        elif delimiter_candidates is not None:
            is_delimiter = sf in delimiter_candidates
        else:
            is_delimiter = pair is not None and delimiter_match_hashes(
                pair, ref_phash, ref_ahash, max_hamming
            )
        if is_delimiter:
            plan.delimiter_hits.append(f)
            if current is not None:
                current.closed_by_delimiter = f
                plan.segments.append(current)
                current = None
            bump_image_progress(f.name)
            continue

        # Nem határoló kép: mindig egy szegmenshez tartozik (újat nyitunk, ha épp nincs nyitott).
        if current is None:
            if use_ocr_cache and f in ocr_by_path:
                raw = ocr_by_path[f]
            else:
                raw = ocr(f)
                ocr_by_path[f] = raw
            ocr_text = (raw or "").strip() if isinstance(raw, str) else ""
            if not ocr_text:
                ocr_text = (f.stem or "").strip() or "azonosítatlan"
            key = safe_folder_name(ocr_text)
            current = Segment(
                folder_key=key,
                plate_image=f,
                ocr_raw=ocr_text,
                photos=[f],
                closed_by_delimiter=None,
            )
        else:
            current.photos.append(f)
        bump_image_progress(f.name)

    if current is not None:
        plan.segments.append(current)

    return plan


def replay_plan_from_cache(
    cache: PlanScanCache,
    non_delimiter_paths: Optional[Iterable[str]] = None,
    force_delimiter_paths: Optional[Iterable[str]] = None,
    ocr_fn: Optional[Callable[[Path], Optional[str]]] = None,
    progress: Optional[Callable[[float, Optional[str]], None]] = None,
) -> OrganizePlan:
    """
    Újraszámolja a TAG/mappa szegmentációt ugyanabból a mappa-bejárásból / hash táblából,
    csak a ``non_delimiter_paths`` / ``force_delimiter_paths`` és az állapotgép fut újra.
    """
    ocr = ocr_fn or ocr_metal_plate_line
    skip_del: set[str] = {norm_path_key(x) for x in (non_delimiter_paths or ())}
    force_del: set[str] = {norm_path_key(x) for x in (force_delimiter_paths or ())}
    if progress is not None:
        progress(0.02, "TAG/mappa újraszámolása (cache — nincs újra hash az egész mappára)…")
    if bool(getattr(cache, "files_sorted_by_name_mtime", False)):
        files_ordered = list(cache.files)
    else:
        files_ordered = sort_media_paths_by_name_then_mtime(list(cache.files))
    # Gyors út: ha a cache már tartalmazza az alap határoló-jelölteket, újraszámoláskor
    # nem kell újra hash-distance döntést futtatni minden képre.
    delimiter_candidates = set(cache.delimiter_candidates)
    plan = _segment_media_to_plan(
        files_ordered,
        cache.hash_by_path,
        cache.ref_phash,
        cache.ref_ahash,
        cache.max_hamming,
        skip_del,
        force_del,
        ocr,
        cache.ocr_by_path,
        use_ocr_cache=True,
        delimiter_candidates=delimiter_candidates,
        total_images=int(getattr(cache, "image_count", 0)) or None,
        progress=progress,
    )
    if progress is not None:
        progress(1.0, "Terv elkészült.")
    return plan


def build_plan(
    source: Path,
    delimiter_ref: Path,
    max_hamming: int = 12,
    recursive: bool = False,
    ocr_fn: Optional[Callable[[Path], Optional[str]]] = None,
    progress: Optional[Callable[[float, Optional[str]], None]] = None,
    delimiter_inner_ratio: float = 0.92,
    non_delimiter_paths: Optional[Iterable[str]] = None,
    force_delimiter_paths: Optional[Iterable[str]] = None,
    scan_cache_holder: Optional[list] = None,
) -> OrganizePlan:
    """
    0) forrás fájljai idő + név szerint
    1) első sikeres OCR kép → TAG / mappa neve
    2–3) következő képek ugyanide, amíg határoló
    4) PDF → jegyzőkönyv, kép → fotók (a szegmensen belül)

    progress: opcionális (0.0–1.0, üzenet) callback pl. Streamlit progress sávhoz.

    A képek pHash/aHash értékei párhuzamosan elő vannak számolva; a fő ciklusban nincs újra hash.

    delimiter_inner_ratio: 1.0 = teljes kép; pl. 0.92 = középső ~92% — sarokvágás / hiányzó sarok
    kevésbé rontja az egyezést (referencia és minden kép ugyanazzal az aránnyal).

    non_delimiter_paths: abszolút útvonalak stringként (``str(path)``), amely képeknél a pHash egyezés
    ellenére sem tekintünk határolónak — a TAG/mappa szétválasztás újraépül.

    force_delimiter_paths: ezek a képek **mindig határolónak** számítanak (kézi felvétel); a ``non_delimiter_paths``
    felülírja, ha ugyanaz az útvonal mindkettőben lenne.

    scan_cache_holder: ha ``list`` (pl. ``holder = []``; ``build_plan(..., scan_cache_holder=holder)``),
    a futás végén ``holder[0]`` egy :class:`PlanScanCache` példány (demóciós ``replay_plan_from_cache``-hez).
    """
    ocr = ocr_fn or ocr_metal_plate_line
    inner = float(delimiter_inner_ratio)
    inner = max(0.65, min(1.0, inner))
    skip_del: set[str] = {str(x) for x in (non_delimiter_paths or ())}
    force_del: set[str] = {str(x) for x in (force_delimiter_paths or ())}

    def rep(frac: float, msg: Optional[str] = None) -> None:
        if progress is not None:
            progress(max(0.0, min(1.0, frac)), msg)

    rep(0.0, "Határoló referencia pHash számítása…")
    ref_pair = phash_and_ahash(delimiter_ref, inner_ratio=inner)
    if ref_pair is None:
        raise ValueError("A határoló referencia kép nem olvasható.")
    ref_phash, ref_ahash = ref_pair

    rep(0.03, "Forrás mappa bejárása és rendezés…")
    files, list_err = list_sorted_media(source, recursive=recursive)
    if list_err:
        raise RuntimeError(list_err)

    n = len(files)
    rep(0.05, f"{n} fájl — TAG/mappa szegmensek és határolók felismerése…")

    image_files = [f for f in files if f.suffix.lower() in IMAGE_SUFFIXES]
    hash_by_path: dict[Path, Tuple[imagehash.ImageHash, imagehash.ImageHash]] = {}
    ni = len(image_files)
    if ni:
        workers = min(8, (os.cpu_count() or 4))
        rep(0.06, f"{ni} kép hash előszámítása ({workers} szál)…")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            _ph = partial(phash_and_ahash, inner_ratio=inner)
            future_map = {pool.submit(_ph, p): p for p in image_files}
            done = 0
            for fut in as_completed(future_map):
                p = future_map[fut]
                try:
                    pair = fut.result()
                except Exception:
                    pair = None
                done += 1
                if pair is not None:
                    hash_by_path[p] = pair
                if progress:
                    rep(0.05 + 0.37 * (done / ni), f"Hash {done}/{ni} — {p.name}")

    ocr_by_path: dict[Path, Optional[str]] = {}
    if scan_cache_holder is not None:
        scan_cache_holder.clear()

    delimiter_candidates: set[str] = set()
    if hash_by_path:
        for p, pair in hash_by_path.items():
            if delimiter_match_hashes(pair, ref_phash, ref_ahash, max_hamming):
                delimiter_candidates.add(norm_path_key(p))

    plan = _segment_media_to_plan(
        files,
        hash_by_path,
        ref_phash,
        ref_ahash,
        max_hamming,
        skip_del,
        force_del,
        ocr,
        ocr_by_path,
        use_ocr_cache=False,
        delimiter_candidates=delimiter_candidates,
        total_images=ni,
        progress=progress,
    )

    if scan_cache_holder is not None:
        scan_cache_holder.append(
            PlanScanCache(
                files=list(files),
                hash_by_path=dict(hash_by_path),
                ref_phash=ref_phash,
                ref_ahash=ref_ahash,
                max_hamming=max_hamming,
                inner_ratio=inner,
                source_str=str(source.expanduser()),
                recursive=recursive,
                image_count=ni,
                files_sorted_by_name_mtime=True,
                ocr_by_path=ocr_by_path,
                delimiter_candidates=delimiter_candidates,
            )
        )

    rep(1.0, "Terv elkészült.")
    return plan


def allocate_unique_folder_name(folder_key: str, used: set[str], *, max_len: int = 80) -> str:
    """
    Általános segéd: **futáson belül** egyedi mappanevet ad (``_2``, ``_3``, … utótag, ha a
    ``folder_key`` már foglalt). A ``used`` halmazba bekerül a kiosztott név. A ``max_len`` a
    teljes (utótaggal együtt) hosszt korlátozza.

    **Megjegyzés:** az ``execute_plan`` **már NEM** ezt használja — az azonos jóváhagyott nevű
    szegmensek **szándékosan KÖZÖS** mappába kerülnek (összevonás), nem utótagolódnak. Ez a
    függvény csak általános segédként marad meg (és külön teszteli az egyediesítő logikát).
    """
    base = (folder_key or "").strip() or "azonosítatlan"
    if base not in used:
        used.add(base)
        return base
    for i in range(2, 1_000_000):
        suffix = f"_{i}"
        trimmed = base[: max(1, max_len - len(suffix))]
        cand = f"{trimmed}{suffix}"
        if cand not in used:
            used.add(cand)
            return cand
    cand = f"{base[: max(1, max_len - 9)]}_{uuid.uuid4().hex[:8]}"
    used.add(cand)
    return cand


def apply_delimiter_photo_assignments(
    segments: list[Segment],
    delimiter_hits: Iterable[Path],
    delim_to_plate: dict[str, Path],
    *,
    excluded: Optional[set[str]] = None,
) -> None:
    """
    A határoló képeket a cél szegmens ``photos`` listájába teszi (a 5. lépés másolásához).

    ``delim_to_plate``: normalizált határoló-útvonal → a cél szegmens ``plate_image``-je
    (a 3. lépés határoló-sor párosítása szerint; záró határolónál a lezárt szegmens).
    A határoló a lista elejére kerül; ha már benne van, nem duplikál.
    """
    skip = excluded or set()
    plate_to_seg = {norm_path_key(s.plate_image): s for s in segments}
    for delim in delimiter_hits:
        dn = norm_path_key(delim)
        if dn in skip:
            continue
        plate = delim_to_plate.get(dn)
        if plate is None:
            continue
        seg = plate_to_seg.get(norm_path_key(plate))
        if seg is None:
            continue
        existing = {norm_path_key(p) for p in seg.photos}
        if dn in existing:
            continue
        seg.photos.insert(0, delim)


def execute_plan(
    plan: OrganizePlan,
    output_root: Path,
    copy_mode: bool = False,
    progress: Optional[Callable[[float, Optional[str]], None]] = None,
) -> list[tuple[str, Path, Path]]:
    """
    Létrehozza output_root/<key>/jegyzőkönyv és fotók, mozgat/másol.

    **Azonos jóváhagyott név = KÖZÖS mappa (összevonás):** ha több szegmens ugyanazt a
    ``folder_key`` nevet viseli (a felhasználó a 3. lépésben szándékosan azonos nevet adott),
    a képeik/PDF-jeik **ugyanabba** az ``output_root/<név>`` mappába kerülnek — nincs ``_2`` /
    ``_3`` utótagolás. (Az alapértelmezett nevek egyedi **sorszámok**, így összevonás csak a
    szándékosan azonosra írt neveknél történik.) A mappán belül a fájlnév-ütközéseket az
    ``unique_dest`` oldja fel, így egyetlen fájl sem írja felül a másikat / vész el.
    Vissza: (művelet, src, dst) napló.
    """
    log: list[tuple[str, Path, Path]] = []
    op = shutil.copy2 if copy_mode else shutil.move
    total_ops = (
        sum(len(seg.pdfs) + len(seg.photos) for seg in plan.segments)
        + len(plan.unassigned_images)
        + len(plan.unassigned_pdfs)
    )
    done_ops = 0

    def rep(frac: float, msg: Optional[str] = None) -> None:
        if progress is not None:
            progress(max(0.0, min(1.0, frac)), msg)

    if total_ops <= 0:
        rep(1.0, "Nincs mozgatandó/másolandó fájl.")
        return log
    rep(0.0, f"Előkészítés — {total_ops} fájl")

    def do_move(src: Path, dest_dir: Path) -> None:
        nonlocal done_ops
        dest = unique_dest(dest_dir, src.name)
        op(str(src), str(dest))
        log.append(("copy" if copy_mode else "move", src, dest))
        done_ops += 1
        rep(done_ops / total_ops, f"{done_ops}/{total_ops} — {src.name}")

    for seg in plan.segments:
        # Azonos név → ugyanaz a mappa (összevonás). A ``mkdir(exist_ok=True)`` miatt a már
        # létező közös mappába gyűlnek a fájlok; az ``unique_dest`` a fájlnév-ütközést kezeli.
        folder_name = safe_folder_name(seg.folder_key or "")
        base = output_root / folder_name
        jegy = base / "jegyzőkönyv"
        fotok = base / "fotók"
        jegy.mkdir(parents=True, exist_ok=True)
        fotok.mkdir(parents=True, exist_ok=True)
        for p in seg.pdfs:
            do_move(p, jegy)
        for p in seg.photos:
            do_move(p, fotok)

    if plan.unassigned_images or plan.unassigned_pdfs:
        ubase = output_root / UNASSIGNED
        uimg = ubase / "fotók"
        updf = ubase / "jegyzőkönyv"
        uimg.mkdir(parents=True, exist_ok=True)
        updf.mkdir(parents=True, exist_ok=True)
        for p in plan.unassigned_images:
            do_move(p, uimg)
        for p in plan.unassigned_pdfs:
            do_move(p, updf)

    return log
