"""Apex tokenizer.

Strips comments, isolates string literals, and yields a flat token stream
with positions. SOQL/SOSL bracket blocks ``[ SELECT ... ]`` are captured as a
single ``SOQL`` token so the reference pass can parse them separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    IDENT = "ident"
    STRING = "string"
    NUMBER = "number"
    ANNOTATION = "annotation"  # @AuraEnabled
    SOQL = "soql"  # [SELECT ...] / [FIND ...]
    PUNCT = "punct"
    EOF = "eof"


@dataclass(frozen=True)
class Tok:
    kind: Kind
    text: str
    pos: int
    line: int

    def lower(self) -> str:
        return self.text.lower()


_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_COMMENT_LINE = re.compile(r"//[^\n]*")

_TOKEN = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<string>'(?:\\.|[^'\\\n])*')
    | (?P<annotation>@[A-Za-z_][A-Za-z0-9_]*)
    | (?P<number>\d+(?:\.\d+)?[LlDd]?)
    | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<punct>==|!=|<=|>=|&&|\|\||\+\+|--|=>|[-+*/%=<>!&|^~?:;,.(){}\[\]])
    """,
    re.VERBOSE,
)


def strip_comments(source: str) -> str:
    """Replace comments with spaces (keeping newlines so line numbers hold)."""

    def _blank(m: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", m.group())

    return _COMMENT_LINE.sub(_blank, _COMMENT_BLOCK.sub(_blank, source))


def tokenize(source: str) -> list[Tok]:
    """Tokenize Apex source. Unknown characters are skipped, never fatal."""
    text = strip_comments(source)
    out: list[Tok] = []
    pos = 0
    line = 1
    n = len(text)
    while pos < n:
        ch = text[pos]
        # SOQL / SOSL block: '[' followed (after whitespace) by SELECT or FIND.
        if ch == "[":
            m = re.match(r"\[\s*(select|find)\b", text[pos:], re.I)
            if m:
                end = _match_bracket(text, pos)
                out.append(Tok(Kind.SOQL, text[pos + 1 : end], pos, line))
                line += text.count("\n", pos, end + 1)
                pos = end + 1
                continue
        m = _TOKEN.match(text, pos)
        if m is None:
            pos += 1
            continue
        kind = m.lastgroup
        raw = m.group()
        if kind == "ws":
            line += raw.count("\n")
        elif kind is not None:
            out.append(Tok(Kind(kind), raw, pos, line))
        pos = m.end()
    out.append(Tok(Kind.EOF, "", pos, line))
    return out


def _match_bracket(text: str, start: int) -> int:
    """Index of the ']' closing the bracket at ``start`` (strings respected)."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == "'":
            j = i + 1
            while j < n and text[j] != "'":
                j += 2 if text[j] == "\\" else 1
            i = j + 1
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1
