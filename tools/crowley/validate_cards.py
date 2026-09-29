"""Validate paraphrase cards; source text remains in memory only.

paragraph_range is a LOCAL edition-specific audit field. T-010 must remove it
from public exports or move it to a local cache; public anchors use progress/SG.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import urlsplit

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.crowley.draw import load_verified
from tools.crowley.paginate import CONFIG, progress, utf8
from tools.crowley.pdf_text import word_count

HERE = Path(__file__).resolve().parent
CARDS = HERE / "cards"
RENDERED = HERE / "cards.md"
FILES = ("overview", "chapters", "periods", "people", "narrator", "values", "sources")
LAYERS = {"身体", "心念", "学问", "象征", "异象", "仪式", "关系", "现实"}
STATUSES = {"draft", "cc_approved", "kico_approved"}
FLAGS = {"插叙", "传闻", "引诗", "修习", "自评", "口述时补述"}
TYPES = {"事件", "议论", "引文"}
PRIVATE_TERMS = HERE.parents[1] / "config/private_terms.json"
_BACKSLASH = re.escape(chr(92))
FORBIDDEN = re.compile(
    rf"(?<![a-z])[a-z]:[{_BACKSLASH}/]|/(?:users|home)/|{_BACKSLASH * 2}[^{_BACKSLASH}]+{_BACKSLASH}", re.I)


def load_private_rules(path=None):
    """Optional local rules; present but invalid files must never be ignored."""
    path = PRIVATE_TERMS if path is None else Path(path)
    try:
        data = load_unique_json(path)
    except FileNotFoundError:
        return []
    if (not isinstance(data, dict) or "patterns" not in data
            or set(data) - {"patterns", "note"}
            or ("note" in data and not isinstance(data["note"], str))
            or not isinstance(data["patterns"], list)
            or not all(isinstance(p, str) and p.strip() for p in data["patterns"])):
        raise ValueError("invalid private rules format")
    try:
        return [re.compile(pattern, re.I) for pattern in data["patterns"]]
    except re.error:
        raise ValueError("invalid private rule expression") from None


PERIOD_TEXT = ("age", "places", "from_where", "money", "people_marginal", "practice", "absent", "output", "absorbing", "world")
SEGMENT_KEYS = {"id", "from", "to", "sg", "period", "types", "flags", "event_time", "place", "speaker",
                "summary", "cast", "stance", "quote_note", "paragraph_range", "sample_label", "notes", "migration_exceptions"}
# Only exact approved fields may bypass length/missing-field checks.
APPROVED_EXCEPTIONS = {
    "23-3": {
        "stance": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"
    },
    "23-4": {
        "summary": "cc61ae246b958a769bf8cfc8ec53a715cebaeeb013b227e09716b2b0c5ea5f4b"
    },
    "23-6": {
        "cast": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    },
    "23-8": {
        "stance": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"
    },
    "63-1": {
        "stance": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"
    },
    "63-5": {
        "stance": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"
    },
    "63-7": {
        "cast": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    },
    "63-8": {
        "summary": "9de83b2ef1636283170c308097e7ca4c977fd0c1baea36f8e8e669ea6a2bedd7"
    },
    "63-9": {
        "summary": "c9b16f8d6fd4974915adf866916da0930831b87b062ffa736e5f34deff71c4df"
    },
    "63-10": {
        "summary": "a09c96d8044940a41509636ac9f0f9ac60742024d15d8474784353b75d258832"
    },
    "63-11": {
        "summary": "928fc3a3359dcca94744f53ba48a416bec2f6c07b41b2db721f4062cf206fc5b",
        "stance": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"
    },
    "77-3": {
        "summary": "1bbe184017ffcd6ca1cd06409874acd973d27d076b53bbbc62536cc4b4114d2a"
    },
    "77-4": {
        "summary": "782ac851a7b837199339aa1311d92ce3fb41651242d1f21914214f62662e64fe"
    },
    "77-6": {
        "summary": "6f8daf2c3dac72dacd7a6aa9fc2626cb91493be4864e2e0546ace109de510eb9"
    },
    "77-7": {
        "summary": "c5fa850989d8f318d2709f5e14c10a1a495f8ab509680a2f238761394e875fc3"
    },
    "77-8": {
        "summary": "8913e1f282f07a2c4a1f69e0264e0be537c810340cd3028050b3227f4564f8f1",
        "stance": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"
    },
    "77-9": {
        "summary": "01e06d6cbb4d8da08bea5461255b10b49f969a945dc8fae6ffa6e55966a4bbf6"
    }
}

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def fingerprint(value):
    return sha256(canonical(value)).hexdigest()


def load_unique_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    def bad_constant(value):
        raise ValueError("non-finite JSON number")
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique, parse_constant=bad_constant)


def load_cards(directory=None):
    directory = CARDS if directory is None else Path(directory)
    return {name: load_unique_json(directory / (name + ".json")) for name in FILES}


def string_fields(value, path=""):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from string_fields(item, f"{path}[{i}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield path + ".<key>", key
            yield from string_fields(item, f"{path}.{key}")


def chapter_layout(book, chapter):
    ps = [p for p in book.paragraphs if p.chapter == chapter]
    cumulative = [0]
    for p in ps:
        cumulative.append(cumulative[-1] + word_count(p.text))
    return ps, cumulative


def sg_label(pages):
    return str(pages[0]) if pages[0] == pages[1] else f"{pages[0]}–{pages[1]}"


def segment_metrics(book, chapter, start, end):
    """Chapter-local one-based paragraph interval [start,end), ending at N+1."""
    ps, cumulative = chapter_layout(book, chapter)
    if type(start) is not int or type(end) is not int or not 1 <= start < end <= len(ps) + 1:
        raise ValueError("invalid natural-paragraph interval")
    pages = [book.sg_range(p.origins) for p in ps[start - 1:end - 1]]
    return {"from": progress(cumulative[start - 1], cumulative[-1]),
            "to": progress(cumulative[end - 1], cumulative[-1]),
            "sg": sg_label([min(p[0] for p in pages), max(p[1] for p in pages)]),
            "words": cumulative[end - 1] - cumulative[start - 1]}


def chapter_sg_ranges(book):
    result = {}
    for p in book.paragraphs:
        lo, hi = book.sg_range(p.origins)
        if p.chapter in result:
            lo, hi = min(lo, result[p.chapter][0]), max(hi, result[p.chapter][1])
        result[p.chapter] = [lo, hi]
    return result


def sentence_count(value):
    value = re.sub(r"【[^】]*】", "", value or "")
    return len([s for s in re.split(r"[。！？]+", value) if re.search(r"[\w\u4e00-\u9fff]", s)])


def tokens(value):
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", value).casefold())


def source_windows(book):
    windows = set()
    def add(text):
        ts = tokens(text)
        windows.update(tuple(ts[i:i + 8]) for i in range(len(ts) - 7))
    chapters = defaultdict(list)
    for p in book.paragraphs:
        chapters[p.chapter].append(re.sub(r"\[\d+\]", "", p.text))
    for ps in chapters.values():
        add("\n".join(ps))
    for n in book.notes:
        add(n.text)
    for b in book.blocks:
        add(b.text)
    return windows


def content_errors(data, ranges, source_ids, windows):
    rules = [FORBIDDEN, *load_private_rules()]
    errors = []
    for path, value in string_fields(data):
        # Required approval enum is metadata, never an exemption for prose.
        if not (path.endswith(".status") and value == "kico_approved") and any(rule.search(value) for rule in rules):
            errors.append(f"{path}: forbidden content")
        if value.count("【") != value.count("】"):
            errors.append(f"{path}: unbalanced citation")
        for citation in re.findall(r"【([^】]*)】", value):
            if citation == "书 卷首":
                continue  # Approved dedication source; not a fictional chapter.
            ext = re.fullmatch(r"外 ([a-z0-9-]+)", citation)
            ref = re.fullmatch(r"书 (\d+)·(\d+)(?:–(\d+))?", citation)
            chapter_ref = re.fullmatch(r"书 (\d+)(?:–(\d+))?", citation)
            chapter_scope = path.startswith(".overview.") or bool(
                re.match(r"^\.periods\.periods\[\d+\]\.position(?:\.|$)", path))
            if ext:
                if ext[1] not in source_ids:
                    errors.append(f"{path}: unknown external source")
            elif ref:
                c, lo, hi = int(ref[1]), int(ref[2]), int(ref[3] or ref[2])
                if c not in ranges or not ranges[c][0] <= lo <= hi <= ranges[c][1]:
                    errors.append(f"{path}: book citation outside chapter range")
            elif chapter_ref and chapter_scope:
                first, last = int(chapter_ref[1]), int(chapter_ref[2] or chapter_ref[1])
                if not 0 <= first <= last <= 96 or not all(c in ranges for c in range(first, last + 1)):
                    errors.append(f"{path}: book chapter citation outside chapter range")
            else:
                errors.append(f"{path}: malformed citation")
        for quoted in re.findall(r'["“「]([^"”」]+)["”」]', value):
            if not re.search(r"[\u4e00-\u9fff]", quoted) and len(tokens(quoted)) > 6:
                errors.append(f"{path}: English quotation exceeds six words")
        ts = tokens(value)
        for i in range(len(ts) - 7):
            window = tuple(ts[i:i + 8])
            if window in windows:
                errors.append(f"{path}: eight-word excerpt overlap; token={i}; sha256={fingerprint(window)}")
                break
    return errors


def validate_structure(data, book):
    errors = []
    def need(condition, message):
        if not condition:
            errors.append(message)
        return bool(condition)
    def keys(obj, fields, path):
        return need(isinstance(obj, dict) and set(obj) == set(fields), path + ": invalid fields")
    def text(value, path, empty=True):
        return need(isinstance(value, str) and (empty or bool(value.strip())), path + ": expected text")
    def rows(value, fields, path):
        if not need(isinstance(value, list), path + ": expected list"):
            return
        for item in value:
            if keys(item, fields, path):
                for field in fields:
                    text(item[field], path + "." + field)
    if not keys(data, FILES, "cards"):
        return errors
    for name in FILES:
        fields = {"schema_version", name}
        if name == "overview": fields = {"schema_version", "status", "summary", "phases", "threads"}
        if name == "narrator": fields = {"schema_version", "status", "fields", "按话题的可信度"}
        if not keys(data[name], fields, name):
            return errors
        need(type(data[name]["schema_version"]) is int and data[name]["schema_version"] == 1, name + ": schema_version must be 1")
        if name not in {"overview", "narrator"} and not need(isinstance(data[name][name], list), name + ": expected list"):
            return errors
    overview = data["overview"]
    need(overview["status"] in STATUSES | {"skeleton"}, "overview: invalid status")
    text(overview["summary"], "overview.summary")
    if keys(overview["threads"], {"note", "lines"}, "overview.threads"):
        threads = overview["threads"]
        text(threads["note"], "overview.threads.note")
        if need(isinstance(threads["lines"], list), "overview.threads: expected lines"):
            need([line.get("id") if isinstance(line, dict) else None for line in threads["lines"]]
                 == ["decline", "fame", "works"], "overview.threads: require decline/fame/works")
            for line in threads["lines"]:
                if not keys(line, {"id", "title", "summary", "items"}, "overview.thread"):
                    continue
                for field in ("title", "summary"): text(line[field], "overview.thread." + field)
                rows(line["items"], {"label", "text", "later"}, "overview.thread.items")
                if isinstance(line["items"], list):
                    for item in line["items"]:
                        if isinstance(item, dict):
                            text(item.get("label"), "overview.thread.item.label", False)
    if not need(isinstance(overview["phases"], list), "overview: expected phases"):
        return errors
    phase_ids = set()
    for phase in overview["phases"]:
        if not keys(phase, {"id", "title", "years", "periods", "achievements", "difficulties", "note"}, "phase"):
            continue
        need(phase["id"] in {f"S{i}" for i in range(1, 8)} and phase["id"] not in phase_ids, "phase: invalid/duplicate id")
        phase_ids.add(phase["id"])
        for field in ("title", "years", "note"): text(phase[field], "phase." + field)
        need(isinstance(phase["periods"], list) and all(isinstance(p, str) for p in phase["periods"]), "phase: invalid periods")
        rows(phase["achievements"], {"text", "when", "why"}, "achievements")
        rows(phase["difficulties"], {"text", "when"}, "difficulties")
    need(phase_ids == {f"S{i}" for i in range(1, 8)}, "overview: require S1–S7")
    period_ids = set()
    for p in data["periods"]["periods"]:
        fields = {"id", "title", "years", "chapters", "status", "phase", "position", "people_important", "layers", "dated_entries", "notes"} | set(PERIOD_TEXT)
        if not keys(p, fields, "period"):
            continue
        pid = p["id"]
        need(isinstance(pid, str) and bool(re.fullmatch(r"P(?:0[1-9]|1[0-7])|APPENDIX", pid)) and pid not in period_ids, "period: invalid/duplicate id")
        period_ids.add(pid)
        need(p["status"] in STATUSES | {"skeleton"}, "period: invalid status")
        need(p["phase"] is None or p["phase"] in phase_ids, "period: unknown phase")
        for field in ("title", "years", "chapters") + PERIOD_TEXT: text(p[field], "period." + field)
        if keys(p["position"], {"has", "wants", "stuck", "shifts"}, "position"):
            for field in ("has", "wants", "stuck"): text(p["position"][field], "position." + field)
            rows(p["position"]["shifts"], {"when", "text"}, "shifts")
        rows(p["people_important"], {"name", "note"}, "important people")
        rows(p["dated_entries"], {"when", "text"}, "dated entries")
        rows(p["notes"], {"label", "text"}, "period notes")
        if keys(p["layers"], {"main", "secondary", "absent", "note"}, "layers"):
            for field in ("main", "secondary", "absent"):
                value = p["layers"][field]
                need(isinstance(value, list) and all(isinstance(x, str) and x in LAYERS for x in value), "layers: invalid value")
            text(p["layers"]["note"], "layers.note")
    need(period_ids == {f"P{i:02}" for i in range(1, 18)} | {"APPENDIX"}, "periods: incomplete skeleton")
    for phase in overview["phases"]:
        if isinstance(phase, dict) and isinstance(phase.get("periods"), list):
            need(all(p in period_ids for p in phase["periods"]), "phase: unknown period")
    phases_by_id = {p["id"]: p for p in overview["phases"]}
    periods_by_id = {p["id"]: p for p in data["periods"]["periods"]}
    for p in data["periods"]["periods"]:
        if p["phase"] is not None and p["phase"] in phases_by_id:
            need(p["id"] in phases_by_id[p["phase"]]["periods"], "period: phase does not point back")
    for phase in overview["phases"]:
        for pid in phase["periods"]:
            if pid in periods_by_id:
                need(periods_by_id[pid]["phase"] == phase["id"], "phase: period does not point back")
    source_ids = set()
    for source in data["sources"]["sources"]:
        if not keys(source, {"id", "title", "url", "accessed"}, "source"):
            continue
        sid = source["id"]
        need(isinstance(sid, str) and bool(re.fullmatch(r"[a-z0-9-]+", sid)) and sid not in source_ids, "source: invalid/duplicate id")
        source_ids.add(sid)
        text(source["title"], "source.title", False)
        parsed = urlsplit(source["url"]) if isinstance(source["url"], str) else None
        need(parsed is not None and parsed.scheme in {"http", "https"} and bool(parsed.netloc), "source: invalid URL")
        try: date.fromisoformat(source["accessed"])
        except (TypeError, ValueError): need(False, "source: invalid date")
    seen = set()
    for chapter in data["chapters"]["chapters"]:
        if not keys(chapter, {"chapter", "status", "segments"}, "chapter"):
            continue
        c = chapter["chapter"]
        if not need(type(c) is int and 0 <= c <= 96 and c not in seen, "chapter: invalid/duplicate number"):
            continue
        seen.add(c)
        need(chapter["status"] in STATUSES, "chapter: invalid status")
        if not need(isinstance(chapter["segments"], list) and bool(chapter["segments"]), "chapter: missing segments"):
            continue
        previous = 1
        ps, _ = chapter_layout(book, c)
        for ordinal, s in enumerate(chapter["segments"], 1):
            sid = f"{c}-{ordinal}"
            if not keys(s, SEGMENT_KEYS, sid):
                continue
            need(s["id"] == sid, sid + ": invalid id")
            if c == 0:
                need(s["period"] is None, sid + ": prelude period must be null")
            else:
                need(s["period"] is not None and s["period"] in period_ids
                     and s["period"] != "APPENDIX", sid + ": unknown period or disallowed scene period")
            for field, allowed in (("types", TYPES), ("flags", FLAGS)):
                value = s[field]
                need(isinstance(value, list) and (field != "types" or bool(value))
                     and all(isinstance(v, str) and v in allowed for v in value) and len(value) == len(set(value)), sid + ": invalid " + field)
            for field in ("event_time", "place", "summary", "sample_label"): text(s[field], sid + "." + field)
            for field in ("speaker", "stance", "quote_note"):
                need(s[field] is None or isinstance(s[field], str), sid + ": invalid " + field)
            interval = s["paragraph_range"]
            if need(isinstance(interval, list) and len(interval) == 2, sid + ": invalid paragraph_range"):
                try:
                    metrics = segment_metrics(book, c, *interval)
                    need(interval[0] == previous, sid + ": paragraph gap/overlap")
                    previous = interval[1]
                    for field in ("from", "to"):
                        need(type(s[field]) in (float, int) and Decimal(str(s[field])).is_finite()
                             and Decimal(str(s[field])).as_tuple().exponent >= -3 and s[field] == metrics[field], sid + ": progress not at selected natural boundary")
                    need(s["from"] < s["to"], sid + ": empty interval")
                    need(s["sg"] == metrics["sg"], sid + ": SG does not match paragraphs")
                except (ValueError, TypeError): need(False, sid + ": invalid natural boundary")
            exceptions = s["migration_exceptions"]
            if not need(isinstance(exceptions, list) and all(isinstance(x, str) for x in exceptions), sid + ": invalid exceptions"):
                exceptions = []
            def permit(field):
                return (chapter["status"] == "kico_approved" and field in exceptions
                        and APPROVED_EXCEPTIONS.get(sid, {}).get(field) == fingerprint(s[field]))
            for field in exceptions:
                need(field in {"summary", "cast", "stance", "quote_note"} and permit(field), sid + ": unapproved/stale exception")
            need(1 <= sentence_count(s["summary"]) <= 4 or permit("summary"), sid + ": summary must contain 1–4 sentences")
            rows(s["cast"], {"name", "state"}, sid + ".cast")
            if "事件" in s["types"]: need(bool(s["cast"]) or permit("cast"), sid + ": event requires cast")
            for person in s["cast"]:
                if isinstance(person, dict) and isinstance(person.get("state"), str):
                    text(person.get("name"), sid + ".cast.name", False)
                    need(1 <= sentence_count(person["state"]) <= 3, sid + ": cast state must contain 1–3 sentences")
            if "议论" in s["types"]: need(bool(s["stance"]) or permit("stance"), sid + ": discussion requires stance")
            if "引文" in s["types"]:
                need(bool(s["quote_note"]) or permit("quote_note"), sid + ": quotation requires quote_note")
                need(bool(s["speaker"]), sid + ": quotation requires speaker")
            rows(s["notes"], {"label", "text"}, sid + ".notes")
        need(previous == len(ps) + 1, f"chapter {c}: does not cover chapter end")
    ids = set()
    required_people = {"身份", "此时对他重不重要", "此刻的状态", "这段关系此刻在做什么"}
    for person in data["people"]["people"]:
        if not keys(person, {"id", "name", "period", "years", "status", "names_in_text", "names_in_cards", "kind", "fields"}, "person"):
            continue
        need(person["id"] not in ids, "person: duplicate id"); ids.add(person["id"])
        need(person["period"] in period_ids, "person: unknown period")
        need(person["status"] in STATUSES, "person: invalid status")
        for field in ("id", "name", "years"): text(person[field], "person." + field, False)
        need(isinstance(person["names_in_text"], list) and bool(person["names_in_text"])
             and all(isinstance(n, str) and n.strip() for n in person["names_in_text"]), "person: invalid names_in_text")
        need(isinstance(person["names_in_cards"], list)
             and all(isinstance(n, str) and n.strip() for n in person["names_in_cards"]), "person: invalid names_in_cards")
        need(person["kind"] in {"full", "short"}, "person: invalid kind")
        need(isinstance(person["fields"], dict) and required_people <= set(person["fields"])
             and all(isinstance(v, str) for v in person["fields"].values()), "person: missing/invalid fields")
    narrator = data["narrator"]
    need(narrator["status"] in STATUSES, "narrator: invalid status")
    expected = {"谁在说", "人称的变化", "口述时的处境（书内）", "口述时的处境（书外）", "说话时间的层次",
                "他自己承认的记录缺口", "他几乎不写的事", "后几章的写作处境", "他说自己怎么写", "叙述框架"}
    need(isinstance(narrator["fields"], dict) and set(narrator["fields"]) == expected
         and all(isinstance(v, str) for v in narrator["fields"].values()), "narrator: invalid fields")
    rows(narrator["按话题的可信度"], {"话题", "怎么用", "证据"}, "narrator topics")
    ids = set()
    for value in data["values"]["values"]:
        if not keys(value, {"id", "title", "status", "fields"}, "value"):
            continue
        need(value["id"] not in ids, "value: duplicate id"); ids.add(value["id"])
        text(value["title"], "value.title", False)
        need(value["status"] in STATUSES, "value: invalid status")
        need(isinstance(value["fields"], dict) and bool(value["fields"])
             and set(value["fields"]) <= {"他明说", "他实际做", "他事后改口", "我们推断", "与常规", "说明"}
             and all(isinstance(v, str) for v in value["fields"].values()), "value: invalid evidence fields")
    return errors


def validate(data, book, windows=None):
    try:
        errors = validate_structure(data, book)
    except (KeyError, TypeError, ValueError, AttributeError):
        return ["malformed card structure"]
    if errors:
        return errors
    return content_errors(data, chapter_sg_ranges(book), {s["id"] for s in data["sources"]["sources"]},
                          source_windows(book) if windows is None else windows)


def render(data, book):
    status_labels = {"kico_approved": "kico_approved".partition("_")[0].capitalize() + " 已确认", "cc_approved": "CC 已审",
                     "draft": "待审草稿", "skeleton": "骨架待填写"}
    out = ["# 克劳利书占 · 卡片审阅稿", "",
           "本审阅稿含本地 PDF 的自然段序号，仅用于核对。开源打包（T-010）须剔除 paragraph_range 或移入本地缓存，并移除相应显示列。", "",
           "## 生涯总览", "",
           "完整一生视野，仅供总览审阅与追问；主读不调用。分期属于编者归纳。", ""]
    overview = data["overview"]
    out += [f"状态：{status_labels[overview['status']]}。", ""]
    out.append(overview["summary"] or "总述待撰写；以下仅为格式骨架。")
    for phase in overview["phases"]:
        out += ["", f"### {phase['id']} · {phase['title'] or '阶段标题待定'} · {phase['years']} · {'、'.join(phase['periods'])}", ""]
        if phase["note"]: out += [phase["note"], ""]
        for item in phase["achievements"]:
            out += ["**做成了什么**", "", item["text"], "", "**为什么上图**", "", item["why"], ""]
        for item in phase["difficulties"]: out += ["**卡在哪**", "", item["text"], ""]
    out += ["## 三条贯穿一生的线", "", overview["threads"]["note"], ""]
    for line in overview["threads"]["lines"]:
        out += [f"### {line['title']}", "", line["summary"], ""]
        for item in line["items"]:
            out += [f"**{item['label']}**", "", item["text"], ""]
            if item["later"]: out += ["后来的地位：" + item["later"], ""]
    out += ["", "## 时期卡", ""]
    labels = {"age": "年龄", "places": "地点与行程", "from_where": "从哪来", "money": "钱和生计", "people_marginal": "边缘的人",
              "practice": "在修什么", "absent": "刻意没做什么", "output": "在产出什么", "absorbing": "在吸收什么", "world": "外部世界"}
    for p in data["periods"]["periods"]:
        out += [f"### {p['id']} · {p['years']} · {p['title']}", "", f"章：{p['chapters']}；状态：{status_labels[p['status']]}。", "",
                f"所属大阶段：{p['phase'] or '待定'}。"]
        pos = p["position"]
        if not any(pos.values()):
            if p["id"] != "APPENDIX": out += ["位置说明待撰写。", ""]
        else:
            cutoff = next((item["text"] for item in p["notes"] if item["label"] == "位置说明只写到"), None)
            out += ["", "**位置说明**" + (f"（只写到 {cutoff}）" if cutoff is not None else ""), ""]
            for key, label in (("has", "进来时已有"), ("wants", "这一段最想推进"), ("stuck", "主要卡在")):
                out += [f"**{label}**", "", pos[key], ""]
            out += ["**这一段里的变化**", ""]
            for item in pos["shifts"]: out.append(f"- {item['text']}")
            out += ["", "以下各栏是证据，供核对和追问；解读时先读上面的位置说明。", ""]
        scoped_labels = {item["text"].split("（")[0]: item["text"] for item in p["notes"] if item["label"] == "栏目适用时间"}
        for item in p["notes"]:
            if item["label"] == "阅读说明": out += ["", item["text"], ""]
        for key in PERIOD_TEXT:
            if p[key]:
                label = scoped_labels.get(labels[key], labels[key])
                out += [f"**{label}**", "", p[key], ""]
            if key == "from_where":
                for item in p["dated_entries"]: out += ["", f"**{item['when']}**", "", item["text"]]
                for item in p["notes"]:
                    if item["label"] == "贯穿各年的线": out += ["", f"**{item['label']}**", "", item["text"]]
            if key == "money" and p["people_important"]:
                out += [f"**{scoped_labels.get('重要的人', '重要的人')}**", ""]
                for person in p["people_important"]:
                    out.append("- " + person["name"] + ("：" + person["note"] if person["note"] else ""))
        if p["layers"]["note"]: out += ["", f"**{scoped_labels.get('层面', '层面')}**", "", p["layers"]["note"]]
        for item in p["notes"]:
            if item["label"] not in {"栏目适用时间", "阅读说明", "贯穿各年的线", "位置说明只写到"}:
                out += ["", f"**{item['label']}**", "", item["text"]]
        out.append("")
    out += ["## 分段索引与场景栏", "", "未另注明说话人时，为口述时的他（1920 年代中期，约 1923–1926）。", ""]
    for c in data["chapters"]["chapters"]:
        out += ["### 序（第 0 章）" if c["chapter"] == 0 else f"### 第 {c['chapter']} 章", "",
                f"状态：{status_labels[c['status']]}。", ""]
        for s in c["segments"]:
            out += [f"#### {s['id']}｜{s['sample_label'] or '＋'.join(s['types'])}｜{s['event_time']}｜{s['place']}", "",
                    f"时期：{s['period'] if s['period'] is not None else '无生活时期，参照叙述者卡'}；进度：[{s['from']:.3f}, {s['to']:.3f})；自然段：[{s['paragraph_range'][0]}, {s['paragraph_range'][1]})；【书 {c['chapter']}·{s['sg']}】", ""]
            if s["flags"]: out += ["标记：" + "、".join(s["flags"]), ""]
            if s["speaker"]: out += ["说话人：" + s["speaker"], ""]
            out += ["**概括**", "", s["summary"], ""]
            if s["cast"]:
                out += ["**场景栏**", ""]
                for person in s["cast"]:
                    out.append("- " + (person["state"] if person["state"].startswith(person["name"])
                                       else f"{person['name']}：{person['state']}"))
            if s["stance"]: out += ["", "**立场**", "", s["stance"]]
            if s["quote_note"]: out += ["", "**引文说明**", "", s["quote_note"]]
            for item in s["notes"]: out += ["", f"**{item['label']}**", "", item["text"]]
            out.append("")
    out += ["## 人物卡", ""]
    current_period = None
    for p in sorted(data["people"]["people"], key=lambda p: (p["period"], p["id"])):
        if p["period"] != current_period:
            current_period = p["period"]
            out += [f"### {current_period}", ""]
        short = " · 短卡" if p["kind"] == "short" else ""
        out += [f"#### {p['name']} · {p['period']}（{p['years']}）{short}", "",
                f"状态：{status_labels[p['status']]}。", "", "书中称呼：" + " / ".join(p["names_in_text"]), ""]
        if p["names_in_cards"]: out += ["场景栏写法：" + " / ".join(p["names_in_cards"]), ""]
        for key, value in p["fields"].items(): out += [f"**{key}**", "", value, ""]
    out += ["## 叙述者卡", "", f"状态：{status_labels[data['narrator']['status']]}。", ""]
    for key, value in data["narrator"]["fields"].items(): out += [f"**{key}**", "", value, ""]
    out += ["### 按话题的可信度", "", "| 话题 | 怎么用 | 证据 |", "|---|---|---|"]
    for row in data["narrator"]["按话题的可信度"]:
        out.append("| " + " | ".join(row[k].replace("|", chr(92) + "|") for k in ("话题", "怎么用", "证据")) + " |")
    out += ["", "## 价值观卡", ""]
    for value in data["values"]["values"]:
        out += [f"### {value['id']} · {value['title']}", "", f"状态：{status_labels[value['status']]}。", ""]
        for key, text in value["fields"].items(): out += [f"**{key}**", "", text, ""]
    out += ["## 书外出处", ""]
    for source in data["sources"]["sources"]:
        out.append(f"- {source['id']}：[{source['title']}]({source['url']})；查阅：{source['accessed']}。")
    out += ["", "## 批次交付与复核说明", "", "### 第 0 批已批准的迁移例外", "",
            "优先保留已确认样卡，不为适配格式改写概括或补写缺项。以下例外绑定原字段摘要；改写内容或新增卡片不继承例外。", ""]
    for c in data["chapters"]["chapters"]:
        for s in c["segments"]:
            for field in s["migration_exceptions"]:
                out.append(f"- {s['id']} / {field}：" + (f"保留样卡的 {sentence_count(s[field])} 句概括。" if field == "summary" else "样卡未单列此栏目，保留空项。"))
    out += ["- 人物卡引用卷首题献，使用【书 卷首】；不伪造正文章号或原书页码。", "",
            "### 结构转换决定", "", "- paragraph_range 使用本章自然段序号的左闭右开区间；进度和页码从正文计算。此字段依赖本地版本，T-010 开源时必须剔除或转入本地缓存。",
            "- sample_label 留存样卡的类型说明；规范 types/flags 用于检索。额外注意、辩护等文字放入 notes。",
            "- 生涯总览与各时期的位置说明于 T-012 转入；总结性文字（总览、位置说明）出处只写到章，证据各栏写到页。",
            "- 层面枚举放入 main/secondary/absent，完整解释保存在 note；日期条目和贯穿各年的说明分别保留。",
            "- PDF 出处按指定章与页所覆盖的正文换算；原来只引章的，换算为该章全部原书页码范围。", "",
            "### 场景长度与时期提示", ""]
    for c in data["chapters"]["chapters"]:
        for s in c["segments"]:
            count = segment_metrics(book, c["chapter"], *s["paragraph_range"])["words"]
            if count < 150 or count > 1500:
                reason = ("沿用已确认样卡的独立场景或连续叙述，不另拆并。" if c["status"] == "kico_approved"
                          else " ".join(n["text"] for n in s["notes"] if n["label"] in {"短段说明", "长段说明"}) or "边界依据待审。")
                out.append(f"- {s['id']}：{count} 词。{reason}")
    out += ["- 23-8 已按第 0 批回审改为 P05；flags 为传闻、口述时补述，不含插叙。该传闻的年份不改变克劳利本人的生活时期。", "",
            "### 第 1 批范围及待复核判断", "",
            "- 第 1 批为序至第 7 章。依第 1 批回审，序的四个场景段 period 均改为 null，只参照叙述者卡；APPENDIX 仅供追问，禁止任何场景段指向它。本次不改其余已审文字。",
            "- period 按本人的生活时期填写。插入的他人故事或议论沿用章的时期；明确转到本人另一阶段才改变时期并标插叙。",
            "- 3-7 明确转到 1895 年本人棋赛经历，使用 P03；3-16 顺序跨入丧父后生活，使用 P02，不标插叙。",
            "- 4-7 的王国游戏从父亲在世时的童年展开，使用 P01 并标插叙；其中茶会未单独注明年份，待审时注意。",
            "- 1-3 以成年他人的婚姻手续议论童年洗礼，保留 P01 与口述时补述；是否需更细拆分由复审核对。",
            "- 2-3 的原文字面年份早于作者出生；仅在卡片标为年份未定，不修改 PDF 或 T-001 提取结果。",
            "- 5-4 与 7-3 保留旧序言引文及引入的完整区间。短场景不为凑词数与另一地点或时期强行合并。", "",
            "### 第 2 批范围及审核修正", "",
            "- 第 2 批第 8–15 章共 86 个场景段已获 CC 批准；序的四处 null 修正也已确认。后续批次以当前已审数据为基线，不回退直接修订。",
            "- 第 10 条落实为：第 0 章必须使用 null，其余章必须使用已定义的生活时期；APPENDIX 不用于任何场景段。序的审阅稿显示为无生活时期、参照叙述者卡。",
            "- 第 8–11 章主线为 P02，第 12–15 章主线为 P03；评论、他人轶事和口述时的时局补述沿用所在生活时期，不按评论涉及的年份另挂时期。",
            "- 10-9 已由 CC 依据书外核对改为 P04：保留原文 1897 年记载，同时注明按遇难日期推断攀登应在 1898 年夏。event_time、stance、notes 的修订及 wiki-hopkinson 来源原样保留，不再沿用此前待核的 P03 判断。",
            "- 10-10 回顾本人答应带吉亚尔莫赴干城章嘉，挂 P10 并标插叙；1902 年是回提的依据，卡片不将其当作决定发生年，也不提前展开远征结果。",
            "- 11-7 的 1898 年找到搭档单列 P03；11-10 自 1895 年夏推进至 10 月大学入学，挂 P03，不标插叙。该自然段跨期，不能从句中另切边界。",
            "- 13-7 回到少年政治氛围与题记 1892 年 9 月的拒见首相诗，挂 P02；完整诗及其标题和收尾未拆开。14-8 回叙 1896–1897 年攀登，仍属 P03。",
            "- 14-2 不补写正文没有说明的异常经验形式；14-9 的 8 月未指明是哪一个登山季，不猜年份。13-3、15-3 的诗句原作者或年代未明示，照实保留未知。",
            "- 本批 12 个不足 150 词的独立小节已逐条说明边界依据，供 CC 判断是否需要合并；没有超过 1,500 词的新增段。19 项旧样卡迁移例外不扩展到新稿。",
            "- 回归核对冻结了第 1 批数据：只把序的四个 period 还原为旧值时，应与回审前数据逐字节一致；旧值仅用于测试，不能进入实际场景路由。", "",
            "### 第 3 批范围及审核修正", "",
            "- 第 3 批第 16–22 章及第 24 章共 110 个场景段已获 CC 批准；第 23 章样卡未改。第 16、17 章主线为 P03，第 18–22 章主线为 P04，第 24 章为 P05。",
            "- 以 CC 完成第 2 批修订后的 19 章数据作冻结基线；新稿移除后，既有 chapters 数据应与基线完全一致。sources.json 本批只读，保留原七条及 wiki-hopkinson，不把来源总数锁死。",
            "- 17-10 顺叙跨入 1898 年离校后的关系决裂，挂 P04；18-1、18-2 明载 5 月学期或复活节，仍挂 P03，不机械套用本章骨架。",
            "- 16-3 巴尔托罗考察和 18-7 的 1902 年运输实验挂 P07；18-4 科利马火山经历挂 P05。朋友早年的住宅谜事及其他传闻沿所在章 P04，不另换时期。",
            "- 18-14 已依 CC 裁定及规则 8 改为 P04，去掉插叙；event_time 为婚后某日（1903 年 8 月以后）、具体时期无法确定。notes 改为时期无法确定，并提醒只按本章挂接，解读时不要对应 P04 的处境。16-10、16-6 与 18-6 的日期精度说明保留。",
            "- 21-1 的底稿异常已由 T-001 第二次返修修正：PDF 第 101 页页首续文按通用脚注缩进规则归回 n041 编者注，并从正文排除。第 21 章七张场景卡保持原自然段区间，重新计算进度和页码，叙述文字不变；全书仍为 851 个抽取段。",
            "- 第 22 章两篇誓约、完整异象旅程、马瑟斯 1900 年 2 月 16 日来信末两段、整顿方案和 4 月 12 日记录均各保留完整区间，标明说话人和日期精度。19-12 的连续对话未切断。",
            "- 22-27 只到 1900 年 6 月末离欧，沿 P04；第 24 章墨西哥实际生活归 P05。24-7 的蒙特卡洛例外与墨西哥弃赌同在一个自然段，作为未定年补述保留，不伪造自然段内边界。",
            "- 本批 21 个不足 150 词的小节已列出保留理由，交 CC 复核；没有超过 1,500 词的新增段。旧样卡的 19 项迁移例外不扩展到新稿。",
            "- 沿用当前 JSON 状态字段，不替 CC 批量变更旧卡状态；旧批审批结果见各批交付说明。", "",
            "### 第 4 批范围及审核结论", "",
            "- 第 4 批第 25–32 章共 98 个场景段已获 CC 批准；T-001 第二次返修亦已批准。第 25 章主线为 P05，第 26–32 章为 P06。",
            "- 开工前已完成 T-001 第二次返修的独立自检，并先按 CC 裁定修正 18-14；本批新稿使用已修正的正文和索引。除获准的 18-14 外，返修后的旧卡保持不变。",
            "- 25-6 回叙接前文 1899 年的阿尔卑斯攀登，挂 P04；25-8 明确转到本人 1922 年 3 月写作及投稿，挂 P17。纯议论提到后世时局不据此换期。",
            "- 26-6 从 1898 年秋开始回述早期星光训练，挂 P04；26-7 续述早期巴黎拜访，保留日期未明。关于他人的故事和否认的传言仍沿章时期，不把传闻当成本人已发生的经历。",
            "- 第 27 章核对马瑟斯的记录内含两方不同说法，完整保留并标明嵌套来源。第 28 章思想干扰分类、第 29 章跨版式短块的 9 月 6 日日记及 10 月两日记录、第 31 章两则 1 月日记、第 32 章诗作均未切断。",
            "- 不把口述时的成熟理论、后来出版及后来的自评倒灌为现场已掌握。禅定后的暂停与贝内特转向出家分别呈现，保留二人不同的反应。",
            "- 32-1 的措辞问题已由 CC 按 PDF 坐标复核：重排后仍错乱，属底稿本身缺陷。保留只转述可辨内容并加注的处理，不修改工具或已审卡片。",
            "- 本批 16 个不足 150 词的小节均有独立理由，没有超过 1,500 词的新增段；旧样卡迁移例外不扩大。",
            "- 规则 8 新增的无法定期处理已采用：按章挂接、取消插叙、明确隔离处境对应；第 65 章 February 1979 误植约束已读，本批未到该章，不提前改原文或卡片。", "",
            "### 第 5 批范围及审核结论", "",
            "- 第 5 批第 33–40 章共 77 个场景段已获 CC 批准（2026-09-28 看板记录）；状态字段沿用 draft，不批量改旧数据。完整读取各章正文与作者自注后起草；该批未扩展到第 41 章。",
            "- 第 33 章主线仍为 P06，33-10 在 3 月 23 日登上远征车厢处顺叙转 P07，不标插叙。第 34–40 章主线为 P07。",
            "- 34-1 的 1901 年 10 月 12 日协议完整保留标题、条款及署日，回挂 P06并标插叙；34-6 的本人穿越中国故事挂 P10，36-10/11 的学校及大学旧事分别挂 P02/P03，39-7 的三年后干城章嘉经历挂 P10。",
            "- 33-5 多年后的华盛顿故事与另一则谈话无法精确定期，沿 P06；36-3 的坎伯兰风暴回忆无法定期，沿 P07。均无插叙标记，notes 明确禁止将这些回忆对应本章生活处境。",
            "- 34-7 为 1,674 词连续政治议论，无场景切换，按允许的长段保留。第 32 自然段作者注指明其中内容节自 1917 年纽约文章，但节录起点未标，speaker 和 quote_note 均注明这一限度；不伪造精确引文起点。",
            "- 35-1 写 4 月 5–22 日筹备，与前章 4 月 14 日抵达的说法不一致；40-4 写 8 月 7 日洗浴，与第 38 章概称 5 月 25 日至 8 月 19 日不洗浴不一致。均加日期差异注，照录而不调和。",
            "- 39-3 原文较高海拔估值写为 22,0000 英尺，数字形式可疑；卡片只写估值与仪器分歧，并注记未校改，不自行替换成推测数字。",
            "- 36-5 所据自然段 8 也有词序不顺，保持只据可辨内容转述并加底稿措辞注。没有据此扩大范围修工具；32-1 的已审数据原样保留。",
            "- 本批 15 个不足 150 词的小节均有独立理由；唯一超过 1,500 词的新段为 34-7。协议、日记、医生自述及所引短诗均在完整区间内，不扩大旧样卡迁移例外。",
            "- 重点区分实际行动、当时记录、口述时归责及反事实登顶判断。关于殖民统治、种族与性别的偏见如实标注；搬运者的具体劳动、技能和支持不被作者的概括替代。",
            "- 新稿移除后，原 35 章数据应与本批开工时完全一致；本批不修改来源库、正文工具、索引、配置或历史抽取日志。", "",
            "### 第 6 批范围及审核结论", "",
            "- 第 6 批第 41–48 章共 83 个场景段已获 CC 批准；状态字段沿用 draft。该批经用户授权在第 5 批审核前开工，第 5 批亦已批准。",
            "- 已采用任务书新增的时期起止：第 41 章远征到 1902 年 9 月 6 日返抵斯利那加收束，挂 P07；第 42 章休养、返孟买及后续旅程挂 P08。",
            "- 第 46 章章内跨期：46-1 清晨出发仍为 P08，46-2 从 1903 年 8 月 12 日婚姻成立起挂 P09；此后登记、蜜月、伦敦、巴黎、那不勒斯、开罗及金字塔过夜均属 P09。顺叙推进，不标插叙，不修改时期骨架。",
            "- 48-5 已按 CC 的书外日期核对改为 P08 并加插叙，保留传闻、口述时补述。根据麦克唐纳 1903 年 3 月 25 日自杀和正文次晨消息，午餐推定约为 3 月 24 日；明确为推定并追加 wiki-hector-macdonald 来源。48-6 科伦坡委员会会面维持原有时间限度，不因午餐日期而一并改期。",
            "- 44-1 钱夹旧事约 1900 年挂 P04，44-2 的 1913 或 1914 年谈话挂 P14，44-3 墨西哥梦醒混淆挂 P05，均为本人跨期回叙并标插叙。44-10 住宅声响的具体时期无法确定，沿 P08 挂接并禁止处境对应。",
            "- 第 42 章富勒长记述、第 43 章文学序言与萧伯纳角色台词、第 44 章五月总结和戏剧式幽灵对话、第 45 章作品说明、第 46 章回旋诗、第 47 章波德莱尔引诗、第 48 章剧末诗均保留完整区间。引文中的医生或戏剧角色不当作克劳利本人身份。",
            "- 47-3 的作诗日保留 1904 年 1 月 7 日与 1903 年 12 月 15 日两个候选，注明日记未记作诗；不替作者消除其记忆疑问。婚后异常光亮、萝丝夜间行为和其他奇异经历区分报告与解释，不补写妻子的独立证词。",
            "- 对妻子的体罚、性别与殖民等级主张明确归属叙述者；不将服从写成双方同意，也不把他对当地证词的贬斥当作已核实事实。关于身体与心境的研究设想只作思想史转述。",
            "- 本批 7 个不足 150 词的独立小节均有边界理由，无超过 1,500 词的新段；旧样卡 19 项迁移例外不扩展。48-7 的旅馆生活与 48-8 的持续作品引文在复核时分开，符合起草规则 2。",
            "- 新稿移除后，原 43 章数据与开工基线完全一致，包括开工时尚待审、现已获批的第 5 批。生涯摘要和时期卡由 CC 并行处理，本批不改其数据，不修改原书工具、索引、配置或历史抽取日志。", "",
            "### 第 7 批范围及审核结论", "",
            "- 第 7 批第 49–56 章共 159 个场景段已获 CC 批准；48-5 日期修正、来源追加与两条书外注均已核对。状态字段沿用原值，48-6 保持原样。",
            "- 第 49 章全部沿 P09：49-8 到 1904 年 4 月 8–10 日三日书写收束；49-9 起的宗教、纪元与伦理阐释全部标口述时补述，区分后来的证明主张、后世例证和当时经验。",
            "- 第 50–56 章主线为 P10。唯一明确跨入另一时期的本人经历为 52-24：1922 年向报刊写珠峰警告，挂 P17 并标插叙。50-4 的 1911 年贝内特来信与 54-3 的 1907 年他人文章是补述，仍沿 P10。",
            "- 51-4 多年后的西班牙满月惊吓、53-15 口述前不久警员到访，均不足以确定具体时期；按规则 8 沿 P10，不标插叙，并注明不能对应本章处境。",
            "- CC 提供的两条书外核对原样加入：52-20 为帕什与搬运者遇难及责任评价，53-13 为加尔各答暗巷开枪；均使用 notes 的书外核对标签与 wiki-crowley 来源，不覆盖作者原说。",
            "- 53-13 的书外注已按 CC 在第 8 批开工前的更正更新：当夜未知，后来朋友转告一枪击中两人、送医并招认，随后报纸与警方消息促成离印；明确书外记载与原文在伤者人数上一致。53-18、53-19 的现场层次未改，后来的转告不倒灌到开枪当夜。",
            "- 52-17 到五号营地的原文字面日期为十九日，与前文八月二十一日及后文八月三十一日冲突，注明而不改成二十九日。52-27 的二万五千英尺自估与较早高度记载的差异也保留，不把自估当成已核实纪录。",
            "- 51-1 的完整法文远征协议、52-17 联署催运信、52-20 里吉事故叙述、52-22 结算声明、52-26 威胁来信、54-13 讥讽行政信，以及全部所引诗歌均保持完整。虚构诗人、译者与文学角色不充作真实史料来源。",
            "- 对部下、医生与妻子的暴力或威胁按作者自述如实写出，和他的必要性辩护分开。事故责任、疾病原因、族群偏见、药物主张和奇异经历均保留说话人及证据限度，不当作核实结论。",
            "- 本批 22 个不足 150 词的小节均注明独立理由，无超过 1,500 词的新段。53-13 为连续暗巷遭遇，未把危急时心理插话切离同一事件；旧样卡迁移例外不扩大。",
            "- 偏离与复核记录集中留在本报告，不新增 File Scope 之外文件。正文提取、索引、配置及历史日志未改；生涯摘要和时期卡继续由 CC 处理。回归基线为仅应用获准 48-5 修正后的原 51 章数据。", "",
            "### 第 8 批范围及审核结论", "",
            "- 第 8 批已获 CC 批准，无需修改；以下保留当批的切分和核对记录。",
            "- 开工前仅替换 53-13 指定的书外核对文字。本批新增第 57–62、64–65 章，139 个 draft 场景段；第 63 章样卡原样保留，未开始第 66 章。",
            "- 第 57 章到 57-12 的 1906 年 6 月 2 日抵利物浦仍为 P10；57-13 起病程、次女与写作转 P11。第 58 章在 P10 回叙框架中标插叙，58-12 明确跳到 1908 年巴黎实验，改挂 P12；当时日记与口述理论另标清楚。",
            "- 第 59 章前六段回叙返英前练习，P10 加插叙；59-7 从回英当晚及后续转 P11，琼斯誓约、九月相聚、十月九日完成及十二月邀请均在 P11。59-13 同一自然段含三年后接受的简短收尾，主要动作仍为当年邀请，明确不把后来受位倒填到 1906 年。",
            "- 第 60–62 章主线为 P11：60-3/4 的 1909 年离婚及诗作挂 P12，60-5 回看 1906 年长女之死挂 P10，60-7 的 1911 年秋入院挂 P14；61-19 的 1908 年 2 月《世界的悲剧》挂 P12；62-5 的 1910 年放宽规则挂 P13。以上本人跨期均标插叙，纯评论和他人后事不机械改期。",
            "- 书内交叉定位：60-9 原文写 1903 年，但第 64 章把同名两篇写作置于 1908 年的马德里和返程；采用后文具体场景挂 P12，保留原年及冲突注。62-7 的西班牙同行据第 63 章末与第 64 章衔接挂 P12；没有据此补定返英就医日期。第 63 章只读核对，未改样卡。",
            "- 第 64 章主线 P12；64-10 上海旧事 P10、64-14 撒哈拉启蒙回述 P13、64-16 的 1919 年 10 月 2 日纽约浴场 P15，均标插叙。64-11/12 的两例物质呈现无确切日期，只沿章挂接并隔离处境对应。",
            "- 第 65 章主线 P12；65-4 提前交代 1909 年 11 月 10 日离伦敦，挂 P13 并标插叙。65-9/10 明确为 1919 年末至 1920 年初本人经历及日记，挂 P16 并标插叙，不因章主时期为 P12 而改写年份。",
            "- February 1979 原样留在 65-10 的 event_time 与误植注中，不自行校成年份。65-9/10 的 Ü 标明为 PDF 卦画字符表现，不当正文词语，不从中推卦名、重排卦或改提取工具。",
            "- 58-19/20 的上海日记、59-8 的富勒记述内嵌誓约及日记、59-11 的十月记录与富勒三点说明、64-16 的浴场记录、65-9/10 的两次长日记均完整保留，明确外层作者、内层说话人与后见评论。第 65 章二月一日条目之后没有明确终引，连同后续论证保留到主线恢复，不伪造精确归属。",
            "- 58-5 所据第 21 自然段词序粘连，61-9 原文部族来源写 Seoul，均仅据可辨内容转述并加注。64-19 保留 December 十三日及离普利茅斯的未明衔接；65-5 的返伦敦与阁楼搜寻衔接不补造；65-19 八月起写《啊哈！》与 60-15 九月记载并列，不擅定写成或出版日。",
            "- 59-4 萨拉马故事按作者自注倾向的 1905 年干城章嘉返程挂 P10，仍注明正文在两次远征之间记忆摇摆。60-18 友谊中断、61-17 雕塑家后事等无年份，按规则 8 沿章且明确不可对应主线处境。",
            "- 本批 19 个不足 150 词的小节均有独立边界理由，无超过 1,500 词的新段。全部诗歌和长引文不切断，旧样卡迁移例外不扩大。偏离、日期疑点与待审判断集中在本报告，不新建范围外文件。", "",
            "### 尚需人工审核", "",
            "- 自动校验检查结构、出处范围、边界、禁用词与逐字摘录；史实、偏见归属、语义对应和时间信息是否越界仍需人工审核。",
            f"- 当前覆盖 {len(data['chapters']['chapters'])} 章、{sum(len(c['segments']) for c in data['chapters']['chapters'])} 场景段；全书分段索引与场景栏均经 CC 审过（第 9–12 批返修后于 2026-09-29 通过）。生涯总览、时期卡、人物卡、叙述者卡与价值观卡于 T-012 转入。", "", "### 验收与回退", "",
            "- --check 只读校验；--render 仅在校验通过后生成本审阅稿。重复渲染应逐字节一致。",
            "- 审阅第 57–62、64–65 章，先核对 57-12/13 与 59-6/7 的回英边界，再看富勒嵌套记录、60-9 的书内日期对照及 65-9/10。应看到完整引文和原样误植，不应看到 1979 被偷偷改年、卦画被当作词句、1920 年日记仍挂 P12，或第 63 章样卡被重写。",
            "- 回退第 8 批时仅移除本批八章并调整数量断言和报告后重渲染；保留 53-13 已核准的新书外注、第 63 章样卡及所有其他已审数据。正文工具、索引、配置、历史日志和 CC 并行文件不变。", ""]
    reworked_segments = {
        9: "66-3、66-15、67-14、67-22、67-27、67-30、68-12、68-17、70-8、70-10、72-2、72-4、72-7、72-13、72-15、72-17",
        10: "75-13、76-8、76-24、76-28",
        11: "84-7、85-5、85-7、88-8、88-9、89-2",
        12: "91-2、92-3、92-5、92-10、93-4、93-9、93-11、93-13、96-1、96-9、96-10",
    }
    for batch, numbers in [(9, list(range(66, 74))), (10, [74, 75, 76, 78, 79, 80, 81, 82]),
                           (11, list(range(83, 91))), (12, list(range(91, 97)))]:
        cards = [c for c in data["chapters"]["chapters"] if c["chapter"] in numbers]
        if not cards:
            continue
        out += [f"### 第 {batch} 批连续交付记录及审核结论", "",
                f"- CC 结论：REWORK 一轮，已按 CC 裁定修改。本批改过的段号：{reworked_segments[batch]}。",
                f"- 本批章节：{'、'.join(str(c['chapter']) for c in cards)}；新增 {sum(len(c['segments']) for c in cards)} 个场景段。",
                "- 已审章节保持原值；不扩展迁移例外，不放宽结构、边界、来源、隐私和防摘录检查。章节清单的严格数量断言仅随新增范围扩展。",
                "- 混写段按主体事件挂期，另一时期写进备注；口述评论、第三方故事沿章处理；只有确实无法定位的本人经历保留时期不确定说明。本批备注如下。", ""]
        for c in cards:
            for s in c["segments"]:
                for note in s["notes"]:
                    if note["label"] in {"时期无法确定", "时期说明", "日期依据", "日期疑点", "书内日期差异",
                                         "数值未校改", "原文误植", "底稿地名", "短段说明", "长段说明", "书外核对"}:
                        out.append(f"- {s['id']}〔{note['label']}〕{note['text']}")
        out += ["", "- 验收：打开本批各章，对照事件时间、时期、说话人与备注；应见自然段连续覆盖及完整诗文，不应把晚年评论倒填为当年认知。",
                "- 回退：只移除本批新章、对应新增回归与报告后重渲染；不改已审章节或并行维护的生涯卡。", ""]
    return "\n".join(out)


def main(argv=None):
    utf8()
    parser = argparse.ArgumentParser(description="校验公开卡片；--check 只读，--render 校验通过后生成审阅稿。")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--render", action="store_true")
    parser.add_argument("--config", type=Path, default=CONFIG)
    args = parser.parse_args(argv)
    try:
        book, _, _ = load_verified(args.config)
        data = load_cards()
        errors = validate(data, book)
        if errors:
            for error in errors: print("ERROR: " + error, file=sys.stderr)
            return 1
        if args.render:
            RENDERED.write_bytes(render(data, book).encode("utf-8"))
        print(f"PASS: {len(data['chapters']['chapters'])} 章 / {sum(len(c['segments']) for c in data['chapters']['chapters'])} 场景段；自然段边界、出处、枚举、禁用词与防摘录检查通过。")
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print("ERROR: card/source input cannot be validated", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
