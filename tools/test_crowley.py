"""Algorithm, journal failure, and local-corpus tests; no source prose fixtures."""
from array import array
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
from hashlib import sha1, sha256
import io
import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.crowley import draw
from tools.crowley import paginate as pg
from tools.crowley.pdf_text import Block, Note, Paragraph, continue_note, normalize, resolve_sg_anchors, word_count


def paragraph(text, kinds=None):
    return Paragraph(1, 1, text, array("I", range(len(text))), kinds or {"prose"})


def invented_unit():
    return dict(unit=7, chapter=1, part=1, unit_in_chapter=2, unit_total_in_chapter=4,
                sg_pages=[35, 36], pdf_pages=[6, 7], footnotes=[])


@pytest.fixture(scope="module")
def corpus():
    if not all(p.is_file() for p in (pg.CONFIG, pg.INDEX, pg.MANIFEST)):
        pytest.skip("local PDF configuration or index is not available")
    config = pg.load_config()
    if not Path(config["pdf_path"]).is_file():
        pytest.skip("local, user-supplied source PDF is not mounted")
    return pg.build(config)


def test_word_count_markers_symbols_numbers_and_unicode():
    assert word_count("alpha[1] --- {575} [2] 1875 186 proof beta") == 5
    assert word_count("--- ... ? ! —") == 0
    assert word_count("été 2026 twenty-five") == 3


def test_normalize_preserves_spelling_maps_and_hyphen():
    raw = "  amber-\nblue  peo˙le\n1875 [1]  "
    text, origins = normalize(raw, 20)
    assert text == "amber-blue peo˙le 1875 [1]"
    assert len(text) == len(origins)
    assert raw[origins[text.index("˙")] - 20] == "˙"
    assert normalize("first line\n....second line", verse=True)[0] == "first line\n....second line"


def test_round_half_up_not_bankers_rounding():
    assert pg.progress(1, 2000) == 0.001
    assert pg.progress(1001, 2000) == 0.501


def test_nearest_boundary_ties_and_tail_merge():
    pieces = [dict(words=n, token=i) for i, n in enumerate([300, 210, 330, 150])]
    groups = pg.choose_groups(pieces, 500, 200)
    assert [[p["words"] for p in g] for g, _ in groups] == [[300, 210], [330, 150]]
    groups = pg.choose_groups([dict(words=n) for n in [450, 100]], 500, 200)
    assert groups[0][1] == ["tail_merged"]
    assert sum(p["words"] for p in groups[0][0]) == 550
    groups = pg.choose_groups([dict(words=n) for n in [490, 222]], 500, 200)
    assert len(groups) == 2  # Approved short chapter tail, not an altered threshold.


def test_long_paragraph_sentence_fallback_and_protected_blocks():
    config = dict(target_words=500, long_paragraph_split=800)
    text = " ".join(("amber " * 99 + "glows.") for _ in range(10))
    parts = pg.fragments(paragraph(text), config)
    assert len(parts) == 2
    assert [word_count(text[a:b]) for a, b, _ in parts] == [500, 500]
    assert all("sentence_fallback" in e for _, _, e in parts)
    for kind in ("quote", "verse"):
        assert pg.fragments(paragraph(text, {kind}), config) == [(0, len(text), ["protected_long_paragraph"])]
    unbroken = "amber " * 900
    assert pg.fragments(paragraph(unbroken), config)[0][2] == ["no_sentence_boundary"]


def test_sentence_context_abbreviations():
    text = "Dr. Amber opens a box. Blue closes it. Copper waits."
    assert draw.context_sentences(text) == "Dr. Amber opens a box. Blue closes it."
    assert draw.context_sentences(text, before=True) == "Blue closes it. Copper waits."


