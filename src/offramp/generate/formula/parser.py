"""Salesforce formula language parser.

Recursive-descent parser for the subset needed to translate the validation
rules and formula fields the fixture org exercises. Coverage:

* literals: number, string, TRUE, FALSE, NULL
* identifiers + dotted refs (Account.Owner.Email)
* unary +/- and NOT
* arithmetic: + - * / and parens
* comparison: = <> != < > <= >=
* logical: && || (also AND/OR/NOT as functions)
* function calls: ISBLANK, ISNULL, IF, AND, OR, NOT, NOT_, LEN, UPPER, LOWER,
  TRIM, LEFT, RIGHT, MID, SUBSTITUTE, FIND, BEGINS, CONTAINS, ISPICKVAL, TODAY,
  NOW, DATE, ADDMONTHS, MAX, MIN, ROUND, FLOOR, CEILING, MOD, ABS, TEXT,
  VALUE, CASE, BLANKVALUE

Anything outside this subset raises :class:`UnsupportedFormulaError` —
consumers (Translation Verification, Tier 1 translator) treat that as a
finding rather than silently degrading.

This is the **deterministic** parser called out in the v2.1 plan §9.4.1:
no LLM fallback, ever. Hallucinated formula translations are the failure
mode this module is built against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class UnsupportedFormulaError(ValueError):
    """The formula uses syntax not yet handled by the parser."""


class TokenKind(StrEnum):
    NUMBER = "NUMBER"
    STRING = "STRING"
    IDENT = "IDENT"
    DOT = "DOT"
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    COMMA = "COMMA"
    PLUS = "PLUS"
    MINUS = "MINUS"
    STAR = "STAR"
    SLASH = "SLASH"
    EQ = "EQ"
    NEQ = "NEQ"
    LT = "LT"
    LE = "LE"
    GT = "GT"
    GE = "GE"
    AND = "AND"  # &&
    OR = "OR"  # ||
    BANG = "BANG"  # !
    EOF = "EOF"


@dataclass(frozen=True)
class Token:
    kind: TokenKind
    text: str
    pos: int


_TOKEN_RE = re.compile(
    r"""
    \s+
    | (?P<NUMBER>\d+(?:\.\d+)?)
    | (?P<STRING>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
    | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<NEQ><>|!=)
    | (?P<LE><=)
    | (?P<GE>>=)
    | (?P<EQ>=)
    | (?P<LT><)
    | (?P<GT>>)
    | (?P<AND>&&)
    | (?P<OR>\|\|)
    | (?P<BANG>!)
    | (?P<DOT>\.)
    | (?P<LPAREN>\()
    | (?P<RPAREN>\))
    | (?P<COMMA>,)
    | (?P<PLUS>\+)
    | (?P<MINUS>-)
    | (?P<STAR>\*)
    | (?P<SLASH>/)
    """,
    re.VERBOSE,
)


def tokenize(source: str) -> list[Token]:
    """Lex a formula into tokens. Raises :class:`UnsupportedFormulaError` on
    any character we don't recognize."""
    tokens: list[Token] = []
    pos = 0
    while pos < len(source):
        m = _TOKEN_RE.match(source, pos)
        if m is None:
            raise UnsupportedFormulaError(
                f"unexpected character at position {pos}: {source[pos]!r}"
            )
        if m.lastgroup is None:
            pos = m.end()
            continue
        tokens.append(Token(kind=TokenKind(m.lastgroup), text=m.group(), pos=pos))
        pos = m.end()
    tokens.append(Token(kind=TokenKind.EOF, text="", pos=pos))
    return tokens


# AST nodes — kept as plain dataclasses so the emitter can pattern-match
# cheaply via match/case.


@dataclass(frozen=True)
class NumberLit:
    value: float


@dataclass(frozen=True)
class StringLit:
    value: str


@dataclass(frozen=True)
class BoolLit:
    value: bool


@dataclass(frozen=True)
class NullLit:
    pass


@dataclass(frozen=True)
class Ident:
    name: str


@dataclass(frozen=True)
class FieldRef:
    """Dotted reference like Account.Owner.Email."""

    parts: tuple[str, ...]


@dataclass(frozen=True)
class UnaryOp:
    op: str  # '-' | 'NOT'
    operand: Node


@dataclass(frozen=True)
class BinaryOp:
    op: str  # '+' '-' '*' '/' '=' '<>' '<' '<=' '>' '>=' 'AND' 'OR'
    left: Node
    right: Node


@dataclass(frozen=True)
class FuncCall:
    name: str  # uppercased
    args: tuple[Node, ...] = field(default_factory=tuple)


Node = NumberLit | StringLit | BoolLit | NullLit | Ident | FieldRef | UnaryOp | BinaryOp | FuncCall


_KNOWN_FUNCTIONS: frozenset[str] = frozenset(
    {
        "ISBLANK",
        "ISNULL",
        "IF",
        "AND",
        "OR",
        "NOT",
        "LEN",
        "UPPER",
        "LOWER",
        "TRIM",
        "LEFT",
        "RIGHT",
        "MID",
        "SUBSTITUTE",
        "FIND",
        "BEGINS",
        "CONTAINS",
        "ISPICKVAL",
        "TODAY",
        "NOW",
        "DATE",
        "ADDMONTHS",
        "MAX",
        "MIN",
        "ROUND",
        "FLOOR",
        "CEILING",
        "MOD",
        "ABS",
        "TEXT",
        "VALUE",
        "CASE",
        "BLANKVALUE",
        "NULLVALUE",
    }
)


