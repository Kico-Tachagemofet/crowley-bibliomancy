"""Read the configured PDF in memory; persist positions and digests, never prose.

Coordinates use one-based PDF pages, PyMuPDF's zero-based block numbers, and
zero-based character offsets into get_text('blocks')[block][4]. Ends are exclusive.
"""
from __future__ import annotations

from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
import re
import statistics

import fitz


PARTS = ((1, 22), (23, 48), (49, 57), (58, 68), (69, 79), (80, 96))
SG = re.compile(r"\{(\d+)[})]")
NOTE_START = re.compile(r"(?m)^[ \t]*(\d+)\.[ \t]+")
WEH_START = re.compile(r"\d+\.?\s+WEH\s+NOTE\b", re.I)
SENTENCE_END = re.compile(r"[.!?][\"'’”\])}]*$")
NOTE_HANGING_INDENT = 18
NOTE_INDENT_TOLERANCE = 3


@dataclass
class Block:
    page: int
    number: int
    box: tuple
    text: str
    base: int
    references: list = field(default_factory=list)

    def position(self, offset=0):
        return {"page": self.page, "block": self.number, "offset": offset}


@dataclass
class Paragraph:
    number: int
    chapter: int
    text: str
    origins: array
    kinds: set = field(default_factory=set)


@dataclass
class Note:
    id: str
    number: int
    chapter: int
    editor: bool
    text: str
    start: dict
    end: dict
    absolute: int
    marker: int | None = None


@dataclass
class Book:
    filename: str
    digest: str
    page_count: int
    blocks: list
    chapters: list
    paragraphs: list
    notes: list
    anchors: list
    exclusions: list
    joins: list
    anomalies: list
    unusual: list
    body_start: dict
    body_end: dict

    def __post_init__(self):
        self.bases = [b.base for b in self.blocks]
        self.anchor_positions = [a["absolute"] for a in self.anchors]

    def position(self, absolute):
        block = self.blocks[bisect_right(self.bases, absolute) - 1]
        return block.position(absolute - block.base)

    def sg_page(self, absolute):
        # The marker closes the original page, rather than opening the next one.
        i = bisect_left(self.anchor_positions, absolute)
        return self.anchors[i]["number"] if i < len(self.anchors) else self.anchors[-1]["number"] + 1

    def sg_range(self, origins):
        # Use resolved anchors; unresolved markers retain their raw values.
        first, last = origins[0], origins[-1]
        values = {self.sg_page(first), self.sg_page(last)}
        lo, hi = bisect_left(self.anchor_positions, first), bisect_right(self.anchor_positions, last)
        values.update(a["number"] for a in self.anchors[lo:hi])
        return [min(values), max(values)]


def resolve_sg_anchors(anchors):
    """Infer only a value uniquely fixed by its two ORIGINAL neighbors.

    This is a single pass, not iterative smoothing. A missing marker is not
    inserted, and an inference is never used as evidence for another inference.
    Raw numbers and source coordinates remain available for audit.
    """
    raw = [a.get("raw_number", a["number"]) for a in anchors]
    resolved, anomalies = [], []
    for i, anchor in enumerate(anchors):
        result = {k: v for k, v in anchor.items() if k != "inferred_number"}
        result.update(number=raw[i], raw_number=raw[i])
        if 0 < i < len(raw) - 1:
            expected = raw[i - 1] + 1
            if expected == raw[i + 1] - 1 and raw[i] != expected:
                result.update(number=expected, inferred_number=expected)
                anomalies.append(dict(kind="sg_inferred_marker", position=anchor["position"],
                                      raw_number=raw[i], inferred_number=expected,
                                      left=anchors[i - 1]["position"], right=anchors[i + 1]["position"],
                                      neighbors=[raw[i - 1], raw[i + 1]], rule="unique_original_neighbors"))
        resolved.append(result)
    for i, (left, right) in enumerate(zip(resolved, resolved[1:])):
        step = right["number"] - left["number"]
        if step != 1:
            issue = dict(kind="sg_gap" if step > 1 else "sg_unresolved_sequence",
                         left=left["position"], right=right["position"],
                         numbers=[left["number"], right["number"]],
                         raw_numbers=[left["raw_number"], right["raw_number"]])
            if step <= 0:
                # Either member of an ambiguous pair might be wrong. Include
                # both closing markers' coverage, not just the point between them.
                issue["coverage_start"] = resolved[max(0, i - 1)]["position"]
            anomalies.append(issue)
    return resolved, anomalies


