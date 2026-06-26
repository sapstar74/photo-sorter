"""
TAG / mappa rendező — fémlap OCR + határoló kép — Streamlit felület.

Követelmény: rendszeren telepített Tesseract OCR
  macOS: brew install tesseract tesseract-lang
"""

from __future__ import annotations

import copy
import hashlib
import html
import logging
import os
import platform
import queue
import shutil
import tempfile
import threading
import time
import zipfile
from collections import defaultdict
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Literal

import streamlit as st
from PIL import Image, ImageOps

import metal_batch_logic as mbl
from folder_picker import ask_directory, ask_open_image_paths
from metal_batch_logic import (
    OrganizePlan,
    PlanScanCache,
    Segment,
    build_plan,
    execute_plan,
    folder_path_error_message,
    list_delimiter_followers_preview,
    list_sorted_media,
    normalize_user_path,
    replay_plan_from_cache,
    safe_folder_name,
    sort_media_paths_by_name_then_mtime,
    write_uploaded_media_to_dir,
)

st.set_page_config(page_title="TAG / mappa (fém + határoló)", layout="wide")


def _register_heif_opener() -> None:
    """
    HEIC/HEIF megnyitás bekapcsolása a PIL-be a ``pillow-heif`` segítségével.

    Modul-importkor **egyszer** fut, minden ``Image.open`` előtt. Működik macOS-en és
    Linuxon (Streamlit Cloud) is, mert a wheel bundle-öli a ``libheif``-et. Ha a csomag
    hiányzik, naplózunk és tovább működünk (a HEIC/HEIF fájlok ilyenkor kimaradnak),
    de a ``requirements.txt`` szerint telepítve kell lennie.
    """
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception as exc:  # ImportError vagy ritka init hiba
        logging.getLogger(__name__).warning(
            "pillow-heif nem elérhető (%s) — HEIC/HEIF fájlok kimaradhatnak.", exc
        )


_register_heif_opener()

_CACHED_DELIM_PREVIEW_SLOT = "_cached_step3_delim_preview_v1"
_CACHED_STEP2_TABLE_SLOT = "_cached_step2_delim_table_v1"
_STEP3_TAGS_BY_DELIM_KEY = "_step3_tags_by_delim_v1"
_STEP3_TAGS_BY_PLATE_KEY = "_step3_tags_by_plate_v1"
_STEP3_TAGS_BY_SEGMENT_KEY = "_step3_tags_by_segment_v1"
_DRAFT_DEMOTED_KEY = "_draft_demoted_delimiter_paths"
_STEP2_DEM_KEYMAP_PREFIX = "_step2_dem_key_to_path_"

_BROWSE_WARN_KEY = "_browse_barrier_msg_metal"
_PENDING_RERUN_SCOPE_KEY = "_pending_rerun_scope"
_STEP3_REFRESH_INFLIGHT_KEY = "_step3_refresh_inflight"
_STEP4_REBUILD_INFLIGHT_KEY = "_step4_rebuild_inflight"
_STEP5_EXEC_INFLIGHT_KEY = "_step5_exec_inflight"
# A legutóbbi sikeres válogatás összegzése — a terv „elfogyasztása” (pop) + rerun után
# a lapok ezt mutatják a félrevezető „készíts kiindulási tervet” üzenet helyett.
_SORT_COMPLETED_KEY = "_sort_completed_summary_v1"
_PREVIEW_THUMB_WIDTH = 84
_GALLERY_COL_THUMB_WIDTH = 128


def _request_rerun(*, scope: Literal["app", "fragment"] = "app") -> None:
    """on_click callback: st.rerun() a callbackben no-op — jelöld, futtasd fragment / main végén."""
    st.session_state[_PENDING_RERUN_SCOPE_KEY] = scope


def _flush_pending_rerun(*, only_scope: Literal["app", "fragment"] | None = None) -> None:
    scope = st.session_state.pop(_PENDING_RERUN_SCOPE_KEY, None)
    if scope is None:
        return
    if only_scope is not None and scope != only_scope:
        st.session_state[_PENDING_RERUN_SCOPE_KEY] = scope
        return
    st.rerun(scope=scope)


def _plan_required_notice() -> tuple[str, str]:
    """
    A lap-őrök (nincs élő terv) üzenete.

    Sikeres válogatás után a terv „elfogyott” (``_plan`` pop) és az app újrafut, ezért a
    ``_plan`` ``None``. Ilyenkor a korábbi „készíts kiindulási tervet” üzenet **félrevezető**
    (a futás valójában sikerült). Ha van ``_SORT_COMPLETED_KEY`` összegzés, **siker** üzenetet
    adunk vissza, különben a szokásos felszólítást.

    Vissza: ``(kind, message)`` ahol ``kind`` ∈ {``"success"``, ``"info"``}.
    """
    summary = st.session_state.get(_SORT_COMPLETED_KEY)
    if isinstance(summary, dict):
        ops = summary.get("ops")
        out = summary.get("out") or ""
        mode = "másolva" if summary.get("copy") else "áthelyezve"
        ops_txt = f"{ops} fájl " if isinstance(ops, int) else ""
        return (
            "success",
            f"✅ A válogatás befejeződött — {ops_txt}{mode} a(z) `{out}` mappába. "
            "Új sorozathoz készíts kiindulási tervet az **1 — Kiindulás** lapon.",
        )
    return (
        "info",
        "Előbb a **1 — Kiindulás** lapon készítsd el a kiindulási tervet.",
    )


def _render_plan_required_notice() -> None:
    """A ``_plan_required_notice`` szerint sikeres-válogatás vagy felszólító üzenet a lapokon."""
    kind, message = _plan_required_notice()
    if kind == "success":
        st.success(message)
    else:
        st.info(message)


def _clear_sort_completed_notice() -> None:
    st.session_state.pop(_SORT_COMPLETED_KEY, None)


def _path_image_file_ok(path: Path) -> bool:
    """Létező, nem üres képfájl (törölt / 0 bájtos útvonalak kiszűrése)."""
    if path.suffix.lower() not in mbl.IMAGE_SUFFIXES:
        return False
    try:
        if not path.is_file():
            return False
        if path.stat().st_size <= 0:
            return False
    except OSError:
        return False
    return True


def _pil_positive_size(img: Image.Image) -> bool:
    try:
        w, h = img.size
        return w > 0 and h > 0
    except Exception:
        return False


# A megjelenített képek mind miniatűrök (max. ``_GALLERY_COL_THUMB_WIDTH`` = 128 px széles),
# ezért a dekódolt forrást ekkora maximális élhosszra zsugorítjuk a cache-elés előtt: így a
# memória korlátos marad (egy nagy HEIC sem foglal 12 MP-nyi RGB-t), a megjelenítés minősége
# pedig változatlan (jóval a 128 px-es kijelzés fölött vágunk).
_THUMB_DECODE_MAX_DIM = 512


def _image_stat_sig(path: Path) -> tuple[int, int] | None:
    """A fájl identitása a cache kulcsához: (méret, mtime_ns). None, ha nem olvasható."""
    try:
        stt = path.stat()
    except OSError:
        return None
    return (int(stt.st_size), int(stt.st_mtime_ns))


@st.cache_data(show_spinner=False, max_entries=2048)
def _decode_thumb_rgb_cached(path_str: str, stat_sig: tuple[int, int]) -> Image.Image | None:
    """
    Egy útvonal RGB miniatűrje **cache-elve** — a kulcs az útvonal + (méret, mtime_ns), így a
    dekódolás (HEIC esetén kifejezetten drága) **menetenként egyszer** fut, nem minden
    Streamlit rerunkor / minden szegmensnél újra. A ``stat_sig`` változására (a fájl módosul)
    a cache automatikusan érvénytelenít, így nincs elavult miniatűr.
    """
    try:
        with Image.open(path_str) as im:
            im = ImageOps.exif_transpose(im)
            rgb = im.convert("RGB")
            if not _pil_positive_size(rgb):
                return None
            rgb.thumbnail((_THUMB_DECODE_MAX_DIM, _THUMB_DECODE_MAX_DIM))
            if not _pil_positive_size(rgb):
                return None
            return rgb
    except Exception:
        return None


def _load_rgb_image(path: Path) -> Image.Image | None:
    if not _path_image_file_ok(path):
        return None
    sig = _image_stat_sig(path)
    if sig is None:
        return None
    return _decode_thumb_rgb_cached(str(path), sig)


def _load_rgb_from_bytes(data: bytes) -> Image.Image | None:
    if not data:
        return None
    try:
        with Image.open(BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            rgb = im.convert("RGB")
            if not _pil_positive_size(rgb):
                return None
            return rgb.copy()
    except Exception:
        return None


def _safe_st_image_pil(
    img: Image.Image,
    *,
    caption: str | None = None,
    width: int | None = None,
    use_container_width: bool = False,
) -> bool:
    """st.image csak érvényes méretű képpel; nem használ érvénytelen width stringet."""
    if not _pil_positive_size(img):
        return False
    thumb_w = width if (isinstance(width, int) and width > 0) else _PREVIEW_THUMB_WIDTH
    kwargs: dict = {}
    if caption is not None:
        kwargs["caption"] = caption
    attempts: list[dict] = []
    if use_container_width:
        attempts.append({"use_container_width": True})
    if isinstance(width, int) and width > 0:
        attempts.append({"width": width})
    attempts.append({"width": thumb_w})
    attempts.append({})  # natív méret — utolsó esély
    seen: set[tuple] = set()
    for extra in attempts:
        key = tuple(sorted(extra.items()))
        if key in seen:
            continue
        seen.add(key)
        try:
            st.image(img, **extra, **kwargs)
            return True
        except Exception:
            continue
    return False


def _safe_st_image_path(
    path: Path,
    *,
    caption: str | None = None,
    width: int | None = None,
    use_container_width: bool = False,
    missing_label: str | None = None,
) -> bool:
    """Útvonalból betölt és megjelenít; False = kihagyva (törölt / sérült)."""
    img = _load_rgb_image(path)
    if img is None:
        if missing_label is not None:
            st.caption(missing_label)
        return False
    return _safe_st_image_pil(
        img,
        caption=caption,
        width=width,
        use_container_width=use_container_width,
    )


def _flush_browse_warning_metal() -> None:
    msg = st.session_state.pop(_BROWSE_WARN_KEY, None)
    if msg:
        st.warning(msg)


def _browse_folder_apply(state_key: str, dialog_title: str) -> None:
    """Csak st.button(on_click=...) hívja: a callback a text_input előtt fut, így szabad a state írása."""
    st.session_state.pop(_BROWSE_WARN_KEY, None)
    picked = ask_directory(dialog_title)
    if picked is None:
        st.session_state[_BROWSE_WARN_KEY] = (
            "A mappaválasztó nem érhető el ezen a környezeten (nincs kijelző vagy hiányzó eszköz). "
            "Írd be kézzel az útvonalat."
        )
    elif picked:
        st.session_state[state_key] = str(normalize_user_path(picked))


def _folder_path_row(
    label: str,
    state_key: str,
    button_key: str,
    dialog_title: str,
) -> None:
    """Szövegmező + Tallózás gomb (macOS: Finder; egyébként rendszer tallózó / külön folyamat)."""
    c_text, c_btn = st.columns([6, 1])
    with c_text:
        st.text_input(
            label,
            key=state_key,
            placeholder="/teljes/út/a/mappához",
            help="Beírhatod kézzel is, vagy válaszd ki a Tallózással.",
        )
    with c_btn:
        st.markdown('<div style="height: 1.6rem"></div>', unsafe_allow_html=True)
        st.button(
            "Tallózás…",
            key=button_key,
            help="Rendszer mappaválasztó",
            on_click=_browse_folder_apply,
            args=(state_key, dialog_title),
        )


def _delimiter_temp_from_upload(uploaded) -> Path:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=Path(uploaded.name).suffix)
    tmp.write(uploaded.getbuffer())
    tmp.close()
    return Path(tmp.name)


# --- Felhő / headless (upload-alapú) mód --------------------------------------
# Streamlit Community Cloudon nincs helyi lemez-hozzáférés és nincs natív Finder/zenity.
# Ilyenkor a felhasználó képeket + PDF-eket TÖLT FEL; ezeket egy ideiglenes munkamappába
# írjuk, és a meglévő pipeline ezt a temp mappát kapja „forrásként”. A végén a rendezett
# kimenetet ZIP-be csomagoljuk és letölthetővé tesszük (nincs helyi áthelyezés).

_INPUT_MODE_KEY = "metal_input_mode"
_INPUT_MODE_LOCAL = "Helyi mappa"
_INPUT_MODE_UPLOAD = "Képek feltöltése"
_UPLOAD_SRC_DIR_KEY = "_upload_src_dir"
_UPLOAD_SRC_SIG_KEY = "_upload_src_sig"
_CLOUD_ZIP_RESULT_KEY = "_cloud_zip_result_v1"
_CLOUD_SRC_TYPES = ["jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff", "heic", "heif", "pdf"]


def _native_browse_available() -> bool:
    """Van-e használható natív mappa/fájl tallózó (különben felhő/headless → feltöltés)."""
    system = platform.system()
    if system == "Darwin":
        return shutil.which("osascript") is not None
    if system == "Linux":
        return bool(os.environ.get("DISPLAY")) or shutil.which("zenity") is not None
    if system == "Windows":
        return True
    return False


def _default_upload_mode() -> bool:
    """Alapértelmezett bemeneti mód: felhőben/headless feltöltés, helyi gépen mappa."""
    force = (os.environ.get("PHOTO_SORTER_INPUT_MODE") or "").strip().lower()
    if force in ("upload", "cloud", "feltoltes"):
        return True
    if force in ("local", "helyi", "folder"):
        return False
    return not _native_browse_available()


def _is_upload_mode() -> bool:
    cur = st.session_state.get(_INPUT_MODE_KEY)
    if cur in (_INPUT_MODE_LOCAL, _INPUT_MODE_UPLOAD):
        return cur == _INPUT_MODE_UPLOAD
    return _default_upload_mode()


def _render_input_mode_selector() -> bool:
    """Bemeneti mód választó (radio). Vissza: True ha feltöltés-mód aktív."""
    if _INPUT_MODE_KEY not in st.session_state:
        st.session_state[_INPUT_MODE_KEY] = (
            _INPUT_MODE_UPLOAD if _default_upload_mode() else _INPUT_MODE_LOCAL
        )
    choice = st.radio(
        "Bemenet módja",
        [_INPUT_MODE_LOCAL, _INPUT_MODE_UPLOAD],
        key=_INPUT_MODE_KEY,
        horizontal=True,
        help=(
            "**Helyi mappa**: a gép egy mappáját olvassa, a fájlokat helyben mozgatja/másolja "
            "(asztali használat). **Képek feltöltése**: felhőben / headless környezethez — "
            "feltöltöd a képeket/PDF-eket, az eredményt ZIP-ben töltöd le."
        ),
    )
    return choice == _INPUT_MODE_UPLOAD


def _persist_uploaded_source_media(files) -> str:
    """
    Feltöltött forrás-médiát ideiglenes munkamappába ír (rerun-stabil: ugyanarra a
    feltöltés-halmazra ugyanazt a mappát adja vissza). Vissza: a temp mappa útvonala.
    """
    sig = tuple((getattr(f, "name", ""), int(getattr(f, "size", 0) or 0)) for f in files)
    cur_dir = st.session_state.get(_UPLOAD_SRC_DIR_KEY)
    cur_sig = st.session_state.get(_UPLOAD_SRC_SIG_KEY)
    if cur_dir and cur_sig == sig and Path(cur_dir).is_dir():
        return cur_dir
    if cur_dir:
        shutil.rmtree(cur_dir, ignore_errors=True)
    dest = Path(tempfile.mkdtemp(prefix="photo_sorter_src_"))
    write_uploaded_media_to_dir(((f.name, f.getvalue()) for f in files), dest)
    st.session_state[_UPLOAD_SRC_DIR_KEY] = str(dest)
    st.session_state[_UPLOAD_SRC_SIG_KEY] = sig
    return str(dest)


def _ensure_cloud_out_dir_placeholder() -> str:
    """
    Feltöltés-módban a cél „gyökér” egy ideiglenes mappa (a helyi cél-útvonal mező nem létezik).
    Csak placeholder az 1. lépés validációjához — a tényleges kimenet a végrehajtáskor friss temp
    mappába készül, majd ZIP-ként tölthető le.
    """
    d = st.session_state.get("_cloud_out_dir")
    if d and Path(d).is_dir():
        return d
    d = tempfile.mkdtemp(prefix="photo_sorter_out_")
    st.session_state["_cloud_out_dir"] = d
    return d


