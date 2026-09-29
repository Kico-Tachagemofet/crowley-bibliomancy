# 克劳利《忏悔录》书占

克劳利《忏悔录》书占的素材层：从本地 PDF 用操作系统提供的安全随机抽一段（约 500 词），问题与段号先写入日志，成功落盘后才输出，不可重抽。查询脚本定位这一段在书中和他一生里的位置，调出场景、时期、人物、叙述者与价值观卡片；解读交给 [bibliomancy-reading](https://github.com/Kico-Tachagemofet/bibliomancy-reading)。

## 需要自备的书

本项目使用的参考版文件为：

- 文件名：`The Confessions of Aleister Crowley An Autohagiography (Aleister Crowley) .pdf`
- 页数：588 页。
- SHA-256：`45b0eb185a107997fe85a89abf663cefad8141e3bae991109069516ca3e5e3ea`

它属于 O.T.O. 网络版一系的文本，正文用 `{n}` 标出 Symonds & Grant 页码。请自备合法取得的副本。本项目不提供原文、书籍下载或下载链接。

用别的版本也能尝试建立索引，但工具依据参考版的版式识别正文、章节与脚注，可能拒绝处理，或切出的段落与卡片对不齐。不要把能运行等同于对应准确；版本局限见下文“卡片说明”。

## 安装与建索引

需要 Python 3.10 或以上。运行依赖标准库和 PyMuPDF；以下命令在仓库根目录执行。

```sh
pip install pymupdf
```

1. 把 `config/crowley.example.json` 复制成 `config/crowley.json`。
2. 将示例里的 `pdf_path` 占位符替换为你自己的 PDF **绝对路径**；现有工具不接受相对路径。保留其余三个切段参数，除非你有意重新分段并核对卡片。
3. 建立本地索引：

```sh
python tools/crowley/paginate.py
```

这会生成本地 `manifest.json`、`unit_index.json` 与 `unit_map_report.md`，都位于 `tools/crowley/`。参考版切为 851 段；报告保留已知底稿问题。只核对已有索引、不改写文件时运行：

```sh
python tools/crowley/paginate.py --check
```

私人词检查可选：把 `config/private_terms.example.json` 复制成 `config/private_terms.json`，在本地填写自己的不区分大小写的正则规则。该文件不要提交。文件不存在时只检查本机路径；文件存在但格式或正则错误时，校验会报错，不会忽略。

## 使用

```sh
python tools/crowley/draw.py draw --question "你的问题"
python tools/crowley/draw.py show --unit N
python tools/crowley/lookup.py --unit N
python tools/crowley/lookup.py --unit N --later
python tools/crowley/validate_cards.py --check
python tools/crowley/validate_cards.py --render
```

`N` 是抽取结果里的全局段号。`draw` 先记录到本地 `data/crowley/draw_log.jsonl` 再输出；抽取后输出失败时按错误信息中的段号用 `show` 恢复，不再执行一次 `draw`。`show` 只读查看，`lookup` 只查背景、不读 PDF、不输出原文、不写日志。`--later` 用于追问，追加明显更晚的条目、生涯总览和 1923 年以后的时间线；引用时标明是后来的事。

`validate_cards.py --render` 在校验通过后生成本地审阅页 `tools/crowley/cards.md`，该页不纳入发布文件。

把 `skills/crowley-bibliomancy/SKILL.md` 放进所用智能体的 skills 目录，配合独立安装的 bibliomancy-reading 使用。本项目不附解读法副本。SKILL.md 的“后来的事”追问依赖 bibliomancy-reading 较新的追问规则；在新版规则可用之前，按 bibliomancy-reading 的现行规则办。

## 卡片说明

六层卡片保存在 `tools/crowley/cards/`：

| 层级 | 规模与用途 |
|---|---|
| 分段索引与场景栏 | 97 章（含序），1,232 个场景段；给出事件、议论、引文、人物状态与备注 |
| 时期卡 | 17 个时期，加 1923 年以后的简要时间线；先看位置说明，再按需核对证据 |
| 人物卡 | 74 张，跨时期的人分别写卡，只描述那个时期的关系 |
| 叙述者卡 | 口述处境、时间层次、记录缺口与按话题的可信度 |
| 价值观卡 | 14 条，区分他说、做、事后改口和编者推断 |
| 生涯总览 | 七个大阶段及钱、名声、作品三条贯穿线，供追问使用 |

公开定位锚点是**章号、本章进度比例、Symonds & Grant 页码**。卡片中保留的 `paragraph_range` 是自然段序号，只对参考版 PDF 有效；别的版本以进度比例和页码为准，仍须人工核对。查询脚本不使用 `paragraph_range`。自动校验中的原文对应与边界检查依赖参考版，其他版本不保证通过。

出处写作 `【书 章·页】`、`【书 章】`、`【外 来源代号】`；来源代号见 `tools/crowley/cards/sources.json`。总结性文字可以引用到章，证据栏引用到页。卡片是背景，解读仍以抽到的段落为主。

状态值：

- `kico_approved`：维护者确认了格式与框架。
- `cc_approved`：经 AI 审核助手逐批对照原文审过。
- `draft`：未审草稿。

## 局限与立场

卡片只转述，不收整段原文；英文引语每处不超过 6 个词，并有与原文连续 8 词重合的防摘录检查。书中对女性、族群、对手等的偏见按原立场转述并标注，不替他辩护，也不删改事实。

书外资料逐条注出处，以维基为主，另有 1934 年《每日先驱报》诽谤案报道与 1929 年《纽约时报》驱逐令报道的转载。自动检查不能代替史实、立场归属和语义对应的人工复核。本项目是书占工具，不是克劳利研究的权威资料。

## 测试

```sh
pip install "pytest>=8,<10"
python -m pytest
```

没有本地 PDF 和索引时，依赖原文或本地索引的测试会跳过；完整测试需要参考版 PDF、已建索引和本地审阅页。建好索引后先执行 `python tools/crowley/validate_cards.py --render`。仅存在于维护环境的历史日志和原始审阅稿相关检查，在这些文件缺失时也会跳过。

## 许可证

Copyright (c) 2026 Kico-Tachagemofet。

- `tools/` 下的软件与测试采用 MIT，见 [LICENSE](LICENSE)；不含 `tools/crowley/cards/`。
- `tools/crowley/cards/` 与 `skills/` 的卡片、skill 文字采用 CC BY-NC-SA 4.0，见 [LICENSE-CONTENT](LICENSE-CONTENT)。

书籍本身是第三方作品，不适用本仓库的许可证；本仓库不授予第三方作品的再分发权。
