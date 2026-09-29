"""Card schema, counterexamples, lossless migration, and local-source checks."""
from array import array
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from hashlib import sha256
import io
import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.crowley import validate_cards as vc
from tools.crowley.pdf_text import Paragraph


@pytest.fixture
def miniature():
    ps = [Paragraph(i + 1, 1, "amber " * 20, array("I", range(i * 100, i * 100 + 120)), {"prose"}) for i in range(2)]
    book = SimpleNamespace(paragraphs=ps, notes=[], blocks=[],
                           sg_range=lambda origins: [10 + origins[0] // 100, 10 + origins[0] // 100])
    phases = [dict(id=f"S{i}", title="", years="", periods=[], achievements=[], difficulties=[], note="") for i in range(1, 8)]
    periods = []
    for pid in [f"P{i:02}" for i in range(1, 18)] + ["APPENDIX"]:
        p = dict(id=pid, title="时期", years="1900", chapters="1", status="skeleton", phase=None,
                 position=dict(has="", wants="", stuck="", shifts=[]), people_important=[],
                 layers=dict(main=[], secondary=[], absent=[], note=""), dated_entries=[], notes=[])
        p.update({k: "" for k in vc.PERIOD_TEXT})
        periods.append(p)
    segments = []
    for i in range(2):
        m = vc.segment_metrics(book, 1, i + 1, i + 2)
        segments.append(dict(id=f"1-{i+1}", **{k: m[k] for k in ("from", "to", "sg")}, period="P01",
                             types=["事件"], flags=[], event_time="约 1900 年", place="村庄", speaker=None,
                             summary="旅人到达村庄。", cast=[dict(name="旅人", state="准备休息。")],
                             stance=None, quote_note=None, paragraph_range=[i + 1, i + 2],
                             sample_label="", notes=[], migration_exceptions=[]))
    narrator_fields = {k: "叙述说明。" for k in ("谁在说", "人称的变化", "口述时的处境（书内）", "口述时的处境（书外）",
                        "说话时间的层次", "他自己承认的记录缺口", "他几乎不写的事", "后几章的写作处境", "他说自己怎么写", "叙述框架")}
    threads = dict(note="", lines=[dict(id=i, title=i, summary="", items=[]) for i in ("decline", "fame", "works")])
    data = dict(overview=dict(schema_version=1, status="skeleton", summary="", phases=phases, threads=threads),
                chapters=dict(schema_version=1, chapters=[dict(chapter=1, status="draft", segments=segments)]),
                periods=dict(schema_version=1, periods=periods), people=dict(schema_version=1, people=[]),
                narrator=dict(schema_version=1, status="draft", fields=narrator_fields, **{"按话题的可信度": []}),
                values=dict(schema_version=1, values=[]),
                sources=dict(schema_version=1, sources=[dict(id="test-source", title="示例资料", url="https://example.org", accessed="2026-09-28")]))
    return data, book


def first(data):
    return data["chapters"]["chapters"][0]["segments"][0]


def chapter(data, number):
    return next(c for c in data["chapters"]["chapters"] if c["chapter"] == number)


def test_small_valid_cards(miniature):
    data, book = miniature
    assert vc.validate(data, book) == []


@pytest.mark.parametrize("number,period,valid", [
    (0, None, True), (0, "P01", False), (0, "APPENDIX", False),
    (1, None, False), (23, None, False), (96, None, False),
    (1, "APPENDIX", False), (23, "APPENDIX", False), (96, "APPENDIX", False),
    (1, "P01", True), (96, "P17", True),
])
def test_scene_period_routing_rule_ten(miniature, number, period, valid):
    data, book = miniature
    c = data["chapters"]["chapters"][0]
    c["chapter"] = number
    for p in book.paragraphs:
        p.chapter = number
    for ordinal, s in enumerate(c["segments"], 1):
        s["id"] = f"{number}-{ordinal}"
        s["period"] = period
    errors = vc.validate(data, book)
    assert (errors == []) is valid
    if not valid:
        assert any("period" in error for error in errors)


@pytest.mark.parametrize("mutation,expected", [
    (lambda d: first(d).pop("summary"), "invalid fields"),
    (lambda d: first(d).update(summary=""), "1–4 sentences"),
    (lambda d: first(d).update(summary="甲。乙。丙。丁。戊。"), "1–4 sentences"),
    (lambda d: first(d).update(cast=[]), "event requires cast"),
    (lambda d: first(d)["cast"][0].update(name=""), "expected text"),
    (lambda d: first(d).update(types=["议论"]), "requires stance"),
    (lambda d: first(d).update(types=["引文"]), "requires quote_note"),
    (lambda d: first(d).update(types=["引文"], quote_note="来信内容。"), "requires speaker"),
    (lambda d: first(d)["cast"][0].update(state="甲。乙。丙。丁。"), "1–3 sentences"),
    (lambda d: first(d).update(types=["未知"]), "invalid types"),
    (lambda d: first(d).update(flags=["未知"]), "invalid flags"),
    (lambda d: first(d).update(period="P99"), "unknown period"),
    (lambda d: d["periods"]["periods"][0]["layers"].update(main=["炼金"]), "layers"),
    (lambda d: d["periods"]["periods"][0].update(phase="S9"), "unknown phase"),
    (lambda d: d["overview"]["phases"][0].update(periods=["P99"]), "unknown period"),
    (lambda d: first(d).update(paragraph_range=[1, 3]), "SG"),
    (lambda d: first(d).update(**{"from": 0.001}), "natural boundary"),
    (lambda d: first(d).update(**{"to": 0.5001}), "natural boundary"),
    (lambda d: first(d).update(sg="10–99"), "SG"),
    (lambda d: d["chapters"]["chapters"][0]["segments"][1].update(paragraph_range=[1, 2]), "gap/overlap"),
    (lambda d: d["chapters"]["chapters"][0]["segments"].pop(), "chapter end"),
    (lambda d: d["sources"]["sources"][0].update(accessed="2026-99-99"), "invalid date"),
    (lambda d: d["sources"]["sources"][0].update(url="file:///private"), "invalid URL"),
    (lambda d: d["chapters"]["chapters"][0].update(status="complete"), "invalid status"),
])
def test_structure_counterexamples(miniature, mutation, expected):
    data, book = miniature
    mutation(data)
    assert any(expected in e for e in vc.validate(data, book))


@pytest.mark.parametrize("citation", ["【书 99·10】", "【书 1·99】", "【书 1·11–10】",
                                    "【书 1·p10】", "【外 unknown】", "【书 1】", "【书 1·10"])
def test_bad_citations(miniature, citation):
    data, book = miniature
    first(data)["summary"] += citation
    assert vc.validate(data, book)


def test_book_external_and_dedication_citations(miniature):
    data, book = miniature
    first(data)["summary"] += "【书 1·10–11】【外 test-source】【书 卷首】"
    assert vc.validate(data, book) == []


@pytest.mark.parametrize("term", ["SecretName", "机密名", "E:" + chr(92) + "private",
                                "C:" + "/private", "/" + "home/person/private"])
def test_forbidden_content_in_arbitrary_layers(miniature, term, tmp_path, monkeypatch):
    rules = tmp_path / "private_terms.json"
    rules.write_text(json.dumps({"patterns": ["secretname", "机密名"]}), encoding="utf-8")
    monkeypatch.setattr(vc, "PRIVATE_TERMS", rules)
    data, book = miniature
    data["overview"]["summary"] = term
    errors = vc.validate(data, book)
    assert any("forbidden" in e for e in errors)
    assert term not in "\n".join(errors)


def test_status_enum_is_only_narrow_name_exception(miniature, tmp_path, monkeypatch):
    rules = tmp_path / "private_terms.json"
    rules.write_text(json.dumps({"patterns": ["kico_approved".partition("_")[0]]}), encoding="utf-8")
    monkeypatch.setattr(vc, "PRIVATE_TERMS", rules)
    data, book = miniature
    data["narrator"]["status"] = "kico_approved"
    assert vc.validate(data, book) == []
    first(data)["summary"] = "kico_approved"
    assert any("forbidden" in e for e in vc.validate(data, book))


def test_eight_words_ignore_punctuation_case_and_do_not_echo():
    text = "amber blue copper dusk elm frost gold hazel"
    windows = {tuple(vc.tokens(text))}
    value = "AMBER, blue—copper; dusk / elm frost gold hazel."
    errors = vc.content_errors({"summary": value}, {}, set(), windows)
    assert len(errors) == 1 and "eight-word" in errors[0]
    assert text not in errors[0] and value not in errors[0]
    assert vc.content_errors({"summary": "amber blue copper dusk elm frost gold"}, {}, set(), windows) == []


def test_seven_word_english_quote_is_too_long_even_without_overlap():
    value = '“amber blue copper dusk elm frost gold”'
    assert any("six words" in e for e in vc.content_errors({"summary": value}, {}, set(), set()))


def test_windows_detect_across_paragraph_boundary():
    book = SimpleNamespace(paragraphs=[
        SimpleNamespace(chapter=1, text="amber blue copper dusk"),
        SimpleNamespace(chapter=1, text="elm frost gold hazel")], notes=[], blocks=[])
    assert tuple("amber blue copper dusk elm frost gold hazel".split()) in vc.source_windows(book)


def test_duplicate_and_nonfinite_json_rejected(tmp_path):
    path = tmp_path / "sample.json"
    for raw in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'):
        path.write_text(raw, encoding="utf-8")
        with pytest.raises(ValueError):
            vc.load_unique_json(path)


@pytest.fixture(scope="module")
def local():
    from tools.crowley.paginate import load_config, CONFIG, INDEX, MANIFEST
    if not all(p.is_file() for p in (CONFIG, INDEX, MANIFEST)):
        pytest.skip("local PDF configuration or index is not available")
    if not Path(load_config()["pdf_path"]).is_file():
        pytest.skip("local source PDF not mounted")
    book, _, _ = vc.load_verified()
    data = vc.load_cards()
    return data, book, vc.source_windows(book)


def test_all_local_cards_and_skeletons(local):
    data, book, windows = local
    assert vc.validate(data, book, windows) == []
    assert [c["chapter"] for c in data["chapters"]["chapters"]] == list(range(97))
    assert [(n, len(chapter(data, n)["segments"])) for n in (23, 63, 77)] == [(23, 8), (63, 11), (77, 9)]
    assert all(chapter(data, n)["status"] == "cc_approved" for n in range(16))
    assert len(data["periods"]["periods"]) == 18
    assert [p["id"] for p in data["periods"]["periods"] if p["status"] == "kico_approved"] == [f"P{i:02}" for i in range(1, 18)] + ["APPENDIX"]
    assert len(data["people"]["people"]) == 74 and len(data["values"]["values"]) == 14
    assert {"wiki-crowley", "wiki-confessions", "wiki-adams", "wiki-neuburg", "wiki-porfiriato",
            "wiki-us-entry-ww1", "wiki-orleans"} <= {x["id"] for x in data["sources"]["sources"]}
    assert data["overview"]["status"] == "kico_approved"
    assert len(data["overview"]["phases"]) == 7
    assert all(p["phase"] and all(p["position"][k] for k in ("has", "wants", "stuck"))
               and len(p["position"]["shifts"]) >= 1 for p in data["periods"]["periods"] if p["id"] != "APPENDIX")
    appendix = next(p for p in data["periods"]["periods"] if p["id"] == "APPENDIX")
    assert appendix["phase"] == "S7" and not any(appendix["position"].values())


def test_progress_exact_paragraph_cover_and_midchapter_flashback(local):
    data, book, _ = local
    for c in data["chapters"]["chapters"]:
        previous = 1
        for s in c["segments"]:
            assert s["paragraph_range"][0] == previous
            metrics = vc.segment_metrics(book, c["chapter"], *s["paragraph_range"])
            assert all(s[k] == metrics[k] for k in ("from", "to", "sg"))
            previous = s["paragraph_range"][1]
        assert previous == len(vc.chapter_layout(book, c["chapter"])[0]) + 1
        assert c["segments"][0]["from"] == 0 and c["segments"][-1]["to"] == 1
    rumor = chapter(data, 23)["segments"][-1]
    assert rumor["period"] == "P05" and rumor["flags"] == ["传闻", "口述时补述"]
    flashback = chapter(data, 3)["segments"][6]
    assert flashback["period"] == "P03" and "插叙" in flashback["flags"]
    forward = chapter(data, 3)["segments"][-1]
    assert forward["period"] == "P02" and "插叙" not in forward["flags"]


def test_approved_exceptions_are_exact_and_cannot_be_inherited(local):
    data, book, _ = local
    assert sum(len(s["migration_exceptions"]) for c in data["chapters"]["chapters"] for s in c["segments"]) == 19
    changed = deepcopy(data)
    chapter(changed, 23)["segments"][3]["summary"] += "新增一条。"
    assert any("stale exception" in e for e in vc.validate_structure(changed, book))
    changed = deepcopy(data)
    chapter(changed, 23)["status"] = "draft"
    assert any("stale exception" in e for e in vc.validate_structure(changed, book))
    changed = deepcopy(data)
    first(changed)["migration_exceptions"] = ["cast"]
    first(changed)["cast"] = []
    assert any("stale exception" in e for e in vc.validate_structure(changed, book))


def normalized_review(text):
    text = re.sub(r"【[^】]*】", "", text)
    text = text.replace("外：维基该公爵条目", "外：")
    text = re.sub(r"^\*\*[^\n]+\*\*\s*$", "", text, flags=re.M)
    return re.sub(r"[\s*|]", "", text)


def sample_prose_lines(source):
    active = False
    scene_instructions = False
    private_rules = vc.load_private_rules()
    for number, line in enumerate(source.splitlines(), 1):
        if line.startswith("## 二、"):
            active = True
            continue
        if line.startswith("## 七、"):
            break
        if line.startswith("## 三、"):
            scene_instructions = True
        if scene_instructions and line.startswith("### 第"):
            scene_instructions = False
        private_annotation = any(rule.search(tag) for tag in re.findall(r"〔[^〕]*〕", line) for rule in private_rules)
        if not active or scene_instructions or not line.strip() or line.startswith("#") or line.strip() == "---" or private_annotation:
            continue
        if line.startswith("**") or line.startswith("|---") or line.startswith("| 话题") or line.startswith("每条写四种证据"):
            continue
        line = re.sub(r"^\s*(?:- |\d+\. )", "", line)
        line = re.sub(r"^\*\*.+?\*\*：", "", line)
        line = re.sub(r"^(概括|场景栏|立场（议论部分）|立场|说话人|注意|口述时的辩护|他明说|他实际做|他事后改口|我们推断|与常规)：", "", line)
        value = normalized_review(line)
        if len(value) >= 4:
            yield number, value


def test_every_sample_prose_line_survives_migration(local):
    data, book, _ = local
    path = vc.HERE.parents[1] / "brainstorms/crowley-sample-cards.md"
    if not path.is_file():
        pytest.skip("local reviewed sample not mounted")
    lines = list(sample_prose_lines(path.read_text(encoding="utf-8")))
    assert len(lines) == 337
    output = normalized_review(vc.render(data, book))
    assert [line for line, value in lines if value not in output] == [592]
    assert dict(lines)[592] not in output
    assert sum(value in output for _, value in lines) == 336
    changed = deepcopy(data)
    chapter(changed, 23)["segments"][0]["summary"] = "一条完全不同的叙述。"
    output = normalized_review(vc.render(changed, book))
    assert any(value not in output for _, value in lines)


def test_real_source_overlap_rejected_in_memory(local):
    data, book, windows = local
    changed = deepcopy(data)
    quoted = " ".join(vc.tokens(book.paragraphs[0].text)[:8])
    changed["overview"]["summary"] = quoted
    errors = vc.validate(changed, book, windows)
    assert any("eight-word" in e for e in errors)
    assert quoted not in "\n".join(errors)


def test_render_deterministic_and_check_read_only(local):
    data, book, _ = local
    output = vc.render(data, book)
    assert vc.render(data, book) == output
    assert vc.RENDERED.read_bytes() == output.encode("utf-8")
    assert output.index("## 生涯总览") < output.index("## 时期卡") < output.index("## 分段索引")
    with patch.object(vc, "load_verified", return_value=(book, {}, "")), \
            patch.object(Path, "write_bytes") as write, redirect_stdout(io.StringIO()):
        assert vc.main(["--check"]) == 0
    write.assert_not_called()


def test_invalid_data_cannot_render(local):
    _, book, _ = local
    with patch.object(vc, "load_verified", return_value=(book, {}, "")), \
            patch.object(vc, "validate", return_value=["invalid test field"]), \
            patch.object(Path, "write_bytes") as write, redirect_stderr(io.StringIO()):
        assert vc.main(["--render"]) == 1
    write.assert_not_called()


def test_batch_zero_unchanged_except_approved_period_correction(local):
    data, _, _ = local
    restored = deepcopy(data["chapters"])
    project_legacy_statuses(restored)
    restored["chapters"] = [c for c in restored["chapters"] if c["chapter"] in {23, 63, 77}]
    rumor = next(s for c in restored["chapters"] for s in c["segments"] if s["id"] == "23-8")
    # Restore the two old metadata values solely to compare with the frozen
    # pre-review artifact. Neither old value is used in live card routing.
    rumor["period"] = "P15"
    rumor["flags"] = ["传闻", "口述时补述", "插叙"]
    encoded = (json.dumps(restored, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assert sha256(encoded).hexdigest() == "70fb51dd475c06b8e6ec071a087620b04f7d276dc635d7785071429ff74e0781"


def test_batch_one_independent_scenes_and_intact_long_quotes(local):
    data, book, _ = local
    drafts = [c for c in data["chapters"]["chapters"] if c["chapter"] < 8]
    assert sum(len(c["segments"]) for c in drafts) == 74
    assert len(chapter(data, 0)["segments"]) == 4
    assert all(s["period"] is None for s in chapter(data, 0)["segments"])
    short = []
    for c in drafts:
        for s in c["segments"]:
            assert not s["migration_exceptions"], "new drafts cannot borrow sample exceptions"
            assert f"【书 {c['chapter']}·{s['sg']}】" in s["summary"]
            words = vc.segment_metrics(book, c["chapter"], *s["paragraph_range"])["words"]
            if words < 150:
                short.append(s["id"])
                assert any(n["label"] == "短段说明" and n["text"].strip() for n in s["notes"])
            if words > 1500:
                assert any(n["label"] == "长段说明" and n["text"].strip() for n in s["notes"])
    assert len(short) == 12
    assert chapter(data, 5)["segments"][3]["paragraph_range"] == [6, 33]
    assert chapter(data, 7)["segments"][2]["paragraph_range"] == [4, 15]
    for c, i in [(5, 3), (7, 2)]:
        s = chapter(data, c)["segments"][i]
        assert "引文" in s["types"] and s["speaker"] and s["quote_note"]


def test_batch_one_unchanged_except_approved_prelude_periods(local):
    data, _, _ = local
    restored = deepcopy(data["chapters"])
    project_legacy_statuses(restored)
    restored["chapters"] = [c for c in restored["chapters"] if c["chapter"] < 8 or c["chapter"] in {23, 63, 77}]
    for s in restored["chapters"][0]["segments"]:
        s["period"] = "APPENDIX"  # Historical snapshot only; forbidden in live scenes.
    encoded = (json.dumps(restored, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assert sha256(encoded).hexdigest() == "2f648d1d64c923be4ccff3dc5e8f11927344f3bbe5ae26d1c077967352f0430b"


def test_batch_two_scene_boundaries_quotes_and_periods(local):
    data, book, _ = local
    counts = [12, 14, 11, 10, 12, 9, 11, 7]
    assert [len(chapter(data, n)["segments"]) for n in range(8, 16)] == counts
    short = []
    for n in range(8, 16):
        for s in chapter(data, n)["segments"]:
            assert s["period"] not in (None, "APPENDIX")
            assert not s["migration_exceptions"]
            assert f"【书 {n}·{s['sg']}】" in s["summary"]
            count = vc.segment_metrics(book, n, *s["paragraph_range"])["words"]
            if count < 150:
                short.append(s["id"])
                assert any(x["label"] == "短段说明" and x["text"].strip() for x in s["notes"])
            if count > 1500:
                assert any(x["label"] == "长段说明" and x["text"].strip() for x in s["notes"])
    assert len(short) == 12
    # Whole quoted poems stay inside one scene, including multi-paragraph poems.
    for n, ordinal, interval in [(8, 2, [3, 6]), (8, 8, [15, 23]),
                                  (9, 12, [15, 20]), (13, 7, [19, 27])]:
        s = chapter(data, n)["segments"][ordinal - 1]
        assert s["paragraph_range"] == interval
        assert "引文" in s["types"] and s["speaker"] and s["quote_note"]
    for n, ordinal, period in [(10, 9, "P04"), (10, 10, "P10"),
                                (11, 7, "P03"), (13, 7, "P02")]:
        s = chapter(data, n)["segments"][ordinal - 1]
        assert s["period"] == period and "插叙" in s["flags"]
    forward = chapter(data, 11)["segments"][-1]
    assert forward["period"] == "P03" and "插叙" not in forward["flags"]
    output = vc.render(data, book)
    assert output.count("时期：无生活时期，参照叙述者卡；") == 4
    assert "时期：APPENDIX；" not in output and "时期：None；" not in output


def test_batch_three_preserves_cc_reviewed_baseline(local):
    data, _, _ = local
    restored = deepcopy(data["chapters"])
    project_legacy_statuses(restored)
    restored["chapters"] = [c for c in restored["chapters"] if c["chapter"] < 16 or c["chapter"] in {23, 63, 77}]
    encoded = (json.dumps(restored, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assert sha256(encoded).hexdigest() == "8bd36dbdd7a831353ef07851b2e39ce5a65f2b14532becbdf9685ddbd650431b"
    corrected = chapter(data, 10)["segments"][8]
    assert corrected["period"] == "P04"
    assert "【外 wiki-hopkinson】" in corrected["notes"][0]["text"]
    # Sources may grow under the other card tasks; retain review evidence.
    assert "wiki-hopkinson" in {s["id"] for s in data["sources"]["sources"]}


def test_batch_three_drafts_quotes_time_jumps_and_source_quarantine(local):
    data, book, _ = local
    numbers = list(range(16, 23)) + [24]
    assert [len(chapter(data, n)["segments"]) for n in numbers] == [13, 10, 19, 15, 11, 7, 27, 8]
    short = []
    for n in numbers:
        assert chapter(data, n)["status"] == "cc_approved"
        for s in chapter(data, n)["segments"]:
            assert s["period"] not in (None, "APPENDIX")
            assert not s["migration_exceptions"]
            assert f"【书 {n}·{s['sg']}】" in s["summary"]
            count = vc.segment_metrics(book, n, *s["paragraph_range"])["words"]
            if count < 150:
                short.append(s["id"])
                assert any(x["label"] == "短段说明" and x["text"].strip() for x in s["notes"])
            if count > 1500:
                assert any(x["label"] == "长段说明" and x["text"].strip() for x in s["notes"])
    assert len(short) == 21
    # Separate quoted documents remain complete, especially the 1900 proposal.
    for n, ordinal, interval in [(19, 12, [28, 41]), (22, 8, [15, 19]),
                                  (22, 15, [30, 49]), (22, 18, [52, 60]),
                                  (22, 20, [62, 64]), (22, 22, [69, 74]),
                                  (22, 23, [74, 76])]:
        s = chapter(data, n)["segments"][ordinal - 1]
        assert s["paragraph_range"] == interval
        assert "引文" in s["types"] and s["speaker"] and s["quote_note"]
    for n, ordinal, period in [(16, 3, "P07"), (16, 5, "P04"),
                                (18, 4, "P05"), (18, 7, "P07"),
                                (19, 14, "P03"), (19, 15, "P03")]:
        s = chapter(data, n)["segments"][ordinal - 1]
        assert s["period"] == period and "插叙" in s["flags"]
    forward = chapter(data, 17)["segments"][-1]
    assert forward["period"] == "P04" and "插叙" not in forward["flags"]
    rumor = chapter(data, 18)["segments"][9]
    assert rumor["period"] == "P04" and "传闻" in rumor["flags"] and "插叙" not in rumor["flags"]
    tentative = chapter(data, 18)["segments"][13]
    assert tentative["period"] == "P04" and "插叙" not in tentative["flags"]
    assert tentative["event_time"] == "婚后某日（1903 年 8 月以后），具体时期无法确定"
    assert any(x["label"] == "时期无法确定" for x in tentative["notes"])
    contaminated = chapter(data, 21)["segments"][0]
    assert any(x["label"] == "底稿已修正" for x in contaminated["notes"])
    # The editor's later biographical material is not narrator testimony.
    assert "1947" not in contaminated["summary"]
    assert "海洛因" not in contaminated["summary"]


def test_batch_four_preserves_rework2_and_reviewed_period_correction(local):
    data, _, _ = local
    restored = deepcopy(data["chapters"])
    project_legacy_statuses(restored)
    restored["chapters"] = [c for c in restored["chapters"] if c["chapter"] < 25 or c["chapter"] in {63, 77}]
    encoded = (json.dumps(restored, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assert sha256(encoded).hexdigest() == "da1bb2ac818e321a08ea97011d189a8bd7516688e4dd396eb664af729b8d91b6"


def test_batch_four_boundaries_complete_quotes_and_time_levels(local):
    data, book, _ = local
    assert [len(chapter(data, n)["segments"]) for n in range(25, 33)] == [19, 17, 7, 8, 16, 11, 10, 10]
    short = []
    for n in range(25, 33):
        assert chapter(data, n)["status"] == "cc_approved"
        for s in chapter(data, n)["segments"]:
            assert not s["migration_exceptions"]
            assert s["period"] not in (None, "APPENDIX")
            assert f"【书 {n}·{s['sg']}】" in s["summary"]
            count = vc.segment_metrics(book, n, *s["paragraph_range"])["words"]
            if count < 150:
                short.append(s["id"])
                assert any(x["label"] == "短段说明" and x["text"].strip() for x in s["notes"])
            if count > 1500:
                assert any(x["label"] == "长段说明" and x["text"].strip() for x in s["notes"])
    assert len(short) == 16
    for n, ordinal, interval in [(26, 3, [4, 8]), (26, 5, [11, 13]),
                                  (27, 5, [16, 21]), (28, 7, [19, 27]),
                                  (29, 9, [16, 22]), (29, 11, [24, 29]),
                                  (31, 5, [15, 21]), (32, 4, [7, 12]),
                                  (32, 10, [20, 25])]:
        s = chapter(data, n)["segments"][ordinal - 1]
        assert s["paragraph_range"] == interval
        assert "引文" in s["types"] and s["speaker"] and s["quote_note"]
    for n, ordinal, period in [(25, 6, "P04"), (25, 8, "P17"), (26, 6, "P04")]:
        s = chapter(data, n)["segments"][ordinal - 1]
        assert s["period"] == period and "插叙" in s["flags"]
    rumor = chapter(data, 25)["segments"][13]
    assert rumor["period"] == "P05" and "传闻" in rumor["flags"] and "插叙" not in rumor["flags"]
    textual_issue = chapter(data, 32)["segments"][0]
    assert any(x["label"] == "底稿措辞" for x in textual_issue["notes"])


def test_batch_five_preserves_approved_through_chapter32(local):
    data, _, _ = local
    restored = deepcopy(data["chapters"])
    project_legacy_statuses(restored)
    restored["chapters"] = [c for c in restored["chapters"] if c["chapter"] < 33 or c["chapter"] in {63, 77}]
    encoded = (json.dumps(restored, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assert sha256(encoded).hexdigest() == "2e100e03092db5f3e234bac7b2d85176fad986ce9a7b7b426ea153ac6a24a00e"


def test_batch_five_complete_documents_and_uncertain_periods(local):
    data, book, _ = local
    assert [len(chapter(data, n)["segments"]) for n in range(33, 41)] == [10, 8, 10, 13, 9, 9, 10, 8]
    short, long = [], []
    for n in range(33, 41):
        assert chapter(data, n)["status"] == "cc_approved"
        for s in chapter(data, n)["segments"]:
            assert not s["migration_exceptions"]
            assert s["period"] not in (None, "APPENDIX")
            assert f"【书 {n}·{s['sg']}】" in s["summary"]
            count = vc.segment_metrics(book, n, *s["paragraph_range"])["words"]
            if count < 150:
                short.append(s["id"])
                assert any(x["label"] == "短段说明" and x["text"].strip() for x in s["notes"])
            if count > 1500:
                long.append((s["id"], count))
                assert any(x["label"] == "长段说明" and x["text"].strip() for x in s["notes"])
    assert len(short) == 15
    assert long == [("34-7", 1674)]
    for n, ordinal, interval in [(33, 4, [8, 11]), (34, 1, [1, 14]),
                                  (34, 2, [14, 17]), (34, 7, [29, 39]),
                                  (35, 5, [13, 17]), (36, 12, [21, 24]),
                                  (39, 6, [18, 22]), (40, 1, [1, 7])]:
        s = chapter(data, n)["segments"][ordinal - 1]
        assert s["paragraph_range"] == interval
        assert "引文" in s["types"] and s["speaker"] and s["quote_note"]
    for n, ordinal, period in [(34, 1, "P06"), (34, 6, "P10"), (36, 10, "P02"),
                                (36, 11, "P03"), (39, 7, "P10")]:
        s = chapter(data, n)["segments"][ordinal - 1]
        assert s["period"] == period and "插叙" in s["flags"]
    forward = chapter(data, 33)["segments"][-1]
    assert forward["period"] == "P07" and "插叙" not in forward["flags"]
    for n, ordinal, period in [(33, 5, "P06"), (36, 3, "P07")]:
        s = chapter(data, n)["segments"][ordinal - 1]
        assert s["period"] == period and "插叙" not in s["flags"]
        assert any(x["label"] == "时期无法确定" for x in s["notes"])
    for n, ordinal, label in [(35, 1, "日期差异"), (40, 4, "日期差异"),
                              (39, 3, "数值未校改"), (36, 5, "底稿措辞")]:
        assert any(x["label"] == label for x in chapter(data, n)["segments"][ordinal - 1]["notes"])


def test_batch_six_preserves_existing_cards_including_unreviewed_batch_five(local):
    data, _, _ = local
    restored = deepcopy(data["chapters"])
    project_legacy_statuses(restored)
    restored["chapters"] = [c for c in restored["chapters"] if c["chapter"] < 41 or c["chapter"] in {63, 77}]
    encoded = (json.dumps(restored, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assert sha256(encoded).hexdigest() == "43b8679eb81885522bc29d3cdbba69b4510c80fd5c26cbe1920d00b3c1fff177"


def test_batch_six_complete_quotes_and_reviewed_period_boundaries(local):
    data, book, _ = local
    assert [len(chapter(data, n)["segments"]) for n in range(41, 49)] == [5, 12, 10, 11, 8, 11, 10, 16]
    short, long = [], []
    for n in range(41, 49):
        assert chapter(data, n)["status"] == "cc_approved"
        for s in chapter(data, n)["segments"]:
            assert not s["migration_exceptions"]
            assert s["period"] not in (None, "APPENDIX")
            assert f"【书 {n}·{s['sg']}】" in s["summary"]
            count = vc.segment_metrics(book, n, *s["paragraph_range"])["words"]
            if count < 150:
                short.append(s["id"])
                assert any(x["label"] == "短段说明" and x["text"].strip() for x in s["notes"])
            if count > 1500:
                long.append(s["id"])
    assert short == ["41-5", "42-2", "42-4", "44-11", "48-1", "48-2", "48-3"]
    assert not long
    for n, ordinal, interval in [(42, 8, [18, 32]), (42, 12, [41, 46]),
                                  (43, 7, [24, 36]), (43, 10, [44, 49]),
                                  (44, 8, [29, 41]), (44, 9, [41, 59]),
                                  (45, 2, [3, 9]), (46, 6, [19, 25]),
                                  (47, 8, [27, 35]), (48, 8, [15, 19])]:
        s = chapter(data, n)["segments"][ordinal - 1]
        assert s["paragraph_range"] == interval
        assert "引文" in s["types"] and s["speaker"] and s["quote_note"]
    assert all(s["period"] == "P07" for s in chapter(data, 41)["segments"])
    assert all(s["period"] == "P08" for s in chapter(data, 42)["segments"])
    assert chapter(data, 46)["segments"][0]["period"] == "P08"
    assert all(s["period"] == "P09" and "插叙" not in s["flags"]
               for s in chapter(data, 46)["segments"][1:])
    for n in (47, 48):
        assert all(s["period"] == "P09" and "插叙" not in s["flags"]
                   for s in chapter(data, n)["segments"] if s["id"] != "48-5")
    macdonald = chapter(data, 48)["segments"][4]
    assert macdonald["period"] == "P08"
    assert set(macdonald["flags"]) == {"插叙", "传闻", "口述时补述"}
    assert macdonald["event_time"] == "1903 年 3 月，麦克唐纳自杀前一天（据书外日期推定）"
    assert macdonald["notes"] == [{
        "label": "日期（书外核对）",
        "text": "维基记麦克唐纳 1903 年 3 月 25 日在巴黎雷吉纳旅馆自杀【外 wiki-hector-macdonald】。原文说午餐后次晨读到报道，随即传出他自杀，可知午餐约在 1903 年 3 月 24 日，属 P08；原文未写年份，这是据书外日期的推定。"
    }]
    assert {"id": "wiki-hector-macdonald", "title": "维基 Hector MacDonald",
            "url": "https://en.wikipedia.org/wiki/Hector_MacDonald",
            "accessed": "2026-09-28"} in data["sources"]["sources"]
    for ordinal, period in [(1, "P04"), (2, "P14"), (3, "P05")]:
        s = chapter(data, 44)["segments"][ordinal - 1]
        assert s["period"] == period and "插叙" in s["flags"]
    for n, ordinal in [(44, 10), (48, 6)]:
        s = chapter(data, n)["segments"][ordinal - 1]
        assert "插叙" not in s["flags"]
        assert any(x["label"] == "时期无法确定" for x in s["notes"])
    assert any(x["label"] == "日期未定" for x in chapter(data, 47)["segments"][2]["notes"])


def test_batch_seven_preserves_reviewed_cards_after_authorized_date_fix(local):
    data, _, _ = local
    restored = deepcopy(data["chapters"])
    project_legacy_statuses(restored)
    restored["chapters"] = [c for c in restored["chapters"] if c["chapter"] < 49 or c["chapter"] in {63, 77}]
    encoded = (json.dumps(restored, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assert sha256(encoded).hexdigest() == "fe3f2abfd75d060afdb1bcb5b1c7c98fb4cec7a7ff89b5b16751530c46a53500"


def test_batch_seven_time_layers_complete_documents_and_external_notes(local):
    data, book, _ = local
    assert [len(chapter(data, n)["segments"]) for n in range(49, 57)] == [17, 25, 12, 27, 19, 15, 21, 23]
    short, long = [], []
    for n in range(49, 57):
        assert chapter(data, n)["status"] == "cc_approved"
        for s in chapter(data, n)["segments"]:
            assert not s["migration_exceptions"]
            assert f"【书 {n}·{s['sg']}】" in s["summary"]
            assert s["period"] == ("P09" if n == 49 else "P17" if s["id"] == "52-24" else "P10")
            assert ("插叙" in s["flags"]) == (s["id"] == "52-24")
            if n == 49 and s["paragraph_range"][0] >= 18:
                assert "口述时补述" in s["flags"]
                assert "事件" not in s["types"]
            count = vc.segment_metrics(book, n, *s["paragraph_range"])["words"]
            if count < 150:
                short.append(s["id"])
                assert any(x["label"] == "短段说明" and x["text"].strip() for x in s["notes"])
            if count > 1500:
                long.append(s["id"])
    assert len(short) == 22
    assert not long
    for n, ordinal, interval in [(49, 3, [5, 6]), (49, 10, [22, 28]),
                                  (50, 4, [12, 17]), (51, 1, [1, 3]),
                                  (52, 17, [49, 55]), (52, 20, [64, 71]),
                                  (52, 22, [74, 77]), (52, 26, [83, 89]),
                                  (53, 3, [9, 15]), (53, 11, [45, 49]),
                                  (53, 17, [78, 83]), (54, 3, [11, 14]),
                                  (54, 4, [14, 18]), (54, 13, [38, 44]),
                                  (56, 8, [18, 21])]:
        s = chapter(data, n)["segments"][ordinal - 1]
        assert s["paragraph_range"] == interval
        assert "引文" in s["types"] and s["speaker"] and s["quote_note"]
    for n, ordinal in [(51, 4), (53, 15)]:
        s = chapter(data, n)["segments"][ordinal - 1]
        assert any(x["label"] == "时期无法确定" for x in s["notes"])
    assert any(x["label"] == "日期未校改" for x in chapter(data, 52)["segments"][16]["notes"])
    for n, ordinal, text in [
        (52, 20, "维基记此次远征争执不断，其他队员认为他鲁莽；Pache 与几名挑夫遇难后，登山界普遍归咎于他【外 wiki-crowley】。卡片照录他本人的说法，并列此记载。"),
        (53, 13, "维基记他开枪打伤两名企图抢劫的人（未致命），此后被迫离开印度【外 wiki-crowley】。原文当夜他不知是否打中；后来朋友转告，他一枪打穿两人，二人送医并招认抢劫（见 53-18）；报纸登出后警方悬赏寻找开枪者，朋友劝他赶快离开（见 53-19）。书外记载与原文在伤者人数上一致。"),
    ]:
        assert {"label": "书外核对", "text": text} in chapter(data, n)["segments"][ordinal - 1]["notes"]
    # What the narrator knew during the shooting differs from the later report.
    shooting = chapter(data, 53)["segments"][12]
    later_report = chapter(data, 53)["segments"][17]
    assert shooting["paragraph_range"] == [51, 70]
    assert "不知道是否击中" in shooting["summary"]
    assert later_report["paragraph_range"] == [83, 86]
    assert "桑顿" in later_report["summary"] and "两人被击中" in later_report["summary"]


def test_batch_eight_preserves_approved_cards_after_external_note_correction(local):
    data, _, _ = local
    restored = deepcopy(data["chapters"])
    project_legacy_statuses(restored)
    restored["chapters"] = [c for c in restored["chapters"] if c["chapter"] < 57 or c["chapter"] in {63, 77}]
    encoded = (json.dumps(restored, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assert sha256(encoded).hexdigest() == "87230005311fd703690ed233776215ef4aa31b8bc5eef54e219f1348c466cf73"


def test_batch_eight_nested_quotes_periods_and_source_anomalies(local):
    data, book, _ = local
    numbers = [57, 58, 59, 60, 61, 62, 64, 65]
    assert [len(chapter(data, n)["segments"]) for n in numbers] == [16, 20, 13, 21, 19, 11, 19, 20]
    short, long = [], []
    for n in numbers:
        assert chapter(data, n)["status"] == "cc_approved"
        for s in chapter(data, n)["segments"]:
            assert not s["migration_exceptions"]
            assert f"【书 {n}·{s['sg']}】" in s["summary"]
            count = vc.segment_metrics(book, n, *s["paragraph_range"])["words"]
            if count < 150:
                short.append(s["id"])
                assert any(x["label"] == "短段说明" and x["text"].strip() for x in s["notes"])
            if count > 1500:
                long.append(s["id"])
    assert len(short) == 19
    assert not long
    for i, s in enumerate(chapter(data, 57)["segments"], 1):
        assert s["period"] == ("P10" if i <= 12 else "P11")
        assert "插叙" not in s["flags"]
    for i, s in enumerate(chapter(data, 58)["segments"], 1):
        assert s["period"] == ("P12" if i == 12 else "P10")
        assert "插叙" in s["flags"]
    for i, s in enumerate(chapter(data, 59)["segments"], 1):
        assert s["period"] == ("P10" if i <= 6 else "P11")
        assert ("插叙" in s["flags"]) == (i <= 6)
    for n, base, exceptions in [
        (60, "P11", {3: "P12", 4: "P12", 5: "P10", 7: "P14", 9: "P12", 14: "P12", 15: "P12"}),
        (61, "P11", {19: "P12"}),
        (62, "P11", {5: "P13", 7: "P12"}),
        (64, "P12", {10: "P10", 14: "P13", 16: "P15"}),
        (65, "P12", {4: "P13", 9: "P16", 10: "P16"}),
    ]:
        for i, s in enumerate(chapter(data, n)["segments"], 1):
            assert s["period"] == exceptions.get(i, base)
            assert ("插叙" in s["flags"]) == (i in exceptions)
    for n, ordinal, interval in [
        (57, 8, [18, 22]), (58, 1, [1, 6]), (58, 6, [23, 26]),
        (58, 8, [29, 37]), (58, 9, [37, 41]), (58, 14, [49, 55]),
        (58, 15, [55, 59]), (58, 16, [59, 70]), (58, 19, [78, 81]),
        (58, 20, [81, 85]), (59, 2, [3, 11]), (59, 4, [14, 19]),
        (59, 8, [32, 43]), (59, 9, [43, 49]), (59, 11, [50, 58]),
        (60, 4, [7, 9]), (60, 6, [10, 11]), (60, 20, [46, 50]),
        (61, 9, [29, 36]), (61, 14, [45, 47]), (62, 3, [5, 14]),
        (62, 7, [21, 26]), (62, 11, [35, 43]), (64, 16, [36, 42]),
        (65, 9, [18, 25]), (65, 10, [25, 37]), (65, 16, [54, 62]),
        (65, 19, [68, 70]),
    ]:
        s = chapter(data, n)["segments"][ordinal - 1]
        assert s["paragraph_range"] == interval
        assert "引文" in s["types"] and s["speaker"] and s["quote_note"]
    for ordinal in (8, 11):
        assert "富勒" in chapter(data, 59)["segments"][ordinal - 1]["speaker"]
    for n, ordinal, label in [
        (58, 5, "底稿措辞"), (59, 4, "日期未定"), (60, 9, "书内日期差异"),
        (60, 18, "时期无法确定"), (61, 9, "底稿地名"), (61, 17, "时期无法确定"),
        (62, 7, "书内时间对照"), (64, 11, "时期无法确定"),
        (64, 12, "时期无法确定"), (64, 19, "日期衔接未校改"),
        (65, 5, "行程衔接未明"), (65, 19, "书内日期并列"),
    ]:
        assert any(x["label"] == label for x in chapter(data, n)["segments"][ordinal - 1]["notes"])
    essays = chapter(data, 60)["segments"][8]
    assert essays["paragraph_range"] == [17, 19]
    assert essays["period"] == "P12"
    prose = chapter(data, 60)["segments"][7]
    assert prose["paragraph_range"] == [12, 17]
    assert prose["period"] == "P11" and not prose["notes"]
    for ordinal in (9, 10):
        s = chapter(data, 65)["segments"][ordinal - 1]
        assert any(x["label"] == "卦画字符" and "Ü" in x["text"] for x in s["notes"])
    typo = chapter(data, 65)["segments"][9]
    assert "February 1979" in typo["event_time"]
    assert any(x["label"] == "原文误植" and "February 1979" in x["text"] for x in typo["notes"])
    assert any(x["label"] == "引文边界" for x in typo["notes"])


def test_continuous_batches_preserve_all_previously_approved_chapters(local):
    data, _, _ = local
    restored = deepcopy(data["chapters"])
    project_legacy_statuses(restored)
    restored["chapters"] = [c for c in restored["chapters"] if c["chapter"] <= 65 or c["chapter"] == 77]
    encoded = (json.dumps(restored, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    assert sha256(encoded).hexdigest() == "3bc8bce54a770d64697816666081c24f815427fa2c39d3e3767357526ca45c16"


def test_batch_nine_dates_quotes_and_conservative_uncertainties(local):
    data, book, _ = local
    assert [len(chapter(data, n)["segments"]) for n in range(66, 74)] == [15, 30, 19, 10, 13, 10, 20, 10]
    for n in range(66, 74):
        for s in chapter(data, n)["segments"]:
            assert not s["migration_exceptions"]
            assert f"【书 {n}·{s['sg']}】" in s["summary"]
            count = vc.segment_metrics(book, n, *s["paragraph_range"])["words"]
            if count < 150:
                assert any(x["label"] == "短段说明" for x in s["notes"])
            assert count <= 1500
            if any(x["label"] == "时期无法确定" for x in s["notes"]):
                assert "插叙" not in s["flags"]
    assert "待 CC 裁定" not in json.dumps(data, ensure_ascii=False)
    for n, ordinal, period in [(67, 10, "P17"), (67, 26, "P01"), (67, 29, "P14"),
                               (70, 12, "P15"), (72, 1, "P05"), (72, 3, "P13"), (72, 16, "P17")]:
        s = chapter(data, n)["segments"][ordinal - 1]
        assert s["period"] == period and "插叙" in s["flags"]
    for n, ordinal, interval in [(69, 2, [4, 13]), (69, 6, [25, 44]),
                                 (71, 2, [7, 9]), (72, 5, [30, 45]), (72, 11, [70, 75])]:
        s = chapter(data, n)["segments"][ordinal - 1]
        assert s["paragraph_range"] == interval
        assert "引文" in s["types"] and s["speaker"] and s["quote_note"]
    assert any(x["label"] == "书外核对" and x["text"] ==
               "维基记 Jones 于 1911 年起诉《窥镜报》诽谤，败诉【外 wiki-george-cecil-jones】。原文未写开庭年份，只提到被引为证据的一句话出自 1910 年春。"
               for x in chapter(data, 67)["segments"][22]["notes"])
    assert [s for s in data["sources"]["sources"] if s["id"] == "wiki-george-cecil-jones"] == [
        {"id": "wiki-george-cecil-jones", "title": "维基 George Cecil Jones",
         "url": "https://en.wikipedia.org/wiki/George_Cecil_Jones", "accessed": "2026-09-28"}]


def test_batch_ten_full_documents_and_period_transitions(local):
    data, book, _ = local
    numbers = [74, 75, 76, 78, 79, 80, 81, 82]
    assert [len(chapter(data, n)["segments"]) for n in numbers] == [8, 13, 29, 10, 8, 8, 13, 8]
    short = []
    for n in numbers:
        for s in chapter(data, n)["segments"]:
            assert not s["migration_exceptions"]
            assert f"【书 {n}·{s['sg']}】" in s["summary"]
            count = vc.segment_metrics(book, n, *s["paragraph_range"])["words"]
            assert count <= 1500
            if count < 150:
                short.append(s["id"])
                assert any(x["label"] == "短段说明" for x in s["notes"])
            if any(x["label"] == "时期无法确定" for x in s["notes"]):
                assert "插叙" not in s["flags"]
    assert "待 CC 裁定" not in json.dumps(data, ensure_ascii=False)
    assert len(short) == 10
    for i, s in enumerate(chapter(data, 75)["segments"], 1):
        assert s["period"] == ("P14" if i == 10 else "P15")
        assert ("插叙" in s["flags"]) == (i == 10)
    for i in (4, 5, 6):
        s = chapter(data, 76)["segments"][i - 1]
        assert s["period"] == "P14" and "插叙" in s["flags"]
    for s in chapter(data, 76)["segments"][3:]:
        assert "引文" in s["types"]
        assert "1920 年 3 月" in s["speaker"] or "1922 年新年" in s["speaker"]
    assert chapter(data, 76)["segments"][-1]["paragraph_range"] == [107, 112]
    assert chapter(data, 76)["segments"][-1]["period"] == "P17"
    returned = chapter(data, 80)["segments"][-1]
    assert returned["paragraph_range"] == [17, 18]
    assert returned["period"] == "P16" and "插叙" not in returned["flags"]
    for n, ordinal, interval in [(74, 4, [9, 20]), (81, 7, [24, 25]), (82, 8, [29, 39])]:
        s = chapter(data, n)["segments"][ordinal - 1]
        assert s["paragraph_range"] == interval
        assert "引文" in s["types"] and s["speaker"] and s["quote_note"]
    assert "不记作杀人" in chapter(data, 82)["segments"][0]["stance"]
    assert "动物被杀" in chapter(data, 82)["segments"][4]["stance"]


def test_batch_eleven_keeps_time_layers_and_required_external_checks(local):
    data, book, _ = local
    assert [len(chapter(data, n)["segments"]) for n in range(83, 91)] == [9, 11, 10, 13, 8, 9, 14, 11]
    short = []
    for n in range(83, 91):
        for s in chapter(data, n)["segments"]:
            assert not s["migration_exceptions"]
            assert f"【书 {n}·{s['sg']}】" in s["summary"]
            count = vc.segment_metrics(book, n, *s["paragraph_range"])["words"]
            assert count <= 1500
            if count < 150:
                short.append(s["id"])
                assert any(x["label"] == "短段说明" for x in s["notes"])
            if any(x["label"] == "时期无法确定" for x in s["notes"]):
                assert "插叙" not in s["flags"]
    assert "待 CC 裁定" not in json.dumps(data, ensure_ascii=False)
    assert len(short) == 10
    for s in chapter(data, 87)["segments"]:
        assert s["period"] == "P15" and s["types"] == ["议论"]
        assert "口述时补述" in s["flags"] and "插叙" not in s["flags"]
    for i, s in enumerate(chapter(data, 89)["segments"], 1):
        assert s["period"] == ("P16" if i in {1, 2, 4, 6} else "P17")
        assert ("插叙" in s["flags"]) == (i in {3, 5})
    assert chapter(data, 89)["segments"][6]["paragraph_range"] == [12, 14]
    for n, i, text in [
        (88, 1, "维基记他回伦敦时哮喘发作，医生开了海洛因，他很快成瘾；本书这几章几乎不提自己的毒瘾【外 wiki-crowley】。"),
        (89, 8, "维基记在修道院期间，海洛因毒瘾主导了他的生活，可卡因也损伤了他的鼻腔；原文几乎不提【外 wiki-crowley】。"),
    ]:
        assert any(x["label"] == "书外核对" and x["text"] == text
                   for x in chapter(data, n)["segments"][i - 1]["notes"])
    for n, i, label in [(84, 7, "书内日期差异"), (85, 5, "日期疑点"), (85, 7, "时期无法确定"),
                        (88, 8, "时期说明"), (88, 9, "时期无法确定"), (89, 2, "原文误植")]:
        assert any(x["label"] == label for x in chapter(data, n)["segments"][i - 1]["notes"])
    diary = chapter(data, 86)["segments"][3]
    assert diary["paragraph_range"] == [8, 12] and "引文" in diary["types"]
    assert "1918 年 9 月 5 日" in diary["speaker"]
    assert chapter(data, 90)["segments"][4]["period"] == "P15"
    assert "插叙" in chapter(data, 90)["segments"][4]["flags"]


def test_batch_twelve_complete_book_distinct_bettys_and_required_notes(local):
    data, book, _ = local
    assert [len(chapter(data, n)["segments"]) for n in range(91, 97)] == [10, 18, 13, 9, 5, 14]
    short = []
    for n in range(91, 97):
        for s in chapter(data, n)["segments"]:
            assert not s["migration_exceptions"]
            assert f"【书 {n}·{s['sg']}】" in s["summary"]
            count = vc.segment_metrics(book, n, *s["paragraph_range"])["words"]
            assert count <= 1500
            if count < 150:
                short.append(s["id"])
                assert any(x["label"] == "短段说明" for x in s["notes"])
            if any(x["label"] == "时期无法确定" for x in s["notes"]):
                assert "插叙" not in s["flags"]
    assert "待 CC 裁定" not in json.dumps(data, ensure_ascii=False)
    assert len(short) == 10
    for n, i, text in [
        (91, 8, "维基记这次去巴黎是想戒海洛因，没有成功【外 wiki-crowley】。原文没有交代此行目的。"),
        (92, 17, "维基记这部小说出版后评价不一，《星期日快报》呼吁焚毁，并动用影响力阻止再版【外 wiki-crowley】。"),
    ]:
        assert any(x["label"] == "书外核对" and x["text"] == text
                   for x in chapter(data, n)["segments"][i - 1]["notes"])
    expected = ("维基记 Loveday 喝了被污染的溪水后感染致死；遗孀 Betty May 回伦敦向报纸讲述修道院，说丈夫被逼喝过献祭猫的血；"
                "小报《约翰牛》称他为世上最邪恶的人，他认为许多指控是诽谤却付不起诉讼费；1923 年 4 月墨索里尼政府下令驱逐他"
                "【外 wiki-crowley】。卡片照录他本人的说法，并列此记载。")
    for i in (6, 8, 9):
        assert any(x["label"] == "书外核对" and x["text"] == expected
                   for x in chapter(data, 96)["segments"][i - 1]["notes"])
    london_betty = chapter(data, 93)["segments"][3]["cast"][1]
    assert "Betty Dartnell" in london_betty["name"] and "继母" in london_betty["state"]
    loveday_betty = chapter(data, 94)["segments"][0]["cast"][2]
    assert "Betty May" in loveday_betty["name"] and "洛夫迪之妻" in loveday_betty["state"]
    for s in chapter(data, 96)["segments"]:
        assert s["period"] == "P17" and "口述时补述" in s["flags"]
        assert "插叙" not in s["flags"]
    assert chapter(data, 96)["segments"][9]["paragraph_range"] == [30, 34]
    assert "1923 年 5 月 2 日" in chapter(data, 96)["segments"][9]["event_time"]
    assert "8 月" in chapter(data, 96)["segments"][9]["event_time"]
    for n, i, interval in [(92, 4, [9, 10]), (93, 1, [1, 4]), (93, 5, [7, 10])]:
        s = chapter(data, n)["segments"][i - 1]
        assert s["paragraph_range"] == interval and "插叙" in s["flags"]
    for i, interval in [(10, [17, 19]), (11, [19, 25]), (12, [25, 32]), (13, [32, 34])]:
        assert chapter(data, 93)["segments"][i - 1]["paragraph_range"] == interval
    telegram = chapter(data, 96)["segments"][0]
    assert telegram["paragraph_range"] == [1, 7]
    assert "引文" in telegram["types"] and "哈蒙德" in telegram["speaker"]


def test_rework_batches_nine_to_twelve_exact_cc_rulings(local):
    data, _, _ = local
    segments = {s["id"]: s for c in data["chapters"]["chapters"] for s in c["segments"]}
    assert "待 CC 裁定" not in json.dumps(data, ensure_ascii=False)
    expected = {"68-17", "70-10", "72-2", "72-4", "72-13", "72-17",
                "75-13", "76-8", "76-28", "85-7", "88-9", "92-5"}
    actual = {s["id"] for c in data["chapters"]["chapters"] if 66 <= c["chapter"] <= 96
              for s in c["segments"] if any(n["label"] == "时期无法确定" for n in s["notes"])}
    assert actual == expected
    assert all("插叙" not in segments[sid]["flags"] for sid in actual)
    for sid in ("67-27", "67-30"):
        assert segments[sid]["period"] == "P14"
        assert segments[sid]["flags"] == ["插叙", "自评", "口述时补述"]
    assert segments["76-24"]["period"] == "P16"
    assert segments["76-24"]["flags"] == ["传闻", "口述时补述"]
    assert segments["92-3"]["period"] == "P13"
    assert segments["92-3"]["flags"] == ["插叙", "口述时补述", "传闻"]
    assert any(n["label"] == "日期依据" and "【外 wiki-crowley】" in n["text"]
               for n in segments["67-27"]["notes"])
    assert segments["67-27"]["event_time"] == "1913 年 11 月 5 日（据宣言所署星期推定）"
    assert segments["76-24"]["stance"] == "记者付款故事被用作暗示，不能据此确认敲诈事实。"
    assert segments["93-11"]["notes"] == []


def test_rework_preserves_all_unlisted_fields_summaries_and_boundaries(local):
    data, _, _ = local
    allowed = json.loads(r'''{"67-27":["period","flags","event_time","notes"],"67-30":["period","flags","event_time","notes"],"76-24":["period","event_time","stance"],"92-3":["period","flags","event_time","notes"],"66-3":["notes","place"],"67-14":["notes","event_time"],"67-22":["notes"],"68-12":["notes"],"70-8":["notes"],"72-7":["notes"],"72-15":["notes","event_time"],"88-8":["notes"],"68-17":["notes"],"70-10":["notes"],"72-2":["notes"],"72-4":["notes"],"72-13":["notes"],"72-17":["notes"],"75-13":["notes"],"76-8":["notes"],"76-28":["notes"],"85-7":["notes"],"88-9":["notes"],"92-5":["notes"],"66-15":["notes"],"84-7":["notes"],"85-5":["notes"],"89-2":["notes"],"91-2":["notes"],"92-10":["notes"],"93-9":["notes"],"93-13":["notes"],"96-1":["notes"],"96-9":["notes"],"96-10":["notes"],"93-4":["notes"],"93-11":["notes"]}''')
    masked = deepcopy(data["chapters"])
    project_legacy_statuses(masked)
    for c in masked["chapters"]:
        for s in c["segments"]:
            for key in allowed.get(s["id"], []):
                s.pop(key)
    def digest(value):
        return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode("utf-8")).hexdigest()
    assert digest(masked) == "dd4d8a9612f4f99ac8a7bfca0c0f79da55a97e0de03106c2b61c88ca2b030f51"
    boundaries = [(c["chapter"], s["id"], *[s[k] for k in ("summary", "paragraph_range", "from", "to", "sg")])
                  for c in data["chapters"]["chapters"] for s in c["segments"]]
    assert digest(boundaries) == "8e234ee853dbda312c72815bc67eca3f2eefd7eca6c4886d80fdbcc48d251438"
    assert len(data["chapters"]["chapters"]) == 97
    assert len(boundaries) == 1232


def test_rework_report_contains_four_completed_rulings_and_no_pending_notes(local):
    data, book, _ = local
    rendered = vc.render(data, book)
    assert "待 CC 裁定" not in rendered
    for batch in (9, 10, 11, 12):
        assert f"第 {batch} 批连续交付记录及审核结论" in rendered
        assert f"第 {batch} 批连续交付记录（待 CC 审）" not in rendered
    assert rendered.count("CC 结论：REWORK 一轮，已按 CC 裁定修改") == 4



def project_legacy_statuses(chapters):
    """Check current approvals, then restore only authorized status metadata for old hashes."""
    for c in chapters["chapters"]:
        assert c["status"] == ("kico_approved" if c["chapter"] in {23, 63, 77} else "cc_approved")
        if c["chapter"] not in {23, 63, 77}:
            c["status"] = "draft"


def test_t012_overview_and_periods_complete_and_bidirectional(local):
    data, _, _ = local
    overview = data["overview"]
    periods = {p["id"]: p for p in data["periods"]["periods"]}
    assert [p["id"] for p in overview["phases"]] == [f"S{i}" for i in range(1, 8)]
    for phase in overview["phases"]:
        assert all(phase[k] for k in ("title", "years", "periods", "note"))
        assert len(phase["achievements"]) == len(phase["difficulties"]) == 1
        assert phase["achievements"][0]["why"]
        for row in phase["achievements"] + phase["difficulties"]:
            assert row["text"] and row["when"] == phase["years"]
        assert set(phase["periods"]) == {pid for pid, p in periods.items() if p["phase"] == phase["id"]}
    lines = overview["threads"]["lines"]
    assert [line["id"] for line in lines] == ["decline", "fame", "works"]
    assert [len(line["items"]) for line in lines] == [4, 8, 7]
    assert overview["threads"]["note"] and all(line["summary"] for line in lines)
    assert all(not item["later"] for line in lines[:2] for item in line["items"])
    assert all(item["later"] != "—" for item in lines[2]["items"])
    assert all(p["status"] == "kico_approved" for p in periods.values())
    assert any(n["label"] == "偏见提示" and "Cypris" in n["text"] for n in periods["P16"]["notes"])
    assert any(n["name"] == "George Cecil Jones 与 Eckenstein" for n in periods["P16"]["people_important"])
    assert len(periods["APPENDIX"]["dated_entries"]) == 20
    assert all(not p["dated_entries"] for pid, p in periods.items() if pid not in {"APPENDIX", "P05", "P12", "P15"})


@pytest.mark.parametrize("path,allowed", [
    (("overview", "summary"), True),
    (("overview", "phases", 0, "achievements", 0, "why"), True),
    (("overview", "threads", "lines", 0, "items", 0, "later"), True),
    (("periods", "periods", 0, "position", "has"), True),
    (("periods", "periods", 0, "position", "shifts", 0, "when"), True),
    (("periods", "periods", 0, "position", "shifts", 0, "text"), True),
    (("periods", "periods", 0, "money"), False),
    (("periods", "periods", 0, "notes", 0, "text"), False),
    (("people", "people", 0, "fields", "身份"), False),
    (("narrator", "fields", "谁在说"), False),
    (("chapters", "chapters", 0, "segments", 0, "summary"), False),
    (("periods", "periods", 0, "position_extra", "has"), False),
    (("other", "overview", "summary"), False),
])
def test_t012_chapter_only_citations_have_exact_scope(path, allowed):
    value = "【书 0】【书 1–2】【书 0–96】"
    for key in reversed(path):
        value = [value] if isinstance(key, int) else {key: value}
    errors = vc.content_errors(value, {i: (1, 999) for i in range(97)}, set(), set())
    assert (errors == []) is allowed
    if not allowed:
        assert all("malformed citation" in e for e in errors)


@pytest.mark.parametrize("citation", ["【书 -1】", "【书 97】", "【书 0–97】", "【书 2–1】", "【书 96–0】"])
def test_t012_chapter_only_citation_limits(citation):
    assert vc.content_errors({"overview": {"summary": citation}}, {i: (1, 999) for i in range(97)}, set(), set())


def test_t012_chapter_only_citation_requires_every_chapter_to_exist():
    assert vc.content_errors({"overview": {"summary": "【书 0–2】"}}, {0: (1, 9), 2: (10, 19)}, set(), set())


def test_t012_phase_links_reject_either_missing_direction(miniature):
    data, book = miniature
    data["overview"]["phases"][0]["periods"] = ["P01"]
    assert any("does not point back" in e for e in vc.validate_structure(data, book))
    data["periods"]["periods"][0]["phase"] = "S1"
    assert vc.validate_structure(data, book) == []
    data["overview"]["phases"][0]["periods"] = []
    assert any("does not point back" in e for e in vc.validate_structure(data, book))


@pytest.mark.parametrize("mutation", [
    lambda t: t.update(note=1),
    lambda t: t["lines"].pop(),
    lambda t: t["lines"][1].update(id="decline"),
    lambda t: t["lines"][0].pop("summary"),
    lambda t: t["lines"][0].update(title=[]),
    lambda t: t["lines"][0].update(items=[dict(label="", text="", later="")]),
    lambda t: t["lines"][0].update(items=[dict(label="标题", text="")]),
    lambda t: t["lines"][0].update(items=[dict(label="标题", text=1, later="")]),
])
def test_t012_thread_schema_counterexamples(miniature, mutation):
    data, book = miniature
    mutation(data["overview"]["threads"])
    assert vc.validate_structure(data, book)


@pytest.mark.parametrize("field,value", [("kind", "other"), ("names_in_cards", "姓名"), ("names_in_cards", [""]), ("names_in_cards", [None])])
def test_t012_people_new_fields_reject_invalid_values(miniature, field, value):
    data, book = miniature
    person = dict(id="person-01", name="旅人", period="P01", years="1900", status="draft",
                  names_in_text=["Traveler"], names_in_cards=["旅人"], kind="full",
                  fields={k: "旅人。" for k in ("身份", "此时对他重不重要", "此刻的状态", "这段关系此刻在做什么")})
    data["people"]["people"] = [person]
    assert vc.validate_structure(data, book) == []
    person[field] = value
    assert vc.validate_structure(data, book)


def test_t012_people_counts_approvals_and_alias_inheritance(local):
    data, _, _ = local
    people = data["people"]["people"]
    assert len(people) == 74
    assert [p["id"] for p in people] == [f"person-{i:02}" for i in range(1, 75)]
    assert all(p["period"] in {f"P{i:02}" for i in range(1, 18)} for p in people)
    assert all(p["status"] == "kico_approved" and p["kind"] == "full" for p in people[:5])
    assert [p["names_in_cards"] for p in people[:5]] == [["埃肯施泰因"], ["萝丝", "罗斯"], ["纽伯格"], ["多萝西"], ["亚当斯"]]
    assert all(p["status"] == "cc_approved" for p in people[5:])
    assert [sum(p["kind"] == kind for p in people[5:]) for kind in ("full", "short")] == [30, 39]
    assert all(p["names_in_cards"] for p in people[5:])
    by_id = {p["id"]: p for p in people}
    for pid in ("person-24", "person-25"):
        for field in ("names_in_text", "names_in_cards"):
            assert by_id[pid][field] == by_id["person-16"][field]
    assert by_id["person-43"]["names_in_cards"] == by_id["person-17"]["names_in_cards"] + ["塞西尔·琼斯"]
    assert by_id["person-56"]["names_in_text"] == by_id["person-49"]["names_in_text"] + ["Frater L. T."]
    for pid in ("person-63", "person-64", "person-68"):
        assert by_id[pid]["names_in_text"] == ["the Ape of Thoth"] + (["Alostrael"] if pid == "person-68" else [])


def test_t012_narrator_values_sources_and_removed_review_markers(local):
    data, _, _ = local
    assert list(data["narrator"]["fields"]) == ["谁在说", "人称的变化", "口述时的处境（书内）", "口述时的处境（书外）",
        "说话时间的层次", "他自己承认的记录缺口", "他几乎不写的事", "后几章的写作处境", "他说自己怎么写", "叙述框架"]
    assert len(data["narrator"]["按话题的可信度"]) == 13
    values = data["values"]["values"]
    assert [v["id"] for v in values] == [f"V{i:02}" for i in range(1, 15)]
    assert all(v["status"] == "kico_approved" for v in values)
    assert [v["title"] for v in values[9:]] == ["暴力与支配", "药物", "宗教", "忠诚与翻脸", "先知的自我定位"]
    assert len(data["sources"]["sources"]) == 12
    assert [s["id"] for s in data["sources"]["sources"][-2:]] == ["herald-1934", "nyt-1929"]
    serialized = json.dumps(data, ensure_ascii=False)
    for marker in ("〔新增〕", "【维基】", "【维基·忏悔录】", "【报】", "【纽约时报】", "【外·"):
        assert marker not in serialized


def test_t012_segments_fingerprint_and_all_chapter_approvals(local):
    data, _, _ = local
    chapters = data["chapters"]["chapters"]
    assert sum(c["status"] == "cc_approved" for c in chapters) == 94
    assert [c["chapter"] for c in chapters if c["status"] == "kico_approved"] == [23, 63, 77]
    segments = [{"chapter": c["chapter"], "segments": c["segments"]} for c in chapters]
    digest = sha256(json.dumps(segments, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert digest == "786f00683f88e8f4533a0efe640adec97146e2325d4c3688d11a79c2b1fdee85"


def test_t012_render_order_statuses_and_appendix(local):
    data, book, _ = local
    rendered = vc.render(data, book)
    assert rendered.count("以下各栏是证据，供核对和追问；解读时先读上面的位置说明。") == 17
    assert "三条贯穿一生的线" in rendered and "后来的地位：" in rendered
    assert "场景栏写法：" in rendered and "短卡" in rendered
    assert "位置说明待撰写。" not in rendered
    assert "待审草稿" not in rendered and "骨架待填写" not in rendered
    assert "kico_approved".partition("_")[0].capitalize() + " 已确认" in rendered and "CC 已审" in rendered
    assert "生涯总览只预留" not in rendered


def test_t012_existing_samples_and_sources_preserved(local):
    data, _, _ = local
    def digest(value):
        return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    periods = deepcopy([p for p in data["periods"]["periods"] if p["id"] in {"P05", "P12", "P15"}])
    for p in periods:
        p.pop("phase")
        p.pop("position")
        if p["id"] == "P05":
            assert p["notes"].pop() == {"label": "位置说明只写到", "text": "1901 年 4 月离开墨西哥"}
    assert digest(periods) == "bebe3636a12353e10f0bcf60d0c4c965fa4d0afd6080edec0e7843001de57c8f"
    people = deepcopy(data["people"]["people"][:5])
    assert [p["names_in_cards"] for p in people] == [["埃肯施泰因"], ["萝丝", "罗斯"], ["纽伯格"], ["多萝西"], ["亚当斯"]]
    for p in people:
        p.pop("names_in_cards")
        assert p.pop("kind") == "full"
    assert digest(people) == "e550937dd03b50c9c306a457c09e3711f73605ca6a4b3ac90d50f8e797e99440"
    assert digest(data["sources"]["sources"][:10]) == "9d21a33776ecf1856dbfb3ad3ec2952a666c93af140e43f83f046d3777607e18"


def test_t012_rework_shift_times_are_dates_not_durations(local):
    data, _, _ = local
    periods = {p["id"]: p for p in data["periods"]["periods"]}
    assert periods["P05"]["position"]["shifts"][2]["when"] == ""
    assert periods["P13"]["position"]["shifts"][4]["when"] == "1910 年冬天"
    duration = re.compile(r"(?:\d+(?:\.\d+)?|[零一二三四五六七八九十百千两几数半]+)\s*(?:个\s*)?(?:小时|分钟|秒钟|秒|天|日|周|星期|月|年)")
    for text in ("67 小时", "3 天", "两周", "几个月"):
        assert duration.fullmatch(text)
    for text in ("冬天", "春天", "八岁左右", "父亲死后的一两年里", "1910 年冬天"):
        assert not duration.fullmatch(text)
    for p in periods.values():
        for shift in p["position"]["shifts"]:
            when = shift["when"]
            # A four-digit calendar year is a date, even when it has no month.
            if not re.fullmatch(r"\d{4}\s*年", when):
                assert not duration.fullmatch(when), (p["id"], when)
            assert not re.search(r"年\s+[\u4e00-\u9fff]", when), (p["id"], when)


def test_t012_rework_p04_shared_citations_cover_every_person(local):
    data, _, _ = local
    p04 = next(p for p in data["periods"]["periods"] if p["id"] == "P04")
    refs = "【书 19·163–166】【书 20·172–173】【书 20·178–179】【书 21·180–184】【书 22·188–189】"
    assert len(p04["people_important"]) == 5
    for person in p04["people_important"]:
        assert person["note"].endswith("\n" + refs)
        assert person["note"].count(refs) == 1


def test_t012_rework_position_render_matches_reviewed_labels(local):
    data, book, _ = local
    rendered = vc.render(data, book)
    assert vc.RENDERED.read_bytes() == rendered.encode("utf-8")
    for label in ("进来时已有", "这一段最想推进", "主要卡在", "这一段里的变化"):
        assert rendered.count(f"**{label}**") == 17
    for old in ("已有积累", "想推进什么", "主要困难", "位置变化（", "**位置说明只写到**"):
        assert old not in rendered
    periods_section = rendered.split("## 时期卡\n", 1)[1].split("## 分段索引与场景栏", 1)[0]
    blocks = re.split(r"(?m)^### ", periods_section)[1:]
    assert len(blocks) == 18
    for p, block in zip(data["periods"]["periods"], blocks):
        assert block.startswith(p["id"] + " · ")
        if p["id"] == "APPENDIX":
            assert "**位置说明**" not in block
            continue
        cutoff = next((n["text"] for n in p["notes"] if n["label"] == "位置说明只写到"), None)
        if cutoff is None:
            assert p["id"] in {"P12", "P15"}
            assert "**位置说明**\n" in block
        else:
            assert block.count(f"**位置说明**（只写到 {cutoff}）") == 1
        shift_list = "\n".join("- " + s["text"] for s in p["position"]["shifts"])
        assert "**这一段里的变化**\n\n" + shift_list + "\n\n以下各栏是证据" in block


def test_t006a_every_person_occurs_in_own_period_by_whole_alias(local):
    from tools.crowley.lookup import split_cast_name
    data, _, _ = local
    people = data["people"]["people"]
    seen, scenes_with_people = set(), 0
    for chapter in data["chapters"]["chapters"]:
        for segment in chapter["segments"]:
            names = {name for cast in segment["cast"] for name in split_cast_name(cast["name"])}
            matches = {p["id"] for p in people if p["period"] == segment["period"] and names.intersection(p["names_in_cards"])}
            seen.update(matches)
            scenes_with_people += bool(matches)
    assert seen == {p["id"] for p in people}
    assert len(seen) == 74 and scenes_with_people == 378
    assert "医生" not in next(p for p in people if p["id"] == "person-38")["names_in_cards"]


def test_private_rules_missing_uses_only_public_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(vc, "PRIVATE_TERMS", tmp_path / "absent.json")
    assert vc.load_private_rules() == []
    assert vc.content_errors({"text": "SecretName 机密名"}, {}, set(), set()) == []
    for value in ("Q:" + chr(92) + "folder", "/" + "users/example", chr(92) * 2 + "server" + chr(92) + "folder"):
        assert any("forbidden" in e for e in vc.content_errors({"text": value}, {}, set(), set()))


@pytest.mark.parametrize("raw", [
    "{broken", "[]", "{}", '{"patterns":"secretname"}', '{"patterns":[1]}',
    '{"patterns":[""]}', '{"patterns":["["]}', '{"patterns":[],"unknown":true}',
    '{"patterns":[],"note":1}', '{"patterns":[],"patterns":[]}',
])
def test_private_rules_invalid_file_fails_closed(raw, tmp_path, monkeypatch):
    rules = tmp_path / "private_terms.json"
    rules.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(vc, "PRIVATE_TERMS", rules)
    with pytest.raises(ValueError):
        vc.content_errors({"text": "ordinary"}, {}, set(), set())


def test_real_local_private_rules_are_loaded_when_present():
    if not vc.PRIVATE_TERMS.is_file():
        pytest.skip("local private rules not installed")
    expected = json.loads(vc.PRIVATE_TERMS.read_text(encoding="utf-8"))["patterns"]
    rules = vc.load_private_rules()
    assert rules and [rule.pattern for rule in rules] == expected
    assert all(rule.flags & re.I for rule in rules)