def _zip_directory_bytes(root: Path) -> bytes:
    """A rendezett kimeneti mappát ZIP-be csomagolja (relatív útvonalakkal)."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(root))
    return buf.getvalue()


def _clear_cloud_zip_result() -> None:
    st.session_state.pop(_CLOUD_ZIP_RESULT_KEY, None)


def _render_cloud_zip_download() -> None:
    """Ha van kész ZIP eredmény (feltöltés-mód), megjeleníti a letöltés gombot (rerun után is)."""
    z = st.session_state.get(_CLOUD_ZIP_RESULT_KEY)
    if not isinstance(z, dict) or not z.get("data"):
        return
    ops = z.get("ops")
    ops_txt = f" — {ops} fájl rendezve" if isinstance(ops, int) else ""
    st.success(f"✅ A rendezés elkészült{ops_txt}. Töltsd le a ZIP-et a mappaszerkezettel.")
    st.download_button(
        "Rendezett mappák letöltése (ZIP)",
        data=z["data"],
        file_name=z.get("name", "rendezett_mappak.zip"),
        mime="application/zip",
        type="primary",
        key="btn_cloud_zip_download",
    )


def _clear_step3_tag_override_snapshots() -> None:
    st.session_state.pop(_STEP3_TAGS_BY_DELIM_KEY, None)
    st.session_state.pop(_STEP3_TAGS_BY_PLATE_KEY, None)
    st.session_state.pop(_STEP3_TAGS_BY_SEGMENT_KEY, None)


def _clear_segment_ocr_widget_keys() -> None:
    for k in list(st.session_state.keys()):
        if not isinstance(k, str):
            continue
        if (
            k.startswith("seg_ocr_raw_")
            or k.startswith("seg_map_folder_")
            or k.startswith("step3_tag_ocr_")
            or k.startswith("step3_map_folder_")
            or k.startswith(_SEGMENT_NAME_WIDGET_PREFIX)
        ):
            del st.session_state[k]


def _clear_plan_exclude_multiselect_keys() -> None:
    """Új terv / terv törlése után, hogy a multiselect kulcsok ne ütközzenek."""
    for k in list(st.session_state.keys()):
        if isinstance(k, str) and k.startswith("exc_sel_"):
            del st.session_state[k]


def _clear_demoted_delimiter_paths() -> None:
    """Új terv / terv törlése: a kézi határoló-kizárások listája és a „piszkos” jelző."""
    st.session_state.pop("_demoted_delimiter_paths", None)
    st.session_state.pop("_delimiter_demotions_dirty", None)
    st.session_state.pop(_DRAFT_DEMOTED_KEY, None)
    _clear_step2_demotion_checkbox_keys()
    _clear_step3_demotion_checkbox_keys()


def _clear_forced_delimiter_paths() -> None:
    st.session_state.pop("_forced_delimiter_paths", None)


def _bump_plan_generation() -> None:
    st.session_state["_plan_generation"] = int(st.session_state.get("_plan_generation", 0)) + 1
    _clear_step2_demotion_checkbox_keys()
    _clear_step3_demotion_checkbox_keys()


def _clear_step2_demotion_checkbox_keys() -> None:
    """Régi 2. lapos kulcsok (kompatibilitás)."""
    for k in list(st.session_state.keys()):
        if isinstance(k, str) and (
            k.startswith("step2_dem_chk_") or k.startswith(_STEP2_DEM_KEYMAP_PREFIX)
        ):
            del st.session_state[k]


def _clear_step3_demotion_checkbox_keys() -> None:
    """Régi 3. lapos checkbox kulcsok (kompatibilitás)."""
    for k in list(st.session_state.keys()):
        if isinstance(k, str) and k.startswith("step3_dem_chk_"):
            del st.session_state[k]


def demoted_paths_from_delimiter_paths(
    paths: list[Path],
    *,
    is_demoted: Callable[[str], bool],
) -> list[str]:
    """Mely útvonalak pipált „nem határoló” (tesztelhető Streamlit nélkül)."""
    dem_out: list[str] = []
    for p in paths:
        ns = _norm_path_str(p)
        if is_demoted(ns):
            dem_out.append(ns)
    return sorted(set(dem_out))


def _step2_dem_checkbox_key(gen: int, ns: str) -> str:
    slug = hashlib.md5(ns.encode("utf-8")).hexdigest()[:12]
    return f"step2_dem_chk_{gen}_{slug}"


def _step2_dem_keymap_session_key(gen: int) -> str:
    return f"{_STEP2_DEM_KEYMAP_PREFIX}{gen}"


def _set_step2_dem_keymap(gen: int, keymap: dict[str, str]) -> None:
    st.session_state[_step2_dem_keymap_session_key(gen)] = dict(keymap)


def _get_step2_dem_keymap(gen: int) -> dict[str, str]:
    raw = st.session_state.get(_step2_dem_keymap_session_key(gen))
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str):
            out[k] = v
    return out


def _demoted_paths_from_step2_keymap(gen: int) -> list[str]:
    """2. lépés checkbox állapotok olvasása mappabejárás nélkül."""
    keymap = _get_step2_dem_keymap(gen)
    if not keymap:
        return []
    out: list[str] = []
    for chk_key, ns in keymap.items():
        if bool(st.session_state.get(chk_key, False)):
            out.append(ns)
    return sorted(set(out))


def _step3_dem_checkbox_key(gen: int, ns: str) -> str:
    slug = hashlib.md5(ns.encode("utf-8")).hexdigest()[:12]
    return f"step3_dem_chk_{gen}_{slug}"


def _demoted_paths_from_step2_widgets(gen: int, paths: list[Path]) -> list[str]:
    visible_ns = {_norm_path_str(p) for p in paths}
    committed = {_norm_path_str(x) for x in (st.session_state.get("_demoted_delimiter_paths") or [])}
    hidden_committed = committed - visible_ns
    from_map = _demoted_paths_from_step2_keymap(gen)
    if from_map:
        return sorted(set(from_map) | hidden_committed)
    from_widgets = demoted_paths_from_delimiter_paths(
        paths,
        is_demoted=lambda ns: bool(
            st.session_state.get(_step2_dem_checkbox_key(gen, ns), False)
        ),
    )
    return sorted(set(from_widgets) | hidden_committed)


def _step2_demotion_widgets_active(gen: int) -> bool:
    prefix = f"step2_dem_chk_{gen}_"
    return any(isinstance(k, str) and k.startswith(prefix) for k in st.session_state)


def _demoted_paths_for_step3_finalize(gen: int, paths: list[Path]) -> list[str]:
    """3. lépés véglegesítés: élő 2. lépés pipák, vagy már mentett / draft lista."""
    if paths and _step2_demotion_widgets_active(gen):
        return _demoted_paths_from_step2_widgets(gen, paths)
    committed = sorted(
        {_norm_path_str(x) for x in (st.session_state.get("_demoted_delimiter_paths") or [])}
    )
    if committed:
        return committed
    draft = st.session_state.get(_DRAFT_DEMOTED_KEY)
    if isinstance(draft, list) and draft:
        return sorted({_norm_path_str(x) for x in draft})
    return []


def _delimiter_demotion_pending_for_step3_finalize(gen: int, paths: list[Path]) -> bool:
    if not paths:
        return False
    if _step2_demotion_widgets_active(gen):
        return _delimiter_demotion_pending_step2(gen, paths)
    draft = st.session_state.get(_DRAFT_DEMOTED_KEY)
    if isinstance(draft, list):
        committed = {_norm_path_str(x) for x in (st.session_state.get("_demoted_delimiter_paths") or [])}
        return {_norm_path_str(x) for x in draft} != committed
    return False


def _sync_draft_demotions_mirror_step2(gen: int, paths: list[Path]) -> None:
    st.session_state[_DRAFT_DEMOTED_KEY] = _demoted_paths_from_step2_widgets(gen, paths)


def _delimiter_demotion_pending_step2(gen: int, paths: list[Path]) -> bool:
    draft = set(_demoted_paths_from_step2_widgets(gen, paths))
    committed = {_norm_path_str(x) for x in (st.session_state.get("_demoted_delimiter_paths") or [])}
    return draft != committed


def _commit_delimiter_demotion_widgets(
    dem_uid: list[str],
    *,
    clear_step2_keys: bool = False,
    clear_step3_keys: bool = False,
    bust_preview_cache: bool = True,
) -> None:
    """Pipák → érvényesített nem-határoló lista (hash/OCR/terv nélkül)."""
    prev_dem = sorted({_norm_path_str(x) for x in (st.session_state.get("_demoted_delimiter_paths") or [])})
    prev_forced = sorted({_norm_path_str(x) for x in (st.session_state.get("_forced_delimiter_paths") or [])})
    st.session_state["_demoted_delimiter_paths"] = dem_uid
    st.session_state.pop(_DRAFT_DEMOTED_KEY, None)
    forced = list(st.session_state.get("_forced_delimiter_paths") or [])
    dset = set(dem_uid)
    new_forced = [x for x in forced if _norm_path_str(x) not in dset]
    st.session_state["_forced_delimiter_paths"] = new_forced
    new_dem = sorted({_norm_path_str(x) for x in dem_uid})
    new_forced_norm = sorted({_norm_path_str(x) for x in new_forced})
    if new_dem != prev_dem or new_forced_norm != prev_forced:
        st.session_state["_delimiter_demotions_dirty"] = True
    if clear_step2_keys:
        _clear_step2_demotion_checkbox_keys()
    if clear_step3_keys:
        _clear_step3_demotion_checkbox_keys()
    if bust_preview_cache:
        bust_step3_delimiter_preview_cache()


def _apply_step2_delimiter_demotion_checkboxes() -> None:
    """2. lépés: pipák mentése — csak session + előnézet-cache, nem terv/hash."""
    plan = st.session_state.get("_plan")
    if not isinstance(plan, OrganizePlan):
        return
    paths, _ = _get_delimiter_table_paths(plan)
    gen = int(st.session_state.get("_plan_generation", 0))
    if not _delimiter_demotion_pending_step2(gen, paths):
        return
    dem_uid = _demoted_paths_from_step2_widgets(gen, paths)
    _commit_delimiter_demotion_widgets(dem_uid, clear_step2_keys=True)
    _request_rerun(scope="fragment")


def _apply_step2_remove_selected_from_list() -> None:
    """2. lépés: kijelöltek eltávolítása a határoló-listából (session + preview cache)."""
    st.session_state.pop("_step2_delete_confirm_pending", None)
    st.session_state.pop("_step2_delete_feedback", None)
    _apply_step2_delimiter_demotion_checkboxes()


def _apply_step3_refresh_delimiter_images() -> None:
    """3. lépés — Képek frissítése: 2. lépés pipái → érvényesített lista + előnézet-cache törlés."""
    if st.session_state.get(_STEP3_REFRESH_INFLIGHT_KEY):
        return
    st.session_state[_STEP3_REFRESH_INFLIGHT_KEY] = True
    plan = st.session_state.get("_plan")
    if not isinstance(plan, OrganizePlan):
        st.session_state["_step3_refresh_feedback"] = (
            "error",
            "Nincs kiindulási terv — előbb a **1 — Kiindulás** lapon készíts tervet.",
        )
        st.session_state[_STEP3_REFRESH_INFLIGHT_KEY] = False
        return
    gen = int(st.session_state.get("_plan_generation", 0))
    paths: list[Path] = []
    keymap = _get_step2_dem_keymap(gen)
    if keymap:
        paths = [Path(ns) for ns in keymap.values()]
    elif _step2_demotion_widgets_active(gen):
        # Régebbi munkameneteknél még nincs keymap eltárolva.
        paths, _ = _get_delimiter_table_paths(plan)
    dem_uid = _demoted_paths_for_step3_finalize(gen, paths)
    had_pending = _delimiter_demotion_pending_for_step3_finalize(gen, paths)
    prev_dem = sorted({_norm_path_str(x) for x in (st.session_state.get("_demoted_delimiter_paths") or [])})
    changed = dem_uid != prev_dem
    if not paths:
        st.session_state["_step3_refresh_feedback"] = (
            "info",
            "Nincs határoló kép a listában — az előnézet-cache törölve.",
        )
    elif not dem_uid and not had_pending:
        st.session_state["_step3_refresh_feedback"] = (
            "info",
            "Nincs **nem határoló** jelölés a 2. lépésen — az előnézet frissítve (cache törölve).",
        )
    else:
        st.session_state["_step3_refresh_feedback"] = (
            "success",
            f"Érvényesítve: **{len(dem_uid)}** nem határoló jelölés; határoló/követő előnézet és TAG mezők frissítve.",
        )
    should_commit = changed or had_pending
    if should_commit:
        _commit_delimiter_demotion_widgets(
            dem_uid,
            clear_step2_keys=_step2_demotion_widgets_active(gen),
            clear_step3_keys=True,
            bust_preview_cache=True,
        )
        _request_rerun(scope="app")
        return
    st.session_state[_STEP3_REFRESH_INFLIGHT_KEY] = False


def _clear_plan_scan_cache() -> None:
    st.session_state.pop("_plan_scan_cache", None)
    st.session_state.pop(_CACHED_DELIM_PREVIEW_SLOT, None)
    bust_step2_delimiter_table_cache()


def _rebuild_plan_core(
    tmp_path: Path,
    src_exp: Path,
    skip: list[str],
    force: list[str],
    *,
    plan_scan_cache: PlanScanCache | None,
    use_cache_replay: bool,
    max_hamming: int,
    recursive: bool,
    delimiter_inner_ratio: float,
    progress: Callable[[float, str | None], None] | None,
) -> tuple[OrganizePlan, PlanScanCache | None]:
    """
    Terv újraszámolás Streamlit nélkül (háttérszálban hívható).
    Visszaadja: (terv, új PlanScanCache vagy None ha cache-replay volt / üres holder).
    """
    if use_cache_replay and isinstance(plan_scan_cache, PlanScanCache):
        try:
            p = replay_plan_from_cache(
                plan_scan_cache,
                non_delimiter_paths=skip,
                force_delimiter_paths=force,
                progress=progress,
            )
            return p, None
        except Exception:
            pass
    holder: list = []
    p = build_plan(
        src_exp,
        tmp_path,
        max_hamming=max_hamming,
        recursive=recursive,
        delimiter_inner_ratio=delimiter_inner_ratio,
        non_delimiter_paths=skip,
        force_delimiter_paths=force,
        scan_cache_holder=holder,
        progress=progress,
    )
    return p, (holder[0] if holder else None)


def _scan_cache_matches_session(cache: PlanScanCache) -> bool:
    src = (st.session_state.get("_src") or "").strip()
    if not src:
        return False
    src_exp = str(_user_folder_path(src))
    mh = int(st.session_state.get("metal_max_hamming", 18))
    rec = bool(st.session_state.get("metal_recursive_chk", st.session_state.get("_recursive", False)))
    din = float(st.session_state.get("metal_del_inner", 0.92))
    return (
        cache.source_str == src_exp
        and cache.recursive == rec
        and cache.max_hamming == mh
        and abs(cache.inner_ratio - din) < 1e-9
    )


def _drain_progress_queue(
    th: threading.Thread,
    q: queue.Queue,
    progress: Callable[[float, str | None], None],
    *,
    abort_message: str = "A háttérfeldolgozás váratlanul megszakadt.",
) -> bool:
    """
    A háttérszál ``("p", frac, msg?)`` és végén ``("done",)`` üzeneteket tesz a queue-ba.
    Igaz, ha a ``done`` üzenet megérkezett.
    """
    while True:
        try:
            item = q.get(timeout=0.2)
        except queue.Empty:
            if not th.is_alive() and q.empty():
                st.error(abort_message)
                return False
            continue
        if not item:
            continue
        if item[0] == "done":
            return True
        if item[0] == "p" and len(item) >= 2:
            frac = float(item[1])
            msg = item[2] if len(item) > 2 else None
            progress(frac, msg if isinstance(msg, str) else None)
            time.sleep(0.05)
    return False


def _rebuild_plan_with_demotions(
    non_delimiter_paths: list[str] | None = None,
    *,
    progress: Callable[[float, str | None], None] | None = None,
) -> OrganizePlan | None:
    """
    A sessionben tárolt referencia-bájtokkal és forrással újraszámolja a tervet.
    ``non_delimiter_paths``: ezek az útvonalak nem számítanak határolónak;
    ``None`` esetén a ``_demoted_delimiter_paths`` session lista.

    Ha ``progress`` meg van adva, a nehéz számítás **háttérszálban** fut, hogy a fő szálon
    a Streamlit progress sáv közben is frissüljön (egy futásbeli batching nélkül).
    """
    raw = st.session_state.get("_del_bytes")
    if not raw:
        st.error(
            "Nincs eltárolt határoló referencia a munkamenetben. "
            "Töltsd fel újra a referenciát, és futtasd az **1 — Kiindulás** lapon a tervkészítést."
        )
        return None
    src = (st.session_state.get("_src") or "").strip()
    if not src:
        st.error("Hiányzik a forrás útvonal a munkamenetből.")
        return None
    name = st.session_state.get("_del_name") or "delimiter_ref.jpg"
    suf = Path(name).suffix or ".jpg"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suf)
    tmp.write(raw)
    tmp.close()
    tmp_path = Path(tmp.name)
    try:
        if non_delimiter_paths is None:
            skip = list(st.session_state.get("_demoted_delimiter_paths", []))
        else:
            skip = list(non_delimiter_paths)
        force = list(st.session_state.get("_forced_delimiter_paths", []))

        cache = st.session_state.get("_plan_scan_cache")
        use_replay = isinstance(cache, PlanScanCache) and _scan_cache_matches_session(cache)
        max_hamming = int(st.session_state.get("metal_max_hamming", 18))
        recursive = bool(
            st.session_state.get("metal_recursive_chk", st.session_state.get("_recursive", False))
        )
        inner_ratio = float(st.session_state.get("metal_del_inner", 0.92))
        src_exp = _user_folder_path(src)

        if progress is None:
            try:
                plan, new_cache = _rebuild_plan_core(
                    tmp_path,
                    src_exp,
                    skip,
                    force,
                    plan_scan_cache=cache if isinstance(cache, PlanScanCache) else None,
                    use_cache_replay=use_replay,
                    max_hamming=max_hamming,
                    recursive=recursive,
                    delimiter_inner_ratio=inner_ratio,
                    progress=None,
                )
            except RuntimeError as e:
                st.error(str(e))
                return None
            except ValueError as e:
                st.error(str(e))
                return None
            except Exception as e:
                st.exception(e)
                return None
            if new_cache is not None:
                st.session_state["_plan_scan_cache"] = new_cache
            return plan

        q: queue.Queue[tuple[str, ...]] = queue.Queue()
        outcome: list[tuple[str, ...]] = []

        def worker() -> None:
            try:

                def _enqueue_prog(f: float, m: str | None = None) -> None:
                    q.put(("p", f, m if m is None else str(m)))

                plan, new_cache = _rebuild_plan_core(
                    tmp_path,
                    src_exp,
                    skip,
                    force,
                    plan_scan_cache=cache if isinstance(cache, PlanScanCache) else None,
                    use_cache_replay=use_replay,
                    max_hamming=max_hamming,
                    recursive=recursive,
                    delimiter_inner_ratio=inner_ratio,
                    progress=_enqueue_prog,
                )
                outcome.append(("ok", plan, new_cache))
            except RuntimeError as e:
                outcome.append(("err", str(e)))
            except ValueError as e:
                outcome.append(("err", str(e)))
            except Exception as e:
                outcome.append(("exc", e))
            finally:
                q.put(("done",))

        th = threading.Thread(target=worker, daemon=True, name="metal_rebuild_plan")
        th.start()
        if not _drain_progress_queue(
            th,
            q,
            progress,
            abort_message="A terv újraszámolása váratlanul megszakadt.",
        ):
            return None

        th.join(timeout=3600)
        if not outcome:
            st.error("A terv újraszámolása nem adott vissza eredményt.")
            return None
        tag = outcome[0][0]
        if tag == "err":
            st.error(str(outcome[0][1]))
            return None
        if tag == "exc":
            ex = outcome[0][1]
            if isinstance(ex, BaseException):
                st.exception(ex)
            else:
                st.error(str(ex))
            return None
        _plan: OrganizePlan = outcome[0][1]  # type: ignore[assignment]
        new_cache = outcome[0][2]
        if new_cache is not None:
            st.session_state["_plan_scan_cache"] = new_cache
        return _plan
    finally:
        tmp_path.unlink(missing_ok=True)


def _apply_photo_exclusions_to_plan(plan: OrganizePlan) -> OrganizePlan:
    """
    A multiselectben kijelölt képek kiesnek a végrehajtásból (nem mozognak / másolódnak).
    Ha egy TAG/mappa szegmensnek minden képe és PDF-je kiesik, a szegmens törlődik a térből.
    """
    p = copy.deepcopy(plan)
    new_segments: list[Segment] = []
    for i, seg in enumerate(p.segments):
        key = f"exc_sel_seg_{i}"
        skip = st.session_state.get(key) or []
        skip_set = {s for s in skip} if isinstance(skip, list) else set()
        photos = [x for x in seg.photos if str(x) not in skip_set]
        pdfs = list(seg.pdfs)
        if not photos and not pdfs:
            continue
        plate = seg.plate_image
        if plate not in photos and photos:
            plate = photos[0]
        new_segments.append(
            Segment(
                folder_key=seg.folder_key,
                plate_image=plate,
                ocr_raw=seg.ocr_raw,
                photos=photos,
                pdfs=pdfs,
                closed_by_delimiter=seg.closed_by_delimiter,
            )
        )
    p.segments = new_segments

    ukey = "exc_sel_unassigned"
    uskip = st.session_state.get(ukey) or []
    uset = {s for s in uskip} if isinstance(uskip, list) else set()
    p.unassigned_images = [x for x in p.unassigned_images if str(x) not in uset]

    return p


def segment_was_manually_renamed(
    seg: Segment,
    *,
    original_ocr: str | None,
    tag_by_segment: dict[str, str] | None = None,
) -> bool:
    """
    Igaz, ha a szegmenshez tartozik **kézi** mappanév-felülírás.

    Az új tervezésben az alapértelmezett név **sorszám** (nem OCR), a stabil mentés
    (``_STEP3_TAGS_BY_SEGMENT_KEY``) pedig **kizárólag a kézi** (a sorszámtól eltérő) neveket
    tartalmazza. Ezért az „átnevezett” jelleg megbízható és sorszám-független jele az, ha a
    szegmens identitásához van mentett felülírás. (Az ``original_ocr`` paraméter csak
    visszafelé-kompatibilitásból maradt; az OCR-szöveg már nem névforrás.)
    """
    by_segment = tag_by_segment or {}
    for sid in _segment_identity_keys(seg):
        if _normalize_tag_text(by_segment.get(sid), ""):
            return True
    return False


def select_execution_segments(
    plan: OrganizePlan,
    *,
    tag_by_segment: dict[str, str] | None = None,
    original_ocr_by_plate: dict[str, str] | None = None,
    drop_unedited_delimiterless: bool = False,
) -> list[Segment]:
    """
    A végrehajtási tervbe (5. lépés) kerülő szegmensek kiválasztása — **központi szerződés**,
    hogy a határoló nélküli, **kézzel átnevezett** mappák soha ne essenek ki.

    Szabályok:
      * minden **határolóval lezárt** szegmens (``closed_by_delimiter is not None``) bekerül;
      * minden **kézzel átnevezett**, határoló nélküli szegmens bekerül
        (``segment_was_manually_renamed`` az eredeti OCR névhez képest);
      * ``drop_unedited_delimiterless=False`` (alapértelmezés): a határoló nélküli, **át nem
        nevezett** szegmensek is megmaradnak — a korábbi viselkedés megőrzése. ``True`` esetén
        ezek kiesnek (csak akkor, ha kifejezetten ezt kérik).

    ``original_ocr_by_plate``: fémlap-útvonal → eredeti OCR/auto név (pl. a terv cache
    ``ocr_by_path`` táblájából); ennek alapján dönthető el megbízhatóan az „átnevezett” jelleg
    akkor is, ha a terv ``ocr_raw``-ja már a felülírt nevet hordozza.
    """
    by_plate = original_ocr_by_plate or {}
    out: list[Segment] = []
    for seg in plan.segments:
        if seg.closed_by_delimiter is not None:
            out.append(seg)
            continue
        if not drop_unedited_delimiterless:
            out.append(seg)
            continue
        original = by_plate.get(_norm_path_str(seg.plate_image))
        if segment_was_manually_renamed(
            seg, original_ocr=original, tag_by_segment=tag_by_segment
        ):
            out.append(seg)
    return out


def _original_ocr_by_plate_from_cache() -> dict[str, str]:
    """A terv cache ``ocr_by_path`` táblája fémlap-útvonal → eredeti OCR névként (ha van)."""
    cache = st.session_state.get("_plan_scan_cache")
    if not isinstance(cache, PlanScanCache):
        return {}
    out: dict[str, str] = {}
    for p, raw in (cache.ocr_by_path or {}).items():
        if isinstance(raw, str) and raw.strip():
            out[_norm_path_str(p)] = raw
    return out


# Galéria max. miniatűr / sor (a határoló-előnézet ``following_max``-szel összhangban).
_STEP3_FOLDER_PHOTO_PREVIEW_MAX = 24


def _step3_images_between_delimiter_row_and_next(
    dix: int,
    preview_rows: list[tuple[Path, list[Path]]],
    files_ordered: list[Path],
    *,
    index_map: dict[str, int] | None = None,
) -> list[Path]:
    """
    Képek a fájlsorrendben: **a jelenlegi határoló után**, egészen **a következő határoló előtt**.
    A két határoló kép **nincs** benne; PDF kihagyva (csak képformátum).
    """
    if dix >= len(preview_rows):
        return []
    delim, _ = preview_rows[dix]
    next_delim = preview_rows[dix + 1][0] if dix + 1 < len(preview_rows) else None
    nd = _norm_path_str(delim)
    loc = index_map if index_map is not None else _ordered_file_index_map(files_ordered)
    i0 = loc.get(nd)
    if i0 is None:
        return []
    i1 = len(files_ordered)
    if next_delim is not None:
        nn = _norm_path_str(next_delim)
        i1 = loc.get(nn, i1)
    out: list[Path] = []
    for k in range(i0 + 1, i1):
        p = files_ordered[k]
        if p.suffix.lower() in mbl.IMAGE_SUFFIXES:
            out.append(p)
    return out


def _ordered_file_index_map(files_ordered: list[Path]) -> dict[str, int]:
    """Normalizált útvonal -> index gyors lekérdezés a 3. lépés sáv-képeihez."""
    return {_norm_path_str(p): i for i, p in enumerate(files_ordered)}


def _render_step3_folder_photos_expander(
    seg: Segment,
    *,
    dix: int | None = None,
    preview_rows: list[tuple[Path, list[Path]]] | None = None,
    files_ordered: list[Path] | None = None,
    index_map: dict[str, int] | None = None,
) -> None:
    """
    Képek a mappában: határoló-sor esetén a **következő határolóig** tartó sáv (a következő határoló nélkül);
    párosítatlan szegmensnél a terv ``seg.photos`` listája.
    """
    if dix is not None and preview_rows is not None and files_ordered:
        photos = _step3_images_between_delimiter_row_and_next(
            dix,
            preview_rows,
            files_ordered,
            index_map=index_map,
        )
        use_interval = True
    else:
        photos = list(seg.photos)
        use_interval = False

    n = len(photos)
    prev = min(n, _STEP3_FOLDER_PHOTO_PREVIEW_MAX)
    if use_interval:
        title = (
            f"Összes kép a mappában — {n} kép (jelen határoló után, következő határoló előtt; "
            f"galéria: első {prev}, max. {_STEP3_FOLDER_PHOTO_PREVIEW_MAX})"
        )
    else:
        title = (
            f"Összes kép a mappában — {n} kép (terv szerinti szegmens; galéria: első {prev}, max. {_STEP3_FOLDER_PHOTO_PREVIEW_MAX})"
        )
    with st.expander(title, expanded=False):
        if not photos:
            st.caption("Nincs kép ebben a sávban." if use_interval else "Nincs kép ehhez a szegmenshez.")
            return
        if use_interval:
            st.caption(
                "A következő határoló kép nem tartozik ehhez a sávhoz. A válogatás (5. lépés) a terv szegmense szerint működik; "
                "ez az előnézet a két határoló közötti képekre."
            )
        chunk = 6
        cap_n = min(n, _STEP3_FOLDER_PHOTO_PREVIEW_MAX)
        for start in range(0, cap_n, chunk):
            part = photos[start : start + chunk]
            cols = st.columns(len(part))
            for ci, p in enumerate(part):
                with cols[ci]:
                    if not _safe_st_image_path(
                        p,
                        caption=p.name[:48],
                        width=_GALLERY_COL_THUMB_WIDTH,
                        missing_label=p.name,
                    ):
                        if p.name:
                            st.caption(p.name)
        if n > _STEP3_FOLDER_PHOTO_PREVIEW_MAX:
            if use_interval:
                st.caption(
                    f"… és még {n - _STEP3_FOLDER_PHOTO_PREVIEW_MAX} kép ebben a sávban (galéria itt csonkolva)."
                )
            else:
                st.caption(
                    f"… és még {n - _STEP3_FOLDER_PHOTO_PREVIEW_MAX} kép (galéria itt csonkolva)."
                )


def _preview_row_segment_indices(
    plan: OrganizePlan,
    preview_rows: list[tuple[Path, list[Path]]],
    files_ord: list[Path] | None,
) -> list[int | None]:
    npr = len(preview_rows)
    seg_ix: list[int | None] = []
    for dix, (delim, followers) in enumerate(preview_rows):
        nd_next = preview_rows[dix + 1][0] if dix + 1 < npr else None
        seg_ix.append(
            _segment_index_for_tag_block(
                plan,
                delim,
                followers,
                next_delimiter=nd_next,
                files_ordered=files_ord,
            )
        )
    return seg_ix


def _normalize_tag_text(raw: object, fallback: str) -> str:
    text = (raw or "").strip() if isinstance(raw, str) else str(raw or "").strip()
    return text or fallback


def default_folder_name_for_segment(index: int) -> str:
    """
    A TAG / mappa **alapértelmezett** neve = a megjelenítési (szegmens-) sorrend szerinti
    **1-alapú sorszám**: az 1. mappa → ``"1"``, a 2. → ``"2"``, … . OCR-szöveg SOSEM lesz
    mappanév; az csak tippként jelenhet meg. A sorszám determinisztikus és a szegmens
    sorrendjéhez kötött, így újrafuttatáskor / 4. lépés újraszámoláskor sem ugrál.
    """
    try:
        return str(int(index) + 1)
    except Exception:
        return "1"


DELIMITERLESS_MARKER = "-xx"


def _step5_debug_enabled() -> bool:
    """Diagnosztika a végrehajtási térhez — csak ha a ``PHOTO_SORTER_DEBUG_STEP5`` env be van állítva."""
    return str(os.environ.get("PHOTO_SORTER_DEBUG_STEP5", "")).strip().lower() in {"1", "true", "yes", "on"}


def _step5_debug_dump_plan(label: str, plan: OrganizePlan, approved: list[str] | None = None) -> None:
    """Jól címkézett sorok a Streamlit terminál-kimenetbe (csak debug flag esetén)."""
    if not _step5_debug_enabled():
        return
    try:
        print(f"[STEP5-DEBUG] {label}: n_segments={len(plan.segments)}", flush=True)
        for i, seg in enumerate(plan.segments):
            print(
                f"[STEP5-DEBUG]   seg[{i}] folder_key={seg.folder_key!r} "
                f"delim={seg.closed_by_delimiter is not None} "
                f"photos={len(seg.photos)} pdfs={len(seg.pdfs)} plate={Path(seg.plate_image).name!r}",
                flush=True,
            )
        if approved is not None:
            distinct = sorted(set(approved))
            print(f"[STEP5-DEBUG]   approved_names={approved}", flush=True)
            print(
                f"[STEP5-DEBUG]   distinct_folder_count={len(distinct)} (expected on disk)",
                flush=True,
            )
    except Exception as exc:  # a diagnosztika sosem akaszthatja meg a végrehajtást
        print(f"[STEP5-DEBUG] dump failed: {exc}", flush=True)


def mark_delimiterless_name(name: str, *, has_delimiter: bool) -> str:
    """
    A **határoló nélküli** (nincs határolókép) mappák nevéhez ``-xx`` jelölőt fűz; a
    határolóval lezártak változatlanok. **Idempotens**: ha a név már ``-xx``-re végződik,
    nem fűz hozzá másodszor (így a 4. lépés újraszámolása / 5. lépés újrafuttatása sem
    duplázza). Üres alapnévre nem tesz puszta ``-xx``-et.
    """
    base = (name or "").strip()
    if has_delimiter or not base:
        return base
    if base.endswith(DELIMITERLESS_MARKER):
        return base
    return f"{base}{DELIMITERLESS_MARKER}"


def _strip_delimiterless_marker(name: str) -> str:
    """
    Eltávolítja a **rendszer** ``-xx`` jelölőt egy névről. A ``-xx`` egy **build-időben**
    számolt, határoló nélküli jelölő (nem felhasználói szöveg) — ezért egy **kézzel** beírt
    névben sosem szabadna szerepelnie. Ez a segéd „öngyógyítja” azokat az eseteket, amikor egy
    korábbi futásból / poisoned session-state-ből a már megjelölt (``KV49752-xx``) érték ragadt
    be a stabil mentésbe vagy a widgetbe: a kézi név feloldásakor levágjuk a fenntartott jelölőt,
    így a felhasználó által beírt **tiszta** név (``KV49752``) nyer.
    """
    base = (name or "").strip()
    while base.endswith(DELIMITERLESS_MARKER) and len(base) > len(DELIMITERLESS_MARKER):
        base = base[: -len(DELIMITERLESS_MARKER)].strip()
    return base


def build_approved_folder_names(
    plan: OrganizePlan,
    *,
    tag_by_segment: dict[str, str] | None = None,
) -> list[str]:
    """
    A **jóváhagyott** mappanevek listája szegmens-sorrendben. Ez a **központi** hely, amely az
    5. lépés előnézetét ÉS a tényleges mappakészítést (``execute_plan``) is táplálja, így az
    előnézet és a lemezen létrejövő mappanév mindig egyezik.

    Névfeloldás szegmensenként:

    * **``tag_by_segment`` megadva (élő flow):** ez a **kézi** felülírások megbízható tára
      (identitás → kézi név; KIZÁRÓLAG a felhasználó által megadott, sorszámtól eltérő neveket
      tartalmazza). Ha a szegmensnek van itt bejegyzése → az a kézi név nyer; **különben a
      szegmens érintetlen → MINDIG az egyedi sorszám-alapértelmezést kapja** (függetlenül attól,
      mi van a ``folder_key``-ben). Így az **át nem nevezett** mappák sosem olvadnak össze egy
      esetleg romlott / azonos ``folder_key`` miatt — minden érintetlen szegmens külön mappa.
    * **``tag_by_segment is None`` (visszafelé-kompatibilitás):** a feloldott ``folder_key``-t
      használjuk (üresre a sorszámra esünk vissza).

    A **határoló nélküli** (``closed_by_delimiter is None``) szegmensek neve ``-xx`` jelölőt kap
    (lásd ``mark_delimiterless_name``), a határolóval lezártak nem. Íráskor az **azonos** nevű
    szegmensek KÖZÖS mappába kerülnek (összevonás) — ez csak a felhasználó által szándékosan
    azonosra írt neveknél fordulhat elő, mert a sorszám-alapértelmezések egyediek.
    """
    names: list[str] = []
    for i, seg in enumerate(plan.segments):
        manual = ""
        if tag_by_segment:
            for sid in _segment_identity_keys(seg):
                v = _normalize_tag_text(tag_by_segment.get(sid), "")
                if v:
                    manual = v
                    break
        is_manual = False
        if manual:
            # Kézi név: a build-időben számolt ``-xx`` jelölőt sosem hordozhatja — ha egy korábbi
            # (poisoned) értékből mégis ott ragadt, levágjuk, hogy a felhasználó **tiszta** neve nyerjen.
            nm = safe_folder_name(_strip_delimiterless_marker(manual))
            is_manual = True
        elif tag_by_segment is not None:
            # Élő flow + nincs kézi név → garantáltan egyedi sorszám (nem a folder_key).
            nm = safe_folder_name(default_folder_name_for_segment(i))
        else:
            nm = seg.folder_key.strip() if isinstance(seg.folder_key, str) else ""
            if not nm:
                nm = safe_folder_name(default_folder_name_for_segment(i))
        # A ``-xx`` jelölő a határoló nélküli mappák **rendszer**-jelölése. Élő flow-ban (van
        # ``tag_by_segment``) CSAK a sorszám-alapértelmezett (át NEM nevezett) határoló nélküli
        # mappákra tesszük — ezek „nincs határoló, nézd át” jelzése marad. A **kézzel elnevezett**
        # határoló nélküli mappa a felhasználó pontos nevét kapja (nincs ``-xx``), különben a 3.
        # lépésben beírt név sosem „menne át” érintetlenül az 5. lépésbe (épp a bejelentett hiba).
        # A visszafelé-kompatibilis (``tag_by_segment is None``) ág változatlan: ott nem tudjuk a
        # kézi/auto különbséget, ezért a korábbi „mindig jelöl” viselkedés marad.
        if tag_by_segment is None or not is_manual:
            nm = mark_delimiterless_name(nm, has_delimiter=seg.closed_by_delimiter is not None)
        names.append(nm)
    return names


def _segment_identity_keys(seg: Segment) -> list[str]:
    keys: list[str] = []
    if seg.closed_by_delimiter is not None:
        keys.append(f"delim:{_norm_path_str(seg.closed_by_delimiter)}")
    keys.append(f"plate:{_norm_path_str(seg.plate_image)}")
    member_paths = sorted(
        {_norm_path_str(p) for p in seg.photos} | {_norm_path_str(p) for p in seg.pdfs}
    )
    if member_paths:
        member_digest = hashlib.sha1("|".join(member_paths).encode("utf-8")).hexdigest()
        keys.append(f"members:{member_digest}")
    return keys


# A megosztott (lépések közti) mappanév-szerkesztő widget-kulcsainak előtagja. A kulcs a
# szegmens **identitásából** származik (nem a sorszámból / pozícióból), így a szerkesztés nem
# szivárog át más mappára, és újrafuttatáskor / lapváltáskor stabil marad.
_SEGMENT_NAME_WIDGET_PREFIX = "segname_"


def _segment_identity_digest(seg: Segment) -> str:
    """Stabil, rövid azonosító a szegmens identitásából (widget-kulcshoz)."""
    ids = _segment_identity_keys(seg)
    return hashlib.sha1("|".join(ids).encode("utf-8")).hexdigest()[:16]


def _store_manual_name_for_segment(seg: Segment, store: dict[str, str] | None) -> str:
    """A szegmenshez tartozó **kézi** mappanév a megosztott tárból (a ``-xx`` jelölő levágva)."""
    if not isinstance(store, dict):
        return ""
    for sid in _segment_identity_keys(seg):
        v = _normalize_tag_text(store.get(sid), "")
        if v:
            return _strip_delimiterless_marker(v)
    return ""


def _on_segment_name_input_change(
    wkey: str, identity_keys: list[str], default_name: str
) -> None:
    """
    A mappanév-mező ``on_change`` visszahívása: a **megosztott tárba** (``_STEP3_TAGS_BY_SEGMENT_KEY``)
    írja a kézi nevet a szegmens minden identitás-kulcsára. Üres / az alapértelmezett sorszámmal
    egyező érték esetén törli a felülírást (visszaesés az automatikus névre). A visszahívás a
    rerun ELŐTT fut, így a tár mindig friss — a többi lépés (3/4/5) widgetjei innen szinkronizálnak.
    """
    typed = _normalize_tag_text(st.session_state.get(wkey), "")
    clean = _strip_delimiterless_marker(typed)
    store = st.session_state.get(_STEP3_TAGS_BY_SEGMENT_KEY)
    if not isinstance(store, dict):
        store = {}
    if clean and clean != default_name:
        for sid in identity_keys:
            store[sid] = clean
    else:
        for sid in identity_keys:
            store.pop(sid, None)
    st.session_state[_STEP3_TAGS_BY_SEGMENT_KEY] = store


def _render_shared_segment_name_input(
    seg: Segment,
    si: int,
    *,
    key_prefix: str,
    label: str = "Mappa név",
    label_visibility: str = "collapsed",
    help_text: str | None = None,
) -> str:
    """
    Egyetlen, **lépések közt megosztott** mappanév-szerkesztő mező egy szegmenshez.

    A **megosztott tár** (``_STEP3_TAGS_BY_SEGMENT_KEY``) az egyetlen igazságforrás: a widget
    minden futáskor a tárból szinkronizálja az értékét, így a 3., 4. és 5. lépésben ugyanahhoz a
    mappához tartozó mezők mindig egyezőt mutatnak (szerkesztés bárhol → mindenhol látszik). Az
    ``on_change`` visszahívás írja a tárat. A widget-kulcs a szegmens **identitásából** képződik
    (``key_prefix`` előtaggal lépésenként eltér, hogy a 3./5. lépés mezője ne ütközzön).

    Vissza: a **tényleges** (alkalmazandó) név (kézi, vagy az alapértelmezett sorszám).
    """
    ids = _segment_identity_keys(seg)
    wkey = f"{_SEGMENT_NAME_WIDGET_PREFIX}{key_prefix}_{_segment_identity_digest(seg)}"
    default_name = default_folder_name_for_segment(si)
    store = st.session_state.get(_STEP3_TAGS_BY_SEGMENT_KEY)
    if not isinstance(store, dict):
        store = {}
        st.session_state[_STEP3_TAGS_BY_SEGMENT_KEY] = store
    manual = _store_manual_name_for_segment(seg, store)
    desired = manual or default_name
    # A tár nyer: a widgetet (a beírás UTÁN, a visszahívással frissített tárból) ráigazítjuk.
    # Ez a beírt értéket sosem írja felül (akkor a tár == widget), csak a MÁSIK lépés mezőjét
    # szinkronizálja — így nincs „stale snapshot felülírja a friss bevitelt” regresszió.
    if st.session_state.get(wkey) != desired:
        st.session_state[wkey] = desired
    st.text_input(
        label,
        key=wkey,
        label_visibility=label_visibility,
        help=help_text,
        on_change=_on_segment_name_input_change,
        args=(wkey, ids, default_name),
    )
    typed = _normalize_tag_text(st.session_state.get(wkey), "")
    return _strip_delimiterless_marker(typed) or default_name


def pick_step3_tag_for_segment(
    *,
    segment: Segment,
    original_ocr: str,
    d_list: list[int],
    tag_by_dix: dict[int, str],
    preview_rows: list[tuple[Path, list[Path]]],
    by_delim: dict[str, str],
    by_segment: dict[str, str],
) -> str:
    """
    Egy szegmenshez több határoló-sor (3. lépés blokk) is tartozhat.
    Az utolsó, az eredeti OCR-től eltérő widget-érték nyer (tényleges szerkesztés);
    különben az utolsó blokk widgetje, majd a stabil határoló-mentés.
    """
    orig = _normalize_tag_text(original_ocr, "azonosítatlan")

    for dix in reversed(d_list):
        if dix not in tag_by_dix:
            continue
        text = _normalize_tag_text(tag_by_dix[dix], "")
        if text and text != orig:
            return text

    for dix in reversed(d_list):
        if dix in tag_by_dix:
            text = _normalize_tag_text(tag_by_dix[dix], orig)
            if text:
                return text

    for sid in _segment_identity_keys(segment):
        saved = by_segment.get(sid)
        if not saved:
            continue
        text = _normalize_tag_text(saved, "")
        if text and text != orig:
            return text

    for sid in _segment_identity_keys(segment):
        saved = by_segment.get(sid)
        if saved:
            return _normalize_tag_text(saved, orig)

    for dix in reversed(d_list):
        delim, _ = preview_rows[dix]
        saved = by_delim.get(_norm_path_str(delim))
        if not saved:
            continue
        text = _normalize_tag_text(saved, "")
        if text and text != orig:
            return text

    for dix in reversed(d_list):
        delim, _ = preview_rows[dix]
        saved = by_delim.get(_norm_path_str(delim))
        if saved:
            return _normalize_tag_text(saved, orig)

    return orig


def apply_step3_tag_edits_to_plan(
    plan: OrganizePlan,
    *,
    tag_by_dix: dict[int, str],
    tag_by_seg: dict[int, str],
    preview_rows: list[tuple[Path, list[Path]]],
    files_ord: list[Path] | None,
    tag_by_delim: dict[str, str] | None = None,
    tag_by_plate: dict[str, str] | None = None,
    tag_by_segment: dict[str, str] | None = None,
) -> OrganizePlan:
    """
  3. lépés TAG / mappa nevek → ``ocr_raw`` + ``folder_key`` (végrehajtás előtt).
  ``tag_by_dix`` / ``tag_by_seg``: aktív űrlapmezők; ``tag_by_delim`` / ``tag_by_plate``:
  stabil mentés (pl. 4. lépés újraszámolás után, ha a widget kulcsok törlődtek).
  """
    p = copy.deepcopy(plan)
    seg_ix = _preview_row_segment_indices(p, preview_rows, files_ord)
    by_delim = tag_by_delim or {}
    by_plate = tag_by_plate or {}
    by_segment = tag_by_segment or {}

    dix_by_si: dict[int, list[int]] = defaultdict(list)
    for dix, si in enumerate(seg_ix):
        if si is not None:
            dix_by_si[si].append(dix)

    touched_si: set[int] = set()
    for si, d_list in dix_by_si.items():
        seg = p.segments[si]
        touched_si.add(si)
        # Alapértelmezés = sorszám (NEM OCR). A kézi szerkesztés ettől eltérő érték.
        default_name = default_folder_name_for_segment(si)
        raw = pick_step3_tag_for_segment(
            segment=seg,
            original_ocr=default_name,
            d_list=d_list,
            tag_by_dix=tag_by_dix,
            preview_rows=preview_rows,
            by_delim=by_delim,
            by_segment=by_segment,
        )
        seg.ocr_raw = raw
        seg.folder_key = safe_folder_name(raw)

    for i, seg in enumerate(p.segments):
        if i in touched_si:
            continue
        # Párosítatlan (pl. a lista elején lévő) szegmens: kézi mező → stabil mentés →
        # **sorszám-alapértelmezés**. OCR-szöveg itt sem lehet a mappanév.
        default_name = default_folder_name_for_segment(i)
        raw_src = tag_by_seg.get(i)
        if raw_src is None:
            for sid in _segment_identity_keys(seg):
                raw_src = by_segment.get(sid)
                if raw_src is not None:
                    break
        if raw_src is None:
            raw_src = by_plate.get(_norm_path_str(seg.plate_image))
        raw = _normalize_tag_text(raw_src, default_name)
        seg.ocr_raw = raw
        seg.folder_key = safe_folder_name(seg.ocr_raw)
    return p


def _step3_tag_inputs_from_session(n_preview: int, n_seg: int) -> tuple[dict[int, str], dict[int, str]]:
    tag_by_dix: dict[int, str] = {}
    for dix in range(n_preview):
        ko = f"step3_tag_ocr_{dix}"
        if ko in st.session_state:
            tag_by_dix[dix] = st.session_state[ko]
    tag_by_seg: dict[int, str] = {}
    for i in range(n_seg):
        key = f"seg_ocr_raw_{i}"
        if key in st.session_state:
            tag_by_seg[i] = st.session_state[key]
    return tag_by_dix, tag_by_seg


def snapshot_step3_tag_overrides_from_plan(
    plan: OrganizePlan,
    preview_rows: list[tuple[Path, list[Path]]],
    files_ord: list[Path] | None,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """
    Határoló- / fémlap- / szegmens-identitás → a felhasználó által **kézzel megadott** név.

    **Csak a kézi (a sorszám-alapértelmezéstől eltérő) neveket** mentjük. Így az át nem nevezett
    mappák alapértelmezése mindig a friss **sorszám** marad (4. lépés újraszámolás / átrendezés
    után sem „ragad be” egy régi sorszám), miközben a kézi nevek identitás szerint túlélnek.
    """
    seg_ix = _preview_row_segment_indices(plan, preview_rows, files_ord)
    # Szegmensenként: van-e kézi (a sorszámtól eltérő) név?
    manual_by_si: dict[int, str] = {}
    for i, seg in enumerate(plan.segments):
        # A fenntartott ``-xx`` jelölő sosem része a **kézi** névnek; ha egy korábbi/poisoned
        # értékből ott ragadt, levágjuk, hogy a mentés is „öngyógyuljon” (a régi jelölt érték ne
        # éledjen újra az 5. lépésben).
        name = _strip_delimiterless_marker(_normalize_tag_text(seg.ocr_raw, ""))
        if name and name != default_folder_name_for_segment(i):
            manual_by_si[i] = name

    by_delim: dict[str, str] = {}
    for dix, (delim, _) in enumerate(preview_rows):
        si = seg_ix[dix]
        if si is None or si not in manual_by_si:
            continue
        by_delim[_norm_path_str(delim)] = manual_by_si[si]

    by_plate: dict[str, str] = {}
    by_segment: dict[str, str] = {}
    for i, name in manual_by_si.items():
        seg = plan.segments[i]
        for sid in _segment_identity_keys(seg):
            by_segment[sid] = name
        by_plate[_norm_path_str(seg.plate_image)] = name
    return by_delim, by_plate, by_segment


def _persist_step3_tag_overrides(plan: OrganizePlan) -> None:
    preview_rows, _ = get_step3_delimiter_preview_rows(plan)
    files_ord = _get_step3_ordered_media_files(plan)
    by_delim, by_plate, by_segment = snapshot_step3_tag_overrides_from_plan(plan, preview_rows, files_ord)
    st.session_state[_STEP3_TAGS_BY_DELIM_KEY] = by_delim
    st.session_state[_STEP3_TAGS_BY_PLATE_KEY] = by_plate
    st.session_state[_STEP3_TAGS_BY_SEGMENT_KEY] = by_segment


def _apply_ocr_edits_to_plan(plan: OrganizePlan, *, persist: bool = True) -> OrganizePlan:
    """
    3. lépés mezők + stabil (kézi) mentés → ``folder_key`` a 5. lépés válogatásához.
    ``persist=False`` esetén mellékhatás nélkül számol (pl. az 5. lépés jóváhagyott-név
    előnézetéhez), és nem írja felül a mentett felülírásokat.
    """
    preview_rows, _ = get_step3_delimiter_preview_rows(plan)
    files_ord = _get_step3_ordered_media_files(plan)
    tag_by_dix, tag_by_seg = _step3_tag_inputs_from_session(len(preview_rows), len(plan.segments))
    applied = apply_step3_tag_edits_to_plan(
        plan,
        tag_by_dix=tag_by_dix,
        tag_by_seg=tag_by_seg,
        preview_rows=preview_rows,
        files_ord=files_ord,
        tag_by_delim=st.session_state.get(_STEP3_TAGS_BY_DELIM_KEY) or {},
        tag_by_plate=st.session_state.get(_STEP3_TAGS_BY_PLATE_KEY) or {},
        tag_by_segment=st.session_state.get(_STEP3_TAGS_BY_SEGMENT_KEY) or {},
    )
    if persist:
        _persist_step3_tag_overrides(applied)
    return applied


def _effective_demotion_display_set() -> set[str]:
    """2. lépés állapotoszlop: piszkozat (pipák) vagy érvényesített lista."""
    draft = st.session_state.get(_DRAFT_DEMOTED_KEY)
    if isinstance(draft, list):
        return {_norm_path_str(x) for x in draft}
    return {_norm_path_str(x) for x in (st.session_state.get("_demoted_delimiter_paths") or [])}


def _effective_demotion_skip_set(gen: int, paths: list[Path]) -> set[str]:
    """
    Mely útvonalak nem számítanak határolónak a 3. lépés előnézetében.
    Élő 2. lépés pipák → piszkozat → érvényesített lista (ugyanaz a sorrend, mint véglegesítéskor).
    """
    if paths and _step2_demotion_widgets_active(gen):
        return {_norm_path_str(x) for x in _demoted_paths_from_step2_widgets(gen, paths)}
    draft = st.session_state.get(_DRAFT_DEMOTED_KEY)
    if isinstance(draft, list):
        return {_norm_path_str(x) for x in draft}
    return {_norm_path_str(x) for x in (st.session_state.get("_demoted_delimiter_paths") or [])}


def _delimiter_candidate_paths_for_demotion_widgets(
    plan: OrganizePlan, files: list[Path]
) -> list[Path]:
    """Hash-találat + kényszerített határolók — pipák olvasásához, ha nincs előnézet-sor."""
    force = {_norm_path_str(x) for x in (st.session_state.get("_forced_delimiter_paths") or [])}
    hit = {_norm_path_str(h) for h in plan.delimiter_hits}
    out: list[Path] = []
    for f in files:
        if f.suffix.lower() not in mbl.IMAGE_SUFFIXES:
            continue
        ns = _norm_path_str(f)
        if ns in force or ns in hit:
            out.append(f)
    return out


def _effective_demotion_skip_set_for_cache(plan: OrganizePlan) -> set[str]:
    """Cache-kulcs: élő pipák esetén a 2. lépés táblázat sorain olvassuk a pipákat."""
    gen = int(st.session_state.get("_plan_generation", 0))
    if _step2_demotion_widgets_active(gen):
        from_map = set(_demoted_paths_from_step2_keymap(gen))
        if from_map:
            return from_map
        table_paths, _ = _get_delimiter_table_paths(plan)
        if table_paths:
            return _effective_demotion_skip_set(gen, table_paths)
    return _effective_demotion_skip_set(gen, [])


def filter_path_list_excluding_norm(paths: list[str], removed_ns: set[str]) -> list[str]:
    """Útvonal-lista szűrése (tesztelhető, Streamlit nélkül)."""
    return [x for x in paths if _norm_path_str(x) not in removed_ns]


def delete_image_files_on_disk(paths: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Törli a fájlokat lemezről. Vissza: (sikeres norm útvonalak, (útvonal, hiba) párok)."""
    deleted: list[str] = []
    failures: list[tuple[str, str]] = []
    for raw in paths:
        ns = _norm_path_str(raw)
        p = Path(ns)
        if not p.is_file():
            failures.append((ns, "a fájl nem létezik"))
            continue
        try:
            p.unlink()
            deleted.append(ns)
        except OSError as exc:
            failures.append((ns, str(exc)))
    return deleted, failures


