"""Build or read-only verify the deterministic, text-free unit index."""
from __future__ import annotations

import argparse
from array import array
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha1, sha256
import json
from pathlib import Path
import re
import statistics
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.crowley.pdf_text import read_pdf, word_count

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CONFIG = ROOT / "config/crowley.json"
MANIFEST = HERE / "manifest.json"
INDEX = HERE / "unit_index.json"
REPORT = HERE / "unit_map_report.md"
VERSION = 1
SENTENCE = re.compile(r"[.!?]+[\"'’”\])}]*\s+(?=[\"'‘“(\[]*\w)")
ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "prof", "rev", "mme", "m", "no", "vol", "pp", "e.g", "i.e", "etc"}


def utf8():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def encode_json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def load_config(path=CONFIG):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    keys = {"pdf_path", "target_words", "merge_tail_below", "long_paragraph_split"}
    if set(config) != keys:
        raise ValueError("config must contain pdf_path and the three segmentation thresholds")
    if not isinstance(config["pdf_path"], str) or not Path(config["pdf_path"]).is_absolute():
        raise ValueError("pdf_path must be absolute")
    for key in keys - {"pdf_path"}:
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    if not config["merge_tail_below"] < config["target_words"] < config["long_paragraph_split"]:
        raise ValueError("require merge_tail_below < target_words < long_paragraph_split")
    return config


def sentence_ends(text):
    ends = []
    for m in SENTENCE.finditer(text):
        left = text[:m.start()]
        word = re.search(r"([A-Za-z.]+)$", left)
        token = word[1] if word else ""
        if m[0].startswith(".") and (token.lower() in ABBREVIATIONS or len(token) == 1 and token.isalpha()
                                         or re.fullmatch(r"(?:[A-Z]\.)+[A-Z]", token)):
            continue
        ends.append(m.end())
    if not ends or ends[-1] != len(text):
        ends.append(len(text))
    return ends


def fragments(paragraph, config):
    text = paragraph.text
    limit, target = config["long_paragraph_split"], config["target_words"]
    if word_count(text) <= limit:
        return [(0, len(text), [])]
    if paragraph.kinds & {"verse", "quote"}:
        return [(0, len(text), ["protected_long_paragraph"])]
    result, start = [], 0
    boundaries = sentence_ends(text)
    while word_count(text[start:]) > limit:
        choices = [end for end in boundaries if end > start and text[start:end].strip()]
        end = min(choices, key=lambda n: (abs(word_count(text[start:n]) - target), n))
        if end == len(text):
            result.append((start, end, ["no_sentence_boundary"]))
            return result
        result.append((start, end, ["sentence_fallback"]))
        start = end
    if text[start:].strip():
        result.append((start, len(text), ["sentence_fallback"]))
    return result


def choose_groups(pieces, target, tail_below):
    groups, current, words = [], [], 0
    for piece in pieces:
        n = piece["words"]
        if current and words + n >= target and abs(words - target) <= abs(words + n - target):
            groups.append((current, []))
            current, words = [], 0
        current.append(piece)
        words += n
        if words >= target:
            groups.append((current, []))
            current, words = [], 0
    if current:
        groups.append((current, []))
    if len(groups) > 1 and sum(p["words"] for p in groups[-1][0]) < tail_below:
        tail, _ = groups.pop()
        groups[-1][0].extend(tail)
        groups[-1][1].append("tail_merged")
    return groups


def render_slices(book, slices):
    chunks, origins, previous = [], array("I"), None
    for s in slices:
        p = book.paragraphs[s["paragraph"] - 1]
        a, b = s["from"], s["to"]
        if not 0 <= a < b <= len(p.text):
            raise ValueError("invalid paragraph slice")
        text = p.text[a:b].strip()
        if previous == p.number:
            chunks[-1] = chunks[-1].rstrip() + " " + text
        else:
            if p.kinds & {"quote", "verse", "attribution"}:
                text = "    " + text.replace("\n", "\n    ")
            chunks.append(text)
        origins.extend(p.origins[a:b])
        previous = p.number
    return "\n\n".join(chunks), origins