def test_invalid_config_and_cli_do_not_draw(tmp_path):
    path = tmp_path / "config.json"
    for invalid in ({}, dict(pdf_path="relative.pdf", target_words=500, merge_tail_below=200, long_paragraph_split=800),
                    dict(pdf_path=str(tmp_path / "source.pdf"), target_words=True, merge_tail_below=200, long_paragraph_split=800)):
        path.write_text(json.dumps(invalid), encoding="utf-8")
        with pytest.raises(ValueError):
            pg.load_config(path)
    for args in (["draw", "--question", " "], ["show", "--chapter", "1"],
                 ["show", "--unit", "1", "--unit-in-chapter", "1"], ["show", "--unit", "0"]):
        with patch.object(draw.secrets, "randbelow") as random, pytest.raises(SystemExit):
            draw.main(args)
        random.assert_not_called()


def test_receipt_is_durable_before_payload_or_stdout(tmp_path):
    log = tmp_path / "draw_log.jsonl"
    unit = invented_unit()
    payload = dict(unit, text="Invented test passage.", context_before=None, context_after=None)
    def observed_payload(*args):
        receipt = json.loads(log.read_text(encoding="utf-8"))
        assert receipt["unit"] == 7
        return payload.copy()
    class ObservedOutput(io.StringIO):
        def write(self, text):
            assert len(log.read_bytes().splitlines()) == 1
            return super().write(text)
    with patch.object(draw, "LOG", log), patch.object(draw, "load_verified", return_value=(None, {"units": [unit]}, "a" * 64)), \
            patch.object(draw, "unit_payload", side_effect=observed_payload), patch.object(draw.secrets, "randbelow", return_value=0) as random, \
            patch.object(draw.os, "fsync", wraps=draw.os.fsync) as sync, redirect_stdout(ObservedOutput()) as output:
        assert draw.main(["draw", "--question", "测试", "--json"]) == 0
    random.assert_called_once_with(1)
    sync.assert_called_once()
    receipt = json.loads(log.read_text(encoding="utf-8"))
    assert set(receipt) == draw.RECEIPT_KEYS
    assert json.loads(output.getvalue())["text"] == payload["text"]


@pytest.mark.parametrize("mode", ["payload", "stdout"])
def test_post_commit_failure_recovers_same_unit_without_retry(tmp_path, mode):
    log = tmp_path / "draw_log.jsonl"
    unit = invented_unit()
    payload = dict(unit, text="Invented test passage.", context_before=None, context_after=None)
    class BrokenOutput(io.StringIO):
        def write(self, text):
            raise BrokenPipeError("output unavailable")
    out, err = BrokenOutput() if mode == "stdout" else io.StringIO(), io.StringIO()
    with patch.object(draw, "LOG", log), patch.object(draw, "load_verified", return_value=(None, {"units": [unit]}, "b" * 64)), \
            patch.object(draw, "unit_payload", side_effect=ValueError("extraction failed") if mode == "payload" else None, return_value=payload), \
            patch.object(draw.secrets, "randbelow", return_value=0) as random, redirect_stdout(out), redirect_stderr(err):
        assert draw.main(["draw", "--question", "测试"]) == 1
    random.assert_called_once()
    assert json.loads(log.read_text(encoding="utf-8"))["unit"] == 7
    assert "show --unit 7" in err.getvalue()


def test_corrupt_log_and_locked_log_block_before_entropy(tmp_path):
    log = tmp_path / "draw_log.jsonl"
    for data in (b'{"partial":', b'{}\n', b'null\n'):
        log.write_bytes(data)
        with patch.object(draw.secrets, "randbelow") as random, pytest.raises(ValueError):
            draw.commit_draw("test", [invented_unit()], "c" * 64, log)
        random.assert_not_called()
        assert log.read_bytes() == data
    log.write_bytes(b"")
    with draw.locked_log(log), patch.object(draw.secrets, "randbelow") as random, pytest.raises(OSError):
        draw.commit_draw("test", [invented_unit()], "c" * 64, log)
    random.assert_not_called()