def prune_organize_plan_removed_paths(plan: OrganizePlan, removed_ns: set[str]) -> OrganizePlan:
    """Eltávolítja a törölt útvonalakat a terv határoló-listájából és szegmenseiből."""
    if not removed_ns:
        return plan
    p = copy.deepcopy(plan)
    p.delimiter_hits = [h for h in p.delimiter_hits if _norm_path_str(h) not in removed_ns]
    p.unassigned_images = [im for im in p.unassigned_images if _norm_path_str(im) not in removed_ns]
    for seg in p.segments:
        seg.photos = [ph for ph in seg.photos if _norm_path_str(ph) not in removed_ns]
        seg.pdfs = [pdf for pdf in seg.pdfs if _norm_path_str(pdf) not in removed_ns]
        if seg.closed_by_delimiter and _norm_path_str(seg.closed_by_delimiter) in removed_ns:
            seg.closed_by_delimiter = None
    return p


def _prune_plan_scan_cache_removed_paths(removed_ns: set[str]) -> None:
    cache = st.session_state.get("_plan_scan_cache")
    if not isinstance(cache, PlanScanCache) or not removed_ns:
        return
    new_files = [f for f in cache.files if _norm_path_str(f) not in removed_ns]
    new_hash = {
        k: v for k, v in cache.hash_by_path.items() if _norm_path_str(k) not in removed_ns
    }
    new_ocr = {
        k: v for k, v in cache.ocr_by_path.items() if _norm_path_str(k) not in removed_ns
    }
    new_delims = {k for k in cache.delimiter_candidates if _norm_path_str(k) not in removed_ns}
    st.session_state["_plan_scan_cache"] = PlanScanCache(
        files=new_files,
        hash_by_path=new_hash,
        ref_phash=cache.ref_phash,
        ref_ahash=cache.ref_ahash,
        max_hamming=cache.max_hamming,
        inner_ratio=cache.inner_ratio,
        source_str=cache.source_str,
        recursive=cache.recursive,
        image_count=sum(1 for f in new_files if f.suffix.lower() in IMAGE_SUFFIXES),
        files_sorted_by_name_mtime=bool(getattr(cache, "files_sorted_by_name_mtime", False)),
        ocr_by_path=new_ocr,
        delimiter_candidates=new_delims,
    )


