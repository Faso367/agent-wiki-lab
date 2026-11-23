"""Bounded, addressable excerpts for both arms of the read pilot."""
from collections import Counter
import math
import re

from materials import VisibleText

READ_CHARS = 1800
SNIPPET_CHARS = 220
STOP = set("a an the and are that this what when where who how why which with from for into about was were have has can will should must based all per any each".split())


class CloudText(VisibleText):
    """The imported HTML stores paragraph boundaries in p and individual lines in br."""

    def handle_starttag(self, tag, attrs):
        if tag == "br" and not self.ignored:
            self.parts.append("\n")
        else:
            super().handle_starttag(tag, attrs)


def blocks(body):
    # Keep list/table rows together and split an oversized paragraph at a line or word.
    result = []
    for para in re.split(r"\n\s*\n", body.strip()):
        value = para.strip()
        while len(value) > READ_CHARS:
            end = value.rfind("\n", 1, READ_CHARS + 1)
            if end == -1:
                end = value.rfind(" ", 1, READ_CHARS + 1)
            if end == -1:
                end = READ_CHARS
            result.append(value[:end])
            value = value[end:].lstrip()
        if value:
            result.append(value)
    return result


def terms(text):
    return set(re.findall(r"[^\W_]+", text.casefold())) - STOP


def ranked(parts, query):
    words = terms(query)
    matches = [words & terms(part) for part in parts]
    frequencies = Counter(word for overlap in matches for word in overlap)
    weights = {word: math.log1p(len(parts) / freq) for word, freq in frequencies.items()}
    return sorted(range(len(parts)), key=lambda i: -sum(weights[word] for word in sorted(matches[i])))


def snippet(body, query):
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    order = ranked(lines, query)
    return lines[order[0]][:SNIPPET_CHARS] if order else ""


def anchor_phrase(part, page_text):
    # Prefer a unique phrase on one rendered line. A short heading can occur several times.
    for line in part.splitlines():
        words = line.split()
        for size in (12, 24):
            phrase = " ".join(words[:size])
            if phrase and page_text.count(phrase.casefold()) == 1:
                return phrase
    return None


def window(parts, query, cursor=None):
    order = ranked(parts, query)
    start = cursor if cursor is not None else next(iter(order), 0)
    if type(start) is not int or start < 0 or (parts and start >= len(parts)) or (not parts and start):
        raise ValueError("invalid_cursor")
    end, remaining = start, READ_CHARS
    while end < len(parts) and len(parts[end]) <= remaining:
        remaining -= len(parts[end]) + 1
        end += 1
    return range(start, end), {
        "next_cursor": end if end < len(parts) else None,
        "matches": [{"cursor": i, "heading": parts[i].splitlines()[0][:100]} for i in order[:4]],
    }