@pytest.mark.parametrize("mode", ["short_write", "fsync"])
def test_persistence_failure_consumes_once_and_reports_identity(tmp_path, mode):
    class ShortWriter(io.BytesIO):
        def write(self, data):
            super().write(data[:5])
            return 5
    @contextmanager
    def fake_log(path):
        yield ShortWriter()
    err = io.StringIO()
    kwargs = dict(side_effect=OSError("disk failure")) if mode == "fsync" else dict(wraps=draw.os.fsync)
    lock = patch.object(draw, "locked_log", fake_log) if mode == "short_write" else patch.object(draw, "LOG", tmp_path / "draw.jsonl")
    with lock, patch.object(draw.os, "fsync", **kwargs), patch.object(draw.secrets, "randbelow", return_value=0) as random, redirect_stderr(err):
        with pytest.raises(OSError):
            draw.commit_draw("test", [invented_unit()], "d" * 64)
    random.assert_called_once()
    assert "段 7" in err.getvalue() and "禁止重抽" in err.getvalue()


def test_corpus_structure_known_positions_and_source_probes(corpus):
    book, manifest, index = corpus
    assert len(book.chapters) == 97
    for c, page in {1: 5, 23: 113, 49: 234, 63: 351, 77: 483, 96: 578}.items():
        assert book.chapters[c]["heading"]["page"] == page
    assert book.chapters[63]["body_start"]["page"] == 352
    for page, block, chapter in [(115, 1, 23), (356, 6, 63), (483, 6, 77)]:
        raw = next(b for b in book.blocks if (b.page, b.number) == (page, block))
        matching = [p for p in book.paragraphs if p.origins[0] <= raw.base < p.origins[-1]]
        assert len(matching) == 1 and matching[0].chapter == chapter
    final_block = next(b for b in book.blocks if (b.page, b.number) == (585, 4))
    assert book.paragraphs[-1].text == normalize(final_block.text)[0]
    assert manifest["body"]["end"] == {"page": 585, "block": 5, "offset": 0}


def test_cross_page_and_sg_continuations_are_single_paragraphs(corpus):
    book, _, _ = corpus
    for left_key, right_key in [((355, 8), (356, 3)), ((356, 9), (356, 11)), ((326, 6), (326, 8))]:
        left = next(b for b in book.blocks if (b.page, b.number) == left_key)
        right = next(b for b in book.blocks if (b.page, b.number) == right_key)
        p = next(p for p in book.paragraphs if p.origins[0] <= left.base <= p.origins[-1])
        assert right.base in p.origins
        expected = normalize(left.text)[0] + " " + normalize(right.text)[0]
        assert expected in p.text
    assert {"prose", "verse", "quote", "attribution"} <= set().union(*(p.kinds for p in book.paragraphs))


def test_notes_all_attached_and_numeric_source_intact(corpus):
    book, manifest, index = corpus
    assert manifest["footnote_counts"] == {"author": 77, "editor": 29}
    assert all(n.marker is not None for n in book.notes)
    notes = {n.id: n for n in book.notes}
    attached = [n["id"] for u in index["units"] for n in u["footnotes"]]
    assert len(attached) == len(set(attached)) == 77
    assert all(not notes[n].editor for n in attached)
    numeric = next(b for b in book.blocks if (b.page, b.number) == (48, 8))
    p = next(p for p in book.paragraphs if numeric.base in p.origins)
    assert "1897[1]" in p.text  # A year plus a separate bold source marker.
    assert any(n.start["page"] == 81 and not n.editor for n in book.notes)
    assert all("WEH" not in draw.retrieve(book, u) for u in index["units"])