def progress(n, total):
    return float((Decimal(n) / Decimal(total)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def sg_unresolved_units(book, units):
    """Flag page uncertainty without moving any text or changing any unit."""
    def key(position):
        return tuple(position[k] for k in ("page", "block", "offset"))
    unresolved = [a for a in book.anomalies if a["kind"] == "sg_unresolved_sequence"]
    issues = []
    for unit in units:
        reasons = []
        if any(key(unit["end"]) > key(a["coverage_start"]) and key(unit["start"]) <= key(a["right"])
               for a in unresolved):
            reasons.append("ambiguous_marker_sequence")
        if unit["sg_endpoints"][0] > unit["sg_endpoints"][1]:
            reasons.append("reversed_endpoints")
        if unit["sg_pages"][1] - unit["sg_pages"][0] > 4:
            reasons.append("span_over_four")
        if reasons:
            issues.append(dict(unit=unit["unit"], reasons=reasons))
    return issues


def build(config):
    book = read_pdf(config["pdf_path"])
    units = []
    for chapter in book.chapters:
        pieces = []
        for p in book.paragraphs:
            if p.chapter != chapter["chapter"]:
                continue
            for a, b, exceptions in fragments(p, config):
                pieces.append(dict(paragraph=p.number, **{"from": a, "to": b},
                                   words=word_count(p.text[a:b]), exceptions=exceptions))
        groups = choose_groups(pieces, config["target_words"], config["merge_tail_below"])
        cumulative = 0
        for number, (group, extra) in enumerate(groups, 1):
            slices = [{k: p[k] for k in ("paragraph", "from", "to")} for p in group]
            text, origins = render_slices(book, slices)
            count = word_count(text)
            exceptions = sorted(set(extra + [e for p in group for e in p["exceptions"]]))
            if not 250 <= count <= 800 and not exceptions:
                if number == len(groups) and config["merge_tail_below"] <= count < 250:
                    exceptions.append("approved_short_chapter_tail")
                else:
                    raise ValueError("unapproved unit length outside 250..800")
            start, end = book.position(origins[0]), book.position(origins[-1] + 1)
            attached = [n for n in book.notes if not n.editor and n.marker is not None
                        and origins[0] <= n.marker <= origins[-1]]
            unit = dict(unit=len(units) + 1, chapter=chapter["chapter"], part=chapter["part"],
                        unit_in_chapter=number, unit_total_in_chapter=len(groups),
                        progress=[progress(cumulative, chapter["words"]), progress(cumulative + count, chapter["words"])],
                        pdf_pages=[start["page"], end["page"]], sg_pages=book.sg_range(origins),
                        sg_endpoints=[book.sg_page(origins[0]), book.sg_page(origins[-1])],
                        start=start, end=end, words=count,
                        block_types=sorted(set().union(*(book.paragraphs[s["paragraph"] - 1].kinds for s in slices))),
                        footnotes=[dict(id=n.id, number=n.number) for n in attached],
                        slices=slices, sha1=sha1(text.encode("utf-8")).hexdigest(), exceptions=exceptions)
            units.append(unit)
            cumulative += count
        if cumulative != chapter["words"]:
            raise ValueError("segmentation changed chapter word count")
    manifest = dict(version=VERSION, extractor="PyMuPDF", extractor_version=__import__("fitz").VersionBind,
                    pdf=dict(filename=book.filename, sha256=book.digest, pages=book.page_count),
                    body=dict(start=book.body_start, end=book.body_end), chapters=book.chapters,
                    exclusions=book.exclusions,
                    footnote_counts=dict(author=sum(not n.editor for n in book.notes),
                                         editor=sum(n.editor for n in book.notes)),
                    footnotes=[dict(id=n.id, number=n.number, chapter=n.chapter, editor=n.editor,
                                    start=n.start, end=n.end,
                                    marker=book.position(n.marker) if n.marker is not None else None,
                                    sha256=sha256(n.text.encode("utf-8")).hexdigest()) for n in book.notes],
                    sg_anchors=[{k: v for k, v in a.items() if k != "absolute"} for a in book.anchors],
                    sg_unresolved_units=sg_unresolved_units(book, units),
                    source_anomalies=book.anomalies)
    index = dict(version=VERSION, manifest_sha256=sha256(encode_json(manifest)).hexdigest(),
                 parameters={k: v for k, v in config.items() if k != "pdf_path"}, units=units)
    validate(book, manifest, index)
    return book, manifest, index


def validate(book, manifest, index):
    if len(book.chapters) != 97:
        raise ValueError("expected prelude plus 96 chapters")
    units = index["units"]
    if manifest["sg_unresolved_units"] != sg_unresolved_units(book, units):
        raise ValueError("unresolved SG page issues missing from manifest")
    if [u["unit"] for u in units] != list(range(1, len(units) + 1)):
        raise ValueError("invalid global unit numbering")
    reconstructed = Counter()
    for u in units:
        text, origins = render_slices(book, u["slices"])
        if sha1(text.encode("utf-8")).hexdigest() != u["sha1"] or word_count(text) != u["words"]:
            raise ValueError("text hash/word count mismatch")
        if "WEH" in text or "-oOo-" in text or re.search(r"\{\d+[})]", text):
            raise ValueError("excluded marker leaked into body")
        for s in u["slices"]:
            p = book.paragraphs[s["paragraph"] - 1]
            if p.chapter != u["chapter"] or s["from"] != reconstructed[p.number]:
                raise ValueError("slice gap, overlap or chapter crossing")
            reconstructed[p.number] = s["to"]
        if u["start"] != book.position(origins[0]) or u["end"] != book.position(origins[-1] + 1):
            raise ValueError("raw coordinate mismatch")
    if any(reconstructed[p.number] != len(p.text) for p in book.paragraphs):
        raise ValueError("paragraph coverage is incomplete")
    for c in range(97):
        selected = [u for u in units if u["chapter"] == c]
        if not selected or selected[0]["progress"][0] != 0 or selected[-1]["progress"][1] != 1:
            raise ValueError("chapter progress endpoints are invalid")
        if any(a["progress"][1] != b["progress"][0] for a, b in zip(selected, selected[1:])):
            raise ValueError("chapter progress has gaps")
        if any(u["progress"][0] >= u["progress"][1] for u in selected):
            raise ValueError("rounded progress is not strictly increasing")
    for note in book.notes:
        if not note.editor and note.marker is not None:
            matches = [u for u in units if any(n["id"] == note.id for n in u["footnotes"])]
            if len(matches) != 1:
                raise ValueError("author note must attach to exactly one unit")


def location(p):
    return f"PDF {p['page']} / block {p['block']} / char {p['offset']}"


def make_report(book, manifest, index):
    units = index["units"]
    counts = [u["words"] for u in units]
    lines = ["# Crowley T-001 核对报告", "",
             "本文件不保存正文或脚注文字。页码为 PDF 一基页码，block/字符偏移为零基；结束位置不含自身。",
             "索引 slices 的 from/to 是清理后自然段的字符偏移；start/end 是原始 PDF block 字符坐标。",
             "", f"- PDF：{book.page_count} 页；SHA-256 `{book.digest}`。",
             f"- 章数：{len(book.chapters)}（序 + 96 章）；自然段：{len(book.paragraphs)}；抽取段：{len(units)}。",
             f"- 段长（词）：最小 {min(counts)} / 中位 {statistics.median(counts):g} / 最大 {max(counts)}。",
             f"- 作者脚注：{manifest['footnote_counts']['author']}；编者注：{manifest['footnote_counts']['editor']}。",
             f"- 正文：{location(book.body_start)} → {location(book.body_end)}（终止标记之前）。",
             "- 序前、终止标记及以后全部排除；正文范围内另排除 PDF 页码、分部/章标题、原书页码标记和编者注。",
             "- sg_pages 使用推断处理后的页码锚点包络；sg_endpoints 保留首尾顺序。仅当原始前值 +1 = 原始后值 −1 时推断中间值，不迭代，不改正文。",
             "- manifest 的 sg_anchors 中 number 为使用值、raw_number 为原值；仅被修正的标记另含 inferred_number。推断不等于已校勘。",
             "- 首个可见原书页码为 31：此前无 30 的边界，序起点只能归入首次标记覆盖区，不能证明全部文字都在第 31 页。",
             "- 最后标记之后按下一页编号记录（推定）；没有把缺失或误植的页码恢复成确定事实。",
             "", "## 章与段对应", "", "| 章 | 部分 | 章号 PDF 页 | 正文首页 | 段号 | 原书页码包络 |", "|---|---|---|---|---|---|"]
    for c in book.chapters:
        us = [u for u in units if u["chapter"] == c["chapter"]]
        lines.append(f"| {'序' if c['chapter'] == 0 else c['chapter']} | {c['part']} | {c['heading']['page']} | {c['body_start']['page']} | {us[0]['unit']}–{us[-1]['unit']} | {min(u['sg_pages'][0] for u in us)}–{max(u['sg_pages'][1] for u in us)} |")
    lines += ["", "## 全部例外段", "", "尾段并入与句末兜底均列出；protected 表示整块诗/引文不切。", "",
              "已确认决定（2026-09-28）：保留 200 词并入阈值，将 5 个 222–246 词的章尾作为验收例外，标为 approved_short_chapter_tail。", ""]
    exceptions = [u for u in units if u["exceptions"]]
    lines += [f"- 段 {u['unit']} / 章 {u['chapter']} / {u['words']} 词 / PDF {u['pdf_pages']}：{', '.join(u['exceptions'])}" for u in exceptions] or ["- None"]
    lines += ["", "## 全部编者注位置", ""]
    lines += [f"- {n.id} / 编号 {n.number} / 章 {n.chapter}：{location(n.start)} → {location(n.end)}" for n in book.notes if n.editor]
    lines += ["", "## 对不上标记的脚注", ""]
    unmatched = [n for n in book.notes if n.marker is None]
    lines += [f"- {n.id} / {'编者' if n.editor else '作者'} / 编号 {n.number} / 章 {n.chapter}：{location(n.start)}；未猜测挂载位置。" for n in unmatched] or ["- None"]
    lines += ["", "## 自然段接合审计", "", "跨页或原书页码间断才考虑接合；新块小写起首或前块无句末标点则接合。版式改变保留独立。以下为全部不确定项。", ""]
    lines += [f"- {location(j['left'])} → {location(j['right'])}：{j['action']} / {j['reason']}" for j in book.joins if j["uncertain"]] or ["- None"]
    lines += ["", f"确定/常规接合：{sum(j['action'] == 'join' and not j['uncertain'] for j in book.joins)} 处；不确定判断：{sum(j['uncertain'] for j in book.joins)} 处。",
              "", "## 原书页码及其他源异常", ""]
    for a in book.anomalies:
        if a["kind"] == "sg_inferred_marker":
            left, right = a["neighbors"]
            lines.append(f"- 推断修正：{a['raw_number']} → {a['inferred_number']}；{location(a['position'])}。"
                         f"依据：前原标记 {left}（{location(a['left'])}）+1 = 后原标记 {right}（{location(a['right'])}）−1 = {a['inferred_number']}。未经另一版本校勘。")
        elif a["kind"] == "sg_unresolved_sequence":
            lines.append(f"- **未解决**：原标记 {a['numbers'][0]} → {a['numbers'][1]}；"
                         f"{location(a['left'])} → {location(a['right'])}。前后原标记不能唯一确定替换值，保留原值。")
        elif a["kind"] == "sg_gap":
            lines.append(f"- 跳号保留：{a['numbers'][0]} → {a['numbers'][1]}；"
                         f"{location(a['left'])} → {location(a['right'])}。不补标记，不据此改写页码。")
        else:
            lines.append("- `" + json.dumps(a, ensure_ascii=False) + "`")
    lines += ["", "### 未解决的段页码", ""]
    lines += [f"- 段 {a['unit']}：未解决（{', '.join(a['reasons'])}）；"
              f"页码 {units[a['unit'] - 1]['sg_pages']}，首尾 {units[a['unit'] - 1]['sg_endpoints']}。"
              for a in manifest["sg_unresolved_units"]] or ["- None"]
    lines += ["", "### T-001 页码修正验收", "",
              f"- 唯一邻值推断修正 {sum(a['kind'] == 'sg_inferred_marker' for a in book.anomalies)} 个标记；"
              f"未解决序列 {sum(a['kind'] == 'sg_unresolved_sequence' for a in book.anomalies)} 处。",
              "- 以下只列任务书指定的段，未调整切段边界。", "",
              "| 段 | 使用页码范围 | 使用首尾页码 |", "|---|---|---|"]
    for number in (16, 17, 304, 305, 364, 365, 418, 419, 467, 563):
        unit = units[number - 1]
        lines.append(f"| {number} | {unit['sg_pages'][0]}–{unit['sg_pages'][1]} | {unit['sg_endpoints'][0]} → {unit['sg_endpoints'][1]} |")
    lines += ["", "- 不变性测试：按段号顺序，对每段除 sg_pages/sg_endpoints 外的全部字段进行规范 JSON 编码，再核对修正前 SHA-256 基准。",
              "- 修正前 851 段不变字段基准：`604ee901f3217f1abe7eba1d2fdd4a97e523db203bac247d7d44fb2fc91d0ce6`。",
              "- 历史第 408 段收据保留原字节及旧 index 摘要；收据兼容测试只在临时副本追加模拟抽取，不更改真实日志。"]
    lines += ["", "### T-001 第二次返修：跨页脚注", "",
              "- 通用条件：当前块是页首非空非页脚块，上一页最后内容块恰为该注末尾，注文本无句末标点，且新块左缩进比脚注首行多 18 点（容差小于 3 点）。作者注与编者注共用接续，随后按原分类附着或排除；代码不写死页码。",
              "- 全书版式依据：573 页首块左缩进约 70.8–70.9 点；常见脚注续行 181 行约 190.8–190.9 点、另 8 行约 106.9 点，对应首行约 172.9、88.9 点。页首落在 190.9 点的块仅一处，PDF 101 / block 1。",
              "- 实际接回 n041 的续文，正文减少 47 词；第 21 章自然段数量不变，仍对应段 142–144，全书仍为 851 段。因原切段算法重新选择相邻自然段，段 142/143 的边界有所变化；段号不变。",
              "- 第 21 章以外 848 段的全部字段（含边界、slices、words、sha1、progress、页码）基准：7f9895324415a7aee104b07afb4affd4c60c70adab5bb64870fdacc5424a0a3b。",
              "- 上节 851 段摘要是第一次页码返修的历史基准；第二次返修只豁免第 21 章，其余段继续保留原字段保护。",
              "- WEH 注 29 条共 81 个句段逐一与规范化正文比较，无整句残留；排除范围的原字符位置也逐一检查。作者注仍为 77 条，全部只附着一次。",
              "- 第 21 章七张场景卡重新按相同自然段区间计算 from/to/sg；概括、场景栏、时期及其他内容不变，21-1 仅将异常说明改为已修正。",
              "- 验收：show --unit 142 应只显示作者正文和原规则允许的自注，不应出现跨页 WEH 续文；旧第 408 段收据保留原字节。", ""]
    lines += ["", "## 保留原样的非 ASCII 字符", "", "不以词典修正拼写。为避免漏报怪字符，按码点穷举全部非 ASCII 字符位置，含正常外文及符号；此表不摘录所在词句。", ""]
    grouped = {}
    for item in book.unusual:
        grouped.setdefault((item["codepoint"], item["kind"]), []).append(location(book.position(item["absolute"])))
    for (codepoint, kind), positions in sorted(grouped.items()):
        lines.append(f"- {codepoint} / {kind} / {len(positions)} 处：" + "；".join(positions))
    lines += ["", "## 实现记录", "", "### Deviations", "",
              "- 按 File Scope 限制，把 implementation-notes 合并到本报告，没有创建额外文件。",
              "- 用户级依赖在隔离环境不可见；在现有 Python 环境执行，未安装依赖。",
              "- 页码修正任务更新：保留原值，仅以两侧原标记的唯一 +1 关系推断使用值；无法确定者标为未解决，正常跳号保留。",
              "- 用户已确认：200–249 词的章尾不并入，作为已批准例外；不改变 200 词阈值。",
              "", "### Discovered edge cases", "",
              "- 一块包含多条作者/编者注；逐条分离。漏点号的作者注按源缩进识别，漏点号的 WEH 注按标签识别。",
              "- 脚注标记主要为独立粗体数字 span，少数正常字重紧接词尾；用字体分段和邻接关系定位，保全年份及逗号。",
              "- 序中的页码可能嵌在正文块末尾；按字符范围移除。章号可能位于上一 PDF 页末。",
              "- 原书页码有倒退、重复、跳页以及两种括号误植；原值留存，可唯一推断的误植另存推断值。",
              "- 换行处词内连字符保留，只去掉布局换行；诗行和缩进引文不在内部兜底切分。",
              "- 缩进引用优先于短行诗歌启发式，避免将两行引文续句识别成诗；表格式边栏导致的接合疑点保留在审计清单。",
              "- 抽取锁住日志本身；损坏日志、来源/索引不一致时在消费随机前失败。",
              "", "### Questions for review", "", "- None", "",
              "### 验收入口与回退", "",
              "- 在项目根运行 `python tools/crowley/paginate.py --check`：应显示 97 章及一致性检查通过；不应重写索引或报告。",
              "- `python tools/crowley/draw.py show --chapter 63 --unit-in-chapter 1`：应显示定位、正文与章内后文；章首不应出现前文。",
              "- 页码修正验收：`python tools/crowley/draw.py show --unit 16` 应显示原书第 48–49 页，不应再显示第 9 页。",
              "- `python tools/crowley/draw.py draw --question \"测试\"`：只抽一次，先记日志，再显示；失败后用日志段号 show，不能重跑 draw 替代。",
              "- `python -m pytest -p no:cacheprovider tools/test_crowley.py`：应全部通过；测试的随机/失败路径使用临时日志。",
              "- 回退仅移除本任务新增工具/索引/config；保留已消费的 data/crowley/draw_log.jsonl，不修改源 PDF。",
              "", "### 交接摘要", "",
              "- 实现偏差：4 类，含用户已确认的章尾例外，均如上明示。",
              "- 最可能复核：原书页码误植和序起点缺少页码边界。",
              "- 边缘情况：7 类，见 Discovered edge cases。",
              "- 下一轮先读：本报告的异常与不确定接合清单。",
              "- 写完与验证分开：最终实际运行结果以交接回复为准。", ""]
    return "\n".join(lines)


def main(argv=None):
    utf8()
    parser = argparse.ArgumentParser(description="生成无正文的确定性索引；--check 只核对，不写文件。")
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--check", action="store_true", help="重建于内存并逐字节核对已保存的清单、索引、报告")
    args = parser.parse_args(argv)
    try:
        book, manifest, index = build(load_config(args.config))
        outputs = {MANIFEST: encode_json(manifest), INDEX: encode_json(index), REPORT: make_report(book, manifest, index).encode("utf-8")}
        if args.check:
            for path, expected in outputs.items():
                if path.read_bytes() != expected:
                    raise ValueError(f"stale or changed output: {path.name}; review and rebuild")
        else:
            for path, content in outputs.items():
                path.write_bytes(content)
        counts = [u["words"] for u in index["units"]]
        print(f"{'CHECK OK' if args.check else 'BUILT'} | 章数 {len(book.chapters)}（序 + 96） | 总段数 {len(counts)}")
        print(f"段长：{min(counts)} / {statistics.median(counts):g} / {max(counts)}（最小 / 中位 / 最大）")
        print(f"克劳利脚注 {manifest['footnote_counts']['author']} / 编者注 {manifest['footnote_counts']['editor']}")
        print(f"排除范围：{location(book.body_start)} 之前；{location(book.body_end)} 起至 PDF 末尾；页码、结构标题、编者注详见报告。")
        print(f"index sha256: {sha256(outputs[INDEX]).hexdigest()}")
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