@dataclass
class _Parser:
    tokens: list[Token]
    i: int = 0

    def peek(self) -> Token:
        return self.tokens[self.i]

    def take(self) -> Token:
        t = self.tokens[self.i]
        self.i += 1
        return t

    def expect(self, kind: TokenKind) -> Token:
        t = self.peek()
        if t.kind is not kind:
            raise UnsupportedFormulaError(
                f"expected {kind} at position {t.pos}, got {t.kind} ({t.text!r})"
            )
        return self.take()

    def parse_expr(self) -> Node:
        return self._parse_or()

    # OR (lowest precedence) → AND → comparison → additive → multiplicative
    # → unary → postfix (function call / dotted ref) → primary

    def _parse_or(self) -> Node:
        left = self._parse_and()
        while self.peek().kind is TokenKind.OR:
            self.take()
            right = self._parse_and()
            left = BinaryOp("OR", left, right)
        return left

    def _parse_and(self) -> Node:
        left = self._parse_cmp()
        while self.peek().kind is TokenKind.AND:
            self.take()
            right = self._parse_cmp()
            left = BinaryOp("AND", left, right)
        return left

    def _parse_cmp(self) -> Node:
        left = self._parse_add()
        if self.peek().kind in {
            TokenKind.EQ,
            TokenKind.NEQ,
            TokenKind.LT,
            TokenKind.LE,
            TokenKind.GT,
            TokenKind.GE,
        }:
            tok = self.take()
            right = self._parse_add()
            return BinaryOp(tok.text, left, right)
        return left

    def _parse_add(self) -> Node:
        left = self._parse_mul()
        while self.peek().kind in {TokenKind.PLUS, TokenKind.MINUS}:
            tok = self.take()
            right = self._parse_mul()
            left = BinaryOp(tok.text, left, right)
        return left

    def _parse_mul(self) -> Node:
        left = self._parse_unary()
        while self.peek().kind in {TokenKind.STAR, TokenKind.SLASH}:
            tok = self.take()
            right = self._parse_unary()
            left = BinaryOp(tok.text, left, right)
        return left

    def _parse_unary(self) -> Node:
        if self.peek().kind is TokenKind.MINUS:
            self.take()
            return UnaryOp("-", self._parse_unary())
        if self.peek().kind is TokenKind.PLUS:
            self.take()
            return self._parse_unary()
        if self.peek().kind is TokenKind.BANG:
            self.take()
            return UnaryOp("NOT", self._parse_unary())
        return self._parse_postfix()

    def _parse_postfix(self) -> Node:
        node = self._parse_primary()
        # Dotted references like Account.Owner.Email — only valid when the
        # head is an Ident.
        if isinstance(node, Ident):
            parts = [node.name]
            while self.peek().kind is TokenKind.DOT:
                self.take()
                t = self.expect(TokenKind.IDENT)
                parts.append(t.text)
            if len(parts) > 1:
                return FieldRef(tuple(parts))
        return node

    def _parse_primary(self) -> Node:
        t = self.peek()
        if t.kind is TokenKind.NUMBER:
            self.take()
            return NumberLit(float(t.text))
        if t.kind is TokenKind.STRING:
            self.take()
            return StringLit(_decode_string(t.text))
        if t.kind is TokenKind.LPAREN:
            self.take()
            node = self.parse_expr()
            self.expect(TokenKind.RPAREN)
            return node
        if t.kind is TokenKind.IDENT:
            name = t.text
            up = name.upper()
            if up in {"TRUE", "FALSE"}:
                self.take()
                return BoolLit(up == "TRUE")
            if up == "NULL":
                self.take()
                return NullLit()
            self.take()
            # Function call?
            if self.peek().kind is TokenKind.LPAREN:
                self.take()
                args: list[Node] = []
                if self.peek().kind is not TokenKind.RPAREN:
                    args.append(self.parse_expr())
                    while self.peek().kind is TokenKind.COMMA:
                        self.take()
                        args.append(self.parse_expr())
                self.expect(TokenKind.RPAREN)
                if up not in _KNOWN_FUNCTIONS:
                    raise UnsupportedFormulaError(f"unknown function {name}()")
                return FuncCall(up, tuple(args))
            return Ident(name)
        raise UnsupportedFormulaError(f"unexpected token {t.kind} ({t.text!r}) at position {t.pos}")


def _decode_string(literal: str) -> str:
    """Strip surrounding quotes and resolve backslash escapes."""
    body = literal[1:-1]
    return body.encode("utf-8").decode("unicode_escape")


def parse(source: str) -> Node:
    """Parse ``source`` into an AST. Raises :class:`UnsupportedFormulaError`."""
    tokens = tokenize(source)
    parser = _Parser(tokens=tokens)
    node = parser.parse_expr()
    if parser.peek().kind is not TokenKind.EOF:
        rest = parser.peek()
        raise UnsupportedFormulaError(f"trailing input at position {rest.pos}: {rest.text!r}")
    return node
