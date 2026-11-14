from dataclasses import dataclass
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parent.parent
PITCH_PATH = ROOT / "pitch" / "PITCH.md"

EM_DASH = "\u2014"
HASH = "\u0023"
H1_PREFIX = HASH + " "
H2_PREFIX = HASH + HASH + " "

EXPECTED_TIME_RANGES = [
    "0:00-0:20",
    "0:20-1:40",
    "1:40-2:15",
    "2:15-2:40",
    "2:40-3:00",
]
EXPECTED_WEIGHTS = [30, 20, 20, 15, 15]
MIN_SPOKEN_WORDS = 380
MAX_SPOKEN_WORDS = 490
MAX_HEADER_WORD_DRIFT = 15

TIME_RANGE_RE = re.compile(r"^(\d+):(\d{2})-(\d+):(\d{2})$")
INSIDE_SUBROW_RE = re.compile(r"^\(inside\s+[^)\n]+\)$", re.IGNORECASE)
STAGE_CUE_RE = re.compile(r"^\*\*([^*(\n]+?)\s*\(([^)\n]+)\)\*\*(?:\s.*)?$")
VALID_MD_LINK_RE = re.compile(r"!?\[([^[\]\n]+)\]\(([^()\s]+)\)")
PLACEHOLDER_KEYWORD_RE = re.compile(
    r"\b(?:TODO|TBD|FIXME|MEASURE|XXX|PLACEHOLDER)\b",
    re.IGNORECASE,
)
DECLARED_WORDS_RE = re.compile(r"\b(\d+)\s+spoken\s+words\b", re.IGNORECASE)
DECLARED_WPM_RE = re.compile(r"\((\d+)\s*wpm\)", re.IGNORECASE)


@dataclass(frozen=True)
class TimeMapRow:
    time_label: str
    start_sec: int | None
    end_sec: int | None
    part: str
    weight: int | None


def _extract_h2_section(markdown_text: str, heading_prefix: str) -> str:
    lines = markdown_text.splitlines()
    start_idx = None
    target = H2_PREFIX + heading_prefix
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped == target or stripped.startswith(target + " "):
            start_idx = idx + 1
            break
    if start_idx is None:
        raise ValueError(f"Missing section heading: {target}")
    end_idx = len(lines)
    for idx in range(start_idx, len(lines)):
        if lines[idx].strip().startswith(H2_PREFIX):
            end_idx = idx
            break
    return "\n".join(lines[start_idx:end_idx])


def parse_and_validate_time_map(
    markdown_text: str,
    expected_end_sec: int = 180,
    expected_weight_sum: int = 100,
) -> list[TimeMapRow]:
    section = _extract_h2_section(markdown_text, "Time map to judging weights")
    table_lines = [
        ln.strip()
        for ln in section.splitlines()
        if ln.strip().startswith("|") and ln.strip().endswith("|")
    ]
    if len(table_lines) < 3:
        raise ValueError("Time map table has no data rows")

    rows: list[TimeMapRow] = []
    for raw_row in table_lines[2:]:
        cols = [c.strip() for c in raw_row[1:-1].split("|")]
        if len(cols) != 3:
            raise ValueError(f"Expected 3 columns in time map row, got {len(cols)}: {raw_row!r}")
        time_col, part_col, weight_col = cols
        if not part_col:
            raise ValueError(f"Empty part description in row: {raw_row!r}")

        weight_matches = re.findall(r"\b\d+\b", weight_col)
        if len(weight_matches) > 1:
            raise ValueError(f"Ambiguous weights in cell: {weight_col!r}")
        weight = int(weight_matches[0]) if weight_matches else None
        if weight is not None and weight <= 0:
            raise ValueError(f"Weight must be positive, got {weight}")

        range_match = TIME_RANGE_RE.match(time_col)
        if range_match:
            m1, s1, m2, s2 = (int(g) for g in range_match.groups())
            if s1 >= 60 or s2 >= 60:
                raise ValueError(f"Invalid seconds in time range: {time_col!r}")
            start_sec = m1 * 60 + s1
            end_sec = m2 * 60 + s2
            if start_sec >= end_sec:
                raise ValueError(f"Non-increasing time range: {time_col!r}")
            rows.append(TimeMapRow(time_col, start_sec, end_sec, part_col, weight))
        elif INSIDE_SUBROW_RE.match(time_col):
            if not any(r.start_sec is not None for r in rows):
                raise ValueError(f"Sub-row {time_col!r} must follow a timed row")
            rows.append(TimeMapRow(time_col, None, None, part_col, weight))
        else:
            raise ValueError(f"Malformed time range cell: {time_col!r}")

    timed_rows = [r for r in rows if r.start_sec is not None and r.end_sec is not None]
    if not timed_rows:
        raise ValueError("Time map contains no timed rows")
    if timed_rows[0].start_sec != 0:
        raise ValueError(f"Time map must start at 0:00, got {timed_rows[0].time_label}")

    for prev_row, next_row in zip(timed_rows, timed_rows[1:]):
        if next_row.start_sec != prev_row.end_sec:
            raise ValueError(
                f"Time gap or overlap between {prev_row.time_label} and {next_row.time_label}"
            )

    if timed_rows[-1].end_sec != expected_end_sec:
        raise ValueError(
            f"Time map must end at {expected_end_sec}s, got {timed_rows[-1].time_label}"
        )

    weights = [r.weight for r in rows if r.weight is not None]
    total_weight = sum(weights)
    if total_weight != expected_weight_sum:
        raise ValueError(
            f"Judging weights must sum to {expected_weight_sum}, got {total_weight} ({weights})"
        )

    return rows


