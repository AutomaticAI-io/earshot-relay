"""Text hygiene for what gets read aloud: the normalizer and the tag/coalesce/cooldown field checks.

The normalizer is plain text in, plain text out, with no network and no state. It exists because senders put things in
a notification that sound terrible out loud: emoji, markdown, long URLs, stack-trace length. It never touches audio.

Every regular expression here is line-bound or linear, and the input is capped, so a hostile body cannot make it slow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .keys import MAX_TEXT_UTF8_BYTES, ApiError

TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
MAX_COALESCE_S = 3600.0
MAX_COOLDOWN_S = 86400.0
MAX_WORDS_LIMIT = 1000
# What a normalizer is willing to read: more than the 64 KiB the phone can fetch is refused before any work is done.
MAX_NORMALIZE_INPUT = MAX_TEXT_UTF8_BYTES
SENTENCE_END = ".!?…。！？"

# ---- emoji and pictographs ----------------------------------------------------------------------------------------
# Ranges, not a library: the Unicode emoji blocks plus the joiners and selectors that glue sequences together. Plain
# text symbols that read fine aloud (the copyright, registered and trade-mark signs, arrows, math) are kept.
_EMOJI_CHARS = (
    "\U0001F000-\U0001FAFF"  # mahjong, cards, enclosed, pictographs, emoticons, transport, supplemental, extended-A
    "\U0001F1E6-\U0001F1FF"  # regional indicators (flags)
    "\u2600-\u27bf"  # miscellaneous symbols and dingbats (checks, crosses, warning, stars)
    "\u2b50\u2b55\u2b1b\u2b1c"  # stars and squares that are emoji
    "\u231a\u231b\u23e9-\u23f3\u23f8-\u23fa"  # watch, hourglass, media controls, alarm clock
    "\u25aa\u25ab\u25b6\u25c0\u25fb-\u25fe"  # geometric shapes that are emoji
    "\u2934\u2935\u3030\u303d\u3297\u3299"
    "\ufe0e\ufe0f\u20e3"  # variation selectors, keycap
    "\U0001F3FB-\U0001F3FF"  # skin tones (inside the range above, kept for clarity)
    "\U000E0020-\U000E007F"  # tag characters (subdivision flags)
)
# The zero-width joiner (U+200D) glues emoji into one sequence, so it goes with them. Between letters it is meaningful
# (Malayalam, Devanagari, Sinhala conjuncts), so a joiner that does not touch an emoji is left alone.
EMOJI_RE = re.compile(f"[{_EMOJI_CHARS}]+(?:\u200d[{_EMOJI_CHARS}]*)*")

# ---- markdown ------------------------------------------------------------------------------------------------------
# Spans are bounded ({1,300}) so a hostile line of unmatched markers cannot make the scan quadratic.
# A code fence is a line that starts with 3+ backticks or tildes (up to 3 spaces of indent) and, for backticks, has no
# backtick in the rest of the line; it ends at a line holding only a fence at least as long. See _strip_fences.
FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
FENCE_CLOSE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*$")
INLINE_CODE_RE = re.compile(r"(?<!`)`++([^`\n]{1,300})`+")
IMAGE_RE = re.compile(r"!\[([^\]\n]{0,300})\]\([^)\n]{0,2000}\)")
LINK_RE = re.compile(r"\[([^\]\n]{1,300})\]\([^)\n]{0,2000}\)")
URL_RE = re.compile(r"(?i)\b(?:https?://|ftp://|www\.)[^\s<>\"'\u201c\u201d]+")
URL_TRAILING = ".,;:!?)]}>*_\u2026"
HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+([^\n]*)$", re.MULTILINE)
HEADING_MARK = "\x00"  # ends a heading line until _join_lines gives it a period
QUOTE_RE = re.compile(r"^[ \t]{0,3}(?:>[ \t]?)+", re.MULTILINE)
RULE_RE = re.compile(r"^[ \t]{0,3}(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.MULTILINE)
TABLE_SEP_RE = re.compile(r"^[ \t|:-]+$")
LIST_RE = re.compile(r"^[ \t]*(?:[-*+\u2022]|\d{1,3}[.)])[ \t]+")
BOLD_RE = re.compile(r"(\*\*|__)(?=\S)([^\n]{1,300}?)(?<=\S)\1")
# __init__, __main__, __post_init__: a Python name, not bold text. A lone word like __bold__ is still bold.
DUNDERS = frozenset(
    "init main name file all call str repr new del doc dict class slots version enter exit iter next len eq ne lt gt "
    "le ge hash bool getitem setitem delitem contains getattr setattr delattr dir format bytes int float add sub mul "
    "aenter aexit await aiter anext module qualname".split())
ITALIC_STAR_RE = re.compile(r"(?<![*\w])\*(?=[^\s*])([^\n*]{1,300}?)(?<=[^\s*])\*(?![*\w])")
ITALIC_UNDER_RE = re.compile(r"(?<![_\w])_(?=[^\s_])([^\n_]{1,300}?)(?<=[^\s_])_(?![_\w])")
STRIKE_RE = re.compile(r"~~(?=\S)([^\n]{1,300}?)(?<=\S)~~")
SPACE_RE = re.compile(r"\s+")
LINE_END_PUNCT = ".!?:;,\u2026\u3002\uff01\uff1f"


@dataclass(frozen=True)
class Normalized:
    text: str
    changed: bool  # the text differs from the trimmed input
    truncated: bool  # cut to EARSHOT_RELAY_MAX_WORDS


def _link(match: re.Match[str]) -> str:
    """A bare URL becomes the word "link"; punctuation that ended the sentence stays."""
    url = match.group(0)
    kept = len(url) - len(url.rstrip(URL_TRAILING))
    return "link" + (url[len(url) - kept:] if kept else "")


def _bold(match: re.Match[str]) -> str:
    if match.group(1) == "__" and (match.group(2) in DUNDERS or re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+", match.group(2))):
        return match.group(0)
    return match.group(2)


def _strip_fences(text: str) -> str:
    """Replace each closed code block with the word "code". An opener with no closer, and a ``` that sits mid-line
    (inline code), is left alone: the rest of the message is never swallowed. One pass over the lines; an opener that
    finds no closer makes every later opener of the same character and at least that length a dead end too, so the
    scan stays linear for practical input."""
    lines = text.split("\n")
    if "```" not in text and "~~~" not in text:
        return text
    out: list[str] = []
    dead: dict[str, int] = {}  # fence character -> shortest opener length known to have no closer
    i = 0
    while i < len(lines):
        opener = FENCE_OPEN_RE.match(lines[i])
        if opener:
            fence, info = opener.group(1), opener.group(2)
            char, size = fence[0], len(fence)
            if (char == "`" and "`" in info) or size >= dead.get(char, 1 << 30):
                opener = None
        if not opener:
            out.append(lines[i])
            i += 1
            continue
        end = None
        for j in range(i + 1, len(lines)):
            closer = FENCE_CLOSE_RE.match(lines[j])
            if closer and closer.group(1)[0] == char and len(closer.group(1)) >= size:
                end = j
                break
        if end is None:
            dead[char] = min(size, dead.get(char, 1 << 30))
            out.append(lines[i])
            i += 1
        else:
            out.append("code")
            i = end + 1
    return "\n".join(out)