def _clear_demotion_checkbox_keys_for_paths(gen: int, removed_ns: set[str]) -> None:
    for ns in removed_ns:
        st.session_state.pop(_step2_dem_checkbox_key(gen, ns), None)
        st.session_state.pop(_step3_dem_checkbox_key(gen, ns), None)
    keymap = _get_step2_dem_keymap(gen)
    if keymap:
        st.session_state[_step2_dem_keymap_session_key(gen)] = {
            k: v for k, v in keymap.items() if _norm_path_str(v) not in removed_ns
        }


def _apply_step2_delete_demoted_delimiter_files() -> None:
    """2. lépés: pipált „nem határoló” képek törlése lemezről + munkamenet takarítás."""
    plan = st.session_state.get("_plan")
    if not isinstance(plan, OrganizePlan):
        return
    paths, _ = _get_delimiter_table_paths(plan)
    gen = int(st.session_state.get("_plan_generation", 0))
    targets = _demoted_paths_from_step2_widgets(gen, paths)
    if not targets:
        st.session_state["_step2_delete_feedback"] = ("warn", "Nincs pipált **nem határoló** kép a törléshez.")
        return

    deleted, failures = delete_image_files_on_disk(targets)
    removed_ns = set(deleted)
    if not removed_ns and failures:
        st.session_state["_step2_delete_feedback"] = (
            "error",
            "Egyetlen fájl sem törölhető: " + "; ".join(f"{p}: {e}" for p, e in failures[:5]),
        )
        return

    st.session_state["_demoted_delimiter_paths"] = filter_path_list_excluding_norm(
        list(st.session_state.get("_demoted_delimiter_paths") or []),
        removed_ns,
    )
    st.session_state["_forced_delimiter_paths"] = filter_path_list_excluding_norm(
        list(st.session_state.get("_forced_delimiter_paths") or []),
        removed_ns,
    )
    draft = st.session_state.get(_DRAFT_DEMOTED_KEY)
    if isinstance(draft, list):
        st.session_state[_DRAFT_DEMOTED_KEY] = filter_path_list_excluding_norm(draft, removed_ns)

    by_delim = st.session_state.get(_STEP3_TAGS_BY_DELIM_KEY)
    if isinstance(by_delim, dict):
        st.session_state[_STEP3_TAGS_BY_DELIM_KEY] = {
            k: v for k, v in by_delim.items() if _norm_path_str(k) not in removed_ns
        }
    by_plate = st.session_state.get(_STEP3_TAGS_BY_PLATE_KEY)
    if isinstance(by_plate, dict):
        st.session_state[_STEP3_TAGS_BY_PLATE_KEY] = {
            k: v for k, v in by_plate.items() if _norm_path_str(k) not in removed_ns
        }
    by_segment = st.session_state.get(_STEP3_TAGS_BY_SEGMENT_KEY)
    if isinstance(by_segment, dict):
        kept: dict[str, str] = {}
        for k, v in by_segment.items():
            if not isinstance(k, str):
                continue
            if ":" not in k:
                kept[k] = v
                continue
            _prefix, raw_ns = k.split(":", 1)
            if _norm_path_str(raw_ns) in removed_ns:
                continue
            kept[k] = v
        st.session_state[_STEP3_TAGS_BY_SEGMENT_KEY] = kept

    st.session_state["_plan"] = prune_organize_plan_removed_paths(plan, removed_ns)
    _prune_plan_scan_cache_removed_paths(removed_ns)
    _clear_demotion_checkbox_keys_for_paths(gen, removed_ns)
    st.session_state["_delimiter_demotions_dirty"] = True
    bust_step3_delimiter_preview_cache()

    msg = f"**{len(deleted)}** kép törölve a lemezről."
    if failures:
        msg += f" {len(failures)} fájl nem törölhető (már hiányzik vagy zárolt)."
    st.session_state["_step2_delete_feedback"] = ("success", msg)
    st.session_state.pop("_step2_delete_confirm_pending", None)