def extract_script_lines_and_cues(markdown_text: str) -> tuple[list[str], list[tuple[str, str]]]:
    lines = markdown_text.splitlines()
    script_start = None
    cut_start = None
    script_heading = H2_PREFIX + "Script"
    cut_prefix = H2_PREFIX + "Cut list"

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if script_start is None and (
            stripped == script_heading or stripped.startswith(script_heading + " ")
        ):
            script_start = idx + 1
        elif script_start is not None and (
            stripped == cut_prefix or stripped.startswith(cut_prefix + " ")
        ):
            cut_start = idx
            break

    if script_start is None:
        raise ValueError("Missing Script section heading")
    if cut_start is None:
        raise ValueError("Missing Cut list section heading after Script")

    spoken_lines: list[str] = []
    cues: list[tuple[str, str]] = []
    for raw in lines[script_start:cut_start]:
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith(H2_PREFIX):
            raise ValueError(f"Unexpected section heading before Cut list: {stripped!r}")
        cue_match = STAGE_CUE_RE.match(stripped)
        if cue_match:
            cues.append((cue_match.group(1).strip(), cue_match.group(2).strip()))
            continue
        spoken_lines.append(stripped)

    if not spoken_lines:
        raise ValueError("No spoken lines found in Script section")
    return spoken_lines, cues


def count_spoken_words(markdown_text: str) -> int:
    spoken_lines, _ = extract_script_lines_and_cues(markdown_text)
    cleaned_lines = [VALID_MD_LINK_RE.sub(r"\1", line) for line in spoken_lines]
    return len(" ".join(cleaned_lines).split())


def parse_declared_word_count(markdown_text: str) -> int:
    header_part = markdown_text.split("\n" + H2_PREFIX, 1)[0]
    match = DECLARED_WORDS_RE.search(header_part)
    if not match:
        raise ValueError("Missing 'NNN spoken words' declaration in header")
    return int(match.group(1))


def validate_word_budget(
    markdown_text: str,
    min_words: int = MIN_SPOKEN_WORDS,
    max_words: int = MAX_SPOKEN_WORDS,
    max_drift: int = MAX_HEADER_WORD_DRIFT,
) -> tuple[int, int]:
    actual = count_spoken_words(markdown_text)
    declared = parse_declared_word_count(markdown_text)
    if not (min_words <= actual <= max_words):
        raise ValueError(
            f"Spoken word count {actual} outside stage budget [{min_words}, {max_words}]"
        )
    if abs(actual - declared) > max_drift:
        raise ValueError(
            f"Spoken word count {actual} drifts from declared {declared} by more than {max_drift}"
        )
    return actual, declared