def test_exclusions_never_contribute_characters_to_body(corpus):
    book, manifest, _ = corpus
    occupied = bytearray(book.blocks[-1].base + len(book.blocks[-1].text) + 1)
    for p in book.paragraphs:
        for position in p.origins:
            occupied[position] = 1
    blocks = {(b.page, b.number): b for b in book.blocks}
    for exclusion in manifest["exclusions"]:
        a, z = exclusion["start"], exclusion["end"]
        lo = blocks[a["page"], a["block"]].base + a["offset"]
        hi = blocks[z["page"], z["block"]].base + z["offset"]
        assert not any(occupied[lo:hi]), exclusion


def test_partition_hashes_lengths_progress_and_boundaries(corpus):
    book, manifest, index = corpus
    pg.validate(book, manifest, index)
    for u in index["units"]:
        text = draw.retrieve(book, u)
        assert sha1(text.encode("utf-8")).hexdigest() == u["sha1"]
        assert word_count(text) == u["words"]
        if not 250 <= u["words"] <= 800:
            assert u["exceptions"] == ["approved_short_chapter_tail"]
            assert u["unit_in_chapter"] == u["unit_total_in_chapter"]
            assert 200 <= u["words"] < 250
        last = u["slices"][-1]
        if last["to"] != len(book.paragraphs[last["paragraph"] - 1].text):
            assert "sentence_fallback" in u["exceptions"]
        assert all(book.paragraphs[s["paragraph"] - 1].chapter == u["chapter"] for s in u["slices"])
    assert sum("approved_short_chapter_tail" in u["exceptions"] for u in index["units"]) == 5
    assert [u["unit"] for u in index["units"] if "approved_short_chapter_tail" in u["exceptions"]] == [112, 168, 187, 225, 430]


def test_index_manifest_text_free_and_reproducible(corpus):
    book, manifest, index = corpus
    assert pg.encode_json(manifest) == pg.MANIFEST.read_bytes()
    assert pg.encode_json(index) == pg.INDEX.read_bytes()
    assert pg.make_report(book, manifest, index).encode("utf-8") == pg.REPORT.read_bytes()
    def visit(value):
        if isinstance(value, str):
            assert len(value) <= 80
            assert ":" + chr(92) not in value and "/" + "Users/" not in value
        elif isinstance(value, dict):
            assert not {"text", "content", "excerpt", "question", "pdf_path"} & value.keys()
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(index)
    visit(manifest)


def test_context_stays_in_chapter_and_show_does_not_log(corpus, tmp_path):
    book, _, index = corpus
    for c in range(97):
        us = [u for u in index["units"] if u["chapter"] == c]
        assert draw.unit_payload(book, index, us[0])["context_before"] is None
        assert draw.unit_payload(book, index, us[-1])["context_after"] is None
    out = io.StringIO()
    with patch.object(draw, "load_verified", return_value=(book, index, "e" * 64)), \
            patch.object(draw, "commit_draw") as commit, patch.object(draw.secrets, "randbelow") as random, redirect_stdout(out):
        assert draw.main(["show", "--chapter", "63", "--unit-in-chapter", "1", "--json"]) == 0
    random.assert_not_called()
    commit.assert_not_called()
    payload = json.loads(out.getvalue())
    assert payload["chapter"] == 63 and payload["context_before"] is None
    assert sha1(payload["text"].encode()).hexdigest() == payload["sha1"]


def test_check_is_read_only(corpus):
    with patch.object(Path, "write_bytes") as write, redirect_stdout(io.StringIO()):
        assert pg.main(["--check"]) == 0
    write.assert_not_called()


