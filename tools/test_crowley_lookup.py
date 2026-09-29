"""Read-only lookup, public anchors, conservative time filtering and exact aliases."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools.crowley import lookup as lk

pytestmark = pytest.mark.skipif(not lk.INDEX.is_file() or not lk.MANIFEST.is_file(), reason="本地索引未建")

@pytest.fixture(scope="module")
def bundle():
    return lk.load_data()


def overlap(unit, segment):
    a, b = map(lambda v: Decimal(str(v)), unit["progress"])
    c, d = Decimal(str(segment["from"])), Decimal(str(segment["to"]))
    return max(Decimal(0), min(b, d) - max(a, c))


def test_all_851_units_have_complete_ordered_public_coverage(bundle):
    cards, index = bundle
    assert len(index["units"]) == 851
    frozen = json.dumps((cards, index), ensure_ascii=False)
    for unit in index["units"]:
        result = lk.lookup(unit["unit"], cards, index)
        segments = result["segments"]
        assert segments
        assert segments[0]["from"] <= unit["progress"][0]
        assert segments[-1]["to"] >= unit["progress"][1]
        assert all(a["to"] == b["from"] for a, b in zip(segments, segments[1:]))
        weights = {}
        for s in segments:
            length = overlap(unit, s)
            assert length > 0
            width = Decimal(str(unit["progress"][1])) - Decimal(str(unit["progress"][0]))
            assert s["share"] == float(round(length / width, 3))
            assert s["continues_after"] == (s["to"] > unit["progress"][1])
            weights[s["period"]] = weights.get(s["period"], Decimal(0)) + length
            assert "paragraph_range" not in s
            assert all(n["label"] not in {"短段说明", "长段说明"} for n in s["notes"])
        main = result["main_period"]
        assert (main is None) == (unit["chapter"] == 0)
        if main:
            assert main["id"] == max(weights, key=weights.get)
            assert {p["id"] for p in result["other_periods"]} == set(weights) - {main["id"]}
        prior = [u["unit"] for u in index["units"] if u["chapter"] == unit["chapter"]
                 and u["unit"] < unit["unit"] and overlap(u, segments[0])]
        assert result["prior_units"] == ([min(prior), max(prior)] if prior else None)
        if prior:
            assert prior == list(range(min(prior), max(prior) + 1))
        # Also exercise text formatting for prelude, every period, and every edge.
        assert lk.format_lookup(result, cards).startswith("## 位置\n")
    assert json.dumps((cards, index), ensure_ascii=False) == frozen


def test_known_unit_408_and_cross_period_unit_65(bundle):
    cards, index = bundle
    r = lk.lookup(408, cards, index)
    assert r["chapter"] == 54
    assert [s["id"] for s in r["segments"]] == ["54-4", "54-5"]
    assert r["main_period"]["id"] == "P10"
    r = lk.lookup(65, cards, index)
    assert r["chapter"] == 10 and r["main_period"]["id"] == "P04"
    assert [p["id"] for p in r["other_periods"]] == ["P10", "P02"]
    assert all(set(p) == {"id", "title", "years", "position"} for p in r["other_periods"])
    for p in r["other_periods"]:
        original = next(x for x in cards["periods"]["periods"] if x["id"] == p["id"])
        assert p["position"] == original["position"]


@pytest.fixture
def synthetic(bundle):
    cards, index = deepcopy(bundle)
    unit = deepcopy(index["units"][0])
    unit.update(unit=1, chapter=1, part=1, unit_in_chapter=1, unit_total_in_chapter=1, progress=[0, 1])
    source = deepcopy(cards["chapters"]["chapters"][1]["segments"][0])
    source.update(id="1-1", period="P15", event_time="1915 年至 1916 年", cast=[], notes=[], **{"from": 0, "to": 0.75})
    flashback = deepcopy(source)
    flashback.update(id="1-2", period="P14", event_time="1922 年补述", **{"from": 0.75, "to": 1})
    cards["chapters"]["chapters"] = [dict(chapter=1, status="cc_approved", segments=[source, flashback])]
    p = next(p for p in cards["periods"]["periods"] if p["id"] == "P15")
    entries = [{"when": "1917 年至 1918 年", "text": "晚年条目独有标记"},
               {"when": "1916 年底", "text": "同年保留标记"}, {"when": "年底", "text": "无年份保留标记"},
               {"when": "1914 年至 1919 年", "text": "首年保留标记"}]
    p["position"]["shifts"] = deepcopy(entries)
    p["dated_entries"] = [{**e, "text": e["text"] + "按年份"} for e in entries]
    return cards, {"units": [unit]}


def test_later_years_main_period_only_and_both_entry_types(synthetic):
    cards, index = synthetic
    r = lk.lookup(1, cards, index)
    main = r["main_period"]
    assert main["event_year"] == 1916
    assert main["event_times"] == ["1915 年至 1916 年"]
    assert len(main["position"]["shifts"]) == len(main["dated_entries"]) == 3
    assert len(main["shifts_later"]) == len(main["dated_entries_later"]) == 1
    text = lk.format_lookup(r, cards)
    assert "晚年条目独有标记" not in text
    for label in ("同年保留标记", "无年份保留标记", "首年保留标记"):
        assert label in text
    assert "另有 2 条明显晚于本段事件（本段事件最晚到 1916 年）的变化，主读不用；追问时加 --later 查看。" in text
    later = lk.lookup(1, cards, index, later=True)
    assert "later" not in r and set(later["later"]) == {"overview", "appendix"}
    text_later = lk.format_lookup(later, cards)
    assert "晚年条目独有标记" in text_later
    assert text_later.index("晚年条目独有标记") > text_later.index("## 后来的事")


def test_no_event_year_keeps_every_entry(synthetic):
    cards, index = synthetic
    cards["chapters"]["chapters"][0]["segments"][0]["event_time"] = "某日"
    main = lk.lookup(1, cards, index)["main_period"]
    assert main["event_year"] is None
    assert len(main["position"]["shifts"]) == len(main["dated_entries"]) == 4
    assert main["shifts_later"] == main["dated_entries_later"] == []


def test_equal_weights_follow_first_scene_and_touching_boundaries_do_not_overlap(synthetic):
    cards, index = synthetic
    a, b = cards["chapters"]["chapters"][0]["segments"]
    a["to"] = b["from"] = 0.5
    assert lk.lookup(1, cards, index)["main_period"]["id"] == "P15"
    index["units"][0]["progress"] = [0.5, 1]
    r = lk.lookup(1, cards, index)
    assert [s["id"] for s in r["segments"]] == ["1-2"]
    assert r["main_period"]["id"] == "P14" and r["prior_units"] is None


@pytest.mark.parametrize("name,expected", [
    ("甲（乙）与丙以及丁、戊，己;庚/辛和壬及癸", list("甲乙丙丁戊己庚辛壬癸")),
    (" L. T. （纽伯格）", ["L. T.", "纽伯格"]),
    ("阿诺德·贝内特", ["阿诺德·贝内特"]), ("猫头鹰", ["猫头鹰"]),
])
def test_whole_name_tokenization(name, expected):
    assert lk.split_cast_name(name) == expected


def test_real_alias_cases_and_no_substring_false_matches(bundle):
    cards, index = bundle
    present = lambda n: {p["id"] for p in lk.lookup(n, cards, index)["people"]["present"]}
    assert {"person-64", "person-66"} <= present(785)  # 88-2
    assert "person-40" not in present(348)  # Allan Bennett, not Arnold Bennett (50-3)
    for n in (734, 735):  # 82-2
        assert not any(p["name"] == "Hilarion" for p in lk.lookup(n, cards, index)["people"]["present"])


def test_other_period_people_still_have_full_cards(synthetic):
    cards, index = synthetic
    seg = cards["chapters"]["chapters"][0]["segments"][1]
    seg["cast"] = [{"name": "L. T.", "state": "在场。"}]
    r = lk.lookup(1, cards, index)
    p = next(p for p in r["people"]["present"] if p["id"] == "person-56")
    assert p["period"] == "P14" and p["fields"]
    assert "Victor Neuburg · P14" in lk.format_lookup(r, cards)
    assert all(p["period"] == "P15" and set(p) == {"id", "name", "period", "years", "身份", "此时对他重不重要"} for p in r["people"]["absent"])


def test_special_notes_survive_and_review_length_notes_do_not(bundle):
    cards, index = bundle
    r = lk.lookup(539, cards, index)
    assert {"65-9", "65-10"} <= {s["id"] for s in r["segments"]}
    assert "卦画字符" in lk.format_lookup(r, cards)
    for sid in ("32-1", "36-1"):
        scene = next(s for ch in cards["chapters"]["chapters"] for s in ch["segments"] if s["id"] == sid)
        n = next(u["unit"] for u in index["units"] if u["chapter"] == int(sid.split('-')[0]) and overlap(u, scene))
        returned = next(s for s in lk.lookup(n, cards, index)["segments"] if s["id"] == sid)
        assert returned["notes"] == [n for n in scene["notes"] if n["label"] not in lk.HIDDEN_NOTES]


def test_fixed_headings_prelude_and_later_only_sections(bundle):
    cards, index = bundle
    r = lk.lookup(408, cards, index)
    default = lk.format_lookup(r, cards)
    for heading in ("## 位置", "## 所在场景段", "## 前情可查", "## 时期卡：P10", "## 人物卡",
                    "### 本段出场", "### 本时期重要、本段没出场", "## 叙述者卡", "## 价值观卡"):
        assert heading in default
    assert lk.EVIDENCE in default and "时期卡写到整个时期结束。只用到本段事件时间为止的条目。" in default
    assert (lk.CONTINUATION in default) == any(s["continues_after"] for s in r["segments"])
    assert "生涯总览" not in default and "1923 年以后" not in default
    extended = lk.format_lookup(lk.lookup(408, cards, index, later=True), cards)
    assert extended.startswith(default)
    for heading in ("## 后来的事（追问用；引用时必须标明是后来的事）", "### 晚于本段事件的变化与年份条目", "### 生涯总览", "### 1923 年以后"):
        assert heading in extended
    for n in (1, 2, 3):
        r = lk.lookup(n, cards, index)
        text = lk.format_lookup(r, cards)
        assert "## 时期卡\n序：无生活时期，参照叙述者卡。" in text
        assert "## 人物卡" not in text and r["people"] == {"present": [], "absent": []}
    assert "本段从场景段开头起，没有同段前情。" in lk.format_lookup(lk.lookup(1, cards, index), cards)
    with_prior = next(lk.lookup(n, cards, index) for n in range(2, 20) if lk.lookup(n, cards, index)["prior_units"])
    assert "用 python tools/crowley/draw.py show --unit M 只读查看；本段之后的段不看。" in lk.format_lookup(with_prior, cards)


def test_public_projection_does_not_need_local_anchors(bundle):
    cards, index = deepcopy(bundle)
    expected = lk.lookup(408, cards, index)
    for ch in cards["chapters"]["chapters"]:
        for s in ch["segments"]:
            s.pop("paragraph_range", None)
    for unit in index["units"]:
        for key in ("slices", "start", "end", "pdf_pages", "sha1", "sg_endpoints"):
            unit.pop(key, None)
    assert lk.lookup(408, cards, index) == expected
    assert set(expected) == {"unit", "chapter", "part", "unit_in_chapter", "unit_total_in_chapter", "sg_pages", "progress", "segments", "prior_units", "main_period", "other_periods", "people", "narrator", "values"}


def test_cli_is_read_only_even_when_source_functions_raise(monkeypatch, tmp_path):
    from tools.crowley import draw, paginate, pdf_text
    def forbidden(*args, **kwargs):
        raise AssertionError("source, randomness, or file write attempted")
    for module, names in ((draw, ("load_verified", "retrieve", "render_slices")),
                          (paginate, ("build", "render_slices", "load_config")), (pdf_text, ("read_pdf",))):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    log = lk.HERE.parents[1] / "data/crowley/draw_log.jsonl"
    if not log.is_file():
        log = tmp_path / "draw_log.jsonl"
        log.write_bytes(b"")
    monkeypatch.setattr(draw, "LOG", log)
    before = sha256(log.read_bytes()).hexdigest()
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    out = io.StringIO()
    with redirect_stdout(out):
        assert lk.main(["--unit", "408", "--json"]) == 0
    assert json.loads(out.getvalue())["unit"] == 408
    assert sha256(log.read_bytes()).hexdigest() == before


def test_cli_fresh_process_imports_only_standard_library_and_reads_only_json():
    script = '''
import sys, runpy
from pathlib import Path
root=Path.cwd()
allowed={str((root/'tools/crowley'/name).resolve()) for name in ['lookup.py','manifest.json','unit_index.json']}
allowed.update(str(p.resolve()) for p in (root/'tools/crowley/cards').glob('*.json'))
def audit(event,args):
 if event=='open' and isinstance(args[0],(str,bytes)):
  name=str(Path(args[0]).resolve())
  if name.startswith(str(root)) and name not in allowed: raise AssertionError('unexpected project read: '+name)
  if args[1] and any(c in args[1] for c in 'wax+'): raise AssertionError('write attempted')
 if event=='import' and args[0].split('.')[0] in {'fitz','pymupdf','tools','random','secrets'}: raise AssertionError('forbidden import')
sys.addaudithook(audit)
sys.argv=['lookup.py','--unit','408','--json']
runpy.run_path(str(root/'tools/crowley/lookup.py'),run_name='__main__')
'''
    result = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["unit"] == 408


@pytest.mark.parametrize("number", [0, -1, 852])
def test_cli_out_of_range_is_error_one_without_stdout(number):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        assert lk.main(["--unit", str(number)]) == 1
    assert not out.getvalue() and err.getvalue().startswith("ERROR: ")


def test_manifest_mismatch_fails_before_cards_read(bundle, tmp_path, monkeypatch):
    _, index = bundle
    path = tmp_path / "index.json"
    path.write_text(json.dumps({**index, "manifest_sha256": "0" * 64}), encoding="utf-8")
    monkeypatch.setattr(lk, "INDEX", path)
    monkeypatch.setattr(lk, "CARDS", tmp_path / "absent")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        assert lk.main(["--unit", "408"]) == 1
    assert out.getvalue() == "" and "ERROR: manifest/index mismatch" in err.getvalue()


@pytest.mark.parametrize("mutation", [
    lambda c, i: c["chapters"]["chapters"][0]["segments"][0].update(to=0.7),
    lambda c, i: c["chapters"]["chapters"][0]["segments"][0].update(period="APPENDIX"),
    lambda c, i: i["units"][0].update(progress=[0.5, 0.5]),
    lambda c, i: i["units"][0].update(unit=9),
])
def test_inconsistent_query_data_is_error_one(synthetic, monkeypatch, mutation):
    cards, index = synthetic
    mutation(cards, index)
    monkeypatch.setattr(lk, "load_data", lambda: (cards, index))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        assert lk.main(["--unit", "1"]) == 1
    assert not out.getvalue() and err.getvalue().startswith("ERROR: ")


def test_rework_percentage_wording_across_all_units(bundle):
    cards, index = bundle
    result = lk.lookup(408, cards, index)
    assert [s["share"] for s in result["segments"]] == [0.177, 0.823]
    assert "本段约 18% 在这个场景段里" in lk.format_lookup(result, cards)
    for unit in index["units"]:
        text = lk.format_lookup(lk.lookup(unit["unit"], cards, index), cards)
        assert "本段覆盖其中" not in text


@pytest.mark.parametrize("share,expected", [
    (0.004, "本段只有不到 1% 在这个场景段里"),
    (0.005, "本段约 1% 在这个场景段里"),
])
def test_rework_small_share_formats_without_changing_json(bundle, share, expected):
    cards, index = bundle
    result = lk.lookup(408, cards, index)
    result["segments"][0]["share"] = share
    before = deepcopy(result)
    assert expected in lk.format_lookup(result, cards)
    assert result == before


@pytest.mark.parametrize("text,year", [
    ("1979", None), ("1874", None), ("1948", None),
    ("1875", 1875), ("1947", 1947),
    ("11875", None), ("18750", None), ("11947", None), ("19470", None),
])
def test_rework_lifetime_year_limits_for_events_and_entries(synthetic, text, year):
    cards, index = synthetic
    cards["chapters"]["chapters"][0]["segments"][0]["event_time"] = text
    assert lk.lookup(1, cards, index)["main_period"]["event_year"] == year
    item = {"when": text, "text": "年份范围测试"}
    current, later = lk.split_later([item], 1874)
    assert later == ([item] if year is not None else [])
    assert current == ([] if year is not None else [item])


def test_rework_unit_539_ignores_original_1979_typo(bundle):
    cards, index = bundle
    result = lk.lookup(539, cards, index)
    assert result["main_period"]["event_year"] == 1920
    assert any("1979" in text for text in result["main_period"]["event_times"])
    assert "卦画字符" in lk.format_lookup(result, cards)


@pytest.mark.parametrize("flags", [[], ["口述时补述"]])
def test_rework_speaker_punctuation_only_before_flags(bundle, flags):
    cards, index = bundle
    result = lk.lookup(408, cards, index)
    result["segments"][0].update(speaker="测试说话人。", flags=flags)
    text = lk.format_lookup(result, cards)
    assert "。；" not in text
    if flags:
        assert "说话人：测试说话人；标记：口述时补述" in text
    else:
        assert "说话人：测试说话人。\n" in text
    assert result["segments"][0]["speaker"] == "测试说话人。"


def test_rework_single_prior_unit_has_no_duplicate_range(bundle):
    cards, index = bundle
    result = lk.lookup(408, cards, index)
    result["prior_units"] = [407, 407]
    text = lk.format_lookup(result, cards)
    assert "同一场景段里、本段之前：第 407 段。" in text
    assert "407–407" not in text
    result["prior_units"] = [406, 407]
    assert "同一场景段里、本段之前：第 406–407 段。" in lk.format_lookup(result, cards)


def test_rework_later_shifts_and_dated_lists_have_distinct_formats(synthetic):
    cards, index = synthetic
    p = next(p for p in cards["periods"]["periods"] if p["id"] == "P15")
    p["position"]["shifts"] = [{"when": "1917 年春", "text": "1917 年春，变化正文。"}]
    p["dated_entries"] = [{"when": "1918 年", "text": "- 第一条正文。\n- 第二条正文。"}]
    result = lk.lookup(1, cards, index, later=True)
    text = lk.format_lookup(result, cards)
    section = text.split("### 晚于本段事件的变化与年份条目\n", 1)[1].split("### 生涯总览", 1)[0]
    assert "- 1917 年春，变化正文。" in section
    assert "- 1917 年春：" not in section
    assert "**1918 年**\n\n- 第一条正文。\n- 第二条正文。" in section
    assert "1918 年：" not in section