def part_for(chapter):
    return 1 if chapter == 0 else next(i for i, (a, b) in enumerate(PARTS, 1) if a <= chapter <= b)


def word_count(text):
    text = re.sub(r"\[\d+\]|\{\d+\}", "", text)
    return sum(any(c.isalnum() for c in token) for token in text.split())


def normalize(raw, base=0, verse=False, replacements=()):
    """Collapse layout whitespace, preserving spelling, hyphens, and raw offsets."""
    changes = {start: (end, value) for start, end, value in replacements}
    chars, origins = [], array("I")
    i = 0
    while i < len(raw):
        if i in changes:
            end, value = changes[i]
            chars.extend(value)
            origins.extend([base + i] * len(value))
            i = end
            continue
        if raw[i].isspace():
            j = i + 1
            while j < len(raw) and raw[j].isspace() and j not in changes:
                j += 1
            # These are lexical hyphens in this PDF (including number compounds).
            join_hyphen = (chars and chars[-1] == "-" and len(chars) > 1
                           and chars[-2].isalnum() and j < len(raw) and raw[j].isalnum()
                           and "\n" in raw[i:j])
            if chars and j < len(raw) and not join_hyphen:
                chars.append("\n" if verse and "\n" in raw[i:j] else " ")
                origins.append(base + i)
            i = j
        else:
            chars.append(raw[i])
            origins.append(base + i)
            i += 1
    # Replacements at the end may have left a final layout space.
    while chars and chars[-1].isspace():
        chars.pop()
        origins.pop()
    return "".join(chars), origins


def block_kind(block, raw):
    if block.box[0] > 330:
        return "attribution"
    lines = [s.strip() for s in raw.splitlines() if s.strip()]
    if any(s.startswith("....") for s in lines):
        return "verse"
    if 98 <= block.box[0] <= 150:
        return "quote"
    if (len(lines) >= 2 and max(map(len, lines)) < 82
            and statistics.median(map(len, lines)) < 62 and block.box[2] < 460):
        return "verse"
    return "prose"


def continue_note(note, block, previous_page_last, note_left, first_on_page):
    """Join only a page-top hanging-indent continuation of an unfinished note."""
    if (not first_on_page or previous_page_last is None
            or note.end["page"] != block.page - 1
            or (previous_page_last.page, previous_page_last.number)
            != (note.end["page"], note.end["block"])
            or note.end["offset"] != len(previous_page_last.text)
            or SENTENCE_END.search(note.text.rstrip())
            or abs(block.box[0] - note_left - NOTE_HANGING_INDENT) >= NOTE_INDENT_TOLERANCE):
        return False
    note.text, _ = normalize(note.text + "\n" + block.text)
    note.end = block.position(len(block.text))
    return True