def _cancel_step2_delete_confirm() -> None:
    st.session_state.pop("_step2_delete_confirm_pending", None)
    st.session_state.pop("_step2_delete_feedback", None)


def _queue_step2_delete_demoted_confirm() -> None:
    plan = st.session_state.get("_plan")
    if not isinstance(plan, OrganizePlan):
        return
    paths, _ = _get_delimiter_table_paths(plan)
    gen = int(st.session_state.get("_plan_generation", 0))
    n = len(_demoted_paths_from_step2_widgets(gen, paths))
    if n == 0:
        st.session_state["_step2_delete_feedback"] = ("warn", "Nincs pipált **nem határoló** kép a törléshez.")
        st.session_state.pop("_step2_delete_confirm_pending", None)
        return
    st.session_state["_step2_delete_confirm_pending"] = n
    st.session_state.pop("_step2_delete_feedback", None)


def _render_delimiter_demotion_table_step2(plan: OrganizePlan) -> None:
    """2. lépés fragment: pipák csak session state; nincs hash/OCR/terv újraszámolás kattintáskor."""
    paths, src_note = _get_delimiter_table_paths(plan)
    gen = int(st.session_state.get("_plan_generation", 0))
    dem_set = _effective_demotion_display_set()
    forced_ns = {_norm_path_str(x) for x in (st.session_state.get("_forced_delimiter_paths") or [])}
    keymap: dict[str, str] = {}

    st.caption(src_note)
    if not paths:
        st.info("Nincs határoló kép ezekkel a beállításokkal — tallózással vagy útvonallal felvehetsz.")
        return

    hdr = st.columns([0.35, 0.45, 1.35, 3.0, 0.85])
    hdr[0].markdown("**Nem**")
    hdr[1].markdown("**#**")
    hdr[2].markdown("**Előnézet**")
    hdr[3].markdown("**Fájl / útvonal**")
    hdr[4].markdown("**Állapot**")
    for j, p in enumerate(paths):
        sp = str(p)
        ns = _norm_path_str(p)
        chk_key = _step2_dem_checkbox_key(gen, ns)
        keymap[chk_key] = ns
        if chk_key not in st.session_state:
            st.session_state[chk_key] = ns in dem_set
        row = st.columns([0.35, 0.45, 1.35, 3.0, 0.85])
        with row[0]:
            st.checkbox(
                "Nem határoló",
                key=chk_key,
                label_visibility="collapsed",
                help="Nem számít határolónak; a táblázat alatti gomb a pipált elemeket eltávolítja a határoló listából.",
            )
        row[1].markdown(str(j + 1))
        with row[2]:
            if _path_image_file_ok(p):
                if not _safe_st_image_path(p, width=_PREVIEW_THUMB_WIDTH):
                    st.caption("—")
            else:
                st.caption("hiányzik" if not p.is_file() else "—")
        with row[3]:
            st.markdown(
                f'<span title="{html.escape(sp)}"><strong>{html.escape(p.name)}</strong></span>',
                unsafe_allow_html=True,
            )
            short = sp if len(sp) <= 96 else sp[:93] + "…"
            st.caption(short)
            if ns in forced_ns:
                st.caption("*(felvett határoló)*")
        with row[4]:
            if ns in dem_set:
                row[4].caption("✗ nem határoló")
            else:
                row[4].caption("határoló")

    _set_step2_dem_keymap(gen, keymap)
    _sync_draft_demotions_mirror_step2(gen, paths)
    if _delimiter_demotion_pending_step2(gen, paths):
        st.warning(
            "Van **el nem mentett** pipa — kattints a **Kijelöltek eltávolítása a listáról** gombra "
            "(vagy a **3 — TAG / mappa** lapon **Képek frissítése**), majd **4 — Terv véglegesítése** "
            "→ **Terv újraszámolása**."
        )
    st.button(
        "Kijelöltek eltávolítása a listáról",
        key="btn_step2_remove_selected_from_list",
        on_click=_apply_step2_remove_selected_from_list,
        help="Érvényesíti a fenti pipákat a munkamenetben; a kijelöltek nem határolóként kikerülnek a listából.",
    )

    fb = st.session_state.pop("_step2_delete_feedback", None)
    if isinstance(fb, tuple) and len(fb) == 2:
        kind, text = fb
        if kind == "success":
            st.success(text)
        elif kind == "error":
            st.error(text)
        else:
            st.warning(text)


@st.fragment
def _fragment_step3_delimiter_demotion_notes(plan: OrganizePlan) -> None:
    """2. lépés pipák állapota fragmentben — TAG / galéria nélkül."""
    st.markdown("##### Határoló jelölések")
    st.caption(
        "A **nem határoló** pipákat a **2 — Határolók** lapon állítod be. "
        "A **Képek frissítése** érvényesíti a jelöléseket és frissíti a határoló/követő képek előnézetét; "
        "a lenti TAG mezők addig a korábbi listát mutatják. Utána a **4 — Terv véglegesítése** lapon **Terv újraszámolása**."
    )
    paths, _ = _get_delimiter_table_paths(plan)
    gen = int(st.session_state.get("_plan_generation", 0))
    if paths:
        if _step2_demotion_widgets_active(gen):
            _sync_draft_demotions_mirror_step2(gen, paths)
        if _delimiter_demotion_pending_for_step3_finalize(gen, paths):
            st.warning(
                "Van **el nem frissített** jelölés a **2 — Határolók** lapon — a lenti előnézet és TAG mezők "
                "**Képek frissítése** gombig a korábbi listát mutatják."
            )
    else:
        st.info("Nincs határoló kép ezekkel a beállításokkal — a **2 — Határolók** lapon felvehetsz.")


def _render_step3_refresh_delimiter_button() -> None:
    """Képek frissítése a fragmenten kívül: teljes app rerun → előnézet + TAG blokkok frissülnek."""
    st.button(
        "Képek frissítése",
        type="primary",
        key="btn_step3_refresh_delimiter_images",
        on_click=_apply_step3_refresh_delimiter_images,
        help="A **2 — Határolók** lapon beállított pipákat érvényesíti, törli az előnézet-cache-t, majd frissíti a határoló/követő képek blokkot; a tervet a 4. lépésen számolod újra.",
    )


def _render_step3_refresh_feedback() -> None:
    fb = st.session_state.pop("_step3_refresh_feedback", None)
    if not fb:
        return
    st.session_state[_STEP3_REFRESH_INFLIGHT_KEY] = False
    kind, text = fb
    if kind == "success":
        st.success(text)
    elif kind == "error":
        st.error(text)
    elif kind == "warning":
        st.warning(text)
    else:
        st.info(text)


def _render_step2_unified_delimiter_table(plan: OrganizePlan) -> None:
    """Egy táblázat: az 1. lépés + felvett határolók, ugyanazzal a sorrenddel, mint a 3. lépés előnézet."""
    st.markdown("##### Határolók")
    st.caption(
        "Egy közös lista (fájlsorrend). **Nem határoló** pipa: csak jelölés (nincs újraszámolás kattintáskor). "
        "Véglegesítés: **3 — TAG / mappa** → **Képek frissítése**, majd **4 — Terv véglegesítése** → **Terv újraszámolása**. "
        "A táblázat alatti gomb a kijelölteket **eltávolítja a határoló listából**."
    )
    _render_delimiter_demotion_table_step2(plan)


def _queue_remove_forced(sp: str) -> None:
    ns = _norm_path_str(sp)
    lst = [x for x in (st.session_state.get("_forced_delimiter_paths") or []) if _norm_path_str(x) != ns]
    st.session_state["_forced_delimiter_paths"] = lst
    st.session_state["_delimiter_demotions_dirty"] = True
    bust_step2_delimiter_table_cache()


def _queue_clear_all_forced() -> None:
    st.session_state["_forced_delimiter_paths"] = []
    st.session_state["_delimiter_demotions_dirty"] = True
    bust_step2_delimiter_table_cache()


def _queue_add_forced_from_browse() -> None:
    """Natív tallózó: több kép egyszerre → kényszerített határolók."""
    st.session_state.pop("_manual_del_err", None)
    st.session_state.pop("_manual_del_info", None)
    picked = ask_open_image_paths("Határoló képek (Cmd / Ctrl: több fájl)")
    if picked is None:
        st.session_state["_manual_del_err"] = (
            "A fájlválasztó nem érhető el ezen a környezeten. Használd a kézi útvonal mezőt, vagy más gépen a Tallózást."
        )
        return
    if not picked:
        return
    lst = list(st.session_state.get("_forced_delimiter_paths", []))
    added = 0
    skipped_nonfile = 0
    skipped_ext = 0
    for raw in picked:
        pth = str(normalize_user_path(raw))
        if not Path(pth).is_file():
            skipped_nonfile += 1
            continue
        if Path(pth).suffix.lower() not in mbl.IMAGE_SUFFIXES:
            skipped_ext += 1
            continue
        if pth not in lst:
            lst.append(pth)
            added += 1
    st.session_state["_forced_delimiter_paths"] = lst
    st.session_state["_delimiter_demotions_dirty"] = True
    bust_step2_delimiter_table_cache()
    if added == 0:
        parts = []
        if skipped_nonfile:
            parts.append(f"{skipped_nonfile} nem létező fájl")
        if skipped_ext:
            parts.append(f"{skipped_ext} nem támogatott kiterjesztés")
        st.session_state["_manual_del_err"] = (
            "Nem került új határoló a listára. " + ("; ".join(parts) if parts else "Ellenőrizd a fájlokat.")
        )
    else:
        st.session_state["_manual_del_err"] = ""
        if skipped_nonfile or skipped_ext:
            st.session_state["_manual_del_info"] = (
                f"Hozzáadva: {added} kép. "
                f"Kihagyva: {skipped_nonfile + skipped_ext} elem."
            )


def _queue_add_forced_from_input() -> None:
    raw = (st.session_state.get("manual_del_one_path") or "").strip()
    if not raw:
        return
    pth = str(normalize_user_path(raw))
    if not Path(pth).is_file():
        st.session_state["_manual_del_err"] = "A fájl nem létezik."
        return
    if Path(pth).suffix.lower() not in mbl.IMAGE_SUFFIXES:
        st.session_state["_manual_del_err"] = "Csak képformátum (jpg, png, …)."
        return
    lst = list(st.session_state.get("_forced_delimiter_paths", []))
    if pth not in lst:
        lst.append(pth)
    st.session_state["_forced_delimiter_paths"] = lst
    st.session_state["_manual_del_err"] = ""
    st.session_state.pop("_manual_del_info", None)
    st.session_state["_delimiter_demotions_dirty"] = True
    bust_step2_delimiter_table_cache()


@st.fragment
def _fragment_step2_delimiter_table(plan: OrganizePlan) -> None:
    """Pipák és táblázat fragmentben — egy pipa nem futtatja újra a tallózás blokkot."""
    _render_step2_unified_delimiter_table(plan)
    _flush_pending_rerun(only_scope="fragment")


def _render_step2_delimiter_finalize(plan: OrganizePlan) -> None:
    st.subheader("2. Határolóképek véglegesítése")
    st.caption(
        "Itt csak **jelölhetsz** (nem határoló lista, további határolók felvétele). "
        "A terv **újraszámolása** a **4 — Terv véglegesítése** lapon történik."
    )
    _fragment_step2_delimiter_table(plan)

    st.divider()
    st.markdown("##### További határolók felvétele")
    st.caption(
        "**Tallózás:** több kép egyszerre (Cmd / Ctrl + kattintás). **Útvonal:** egy fájl, majd *Hozzáadás*. "
        "A felvett képek a fenti közös listában jelennek meg."
    )
    tb = st.columns([1.0, 2.5, 1.0])
    with tb[0]:
        st.button(
            "Tallózás…",
            key="btn_browse_manual_del",
            on_click=_queue_add_forced_from_browse,
            help="Rendszer fájlválasztó: egyszerre több kép.",
        )
    with tb[1]:
        st.text_input(
            "Teljes útvonal (egy kép)",
            key="manual_del_one_path",
            placeholder="/teljes/út/kép.jpg",
            label_visibility="collapsed",
        )
    with tb[2]:
        st.button(
            "Hozzáadás",
            key="btn_add_manual_del",
            on_click=_queue_add_forced_from_input,
            type="primary",
        )
    info = st.session_state.pop("_manual_del_info", "")
    if info:
        st.info(info)
    err = st.session_state.pop("_manual_del_err", "")
    if err:
        st.error(err)

    forced = list(st.session_state.get("_forced_delimiter_paths", []))
    if forced:
        st.button(
            "Összes felvett határoló törlése (tallózás / útvonal)",
            key="btn_clear_all_forced",
            on_click=_queue_clear_all_forced,
            help="Kiüríti a tallózással és az útvonal mezővel felvett kiegészítő listát; nem törli a „nem határoló” jelöléseket.",
        )

    dem = st.session_state.get("_demoted_delimiter_paths") or []
    if dem:
        nd = len(dem)
        with st.expander(f"Kézi **nem** határoló lista ({nd})", expanded=False):
            for line in dem[-50:]:
                st.text(line)
        if st.button(
            "Összes „nem határoló” kijelölés törlése",
            key="btn_clear_demotions",
            help="Kiüríti a kizárási listát; a terv újraszámolása a 4. lépésben.",
        ):
            st.session_state["_demoted_delimiter_paths"] = []
            st.session_state.pop(_DRAFT_DEMOTED_KEY, None)
            st.session_state["_delimiter_demotions_dirty"] = True
            _clear_step2_demotion_checkbox_keys()
            _clear_step3_demotion_checkbox_keys()
            bust_step3_delimiter_preview_cache()


def _norm_path_str(p: Path | str) -> str:
    return str(normalize_user_path(p))


def _user_folder_path(raw: str) -> Path:
    """Felhasználói mappaútvonal (beírás / tallózás / session) normalizálása."""
    return normalize_user_path(raw)


def _index_in_ordered_files(ordered_files: list[Path], target: Path) -> int | None:
    t = _norm_path_str(target)
    for i, f in enumerate(ordered_files):
        if _norm_path_str(f) == t:
            return i
    return None


def _append_missing_forced_delimiter_rows(
    rows: list[tuple[Path, list[Path]]],
    forced_paths: list[str],
    ordered_files: list[Path],
    is_delimiter: Callable[[Path], bool],
    *,
    following_max: int = 24,
) -> list[tuple[Path, list[Path]]]:
    """
    A 2. lépésben kézzel felsorolt, de a fő bejárásban még nem szereplő kényszerített határolók
    beszúrása ugyanazzal a követő-kép logikával, majd rendezés a fájlsorrend szerint.
    """
    seen = {_norm_path_str(d) for d, _ in rows}
    extra: list[tuple[Path, list[Path]]] = []
    for fp in forced_paths:
        p = normalize_user_path(fp)
        sp = _norm_path_str(p)
        if sp in seen:
            continue
        if not p.is_file() or p.suffix.lower() not in mbl.IMAGE_SUFFIXES:
            continue
        if not is_delimiter(p):
            continue
        seen.add(sp)
        ix = _index_in_ordered_files(ordered_files, p)
        if ix is None:
            extra.append((p, []))
            continue
        followers: list[Path] = []
        j = ix + 1
        while j < len(ordered_files) and len(followers) < following_max:
            g = ordered_files[j]
            if g.suffix.lower() not in mbl.IMAGE_SUFFIXES:
                j += 1
                continue
            if is_delimiter(g):
                break
            followers.append(g)
            j += 1
        extra.append((p, followers))
    combined = list(rows) + extra
    idx_map = {_norm_path_str(f): i for i, f in enumerate(ordered_files)}
    combined.sort(key=lambda t: idx_map.get(_norm_path_str(t[0]), 10**12))
    return combined


def _list_delimiter_followers_fallback_from_plan(
    files: list[Path],
    plan: OrganizePlan,
    skip: set[str],
    force: set[str],
    *,
    following_max: int = 24,
) -> list[tuple[Path, list[Path]]]:
    """Cache nélkül: fájlsorrend + 1. lépés ``delimiter_hits`` + 2. lépés skip/force (hash nélkül)."""
    hit_str = {_norm_path_str(h) for h in plan.delimiter_hits}

    def is_delimiter(f: Path) -> bool:
        if f.suffix.lower() not in mbl.IMAGE_SUFFIXES:
            return False
        sf = _norm_path_str(f)
        if sf in skip:
            return False
        if sf in force:
            return True
        return sf in hit_str

    out: list[tuple[Path, list[Path]]] = []
    for i, f in enumerate(files):
        if not is_delimiter(f):
            continue
        followers: list[Path] = []
        j = i + 1
        while j < len(files) and len(followers) < following_max:
            g = files[j]
            if g.suffix.lower() not in mbl.IMAGE_SUFFIXES:
                j += 1
                continue
            if is_delimiter(g):
                break
            followers.append(g)
            j += 1
        out.append((f, followers))
    return out


def _ordered_media_files_light() -> tuple[list[Path] | None, str | None]:
    """Fájllista hash / OCR nélkül — pipás táblázathoz."""
    cache = st.session_state.get("_plan_scan_cache")
    if isinstance(cache, PlanScanCache) and _scan_cache_matches_session(cache):
        return sort_media_paths_by_name_then_mtime(list(cache.files)), None
    src = (st.session_state.get("_src") or "").strip()
    if not src:
        return None, "Hiányzik a forrás útvonal — nem készíthető lista."
    source = _user_folder_path(src)
    rec = bool(st.session_state.get("metal_recursive_chk", st.session_state.get("_recursive", False)))
    files, list_err = list_sorted_media(source, recursive=rec)
    if list_err:
        return None, list_err
    return sort_media_paths_by_name_then_mtime(files), None


def _step2_table_paths_from_scan_cache(
    plan: OrganizePlan,
    files_ordered: list[Path],
    cache: PlanScanCache,
    force_paths: list[str],
    committed_demoted: set[str],
) -> list[Path]:
    """
    Határoló útvonalak a 2. lépés táblázatához — O(darabszám), hash újraszámolás nélkül.

    A ``delimiter_candidates`` + plan hit + force mellett a cache-beli ``hash_by_path`` lookup is
    szükséges (pl. ``/tmp`` vs ``/private/tmp`` alias), különben kézi felvétel után csak a forced
    sorok maradhatnak meg.
    """
    force_set = {_norm_path_str(x) for x in force_paths}
    hit_str = {_norm_path_str(h) for h in plan.delimiter_hits}
    baseline_delims = {_norm_path_str(x) for x in cache.delimiter_candidates}
    hmap = cache.hash_by_path

    def is_d(f: Path) -> bool:
        if f.suffix.lower() not in mbl.IMAGE_SUFFIXES:
            return False
        sf = _norm_path_str(f)
        if sf in committed_demoted:
            return False
        if sf in force_set:
            return True
        if sf in baseline_delims:
            return True
        pair = hmap.get(f)
        if pair is not None and mbl.delimiter_match_hashes(
            pair, cache.ref_phash, cache.ref_ahash, cache.max_hamming
        ):
            return True
        return sf in hit_str

    rows = [(f, []) for f in files_ordered if is_d(f)]
    rows = _append_missing_forced_delimiter_rows(
        rows, force_paths, files_ordered, is_d, following_max=0
    )
    return [p for p, _ in rows]