@pytest.mark.parametrize("damage", ["source", "index", "manifest", "rules"])
def test_changed_inputs_fail_closed_before_entropy(corpus, tmp_path, damage):
    book, manifest, index = corpus
    local_m, local_i = tmp_path / "manifest.json", tmp_path / "index.json"
    m, ix = deepcopy(manifest), deepcopy(index)
    if damage == "source":
        m["pdf"]["sha256"] = "0" * 64
        ix["manifest_sha256"] = sha256(pg.encode_json(m)).hexdigest()
    elif damage == "index":
        ix["units"][0]["sha1"] = "0" * 40
    elif damage == "manifest":
        m["footnote_counts"]["author"] += 1
    local_m.write_bytes(pg.encode_json(m))
    local_i.write_bytes(pg.encode_json(ix))
    config_path = pg.CONFIG
    if damage == "rules":
        config = pg.load_config()
        config["target_words"] += 1
        config_path = tmp_path / "config.json"
        config_path.write_bytes(pg.encode_json(config))
    with patch.object(draw, "MANIFEST", local_m), patch.object(draw, "INDEX", local_i), \
            patch.object(draw.secrets, "randbelow") as random, redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
        assert draw.main(["draw", "--config", str(config_path), "--question", "test"]) == 1
    random.assert_not_called()
    assert not output.getvalue()


@pytest.mark.parametrize("raw,expected", [
    ([48, 9, 50], [48, 49, 50]),
    ([544, 544, 546], [544, 545, 546]),
    ([543, 544, 544, 545, 546], [543, 544, 544, 545, 546]),
    ([65, 67], [65, 67]),
    ([65, 67, 68], [65, 67, 68]),
    ([48, 9, 51], [48, 9, 51]),
    ([48, 9, 10, 51], [48, 9, 10, 51]),
    ([48, 49, 50], [48, 49, 50]),
    ([9, 50, 51], [9, 50, 51]),
])
def test_sg_inference_uses_only_unique_original_neighbors(raw, expected):
    anchors = [dict(number=n, absolute=i * 10, position=dict(page=1, block=i, offset=0))
               for i, n in enumerate(raw)]
    original = deepcopy(anchors)
    resolved, issues = resolve_sg_anchors(anchors)
    assert anchors == original
    assert [a["number"] for a in resolved] == expected
    assert [a["raw_number"] for a in resolved] == raw
    assert len(resolved) == len(raw)  # Missing markers are never fabricated.
    for i, anchor in enumerate(resolved):
        if raw[i] != expected[i]:
            assert anchor["inferred_number"] == raw[i - 1] + 1 == raw[i + 1] - 1
            issue = next(a for a in issues if a["kind"] == "sg_inferred_marker" and a["position"] == anchor["position"])
            assert issue["raw_number"] == raw[i]
            assert issue["inferred_number"] == expected[i]
        else:
            assert "inferred_number" not in anchor
    assert resolve_sg_anchors(resolved) == (resolved, issues)
    if any(b <= a for a, b in zip(expected, expected[1:])):
        assert any(a["kind"] == "sg_unresolved_sequence" for a in issues)
    if raw == [65, 67]:
        assert [a["kind"] for a in issues] == ["sg_gap"]


def test_sg_known_rework_units_and_manifest_evidence(corpus):
    book, manifest, index = corpus
    expected = {16: [48, 49], 17: [49, 50], 304: [354, 355], 305: [355, 356],
                364: [420, 421], 365: [421, 422], 418: [474, 475], 419: [475, 476],
                467: [525, 527], 563: [623, 624]}
    for number, pages in expected.items():
        unit = index["units"][number - 1]
        assert unit["sg_pages"] == pages
        assert unit["sg_endpoints"] == pages
    corrections = [a for a in manifest["sg_anchors"] if "inferred_number" in a]
    assert {(a["raw_number"], a["inferred_number"]) for a in corrections} == {
        (9, 49), (335, 355), (412, 421), (465, 475), (536, 526), (642, 624)}
    report = pg.make_report(book, manifest, index)
    for anchor in corrections:
        assert anchor["number"] == anchor["inferred_number"]
        assert f"{anchor['raw_number']} → {anchor['inferred_number']}" in report
        assert pg.location(anchor["position"]) in report
    unresolved = [a for a in manifest["source_anomalies"] if a["kind"] == "sg_unresolved_sequence"]
    assert len(unresolved) == 1 and unresolved[0]["numbers"] == [544, 544]
    assert "**未解决**：原标记 544 → 544" in report
    assert "推断" in report and "未经另一版本校勘" in report


