"""Uniform secure draws, durable receipts before output, read-only recovery."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha1, sha256
import json
import os
from pathlib import Path
import secrets
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.crowley.paginate import (CONFIG, INDEX, MANIFEST, ROOT, build, encode_json,
                                   load_config, render_slices, sentence_ends, utf8)

LOG = ROOT / "data/crowley/draw_log.jsonl"
RECEIPT_KEYS = {"ts", "question", "unit", "chapter", "unit_in_chapter",
                "unit_total_in_chapter", "sg_pages", "index_sha256"}


def load_verified(config_path=CONFIG):
    config = load_config(config_path)
    manifest_bytes, index_bytes = MANIFEST.read_bytes(), INDEX.read_bytes()
    manifest, index = json.loads(manifest_bytes), json.loads(index_bytes)
    if index["manifest_sha256"] != sha256(manifest_bytes).hexdigest():
        raise ValueError("manifest/index mismatch; review before rebuilding")
    if sha256(Path(config["pdf_path"]).read_bytes()).hexdigest() != manifest["pdf"]["sha256"]:
        raise ValueError("PDF changed; review before rebuilding or drawing")
    book, rebuilt_manifest, rebuilt_index = build(config)
    if encode_json(rebuilt_manifest) != manifest_bytes or encode_json(rebuilt_index) != index_bytes:
        raise ValueError("source, extraction rules or index changed; review and rebuild first")
    return book, index, sha256(index_bytes).hexdigest()


def retrieve(book, unit):
    text, _ = render_slices(book, unit["slices"])
    if sha1(text.encode("utf-8")).hexdigest() != unit["sha1"]:
        raise ValueError("retrieved text hash mismatch; no output")
    return text


def context_sentences(text, before=False):
    sentences = []
    for paragraph in text.split("\n\n"):
        start = 0
        for end in sentence_ends(paragraph):
            part = paragraph[start:end].strip()
            if part:
                sentences.append(part)
            start = end
    return " ".join(sentences[-2:] if before else sentences[:2])


def unit_payload(book, index, unit):
    output = dict(unit)
    output.update(text=retrieve(book, unit), context_before=None, context_after=None)
    notes = {n.id: n for n in book.notes}
    output["footnotes"] = [dict(id=n["id"], number=n["number"], text=notes[n["id"]].text)
                           for n in unit["footnotes"]]
    for delta, field in ((-1, "context_before"), (1, "context_after")):
        i = unit["unit"] - 1 + delta
        if 0 <= i < len(index["units"]):
            neighbor = index["units"][i]
            if neighbor["chapter"] == unit["chapter"]:
                output[field] = context_sentences(retrieve(book, neighbor), before=delta < 0)
    return output


def format_unit(payload):
    chapter = "序" if payload["chapter"] == 0 else f"第 {payload['chapter']} 章"
    lines = [f"第 {payload['unit']} 段 ｜ {chapter}（第 {payload['part']} 部分）· 本章第 {payload['unit_in_chapter']}/{payload['unit_total_in_chapter']} 段",
             f"Symonds & Grant 第 {payload['sg_pages'][0]}–{payload['sg_pages'][1]} 页 ｜ PDF 第 {payload['pdf_pages'][0]}–{payload['pdf_pages'][1]} 页"]
    if payload["context_before"]:
        lines.append("[前文语境] " + payload["context_before"])
    lines.append(payload["text"])
    if payload["footnotes"]:
        lines.append("[脚注]\n" + "\n".join(f"[{n['number']}] {n['text']}" for n in payload["footnotes"]))
    if payload["context_after"]:
        lines.append("[后文语境] " + payload["context_after"])
    return "\n\n".join(lines)


def validate_receipt(receipt):
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_KEYS:
        raise ValueError("invalid draw receipt; inspect log before another draw")
    if not isinstance(receipt["question"], str) or not receipt["question"].strip():
        raise ValueError("invalid question in log")
    for key in ("unit", "unit_in_chapter", "unit_total_in_chapter"):
        if type(receipt[key]) is not int or receipt[key] < 1:
            raise ValueError("invalid unit in log")
    if (type(receipt["chapter"]) is not int or not 0 <= receipt["chapter"] <= 96
            or receipt["unit_in_chapter"] > receipt["unit_total_in_chapter"]):
        raise ValueError("invalid chapter in log")
    pages = receipt["sg_pages"]
    if (not isinstance(pages, list) or len(pages) != 2 or any(type(p) is not int or p < 1 for p in pages)
            or pages[0] > pages[1]):
        raise ValueError("invalid page range in log")
    digest = receipt["index_sha256"]
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("invalid index digest in log")
    if not isinstance(receipt["ts"], str) or datetime.fromisoformat(receipt["ts"]).tzinfo is None:
        raise ValueError("invalid receipt timestamp")


@contextmanager
def locked_log(path):
    """Nonblocking lock on the journal itself; no sidecar files and no retries."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as log:
        log.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(log.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(log.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            log.seek(0)
            data = log.read()
            if data and not data.endswith(b"\n"):
                raise ValueError("partial draw receipt; do not redraw, inspect the journal")
            for line in data.splitlines():
                validate_receipt(json.loads(line))
            yield log
        finally:
            log.seek(0)
            if os.name == "nt":
                msvcrt.locking(log.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(log.fileno(), fcntl.LOCK_UN)


def commit_draw(question, units, index_digest, log_path=None):
    if not question.strip() or not units:
        raise ValueError("a nonempty question and unit index are required")
    selected = None
    try:
        with locked_log(LOG if log_path is None else log_path) as log:
            selected = units[secrets.randbelow(len(units))]
            receipt = {"ts": datetime.now(timezone.utc).isoformat(), "question": question,
                       **{k: selected[k] for k in ("unit", "chapter", "unit_in_chapter",
                                                  "unit_total_in_chapter", "sg_pages")},
                       "index_sha256": index_digest}
            validate_receipt(receipt)
            data = (json.dumps(receipt, ensure_ascii=False) + "\n").encode("utf-8")
            log.seek(0, os.SEEK_END)
            if log.write(data) != len(data):
                raise OSError("short journal write")
            log.flush()
            os.fsync(log.fileno())
        return selected, receipt
    except BaseException:
        if selected is not None:
            print(f"抽取已消费：段 {selected['unit']}，index {index_digest}。日志状态可能不完整；禁止重抽，核对日志后用 show 恢复。", file=sys.stderr)
        raise


def select_show(units, unit=None, chapter=None, unit_in_chapter=None):
    if unit is not None:
        if chapter is not None or unit_in_chapter is not None or not 1 <= unit <= len(units):
            raise ValueError(f"--unit must be within 1..{len(units)} and used alone")
        return units[unit - 1]
    matches = [u for u in units if u["chapter"] == chapter and u["unit_in_chapter"] == unit_in_chapter]
    if len(matches) != 1:
        raise ValueError("chapter/unit-in-chapter not found")
    return matches[0]


def main(argv=None):
    utf8()
    parser = argparse.ArgumentParser(description="draw 新抽一次，先写日志再输出；show 查看/恢复指定段，不消费随机。")
    parser.add_argument("--config", type=Path, default=CONFIG)
    subs = parser.add_subparsers(dest="command", required=True)
    draw_parser = subs.add_parser("draw", help="消费一次安全随机；失败后按日志 show，不重抽")
    draw_parser.add_argument("--question", required=True)
    show_parser = subs.add_parser("show", help="只读查看段；第 0 章为序")
    choice = show_parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--unit", type=int, help="全局段号，起于 1")
    choice.add_argument("--chapter", type=int, choices=range(97), metavar="0..96")
    show_parser.add_argument("--unit-in-chapter", type=int, help="本章段号，起于 1；与 --chapter 配对")
    for sub in (draw_parser, show_parser):
        sub.add_argument("--json", action="store_true", help="输出含正文、脚注、定位和前后语境的 JSON；不把正文存盘")
        sub.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="本地配置文件，可放在子命令前或后")
    args = parser.parse_args(argv)
    if args.command == "draw" and not args.question.strip():
        parser.error("--question cannot be blank")
    if args.command == "show":
        if args.chapter is not None and (args.unit_in_chapter is None or args.unit_in_chapter < 1):
            parser.error("--chapter requires a positive --unit-in-chapter")
        if args.unit is not None and (args.unit < 1 or args.unit_in_chapter is not None):
            parser.error("use a positive --unit alone, or --chapter with --unit-in-chapter")
    consumed = None
    try:
        book, index, digest = load_verified(args.config)
        if args.command == "draw":
            unit, receipt = commit_draw(args.question, index["units"], digest)
            consumed = unit["unit"]
        else:
            unit = select_show(index["units"], args.unit, args.chapter, args.unit_in_chapter)
        payload = unit_payload(book, index, unit)
        if args.command == "draw":
            payload["receipt"] = receipt
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else format_unit(payload), flush=True)
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if consumed is not None:
            print(f"抽取已记入日志：段 {consumed}。用 show --unit {consumed} 恢复；不要重抽。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