def read_pdf(path):
    path = Path(path)
    data = path.read_bytes()
    blocks, base = [], 0
    with fitz.open(stream=data, filetype="pdf") as pdf:
        page_count = len(pdf)
        for page in pdf:
            layouts = {b["number"]: b for b in page.get_text("dict", flags=fitz.TEXTFLAGS_BLOCKS)["blocks"] if b["type"] == 0}
            for item in page.get_text("blocks"):
                if item[6] != 0:
                    continue
                refs, cursor = [], 0
                for line in layouts[item[5]]["lines"]:
                    for span in line["spans"]:
                        start = item[4].find(span["text"], cursor)
                        if start < 0:
                            raise ValueError("span/block extraction mismatch")
                        cursor = start + len(span["text"])
                        value = span["text"].strip()
                        # In this edition note markers are separate bold numeric
                        # spans, NOT superscripts. This also disambiguates a marker
                        # attached to a year from the digits of the year itself.
                        offset = start + len(span["text"]) - len(span["text"].lstrip())
                        adjacent = offset > 0 and (item[4][offset - 1].isalpha() or item[4][offset - 1] in "\"'’).,!?;")
                        if value.isdigit() and len(value) <= 2 and (span["flags"] & 16 or adjacent) and span["size"] < 11:
                            refs.append((offset, offset + len(value), int(value)))
                blocks.append(Block(page.number + 1, item[5], item[:4], item[4], base, refs))
                base += len(item[4]) + 1
    preludes = [i for i, b in enumerate(blocks) if b.text.strip() == "PRELUDE"]
    ends = [i for i, b in enumerate(blocks) if b.text.strip() == "-oOo-"]
    if len(preludes) != 1 or len(ends) != 1 or preludes[0] >= ends[0]:
        raise ValueError("expected exactly one PRELUDE and terminal marker")
    first, last = preludes[0], ends[0]
    # Audit chapter lists against the actual PDF's contents page.
    toc_ranges = []
    for b in blocks[:first]:
        if "Chapter " in b.text:
            numbers = [int(n) for n in re.findall(r"\d+", re.sub(r"\{\d+\}", "", b.text.split("Chapter ", 1)[1]))]
            toc_ranges.append(numbers)
    if toc_ranges != [list(range(a, b + 1)) for a, b in PARTS]:
        raise ValueError("PDF contents do not match the six expected chapter ranges")

    exclusions = [dict(kind="front_matter", start=blocks[0].position(), end=blocks[first].position()),
                  dict(kind="back_matter", start=blocks[last].position(),
                       end=blocks[-1].position(len(blocks[-1].text)))]
    chapters = [{"chapter": 0, "part": 1, "heading": blocks[first].position()}]
    anchors, notes, body, anomalies, unusual = [], [], [], [], []
    chapter, expected, current_part, need_part_title = 0, 1, 1, False
    last_content_block = None
    by_position = {(b.page, b.number): b for b in blocks}
    page_edges = {}
    for b in blocks:
        if b.text.strip() and not (b.box[1] > 780 and b.text.strip().isdigit()):
            page_edges.setdefault(b.page, [b, b])[1] = b
    for bi in range(first, last):
        b = blocks[bi]
        raw, stripped = b.text, b.text.strip()
        def exclude(kind, start=0, end=None):
            exclusions.append(dict(kind=kind, start=b.position(start), end=b.position(len(raw) if end is None else end)))
        if not stripped:
            exclude("blank")
            continue
        if b.box[1] > 780 and stripped.isdigit():
            if int(stripped) != b.page:
                raise ValueError("unexpected PDF footer number")
            exclude("pdf_page_number")
            continue
        if notes:
            note = notes[-1]
            original = by_position[note.start["page"], note.start["block"]]
            old_end = note.end.copy()
            previous = page_edges.get(b.page - 1)
            if continue_note(note, b, previous[1] if previous else None, original.box[0],
                             b is page_edges[b.page][0]):
                exclude("editor_note" if note.editor else "author_note")
                anomalies.append(dict(kind="footnote_page_continuation", note=note.id,
                                      left=old_end, right=b.position(), editor=note.editor,
                                      hanging_indent=NOTE_HANGING_INDENT,
                                      tolerance=NOTE_INDENT_TOLERANCE))
                continue
        if stripped == "PRELUDE":
            exclude("prelude_heading")
            continue
        compact = re.sub(r"\s+", "", stripped)
        if compact in {"PARTONE", "PARTTWO", "PARTTHREE", "PARTFOUR", "PARTFIVE", "PARTSIX"}:
            current_part += 1
            need_part_title = True
            exclude("part_heading")
            continue
        if need_part_title:
            if not stripped.isupper() or current_part > 6:
                raise ValueError("unexpected part title")
            need_part_title = False
            exclude("part_title")
            continue
        if stripped.isdigit() and 280 < b.box[0] < 310:
            if int(stripped) != expected or part_for(expected) != current_part:
                raise ValueError(f"non-monotonic chapter/part at PDF {b.page}, block {b.number}")
            chapter = expected
            expected += 1
            chapters.append({"chapter": chapter, "part": current_part, "heading": b.position()})
            exclude("chapter_heading")
            continue

        # Split each numbered footnote, including mixed-author blocks. A known
        # unindented editorial note omits the full stop; detect by its own label.
        note_matches = list(NOTE_START.finditer(raw))
        note_block = bool(note_matches and (abs(b.box[0] - 173) < 3 or abs(b.box[0] - 89) < 3))
        indented_number = re.match(r"^[ \t]{4,}(\d+)[ \t]{2,}", raw)
        malformed = (WEH_START.match(stripped) or indented_number) and not note_block
        if note_block or malformed:
            if malformed:
                note_matches = [re.search(r"[ \t]*(\d+)\s+", raw)]
                anomalies.append(dict(kind="note_without_dot", position=b.position()))
            for j, match in enumerate(note_matches):
                start, end = match.start(), note_matches[j + 1].start() if j + 1 < len(note_matches) else len(raw)
                text, _ = normalize(raw[match.end():end])
                editor = bool(re.match(r"WEH\s+NOTE\b", text, re.I))
                note = Note(f"n{len(notes) + 1:03}", int(match[1]), chapter, editor, text,
                            b.position(start), b.position(end), b.base + start)
                notes.append(note)
                exclude("editor_note" if editor else "author_note", start, end)
            continue
        if "WEH" in raw:
            raise ValueError(f"unclassified editor text at PDF {b.page}, block {b.number}")

        matches = list(SG.finditer(raw))
        if re.fullmatch(r"\(\d{3}\)", stripped):
            matches = [re.search(r"\((\d+)\)", raw)]
        cursor = 0
        ranges = []
        for m in matches:
            if raw[cursor:m.start()].strip():
                ranges.append((cursor, m.start()))
            anchors.append(dict(number=int(m[1]), position=b.position(m.start()), absolute=b.base + m.start()))
            exclude("sg_page_marker", m.start(), m.end())
            if not re.fullmatch(r"\{\d+\}", m[0]):
                anomalies.append(dict(kind="sg_bracket_variant", position=b.position(m.start()), number=int(m[1])))
            cursor = m.end()
        if raw[cursor:].strip():
            ranges.append((cursor, len(raw)))
        for start, end in ranges:
            kind = block_kind(b, raw[start:end])
            body.append(dict(block=b, start=start, end=end, chapter=chapter, kind=kind, replacements=[]))
            last_content_block = b

    if expected != 97 or len(chapters) != 97 or current_part != 6 or need_part_title:
        raise ValueError("incomplete chapter/part structure")
    if not anchors or not body:
        raise ValueError("no body or reference-page anchors")

    # Nearest still-unclaimed preceding marker in the same chapter. Numbering
    # restarts frequently, so global number-only matching would silently misattach.
    candidates = []
    for bi, item in enumerate(body):
        b = item["block"]
        for start, end, number in b.references:
            if item["start"] <= start < end <= item["end"]:
                candidates.append((b.base + start, number, bi, start, end))
    claimed = set()
    for note in notes:
        options = [m for m in candidates if m[0] < note.absolute and m[1] == note.number
                   and body[m[2]]["chapter"] == note.chapter and m[0] not in claimed]
        if not options:
            # A malformed editorial entry in this edition also lacks a separate
            # marker span. Restrict the fallback to attached word suffixes with
            # an actual unmatched note; never strip ordinary numeric tokens.
            for bi, item in enumerate(body):
                b = item["block"]
                if item["chapter"] != note.chapter or b.base >= note.absolute:
                    continue
                pattern = re.compile(r"(?<=[^\W\d_])" + str(note.number) + r"(?!\d)")
                for m in pattern.finditer(b.text, item["start"], item["end"]):
                    absolute = b.base + m.start()
                    if absolute < note.absolute and absolute not in claimed:
                        options.append((absolute, note.number, bi, m.start(), m.end()))
            if options:
                candidate = options[-1]
                anomalies.append(dict(kind="plain_text_note_marker",
                                      position=body[candidate[2]]["block"].position(candidate[3]), number=note.number))
        if not options:
            continue
        marker = options[-1]
        note.marker = marker[0]
        claimed.add(marker[0])
        body[marker[2]]["replacements"].append((marker[3], marker[4], "" if note.editor else f"[{note.number}]"))
    for marker in candidates:
        if marker[0] not in claimed:
            item = body[marker[2]]
            anomalies.append(dict(kind="unclaimed_bold_number", position=item["block"].position(marker[3]), number=marker[1]))

    paragraphs, joins = [], []
    previous_item = None
    for item in body:
        b, start, end, kind = item["block"], item["start"], item["end"], item["kind"]
        changes = [(s - start, e - start, value) for s, e, value in item["replacements"]]
        text, origins = normalize(b.text[start:end], b.base + start, kind == "verse", changes)
        if not text:
            continue
        # A short continuation can look like a two-line poem in isolation. Its
        # lowercase start and preceding unfinished prose take precedence over
        # that width heuristic (never over explicit dotted verse indentation).
        if previous_item and paragraphs[-1].chapter == item["chapter"] and kind == "verse":
            pb = previous_item["block"]
            gap = pb.page != b.page or pb.number + 1 < b.number or pb is b
            if (gap and previous_item["kind"] == "prose" and abs(pb.box[0] - b.box[0]) < 3
                    and text[0].islower() and not SENTENCE_END.search(paragraphs[-1].text)
                    and not any(s.strip().startswith("....") for s in b.text[start:end].splitlines())):
                kind = item["kind"] = "prose"
                text, origins = normalize(b.text[start:end], b.base + start, False, changes)
                anomalies.append(dict(kind="short_prose_continuation", position=b.position(start)))
        for j, char in enumerate(text):
            if ord(char) > 127:
                unusual.append(dict(codepoint=f"U+{ord(char):04X}", absolute=origins[j], kind="body"))
        merge, uncertain = False, False
        if previous_item and paragraphs[-1].chapter == item["chapter"]:
            prev = paragraphs[-1]
            pb = previous_item["block"]
            gap = pb.page != b.page or pb.number + 1 < b.number or pb is b
            lower = next((c for c in text if c.isalpha()), "").islower()
            terminal = bool(SENTENCE_END.search(prev.text))
            compatible = kind == previous_item["kind"] and kind != "attribution"
            merge = bool(gap and compatible and (lower or not terminal))
            uncertain = merge and (not lower or terminal or kind != "prose")
            if gap and not compatible and not terminal:
                joins.append(dict(left=pb.position(previous_item["end"]), right=b.position(start),
                                  action="keep_separate", reason="layout_change", uncertain=True))
        if merge:
            prev = paragraphs[-1]
            separator = "\n" if kind == "verse" else " "
            if prev.text.endswith("-") and len(prev.text) > 1 and prev.text[-2].isalnum() and text[0].isalnum():
                separator = ""
            prev.text += separator + text
            if separator:
                prev.origins.append(origins[0])
            prev.origins.extend(origins)
            prev.kinds.add(kind)
            joins.append(dict(left=previous_item["block"].position(previous_item["end"]),
                              right=b.position(start), action="join", reason="continuation",
                              uncertain=uncertain))
        else:
            paragraphs.append(Paragraph(len(paragraphs) + 1, item["chapter"], text, origins, {kind}))
        previous_item = item

    for note in notes:
        # Report all non-ASCII source codepoints, rather than guessing which are typos.
        start_block = by_position[note.start["page"], note.start["block"]]
        end_block = by_position[note.end["page"], note.end["block"]]
        lo, hi = start_block.base + note.start["offset"], end_block.base + note.end["offset"]
        for b in blocks:
            if b.base >= hi:
                break
            if b.base + len(b.text) <= lo:
                continue
            for j in range(max(0, lo - b.base), min(len(b.text), hi - b.base)):
                if ord(b.text[j]) > 127:
                    unusual.append(dict(codepoint=f"U+{ord(b.text[j]):04X}", absolute=b.base + j,
                                        kind="editor_note" if note.editor else "author_note"))
    anchors, sg_anomalies = resolve_sg_anchors(anchors)
    anomalies.extend(sg_anomalies)
    anomalies.append(dict(kind="sg_initial_marker_only", position=anchors[0]["position"], number=anchors[0]["number"]))
    anomalies.append(dict(kind="sg_final_page_inferred", position=last_content_block.position(), number=anchors[-1]["number"] + 1))
    book = Book(path.name, sha256(data).hexdigest(), page_count, blocks, chapters, paragraphs,
                notes, anchors, exclusions, joins, anomalies, unusual,
                body[0]["block"].position(body[0]["start"]), blocks[last].position())
    for meta in chapters:
        ps = [p for p in paragraphs if p.chapter == meta["chapter"]]
        if not ps:
            raise ValueError("empty chapter")
        meta.update(body_start=book.position(ps[0].origins[0]),
                    body_end=book.position(ps[-1].origins[-1] + 1),
                    sg_start=book.sg_page(ps[0].origins[0]),
                    words=sum(word_count(p.text) for p in ps))
    return book