def _step2_table_paths_from_fallback(
    plan: OrganizePlan,
    files: list[Path],
    force_paths: list[str],
    committed_demoted: set[str],
) -> list[Path]:
    force_set = {_norm_path_str(x) for x in force_paths}

    def is_d(f: Path) -> bool:
        if f.suffix.lower() not in mbl.IMAGE_SUFFIXES:
            return False
        sf = _norm_path_str(f)
        if sf in committed_demoted:
            return False
        if sf in force_set:
            return True
        return sf in {_norm_path_str(h) for h in plan.delimiter_hits}

    rows = [(f, []) for f in files if is_d(f)]
    rows = _append_missing_forced_delimiter_rows(rows, force_paths, files, is_d, following_max=0)
    return [p for p, _ in rows]


def _step2_delimiter_table_cache_key(plan: OrganizePlan) -> tuple:
    """Élő pipák (piszkozat) nem invalidálják — a sorok kattintásig maradnak."""
    gen = int(st.session_state.get("_plan_generation", 0))
    committed = tuple(
        sorted(_norm_path_str(x) for x in (st.session_state.get("_demoted_delimiter_paths") or []))
    )
    forced = tuple(
        sorted(_norm_path_str(x) for x in (st.session_state.get("_forced_delimiter_paths") or []))
    )
    hits = tuple(_norm_path_str(h) for h in plan.delimiter_hits)
    src = (st.session_state.get("_src") or "").strip()
    rec = bool(st.session_state.get("metal_recursive_chk", st.session_state.get("_recursive", False)))
    mh = int(st.session_state.get("metal_max_hamming", 18))
    din = float(st.session_state.get("metal_del_inner", 0.92))
    cache = st.session_state.get("_plan_scan_cache")
    c_ok = isinstance(cache, PlanScanCache) and _scan_cache_matches_session(cache)
    nf = len(cache.files) if c_ok and isinstance(cache, PlanScanCache) else -1
    return (gen, src, rec, mh, round(din, 4), committed, forced, hits, c_ok, nf)


def _compute_step2_delimiter_table_paths(plan: OrganizePlan) -> tuple[list[Path], str]:
    """
    Határoló útvonalak a 2. lépés pipás táblázatához.

    A 2. lépés táblázatából csak a már **érvényesített** „nem határoló” elemek esnek ki.
    A még csak bepipált (piszkozat) sorok kattintásig maradnak, így nem tűnnek el azonnal.
    """
    files, err = _ordered_media_files_light()
    if err:
        return [], err
    if not files:
        return [], "Nincs médiafájl a forrásban."
    force_paths = [
        str(normalize_user_path(x)) for x in (st.session_state.get("_forced_delimiter_paths") or [])
    ]
    committed_demoted = {
        _norm_path_str(x) for x in (st.session_state.get("_demoted_delimiter_paths") or [])
    }
    cache = st.session_state.get("_plan_scan_cache")
    if isinstance(cache, PlanScanCache) and _scan_cache_matches_session(cache):
        files_ordered = _ordered_files_from_cache(cache)
        paths = _step2_table_paths_from_scan_cache(
            plan, files_ordered, cache, force_paths, committed_demoted
        )
        return (
            paths,
            "Forrás: **terv cache** + kiindulási terv (az érvényesített **nem határoló** elemek már kikerültek).",
        )

    paths = _step2_table_paths_from_fallback(plan, files, force_paths, committed_demoted)
    return (
        paths,
        "Forrás: **fájllista** + kiindulási terv (hash nélkül; az érvényesített **nem határoló** elemek kikerültek).",
    )


def get_step2_delimiter_table_paths(plan: OrganizePlan) -> tuple[list[Path], str]:
    """2. lépés határoló-lista — session cache (pipa kattintás nem invalidál)."""
    k = _step2_delimiter_table_cache_key(plan)
    slot = st.session_state.get(_CACHED_STEP2_TABLE_SLOT)
    if isinstance(slot, dict) and slot.get("key") == k:
        return list(slot["paths"]), slot["note"]
    paths, note = _compute_step2_delimiter_table_paths(plan)
    st.session_state[_CACHED_STEP2_TABLE_SLOT] = {"key": k, "paths": list(paths), "note": note}
    return paths, note


def bust_step2_delimiter_table_cache() -> None:
    st.session_state.pop(_CACHED_STEP2_TABLE_SLOT, None)


def _get_delimiter_table_paths(plan: OrganizePlan) -> tuple[list[Path], str]:
    return get_step2_delimiter_table_paths(plan)


def _step3_demotion_skip_for_preview(plan: OrganizePlan, files_ordered: list[Path]) -> set[str]:
    """3. lépés előnézet: élő 2. lépés pipák a táblázat sorain, különben piszkozat / érvényesített."""
    gen = int(st.session_state.get("_plan_generation", 0))
    if _step2_demotion_widgets_active(gen):
        table_paths, _ = _get_delimiter_table_paths(plan)
        return _effective_demotion_skip_set(gen, table_paths or [])
    return _effective_demotion_skip_set(gen, files_ordered)


def _ordered_files_from_cache(cache: PlanScanCache) -> list[Path]:
    """
    A cache-ben tárolt fájlsorrend.
    Ha már név+mtime szerint rendezettként jelöltük, ne rendezzük újra.
    """
    if bool(getattr(cache, "files_sorted_by_name_mtime", False)):
        return list(cache.files)
    return sort_media_paths_by_name_then_mtime(list(cache.files))


def _compute_step3_delimiter_preview(plan: OrganizePlan) -> tuple[list[tuple[Path, list[Path]]], str]:
    """Határoló + követő képek sorai és rövid magyarázat a forráshoz (kézi határolók beolvasztva)."""
    force_paths = [str(normalize_user_path(x)) for x in (st.session_state.get("_forced_delimiter_paths") or [])]
    force_set = set(force_paths)
    cache = st.session_state.get("_plan_scan_cache")
    if isinstance(cache, PlanScanCache) and _scan_cache_matches_session(cache):
        files_ordered = _ordered_files_from_cache(cache)
        skip = _step3_demotion_skip_for_preview(plan, files_ordered)
        rows = list_delimiter_followers_preview(
            cache, skip, force_set, following_max=24, file_sequence=files_ordered
        )
        baseline_delims = set(cache.delimiter_candidates)

        def is_d(f: Path) -> bool:
            if f.suffix.lower() not in mbl.IMAGE_SUFFIXES:
                return False
            sf = _norm_path_str(f)
            if sf in skip:
                return False
            if sf in force_set:
                return True
            if sf in baseline_delims:
                return True
            pair = cache.hash_by_path.get(f)
            if pair is None:
                return False
            return mbl.delimiter_match_hashes(
                pair, cache.ref_phash, cache.ref_ahash, cache.max_hamming
            )

        rows = _append_missing_forced_delimiter_rows(
            rows, force_paths, files_ordered, is_d, following_max=24
        )
        return (
            rows,
            "Forrás: **terv cache** + 2. lépés; fájlsorrend az előnézetben: **név**, majd módosítás ideje.",
        )

    src = (st.session_state.get("_src") or "").strip()
    if not src:
        return [], "Hiányzik a forrás útvonal — nem készíthető előnézet."
    source = _user_folder_path(src)
    rec = bool(st.session_state.get("metal_recursive_chk", st.session_state.get("_recursive", False)))
    files, list_err = list_sorted_media(source, recursive=rec)
    if list_err:
        return [], list_err
    skip = _step3_demotion_skip_for_preview(plan, files)
    rows = _list_delimiter_followers_fallback_from_plan(files, plan, skip, force_set, following_max=24)
    hit_str = {_norm_path_str(h) for h in plan.delimiter_hits}

    def is_d(f: Path) -> bool:
        if f.suffix.lower() not in mbl.IMAGE_SUFFIXES:
            return False
        sf = _norm_path_str(f)
        if sf in skip:
            return False
        if sf in force_set:
            return True
        return sf in hit_str

    rows = _append_missing_forced_delimiter_rows(rows, force_paths, files, is_d, following_max=24)
    return (
        rows,
        "Forrás: **fájllista** + 1. lépésbeli határoló-lista + 2. lépés jelölései (cache hiányzik vagy nem egyezik a beállításokkal — kevésbé pontos, 4. lépés után cache-ből pontosabb).",
    )


def _sanitize_delimiter_preview_rows(
    rows: list[tuple[Path, list[Path]]],
) -> list[tuple[Path, list[Path]]]:
    """Törölt / hiányzó útvonalak kiszűrése (cache és külső törlés után)."""
    out: list[tuple[Path, list[Path]]] = []
    for delim, followers in rows:
        try:
            if not delim.is_file():
                continue
        except OSError:
            continue
        kept: list[Path] = []
        for f in followers:
            try:
                if f.is_file():
                    kept.append(f)
            except OSError:
                continue
        out.append((delim, kept))
    return out


def _delimiter_preview_cache_key(plan: OrganizePlan) -> tuple:
    """OCR / TAG név szerkesztés nem változtatja — így a 3. lapon a bevitel nem indít felesleges újraszámolást."""
    gen = int(st.session_state.get("_plan_generation", 0))
    dem = tuple(sorted(_effective_demotion_skip_set_for_cache(plan)))
    forced = tuple(sorted(_norm_path_str(x) for x in (st.session_state.get("_forced_delimiter_paths") or [])))
    hits = tuple(_norm_path_str(h) for h in plan.delimiter_hits)
    src = (st.session_state.get("_src") or "").strip()
    rec = bool(st.session_state.get("metal_recursive_chk", st.session_state.get("_recursive", False)))
    mh = int(st.session_state.get("metal_max_hamming", 18))
    din = float(st.session_state.get("metal_del_inner", 0.92))
    cache = st.session_state.get("_plan_scan_cache")
    c_ok = isinstance(cache, PlanScanCache) and _scan_cache_matches_session(cache)
    nf = len(cache.files) if c_ok and isinstance(cache, PlanScanCache) else -1
    return (gen, src, rec, mh, round(din, 4), dem, forced, hits, c_ok, nf)


def get_step3_delimiter_preview_rows(plan: OrganizePlan) -> tuple[list[tuple[Path, list[Path]]], str]:
    """Ugyanazon futáson / szövegbeíráskor (fragment) újrahasználja a határoló+sorrend számítást."""
    gen = int(st.session_state.get("_plan_generation", 0))
    if _step2_demotion_widgets_active(gen):
        table_paths, _ = _get_delimiter_table_paths(plan)
        if table_paths:
            _sync_draft_demotions_mirror_step2(gen, table_paths)
    k = _delimiter_preview_cache_key(plan)
    slot = st.session_state.get(_CACHED_DELIM_PREVIEW_SLOT)
    if isinstance(slot, dict) and slot.get("key") == k:
        rows = _sanitize_delimiter_preview_rows(slot["rows"])
        if rows != slot["rows"]:
            slot["rows"] = rows
            st.session_state[_CACHED_DELIM_PREVIEW_SLOT] = slot
        return rows, slot["note"]
    rows, note = _compute_step3_delimiter_preview(plan)
    rows = _sanitize_delimiter_preview_rows(rows)
    st.session_state[_CACHED_DELIM_PREVIEW_SLOT] = {"key": k, "rows": rows, "note": note}
    return rows, note


def bust_step3_delimiter_preview_cache() -> None:
    """Gomb callback: kötelező újraszámolás a következő megjelenítéskor."""
    st.session_state.pop(_CACHED_DELIM_PREVIEW_SLOT, None)
    bust_step2_delimiter_table_cache()


def _render_step3_delimiter_followers_block(rows: list[tuple[Path, list[Path]]], src_note: str) -> None:
    st.markdown("#### Határolók és közvetlenül követő képek")
    st.caption(
        "**Fontos:** itt **minden határoló képhez pontosan egy sor** tartozik — a **fájlsorrend** szerinti előnézet "
        "(a **2. lépés** szerinti megmaradó határolók és kényszerítettek). Minden sorban: a határoló + a következő "
        "**nem határoló** képek a következő határolóig (max. megjelenített darab). **PDF** itt nincs."
    )
    st.caption(src_note)
    if not rows:
        st.info("Nincs megjeleníthető határoló kép ezekkel a beállításokkal.")
        return
    for bi, (delim, followers) in enumerate(rows):
        title = f"Határoló {bi + 1}: `{delim.name}` — {len(followers)} követő kép"
        with st.expander(title, expanded=False):
            st.caption(str(delim))
            imgs: list[Path] = [delim] + list(followers)
            chunk = 5
            for start in range(0, len(imgs), chunk):
                part = imgs[start : start + chunk]
                cols = st.columns(len(part))
                for ci, p in enumerate(part):
                    with cols[ci]:
                        if start == 0 and ci == 0:
                            cap = "határoló"
                        else:
                            cap = p.name
                        if not _safe_st_image_path(
                            p,
                            caption=cap[:48],
                            width=_GALLERY_COL_THUMB_WIDTH,
                            missing_label=p.name,
                        ):
                            st.text(p.name)


def _get_step3_ordered_media_files(plan: OrganizePlan) -> list[Path] | None:
    """Ugyanabból a forrásból és sorrendből, mint a határoló-előnézet (képek + PDF egy listában)."""
    cache = st.session_state.get("_plan_scan_cache")
    if isinstance(cache, PlanScanCache) and _scan_cache_matches_session(cache):
        return _ordered_files_from_cache(cache)
    src = (st.session_state.get("_src") or "").strip()
    if not src:
        return None
    source = _user_folder_path(src)
    rec = bool(st.session_state.get("metal_recursive_chk", st.session_state.get("_recursive", False)))
    files, list_err = list_sorted_media(source, recursive=rec)
    if list_err:
        return None
    return files


def _segment_index_between_delimiters(
    plan: OrganizePlan,
    delim: Path,
    next_delimiter: Path | None,
    files_ordered: list[Path],
) -> int | None:
    """
    Visszaesés: melyik szegmens fémlapja esik a ``delim`` és a következő határoló közötti
    fájlsorrend-ablakba (a 24-es ``followers`` csonkolás nélkül is párosítható).
    """
    nd = _norm_path_str(delim)
    i0: int | None = None
    for i, f in enumerate(files_ordered):
        if _norm_path_str(f) == nd:
            i0 = i
            break
    if i0 is None:
        return None
    i1 = len(files_ordered)
    if next_delimiter is not None:
        nn = _norm_path_str(next_delimiter)
        for j in range(i0 + 1, len(files_ordered)):
            if _norm_path_str(files_ordered[j]) == nn:
                i1 = j
                break
    window_norms: set[str] = set()
    for k in range(i0 + 1, i1):
        p = files_ordered[k]
        if p.suffix.lower() in mbl.IMAGE_SUFFIXES:
            window_norms.add(_norm_path_str(p))
    for si, seg in enumerate(plan.segments):
        if _norm_path_str(seg.plate_image) in window_norms:
            return si
    return None


def _segment_index_for_closing_delimiter(plan: OrganizePlan, delim: Path) -> int | None:
    """Melyik terv-szegmens zárul ezzel a határoló képpel (útvonal egyezés)."""
    nd = _norm_path_str(delim)
    for si, seg in enumerate(plan.segments):
        if seg.closed_by_delimiter is None:
            continue
        if _norm_path_str(seg.closed_by_delimiter) == nd:
            return si
    return None


def _segment_index_for_tag_block(
    plan: OrganizePlan,
    delim: Path,
    followers: list[Path],
    *,
    next_delimiter: Path | None = None,
    files_ordered: list[Path] | None = None,
) -> int | None:
    """
    Melyik terv-szegmens tartozik ehhez a határoló előnézeti sorhoz.

    A 3. lépés sora a határoló **utáni** képeket jeleníti meg (követők / a következő határolóig
    tartó sáv), tehát a felhasználó az **itt induló** (a sorban LÁTHATÓ) szegmenst nevezi el.
    Ezért **kizárólag** azt a szegmenst párosítjuk, amelynek a fémlapja a követők között / a
    sávban van — így a megjelenített és a szerkesztett szegmens MINDIG ugyanaz (1:1):

    1) fémlap a (csonka) követők között,
    2) fémlap a következő határolóig tartó teljes fájlsorrend-ablakban (nem függ a 24-es limittől),
    3) különben ``None`` — ehhez a sorhoz nincs „utána induló” szegmens (pl. záró / egymás utáni
       határoló). Ilyenkor a szegmens **nem** ehhez a sorhoz tartozik.

    A korábbi „lezáró egyezés” visszaesést **szándékosan elhagytuk**: az a sor által MUTATOTT
    (követő) szegmens helyett az ELŐTTE lévőt párosította, ami eltolta a neveket és a lista
    elején lévő (határoló nélküli) szegmenst árván hagyta. A lista elején lévő / párosítatlan
    szegmenseket a 3. lépés űrlapja külön, kiemelten jeleníti meg (saját szövegmező).
    """
    fs = {_norm_path_str(p) for p in followers}
    for i, seg in enumerate(plan.segments):
        if _norm_path_str(seg.plate_image) in fs:
            return i
    if files_ordered:
        si = _segment_index_between_delimiters(plan, delim, next_delimiter, files_ordered)
        if si is not None:
            return si
    return None


def _tag_segment_anchor_thumbnails(dix: int, rows: list[tuple[Path, list[Path]]]) -> list[Path]:
    """Egy határoló-sor: a határoló kép + az első követő (ha van), max 2 kép."""
    if dix >= len(rows):
        return []
    delim, followers = rows[dix]
    out: list[Path] = [delim]
    if followers:
        out.append(followers[0])
    return [p for p in out[:2] if _path_image_file_ok(p)]


def _render_step3_tag_mappa_header_delimiter_preview(plan: OrganizePlan) -> None:
    """Teljes futás: lapváltás, véglegesítés, TAG nélkül; **nem** fut újra pipa-jelöléskor (fragment)."""
    st.caption(
        "A **Határolók és közvetlenül követő képek** blokk minden határolóhoz egy sort mutat (fájlsorrend). "
        "Lent: **határoló + első követő** miniatűr, **TAG / mappa név**, **Összes kép a mappában**. "
        "TAG gépeléskor csak a szövegmező fut újra."
    )
    preview_rows, preview_note = get_step3_delimiter_preview_rows(plan)
    _render_step3_delimiter_followers_block(preview_rows, preview_note)


@st.fragment
def _fragment_step3_segment_name_editor(plan: OrganizePlan, si: int) -> None:
    """
    Egy mappa (szegmens) neve — csak ez a mező fut újra gépeléskor (fragment). A **megosztott
    tárba** ír (lépések közti egységes igazságforrás), így a 4. és 5. lépésbe is átszinkronizál.
    """
    seg = plan.segments[si]
    has_delim = seg.closed_by_delimiter is not None
    st.markdown("**Mappa név**")
    eff = _render_shared_segment_name_input(
        seg,
        si,
        key_prefix="s3",
        help_text=(
            "Alapértelmezés a sorszám; írd át tetszőleges névre — MINDEN mappa neve szerkeszthető "
            "(a határoló nélküliek is). Tiltott karakterek a válogatáskor cserélve. Üres mező = "
            "automatikus alapértelmezett név."
        ),
    )
    st.caption(f"Mappa (fájlrendszer): `{safe_folder_name(eff)}`")
    if not has_delim:
        st.caption(
            "Nincs határoló kép ehhez a mappához — a beírt név **pontosan** így lesz alkalmazva; "
            "üresen hagyva automatikus `-xx` jelölésű sorszámot kap."
        )
    ocr_hint = _normalize_tag_text(seg.ocr_raw, "")
    if ocr_hint and ocr_hint != default_folder_name_for_segment(si):
        st.caption(f"OCR (tipp, nem mappanév): `{html.escape(ocr_hint[:60])}`")


