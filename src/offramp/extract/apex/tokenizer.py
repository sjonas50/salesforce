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
    """Replace comments with spaces, leaving string literals and newlines intact.

    A single left-to-right pass so that ``'http://x'`` or ``'a /* b'`` inside a
    string literal is never mistaken for a comment.
    """
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "'":
            j = i + 1
            while j < n and source[j] != "'" and source[j] != "\n":
                j += 2 if source[j] == "\\" else 1
            out.append(source[i : j + 1])
            i = j + 1
        elif source.startswith("//", i):
            j = source.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
        elif source.startswith("/*", i):
            j = source.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append("".join("\n" if c == "\n" else " " for c in source[i:j]))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


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
