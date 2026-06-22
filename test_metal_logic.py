"""Gyors ellenőrzések a photo_sorter logikájához (futtatás: python test_metal_logic.py)."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from metal_batch_logic import (
    IMAGE_SUFFIXES,
    sort_media_paths_by_name_then_mtime,
    safe_folder_name,
)


def test_sort_by_name_then_mtime() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        names = [
            "20240515_114605.jpg",
            "20240515_091839.jpg",
            "20240515_091911.jpg",
        ]
        paths = []
        for i, name in enumerate(names):
            p = root / name
            p.write_bytes(b"x")
            # mtime fordított sorrendben — a névnek kell nyerni
            t = 1_700_000_000 + (len(names) - i) * 100
            import os

            os.utime(p, (t, t))
            paths.append(p)
        ordered = sort_media_paths_by_name_then_mtime(paths)
        assert [p.name for p in ordered] == [
            "20240515_091839.jpg",
            "20240515_091911.jpg",
            "20240515_114605.jpg",
        ]


def test_safe_folder_name() -> None:
    assert safe_folder_name("  A/B:test  ") == "A_B_test"
    assert safe_folder_name("") == "azonosítatlan"


def test_apply_step3_tag_edits_to_plan() -> None:
    from organizer_metal_app import apply_step3_tag_edits_to_plan
    from metal_batch_logic import OrganizePlan, Segment

    root = Path("/tmp/photo_sorter_test_tags")
    delim = root / "20240515_091800_delim.jpg"
    plate = root / "20240515_091839.jpg"
    seg = Segment(
        folder_key="OCR_DEFAULT",
        plate_image=plate,
        ocr_raw="OCR_DEFAULT",
        photos=[plate],
        closed_by_delimiter=delim,
    )
    plan = OrganizePlan(segments=[seg], delimiter_hits=[delim])
    preview = [(delim, [plate])]
    applied = apply_step3_tag_edits_to_plan(
        plan,
        tag_by_dix={0: "  Felhasználó / mappa  "},
        tag_by_seg={},
        preview_rows=preview,
        files_ord=None,
    )
    assert applied.segments[0].ocr_raw == "Felhasználó / mappa"
    assert applied.segments[0].folder_key == "Felhasználó___mappa"

    # Widget kulcsok nélkül: stabil határoló-útvonal mentés
    restored = apply_step3_tag_edits_to_plan(
        OrganizePlan(
            segments=[
                Segment(
                    folder_key="UJ_OCR",
                    plate_image=plate,
                    ocr_raw="UJ_OCR",
                    photos=[plate],
                    closed_by_delimiter=delim,
                )
            ],
            delimiter_hits=[delim],
        ),
        tag_by_dix={},
        tag_by_seg={},
        preview_rows=preview,
        files_ord=None,
        tag_by_delim={str(delim.expanduser()): "Felhasználó / mappa"},
    )
    assert restored.segments[0].folder_key == "Felhasználó___mappa"


def test_pick_step3_tag_when_later_block_has_default_ocr() -> None:
    """Utolsó blokk OCR-alapértelmezése ne írja felül az első blokk szerkesztett nevét."""
    from organizer_metal_app import apply_step3_tag_edits_to_plan, pick_step3_tag_for_segment
    from metal_batch_logic import OrganizePlan, Segment

    root = Path("/tmp/photo_sorter_test_multi_pick")
    delim1 = root / "delim1.jpg"
    delim2 = root / "delim2.jpg"
    plate = root / "plate.jpg"
    seg = Segment(
        folder_key="OCR_DEFAULT",
        plate_image=plate,
        ocr_raw="OCR_DEFAULT",
        photos=[plate, root / "p2.jpg"],
        closed_by_delimiter=delim2,
    )
    plan = OrganizePlan(segments=[seg])
    preview = [(delim1, [plate]), (delim2, [root / "p2.jpg"])]

    picked = pick_step3_tag_for_segment(
        segment=seg,
        original_ocr="OCR_DEFAULT",
        d_list=[0, 1],
        tag_by_dix={0: "USER_NAME", 1: "OCR_DEFAULT"},
        preview_rows=preview,
        by_delim={},
        by_segment={},
    )
    assert picked == "USER_NAME"

    applied = apply_step3_tag_edits_to_plan(
        plan,
        tag_by_dix={0: "USER_NAME", 1: "OCR_DEFAULT"},
        tag_by_seg={},
        preview_rows=preview,
        files_ord=None,
    )
    assert applied.segments[0].ocr_raw == "USER_NAME"
    assert applied.segments[0].folder_key == safe_folder_name("USER_NAME")


def test_step3_edited_folder_name_survives_step4_rebuild_and_step5_prepare() -> None:
    from organizer_metal_app import apply_step3_tag_edits_to_plan, snapshot_step3_tag_overrides_from_plan
    from metal_batch_logic import OrganizePlan, Segment

    root = Path("/tmp/photo_sorter_step3_step5_override")
    delim = root / "d1.jpg"
    plate = root / "p1.jpg"
    old_plan = OrganizePlan(
        segments=[
            Segment(
                folder_key="OCR_DEFAULT",
                plate_image=plate,
                ocr_raw="OCR_DEFAULT",
                photos=[plate],
                closed_by_delimiter=delim,
            )
        ],
        delimiter_hits=[delim],
    )
    preview_old = [(delim, [plate])]
    edited_old = apply_step3_tag_edits_to_plan(
        old_plan,
        tag_by_dix={0: "xxx"},
        tag_by_seg={},
        preview_rows=preview_old,
        files_ord=[delim, plate],
    )
    assert edited_old.segments[0].folder_key == "xxx"
    by_delim, by_plate, by_segment = snapshot_step3_tag_overrides_from_plan(
        edited_old, preview_old, [delim, plate]
    )

    rebuilt_plan = OrganizePlan(
        segments=[
            Segment(
                folder_key="OCR_DEFAULT",
                plate_image=plate,
                ocr_raw="OCR_DEFAULT",
                photos=[plate],
                closed_by_delimiter=None,
            )
        ],
        delimiter_hits=[],
    )
    step4_applied = apply_step3_tag_edits_to_plan(
        rebuilt_plan,
        tag_by_dix={},
        tag_by_seg={},
        preview_rows=[],
        files_ord=[plate],
        tag_by_delim=by_delim,
        tag_by_plate=by_plate,
        tag_by_segment=by_segment,
    )
    assert step4_applied.segments[0].folder_key == "xxx"

    step5_prepared = apply_step3_tag_edits_to_plan(
        step4_applied,
        tag_by_dix={},
        tag_by_seg={},
        preview_rows=[],
        files_ord=[plate],
        tag_by_delim=by_delim,
        tag_by_plate=by_plate,
        tag_by_segment=by_segment,
    )
    assert step5_prepared.segments[0].folder_key == "xxx"


def test_step3_override_survives_when_segment_plate_changes_after_rebuild() -> None:
    from organizer_metal_app import apply_step3_tag_edits_to_plan, snapshot_step3_tag_overrides_from_plan
    from metal_batch_logic import OrganizePlan, Segment

    root = Path("/tmp/photo_sorter_step3_identity_members")
    delim = root / "d1.jpg"
    p1 = root / "p1.jpg"
    p2 = root / "p2.jpg"
    old_plan = OrganizePlan(
        segments=[
            Segment(
                folder_key="OCR_DEFAULT",
                plate_image=p1,
                ocr_raw="OCR_DEFAULT",
                photos=[p1, p2],
                closed_by_delimiter=delim,
            )
        ],
        delimiter_hits=[delim],
    )
    edited_old = apply_step3_tag_edits_to_plan(
        old_plan,
        tag_by_dix={0: "xxx"},
        tag_by_seg={},
        preview_rows=[(delim, [p1])],
        files_ord=[delim, p1, p2],
    )
    by_delim, by_plate, by_segment = snapshot_step3_tag_overrides_from_plan(
        edited_old, [(delim, [p1])], [delim, p1, p2]
    )

    rebuilt_plan = OrganizePlan(
        segments=[
            Segment(
                folder_key="OCR_DEFAULT",
                plate_image=p2,
                ocr_raw="OCR_DEFAULT",
                photos=[p1, p2],
                closed_by_delimiter=None,
            )
        ],
        delimiter_hits=[],
    )
    step4_applied = apply_step3_tag_edits_to_plan(
        rebuilt_plan,
        tag_by_dix={},
        tag_by_seg={},
        preview_rows=[],
        files_ord=[p1, p2],
        tag_by_delim=by_delim,
        tag_by_plate=by_plate,
        tag_by_segment=by_segment,
    )
    assert step4_applied.segments[0].folder_key == "xxx"


def test_step3_override_survives_multiple_rebuild_roundtrips() -> None:
    from organizer_metal_app import apply_step3_tag_edits_to_plan, snapshot_step3_tag_overrides_from_plan
    from metal_batch_logic import OrganizePlan, Segment

    root = Path("/tmp/photo_sorter_step3_multi_rebuild")
    delim = root / "d1.jpg"
    plate = root / "p1.jpg"
    old_plan = OrganizePlan(
        segments=[
            Segment(
                folder_key="OCR_DEFAULT",
                plate_image=plate,
                ocr_raw="OCR_DEFAULT",
                photos=[plate],
                closed_by_delimiter=delim,
            )
        ],
        delimiter_hits=[delim],
    )
    edited_old = apply_step3_tag_edits_to_plan(
        old_plan,
        tag_by_dix={0: "xxx"},
        tag_by_seg={},
        preview_rows=[(delim, [plate])],
        files_ord=[delim, plate],
    )
    by_delim, by_plate, by_segment = snapshot_step3_tag_overrides_from_plan(
        edited_old, [(delim, [plate])], [delim, plate]
    )

    rebuilt1 = OrganizePlan(
        segments=[
            Segment(
                folder_key="OCR_DEFAULT",
                plate_image=plate,
                ocr_raw="OCR_DEFAULT",
                photos=[plate],
                closed_by_delimiter=None,
            )
        ],
        delimiter_hits=[],
    )
    rebuilt1 = apply_step3_tag_edits_to_plan(
        rebuilt1,
        tag_by_dix={},
        tag_by_seg={},
        preview_rows=[],
        files_ord=[plate],
        tag_by_delim=by_delim,
        tag_by_plate=by_plate,
        tag_by_segment=by_segment,
    )
    assert rebuilt1.segments[0].folder_key == "xxx"

    by_delim2, by_plate2, by_segment2 = snapshot_step3_tag_overrides_from_plan(rebuilt1, [], [plate])
    rebuilt2 = OrganizePlan(
        segments=[
            Segment(
                folder_key="OCR_DEFAULT",
                plate_image=plate,
                ocr_raw="OCR_DEFAULT",
                photos=[plate],
                closed_by_delimiter=None,
            )
        ],
        delimiter_hits=[],
    )
    rebuilt2 = apply_step3_tag_edits_to_plan(
        rebuilt2,
        tag_by_dix={},
        tag_by_seg={},
        preview_rows=[],
        files_ord=[plate],
        tag_by_delim=by_delim2,
        tag_by_plate=by_plate2,
        tag_by_segment=by_segment2,
    )
    assert rebuilt2.segments[0].folder_key == "xxx"


def test_delimiter_table_paths_from_fallback() -> None:
    from organizer_metal_app import (
        _list_delimiter_followers_fallback_from_plan,
        _norm_path_str,
    )
    from metal_batch_logic import OrganizePlan

    root = Path("/tmp/photo_sorter_test_delim_table")
    d1 = root / "20240515_091800_delim.jpg"
    d2 = root / "20240515_114605_delim.jpg"
    mid = root / "20240515_091839.jpg"
    files = [d1, mid, d2]
    plan = OrganizePlan(segments=[], delimiter_hits=[d1, d2])
    rows = _list_delimiter_followers_fallback_from_plan(files, plan, set(), set(), following_max=1)
    paths = [p for p, _ in rows]
    assert [_norm_path_str(p) for p in paths] == [_norm_path_str(d1), _norm_path_str(d2)]


def test_step2_candidate_list_filters_committed_demotions() -> None:
    """2. lépés táblázat: az érvényesített nem-határolók kikerülnek a listából."""
    from organizer_metal_app import (
        _list_delimiter_followers_fallback_from_plan,
        _norm_path_str,
    )
    from metal_batch_logic import OrganizePlan

    root = Path("/tmp/photo_sorter_test_step2_unfiltered")
    d1 = root / "a_delim.jpg"
    d2 = root / "b_delim.jpg"
    mid = root / "between.jpg"
    files = [d1, mid, d2]
    plan = OrganizePlan(segments=[], delimiter_hits=[d1, d2])
    rows_table = _list_delimiter_followers_fallback_from_plan(
        files, plan, set(), set(), following_max=1
    )
    rows_after_commit = _list_delimiter_followers_fallback_from_plan(
        files, plan, {_norm_path_str(d2)}, set(), following_max=1
    )
    table_delims = {_norm_path_str(p) for p, _ in rows_table}
    filtered_delims = {_norm_path_str(p) for p, _ in rows_after_commit}
    assert table_delims == {_norm_path_str(d1), _norm_path_str(d2)}
    assert filtered_delims == {_norm_path_str(d1)}


def test_demoted_delimiter_excluded_from_preview_rows() -> None:
    from organizer_metal_app import (
        _list_delimiter_followers_fallback_from_plan,
        _norm_path_str,
    )
    from metal_batch_logic import OrganizePlan

    root = Path("/tmp/photo_sorter_test_demoted_preview")
    d1 = root / "a_delim.jpg"
    d2 = root / "b_delim.jpg"
    mid = root / "between.jpg"
    files = [d1, mid, d2]
    plan = OrganizePlan(segments=[], delimiter_hits=[d1, d2])
    skip = {_norm_path_str(d2)}
    rows = _list_delimiter_followers_fallback_from_plan(
        files, plan, skip, set(), following_max=2
    )
    delim_paths = {_norm_path_str(p) for p, _ in rows}
    assert _norm_path_str(d2) not in delim_paths
    assert _norm_path_str(d1) in delim_paths
    assert len(rows) == 1
    follower_ns = {_norm_path_str(f) for f in rows[0][1]}
    assert _norm_path_str(mid) in follower_ns
    assert _norm_path_str(d2) in follower_ns


def test_demoted_paths_from_delimiter_paths() -> None:
    from organizer_metal_app import demoted_paths_from_delimiter_paths

    root = Path("/tmp/photo_sorter_test_demote")
    paths = [root / "a_delim.jpg", root / "b_delim.jpg", root / "c_delim.jpg"]
    demoted_ns = {str(paths[1].expanduser())}
    out = demoted_paths_from_delimiter_paths(
        paths,
        is_demoted=lambda ns: ns in demoted_ns,
    )
    assert out == [str(paths[1].expanduser())]


def test_flush_pending_rerun_only_scope() -> None:
    """_flush_pending_rerun(only_scope=…) ne nyelje el a másik scope pendingjét."""
    from organizer_metal_app import _PENDING_RERUN_SCOPE_KEY, _flush_pending_rerun, _request_rerun

    class _FakeRerun(Exception):
        def __init__(self, scope: str) -> None:
            self.scope = scope

    import organizer_metal_app as app_mod

    state: dict = {}
    app_mod.st.session_state = state  # type: ignore[assignment]
    calls: list[str] = []

    def fake_rerun(*, scope: str = "app") -> None:
        calls.append(scope)
        raise _FakeRerun(scope)

    orig = app_mod.st.rerun
    app_mod.st.rerun = fake_rerun  # type: ignore[method-assign]
    try:
        _request_rerun(scope="app")
        try:
            _flush_pending_rerun(only_scope="fragment")
        except _FakeRerun:
            pass
        assert state[_PENDING_RERUN_SCOPE_KEY] == "app"
        assert calls == []
        try:
            _flush_pending_rerun(only_scope="app")
        except _FakeRerun as e:
            assert e.scope == "app"
        assert _PENDING_RERUN_SCOPE_KEY not in state
        assert calls == ["app"]
    finally:
        app_mod.st.rerun = orig  # type: ignore[method-assign]


def test_filter_path_list_excluding_norm() -> None:
    from organizer_metal_app import filter_path_list_excluding_norm, _norm_path_str

    a = "/tmp/a.jpg"
    b = "/tmp/b.jpg"
    out = filter_path_list_excluding_norm([a, b], {_norm_path_str(b)})
    assert out == [a]


def test_step2_keymap_paths_without_rescan() -> None:
    from organizer_metal_app import (
        _demoted_paths_from_step2_keymap,
        _set_step2_dem_keymap,
        _step2_dem_checkbox_key,
    )
    import organizer_metal_app as app_mod

    state: dict = {}
    app_mod.st.session_state = state  # type: ignore[assignment]
    gen = 7
    a = "/tmp/a_delim.jpg"
    b = "/tmp/b_delim.jpg"
    ka = _step2_dem_checkbox_key(gen, a)
    kb = _step2_dem_checkbox_key(gen, b)
    _set_step2_dem_keymap(gen, {ka: a, kb: b})
    state[ka] = True
    state[kb] = False
    assert _demoted_paths_from_step2_keymap(gen) == [a]


def test_step2_widget_read_keeps_hidden_committed_demotions() -> None:
    from organizer_metal_app import (
        _demoted_paths_from_step2_widgets,
        _set_step2_dem_keymap,
        _step2_dem_checkbox_key,
    )
    import organizer_metal_app as app_mod

    state: dict = {}
    app_mod.st.session_state = state  # type: ignore[assignment]
    gen = 11
    visible = "/tmp/visible_delim.jpg"
    hidden_committed = "/tmp/hidden_delim.jpg"
    chk = _step2_dem_checkbox_key(gen, visible)
    _set_step2_dem_keymap(gen, {chk: visible})
    state[chk] = False
    state["_demoted_delimiter_paths"] = [hidden_committed]

    out = _demoted_paths_from_step2_widgets(gen, [Path(visible)])
    assert out == [hidden_committed]


def test_delete_image_files_on_disk() -> None:
    from organizer_metal_app import delete_image_files_on_disk

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "del_me.jpg"
        p.write_bytes(b"x")
        deleted, failures = delete_image_files_on_disk([str(p)])
        assert failures == []
        assert deleted == [str(p.expanduser())]
        assert not p.exists()
        _deleted2, failures2 = delete_image_files_on_disk([str(p)])
        assert failures2 and "nem létezik" in failures2[0][1]


def test_prune_organize_plan_removed_paths() -> None:
    from organizer_metal_app import prune_organize_plan_removed_paths, _norm_path_str
    from metal_batch_logic import OrganizePlan, Segment

    root = Path("/tmp/photo_sorter_test_prune")
    d1 = root / "d1.jpg"
    d2 = root / "d2.jpg"
    plate = root / "plate.jpg"
    seg = Segment(
        folder_key="k",
        plate_image=plate,
        ocr_raw="x",
        photos=[plate, d1],
        closed_by_delimiter=d1,
    )
    plan = OrganizePlan(segments=[seg], delimiter_hits=[d1, d2])
    pruned = prune_organize_plan_removed_paths(plan, {_norm_path_str(d1)})
    assert _norm_path_str(d1) not in {_norm_path_str(h) for h in pruned.delimiter_hits}
    assert pruned.segments[0].closed_by_delimiter is None
    assert all(_norm_path_str(ph) != _norm_path_str(d1) for ph in pruned.segments[0].photos)


def test_four_delimiters_can_yield_three_segments() -> None:
    """
    A határolók száma nem egyezik a TAG/mappa szegmensek számával:
    - vezető határoló (nincs előtte fotóblokk) nem nyit új mappát;
    - két egymás utáni határoló között üres fotóintervallum nem hoz létre szegmenst.
    """
    from metal_batch_logic import _segment_media_to_plan

    root = Path("/tmp/photo_sorter_test_4d3s")
    d1, d2, d3, d4 = (
        root / "delim1.jpg",
        root / "delim2.jpg",
        root / "delim3.jpg",
        root / "delim4.jpg",
    )
    p1, p2, p3 = root / "plate1.jpg", root / "plate2.jpg", root / "plate3.jpg"
    files = [d1, p1, d2, p2, d3, p3, d4]
    force = {str(x) for x in (d1, d2, d3, d4)}

    def ocr(_p: Path) -> str:
        return "TAG"

    plan = _segment_media_to_plan(
        files,
        hash_by_path={},
        ref_phash=None,  # type: ignore[arg-type]
        ref_ahash=None,  # type: ignore[arg-type]
        max_hamming=12,
        skip_del=set(),
        force_del=force,
        ocr=ocr,
        ocr_by_path={},
        use_ocr_cache=False,
        progress=None,
    )
    assert len(plan.delimiter_hits) == 4
    assert len(plan.segments) == 3

    files2 = [p1, d1, d2, p2, d3, p3, d4]
    plan2 = _segment_media_to_plan(
        files2,
        hash_by_path={},
        ref_phash=None,  # type: ignore[arg-type]
        ref_ahash=None,  # type: ignore[arg-type]
        max_hamming=12,
        skip_del=set(),
        force_del=force,
        ocr=ocr,
        ocr_by_path={},
        use_ocr_cache=False,
        progress=None,
    )
    assert len(plan2.delimiter_hits) == 4
    assert len(plan2.segments) == 3


def test_four_delimiters_three_photo_blocks_yield_three_segments() -> None:
    """
    N határoló nem jelent N mappát: üres szegmens (határoló előtti/utáni vagy egymás utáni
    határolók között nincs fénykép) nem nyit TAG/mappa szegmenst.
    """
    from metal_batch_logic import OrganizePlan, _segment_media_to_plan
    import imagehash

    root = Path("/tmp/photo_sorter_test_delim_seg_count")
    d1, p1, d2, p2, d3, p3, d4 = (
        root / "01_delim.jpg",
        root / "02_plate.jpg",
        root / "03_delim.jpg",
        root / "04_plate.jpg",
        root / "05_delim.jpg",
        root / "06_plate.jpg",
        root / "07_delim.jpg",
    )
    files = [d1, p1, d2, p2, d3, p3, d4]
    force = {str(x) for x in (d1, d2, d3, d4)}
    ref = imagehash.hex_to_hash("0000000000000000")

    plan: OrganizePlan = _segment_media_to_plan(
        files,
        {},
        ref,
        ref,
        max_hamming=12,
        skip_del=set(),
        force_del=force,
        ocr=lambda p: p.stem,
        ocr_by_path={},
        use_ocr_cache=False,
        progress=None,
    )
    assert len(plan.delimiter_hits) == 4
    assert len(plan.segments) == 3
    assert [len(s.photos) for s in plan.segments] == [1, 1, 1]


def test_replay_plan_from_cache_prefers_cached_delimiter_candidates() -> None:
    from metal_batch_logic import PlanScanCache, replay_plan_from_cache
    import imagehash

    root = Path("/tmp/photo_sorter_test_replay_fast")
    d1 = root / "01_delim.jpg"
    p1 = root / "02_plate.jpg"
    d2 = root / "03_delim.jpg"
    p2 = root / "04_plate.jpg"
    files = [d1, p1, d2, p2]
    ref = imagehash.hex_to_hash("0000000000000000")
    cache = PlanScanCache(
        files=files,
        hash_by_path={},
        ref_phash=ref,
        ref_ahash=ref,
        max_hamming=12,
        inner_ratio=0.92,
        source_str="/tmp/irrelevant",
        recursive=False,
        delimiter_candidates={str(d1.expanduser()), str(d2.expanduser())},
        ocr_by_path={},
    )
    ocr_calls: list[Path] = []

    def fake_ocr(p: Path) -> str:
        ocr_calls.append(p)
        return p.stem

    plan = replay_plan_from_cache(cache, ocr_fn=fake_ocr)
    assert len(plan.delimiter_hits) == 2
    assert [s.plate_image for s in plan.segments] == [p1, p2]
    assert ocr_calls == [p1, p2]


def test_list_delimiter_followers_preview_prefers_cached_candidates() -> None:
    from metal_batch_logic import PlanScanCache, list_delimiter_followers_preview
    import imagehash

    root = Path("/tmp/photo_sorter_test_preview_fast")
    d1 = root / "a_delim.jpg"
    p1 = root / "b_plate.jpg"
    d2 = root / "c_delim.jpg"
    files = [d1, p1, d2]
    ref = imagehash.hex_to_hash("0000000000000000")
    cache = PlanScanCache(
        files=files,
        hash_by_path={},
        ref_phash=ref,
        ref_ahash=ref,
        max_hamming=12,
        inner_ratio=0.92,
        source_str="/tmp/irrelevant",
        recursive=False,
        delimiter_candidates={str(d1.expanduser()), str(d2.expanduser())},
        ocr_by_path={},
    )
    rows = list_delimiter_followers_preview(cache, non_delimiter_paths=[], force_delimiter_paths=[])
    assert [r[0] for r in rows] == [d1, d2]
    assert rows[0][1] == [p1]
    assert rows[1][1] == []


def test_replay_plan_from_cache_uses_cached_file_order_when_marked_sorted() -> None:
    import imagehash
    import metal_batch_logic as mbl
    from metal_batch_logic import PlanScanCache, replay_plan_from_cache

    root = Path("/tmp/photo_sorter_test_replay_no_resort")
    d1 = root / "01_delim.jpg"
    p1 = root / "02_plate.jpg"
    d2 = root / "03_delim.jpg"
    files = [d1, p1, d2]
    ref = imagehash.hex_to_hash("0000000000000000")
    cache = PlanScanCache(
        files=files,
        hash_by_path={},
        ref_phash=ref,
        ref_ahash=ref,
        max_hamming=12,
        inner_ratio=0.92,
        source_str="/tmp/irrelevant",
        recursive=False,
        image_count=3,
        files_sorted_by_name_mtime=True,
        delimiter_candidates={str(d1.expanduser()), str(d2.expanduser())},
        ocr_by_path={},
    )
    orig_sort = mbl.sort_media_paths_by_name_then_mtime

    def _boom(_paths):
        raise AssertionError("sort_media_paths_by_name_then_mtime should not be called")

    mbl.sort_media_paths_by_name_then_mtime = _boom  # type: ignore[assignment]
    try:
        plan = replay_plan_from_cache(cache, ocr_fn=lambda p: p.stem)
    finally:
        mbl.sort_media_paths_by_name_then_mtime = orig_sort  # type: ignore[assignment]
    assert len(plan.delimiter_hits) == 2
    assert [s.plate_image for s in plan.segments] == [p1]


def test_step3_ordered_media_files_uses_sorted_cache_without_resort() -> None:
    import imagehash
    import organizer_metal_app as app_mod
    import metal_batch_logic as mbl
    from metal_batch_logic import OrganizePlan, PlanScanCache

    root = Path("/tmp/photo_sorter_test_step3_cache_files")
    d1 = root / "01_delim.jpg"
    p1 = root / "02_plate.jpg"
    files = [d1, p1]
    ref = imagehash.hex_to_hash("0000000000000000")
    state: dict = {
        "_src": str(root.expanduser()),
        "metal_recursive_chk": False,
        "_recursive": False,
        "metal_max_hamming": 12,
        "metal_del_inner": 0.92,
        "_plan_scan_cache": PlanScanCache(
            files=files,
            hash_by_path={},
            ref_phash=ref,
            ref_ahash=ref,
            max_hamming=12,
            inner_ratio=0.92,
            source_str=str(root.expanduser()),
            recursive=False,
            files_sorted_by_name_mtime=True,
            ocr_by_path={},
        ),
    }
    app_mod.st.session_state = state  # type: ignore[assignment]
    orig_sort = mbl.sort_media_paths_by_name_then_mtime

    def _boom(_paths):
        raise AssertionError("sort_media_paths_by_name_then_mtime should not be called")

    mbl.sort_media_paths_by_name_then_mtime = _boom  # type: ignore[assignment]
    try:
        got = app_mod._get_step3_ordered_media_files(OrganizePlan())
    finally:
        mbl.sort_media_paths_by_name_then_mtime = orig_sort  # type: ignore[assignment]
    assert got == files


def test_compute_step3_delimiter_preview_uses_sorted_cache_without_resort() -> None:
    import imagehash
    import organizer_metal_app as app_mod
    import metal_batch_logic as mbl
    from metal_batch_logic import OrganizePlan, PlanScanCache, Segment

    root = Path("/tmp/photo_sorter_test_step3_preview_cache")
    d1 = root / "01_delim.jpg"
    p1 = root / "02_plate.jpg"
    files = [d1, p1]
    ref = imagehash.hex_to_hash("0000000000000000")
    cache = PlanScanCache(
        files=files,
        hash_by_path={},
        ref_phash=ref,
        ref_ahash=ref,
        max_hamming=12,
        inner_ratio=0.92,
        source_str=str(root.expanduser()),
        recursive=False,
        files_sorted_by_name_mtime=True,
        delimiter_candidates={str(d1.expanduser())},
        ocr_by_path={},
    )
    state: dict = {
        "_src": str(root.expanduser()),
        "metal_recursive_chk": False,
        "_recursive": False,
        "metal_max_hamming": 12,
        "metal_del_inner": 0.92,
        "_plan_scan_cache": cache,
        "_forced_delimiter_paths": [],
        "_demoted_delimiter_paths": [],
        "_plan_generation": 1,
    }
    app_mod.st.session_state = state  # type: ignore[assignment]
    plan = OrganizePlan(
        segments=[
            Segment(folder_key="x", plate_image=p1, ocr_raw="x", photos=[p1], closed_by_delimiter=d1)
        ],
        delimiter_hits=[d1],
    )
    orig_sort = mbl.sort_media_paths_by_name_then_mtime

    def _boom(_paths):
        raise AssertionError("sort_media_paths_by_name_then_mtime should not be called")

    mbl.sort_media_paths_by_name_then_mtime = _boom  # type: ignore[assignment]
    try:
        rows, _note = app_mod._compute_step3_delimiter_preview(plan)
    finally:
        mbl.sort_media_paths_by_name_then_mtime = orig_sort  # type: ignore[assignment]
    assert rows and rows[0][0] == d1


def test_execute_plan_reports_progress() -> None:
    from metal_batch_logic import OrganizePlan, Segment, execute_plan

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "src"
        out = root / "out"
        src.mkdir(parents=True, exist_ok=True)
        p1 = src / "a.jpg"
        p2 = src / "b.pdf"
        p1.write_bytes(b"img")
        p2.write_bytes(b"pdf")
        seg = Segment(folder_key="k", plate_image=p1, ocr_raw="k", photos=[p1], pdfs=[p2])
        plan = OrganizePlan(segments=[seg])
        seen: list[tuple[float, str | None]] = []

        def _prog(f: float, m: str | None = None) -> None:
            seen.append((f, m))

        log = execute_plan(plan, out, copy_mode=True, progress=_prog)
        assert len(log) == 2
        assert seen
        assert seen[0][0] == 0.0
        assert any("1/2" in (msg or "") for _f, msg in seen)
        assert any("2/2" in (msg or "") for _f, msg in seen)
        assert seen[-1][0] == 1.0


def test_safe_image_display_helpers() -> None:
    from organizer_metal_app import _load_rgb_image, _path_image_file_ok, _safe_st_image_pil
    from PIL import Image

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        good = root / "ok.jpg"
        good.write_bytes(
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xfb\xd5\xff\xd9"
        )
        empty = root / "empty.jpg"
        empty.write_bytes(b"")
        missing = root / "gone.jpg"
        assert _path_image_file_ok(good)
        assert not _path_image_file_ok(empty)
        assert not _path_image_file_ok(missing)
        loaded = _load_rgb_image(good)
        assert loaded is not None
        assert loaded.size[0] > 0 and loaded.size[1] > 0
        assert _load_rgb_image(empty) is None
        assert _load_rgb_image(missing) is None

    tiny = Image.new("RGB", (1, 1), color=(128, 64, 32))
    assert tiny.size[0] > 0 and tiny.size[1] > 0
    # width="stretch" nem mehet st.image-nek; use_container_width a helyes API.
    import inspect

    sig = inspect.signature(_safe_st_image_pil)
    assert "use_container_width" in sig.parameters
    w_ann = str(sig.parameters["width"].annotation)
    assert "str" not in w_ann


def test_sanitize_delimiter_preview_rows() -> None:
    from organizer_metal_app import _sanitize_delimiter_preview_rows

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        keep = root / "keep.jpg"
        keep.write_bytes(b"x")
        gone = root / "gone.jpg"
        rows = [(keep, [gone]), (gone, [])]
        pruned = _sanitize_delimiter_preview_rows(rows)
        assert len(pruned) == 1
        assert pruned[0][0] == keep
        assert pruned[0][1] == []


def test_execution_plan_includes_delimiterless_renamed_segments() -> None:
    """
    A végrehajtási terv tartalmazza:
      * a határolós szegmenst,
      * a kézzel átnevezett, **határoló nélküli** szegmenst,
      * és (a meglévő viselkedés megőrzése) az át nem nevezett, határoló nélküli szegmenst.
    Külön ellenőrizzük, hogy ``drop_unedited_delimiterless=True`` esetén csak az át nem
    nevezett határoló nélküli szegmens esik ki.
    """
    from organizer_metal_app import (
        apply_step3_tag_edits_to_plan,
        select_execution_segments,
        segment_was_manually_renamed,
        _segment_identity_keys,
        _norm_path_str,
    )
    from metal_batch_logic import OrganizePlan, Segment, execute_plan

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "src"
        out = root / "out"
        src.mkdir(parents=True, exist_ok=True)

        delim = src / "00_delim.jpg"
        p_backed = src / "01_backed.jpg"
        p_plain = src / "02_plain.jpg"
        p_renamed = src / "03_renamed.jpg"
        for f in (delim, p_backed, p_plain, p_renamed):
            f.write_bytes(b"x")

        seg_backed = Segment(
            folder_key="OCR_BACKED",
            plate_image=p_backed,
            ocr_raw="OCR_BACKED",
            photos=[p_backed],
            closed_by_delimiter=delim,
        )
        seg_plain = Segment(
            folder_key="OCR_PLAIN",
            plate_image=p_plain,
            ocr_raw="OCR_PLAIN",
            photos=[p_plain],
            closed_by_delimiter=None,
        )
        seg_renamed = Segment(
            folder_key="OCR_RENAMED_DEFAULT",
            plate_image=p_renamed,
            ocr_raw="OCR_RENAMED_DEFAULT",
            photos=[p_renamed],
            closed_by_delimiter=None,
        )
        plan = OrganizePlan(
            segments=[seg_backed, seg_plain, seg_renamed],
            delimiter_hits=[delim],
        )

        # A felhasználó a 3. lépésben a határoló nélküli `seg_renamed`-et átnevezte.
        by_segment: dict[str, str] = {}
        for sid in _segment_identity_keys(seg_renamed):
            by_segment[sid] = "USER RENAMED"

        # 5. lépés előkészítés: TAG felülírások alkalmazása (mint az `apply_step3_tag_edits_to_plan` hívás).
        applied = apply_step3_tag_edits_to_plan(
            plan,
            tag_by_dix={},
            tag_by_seg={},
            preview_rows=[],
            files_ord=None,
            tag_by_delim={},
            tag_by_plate={},
            tag_by_segment=by_segment,
        )
        renamed_seg = applied.segments[2]
        assert renamed_seg.folder_key == "USER_RENAMED"
        # Új tervezés: az át nem nevezett szegmensek alapértelmezése a SORSZÁM (nem OCR).
        assert applied.segments[0].folder_key == "1"  # határolós, nincs kézi név
        assert applied.segments[1].folder_key == "2"  # határoló nélküli, nincs kézi név

        original_ocr_by_plate = {
            _norm_path_str(p_backed): "OCR_BACKED",
            _norm_path_str(p_plain): "OCR_PLAIN",
            _norm_path_str(p_renamed): "OCR_RENAMED_DEFAULT",
        }

        # „Átnevezett” felismerés: a határoló nélküli renamed igen, a plain nem.
        assert segment_was_manually_renamed(
            renamed_seg, original_ocr="OCR_RENAMED_DEFAULT", tag_by_segment=by_segment
        )
        assert not segment_was_manually_renamed(
            applied.segments[1], original_ocr="OCR_PLAIN", tag_by_segment=by_segment
        )

        # Alap (megőrzés): mindhárom bennmarad.
        kept = select_execution_segments(
            applied,
            tag_by_segment=by_segment,
            original_ocr_by_plate=original_ocr_by_plate,
            drop_unedited_delimiterless=False,
        )
        kept_keys = {s.folder_key for s in kept}
        assert kept_keys == {"1", "2", "USER_RENAMED"}

        # Csak az át nem nevezett, határoló nélküli esik ki, ha kifejezetten kérjük.
        kept_strict = select_execution_segments(
            applied,
            tag_by_segment=by_segment,
            original_ocr_by_plate=original_ocr_by_plate,
            drop_unedited_delimiterless=True,
        )
        kept_strict_keys = {s.folder_key for s in kept_strict}
        # Határolós ("1") + kézzel átnevezett ("USER_RENAMED"); az át nem nevezett "2" kiesik.
        assert kept_strict_keys == {"1", "USER_RENAMED"}
        assert "2" not in kept_strict_keys

        # Tényleges végrehajtás (alap viselkedés): a mappák létrejönnek.
        exec_plan = OrganizePlan(segments=kept, delimiter_hits=applied.delimiter_hits)
        execute_plan(exec_plan, out, copy_mode=True)
        created = {p.name for p in out.iterdir()} if out.exists() else set()
        assert "1" in created  # határolós, sorszám-alapértelmezés
        assert "USER_RENAMED" in created
        assert "2" in created  # határoló nélküli, sorszám-alapértelmezés


def test_allocate_unique_folder_name() -> None:
    from metal_batch_logic import allocate_unique_folder_name

    used: set[str] = set()
    assert allocate_unique_folder_name("ADATTABLA", used) == "ADATTABLA"
    assert allocate_unique_folder_name("ADATTABLA", used) == "ADATTABLA_2"
    assert allocate_unique_folder_name("ADATTABLA", used) == "ADATTABLA_3"
    assert allocate_unique_folder_name("MAS", used) == "MAS"
    # üres név → „azonosítatlan”, majd utótagolt
    assert allocate_unique_folder_name("", used) == "azonosítatlan"
    assert allocate_unique_folder_name("", used) == "azonosítatlan_2"
    # hossz-korlát: a teljes név (utótaggal) a max_len-en belül marad
    long = "x" * 80
    a = allocate_unique_folder_name(long, used, max_len=80)
    b = allocate_unique_folder_name(long, used, max_len=80)
    assert a == long
    assert len(b) <= 80 and b.endswith("_2")


def test_execute_plan_merges_segments_with_identical_approved_names() -> None:
    """
    ÚJ szemantika: az **azonos jóváhagyott nevű** szegmensek KÖZÖS mappába kerülnek
    (összevonás, nincs ``_2``/``_3`` utótag); az **eltérő nevűek** külön mappába.
    A megkülönböztető nevek száma == a létrejövő mappák száma; egyetlen fájl sem vész el.
    """
    from metal_batch_logic import OrganizePlan, Segment, execute_plan

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "src"
        out = root / "out"
        src.mkdir(parents=True, exist_ok=True)

        def mk(name: str) -> Path:
            p = src / name
            p.write_bytes(b"x")
            return p

        segs: list[Segment] = []
        delims: list[Path] = []
        total_files = 0
        # 3 határolós, AZONOS OCR-alapnévvel (boilerplate) → ütköznek
        for i in range(3):
            d = mk(f"d{i}.jpg")
            delims.append(d)
            ph = mk(f"backed_{i}.jpg")
            segs.append(
                Segment(folder_key="ADATTABLA", plate_image=ph, ocr_raw="ADATTABLA",
                        photos=[ph], closed_by_delimiter=d)
            )
            total_files += 1
        # 2 határolós, EGYEDI névvel
        for i in range(2):
            d = mk(f"du_{i}.jpg")
            delims.append(d)
            ph = mk(f"uniq_{i}.jpg")
            segs.append(
                Segment(folder_key=f"UNIQ_{i}", plate_image=ph, ocr_raw=f"UNIQ_{i}",
                        photos=[ph], closed_by_delimiter=d)
            )
            total_files += 1
        # 3 határoló NÉLKÜLI, át nem nevezett, azonos OCR-névvel → ütköznek
        for i in range(3):
            ph = mk(f"less_{i}.jpg")
            segs.append(
                Segment(folder_key="ADATTABLA", plate_image=ph, ocr_raw="ADATTABLA",
                        photos=[ph], closed_by_delimiter=None)
            )
            total_files += 1
        # 2 határoló NÉLKÜLI, AZONOS kézi névre átnevezve → ütköznek
        for i in range(2):
            ph = mk(f"ren_{i}.jpg")
            segs.append(
                Segment(folder_key="KEZI", plate_image=ph, ocr_raw="KEZI",
                        photos=[ph], closed_by_delimiter=None)
            )
            total_files += 1

        plan = OrganizePlan(segments=segs, delimiter_hits=delims)
        n_seg = len(plan.segments)
        assert n_seg == 10
        # Megkülönböztető jóváhagyott nevek: ADATTABLA, UNIQ_0, UNIQ_1, KEZI → 4 db.
        distinct_names = {s.folder_key for s in plan.segments}
        assert distinct_names == {"ADATTABLA", "UNIQ_0", "UNIQ_1", "KEZI"}

        log = execute_plan(plan, out, copy_mode=True)

        created = sorted([d.name for d in out.iterdir() if d.is_dir()])
        # ÚJ: a mappák száma a MEGKÜLÖNBÖZTETŐ nevek száma (összevonás), NEM a szegmensszám.
        assert len(created) == len(distinct_names), f"várt {len(distinct_names)} mappa, kapott: {created}"
        assert set(created) == distinct_names
        # Nincs utótagolt ütköző mappa.
        assert "ADATTABLA_2" not in created and "KEZI_2" not in created
        # Minden fotó átkerült (egy fájl sem veszett el; az azonos nevűek összevonva).
        moved_photos = sum(1 for _kind, _src, dst in log if "fotók" in str(dst))
        assert moved_photos == total_files
        # A közös mappákban tényleg ott van minden fájl (ADATTABLA: 3+3=6, KEZI: 2).
        n_adattabla = len(list((out / "ADATTABLA" / "fotók").iterdir()))
        n_kezi = len(list((out / "KEZI" / "fotók").iterdir()))
        assert n_adattabla == 6, n_adattabla
        assert n_kezi == 2, n_kezi
        assert len(list((out / "UNIQ_0" / "fotók").iterdir())) == 1
        # Lemezen összesen annyi fotó, amennyi szegmens-fotó volt (semmi nem veszett el).
        total_on_disk = sum(len(list((out / nm / "fotók").iterdir())) for nm in distinct_names)
        assert total_on_disk == total_files


def test_execute_plan_index_defaults_distinct_and_shared_name_merges() -> None:
    """
    (a) Sorszám-alapértelmezés, szerkesztés nélkül: N szegmens → N külön mappa (1..N).
    (b) Ha a felhasználó 3 szegmenst AZONOS névre ("KÖZÖS") állít, a 3 → EGY "KÖZÖS" mappa,
        benne mindhárom szegmens összes képével; az azonos fájlnevek a mappán belül
        de-duplikálódnak (semmi nem íródik felül / vész el).
    """
    from metal_batch_logic import OrganizePlan, Segment, execute_plan

    # (a) index-alapértelmezés → N külön mappa
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "src"
        out = root / "out"
        src.mkdir(parents=True, exist_ok=True)
        segs = []
        for i in range(5):
            ph = src / f"p{i}.jpg"
            ph.write_bytes(b"x")
            # az 5. lépés jóváhagyott neve = a sorszám
            segs.append(Segment(folder_key=str(i + 1), plate_image=ph, ocr_raw=str(i + 1),
                                photos=[ph], closed_by_delimiter=None))
        execute_plan(OrganizePlan(segments=segs), out, copy_mode=True)
        created = sorted(d.name for d in out.iterdir() if d.is_dir())
        assert created == ["1", "2", "3", "4", "5"], created

    # (b) három szegmens ugyanarra a kézi névre → egy közös mappa, fájlnév-ütközés de-dup
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        out = root / "out"
        segs = []
        total = 0
        for i in range(3):
            sub = root / "src" / f"blokk{i}"
            sub.mkdir(parents=True, exist_ok=True)
            # MINDEGYIK szegmensben azonos fájlnév ("kep.jpg") → a közös mappában de-dup kell
            ph = sub / "kep.jpg"
            ph.write_bytes(b"x")
            extra = sub / f"extra{i}.jpg"
            extra.write_bytes(b"y")
            segs.append(Segment(folder_key="KÖZÖS", plate_image=ph, ocr_raw="KÖZÖS",
                                photos=[ph, extra], closed_by_delimiter=None))
            total += 2
        log = execute_plan(OrganizePlan(segments=segs), out, copy_mode=True)
        created = sorted(d.name for d in out.iterdir() if d.is_dir())
        assert created == ["KÖZÖS"], created  # EGY közös mappa
        files = sorted(p.name for p in (out / "KÖZÖS" / "fotók").iterdir())
        assert len(files) == total, files  # mind a 6 fájl megvan (egy sem veszett el)
        # az azonos "kep.jpg" nevek de-duplikálva (kep.jpg, kep_1.jpg, kep_2.jpg)
        assert sum(1 for f in files if f.startswith("kep")) == 3
        assert len(set(files)) == len(files)  # nincs duplikált fájlnév


def test_delimiterless_segments_get_xx_marker() -> None:
    """
    A határoló nélküli (nincs határolókép) szegmensek jóváhagyott neve ``-xx``-re végződik —
    sorszám-alapértelmezésre (``2-xx``) és kézi névre (``ALMA-xx``) is. A határolóval lezártak
    neve jelölő nélküli. A jelölő idempotens.
    """
    from metal_batch_logic import OrganizePlan, Segment
    from organizer_metal_app import build_approved_folder_names, mark_delimiterless_name

    root = Path("/tmp/photo_sorter_xx_marker")
    d = root / "d.jpg"
    seg_backed = Segment(folder_key="", plate_image=root / "a.jpg", ocr_raw="x",
                         photos=[root / "a.jpg"], closed_by_delimiter=d)        # idx0 → "1" (backed)
    seg_less_default = Segment(folder_key="", plate_image=root / "b.jpg", ocr_raw="x",
                               photos=[root / "b.jpg"], closed_by_delimiter=None)  # idx1 → "2-xx"
    seg_less_manual = Segment(folder_key="ALMA", plate_image=root / "c.jpg", ocr_raw="x",
                              photos=[root / "c.jpg"], closed_by_delimiter=None)   # "ALMA-xx"
    plan = OrganizePlan(segments=[seg_backed, seg_less_default, seg_less_manual], delimiter_hits=[d])

    assert build_approved_folder_names(plan) == ["1", "2-xx", "ALMA-xx"]
    # idempotens / szabály-egységek
    assert mark_delimiterless_name("ALMA-xx", has_delimiter=False) == "ALMA-xx"
    assert mark_delimiterless_name("ALMA", has_delimiter=False) == "ALMA-xx"
    assert mark_delimiterless_name("X", has_delimiter=True) == "X"
    assert mark_delimiterless_name("", has_delimiter=False) == ""


def test_execution_includes_all_step3_segments_with_xx_and_merge() -> None:
    """
    Teljesség + ``-xx`` + összevonás egy körben: minden 3. lépésben látható szegmens (határolós
    ÉS határoló nélküli) bekerül a végrehajtásba; a határoló nélküliek ``-xx`` jelölést kapnak;
    két azonos nevű határoló nélküli (``ALMA-xx``) EGY mappába olvad. A jelölő újrafuttatva sem
    duplázódik.
    """
    from metal_batch_logic import OrganizePlan, Segment, execute_plan
    from organizer_metal_app import build_approved_folder_names

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "src"
        out = root / "out"
        src.mkdir(parents=True, exist_ok=True)

        def mk(n: str) -> Path:
            p = src / n
            p.write_bytes(b"x")
            return p

        d0, d1 = mk("d0.jpg"), mk("d1.jpg")
        backed0 = Segment(folder_key="", plate_image=mk("p0.jpg"), ocr_raw="x",
                          photos=[mk("ph0.jpg")], closed_by_delimiter=d0)
        backed1 = Segment(folder_key="", plate_image=mk("p1.jpg"), ocr_raw="x",
                          photos=[mk("ph1.jpg")], closed_by_delimiter=d1)
        less_default = Segment(folder_key="", plate_image=mk("p2.jpg"), ocr_raw="x",
                               photos=[mk("ph2.jpg")], closed_by_delimiter=None)
        less_alma_a = Segment(folder_key="ALMA", plate_image=mk("p3.jpg"), ocr_raw="x",
                              photos=[mk("ph3.jpg")], closed_by_delimiter=None)
        less_alma_b = Segment(folder_key="ALMA", plate_image=mk("p4.jpg"), ocr_raw="x",
                              photos=[mk("ph4.jpg")], closed_by_delimiter=None)
        plan = OrganizePlan(
            segments=[backed0, backed1, less_default, less_alma_a, less_alma_b],
            delimiter_hits=[d0, d1],
        )

        approved = build_approved_folder_names(plan)
        assert approved == ["1", "2", "3-xx", "ALMA-xx", "ALMA-xx"], approved
        for seg, nm in zip(plan.segments, approved):
            seg.folder_key = nm

        # Idempotens + rebuild túléli: a már jelölt nevekből nem lesz "-xx-xx".
        approved2 = build_approved_folder_names(plan)
        assert approved2 == approved
        assert not any(nm.endswith("-xx-xx") for nm in approved2)

        execute_plan(plan, out, copy_mode=True)
        created = sorted(d.name for d in out.iterdir() if d.is_dir())
        # Megkülönböztető nevek: a két ALMA-xx EGY mappa → 4 mappa, MINDEN szegmens benne.
        assert created == ["1", "2", "3-xx", "ALMA-xx"], created
        # A két határoló nélküli ALMA összevonva (2 fotó egy közös mappában).
        assert len(list((out / "ALMA-xx" / "fotók").iterdir())) == 2
        # A határoló nélküli alapértelmezett külön mappa, jelölővel.
        assert (out / "3-xx" / "fotók").is_dir()
        # A határolós mappák jelölő nélkül.
        assert (out / "1").is_dir() and (out / "2").is_dir()
        assert not (out / "1-xx").exists() and not (out / "2-xx").exists()


def test_untouched_segments_each_get_a_folder_real_flow() -> None:
    """
    VALÓS folyamat (a ``_segment_media_to_plan`` szegmentál): a felhasználó CSAK néhány mappát
    nevez át; minden ÉRINTETLEN (sorszám-alapértelmezett) szegmensnek IS külön mappát kell kapnia.

    Bizonyítja a gyökérokot is: ha az érintetlen szegmensek ``folder_key``-je (felfelé valamiért)
    AZONOS / romlott, a ``folder_key``-alapú névadás összevonná őket (csak a módosított marad meg) —
    a **stabil tár alapú** ``build_approved_folder_names(..., tag_by_segment=...)`` viszont garantáltan
    EGYEDI sorszámot ad az érintetleneknek, így minden szegmens külön mappa lesz.
    """
    import organizer_metal_app as app_mod
    from organizer_metal_app import (
        build_approved_folder_names,
        select_execution_segments,
        _apply_ocr_edits_to_plan,
        apply_step3_tag_edits_to_plan,
        _apply_photo_exclusions_to_plan,
        _segment_identity_keys,
        _STEP3_TAGS_BY_SEGMENT_KEY,
        _STEP3_TAGS_BY_DELIM_KEY,
        _STEP3_TAGS_BY_PLATE_KEY,
    )
    from metal_batch_logic import _segment_media_to_plan, execute_plan, norm_path_key
    import copy as _copy

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "src"
        src.mkdir()
        out = root / "out"
        # layout: pA D1 pB D2 pC D3 pD → seg0,1,2 határolós; seg3 határoló nélküli
        names_in = ["00_pA.jpg", "01_D1.jpg", "02_pB.jpg", "03_D2.jpg",
                    "04_pC.jpg", "05_D3.jpg", "06_pD.jpg"]
        files = []
        for n in names_in:
            p = src / n
            p.write_bytes(b"img")
            files.append(p)
        delim_set = {norm_path_key(files[1]), norm_path_key(files[3]), norm_path_key(files[5])}
        plan = _segment_media_to_plan(
            files, hash_by_path={}, ref_phash=None, ref_ahash=None, max_hamming=18,
            skip_del=set(), force_del=set(), ocr=lambda p: None, ocr_by_path={},
            use_ocr_cache=False, delimiter_candidates=delim_set, total_images=None, progress=None,
        )
        assert len(plan.segments) == 4

        # A felhasználó CSAK a 2. szegmenst nevezi át (stabil tár = kizárólag kézi nevek).
        store = {}
        for sid in _segment_identity_keys(plan.segments[1]):
            store[sid] = "ALMA"

        app_mod.st.session_state = {  # type: ignore[assignment]
            "_plan": plan, "_src": str(src),
            _STEP3_TAGS_BY_SEGMENT_KEY: store,
            _STEP3_TAGS_BY_DELIM_KEY: {}, _STEP3_TAGS_BY_PLATE_KEY: {},
        }

        # --- Gyökérok-bizonyíték: romlott/azonos folder_key az érintetlen szegmenseken ---
        corrupt = _copy.deepcopy(plan)
        for s in corrupt.segments:
            s.folder_key = "azonosítatlan"
        corrupt.segments[1].folder_key = "ALMA"
        # folder_key-alapú (régi) feloldás → az érintetlenek összeolvadnak (a hibajelenség):
        legacy = build_approved_folder_names(corrupt)
        assert legacy.count("azonosítatlan") + legacy.count("azonosítatlan-xx") >= 3
        assert len(set(legacy)) < 4  # összeomlik (csak a módosított marad külön)
        # tár-alapú (új) feloldás → minden érintetlen EGYEDI sorszámot kap:
        fixed = build_approved_folder_names(corrupt, tag_by_segment=store)
        assert fixed == ["1", "ALMA", "3", "4-xx"], fixed
        assert len(set(fixed)) == 4

        # --- Teljes, valós step5 lánc → minden szegmens külön mappa a lemezen ---
        p = _apply_ocr_edits_to_plan(plan)
        p = apply_step3_tag_edits_to_plan(
            p, tag_by_dix={}, tag_by_seg={}, preview_rows=[], files_ord=None,
            tag_by_delim={}, tag_by_plate={}, tag_by_segment=store,
        )
        p = _apply_photo_exclusions_to_plan(p)
        p.segments = select_execution_segments(
            p, tag_by_segment=store, original_ocr_by_plate=None, drop_unedited_delimiterless=False,
        )
        approved = build_approved_folder_names(p, tag_by_segment=store)
        assert approved == ["1", "ALMA", "3", "4-xx"], approved
        for seg, nm in zip(p.segments, approved):
            seg.folder_key = nm
        execute_plan(p, out, copy_mode=True)
        created = sorted(d.name for d in out.iterdir() if d.is_dir())
        assert created == ["1", "3", "4-xx", "ALMA"], created  # mind a 4 mappa létrejön


def test_step5_preview_uses_current_step3_name_over_stale_snapshot() -> None:
    """
    Gyökérok-regresszió: a felhasználó a 3. lépésben átírja a mappanevet, de a stabil mentésben
    (``_STEP3_TAGS_BY_SEGMENT_KEY``) még egy KORÁBBI érték szerepel. Az 5. lépés előnézete
    (``_resolve_execution_plan_for_preview`` → ``build_approved_folder_names``) a MOST beírt
    nevet használja, nem a korábbit. Korábban a preview persist=False mellett a stale snapshotot
    (vagy a sorszám-alapértelmezést) mutatta — pont a bejelentett „régi érték ragad be” hiba.
    """
    import imagehash
    from PIL import Image
    import organizer_metal_app as app_mod
    from organizer_metal_app import (
        _resolve_execution_plan_for_preview,
        build_approved_folder_names,
        _apply_ocr_edits_to_plan,
        _segment_identity_keys,
        _STEP3_TAGS_BY_SEGMENT_KEY,
        _STEP3_TAGS_BY_DELIM_KEY,
        _STEP3_TAGS_BY_PLATE_KEY,
    )
    from metal_batch_logic import OrganizePlan, Segment, PlanScanCache

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        def mk(n: str) -> Path:
            p = root / n
            Image.new("RGB", (8, 8), (120, 200, 50)).save(p)
            return p

        # layout: d1 pA d2 pB  -> segA határolóval lezárt; segB záró, határoló nélküli
        d1, pA, d2, pB = mk("01_d1.jpg"), mk("02_pA.jpg"), mk("03_d2.jpg"), mk("04_pB.jpg")
        files = [d1, pA, d2, pB]
        ref = imagehash.hex_to_hash("0000000000000000")

        plan = OrganizePlan(
            segments=[
                Segment(folder_key="OCR_A", plate_image=pA, ocr_raw="OCR_A", photos=[pA], closed_by_delimiter=d2),
                Segment(folder_key="OCR_B", plate_image=pB, ocr_raw="OCR_B", photos=[pB], closed_by_delimiter=None),
            ],
            delimiter_hits=[d1, d2],
        )
        cache = PlanScanCache(
            files=list(files), hash_by_path={}, ref_phash=ref, ref_ahash=ref,
            max_hamming=18, inner_ratio=0.92, source_str=str(root.expanduser()),
            recursive=False, image_count=4, files_sorted_by_name_mtime=True,
            ocr_by_path={}, delimiter_candidates={str(d1.expanduser()), str(d2.expanduser())},
        )

        # STALE mentés: segB-hez egy KORÁBBI „KV49752” név (mint egy előző körből).
        stale = {}
        for sid in _segment_identity_keys(plan.segments[1]):
            stale[sid] = "KV49752"

        app_mod.st.session_state = {  # type: ignore[assignment]
            "_plan": plan, "_src": str(root),
            "metal_recursive_chk": False, "_recursive": False,
            "metal_max_hamming": 18, "metal_del_inner": 0.92,
            "_plan_scan_cache": cache,
            "_forced_delimiter_paths": [], "_demoted_delimiter_paths": [],
            "_plan_generation": 1,
            _STEP3_TAGS_BY_SEGMENT_KEY: dict(stale),
            _STEP3_TAGS_BY_DELIM_KEY: {}, _STEP3_TAGS_BY_PLATE_KEY: {},
            # A felhasználó MOST a 3. lépés mezőkbe ÚJ neveket ír (a stale-től eltérőt).
            "step3_tag_ocr_0": "ALMA",
            "step3_tag_ocr_1": "KV99999",
        }

        # 5. lépés előnézet: a MOST beírt nevet kell mutatnia (NEM a stale „KV49752”-t).
        resolved = _resolve_execution_plan_for_preview()
        assert resolved is not None
        prepared, live_by_segment = resolved
        names = build_approved_folder_names(prepared, tag_by_segment=live_by_segment)
        assert names == ["ALMA", "KV99999-xx"], names
        assert "KV49752" not in " ".join(names)

        # 5. lépés végrehajtás-előkészítés (persist=True): szintén a friss nevet használja,
        # és a snapshotot felülírja az ÚJ értékre (a régi nem éled fel).
        executed = _apply_ocr_edits_to_plan(app_mod.st.session_state["_plan"])
        exec_names = build_approved_folder_names(
            executed, tag_by_segment=app_mod.st.session_state.get(_STEP3_TAGS_BY_SEGMENT_KEY) or {}
        )
        assert exec_names == ["ALMA", "KV99999-xx"], exec_names
        saved_values = set((app_mod.st.session_state.get(_STEP3_TAGS_BY_SEGMENT_KEY) or {}).values())
        assert "KV49752" not in saved_values
        assert "KV99999" in saved_values


def test_plan_required_notice_shows_success_after_sort() -> None:
    """A sikeres válogatás után a lap-őr ne a félrevezető „készíts tervet” üzenetet adja."""
    import organizer_metal_app as app_mod
    from organizer_metal_app import _plan_required_notice, _SORT_COMPLETED_KEY

    # Nincs befejezett válogatás → felszólítás.
    app_mod.st.session_state = {}  # type: ignore[assignment]
    kind, msg = _plan_required_notice()
    assert kind == "info"
    assert "Kiindulás" in msg

    # Befejezett válogatás összegzése → siker üzenet, NEM „készíts tervet”.
    app_mod.st.session_state = {  # type: ignore[assignment]
        _SORT_COMPLETED_KEY: {"ops": 7, "out": "/tmp/out", "copy": False}
    }
    kind2, msg2 = _plan_required_notice()
    assert kind2 == "success"
    assert "7" in msg2
    assert "/tmp/out" in msg2
    assert "készíts kiindulási tervet" in msg2  # csak útmutató, nem hibaként


def test_select_execution_segments_robust_to_missing_cache() -> None:
    """A végrehajtás-előkészítés sosem akadhat meg, ha nincs (vagy üres) terv-cache."""
    import organizer_metal_app as app_mod
    from organizer_metal_app import (
        select_execution_segments,
        _original_ocr_by_plate_from_cache,
    )
    from metal_batch_logic import OrganizePlan, Segment

    root = Path("/tmp/photo_sorter_exec_robust")
    delim = root / "d.jpg"
    seg_backed = Segment(
        folder_key="BACKED", plate_image=root / "a.jpg", ocr_raw="BACKED",
        photos=[root / "a.jpg"], closed_by_delimiter=delim,
    )
    seg_less = Segment(
        folder_key="LESS", plate_image=root / "b.jpg", ocr_raw="LESS",
        photos=[root / "b.jpg"], closed_by_delimiter=None,
    )
    plan = OrganizePlan(segments=[seg_backed, seg_less], delimiter_hits=[delim])

    # Nincs cache → üres térkép, nincs kivétel.
    app_mod.st.session_state = {}  # type: ignore[assignment]
    assert _original_ocr_by_plate_from_cache() == {}

    # Alap: mindkettő bennmarad, akkor is, ha nincs eredeti OCR térkép.
    kept = select_execution_segments(plan, tag_by_segment={}, original_ocr_by_plate=None)
    assert {s.folder_key for s in kept} == {"BACKED", "LESS"}

    # Szigorú mód + hiányzó eredeti OCR: a határolós SOSEM esik ki; az át nem nevezett
    # határoló nélküli kiesik (eredeti==jelenlegi, nincs felülírás).
    strict = select_execution_segments(
        plan, tag_by_segment={}, original_ocr_by_plate={}, drop_unedited_delimiterless=True
    )
    assert "BACKED" in {s.folder_key for s in strict}


def test_step3_interval_between_delimiters() -> None:
    from organizer_metal_app import _step3_images_between_delimiter_row_and_next

    root = Path("/tmp/photo_sorter_test_interval")
    files = [
        root / "20240515_091800_delim.jpg",
        root / "20240515_091839.jpg",
        root / "20240515_091911.jpg",
        root / "20240515_114605_delim.jpg",
        root / "20240515_120000.jpg",
    ]
    preview = [
        (files[0], [files[1]]),
        (files[3], [files[4]]),
    ]
    between = _step3_images_between_delimiter_row_and_next(0, preview, files)
    assert [p.name for p in between] == ["20240515_091839.jpg", "20240515_091911.jpg"]
    between2 = _step3_images_between_delimiter_row_and_next(1, preview, files)
    assert [p.name for p in between2] == ["20240515_120000.jpg"]
    # következő határoló nincs a sávban
    assert all(p.suffix.lower() in IMAGE_SUFFIXES for p in between)


def _platefirst_plan_and_preview():
    """Segéd: fémlap-ELSŐ, vezető határoló nélküli elrendezés (pA D1 pB D2 pC) — seg0 árva."""
    from metal_batch_logic import OrganizePlan, Segment
    root = Path("/tmp/photo_sorter_idx")
    pA, pB, pC = root / "00_pA.jpg", root / "02_pB.jpg", root / "04_pC.jpg"
    D1, D2 = root / "01_d1.jpg", root / "03_d2.jpg"
    files = [pA, D1, pB, D2, pC]

    def make():
        return [
            Segment(folder_key="J", plate_image=pA, ocr_raw="OCR_JUNK_A\nmasodik", photos=[pA], closed_by_delimiter=D1),
            Segment(folder_key="J", plate_image=pB, ocr_raw="OCR_JUNK_B", photos=[pB], closed_by_delimiter=D2),
            Segment(folder_key="J", plate_image=pC, ocr_raw="OCR_JUNK_C", photos=[pC], closed_by_delimiter=None),
        ]

    preview = [(D1, [pB]), (D2, [pC])]
    return OrganizePlan, make, preview, files


def test_default_folder_names_are_sequential_indices() -> None:
    """Szerkesztés nélkül minden mappa alapértelmezett neve a sorszám: '1','2','3' (NEM OCR)."""
    from organizer_metal_app import (
        apply_step3_tag_edits_to_plan,
        build_approved_folder_names,
        default_folder_name_for_segment,
        _preview_row_segment_indices,
    )

    OrganizePlan, make, preview, files = _platefirst_plan_and_preview()
    plan = OrganizePlan(segments=make(), delimiter_hits=[preview[0][0], preview[1][0]])

    # A sorok az UTÁNUK induló (megjelenített) szegmenst párosítják; seg0 árva (vezető határoló nélkül).
    assert _preview_row_segment_indices(plan, preview, files) == [1, 2]

    applied = apply_step3_tag_edits_to_plan(plan, tag_by_dix={}, tag_by_seg={}, preview_rows=preview, files_ord=files)
    assert [s.folder_key for s in applied.segments] == ["1", "2", "3"]
    # OCR sosem szivárog be a névbe (a seg0 OCR-je többsoros volt):
    assert all("\n" not in s.folder_key for s in applied.segments)
    # Jóváhagyott-név lista: a határoló nélküli 3. szegmens '-xx' jelölőt kap, a határolós kettő nem.
    assert build_approved_folder_names(applied) == ["1", "2", "3-xx"]
    assert default_folder_name_for_segment(0) == "1" and default_folder_name_for_segment(9) == "10"


def test_default_and_manual_mix_survive_snapshot_rebuild() -> None:
    """
    Néhány mappát átnevez a felhasználó, másokat alapértelmezetten hagy:
    a szerkesztett → kézi név, az érintetlen → a sorszáma; mindez túléli a snapshot → újraszámolást.
    """
    from organizer_metal_app import (
        apply_step3_tag_edits_to_plan,
        snapshot_step3_tag_overrides_from_plan,
        build_approved_folder_names,
    )
    from metal_batch_logic import safe_folder_name

    OrganizePlan, make, preview, files = _platefirst_plan_and_preview()
    plan = OrganizePlan(segments=make(), delimiter_hits=[preview[0][0], preview[1][0]])

    # seg0 (árva, vezető) átnevezve a saját mezőjében; seg2 a hozzá tartozó határoló-sorban;
    # seg1 ÉRINTETLEN (marad a sorszáma).
    # seg_ix == [1,2]: dix0->seg1, dix1->seg2. Tehát seg2-t a dix=1 sorban nevezzük.
    tag_by_seg = {0: "EGYEDI_ELSO"}
    tag_by_dix = {1: "EGYEDI_HARMADIK"}

    applied = apply_step3_tag_edits_to_plan(
        plan, tag_by_dix=tag_by_dix, tag_by_seg=tag_by_seg, preview_rows=preview, files_ord=files
    )
    assert applied.segments[0].folder_key == safe_folder_name("EGYEDI_ELSO")
    assert applied.segments[1].folder_key == "2"  # érintetlen → sorszám
    assert applied.segments[2].folder_key == safe_folder_name("EGYEDI_HARMADIK")
    # seg2 határoló nélküli → '-xx' jelölő a jóváhagyott névben; seg0/seg1 határolós → nincs.
    assert build_approved_folder_names(applied) == [
        safe_folder_name("EGYEDI_ELSO"), "2", safe_folder_name("EGYEDI_HARMADIK") + "-xx"
    ]

    # snapshot CSAK a kézi neveket menti; az érintetlen marad a (friss) sorszám.
    by_d, by_p, by_s = snapshot_step3_tag_overrides_from_plan(applied, preview, files)
    # a "2" (sorszám) nem kerül a mentésbe:
    assert "2" not in set(by_s.values())
    assert set(by_s.values()) == {"EGYEDI_ELSO", "EGYEDI_HARMADIK"}

    rebuilt = OrganizePlan(segments=make(), delimiter_hits=[preview[0][0], preview[1][0]])
    rebuilt = apply_step3_tag_edits_to_plan(
        rebuilt, tag_by_dix={}, tag_by_seg={}, preview_rows=preview, files_ord=files,
        tag_by_delim=by_d, tag_by_plate=by_p, tag_by_segment=by_s,
    )
    assert rebuilt.segments[0].folder_key == safe_folder_name("EGYEDI_ELSO")
    assert rebuilt.segments[1].folder_key == "2"  # továbbra is a sorszám
    assert rebuilt.segments[2].folder_key == safe_folder_name("EGYEDI_HARMADIK")


def test_safe_folder_name_bounds_noisy_multiline_ocr() -> None:
    """Garázs-őr: a többsoros, zajos OCR sosem lehet a mappanév — csak az első sor, korlátos hosszon."""
    noisy = "2.we-a2a_aAityfaPil4eatsjle\nmasodik szemet sor\nharmadik\n\nnegyedik"
    name = safe_folder_name(noisy)
    assert "\n" not in name and "\r" not in name
    assert name == "2.we-a2a_aAityfaPil4eatsjle"  # csak az első nem üres sor
    # hosszúság-korlát
    long_line = "x" * 200
    assert len(safe_folder_name(long_line)) <= 80
    # csupa whitespace / üres → stabil tartalék
    assert safe_folder_name("\n\n   \n") == "azonosítatlan"
    assert safe_folder_name("") == "azonosítatlan"


def test_segment_index_maps_displayed_follower_not_closing() -> None:
    """
    A határoló-sor a határoló UTÁNI (itt induló) szegmenst párosítja, sosem a lezártat.
    Üres követő-sávra (záró/egymás utáni határoló) ``None`` — nincs eltolás / kettős párosítás.
    """
    from organizer_metal_app import _segment_index_for_tag_block
    from metal_batch_logic import OrganizePlan, Segment

    root = Path("/tmp/photo_sorter_map_follower")
    D1, D2 = root / "d1.jpg", root / "d2.jpg"
    pA, pB = root / "pA.jpg", root / "pB.jpg"
    segs = [
        Segment(folder_key="A", plate_image=pA, ocr_raw="A", photos=[pA], closed_by_delimiter=D1),
        Segment(folder_key="B", plate_image=pB, ocr_raw="B", photos=[pB], closed_by_delimiter=D2),
    ]
    plan = OrganizePlan(segments=segs, delimiter_hits=[D1, D2])
    files = [pA, D1, pB, D2]
    # D1 sora a UTÁNA következő pB (segB) szegmenst mutatja — nem a lezárt segA-t.
    assert _segment_index_for_tag_block(plan, D1, [pB], next_delimiter=D2, files_ordered=files) == 1
    # D2 sora: utána nincs kép (záró határoló) → None (nem esik vissza a lezárt segB-re).
    assert _segment_index_for_tag_block(plan, D2, [], next_delimiter=None, files_ordered=files) is None


def test_step3_names_survive_plate_first_no_leading_delimiter() -> None:
    """
    Valós elrendezés: fémlap-ELSŐ, NINCS vezető határoló, van záró (határoló nélküli) szegmens
    — ``pA D1 pB D2 pC D3 pD``. Az első mappa (pA) párosítatlan: a felhasználó külön mezőben
    (``seg_ocr_raw``) nevezi el; a többi a határoló-sorban a MEGJELENÍTETT követő szerint.
    Egyik szegmens DEFAULT marad → a neve korlátos (nem nyers több­soros OCR).
    Minden kézi név túléli a snapshot → újraszámolást, eltolódás nélkül.
    """
    from organizer_metal_app import (
        apply_step3_tag_edits_to_plan,
        snapshot_step3_tag_overrides_from_plan,
        _preview_row_segment_indices,
    )
    from metal_batch_logic import OrganizePlan, Segment, safe_folder_name

    root = Path("/tmp/photo_sorter_platefirst")
    pA, pB, pC, pD = (root / "00_pA.jpg", root / "02_pB.jpg", root / "04_pC.jpg", root / "06_pD.jpg")
    D1, D2, D3 = (root / "01_d1.jpg", root / "03_d2.jpg", root / "05_d3.jpg")
    files = [pA, D1, pB, D2, pC, D3, pD]
    noisy0 = "junk0_line1\njunk0_line2"  # ha default maradna, csak az első sor lenne a név

    def make_segs():
        return [
            Segment(folder_key=safe_folder_name(noisy0), plate_image=pA, ocr_raw=noisy0, photos=[pA], closed_by_delimiter=D1),
            Segment(folder_key="OCRB", plate_image=pB, ocr_raw="OCRB", photos=[pB], closed_by_delimiter=D2),
            Segment(folder_key="OCRC", plate_image=pC, ocr_raw="OCRC", photos=[pC], closed_by_delimiter=D3),
            Segment(folder_key="OCRD_l1\nOCRD_l2", plate_image=pD, ocr_raw="OCRD_l1\nOCRD_l2", photos=[pD], closed_by_delimiter=None),
        ]

    preview = [(D1, [pB]), (D2, [pC]), (D3, [pD])]
    plan = OrganizePlan(segments=make_segs(), delimiter_hits=[D1, D2, D3])

    seg_ix = _preview_row_segment_indices(plan, preview, files)
    assert seg_ix == [1, 2, 3], seg_ix  # az ELSŐ szegmens párosítatlan (nincs vezető határoló)
    matched = {x for x in seg_ix if x is not None}
    orphan = [i for i in range(4) if i not in matched]
    assert orphan == [0], orphan

    # seg3-at DEFAULT-on hagyjuk (garázs-őr ellenőrzés). A többit kézzel nevezzük.
    intended = {0: "ELSO_KEZI", 1: "MASODIK", 2: "HARMADIK"}
    tag_by_seg = {0: intended[0]}  # az árva (vezető) szegmens külön mezőben
    tag_by_dix = {dix: intended[seg_ix[dix]] for dix in range(len(preview)) if seg_ix[dix] in intended}

    applied = apply_step3_tag_edits_to_plan(
        plan, tag_by_dix=tag_by_dix, tag_by_seg=tag_by_seg, preview_rows=preview, files_ord=files
    )
    assert applied.segments[0].folder_key == safe_folder_name("ELSO_KEZI")
    assert applied.segments[1].folder_key == safe_folder_name("MASODIK")
    assert applied.segments[2].folder_key == safe_folder_name("HARMADIK")
    # Az át nem nevezett (default) szegmens neve a SORSZÁM (4.), nem OCR — soha nem nyers OCR:
    assert "\n" not in applied.segments[3].folder_key
    assert applied.segments[3].folder_key == "4"

    # snapshot → újraszámolás: a kézi nevek maradjanak, eltolódás nélkül.
    by_d, by_p, by_s = snapshot_step3_tag_overrides_from_plan(applied, preview, files)
    rebuilt = OrganizePlan(segments=make_segs(), delimiter_hits=[D1, D2, D3])
    rebuilt = apply_step3_tag_edits_to_plan(
        rebuilt, tag_by_dix={}, tag_by_seg={}, preview_rows=preview, files_ord=files,
        tag_by_delim=by_d, tag_by_plate=by_p, tag_by_segment=by_s,
    )
    assert rebuilt.segments[0].folder_key == safe_folder_name("ELSO_KEZI")
    assert rebuilt.segments[1].folder_key == safe_folder_name("MASODIK")
    assert rebuilt.segments[2].folder_key == safe_folder_name("HARMADIK")


def test_step3_first_and_mid_segment_names_survive_rebuild() -> None:
    """
    Regresszió: a határoló-sor a határoló UTÁNI (itt induló) szegmenst nevezi el, nem az
    általa lezártat. Korábban emiatt az ELSŐ (kettős párosítású) és az UTOLSÓ/„4.”
    (követőként megjelenő, határoló nélküli) mappa elveszítette a kézi nevet.

    Itt egy határoló-ELSŐ elrendezést építünk (D, plate, D, plate, …), minden szegmensre
    kézi nevet adunk, majd snapshot → újraszámolás után ellenőrizzük, hogy MINDEN szegmens
    (kiemelten az 1. és a 4.) megtartotta a felhasználói nevet.
    """
    from organizer_metal_app import (
        apply_step3_tag_edits_to_plan,
        snapshot_step3_tag_overrides_from_plan,
        _preview_row_segment_indices,
    )
    from metal_batch_logic import OrganizePlan, Segment, safe_folder_name

    root = Path("/tmp/photo_sorter_first_mid_names")
    D1, D2, D3, D4 = (root / "01_D1.jpg", root / "03_D2.jpg", root / "05_D3.jpg", root / "07_D4.jpg")
    pA, pB, pC, pD = (root / "02_pA.jpg", root / "04_pB.jpg", root / "06_pC.jpg", root / "08_pD.jpg")
    files = [D1, pA, D2, pB, D3, pC, D4, pD]

    def make_segs():
        return [
            Segment(folder_key="OCR_A", plate_image=pA, ocr_raw="OCR_A", photos=[pA], closed_by_delimiter=D2),
            Segment(folder_key="OCR_B", plate_image=pB, ocr_raw="OCR_B", photos=[pB], closed_by_delimiter=D3),
            Segment(folder_key="OCR_C", plate_image=pC, ocr_raw="OCR_C", photos=[pC], closed_by_delimiter=D4),
            Segment(folder_key="OCR_D", plate_image=pD, ocr_raw="OCR_D", photos=[pD], closed_by_delimiter=None),
        ]

    preview = [(D1, [pA]), (D2, [pB]), (D3, [pC]), (D4, [pD])]
    plan = OrganizePlan(segments=make_segs(), delimiter_hits=[D1, D2, D3, D4])

    # Minden határoló-sor a MEGJELENÍTETT (követő) szegmensre mutasson — 1:1, nincs kettős párosítás.
    seg_ix = _preview_row_segment_indices(plan, preview, files)
    assert seg_ix == [0, 1, 2, 3], seg_ix

    intended = {0: "ELSO_KEZI", 1: "MASODIK", 2: "HARMADIK", 3: "NEGYEDIK_KEZI"}
    # A felhasználó a sorban MEGJELENÍTETT fémlaphoz írja a nevet (step3_tag_ocr_{dix}).
    tag_by_dix = {dix: intended[si] for dix, si in enumerate(seg_ix)}

    applied = apply_step3_tag_edits_to_plan(
        plan, tag_by_dix=tag_by_dix, tag_by_seg={}, preview_rows=preview, files_ord=files
    )
    for i in range(4):
        assert applied.segments[i].folder_key == safe_folder_name(intended[i]), (
            i, applied.segments[i].folder_key
        )

    # Snapshot → újraszámolás (friss OCR-nevek, üres widgetek) — a kézi nevek maradjanak meg.
    by_delim, by_plate, by_segment = snapshot_step3_tag_overrides_from_plan(applied, preview, files)
    rebuilt = OrganizePlan(segments=make_segs(), delimiter_hits=[D1, D2, D3, D4])
    rebuilt = apply_step3_tag_edits_to_plan(
        rebuilt, tag_by_dix={}, tag_by_seg={}, preview_rows=preview, files_ord=files,
        tag_by_delim=by_delim, tag_by_plate=by_plate, tag_by_segment=by_segment,
    )
    # Kiemelten: az 1. (vezető) és a 4. (utolsó, határoló nélküli) is megtartotta a kézi nevet.
    assert rebuilt.segments[0].folder_key == safe_folder_name("ELSO_KEZI")
    assert rebuilt.segments[3].folder_key == safe_folder_name("NEGYEDIK_KEZI")
    for i in range(4):
        assert rebuilt.segments[i].folder_key == safe_folder_name(intended[i]), (
            i, rebuilt.segments[i].folder_key
        )


def test_step3_no_segments_notice_messages() -> None:
    """0 TAG/mappa szegmens esetén a 3. lépés egyértelmű, mód-tudatos útmutatót ad (nem 'forrásmappa')."""
    from organizer_metal_app import step3_no_segments_notice

    # Vannak határoló-sorok, de nincs szegmens → 'error' + helyreállítási lépések.
    kind, msg = step3_no_segments_notice(has_delimiter_rows=True, upload_mode=True)
    assert kind == "error"
    assert "Nincs egyetlen TAG/mappa szegmens sem" in msg
    assert "küszöb" in msg  # pHash/aHash küszöb csökkentése
    assert "2 — Határolók" in msg  # demóciós helyreállítás
    # Feltöltés-módban NEM utalunk helyi forrásmappára mint hibára/teendőre.
    assert "ellenőrizd a forrásmappát" not in msg
    assert "Feltöltés-mód" in msg

    # Helyi mód: ugyanaz a lényeg, de a feltöltés-specifikus zárójeles megjegyzés nélkül.
    kind_l, msg_l = step3_no_segments_notice(has_delimiter_rows=True, upload_mode=False)
    assert kind_l == "error"
    assert "Feltöltés-mód" not in msg_l

    # Nincs határoló-sor sem → 'info', a kiindulási tervre irányít.
    kind2, msg2 = step3_no_segments_notice(has_delimiter_rows=False, upload_mode=True)
    assert kind2 == "info"
    assert "1 — Kiindulás" in msg2


def test_write_uploaded_media_to_dir_preserves_names_and_order() -> None:
    """Feltöltés-mód adapter: a (név, bájtok) párokat temp mappába írja, a pipeline rendezhető."""
    from metal_batch_logic import write_uploaded_media_to_dir, list_sorted_media

    with tempfile.TemporaryDirectory() as td:
        dest = Path(td) / "work"
        items = [
            ("20240515_114605.jpg", b"\xff\xd8jpegA"),
            ("20240515_091839.jpg", b"\xff\xd8jpegB"),
            ("jegyzokonyv.pdf", b"%PDF-1.4 test"),
        ]
        written = write_uploaded_media_to_dir(items, dest)
        assert len(written) == 3
        assert all(p.exists() for p in written)
        # A tartalom megmaradt és a fájlnév szerinti sorrend stabil (időbélyeg a névben).
        assert (dest / "20240515_091839.jpg").read_bytes() == b"\xff\xd8jpegB"
        ordered, err = list_sorted_media(dest, recursive=False)
        assert err is None
        assert [p.name for p in ordered] == [
            "20240515_091839.jpg",
            "20240515_114605.jpg",
            "jegyzokonyv.pdf",
        ]


def test_write_uploaded_media_to_dir_resolves_name_collisions() -> None:
    """Azonos feltöltött fájlnév nem írja felül a korábbit (unique_dest utótag)."""
    from metal_batch_logic import write_uploaded_media_to_dir

    with tempfile.TemporaryDirectory() as td:
        dest = Path(td)
        items = [
            ("kep.jpg", b"first"),
            ("kep.jpg", b"second"),
            ("sub/kep.jpg", b"third"),  # útvonal-rész eldobva → szintén "kep.jpg"
        ]
        written = write_uploaded_media_to_dir(items, dest)
        assert len(written) == 3
        assert len({p.name for p in written}) == 3, [p.name for p in written]
        contents = sorted(p.read_bytes() for p in written)
        assert contents == [b"first", b"second", b"third"]


if __name__ == "__main__":
    test_sort_by_name_then_mtime()
    test_safe_folder_name()
    test_apply_step3_tag_edits_to_plan()
    test_pick_step3_tag_when_later_block_has_default_ocr()
    test_step3_edited_folder_name_survives_step4_rebuild_and_step5_prepare()
    test_step3_override_survives_when_segment_plate_changes_after_rebuild()
    test_step3_override_survives_multiple_rebuild_roundtrips()
    test_delimiter_table_paths_from_fallback()
    test_step2_candidate_list_filters_committed_demotions()
    test_demoted_delimiter_excluded_from_preview_rows()
    test_demoted_paths_from_delimiter_paths()
    test_flush_pending_rerun_only_scope()
    test_filter_path_list_excluding_norm()
    test_step2_keymap_paths_without_rescan()
    test_step2_widget_read_keeps_hidden_committed_demotions()
    test_delete_image_files_on_disk()
    test_prune_organize_plan_removed_paths()
    test_four_delimiters_can_yield_three_segments()
    test_four_delimiters_three_photo_blocks_yield_three_segments()
    test_replay_plan_from_cache_prefers_cached_delimiter_candidates()
    test_list_delimiter_followers_preview_prefers_cached_candidates()
    test_replay_plan_from_cache_uses_cached_file_order_when_marked_sorted()
    test_step3_ordered_media_files_uses_sorted_cache_without_resort()
    test_compute_step3_delimiter_preview_uses_sorted_cache_without_resort()
    test_execute_plan_reports_progress()
    test_execution_plan_includes_delimiterless_renamed_segments()
    test_allocate_unique_folder_name()
    test_execute_plan_merges_segments_with_identical_approved_names()
    test_execute_plan_index_defaults_distinct_and_shared_name_merges()
    test_delimiterless_segments_get_xx_marker()
    test_execution_includes_all_step3_segments_with_xx_and_merge()
    test_untouched_segments_each_get_a_folder_real_flow()
    test_step5_preview_uses_current_step3_name_over_stale_snapshot()
    test_plan_required_notice_shows_success_after_sort()
    test_select_execution_segments_robust_to_missing_cache()
    test_step3_interval_between_delimiters()
    test_default_folder_names_are_sequential_indices()
    test_default_and_manual_mix_survive_snapshot_rebuild()
    test_safe_folder_name_bounds_noisy_multiline_ocr()
    test_segment_index_maps_displayed_follower_not_closing()
    test_step3_names_survive_plate_first_no_leading_delimiter()
    test_step3_first_and_mid_segment_names_survive_rebuild()
    test_safe_image_display_helpers()
    test_sanitize_delimiter_preview_rows()
    test_step3_no_segments_notice_messages()
    test_write_uploaded_media_to_dir_preserves_names_and_order()
    test_write_uploaded_media_to_dir_resolves_name_collisions()
    print("OK — minden teszt sikeres")
