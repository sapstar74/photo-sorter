"""
Fotó szétválogató — Streamlit.
Forrás mappa képeit kategória gombokkal másolod vagy áthelyezed cél almappákba.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from metal_batch_logic import normalize_user_path

import streamlit as st
from PIL import Image

from folder_picker import ask_directory

_BROWSE_WARN_KEY = "_browse_barrier_msg_ps"

IMAGE_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".tiff",
    ".tif",
    ".heic",
    ".heif",
}


def _register_heif_opener() -> None:
    """HEIC/HEIF megnyitás bekapcsolása a PIL-be (pillow-heif); hiba esetén naplóz és tovább."""
    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "pillow-heif nem elérhető (%s) — HEIC/HEIF fájlok kimaradhatnak.", exc
        )


_register_heif_opener()


def list_images(source: Path, recursive: bool) -> list[Path]:
    if not source.is_dir():
        return []
    paths: list[Path] = []
    if recursive:
        for p in source.rglob("*"):
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES:
                paths.append(p)
    else:
        for p in source.iterdir():
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES:
                paths.append(p)
    return sorted(paths, key=lambda x: x.name.lower())


def unique_dest(dest_dir: Path, filename: str) -> Path:
    dest = dest_dir / filename
    if not dest.exists():
        return dest
    stem = Path(filename).stem
    suf = Path(filename).suffix
    for i in range(1, 10_000):
        cand = dest_dir / f"{stem}_{i}{suf}"
        if not cand.exists():
            return cand
    return dest_dir / f"{stem}_{uuid.uuid4().hex[:8]}{suf}"


def apply_action(src: Path, dest_dir: Path, copy_mode: bool) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = unique_dest(dest_dir, src.name)
    if copy_mode:
        shutil.copy2(src, dest)
    else:
        shutil.move(str(src), str(dest))
    return dest


def init_state() -> None:
    defaults = {
        "ps_source": "",
        "ps_out_root": "",
        "ps_categories": "Megtartandó\nUtazás\n_kuka",
        "ps_recursive": False,
        "ps_copy": False,
        "ps_files": [],
        "ps_idx": 0,
        "ps_last_action": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v
    if "in_source" not in st.session_state:
        st.session_state.in_source = st.session_state.ps_source
    if "in_out_root" not in st.session_state:
        st.session_state.in_out_root = st.session_state.ps_out_root


def _flush_browse_warning_ps() -> None:
    msg = st.session_state.pop(_BROWSE_WARN_KEY, None)
    if msg:
        st.warning(msg)


def _browse_path_apply(state_key: str, dialog_title: str) -> None:
    st.session_state.pop(_BROWSE_WARN_KEY, None)
    picked = ask_directory(dialog_title)
    if picked is None:
        st.session_state[_BROWSE_WARN_KEY] = (
            "A mappaválasztó nem érhető el ezen a környezeten. Írd be kézzel az útvonalat."
        )
    elif picked:
        st.session_state[state_key] = str(normalize_user_path(picked))


def main() -> None:
    st.set_page_config(page_title="Fotó szétválogató", layout="wide")
    init_state()

    st.title("Fotó szétválogató")
    st.caption("Forrás mappa → kategória gombokkal másolás vagy áthelyezés.")

    _flush_browse_warning_ps()

    c_src_a, c_src_b = st.columns([5, 1])
    with c_src_a:
        st.text_input(
            "Forrás mappa (abszolút útvonal)",
            key="in_source",
            placeholder="/teljes/út/forrás/mappához",
            help="Kézzel vagy Tallózással.",
        )
    with c_src_b:
        st.markdown('<div style="height: 1.6rem"></div>', unsafe_allow_html=True)
        st.button(
            "Tallózás…",
            key="browse_ps_src",
            help="Rendszer mappaválasztó",
            on_click=_browse_path_apply,
            args=("in_source", "Forrás mappa"),
        )

    source_str = (st.session_state.get("in_source") or "").strip()
    out_default = (
        str(normalize_user_path(source_str) / "szétválogatva") if source_str else ""
    )

    c_out_a, c_out_b = st.columns([5, 1])
    with c_out_a:
        st.text_input(
            "Cél gyökér (ide kerülnek a kategória mappák)",
            key="in_out_root",
            placeholder=out_default or "/teljes/út/cél/mappához",
            help="Ha üres, a forrás alatti „szétválogatva” mappa lesz az alapértelmezett.",
        )
    with c_out_b:
        st.markdown('<div style="height: 1.6rem"></div>', unsafe_allow_html=True)
        st.button(
            "Tallózás…",
            key="browse_ps_out",
            help="Rendszer mappaválasztó",
            on_click=_browse_path_apply,
            args=("in_out_root", "Cél gyökér mappa"),
        )

    out_str = (st.session_state.get("in_out_root") or "").strip() or out_default
    st.session_state.ps_source = source_str
    st.session_state.ps_out_root = out_str

    cats_raw = st.text_area(
        "Kategóriák (soronként egy mappanév)",
        value=st.session_state.ps_categories,
        height=120,
        key="in_cats",
    )
    st.session_state.ps_categories = cats_raw

    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        recursive = st.checkbox("Almappák is (rekurzív)", value=st.session_state.ps_recursive)
        st.session_state.ps_recursive = recursive
    with c2:
        copy_mode = st.checkbox("Másolás (ha ki van pipálva: áthelyezés)", value=st.session_state.ps_copy)
        st.session_state.ps_copy = copy_mode

    with c3:
        if st.button("Képek betöltése / lista frissítése", type="primary"):
            src = normalize_user_path(st.session_state.ps_source)
            st.session_state.ps_files = [str(p) for p in list_images(src, recursive)]
            st.session_state.ps_idx = 0
            st.session_state.ps_last_action = None
            if not st.session_state.ps_files:
                st.warning("Nincs kép a megadott mappában (vagy a mappa nem létezik).")
            else:
                st.success(f"{len(st.session_state.ps_files)} kép betöltve.")

    files: list[str] = st.session_state.ps_files
    idx: int = int(st.session_state.ps_idx)
    categories = [ln.strip() for ln in cats_raw.splitlines() if ln.strip()]

    if not files:
        st.info("Add meg a forrás mappát, majd kattints a **Képek betöltése** gombra.")
        return

    if idx >= len(files):
        st.balloons()
        st.success("Kész — nincs több kép ebben a listában.")
        if st.button("Lista újraépítése (üres forrás ellenőrzéséhez)"):
            src = normalize_user_path(st.session_state.ps_source)
            st.session_state.ps_files = [str(p) for p in list_images(src, st.session_state.ps_recursive)]
            st.session_state.ps_idx = 0
            st.rerun()
        return

    current = Path(files[idx])
    out_root = normalize_user_path(st.session_state.ps_out_root)

    st.divider()
    left, right = st.columns([1, 1])
    with left:
        st.subheader(f"Kép {idx + 1} / {len(files)}")
        st.code(str(current), language=None)
        try:
            img = Image.open(current)
            st.image(img, use_container_width=True)
        except Exception as e:
            st.error(f"Nem sikerült megnyitni: {e}")
            if st.button("Kihagyás (következő)", key="skip_bad"):
                st.session_state.ps_idx = idx + 1
                st.rerun()

    with right:
        st.subheader("Kategória")
        verb = "Másolás ide:" if copy_mode else "Áthelyezés ide:"
        for cat in categories:
            safe = cat.replace("/", "_").replace("\\", "_")
            dest_dir = out_root / safe
            if st.button(f"{verb} **{cat}**", key=f"cat_{safe}_{idx}"):
                try:
                    dest = apply_action(current, dest_dir, copy_mode)
                    st.session_state.ps_last_action = {
                        "src": str(current),
                        "dest": str(dest),
                        "copy": copy_mode,
                    }
                    new_list = [f for f in files if f != str(current)]
                    st.session_state.ps_files = new_list
                    st.session_state.ps_idx = min(idx, max(0, len(new_list) - 1))
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        st.divider()
        cc1, cc2 = st.columns(2)
        with cc1:
            if st.button("Kihagyás (nem nyúlunk hozzá)", key="skip"):
                st.session_state.ps_idx = idx + 1
                st.rerun()
        with cc2:
            last = st.session_state.ps_last_action
            if last and st.button("Utolsó művelet visszavonása"):
                try:
                    d = Path(last["dest"])
                    s = Path(last["src"])
                    if last["copy"]:
                        if d.exists():
                            d.unlink()
                    else:
                        if d.exists():
                            d.rename(s)
                    if str(current) not in st.session_state.ps_files:
                        st.session_state.ps_files.insert(idx, last["src"])
                    st.session_state.ps_last_action = None
                    st.rerun()
                except Exception as e:
                    st.error(f"Visszavonás sikertelen: {e}")


if __name__ == "__main__":
    main()