def step3_no_segments_notice(
    *, has_delimiter_rows: bool, upload_mode: bool
) -> tuple[str, str]:
    """
    Üzenet, ha a tervben **nincs egyetlen TAG/mappa szegmens sem**, így nincs mit elnevezni
    (ezért hiányoznak a mappanév-mezők). A leggyakoribb ok: a jelenlegi pHash-küszöbbel **minden
    (vagy szinte minden) kép határolónak minősült**, ezért nincs „lap”/fotó, ami mappát nyitna.

    Vissza: ``(kind, message)`` ahol ``kind`` ∈ {"error", "info"}.
    """
    if not has_delimiter_rows:
        return (
            "info",
            "Nincs megjeleníthető TAG/mappa szegmens. Készíts kiindulási tervet az "
            "**1 — Kiindulás** lapon (tölts fel/adj meg forrást és határoló referenciát).",
        )
    demote_hint = (
        "a **2 — Határolók** lapon vedd ki a tévesen határolónak jelölt képeket "
        "(**nem határoló** pipa), majd itt a **Képek frissítése**, végül a "
        "**4 — Terv véglegesítése** lapon **Terv újraszámolása**"
    )
    return (
        "error",
        "**Nincs egyetlen TAG/mappa szegmens sem**, ezért nincs mit elnevezni — emiatt hiányoznak "
        "a mappanév-mezők. A jelenlegi **pHash / aHash küszöbbel minden (vagy szinte minden) kép "
        "határolónak minősült**, így nincs olyan „lap”/fotó, ami mappát nyitna.\n\n"
        "**Megoldás:**\n"
        "- Az **1 — Kiindulás** lapon **csökkentsd** a *pHash / aHash küszöböt* (kisebb érték = "
        "kevesebb kép lesz határoló), és **készítsd újra** a kiindulási tervet; **vagy**\n"
        f"- {demote_hint}.\n\n"
        + (
            "_(Feltöltés-mód: nincs helyi forrásmappa — a fenti lépésekkel javítható.)_"
            if upload_mode
            else ""
        ),
    )


def _render_step3_tag_mappa_forms(plan: OrganizePlan) -> None:
    """Soronként: határoló + követő miniatűr; TAG fragmentben; „Összes kép a mappában” expander."""
    # A mappanév-szerkesztés most a **megosztott tárba** (identitás-alapú) ír. A korábbi, sor- /
    # szegmens-index alapú widgetek (``step3_tag_ocr_*`` / ``seg_ocr_raw_*``) már nincsenek a UI-ban;
    # ha egy korábbi (hot-reload előtti) menetből beragadtak, töröljük őket, hogy NE írhassák felül a
    # frissen beírt nevet (a régi „stale widget nyer a tár felett” regresszió elkerülése).
    for _legacy in [
        k
        for k in list(st.session_state.keys())
        if isinstance(k, str)
        and (k.startswith("step3_tag_ocr_") or k.startswith("seg_ocr_raw_"))
    ]:
        del st.session_state[_legacy]
    preview_rows, _preview_note = get_step3_delimiter_preview_rows(plan)
    files_ord = _get_step3_ordered_media_files(plan)
    file_index_map = _ordered_file_index_map(files_ord) if files_ord else None
    st.divider()
    n_prev = len(preview_rows)

    # Ha a tervben nincs egyetlen TAG/mappa szegmens sem (jellemzően: minden kép határolónak
    # minősült), akkor nincs mappanév-mező, amit kitölteni. A korábbi viselkedés egy sor
    # félrevezető, „ellenőrizd a forrásmappát” típusú figyelmeztetést mutatott (felhő/feltöltés
    # módban nincs is forrásmappa). Helyette EGY világos, mindkét módban érvényes útmutatót adunk.
    if len(plan.segments) == 0:
        kind, msg = step3_no_segments_notice(
            has_delimiter_rows=bool(preview_rows), upload_mode=_is_upload_mode()
        )
        if kind == "error":
            st.error(msg)
        else:
            st.info(msg)
        return
    seg_ix_preview: list[int | None] = []
    for dix, (delim, followers) in enumerate(preview_rows):
        nd_next = preview_rows[dix + 1][0] if dix + 1 < n_prev else None
        seg_ix_preview.append(
            _segment_index_for_tag_block(
                plan,
                delim,
                followers,
                next_delimiter=nd_next,
                files_ordered=files_ord,
            )
        )
    # Szegmens → az őt megjelenítő (első) határoló-sor. Így MINDEN mappához (szegmenshez) pontosan
    # EGY szerkesztő tartozik — a határolóval lezártakhoz a határoló-sor előnézetével, a határoló
    # nélküliekhez (lista eleji / párosítatlan) a fémlap + saját képeivel. Nincs külön szakasz.
    row_for_si: dict[int, int] = {}
    for dix, si in enumerate(seg_ix_preview):
        if si is not None and si not in row_for_si:
            row_for_si[si] = dix

    st.markdown("##### TAG / mappa szegmensek — **minden mappa neve szerkeszthető**")
    st.caption(
        f"**Mappák (szegmensek):** {len(plan.segments)} db; **határoló sorok** (felül): {n_prev} db. "
        "Az **alapértelmezett név a sorszám** (1, 2, 3, …) — írd át tetszőleges névre; OCR-szöveg csak tippként jelenik meg, sosem lesz mappanév. "
        "**Minden mappa** átnevezhető — a határoló nélküli (lista eleji / párosítatlan / záró) mappák is. "
        "**Összes kép a mappában** = a jelen határoló után a következő határolóig tartó kép-sáv (a következő határoló kép nélkül). "
        "A név szerkesztésekor csak a szövegmező fut újra; a beírt név a 4. és 5. lépésbe is átszinkronizál."
    )

    for si, seg in enumerate(plan.segments):
        dix = row_for_si.get(si)
        if dix is not None:
            delim = preview_rows[dix][0]
            st.markdown(
                f"#### Mappa {si + 1} — határoló: `{html.escape(delim.name)}`",
                unsafe_allow_html=True,
            )
            st.markdown(f"**Határoló** (határoló + első követő)")
            thumbs = _tag_segment_anchor_thumbnails(dix, preview_rows)
            captions = ["Határoló", "első követő"]
        else:
            st.markdown(
                f"#### Mappa {si + 1} — *(nincs határoló kép)*",
                unsafe_allow_html=True,
            )
            st.markdown("**Fémlap / első kép**")
            thumbs = [
                p
                for p in ([seg.plate_image] + list(seg.photos[:1]))
                if _path_image_file_ok(p)
            ]
            captions = ["fémlap", "első kép"]

        if thumbs:
            tc = st.columns(len(thumbs))
            for ci, p in enumerate(thumbs):
                with tc[ci]:
                    cap = captions[ci] if ci < len(captions) else p.name
                    if not _safe_st_image_path(
                        p,
                        caption=f"{cap}: {p.name}"[:56],
                        width=_GALLERY_COL_THUMB_WIDTH,
                        missing_label=str(p),
                    ):
                        st.caption(str(p))
        else:
            st.caption("Nincs előnézeti kép ehhez a mappához.")

        _fragment_step3_segment_name_editor(plan, si)
        if dix is not None:
            _render_step3_folder_photos_expander(
                seg,
                dix=dix,
                preview_rows=preview_rows,
                files_ordered=files_ord,
                index_map=file_index_map,
            )
        else:
            _render_step3_folder_photos_expander(seg)


def _render_step3_tag_mappa(plan: OrganizePlan) -> None:
    """Képek frissítése gomb teljes app rerun; előnézet + TAG csak frissítés / lapváltás / név szerkesztés után."""
    st.subheader("3. TAG-ek és mappanevek azonosítása")
    _fragment_step3_delimiter_demotion_notes(plan)
    _render_step3_refresh_delimiter_button()
    _render_step3_refresh_feedback()
    _flush_pending_rerun(only_scope="app")
    st.divider()
    _render_step3_tag_mappa_header_delimiter_preview(plan)
    _render_step3_tag_mappa_forms(plan)


def _render_step4_finalize_plan() -> None:
    if st.session_state.get(_STEP4_REBUILD_INFLIGHT_KEY) and not st.session_state.get(
        "_delimiter_demotions_dirty"
    ):
        st.session_state[_STEP4_REBUILD_INFLIGHT_KEY] = False
    st.subheader("4. Terv véglegesítése")
    st.caption(
        "Itt **érvényesíted** a **3. lépésben frissített** határoló-jelöléseket és a **kézi határolókat**: "
        "a gomb újraszámolja a TAG/mappa tervet (cache-ből, ha lehet). Ezután válaszd ki a válogatásból kihagyandó képeket."
    )
    if st.session_state.get("_delimiter_demotions_dirty"):
        st.warning(
            "Van **el nem érvényesített** határoló-módosítás — a **2. lépésben** **Kijelöltek eltávolítása a listáról** vagy a **3. lépésben** "
            "**Képek frissítése**, majd itt a **Terv újraszámolása**."
        )
        st.caption(
            "A **Terv újraszámolása** gomb alatt megjelenik a haladás (progress sáv + állapotsor)."
        )
        if st.button(
            "Terv újraszámolása (határolók + kényszerített lista → TAG/mappa)",
            type="primary",
            key="btn_apply_delimiter_demotions",
            help="Gyorsított újraszámolás, ha lehet (cache). Az 3. lapon szerkesztett OCR szövegek a szegmensek változása miatt újra ellenőrizendők.",
        ):
            if st.session_state.get(_STEP4_REBUILD_INFLIGHT_KEY):
                st.info("A terv újraszámolása már fut.")
                return
            st.session_state[_STEP4_REBUILD_INFLIGHT_KEY] = True
            # Progress a status-on kívül: Streamlit egy futás alatt csak így frissül folyamatosan.
            bar = st.progress(0, text="Indítás…")
            status_line = st.empty()

            def _step4_plan_progress(frac: float, msg: str | None = None) -> None:
                f = max(0.0, min(1.0, float(frac)))
                t = (msg or "").strip()
                if len(t) > 140:
                    t = t[:137] + "…"
                label = t or "Feldolgozás…"
                bar.progress(f, text=label)
                status_line.caption(label)
                time.sleep(0.03)

            old_plan = st.session_state.get("_plan")
            if isinstance(old_plan, OrganizePlan):
                _step4_plan_progress(0.02, "Előkészítés: 3. lépés név-felülírások mentése…")
                edited_old = _apply_ocr_edits_to_plan(old_plan)
                pr_old, _ = get_step3_delimiter_preview_rows(edited_old)
                fo_old = _get_step3_ordered_media_files(edited_old)
                by_d, by_p, by_s = snapshot_step3_tag_overrides_from_plan(edited_old, pr_old, fo_old)
                st.session_state[_STEP3_TAGS_BY_DELIM_KEY] = by_d
                st.session_state[_STEP3_TAGS_BY_PLATE_KEY] = by_p
                st.session_state[_STEP3_TAGS_BY_SEGMENT_KEY] = by_s

            new_plan: OrganizePlan | None = None
            with st.status("Terv újraszámolása…", expanded=True) as rebuild_status:
                _step4_plan_progress(0.06, "Terv újraszámolás indítása…")
                new_plan = _rebuild_plan_with_demotions(
                    non_delimiter_paths=list(st.session_state.get("_demoted_delimiter_paths", [])),
                    progress=_step4_plan_progress,
                )
                if new_plan is not None:
                    rebuild_status.update(label="Terv újraszámolva", state="complete")
                else:
                    rebuild_status.update(label="Terv újraszámolása sikertelen", state="error")

            if new_plan is not None:
                pr_new, _ = get_step3_delimiter_preview_rows(new_plan)
                fo_new = _get_step3_ordered_media_files(new_plan)
                new_plan = apply_step3_tag_edits_to_plan(
                    new_plan,
                    tag_by_dix={},
                    tag_by_seg={},
                    preview_rows=pr_new,
                    files_ord=fo_new,
                    tag_by_delim={},
                    tag_by_plate={},
                    tag_by_segment=st.session_state.get(_STEP3_TAGS_BY_SEGMENT_KEY) or {},
                )
                _persist_step3_tag_overrides(new_plan)
                bar.progress(1.0, text="Kész")
                status_line.caption("Terv elkészült.")
                time.sleep(0.35)
                st.session_state["_plan"] = new_plan
                st.session_state["_delimiter_demotions_dirty"] = False
                _bump_plan_generation()
                _clear_segment_ocr_widget_keys()
                _clear_plan_exclude_multiselect_keys()
                st.rerun()
            else:
                time.sleep(0.5)
                st.session_state[_STEP4_REBUILD_INFLIGHT_KEY] = False
    else:
        st.success("A határoló-jelölések és a kényszerített lista **szinkronban** vannak a tervvel (vagy nem volt módosítás).")

    st.divider()
    if st.session_state.get("_delimiter_demotions_dirty"):
        st.info(
            "Előbb kattints a **Terv újraszámolása** gombra — utána jelenik meg a kihagyandó képek listája "
            "(a TAG-ek száma a módosításoktól függően változhat)."
        )
        return

    plan2 = st.session_state.get("_plan")
    if not isinstance(plan2, OrganizePlan):
        return
    st.success(
        f"TAG/mappa darab: {len(plan2.segments)}, határoló találat: {len(plan2.delimiter_hits)}, "
        f"TAG nélküli képek: {len(plan2.unassigned_images)}, PDF (TAG előtt): {len(plan2.unassigned_pdfs)}"
    )
    st.caption("Válaszd ki, mely képek **ne** kerüljenek mozgatásra / másolásra.")
    for i, seg in enumerate(plan2.segments):
        if seg.photos:
            st.multiselect(
                f"TAG/Mappa {i + 1}: kihagyandó képek a válogatásból",
                options=[str(p) for p in seg.photos],
                default=[],
                key=f"exc_sel_seg_{i}",
                help="A kijelöltek a forráson maradnak. Ha minden kép és PDF kiesik, a TAG elvész a térből.",
            )
    if plan2.unassigned_images:
        st.subheader("TAG nélküli képek — kihagyás")
        st.caption("Ezekhez nem tartozott OCR-azonosító; a kijelöltek a forráson maradnak.")
        st.multiselect(
            "Kihagyandó fájlok",
            options=[str(p) for p in plan2.unassigned_images],
            default=[],
            key="exc_sel_unassigned",
        )


def _distinct_folder_names_with_counts(names: list[str]) -> list[tuple[str, int]]:
    """Sorrendtartó, megkülönböztető mappanevek + hány szegmens kerül abba (összevonás)."""
    order: list[str] = []
    counts: dict[str, int] = {}
    for nm in names:
        if nm not in counts:
            counts[nm] = 0
            order.append(nm)
        counts[nm] += 1
    return [(nm, counts[nm]) for nm in order]


def _live_tag_by_segment_from_plan(prepared: OrganizePlan) -> dict[str, str]:
    """
    A JELENLEGI (élő) kézi mappanév-térkép a már feldolgozott tervből (widget + stabil mentés
    egyesítve). A ``build_approved_folder_names`` ``tag_by_segment`` paramétere a kézi nevek
    egyetlen megbízható forrása; az előnézet **persist=False** miatt a session-snapshot ott
    elavult lehet, ezért a ``prepared`` tervből számoljuk újra (mellékhatás nélkül), így az
    előnézet a **most beírt** neveket mutatja, nem egy korábbit.
    """
    try:
        pr, _ = get_step3_delimiter_preview_rows(prepared)
        fo = _get_step3_ordered_media_files(prepared)
        _by_d, _by_p, by_segment = snapshot_step3_tag_overrides_from_plan(prepared, pr, fo)
        return by_segment
    except Exception:
        return st.session_state.get(_STEP3_TAGS_BY_SEGMENT_KEY) or {}


def _resolve_execution_plan_for_preview() -> tuple[OrganizePlan, dict[str, str]] | None:
    """
    Mellékhatás nélkül előállítja a végrehajtási tervet (a jóváhagyott nevek előnézetéhez):
    3. lépés nevek alkalmazása → szegmens-kiválasztás. NEM ír session-állapotot.
    Vissza: (terv, élő kézi-név térkép) — a térképet a ``build_approved_folder_names``-hez.
    """
    plan0 = st.session_state.get("_plan")
    if not isinstance(plan0, OrganizePlan):
        return None
    # ``_apply_ocr_edits_to_plan`` MÁR egyesíti az élő 3. lépés mezőket (widget) és a stabil
    # mentést (``pick_step3_tag_for_segment`` a widgetet preferálja). NE alkalmazzunk rá egy
    # második, ÜRES widgetű + csak-snapshot menetet: az felülírná a frissen beírt neveket egy
    # esetleg üres / korábbi (stale) snapshot-értékkel (előnézet persist=False → a snapshot
    # nem frissül, így a régi érték „ragadna be”).
    prepared = _apply_ocr_edits_to_plan(plan0, persist=False)
    live_by_segment = _live_tag_by_segment_from_plan(prepared)
    try:
        prepared.segments = select_execution_segments(
            prepared,
            tag_by_segment=live_by_segment,
            original_ocr_by_plate=_original_ocr_by_plate_from_cache(),
            drop_unedited_delimiterless=False,
        )
    except Exception:
        pass
    return prepared, live_by_segment


def _render_step5_segment_name_editor(seg: Segment, si: int) -> None:
    """Egy mappa neve az 5. lépésben — a **megosztott tárba** ír (ugyanaz, mint a 3./4. lépés)."""
    eff = _render_shared_segment_name_input(
        seg,
        si,
        key_prefix="s5",
        label=f"Mappa {si + 1} neve",
        label_visibility="visible",
        help_text=(
            "Itt a végrehajtás ELŐTT is átírható bármely mappa neve (a határoló nélküliek is). "
            "A változás azonnal a tényleges másolásra/áthelyezésre érvényes, és visszaszinkronizál a 3. lépésbe."
        ),
    )
    has_delim = seg.closed_by_delimiter is not None
    suffix = "" if has_delim else "  *(határoló nélküli — üresen automatikus `-xx`)*"
    st.caption(f"Mappa (fájlrendszer): `{safe_folder_name(eff)}`{suffix}")


def _render_step5_editable_folder_names(prepared: OrganizePlan) -> None:
    """
    Szerkeszthető mappanév-mező **minden** célmappához, közvetlenül a végrehajtás előtt. A
    megosztott tárba ír (3./4./5. lépés egységes igazságforrása), így a bevitt név érvényesül a
    tényleges másolásra/áthelyezésre is, és bárhol szerkesztve mindenhol megjelenik.
    """
    if not prepared.segments:
        return
    with st.expander(
        f"Mappanevek szerkesztése a végrehajtás előtt — {len(prepared.segments)} mappa",
        expanded=False,
    ):
        st.caption(
            "Bármely mappa neve itt is módosítható (a határoló nélkülieké is). Üres mező → automatikus "
            "alapértelmezett (sorszám; határoló nélkül `-xx` jelölővel). **Azonos nevű mappák képei egy "
            "közös mappába** kerülnek (összevonás)."
        )
        for si, seg in enumerate(prepared.segments):
            _render_step5_segment_name_editor(seg, si)


