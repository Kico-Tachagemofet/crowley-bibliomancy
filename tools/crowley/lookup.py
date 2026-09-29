"""Read-only background lookup from public progress anchors; no source text or draws.

Only index, manifest and card JSON files are read. No PDF, local paragraph
anchors, random numbers, receipts or caches are needed or written.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path
import re
import sys


HERE = Path(__file__).resolve().parent
INDEX = HERE / "unit_index.json"
MANIFEST = HERE / "manifest.json"
CARDS = HERE / "cards"
CARD_FILES = ("chapters", "periods", "people", "narrator", "values", "overview")
YEAR = re.compile(r"(?<!\d)(?:187[5-9]|18[89]\d|19[0-3]\d|194[0-7])(?!\d)")
HIDDEN_NOTES = {"短段说明", "长段说明"}
CONTINUATION = "（这个场景段在本段之后还有内容。场景栏描述整个场景段，本段之后才发生的，不能当作已经发生；以本段原文为准。）"
EVIDENCE = "以下各栏是证据，供核对和追问；解读时先读上面的位置说明。"
PERIOD_LABELS = {
    "age": "年龄", "places": "地点与行程", "from_where": "从哪来", "money": "钱和生计",
    "people_marginal": "边缘的人", "practice": "在修什么", "absent": "刻意没做什么",
    "output": "在产出什么", "absorbing": "在吸收什么", "world": "外部世界",
}


def split_cast_name(name):
    """Split the approved separators, retaining parenthetical names as tokens."""
    while re.search(r"（[^（）]*）", name):
        name = re.sub(r"（([^（）]*)）", r"、\1、", name)
    return [part.strip() for part in re.split(r"以及|与|和|及|[、，,；;/／]", name) if part.strip()]


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json(raw):
    def invalid(value):
        raise ValueError("non-finite JSON number")
    return json.loads(raw, object_pairs_hook=_unique, parse_constant=invalid)


def _span(start, end):
    if isinstance(start, bool) or isinstance(end, bool):
        raise ValueError("invalid progress interval")
    a, b = Decimal(str(start)), Decimal(str(end))
    if not a.is_finite() or not b.is_finite() or not 0 <= a < b <= 1:
        raise ValueError("invalid progress interval")
    return a, b


def _overlap(a, b):
    return max(Decimal(0), min(a[1], b[1]) - max(a[0], b[0]))


def _by_id(rows, key):
    result = {}
    for row in rows:
        if row[key] in result:
            raise ValueError(f"duplicate {key}")
        result[row[key]] = row
    return result


def load_data(index_path=None, manifest_path=None, cards_path=None):
    """Load only JSON and fail closed when the index and manifest disagree."""
    index_path = Path(index_path) if index_path is not None else INDEX
    manifest_path = Path(manifest_path) if manifest_path is not None else MANIFEST
    cards_path = Path(cards_path) if cards_path is not None else CARDS
    raw = manifest_path.read_bytes()
    index, manifest = _json(index_path.read_bytes()), _json(raw)
    if index["manifest_sha256"] != sha256(raw).hexdigest():
        raise ValueError("manifest/index mismatch")
    chapters = _by_id(manifest["chapters"], "chapter")
    grouped = {}
    for number, unit in enumerate(index["units"], 1):
        if unit["unit"] != number or unit["chapter"] not in chapters:
            raise ValueError("invalid unit sequence or chapter")
        if unit["part"] != chapters[unit["chapter"]]["part"]:
            raise ValueError("unit/manifest part mismatch")
        grouped.setdefault(unit["chapter"], []).append(unit)
    if set(grouped) != set(chapters):
        raise ValueError("index/manifest chapters mismatch")
    for units in grouped.values():
        previous = Decimal(0)
        for number, unit in enumerate(units, 1):
            a, b = _span(*unit["progress"])
            if a != previous or unit["unit_in_chapter"] != number or unit["unit_total_in_chapter"] != len(units):
                raise ValueError("unit chapter coverage mismatch")
            previous = b
        if previous != 1:
            raise ValueError("unit chapter end mismatch")
    cards = {name: _json((cards_path / f"{name}.json").read_bytes()) for name in CARD_FILES}
    if set(_by_id(cards["chapters"]["chapters"], "chapter")) != set(chapters):
        raise ValueError("cards/index chapters mismatch")
    return cards, index


def split_later(items, event_year):
    current, later = [], []
    for item in items:
        year = YEAR.search(item["when"])
        target = later if event_year is not None and year and int(year[0]) > event_year else current
        target.append(deepcopy(item))
    return current, later


def lookup(unit_no, cards_doc, index, later=False):
    """Query an in-memory JSON bundle without mutating it; suitable for batch use."""
    units = index["units"]
    if type(unit_no) is not int or not 1 <= unit_no <= len(units):
        raise ValueError(f"unit must be between 1 and {len(units)}")
    unit = units[unit_no - 1]
    if unit["unit"] != unit_no:
        raise ValueError("unit sequence mismatch")
    span = _span(*unit["progress"])
    chapters = _by_id(cards_doc["chapters"]["chapters"], "chapter")
    periods = _by_id(cards_doc["periods"]["periods"], "id")
    segments, weights = [], {}
    previous = Decimal(0)
    for seg in chapters[unit["chapter"]]["segments"]:
        seg_span = _span(seg["from"], seg["to"])
        if seg_span[0] != previous:
            raise ValueError("scene gap/overlap")
        previous = seg_span[1]
        pid = seg["period"]
        if (unit["chapter"] == 0 and pid is not None) or (unit["chapter"] != 0 and (pid not in periods or pid == "APPENDIX")):
            raise ValueError("invalid scene period")
        overlap = _overlap(span, seg_span)
        if not overlap:
            continue
        public = deepcopy({key: value for key, value in seg.items() if key != "paragraph_range"})
        public.update(share=float(round(overlap / (span[1] - span[0]), 3)), continues_after=seg_span[1] > span[1])
        public["notes"] = [note for note in public["notes"] if note["label"] not in HIDDEN_NOTES]
        segments.append(public)
        weights[pid] = weights.get(pid, Decimal(0)) + overlap
    if previous != 1 or not segments:
        raise ValueError("incomplete scene coverage")
    # Dict insertion order resolves equal weights by the earliest scene.
    main_id = max(weights, key=weights.get)
    main = deepcopy(periods[main_id]) if main_id is not None else None
    other = [deepcopy({key: periods[pid][key] for key in ("id", "title", "years", "position")})
             for pid in weights if pid is not None and pid != main_id]
    if main is not None:
        main["event_times"] = [s["event_time"] for s in segments if s["period"] == main_id]
        years = [int(year) for text in main["event_times"] for year in YEAR.findall(text)]
        main["event_year"] = max(years, default=None)
        main["position"]["shifts"], main["shifts_later"] = split_later(main["position"]["shifts"], main["event_year"])
        main["dated_entries"], main["dated_entries_later"] = split_later(main["dated_entries"], main["event_year"])
    first_span = _span(segments[0]["from"], segments[0]["to"])
    prior = [u["unit"] for u in units if u["chapter"] == unit["chapter"] and u["unit"] < unit_no
             and _overlap(_span(*u["progress"]), first_span)]
    tokens_by_period = {}
    for s in segments:
        tokens_by_period.setdefault(s["period"], set()).update(
            token for cast in s["cast"] for token in split_cast_name(cast["name"]))
    present, absent = [], []
    for person in cards_doc["people"]["people"]:
        if person["period"] in tokens_by_period and tokens_by_period[person["period"]].intersection(person["names_in_cards"]):
            present.append(deepcopy(person))
        elif main_id is not None and person["period"] == main_id:
            absent.append({**{key: person[key] for key in ("id", "name", "period", "years")},
                           **{key: person["fields"][key] for key in ("身份", "此时对他重不重要")}})
    result = {key: deepcopy(unit[key]) for key in ("unit", "chapter", "part", "unit_in_chapter", "unit_total_in_chapter", "sg_pages", "progress")}
    result.update(segments=segments, prior_units=[min(prior), max(prior)] if prior else None,
                  main_period=main, other_periods=other, people={"present": present, "absent": absent},
                  narrator=deepcopy(cards_doc["narrator"]), values=deepcopy(cards_doc["values"]))
    if later:
        result["later"] = {"overview": deepcopy(cards_doc["overview"]), "appendix": deepcopy(periods["APPENDIX"])}
    return result


def _field(lines, label, text):
    if text:
        lines.extend([f"**{label}**", "", text, ""])


def _position(lines, period):
    cutoff = next((n["text"] for n in period.get("notes", []) if n["label"] == "位置说明只写到"), None)
    lines += ["**位置说明**" + (f"（只写到 {cutoff}）" if cutoff is not None else ""), ""]
    for key, label in (("has", "进来时已有"), ("wants", "这一段最想推进"), ("stuck", "主要卡在")):
        _field(lines, label, period["position"][key])
    lines += ["**这一段里的变化**", ""]
    lines += ["- " + s["text"] for s in period["position"]["shifts"]]
    lines.append("")


def _period_evidence(lines, period):
    scoped = {n["text"].split("（")[0]: n["text"] for n in period["notes"] if n["label"] == "栏目适用时间"}
    for n in period["notes"]:
        if n["label"] == "阅读说明":
            lines += [n["text"], ""]
    for key, label in PERIOD_LABELS.items():
        _field(lines, scoped.get(label, label), period[key])
        if key == "from_where":
            for item in period["dated_entries"]:
                _field(lines, item["when"], item["text"])
            for n in period["notes"]:
                if n["label"] == "贯穿各年的线":
                    _field(lines, n["label"], n["text"])
        if key == "money" and period["people_important"]:
            lines += [f"**{scoped.get('重要的人', '重要的人')}**", ""]
            lines += ["- " + p["name"] + ("：" + p["note"] if p["note"] else "") for p in period["people_important"]]
            lines.append("")
    _field(lines, scoped.get("层面", "层面"), period["layers"]["note"])
    for n in period["notes"]:
        if n["label"] not in {"栏目适用时间", "阅读说明", "贯穿各年的线", "位置说明只写到"}:
            _field(lines, n["label"], n["text"])


def _overview(lines, overview):
    lines += ["### 生涯总览", "", overview["summary"], ""]
    for phase in overview["phases"]:
        lines += [f"#### {phase['id']} · {phase['title']} · {phase['years']} · {'、'.join(phase['periods'])}", "", phase["note"], ""]
        for item in phase["achievements"]:
            _field(lines, "做成了什么", item["text"])
            _field(lines, "为什么上图", item["why"])
        for item in phase["difficulties"]:
            _field(lines, "卡在哪", item["text"])
    lines += ["#### 三条贯穿一生的线", "", overview["threads"]["note"], ""]
    for thread in overview["threads"]["lines"]:
        lines += [f"##### {thread['title']}", "", thread["summary"], ""]
        for item in thread["items"]:
            _field(lines, item["label"], item["text"])
            if item["later"]:
                lines += ["后来的地位：" + item["later"], ""]


def format_lookup(result, cards_doc):
    """Render the skill's fixed headings; later-only content stays at the end."""
    r = result
    lo, hi = r["sg_pages"]
    pages = str(lo) if lo == hi else f"{lo}–{hi}"
    lines = ["## 位置", f"第 {r['unit']} 段 ｜ 第 {r['chapter']} 章（第 {r['part']} 部分）· 本章第 {r['unit_in_chapter']}/{r['unit_total_in_chapter']} 段 ｜ Symonds & Grant 第 {pages} 页", "", "## 所在场景段"]
    for s in r["segments"]:
        lines += [f"### {s['id']} ｜ {'＋'.join(s['types'])} ｜ {s['event_time']} ｜ {s['place']}"]
        percentage = (Decimal(str(s["share"])) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        coverage = f"本段约 {percentage}% 在这个场景段里" if percentage else "本段只有不到 1% 在这个场景段里"
        metadata = f"时期：{s['period'] or '无生活时期'}；{coverage}"
        if s["speaker"]:
            speaker = s["speaker"].removesuffix("。") if s["flags"] else s["speaker"]
            metadata += "；说话人：" + speaker
        if s["flags"]: metadata += "；标记：" + "、".join(s["flags"])
        lines += [metadata, ""]
        _field(lines, "概括", s["summary"])
        if s["cast"]:
            lines += ["**场景栏**", ""]
            lines += ["- " + (p["state"] if p["state"].startswith(p["name"]) else f"{p['name']}：{p['state']}") for p in s["cast"]]
            lines.append("")
        _field(lines, "立场", s["stance"])
        _field(lines, "引文说明", s["quote_note"])
        for note in s["notes"]:
            _field(lines, note["label"], note["text"])
        if s["continues_after"]: lines += [CONTINUATION, ""]
    lines += ["## 前情可查"]
    if r["prior_units"]:
        a, b = r["prior_units"]
        prior_range = str(a) if a == b else f"{a}–{b}"
        lines += [f"同一场景段里、本段之前：第 {prior_range} 段。用 python tools/crowley/draw.py show --unit M 只读查看；本段之后的段不看。", ""]
    else:
        lines += ["本段从场景段开头起，没有同段前情。", ""]
    main = r["main_period"]
    if main is None:
        lines += ["## 时期卡", "序：无生活时期，参照叙述者卡。", ""]
    else:
        phase = next(p for p in cards_doc["overview"]["phases"] if p["id"] == main["phase"])
        lines += [f"## 时期卡：{main['id']} · {main['years']} · {main['title']}（大阶段 {phase['id']} · {phase['title']}）",
                  "（时期卡写到整个时期结束。只用到本段事件时间为止的条目。本段事件时间：" + "；".join(main["event_times"]) + "）", ""]
        _position(lines, main)
        count = len(main["shifts_later"]) + len(main["dated_entries_later"])
        if count:
            lines += [f"另有 {count} 条明显晚于本段事件（本段事件最晚到 {main['event_year']} 年）的变化，主读不用；追问时加 --later 查看。", ""]
        lines += [EVIDENCE, ""]
        _period_evidence(lines, main)
    if r["other_periods"]:
        lines += ["## 本段涉及的其他时期", ""]
        for p in r["other_periods"]:
            lines += [f"本段有一部分属于 {p['id']}。以下只列该时期的位置说明。", "", f"### {p['id']} · {p['years']} · {p['title']}", ""]
            # JSON intentionally keeps only the four public position fields;
            # the heading's cutoff is read from the matching original card.
            original = next(card for card in cards_doc["periods"]["periods"] if card["id"] == p["id"])
            _position(lines, {**p, "notes": original["notes"]})
    if main is not None:
        lines += ["## 人物卡", "", "### 本段出场", ""]
        for p in r["people"]["present"]:
            short = " · 短卡" if p["kind"] == "short" else ""
            lines += [f"#### {p['name']} · {p['period']}（{p['years']}）{short}", "", "书中称呼：" + " / ".join(p["names_in_text"]), ""]
            if p["names_in_cards"]: lines += ["场景栏写法：" + " / ".join(p["names_in_cards"]), ""]
            for label, text in p["fields"].items(): _field(lines, label, text)
        lines += ["### 本时期重要、本段没出场", ""]
        for p in r["people"]["absent"]:
            lines += [f"- {p['name']} · {p['period']}（{p['years']}）：身份：{p['身份']}；此时对他重不重要：{p['此时对他重不重要']}"]
        lines.append("")
    lines += ["## 叙述者卡", ""]
    for label, text in r["narrator"]["fields"].items(): _field(lines, label, text)
    lines += ["### 按话题的可信度", "", "| 话题 | 怎么用 | 证据 |", "|---|---|---|"]
    for row in r["narrator"]["按话题的可信度"]:
        lines.append("| " + " | ".join(row[k].replace("|", chr(92) + "|") for k in ("话题", "怎么用", "证据")) + " |")
    lines += ["", "## 价值观卡", ""]
    for value in r["values"]["values"]:
        lines += [f"### {value['id']} · {value['title']}", ""]
        for label, text in value["fields"].items(): _field(lines, label, text)
    if "later" in r:
        lines += ["## 后来的事（追问用；引用时必须标明是后来的事）", "", "### 晚于本段事件的变化与年份条目", ""]
        if main:
            lines += [f"- {item['text']}" for item in main["shifts_later"]]
            lines.append("")
            for item in main["dated_entries_later"]:
                _field(lines, item["when"], item["text"])
        _overview(lines, r["later"]["overview"])
        lines += ["### 1923 年以后", ""]
        _period_evidence(lines, r["later"]["appendix"])
    return "\n".join(lines).rstrip() + "\n"


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="只读查询：全局段号的位置、场景与背景卡片；不读原文、不抽取、不写文件或日志。")
    parser.add_argument("--unit", type=int, required=True, help="抽取结果里的全局段号（1–851）")
    parser.add_argument("--later", action="store_true", help="追问时追加后来的变化、生涯总览与 1923 年以后的时间线")
    parser.add_argument("--json", action="store_true", help="输出结构化结果")
    args = parser.parse_args(argv)
    try:
        cards, index = load_data()
        result = lookup(args.unit, cards, index, later=args.later)
        output = json.dumps(result, ensure_ascii=False, indent=2) if args.json else format_lookup(result, cards)
    except (OSError, ValueError, KeyError, TypeError, IndexError, StopIteration, ArithmeticError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(output, end="\n" if args.json else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