def test_sg_ranges_ordered_and_bounded_or_explicitly_unresolved(corpus):
    book, manifest, index = corpus
    flagged = {a["unit"] for a in manifest["sg_unresolved_units"]}
    report = pg.make_report(book, manifest, index)
    for unit in index["units"]:
        if unit["unit"] in flagged:
            assert f"段 {unit['unit']}：未解决" in report
        else:
            assert unit["sg_endpoints"][0] <= unit["sg_endpoints"][1]
            assert unit["sg_pages"][1] - unit["sg_pages"][0] <= 4
    # This PDF also meets the bounds even at its explicitly ambiguous duplicate.
    assert all(u["sg_endpoints"][0] <= u["sg_endpoints"][1] for u in index["units"])
    assert all(u["sg_pages"][1] - u["sg_pages"][0] <= 4 for u in index["units"])


def test_rework_preserves_every_unit_field_except_sg(corpus):
    _, _, index = corpus
    assert len(index["units"]) == 851
    # Frozen BEFORE rework. Every field of every unit is included except the two
    # explicitly authorized SG fields, including raw boundaries, all slices,
    # words, sha1, progress, footnote identities, types and exception decisions.
    stable = [{k: v for k, v in unit.items() if k not in {"sg_pages", "sg_endpoints"}}
              for unit in index["units"] if unit["chapter"] != 21]
    canonical = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    # Rework 2 separately authorizes chapter 21; retain the original guard on
    # every other unit, rather than replacing it with a new whole-book hash.
    assert sha256(canonical).hexdigest() == "22fe2cdee8cc9ad92d4f25b1229d75d6842433e24d2637e2b343d77fa0d9bd45"


@pytest.mark.parametrize("editor", [True, False])
def test_page_top_footnote_continuation_and_author_attachment(editor):
    previous = Block(6, 9, (173, 710, 520, 770), "1. Amber note continues", 100)
    current = Block(7, 0, (191, 70, 520, 100), "beyond the page.", 200)
    note = Note("n001", 1, 1, editor, "Amber note continues",
                previous.position(), previous.position(len(previous.text)), 100, marker=3)
    assert continue_note(note, current, previous, previous.box[0], True)
    assert note.text == "Amber note continues beyond the page."
    assert note.end == current.position(len(current.text))
    p = paragraph("Invented body.")
    book = SimpleNamespace(paragraphs=[p], notes=[note])
    u = dict(unit=1, chapter=1, slices=[dict(paragraph=1, **{"from": 0, "to": len(p.text)})],
             sha1=sha1(p.text.encode()).hexdigest(),
             footnotes=[] if editor else [dict(id=note.id, number=note.number)])
    payload = draw.unit_payload(book, {"units": [u]}, u)
    assert "Amber" not in payload["text"]
    assert payload["footnotes"] == ([] if editor else [dict(id=note.id, number=1, text=note.text)])


@pytest.mark.parametrize("terminal,x,first,previous_matches", [
    (True, 191, True, True), (False, 71, True, True),
    (False, 191, False, True), (False, 191, True, False),
])
def test_note_continuation_requires_all_layout_guards(terminal, x, first, previous_matches):
    previous = Block(6, 9, (173, 710, 520, 770), "1. Amber note", 100)
    current = Block(7, 0, (x, 70, 520, 100), "Body remains separate.", 200)
    note = Note("n001", 1, 1, True, "Amber note." if terminal else "Amber note",
                previous.position(), previous.position(len(previous.text)), 100)
    before = deepcopy(note)
    last = previous if previous_matches else Block(6, 10, (71, 775, 500, 779), "Other", 180)
    assert not continue_note(note, current, last, 173, first)
    assert note == before