def _render_step5_approved_folder_names_preview() -> None:
    """A létrehozandó mappák (jóváhagyott nevek) átlátható listája a végrehajtás előtt."""
    try:
        resolved = _resolve_execution_plan_for_preview()
    except Exception:
        resolved = None
    if resolved is None:
        return
    prepared, live_by_segment = resolved
    _render_step5_editable_folder_names(prepared)
    names = build_approved_folder_names(
        prepared,
        tag_by_segment=live_by_segment,
    )
    distinct = _distinct_folder_names_with_counts(names)
    with st.expander(f"Létrehozandó mappák előnézete — {len(distinct)} mappa", expanded=True):
        st.caption(
            "**Jóváhagyott mappanevek**: kézi név, ha a **3 — Mappanevek** vagy itt az 5. lépésben "
            "átírtad; egyébként a **sorszám**. OCR-szöveg sosem lesz mappanév. **Azonos nevet adva több "
            "blokknak azok képei EGY KÖZÖS mappába kerülnek** (összevonás); az eltérő nevek külön mappába."
        )
        if not distinct:
            st.info("Nincs létrehozandó mappa (nincs szegmens a tervben).")
            return
        for idx, (nm, cnt) in enumerate(distinct, 1):
            if cnt > 1:
                st.write(f"{idx}. `{nm}`  —  {cnt} szegmens összevonva")
            else:
                st.write(f"{idx}. `{nm}`")


def _render_step5_execute_block() -> None:
    st.subheader("5. Képek válogatásának indítása")
    st.checkbox("Másolás (kikapcsolva: áthelyezés)", key="metal_copy_mode", value=False)
    st.checkbox(
        "TAG nélküli fájlok is menjenek a cél `_nincs_köteg` alá",
        key="metal_move_unassigned",
        value=False,
    )
    if st.session_state.get("_delimiter_demotions_dirty"):
        st.error("Előbb a **4 — Terv véglegesítése** lapon futtasd a **Terv újraszámolása** gombot.")

    _render_step5_approved_folder_names_preview()

    if st.button("Válogatás végrehajtása — mozgatás / másolás", type="primary", key="btn_exec_sort"):
        if st.session_state.get(_STEP5_EXEC_INFLIGHT_KEY):
            st.info("A válogatás végrehajtása már fut.")
            return
        st.session_state[_STEP5_EXEC_INFLIGHT_KEY] = True
        try:
            exec_bar = st.progress(0, text="Indítás…")
            exec_status = st.empty()

            exec_bar.progress(0.03, text="Előkészítés: TAG/mappa nevek alkalmazása…")
            exec_status.caption("Előkészítés: TAG/mappa nevek alkalmazása…")
            # ``_apply_ocr_edits_to_plan`` egyesíti az élő 3. lépés mezőket és a stabil mentést
            # (a widget nyer), és persist=True-val frissíti is a snapshotot. NE alkalmazzunk rá
            # egy második, üres widgetű + csak-snapshot menetet — az visszaírná a régi/üres
            # snapshot-nevet a frissen beírt érték helyett.
            plan: OrganizePlan = _apply_ocr_edits_to_plan(st.session_state["_plan"])

            exec_bar.progress(0.08, text="Előkészítés: kihagyások és cél beállítása…")
            exec_status.caption("Előkészítés: kihagyások és cél beállítása…")
            plan = _apply_photo_exclusions_to_plan(plan)
            # Központi szerződés: a határolós szegmensek + a kézzel átnevezett, határoló nélküli
            # szegmensek mindig bekerülnek a végrehajtási tervbe (a határoló nélküli, át nem
            # nevezett szegmensek viselkedése változatlan: alapból bennmaradnak).
            # Védő: a kiválasztás soha ne tudja megakasztani a végrehajtást — hiba esetén a
            # teljes (szűretlen) szegmenslistára esünk vissza.
            try:
                plan.segments = select_execution_segments(
                    plan,
                    tag_by_segment=st.session_state.get(_STEP3_TAGS_BY_SEGMENT_KEY) or {},
                    original_ocr_by_plate=_original_ocr_by_plate_from_cache(),
                    drop_unedited_delimiterless=False,
                )
            except Exception:
                pass

            # Explicit, jóváhagyott mappanév-lista: kézi név (a stabil tárból), ha van; különben
            # garantáltan egyedi sorszám. A tárat adjuk át, hogy az érintetlen mappák SOHA ne
            # olvadjanak össze egy esetleg romlott/azonos folder_key miatt (csak a szándékosan
            # azonosra írt kézi nevek olvadnak össze). Ezt írjuk vissza a szegmensekre.
            approved_names = build_approved_folder_names(
                plan,
                tag_by_segment=st.session_state.get(_STEP3_TAGS_BY_SEGMENT_KEY) or {},
            )
            _step5_debug_dump_plan("execute prep (after select)", plan, approved_names)
            for seg, nm in zip(plan.segments, approved_names):
                seg.folder_key = nm
            _distinct_exec = _distinct_folder_names_with_counts(approved_names)
            st.markdown(f"**Létrehozandó mappák — {len(_distinct_exec)} db (jóváhagyott nevek):**")
            st.caption(
                "Kézi név, ha megadtad; egyébként a **sorszám**. **Azonos nevű blokkok képei egy "
                "közös mappába kerülnek** (összevonás); az eltérő nevek külön mappába."
            )
            for _idx, (_nm, _cnt) in enumerate(_distinct_exec, 1):
                if _cnt > 1:
                    st.write(f"{_idx}. `{_nm}`  —  {_cnt} szegmens összevonva")
                else:
                    st.write(f"{_idx}. `{_nm}`")

            upload_mode = _is_upload_mode()
            if upload_mode:
                # Felhő / headless: friss ideiglenes kimeneti mappa, a végén ZIP-letöltés.
                out_root = Path(tempfile.mkdtemp(prefix="photo_sorter_out_"))
                copy_mode = True  # a feltöltött forrás temp mappa épségben marad
            else:
                out_root = _user_folder_path(st.session_state["_out"])
                copy_mode = bool(st.session_state.get("metal_copy_mode", False))
            move_unassigned = bool(st.session_state.get("metal_move_unassigned", False))
            if not move_unassigned:
                plan = OrganizePlan(
                    segments=plan.segments,
                    unassigned_images=[],
                    unassigned_pdfs=[],
                    delimiter_hits=plan.delimiter_hits,
                )

            def _exec_progress(frac: float, msg: str | None = None) -> None:
                f = 0.1 + 0.9 * max(0.0, min(1.0, float(frac)))
                label = ((msg or "Végrehajtás…").strip() or "Végrehajtás…")[:140]
                exec_bar.progress(f, text=label)
                exec_status.caption(label)

            log = execute_plan(plan, out_root, copy_mode=copy_mode, progress=_exec_progress)
            if _step5_debug_enabled():
                try:
                    created_dirs = sorted(
                        d.name for d in out_root.iterdir()
                        if d.is_dir() and d.name != mbl.UNASSIGNED
                    )
                    print(
                        f"[STEP5-DEBUG] FOLDERS CREATED ON DISK: count={len(created_dirs)} {created_dirs}",
                        flush=True,
                    )
                except Exception as exc:
                    print(f"[STEP5-DEBUG] disk count failed: {exc}", flush=True)
            if upload_mode:
                exec_bar.progress(0.97, text="ZIP csomagolása…")
                exec_status.caption("Rendezett mappák ZIP-be csomagolása…")
                zip_bytes = _zip_directory_bytes(out_root)
                st.session_state[_CLOUD_ZIP_RESULT_KEY] = {
                    "data": zip_bytes,
                    "name": "rendezett_mappak.zip",
                    "ops": len(log),
                }
                shutil.rmtree(out_root, ignore_errors=True)
            exec_bar.progress(1.0, text="Kész")
            exec_status.caption("Válogatás befejezve.")
            st.success(f"Kész. Műveletek száma: {len(log)}")
            with st.expander("Utolsó 100 naplósor"):
                for kind, a, b in log[-100:]:
                    st.text(f"{kind}: {a} -> {b}")
            # A pop + rerun után a lapok ebből mutatnak egyértelmű „sikeres válogatás”
            # üzenetet a félrevezető „készíts kiindulási tervet” felirat helyett.
            if not upload_mode:
                st.session_state[_SORT_COMPLETED_KEY] = {
                    "ops": len(log),
                    "out": str(out_root),
                    "copy": copy_mode,
                }
            st.session_state.pop("_plan", None)
            st.session_state.pop("_del_bytes", None)
            st.session_state.pop("_del_name", None)
            _clear_demoted_delimiter_paths()
            _clear_forced_delimiter_paths()
            _clear_plan_scan_cache()
            _clear_step3_tag_override_snapshots()
            _request_rerun(scope="app")
        except Exception as e:
            st.exception(e)
        finally:
            st.session_state[_STEP5_EXEC_INFLIGHT_KEY] = False


def main() -> None:
    if mbl.pytesseract is None:
        st.error("A `pytesseract` nincs telepítve. Futtasd: `pip install -r requirements.txt`")
        return
    st.title("TAG / mappa — fémlap OCR + határoló")
    st.markdown(
        """
**Öt lépésben:**  
**1.** Kiindulási terv — forrás, cél, határoló referencia, hash beállítások, **határolók automatikus azonosítása**.  
**2.** Határolók — lista, **nem határoló** pipák (fragment, könnyű), kijelöltek eltávolítása a listáról.  
**3.** TAG / mappa — **Képek frissítése** (a 2. lépés pipáit érvényesíti + határoló/követő előnézet, fragment); határoló galéria; lent **TAG / mappa név** (gépeléskor csak a mező fut újra).  
**4.** Terv véglegesítése — **terv újraszámolása** (2. vagy 3. lépésben érvényesített határoló-jelölések), majd kihagyandó képek.  
**5.** **Válogatás indítása** — másolás / áthelyezés a cél mappákba.

A forrás fájljai **fájlnév** (lexikografikusan, tipikusan időbélyeg a névben), majd **módosítás ideje** szerint kerülnek feldolgozásra. Minden TAG-en belül: `jegyzőkönyv/` (PDF) és `fotók/` (képek).
        """
    )

    if "metal_src_dir" not in st.session_state:
        st.session_state.metal_src_dir = ""
    if "metal_out_dir" not in st.session_state:
        st.session_state.metal_out_dir = ""

    _flush_browse_warning_metal()

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        [
            "1 — Kiindulás",
            "2 — Határolók",
            "3 — TAG / mappa",
            "4 — Terv véglegesítése",
            "5 — Válogatás indítása",
        ]
    )

    src = (st.session_state.get("metal_src_dir") or "").strip()
    out = (st.session_state.get("metal_out_dir") or "").strip()

    with tab1:
        st.subheader("1. Kiindulási terv — határolóképek azonosítása")
        upload_mode = _render_input_mode_selector()
        if upload_mode:
            st.caption(
                "**Feltöltés-mód** (felhő / headless): tölts fel képeket és PDF-eket. A rendezés "
                "egy szerveroldali ideiglenes mappában készül, a végén **ZIP-ben** töltöd le — "
                "helyi fájlok nem mozdulnak."
            )
            src_files = st.file_uploader(
                "Forrás képek és PDF-ek (több fájl)",
                type=_CLOUD_SRC_TYPES,
                accept_multiple_files=True,
                key="metal_src_uploads",
                help="A feldolgozási sorrend a fájlnév szerint alakul (tipikusan időbélyeg a névben).",
            )
            if src_files:
                src = _persist_uploaded_source_media(src_files)
                st.session_state["metal_src_dir"] = src
                out = _ensure_cloud_out_dir_placeholder()
                st.session_state["metal_out_dir"] = out
                st.caption(f"{len(src_files)} fájl feltöltve és előkészítve a feldolgozáshoz.")
            else:
                src = ""
        else:
            _folder_path_row(
                "Forrás mappa (abszolút útvonal)",
                "metal_src_dir",
                "metal_browse_src",
                "Forrás mappa",
            )
            _folder_path_row(
                "Cél gyökér (ide kerülnek a TAG / mappa mappák)",
                "metal_out_dir",
                "metal_browse_out",
                "Cél gyökér mappa",
            )
            st.caption("A cél mappa a **5.** lépésben használatos; már most add meg.")
        del_file = st.file_uploader(
            "Határoló referencia kép",
            type=["jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff", "heic", "heif"],
            help="A forrássorozatban ehhez hasonló képek lesznek határolóként felismerve (pHash / aHash).",
        )
        if del_file is not None:
            img = _load_rgb_from_bytes(del_file.getvalue())
            if img is not None:
                st.caption(f"Kijelölve: **{del_file.name}** — előnézet:")
                if not _safe_st_image_pil(img, use_container_width=True):
                    st.warning("Az előnézet üres vagy érvénytelen méretű kép.")
            else:
                st.warning("Az előnézet nem nyitható meg (üres vagy sérült fájl).")

        max_h = st.slider(
            "pHash / aHash küszöb (nagyobb = engedékenyebb határoló egyezés)",
            4,
            48,
            18,
            key="metal_max_hamming",
            help="EXIF után pHash + aHash. Kevesebb találat → növeld.",
        )
        del_inner = st.slider(
            "Határoló: középső terület aránya (sarok figyelmen kívül)",
            min_value=0.80,
            max_value=1.0,
            value=0.92,
            step=0.02,
            key="metal_del_inner",
            help="1.0 = teljes kép; kisebb érték = sarok kevésbé zavarja a hasht.",
        )
        recursive = st.checkbox("Rekurzív (almappák is)", value=False, key="metal_recursive_chk")

        st.caption(
            "Az OCR (fémlap) **automatikus** előfeldolgozást használ (percentilis, CLAHE)."
        )

        if st.button("Kiindulási terv készítése — határolók azonosítása", type="primary", key="btn_initial_plan"):
            if not src or not out:
                if upload_mode:
                    st.error("Tölts fel legalább egy **forrás** képet vagy PDF-et.")
                else:
                    st.error("Add meg a **forrás** és a **cél** mappát.")
            elif not del_file:
                st.error("Töltsd fel a határoló **referencia** képet.")
            else:
                source = _user_folder_path(src)
                out_root = _user_folder_path(out)
                if not source.is_dir():
                    st.error(folder_path_error_message(source))
                elif not out_root.parent.exists():
                    st.error(f"A cél gyökér szülőmappája nem elérhető: {out_root.parent}")
                else:
                    tmp_del = _delimiter_temp_from_upload(del_file)
                    del_path = tmp_del
                    plan: OrganizePlan | None = None
                    scan_holder: list = []
                    build_outcome: list = []

                    with st.status("Kiindulási terv készítése…", expanded=True) as plan_status:
                        progress_widget = st.progress(0, text="Indítás…")
                        plan_status_line = st.empty()

                        def _plan_progress(frac: float, msg: str | None = None) -> None:
                            label = (msg or "Terv készítése…")[:120]
                            f = max(0.0, min(1.0, float(frac)))
                            try:
                                progress_widget.progress(f, text=label)
                            except TypeError:
                                progress_widget.progress(f)
                            plan_status_line.caption(label)

                        q_build: queue.Queue = queue.Queue()

                        def _build_worker() -> None:
                            try:

                                def _enqueue_prog(f: float, m: str | None = None) -> None:
                                    q_build.put(("p", f, m if m is None else str(m)))

                                p = build_plan(
                                    source,
                                    del_path,
                                    max_hamming=max_h,
                                    recursive=recursive,
                                    progress=_enqueue_prog,
                                    delimiter_inner_ratio=del_inner,
                                    non_delimiter_paths=[],
                                    force_delimiter_paths=[],
                                    scan_cache_holder=scan_holder,
                                )
                                build_outcome.append(("ok", p))
                            except RuntimeError as e:
                                build_outcome.append(("err", str(e)))
                            except ValueError as e:
                                build_outcome.append(("err", str(e)))
                            except Exception as e:
                                build_outcome.append(("exc", e))
                            finally:
                                q_build.put(("done",))

                        th_build = threading.Thread(
                            target=_build_worker, daemon=True, name="metal_initial_plan"
                        )
                        th_build.start()
                        if _drain_progress_queue(
                            th_build,
                            q_build,
                            _plan_progress,
                            abort_message="A terv készítése váratlanul megszakadt.",
                        ):
                            th_build.join(timeout=7200)
                            if build_outcome:
                                tag = build_outcome[0][0]
                                if tag == "ok":
                                    plan = build_outcome[0][1]  # type: ignore[assignment]
                                    progress_widget.progress(1.0, text="Kész")
                                    plan_status.update(
                                        label="Kiindulási terv kész", state="complete"
                                    )
                                    time.sleep(0.35)
                                elif tag == "err":
                                    st.error(str(build_outcome[0][1]))
                                    plan_status.update(
                                        label="Terv készítése sikertelen", state="error"
                                    )
                                elif tag == "exc":
                                    ex = build_outcome[0][1]
                                    if isinstance(ex, BaseException):
                                        st.exception(ex)
                                    else:
                                        st.error(str(ex))
                                    plan_status.update(
                                        label="Terv készítése sikertelen", state="error"
                                    )
                    tmp_del.unlink(missing_ok=True)

                    if plan is not None:
                        _clear_segment_ocr_widget_keys()
                        _clear_step3_tag_override_snapshots()
                        _clear_plan_exclude_multiselect_keys()
                        _clear_demoted_delimiter_paths()
                        _clear_forced_delimiter_paths()
                        _clear_plan_scan_cache()
                        if scan_holder:
                            st.session_state["_plan_scan_cache"] = scan_holder[0]
                        _clear_sort_completed_notice()
                        _clear_cloud_zip_result()
                        st.session_state["_plan"] = plan
                        st.session_state["_out"] = str(out_root)
                        st.session_state["_src"] = str(source)
                        st.session_state["_recursive"] = recursive
                        st.session_state["_del_bytes"] = del_file.getvalue()
                        st.session_state["_del_name"] = del_file.name
                        st.session_state["_delimiter_demotions_dirty"] = False
                        _bump_plan_generation()
                        st.balloons()
                        st.success("Kiindulási terv kész. Folytasd a **2 — Határolók** lappal.")

        if st.button("Terv és munkamenet törlése", key="btn_reset_all_plan"):
            _clear_segment_ocr_widget_keys()
            _clear_step3_tag_override_snapshots()
            _clear_plan_exclude_multiselect_keys()
            _clear_demoted_delimiter_paths()
            _clear_forced_delimiter_paths()
            _clear_plan_scan_cache()
            _clear_sort_completed_notice()
            _clear_cloud_zip_result()
            st.session_state.pop("_plan", None)
            st.session_state.pop("_del_bytes", None)
            st.session_state.pop("_del_name", None)
            st.rerun()

    plan_live = st.session_state.get("_plan")
    if not isinstance(plan_live, OrganizePlan):
        plan_live = None

    with tab2:
        if plan_live is None:
            _render_plan_required_notice()
        else:
            _render_step2_delimiter_finalize(plan_live)

    # A 4/5. lépés gombjai a teljes appot újrafuttatják; tab3 render gyakran a legdrágább.
    # Ezért a 4/5. lépést előbb rendereljük, így a progress UI hamarabb látszik.
    with tab4:
        if plan_live is None:
            _render_plan_required_notice()
        else:
            _render_step4_finalize_plan()

    with tab5:
        _render_cloud_zip_download()
        if plan_live is None:
            _render_plan_required_notice()
        elif not _is_upload_mode() and not (st.session_state.get("metal_out_dir") or "").strip():
            st.warning("A **cél gyökér** mappa nincs megadva az 1. lapon.")
        else:
            _render_step5_execute_block()

    with tab3:
        if plan_live is None:
            _render_plan_required_notice()
        else:
            _render_step3_tag_mappa(plan_live)

    _flush_pending_rerun()


if __name__ == "__main__":
    main()