def _strip_markdown(text: str) -> str:
    text = _strip_fences(text)
    text = IMAGE_RE.sub(lambda m: m.group(1), text)
    text = LINK_RE.sub(lambda m: m.group(1), text)
    text = INLINE_CODE_RE.sub(lambda m: m.group(1), text)
    text = HEADING_RE.sub(lambda m: m.group(1) + HEADING_MARK, text)
    text = RULE_RE.sub("", text)
    text = QUOTE_RE.sub("", text)
    text = BOLD_RE.sub(_bold, text)
    text = STRIKE_RE.sub(lambda m: m.group(1), text)
    text = ITALIC_STAR_RE.sub(lambda m: m.group(1), text)
    return ITALIC_UNDER_RE.sub(lambda m: m.group(1), text)


def _join_lines(text: str) -> str:
    """One line per list item, heading or table row becomes its own sentence, so the voice pauses between them; any
    other line break is just a space (a hard-wrapped paragraph is not several sentences)."""
    out: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        heading = line.endswith(HEADING_MARK)
        line = line.rstrip(HEADING_MARK).strip()
        if not line or ("|" in line and TABLE_SEP_RE.fullmatch(line)):
            continue
        item = LIST_RE.sub("", line, count=1)
        row = line.startswith("|")
        if row:
            item = ", ".join(c.strip() for c in line.strip("|").split("|") if c.strip())
        if (item != line or heading) and item and item[-1] not in LINE_END_PUNCT:
            item += "."
        out.append(item or line)
    return " ".join(out)


def _truncate_words(text: str, max_words: int) -> tuple[str, bool]:
    words = text.split(" ")
    if len(words) <= max_words:
        return text, False
    head = " ".join(words[:max_words])
    ends = [m.end() for m in re.finditer(rf"[{re.escape(SENTENCE_END)}](?=\s|$)", head)]
    if ends:
        return head[:ends[-1]], True
    return head.rstrip(",;:-\u2013\u2014 ") + ".", True


def normalize_text(raw: str, *, max_words: int | None = None) -> Normalized:
    """Strip emoji and markdown, say links as "link", collapse whitespace and optionally cut at `max_words`.

    Headings, list items and table rows each end in a period, so the voice pauses. A text with nothing left (only
    emoji, say) returns "" and the caller refuses it. `max_words` cuts at the last full sentence inside the limit,
    else at the limit with a trailing period.
    """
    original = raw.strip()
    if len(original.encode("utf-8", "surrogatepass")) > MAX_NORMALIZE_INPUT:
        raise ApiError(422, "text_too_long", f"text is over {MAX_NORMALIZE_INPUT} UTF-8 bytes", "text")
    text = original.replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = _strip_markdown(text)
    text = URL_RE.sub(_link, text)
    text = EMOJI_RE.sub(" ", text)
    text = _join_lines(text)
    text = SPACE_RE.sub(" ", text).strip()
    truncated = False
    if max_words and text:
        text, truncated = _truncate_words(text, max_words)
    return Normalized(text, text != original, truncated)


def check_tag(tag: object) -> str | None:
    if tag is None:
        return None
    if not isinstance(tag, str) or TAG_RE.fullmatch(tag) is None:
        raise ApiError(422, "invalid_tag", "tag must match ^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$", "tag")
    return tag


def check_seconds(name: str, value: object, maximum: float) -> float | None:
    """coalesce_s and cooldown_s: a number of seconds above 0 and at most `maximum`. null and 0 mean off."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(422, "invalid_field", f"{name} must be a number of seconds", name)
    if value != value or value < 0 or value > maximum:  # NaN, negative, too long
        raise ApiError(422, "invalid_field", f"{name} must be between 0 and {maximum:g} seconds", name)
    return float(value) or None


def check_flag(name: str, value: object) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ApiError(422, "invalid_field", f"{name} must be true or false", name)
    return value