def test_rework2_only_chapter21_units_change_and_all_editor_sentences_excluded(corpus):
    book, _, index = corpus
    assert len(index["units"]) == 851
    us = [u for u in index["units"] if u["chapter"] == 21]
    assert [u["unit"] for u in us] == [142, 143, 144]
    assert [u["words"] for u in us] == [518, 534, 535]
    other = [u for u in index["units"] if u["chapter"] != 21]
    canonical = json.dumps(other, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert sha256(canonical).hexdigest() == "7f9895324415a7aee104b07afb4affd4c60c70adab5bb64870fdacc5424a0a3b"
    continued = [a for a in book.anomalies if a["kind"] == "footnote_page_continuation"]
    assert len(continued) == 1 and continued[0]["note"] == "n041"
    note = next(n for n in book.notes if n.id == "n041")
    tail = next(b for b in book.blocks if (b.page, b.number) == (101, 1))
    assert note.editor and note.text.endswith(normalize(tail.text)[0])
    assert note.end == tail.position(len(tail.text))
    p = [p for p in book.paragraphs if p.chapter == 21][3]
    preceding = next(b for b in book.blocks if (b.page, b.number) == (100, 9))
    assert p.text.endswith(normalize(preceding.text)[0])
    assert all(origin < tail.base or origin >= tail.base + len(tail.text)
               for p in book.paragraphs for origin in p.origins)
    norm = lambda s: " ".join(re.findall(r"\w+", s.casefold()))
    body = " " + norm(" ".join(p.text for p in book.paragraphs)) + " "
    checked = 0
    for n in book.notes:
        if not n.editor:
            continue
        start = 0
        for end in pg.sentence_ends(n.text):
            sentence = norm(n.text[start:end])
            start = end
            if sentence:
                assert " " + sentence + " " not in body, n.id
                checked += 1
    assert checked == 81
    with redirect_stdout(io.StringIO()) as out:
        assert draw.main(["show", "--unit", "142"]) == 0
    assert norm(tail.text) not in norm(out.getvalue())


def test_historical_receipt_survives_show_and_new_index_draw(corpus, tmp_path):
    book, _, index = corpus
    if not draw.LOG.is_file():
        pytest.skip("private historical draw log is not mounted")
    before = draw.LOG.read_bytes()
    old_line = before.splitlines(keepends=True)[0]
    assert sha256(old_line).hexdigest() == "a98b4db3576b1f10d44ac4dd5a0e636c3ecef76971856a51997b78185d1f0252"
    old = json.loads(old_line)
    new_digest = sha256(pg.encode_json(index)).hexdigest()
    assert old["unit"] == 408 and old["index_sha256"] != new_digest
    draw.validate_receipt(old)
    with patch.object(draw, "load_verified", return_value=(book, index, new_digest)), \
            patch.object(draw.secrets, "randbelow") as random, redirect_stdout(io.StringIO()) as output:
        assert draw.main(["show", "--unit", "408", "--json"]) == 0
    assert json.loads(output.getvalue())["unit"] == 408
    random.assert_not_called()
    temporary_log = tmp_path / "history.jsonl"
    temporary_log.write_bytes(before)
    # An isolated, mocked draw proves compatibility without consuming a real
    # draw or appending to the original historical journal.
    with patch.object(draw, "LOG", temporary_log), \
            patch.object(draw, "load_verified", return_value=(book, index, new_digest)), \
            patch.object(draw.secrets, "randbelow", return_value=15) as random, redirect_stdout(io.StringIO()) as output:
        assert draw.main(["draw", "--question", "test", "--json"]) == 0
    random.assert_called_once_with(851)
    payload = json.loads(output.getvalue())
    assert payload["receipt"]["sg_pages"] == payload["sg_pages"] == [48, 49]
    assert payload["receipt"]["index_sha256"] == new_digest
    after = temporary_log.read_bytes()
    assert after.startswith(before) and len(after[len(before):].splitlines()) == 1
    assert draw.LOG.read_bytes() == before