def find_stray_bracket_placeholders(markdown_text: str) -> list[str]:
    def _strip_clean_link(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        if PLACEHOLDER_KEYWORD_RE.search(label) or PLACEHOLDER_KEYWORD_RE.search(target):
            return match.group(0)
        return " " * len(match.group(0))

    scrubbed = VALID_MD_LINK_RE.sub(_strip_clean_link, markdown_text)
    return re.findall(r"\[[^\]\n]*\]|\[[^\s\]\n]*", scrubbed)


def validate_pitch_hygiene(markdown_text: str) -> None:
    stray = find_stray_bracket_placeholders(markdown_text)
    if stray:
        raise ValueError(f"Unmeasured or stray bracket placeholders found: {stray}")
    if "9999999999" in markdown_text or "9,999,999,999" in markdown_text:
        raise ValueError("Sentinel 9999999999 found in pitch text")
    if EM_DASH in markdown_text:
        raise ValueError("Em dash found in pitch text")


def test_pitch_time_map_contiguous_and_weights_sum_to_100():
    pitch_text = PITCH_PATH.read_text(encoding="utf-8")
    rows = parse_and_validate_time_map(pitch_text)
    timed_labels = [r.time_label for r in rows if r.start_sec is not None]
    assert timed_labels == EXPECTED_TIME_RANGES
    sub_labels = [r.time_label for r in rows if r.start_sec is None]
    assert sub_labels == ["(inside demo)"]
    weights = [r.weight for r in rows if r.weight is not None]
    assert weights == EXPECTED_WEIGHTS
    assert sum(weights) == 100


def test_pitch_script_stage_cues_align_with_time_map():
    pitch_text = PITCH_PATH.read_text(encoding="utf-8")
    rows = parse_and_validate_time_map(pitch_text)
    timed_starts = [r.time_label.split("-", 1)[0] for r in rows if r.start_sec is not None]
    _, cues = extract_script_lines_and_cues(pitch_text)
    cue_times = [cue_time for _, cue_time in cues]
    assert cue_times == timed_starts


def test_pitch_spoken_word_budget_and_header_declaration():
    pitch_text = PITCH_PATH.read_text(encoding="utf-8")
    actual_words, declared_words = validate_word_budget(pitch_text)
    assert MIN_SPOKEN_WORDS <= actual_words <= MAX_SPOKEN_WORDS
    assert abs(actual_words - declared_words) <= MAX_HEADER_WORD_DRIFT

    header_part = pitch_text.split("\n" + H2_PREFIX, 1)[0]
    wpm_match = DECLARED_WPM_RE.search(header_part)
    assert wpm_match is not None
    declared_wpm = int(wpm_match.group(1))
    expected_wpm = round(actual_words / 3.0)
    assert abs(declared_wpm - expected_wpm) <= 5


def test_pitch_no_placeholders_sentinels_or_em_dashes():
    pitch_text = PITCH_PATH.read_text(encoding="utf-8")
    validate_pitch_hygiene(pitch_text)
    assert find_stray_bracket_placeholders(pitch_text) == []
    assert "9999999999" not in pitch_text
    assert EM_DASH not in pitch_text

    links = VALID_MD_LINK_RE.findall(pitch_text)
    assert links
    for _, target in links:
        if target.startswith(("http://", "https://", "mailto:", HASH, "/")):
            continue
        rel_part = target.split(HASH, 1)[0]
        if rel_part:
            assert (PITCH_PATH.parent / rel_part).is_file(), f"Broken link in PITCH.md: {target}"


def _build_synthetic_time_map(rows_spec: list[tuple[str, str, str]]) -> str:
    lines = [
        H1_PREFIX + "Synthetic Pitch",
        "",
        H2_PREFIX + "Time map to judging weights",
        "",
        "| Time | Part | Weight it feeds |",
        "|---|---|---|",
    ]
    for t_col, p_col, w_col in rows_spec:
        lines.append(f"| {t_col} | {p_col} | {w_col} |")
    return "\n".join(lines)


def test_synthetic_time_map_catches_gaps_overlaps_and_bounds():
    valid_doc = _build_synthetic_time_map(
        [
            ("0:00-0:20", "Hook", "frames demo"),
            ("0:20-1:40", "Demo", "working demo 30"),
            ("(inside demo)", "Match", "precise match 20"),
            ("1:40-2:15", "Controls", "controls 20"),
            ("2:15-2:40", "Exceptions", "exceptions 15"),
            ("2:40-3:00", "Open", "openness 15"),
        ]
    )
    parsed = parse_and_validate_time_map(valid_doc)
    assert len(parsed) == 6

    gap_doc = _build_synthetic_time_map(
        [
            ("0:00-0:20", "Hook", "frames demo"),
            ("0:20-1:35", "Demo", "working demo 50"),
            ("1:40-3:00", "Rest", "controls 50"),
        ]
    )
    with pytest.raises(ValueError, match="gap or overlap"):
        parse_and_validate_time_map(gap_doc)

    overlap_doc = _build_synthetic_time_map(
        [
            ("0:00-0:25", "Hook", "frames demo"),
            ("0:20-3:00", "Demo", "working demo 100"),
        ]
    )
    with pytest.raises(ValueError, match="gap or overlap"):
        parse_and_validate_time_map(overlap_doc)

    bad_start_doc = _build_synthetic_time_map(
        [
            ("0:05-1:30", "Hook", "working demo 50"),
            ("1:30-3:00", "Close", "openness 50"),
        ]
    )
    with pytest.raises(ValueError, match="start at 0:00"):
        parse_and_validate_time_map(bad_start_doc)

    bad_end_doc = _build_synthetic_time_map(
        [
            ("0:00-1:30", "Hook", "working demo 50"),
            ("1:30-2:50", "Close", "openness 50"),
        ]
    )
    with pytest.raises(ValueError, match="end at 180s"):
        parse_and_validate_time_map(bad_end_doc)

    bad_seconds_doc = _build_synthetic_time_map(
        [
            ("0:00-1:75", "Hook", "working demo 50"),
            ("1:75-3:00", "Close", "openness 50"),
        ]
    )
    with pytest.raises(ValueError, match="Invalid seconds"):
        parse_and_validate_time_map(bad_seconds_doc)


def test_synthetic_time_map_catches_wrong_weight_sums():
    under_sum_doc = _build_synthetic_time_map(
        [
            ("0:00-0:20", "Hook", "frames demo"),
            ("0:20-1:40", "Demo", "working demo 30"),
            ("(inside demo)", "Match", "precise match 20"),
            ("1:40-2:15", "Controls", "controls 20"),
            ("2:15-2:40", "Exceptions", "exceptions 15"),
            ("2:40-3:00", "Open", "openness 10"),
        ]
    )
    with pytest.raises(ValueError, match="sum to 100"):
        parse_and_validate_time_map(under_sum_doc)

    over_sum_doc = _build_synthetic_time_map(
        [
            ("0:00-1:30", "Demo", "working demo 60"),
            ("1:30-3:00", "Open", "openness 50"),
        ]
    )
    with pytest.raises(ValueError, match="sum to 100"):
        parse_and_validate_time_map(over_sum_doc)


def _build_synthetic_script(spoken_word_count: int, declared_words: int) -> str:
    half = spoken_word_count // 2
    rem = spoken_word_count - half
    para_one = " ".join(["alpha"] * half)
    para_two = " ".join(["bravo"] * rem)
    return "\n".join(
        [
            H1_PREFIX + "Pitch, 3 minutes",
            "",
            f"Spoken by Presenter. {declared_words} spoken words.",
            "",
            H2_PREFIX + "Script",
            "",
            "**Hook (0:00)**",
            "",
            para_one,
            "",
            "**Demo (0:20)** (live in `/console.html`, see [DEMO.md](DEMO.md))",
            "",
            para_two,
            "",
            H2_PREFIX + "Cut list if we run over",
            "",
            "1. Drop extra words that must never count toward spoken total.",
        ]
    )


def test_synthetic_word_counter_excludes_cues_and_enforces_budget():
    valid_md = _build_synthetic_script(spoken_word_count=420, declared_words=430)
    actual, declared = validate_word_budget(valid_md)
    assert actual == 420
    assert declared == 430

    too_short_md = _build_synthetic_script(spoken_word_count=320, declared_words=320)
    with pytest.raises(ValueError, match="outside stage budget"):
        validate_word_budget(too_short_md)

    too_long_md = _build_synthetic_script(spoken_word_count=510, declared_words=510)
    with pytest.raises(ValueError, match="outside stage budget"):
        validate_word_budget(too_long_md)

    drifted_header_md = _build_synthetic_script(spoken_word_count=430, declared_words=400)
    with pytest.raises(ValueError, match="drifts from declared"):
        validate_word_budget(drifted_header_md)


@pytest.mark.parametrize(
    "bad_snippet",
    [
        "Latency is [TODO] ms.",
        "Throughput is [TODO: measure live] rps.",
        "Catalog count is [MEASURE] items.",
        "Replace [brackets] before stage.",
        "Unclosed [TODO before deadline",
        "Broken empty link [NUMBERS.md]() in text.",
        "Disguised placeholder [TODO](NUMBERS.md) in link.",
    ],
)
def test_synthetic_placeholder_detector_catches_stray_brackets(bad_snippet):
    found = find_stray_bracket_placeholders(bad_snippet)
    assert found, f"Expected placeholder detection for: {bad_snippet!r}"
    with pytest.raises(ValueError, match="bracket placeholders"):
        validate_pitch_hygiene(bad_snippet)


def test_synthetic_hygiene_allows_clean_links_and_blocks_sentinels():
    clean = "Every number comes from [NUMBERS.md](NUMBERS.md) and [DEMO.md](DEMO.md)."
    assert find_stray_bracket_placeholders(clean) == []
    validate_pitch_hygiene(clean)

    with pytest.raises(ValueError, match="9999999999"):
        validate_pitch_hygiene("Stock balance is 9999999999 cases.")

    with pytest.raises(ValueError, match="Em dash"):
        validate_pitch_hygiene("Agents buy " + EM_DASH + " humans approve.")


def test_owned_file_hygiene():
    self_text = Path(__file__).read_text(encoding="utf-8")
    assert EM_DASH not in self_text
    assert HASH not in self_text
