"""Grammar-backed Apex analysis (AD-31, grammar path).

Walks the parse tree produced by :mod:`offramp.extract.apex.ast_bridge`
(Salesforce's ANTLR grammar) and fills the same :class:`ApexAnalysis`
contract as the tokenizer in :mod:`offramp.extract.apex.references`. Where the
tokenizer guesses from token adjacency, this walker knows:

* which identifiers are *declared* (fields, properties, parameters, locals,
  loop and catch variables) and their types, with block scoping, so
  ``address.MailingCity__c`` resolves to the variable's type rather than the
  standard ``Address`` object;
* which names are the class's own members, inner types and enum constants,
  so they are never reported as references to other classes;
* the exact shape of every DML, ``new``, cast, ``instanceof`` and
  ``Database.*`` call.

Vocabulary (platform namespaces, entry-point annotations, SOQL parsing) is
shared with the tokenizer so both engines classify names identically; only the
recognition is different.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from offramp.extract.apex.ast_bridge import Node
from offramp.extract.apex.model import ApexAnalysis, AsyncRef, DmlRef, SoqlRef
from offramp.extract.apex.references import (
    _ASYNC,
    _CALLOUT_TYPES,
    _DATABASE_DML,
    _DYNAMIC_QUERY,
    _KEYWORDS,
    _SETTING_ACCESSORS,
    _SYSTEM_NAMESPACES,
    _derive_entry_points,
    _finalize,
    _fold,
    _is_org_type,
    _parse_soql,
    _Refs,
    is_sobject_name,
)

_MODIFIERS = {"global", "public", "private", "protected", "abstract", "virtual"}
_COLLECTIONS = {"list", "set", "map"}
_BRANCH_RULES = {
    "IfStatement",
    "ForStatement",
    "WhileStatement",
    "DoWhileStatement",
    "SwitchStatement",
    "CatchClause",
    "CondExpression",
}
_TRIGGER_CONTEXT = {"new", "old", "newmap", "oldmap"}
_ELEMENT_ACCESSORS = {"get", "remove"}  # collection.get(k) yields an element


# ---- tree helpers -----------------------------------------------------------


def _rule(n: Any) -> str:
    return str(n[0]) if isinstance(n, list) and n else ""


def _rules(n: Node, name: str) -> list[Node]:
    return [c for c in n[1:] if isinstance(c, list) and c and c[0] == name]


def _first(n: Node, name: str) -> Node | None:
    for c in n[1:]:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def _terms(n: Node) -> list[str]:
    return [c for c in n[1:] if isinstance(c, str)]


def _lists(n: Node) -> list[Node]:
    return [c for c in n[1:] if isinstance(c, list)]


def _text(n: Any) -> str:
    """All terminals of a subtree, concatenated (identifiers joined with dots)."""
    if isinstance(n, str):
        return n
    if not isinstance(n, list):
        return ""
    parts = [_text(c) for c in n[1:]]
    if n and n[0] in {"TypeRef", "QualifiedName", "CreatedName"}:
        return ".".join(p for p in parts if p)
    return "".join(parts)


def _walk(n: Any) -> Iterator[Node]:
    if isinstance(n, list) and n:
        yield n
        for c in n[1:]:
            yield from _walk(c)


def _ident(n: Node | None) -> str:
    """Text of an ``Id`` / ``AnyId`` node."""
    if n is None:
        return ""
    terms = _terms(n)
    return terms[0] if terms else ""


# ---- types ------------------------------------------------------------------


@dataclass(frozen=True)
class _Type:
    name: str  # dotted, without generics: 'Lead', 'List', 'Database.QueryLocator'
    args: tuple[_Type, ...] = ()
    array: bool = False

    @property
    def is_collection(self) -> bool:
        return self.array or self.name.lower() in _COLLECTIONS

    @property
    def element(self) -> _Type:
        """``List<Lead>`` → ``Lead``; ``Map<Id, Account>`` → ``Account``; ``Lead[]`` → ``Lead``."""
        if self.array:
            return _Type(self.name, self.args)
        if self.name.lower() in _COLLECTIONS and self.args:
            return self.args[-1].element if self.args[-1].is_collection else self.args[-1]
        return self


def _type_ref(n: Node) -> _Type:
    """``TypeRef`` → ``_Type`` (``TypeName`` (``.`` ``TypeName``)* ``ArraySubscripts``)."""
    names: list[str] = []
    args: tuple[_Type, ...] = ()
    for tn in _rules(n, "TypeName"):
        ident = _first(tn, "Id")
        names.append(_ident(ident) if ident is not None else (_terms(tn) or [""])[0])
        ta = _first(tn, "TypeArguments")
        if ta is not None:
            tl = _first(ta, "TypeList")
            if tl is not None:
                args = tuple(_type_ref(t) for t in _rules(tl, "TypeRef"))
    subs = _first(n, "ArraySubscripts")
    array = bool(subs is not None and _terms(subs))
    return _Type(".".join(x for x in names if x), args, array)


def _created_type(creator: Node) -> _Type:
    """``Creator`` of a ``new`` expression → ``_Type``."""
    cn = _first(creator, "CreatedName")
    names: list[str] = []
    args: tuple[_Type, ...] = ()
    if cn is not None:
        for pair in _rules(cn, "IdCreatedNamePair"):
            names.append(_ident(_first(pair, "AnyId")))
            tl = _first(pair, "TypeList")
            if tl is not None:
                args = tuple(_type_ref(t) for t in _rules(tl, "TypeRef"))
    array = _first(creator, "ArrayCreatorRest") is not None
    return _Type(".".join(names), args, array)


# ---- walker -----------------------------------------------------------------


class _Walker:
    def __init__(self, a: ApexAnalysis) -> None:
        self.a = a
        self.r = _Refs()
        self.scopes: list[dict[str, _Type]] = [{}]
        self.own: set[str] = set()  # lower-case member / inner type / enum constant names
        self.own_types: set[str] = set()  # lower-case class name + inner type names
        self.trigger_object: str | None = None
        self.depth = 0  # type-declaration nesting

    # -- scopes ---------------------------------------------------------------

    def _push(self) -> None:
        self.scopes.append({})

    def _pop(self) -> None:
        self.scopes.pop()

    def _declare(self, name: str, typ: _Type) -> None:
        if name:
            self.scopes[-1][name.lower()] = typ

    def _lookup(self, name: str) -> _Type | None:
        low = name.lower()
        for scope in reversed(self.scopes):
            if low in scope:
                return scope[low]
        return None

    # -- dispatch -------------------------------------------------------------

    def visit(self, n: Any) -> None:
        if isinstance(n, str):
            self._terminal(n)
            return
        if not isinstance(n, list) or not n:
            return
        rule = n[0]
        if rule in _BRANCH_RULES:
            self.a.branches += 1
        handler = getattr(self, f"_v_{rule}", None)
        if handler is not None:
            handler(n)
        else:
            self._children(n)

    def _children(self, n: Node) -> None:
        for c in n[1:]:
            self.visit(c)

    def _terminal(self, text: str) -> None:
        if text.startswith("'") and text.lower().startswith("'callout:"):
            lit = text[1:-1]
            self.r.named_creds.add(lit.split(":", 1)[1].split("/", 1)[0])

    # -- declarations ---------------------------------------------------------

    def _v_TypeDeclaration(self, n: Node) -> None:
        # Top-level header: modifiers + annotations + the declaration itself.
        for mod in _rules(n, "Modifier"):
            terms = [t.lower() for t in _terms(mod)]
            ann = _first(mod, "Annotation")
            if ann is not None:
                name = self._annotation_name(ann)
                self.a.annotations.append(name)
                if name.lower() == "istest":
                    self.a.is_test = True
            elif terms and terms[0] in {"with", "without", "inherited"} and "sharing" in terms:
                self.a.sharing = terms[0]
            elif terms and terms[0] in _MODIFIERS:
                self.a.modifiers.append(terms[0])
        for decl in _lists(n):
            if _rule(decl) != "Modifier":
                self.visit(decl)

    def _annotation_name(self, ann: Node) -> str:
        qn = _first(ann, "QualifiedName")
        return _text(qn) if qn is not None else _ident(_first(ann, "Id"))

    def _v_Annotation(self, n: Node) -> None:
        self.r.annotations.append(self._annotation_name(n))

    def _type_header(self, n: Node, kind: str) -> str:
        name = _ident(_first(n, "Id"))
        if self.depth == 0:
            self.a.kind = kind
            self.a.name = name
        else:
            self.a.inner_types.append(name)
        self.own.add(name.lower())
        self.own_types.add(name.lower())
        return name

    def _v_ClassDeclaration(self, n: Node) -> None:
        self._type_header(n, "class")
        terms = [t.lower() for t in _terms(n)]
        ext = _first(n, "TypeRef")
        if ext is not None and "extends" in terms:
            t = _type_ref(ext)
            if self.depth == 0:
                self.a.extends = t.name
            else:
                self._classify(_Type(t.name))
            for arg in t.args:
                self._classify(arg)
        tl = _first(n, "TypeList")
        if tl is not None:
            for tr in _rules(tl, "TypeRef"):
                t = _type_ref(tr)
                if self.depth == 0:
                    self.a.implements.append(t.name)
                else:
                    self._classify(_Type(t.name))
                for arg in t.args:
                    self._classify(arg)
        body = _first(n, "ClassBody")
        if body is not None:
            self._type_body(body)

    def _v_InterfaceDeclaration(self, n: Node) -> None:
        self._type_header(n, "interface")
        tl = _first(n, "TypeList")
        if tl is not None:
            for tr in _rules(tl, "TypeRef"):
                t = _type_ref(tr)
                if self.depth == 0:
                    self.a.extends = self.a.extends or t.name
                    self.a.implements.append(t.name)
                else:
                    self._classify(_Type(t.name))
        body = _first(n, "InterfaceBody")
        if body is not None:
            self._type_body(body)

    def _v_EnumDeclaration(self, n: Node) -> None:
        self._type_header(n, "enum")
        consts = _first(n, "EnumConstants")
        if consts is not None:
            for ident in _rules(consts, "Id"):
                self.own.add(_ident(ident).lower())

    def _type_body(self, body: Node) -> None:
        """Pre-register member names (Apex members are visible before their declaration).

        Fields, properties and methods of *this* body only go into scope; nested
        type names and enum constants are collected from the whole subtree because
        the outer class can name them bare (``Kind.A``, ``new Wrapper()``).
        """
        self.depth += 1
        self._push()
        for member in _walk(body):
            rule = _rule(member)
            if rule in {"ClassDeclaration", "InterfaceDeclaration", "EnumDeclaration"}:
                self.own.add(_ident(_first(member, "Id")).lower())
                self.own_types.add(_ident(_first(member, "Id")).lower())
                consts = _first(member, "EnumConstants")
                if consts is not None:
                    for ident in _rules(consts, "Id"):
                        self.own.add(_ident(ident).lower())
        for member in self._direct_members(body):
            rule = _rule(member)
            if rule == "FieldDeclaration":
                typ = self._field_type(member)
                vds = _first(member, "VariableDeclarators")
                for vd in _rules(vds, "VariableDeclarator") if vds is not None else []:
                    name = _ident(_first(vd, "Id"))
                    self.own.add(name.lower())
                    if typ is not None:
                        self._declare(name, typ)
            elif rule == "PropertyDeclaration":
                typ = self._field_type(member)
                name = _ident(_first(member, "Id"))
                self.own.add(name.lower())
                if typ is not None:
                    self._declare(name, typ)
            elif rule in {"MethodDeclaration", "InterfaceMethodDeclaration"}:
                self.own.add(_ident(_first(member, "Id")).lower())
        self._children(body)
        self._pop()
        self.depth -= 1

    @staticmethod
    def _direct_members(body: Node) -> list[Node]:
        """Member declarations of one class/interface body, not of nested types."""
        out: list[Node] = []
        for decl in _lists(body):
            if _rule(decl) == "ClassBodyDeclaration":
                md = _first(decl, "MemberDeclaration")
                if md is not None and _lists(md):
                    out.append(_lists(md)[0])
            elif _rule(decl) == "InterfaceMethodDeclaration":
                out.append(decl)
        return out

    @staticmethod
    def _field_type(member: Node) -> _Type | None:
        tr = _first(member, "TypeRef")
        return _type_ref(tr) if tr is not None else None

    def _v_ClassBodyDeclaration(self, n: Node) -> None:
        for mod in _rules(n, "Modifier"):
            ann = _first(mod, "Annotation")
            if ann is not None:
                self.r.annotations.append(self._annotation_name(ann))
        for c in _lists(n):
            if _rule(c) != "Modifier":
                self.visit(c)

    def _v_FieldDeclaration(self, n: Node) -> None:
        typ = self._field_type(n)
        if typ is not None:
            self._classify(typ)
        vds = _first(n, "VariableDeclarators")
        if vds is not None:
            self._declarators(vds, typ)

    def _v_PropertyDeclaration(self, n: Node) -> None:
        typ = self._field_type(n)
        if typ is not None:
            self._classify(typ)
        block = _first(n, "PropertyBlock")
        if block is not None:
            self._push()
            if typ is not None:
                self._declare("value", typ)  # implicit setter argument
            self._children(block)
            self._pop()

    def _v_LocalVariableDeclaration(self, n: Node) -> None:
        typ = self._field_type(n)
        if typ is not None:
            self._classify(typ)
        vds = _first(n, "VariableDeclarators")
        if vds is not None:
            self._declarators(vds, typ)

    def _declarators(self, vds: Node, typ: _Type | None) -> None:
        for vd in _rules(vds, "VariableDeclarator"):
            name = _ident(_first(vd, "Id"))
            if typ is not None:
                self._declare(name, typ)
            for c in _lists(vd):
                if _rule(c) != "Id":
                    self.visit(c)

    def _method(self, n: Node) -> None:
        self.a.methods += 1
        tr = _first(n, "TypeRef")
        if tr is not None:
            self._classify(_type_ref(tr))
        self._push()
        params = _first(n, "FormalParameters")
        if params is not None:
            for fp in _walk(params):
                if _rule(fp) == "FormalParameter":
                    ptr = _first(fp, "TypeRef")
                    if ptr is not None:
                        ptype = _type_ref(ptr)
                        self._classify(ptype)
                        self._declare(_ident(_first(fp, "Id")), ptype)
        block = _first(n, "Block")
        if block is not None:
            self._children(block)  # the method scope doubles as the block scope
        self._pop()

    _v_MethodDeclaration = _method
    _v_ConstructorDeclaration = _method
    _v_InterfaceMethodDeclaration = _method

    def _v_Block(self, n: Node) -> None:
        self._push()
        self._children(n)
        self._pop()

    def _v_ForStatement(self, n: Node) -> None:
        self._push()
        ctl = _first(n, "ForControl")
        if ctl is not None:
            enhanced = _first(ctl, "EnhancedForControl")
            if enhanced is not None:
                tr = _first(enhanced, "TypeRef")
                if tr is not None:
                    t = _type_ref(tr)
                    self._classify(t)
                    self._declare(_ident(_first(enhanced, "Id")), t)
                for c in _lists(enhanced):
                    if _rule(c) not in {"TypeRef", "Id"}:
                        self.visit(c)
            else:
                self._children(ctl)
        for c in _lists(n):
            if _rule(c) != "ForControl":
                self.visit(c)
        self._pop()

    def _v_CatchClause(self, n: Node) -> None:
        self._push()
        qn = _first(n, "QualifiedName")
        if qn is not None:
            t = _Type(_text(qn))
            self._classify(t)
            self._declare(_ident(_first(n, "Id")), t)
        block = _first(n, "Block")
        if block is not None:
            self._children(block)
        self._pop()

    def _v_WhenValue(self, n: Node) -> None:
        # ``when AfterInsert`` / ``when 'x'``: enum constants or literals, never references.
        return

    def _v_TriggerUnit(self, n: Node) -> None:
        self.a.kind = "trigger"
        ids = _rules(n, "Id")
        if ids:
            self.a.name = _ident(ids[0])
        if len(ids) > 1:
            self.a.trigger_object = _ident(ids[1])
            self.trigger_object = self.a.trigger_object
        for case in _rules(n, "TriggerCase"):
            self.a.trigger_events.append(" ".join(t.lower() for t in _terms(case)))
        block = _first(n, "TriggerBlock")
        if block is not None:
            self._push()
            self.depth += 1
            self._children(block)
            self.depth -= 1
            self._pop()

    # -- statements -----------------------------------------------------------

    def _dml(self, op: str, n: Node) -> None:
        exprs = _lists(n)
        if not exprs:
            return
        target = exprs[0]
        if _rule(target) == "PrimaryExpression" and _first(target, "SoqlPrimary") is not None:
            lit = _first(_first(target, "SoqlPrimary") or [], "SoqlLiteral")
            sobject = _parse_soql(self._soql_text(lit)).sobject if lit is not None else None
            self.a.dml.append(DmlRef(op=op, target="[SOQL]", sobject=sobject or None))
        else:
            self.a.dml.append(
                DmlRef(op=op, target=_text(target)[:80], sobject=self._sobject_of(target))
            )
        if op == "upsert":
            qn = _first(n, "QualifiedName")
            if qn is not None:
                ext = _text(qn)
                if "." in ext and is_sobject_name(ext.split(".", 1)[0]):
                    self.r.fields.add(ext)
        self._children(n)

    def _v_InsertStatement(self, n: Node) -> None:
        self._dml("insert", n)

    def _v_UpdateStatement(self, n: Node) -> None:
        self._dml("update", n)

    def _v_DeleteStatement(self, n: Node) -> None:
        self._dml("delete", n)

    def _v_UndeleteStatement(self, n: Node) -> None:
        self._dml("undelete", n)

    def _v_UpsertStatement(self, n: Node) -> None:
        self._dml("upsert", n)

    def _v_MergeStatement(self, n: Node) -> None:
        self._dml("merge", n)

    # -- expressions ----------------------------------------------------------

    def _v_SoqlLiteral(self, n: Node) -> None:
        self.a.soql.append(_parse_soql(self._soql_text(n)))

    _v_SoslLiteral = _v_SoqlLiteral

    @staticmethod
    def _soql_text(n: Node) -> str:
        raw = _terms(n)[0] if _terms(n) else ""
        raw = raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            raw = raw[1:-1]
        return raw.strip()

    def _v_CastExpression(self, n: Node) -> None:
        tr = _first(n, "TypeRef")
        if tr is not None:
            self._classify(_type_ref(tr))
        for c in _lists(n):
            if _rule(c) != "TypeRef":
                self.visit(c)

    _v_InstanceOfExpression = _v_CastExpression

    def _v_TypeRefPrimary(self, n: Node) -> None:
        tr = _first(n, "TypeRef")
        if tr is not None:
            self._classify(_type_ref(tr))

    def _v_TypeRef(self, n: Node) -> None:
        # A type in a position not covered above (e.g. generic method argument).
        self._classify(_type_ref(n))

    def _v_IdPrimary(self, n: Node) -> None:
        # A bare identifier is a variable, constant or enum value — never a type.
        return

    def _v_MethodCall(self, n: Node) -> None:
        # ``foo(args)`` / ``this(args)`` / ``super(args)``: own method; only args matter.
        for c in _lists(n):
            if _rule(c) != "Id":
                self.visit(c)

    def _v_NewExpression(self, n: Node) -> None:
        creator = _first(n, "Creator")
        if creator is None:
            self._children(n)
            return
        typ = _created_type(creator)
        low = typ.name.lower()
        for arg in typ.args:
            self._classify(arg)
        rest = _first(creator, "ClassCreatorRest")
        if is_sobject_name(typ.name) and not typ.array:
            self.r.sobjects.add(typ.name)
            pairs = self._named_args(rest) if rest is not None else []
            for fname, _ in pairs:
                self.r.fields.add(f"{typ.name}.{fname}")
                self.r.field_writes.add(f"{typ.name}.{fname}")
            # A dispatch table defined in code: ``new Trigger_Handler__c(Class__c='X',
            # Object__c='Account', Trigger_Action__c='AfterInsert')`` (TDTM, NPSP/EDA).
            names = {f.split("__", 1)[-1] if "__" in f[:-3] else f: v for f, v in pairs}
            if names.get("Class__c") and names.get("Object__c"):
                self.r.dispatch_rows.append(
                    {"sobject": typ.name, **{f: v for f, v in pairs if v is not None}}
                )
        elif low in _CALLOUT_TYPES:
            self.r.callouts.add(typ.name)
        elif not typ.is_collection:
            self._classify(_Type(typ.name))
        # Visit argument values (not the named-argument field names).
        if rest is not None:
            for value in self._arguments(rest):
                if _rule(value) == "AssignExpression" and is_sobject_name(typ.name):
                    for c in _lists(value)[1:]:
                        self.visit(c)
                else:
                    self.visit(value)
        for c in _lists(creator):
            if _rule(c) not in {"CreatedName", "ClassCreatorRest"}:
                self.visit(c)

    @staticmethod
    def _arguments(rest: Node) -> list[Node]:
        args = _first(rest, "Arguments")
        if args is None:
            return []
        el = _first(args, "ExpressionList")
        return _lists(el) if el is not None else []

    def _named_args(self, rest: Node) -> list[tuple[str, str | None]]:
        out: list[tuple[str, str | None]] = []
        for arg in self._arguments(rest):
            if _rule(arg) != "AssignExpression":
                continue
            parts = _lists(arg)
            if len(parts) < 2:
                continue
            lhs = parts[0]
            if _rule(lhs) != "PrimaryExpression" or _first(lhs, "IdPrimary") is None:
                continue
            fname = _ident(_first(_first(lhs, "IdPrimary") or [], "Id"))
            out.append((fname, self._literal(parts[1])))
        return out

    def _v_AssignExpression(self, n: Node) -> None:
        parts = _lists(n)
        if parts and _rule(parts[0]) == "DotExpression":
            self._chain(parts[0], write=True)
        elif parts:
            self.visit(parts[0])
        for c in parts[1:]:
            self.visit(c)

    def _v_DotExpression(self, n: Node) -> None:
        self._chain(n, write=False)

    # -- member chains --------------------------------------------------------

    def _chain(self, n: Node, *, write: bool) -> None:
        """``head.m1.m2(...).m3``: resolve the head, then follow typed members."""
        parts: list[tuple[str, Node | None]] = []
        node: Node = n
        while _rule(node) == "DotExpression":
            subs = _lists(node)
            if len(subs) < 2:
                break
            base, tail = subs[0], subs[1]
            if _rule(tail) == "AnyId":
                parts.insert(0, (_ident(tail), None))
            elif _rule(tail) == "DotMethodCall":
                args = _first(tail, "ExpressionList")
                parts.insert(0, (_ident(_first(tail, "AnyId")), args if args is not None else []))
            node = base
        for _, args in parts:
            if args:
                self._children(args)
        if not parts:
            self.visit(node)
            return
        if _rule(node) == "PrimaryExpression" and _lists(node):
            prim = _lists(node)[0]
            pr = _rule(prim)
            if pr == "IdPrimary":
                self._id_chain(_ident(_first(prim, "Id")), parts, write)
                return
            if pr == "ThisPrimary":
                typ = self._lookup(parts[0][0])
                if typ is not None:
                    self._typed_chain(typ, parts[1:], write)
                return
            if pr == "SuperPrimary":
                return
            self.visit(prim)
            typ = self._expr_type(node)
            if typ is not None:
                self._typed_chain(typ, parts, write)
            return
        self.visit(node)
        typ = self._expr_type(node)
        if typ is not None:
            self._typed_chain(typ, parts, write)

    def _id_chain(self, name: str, parts: list[tuple[str, Node | None]], write: bool) -> None:
        low = name.lower()
        member, args = parts[0]
        m = member.lower()
        is_call = args is not None
        typ = self._lookup(name)
        if typ is not None:
            self._typed_chain(typ, parts, write)
            return
        if low == "trigger":
            if self.trigger_object and m in _TRIGGER_CONTEXT:
                coll = _Type("List", (_Type(self.trigger_object),))
                self._typed_chain(coll, parts[1:], write)
            return
        if low == "database" and is_call:
            if m in _DATABASE_DML:
                arg0 = _lists(args)[0] if args and _lists(args) else None
                self.a.dml.append(
                    DmlRef(
                        op=_DATABASE_DML[m],
                        target=_text(arg0)[:80] if arg0 is not None else "",
                        sobject=self._sobject_of(arg0) if arg0 is not None else None,
                        via_database_class=True,
                    )
                )
            elif m in _DYNAMIC_QUERY:
                self._dynamic_query(_lists(args)[0] if args and _lists(args) else None)
            elif (low, m) in _ASYNC:
                self.a.async_calls.append(
                    AsyncRef(mechanism=_ASYNC[(low, m)], target_class=self._async_target(args))
                )
            return
        if low == "system":
            if (low, m) in _ASYNC and is_call:
                self.a.async_calls.append(
                    AsyncRef(mechanism=_ASYNC[(low, m)], target_class=self._async_target(args))
                )
            elif m == "label" and len(parts) > 1:
                self.r.labels.add(parts[1][0])
            return
        if low == "type" and m == "forname" and is_call:
            lits = [self._string_literal(x) for x in _lists(args)] if args else []
            if len(lits) == 1 and lits[0] is not None:
                self.r.forname.add(lits[0])
            else:
                self.r.dynamic.add("dynamic_type")
            return
        if low == "label":
            self.r.labels.add(member)
            return
        if low == "schema":
            if m == "sobjecttype" and len(parts) > 1:
                obj = parts[1][0]
                self.r.sobjects.add(obj)
                if len(parts) > 3 and parts[2][0].lower() == "fields" and parts[3][1] is None:
                    self.r.fields.add(f"{obj}.{parts[3][0]}")
            elif m in {"getglobaldescribe", "describesobjects"}:
                self.r.dynamic.add("global_describe")
            return
        if low in self.own or low == self.a.name.lower():
            return  # own static member, inner type or enum: ``Kind.A``, ``CONST.length()``
        if is_sobject_name(name) and low not in _SYSTEM_NAMESPACES:
            if m in _SETTING_ACCESSORS:
                self.r.settings.add(name)
            elif m == "sobjecttype":
                self.r.sobjects.add(name)
            elif not is_call:
                self.r.sobjects.add(name)
                self.r.fields.add(f"{name}.{member}")
            return
        if low in _SYSTEM_NAMESPACES or low in _KEYWORDS:
            return
        if name[0].isupper():
            self.r.class_refs.add(name)
            if is_call:
                self.r.method_calls.add(f"{name}.{member}")
            return
        if is_call:
            # Apex is case-insensitive: ``customerServices.get()`` may be a static call on
            # CustomerServices or a method on an inherited variable. The graph builder keeps
            # the candidate only if a class by that name exists.
            self.r.candidate_refs.add(name)

    def _typed_chain(self, typ: _Type, parts: list[tuple[str, Node | None]], write: bool) -> None:
        if not parts:
            return
        member, args = parts[0]
        m = member.lower()
        is_call = args is not None
        if typ.is_collection:
            if is_call and m in _ELEMENT_ACCESSORS:
                self._typed_chain(typ.element, parts[1:], write)
            elif is_call and m in {"values", "clone", "deepclone"}:
                self._typed_chain(typ, parts[1:], write)
            return
        tname = typ.name
        if is_sobject_name(tname):
            self.r.sobjects.add(tname)
            if is_call:
                if m in {"get", "put"}:
                    lit = self._string_literal(_lists(args)[0]) if args and _lists(args) else None
                    if lit:
                        self.r.fields.add(f"{tname}.{lit}")
                        if m == "put":
                            self.r.field_writes.add(f"{tname}.{lit}")
                    else:
                        self.r.dynamic.add("dynamic_field")
                elif m == "newsobject":
                    self.r.dynamic.add("dynamic_sobject")
                elif m == "clone":
                    self._typed_chain(typ, parts[1:], write)
                return
            self.r.fields.add(f"{tname}.{member}")
            if write and len(parts) == 1:
                self.r.field_writes.add(f"{tname}.{member}")
            return
        if _is_org_type(tname) and tname.lower() not in self.own_types:
            self.r.class_refs.add(tname)
            if is_call:
                self.r.method_calls.add(f"{tname}.{member}")

    # -- typing ---------------------------------------------------------------

    def _expr_type(self, n: Node | None) -> _Type | None:
        if n is None:
            return None
        rule = _rule(n)
        if rule == "PrimaryExpression":
            prim = _lists(n)[0] if _lists(n) else None
            if prim is None:
                return None
            pr = _rule(prim)
            if pr == "IdPrimary":
                return self._lookup(_ident(_first(prim, "Id")))
            if pr == "SoqlPrimary":
                lit = _first(prim, "SoqlLiteral")
                obj = _parse_soql(self._soql_text(lit)).sobject if lit is not None else ""
                return _Type("List", (_Type(obj),)) if obj else None
            return None
        if rule in {"CastExpression", "InstanceOfExpression"}:
            tr = _first(n, "TypeRef")
            return _type_ref(tr) if tr is not None else None
        if rule == "SubExpression":
            subs = _lists(n)
            return self._expr_type(subs[0]) if subs else None
        if rule == "ArrayExpression":
            subs = _lists(n)
            t = self._expr_type(subs[0]) if subs else None
            return t.element if t is not None and t.is_collection else None
        if rule == "NewExpression":
            creator = _first(n, "Creator")
            return _created_type(creator) if creator is not None else None
        if rule == "DotExpression":
            subs = _lists(n)
            if len(subs) < 2:
                return None
            base, tail = subs[0], subs[1]
            member = _ident(tail if _rule(tail) == "AnyId" else _first(tail, "AnyId"))
            is_call = _rule(tail) == "DotMethodCall"
            if (
                _rule(base) == "PrimaryExpression"
                and _first(base, "IdPrimary") is not None
                and _ident(_first(_first(base, "IdPrimary") or [], "Id")).lower() == "trigger"
                and self.trigger_object
                and member.lower() in _TRIGGER_CONTEXT
            ):
                return _Type("List", (_Type(self.trigger_object),))
            bt = self._expr_type(base)
            if bt is None:
                return None
            if bt.is_collection and is_call and member.lower() in _ELEMENT_ACCESSORS:
                return bt.element
            if bt.is_collection and is_call and member.lower() in {"values", "clone", "deepclone"}:
                return bt
            if not bt.is_collection and is_call and member.lower() == "clone":
                return bt
            return None
        return None

    def _sobject_of(self, n: Node) -> str | None:
        t = self._expr_type(n)
        if t is None:
            return None
        e = t.element
        return e.name if is_sobject_name(e.name) else None

    def _classify(self, typ: _Type) -> None:
        name = typ.name
        low = name.lower()
        head = name.split(".", 1)[0]
        if name and low not in _KEYWORDS and low not in _COLLECTIONS:
            if is_sobject_name(name):
                self.r.sobjects.add(name)
            elif "." in name and is_sobject_name(head):
                self.r.sobjects.add(head)  # ``Account.SObjectType`` used as a type
            elif low in _CALLOUT_TYPES:
                self.r.callouts.add(name)
            elif low in self.own_types or low == self.a.name.lower():
                pass
            elif _is_org_type(name):
                self.r.class_refs.add(name)
        for arg in typ.args:
            self._classify(arg)

    # -- literals / dynamic ---------------------------------------------------

    @staticmethod
    def _literal(n: Node) -> str | None:
        if _rule(n) != "PrimaryExpression":
            return None
        lp = _first(n, "LiteralPrimary")
        lit = _first(lp, "Literal") if lp is not None else None
        if lit is None or not _terms(lit):
            return None
        text = _terms(lit)[0]
        if text.startswith("'"):
            return text.strip("'")
        if text.lower() in {"true", "false"} or text[0].isdigit():
            return text
        return None

    @staticmethod
    def _string_literal(n: Node) -> str | None:
        if _rule(n) != "PrimaryExpression":
            return None
        lp = _first(n, "LiteralPrimary")
        lit = _first(lp, "Literal") if lp is not None else None
        if lit is None or not _terms(lit) or not _terms(lit)[0].startswith("'"):
            return None
        return _terms(lit)[0][1:-1]

    def _dynamic_query(self, arg: Node | None) -> None:
        lit = self._string_literal(arg) if arg is not None else None
        concatenated = False
        if lit is None and arg is not None and _rule(arg) in {"Arth1Expression", "Arth2Expression"}:
            subs = _lists(arg)
            lit = self._string_literal(subs[0]) if subs else None
            concatenated = True
        if lit is None:
            self.a.soql.append(SoqlRef(sobject="", dynamic=True))
            self.r.dynamic.add("dynamic_soql")
            return
        q = _parse_soql(lit.replace("\\'", "'"))
        q.dynamic = True
        self.a.soql.append(q)
        if concatenated:
            self.r.dynamic.add("dynamic_soql")

    def _async_target(self, args: Node | None) -> str | None:
        if not args:
            return None
        for node in _walk(args):
            if _rule(node) == "NewExpression":
                creator = _first(node, "Creator")
                if creator is not None:
                    return _created_type(creator).name
        first = _lists(args)[0] if _lists(args) else None
        t = self._expr_type(first) if first is not None else None
        if t is not None and _is_org_type(t.name) and not t.is_collection:
            return t.name
        return None


# ---- entry point ------------------------------------------------------------


def analyze_tree(tree: Node, source: str, *, name_hint: str | None = None) -> ApexAnalysis:
    """Build an :class:`ApexAnalysis` from an emitted parse tree."""
    a = ApexAnalysis(name=name_hint or "", kind="unknown", engine="ast")
    a.lines = source.count("\n") + 1 if source else 0
    a.tokens = sum(1 for _ in _terminals(tree))
    w = _Walker(a)
    w.visit(tree)
    _fold(a, w.r)
    _derive_entry_points(a)
    _finalize(a)
    return a


def _terminals(n: Any) -> Iterator[str]:
    if isinstance(n, str):
        yield n
    elif isinstance(n, list):
        for c in n[1:]:
            yield from _terminals(c)
