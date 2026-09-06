"""Apex reference extraction over the token stream (C20).

Heuristic but deterministic. Every reference is *raw*: the dependency builder
resolves class names against the org's Apex corpus and sObject names against
the schema, so a false positive here becomes an unresolved reference in the
coverage report rather than a bad edge.
"""

from __future__ import annotations

import re

from offramp.extract.apex.model import ApexAnalysis, AsyncRef, DmlRef, SoqlRef
from offramp.extract.apex.tokenizer import Kind, Tok, tokenize

STANDARD_SOBJECTS: frozenset[str] = frozenset(
    {
        "account",
        "contact",
        "lead",
        "opportunity",
        "case",
        "task",
        "event",
        "user",
        "campaign",
        "campaignmember",
        "contract",
        "order",
        "orderitem",
        "product2",
        "pricebook2",
        "pricebookentry",
        "quote",
        "quotelineitem",
        "asset",
        "solution",
        "group",
        "groupmember",
        "profile",
        "permissionset",
        "permissionsetassignment",
        "attachment",
        "contentversion",
        "contentdocument",
        "contentdocumentlink",
        "emailmessage",
        "opportunitylineitem",
        "opportunitycontactrole",
        "accountcontactrelation",
        "individual",
        "workorder",
        "workorderlineitem",
        "serviceappointment",
        "entitlement",
        "milestone",
        "casecomment",
        "feeditem",
        "note",
        "emailtemplate",
        "organization",
        "userrole",
        "queuesobject",
        "recordtype",
        "processinstance",
        "processinstanceworkitem",
        "cronTrigger".lower(),
        "asyncapexjob",
        "apexclass",
        "apextrigger",
        "customobject",
        "period",
        "fiscalyearsettings",
        "topic",
        "dashboard",
        "report",
        "folder",
        "document",
        "collaborationgroup",
        "chatteractivity",
        "knowledge__kav",
        "servicecontract",
        "location",
        "address",
        "producttemplate",
        "businesshours",
        "holiday",
    }
)
_CUSTOM_SUFFIX = re.compile(r"__(c|e|mdt|b|x|share|history|changeevent|kav|feed)$", re.I)

_DML_OPS = {"insert", "update", "upsert", "delete", "undelete", "merge"}
_DATABASE_DML = {
    "insert": "insert",
    "update": "update",
    "upsert": "upsert",
    "delete": "delete",
    "undelete": "undelete",
    "merge": "merge",
    "insertimmediate": "insert",
    "updateimmediate": "update",
    "deleteimmediate": "delete",
}
_ASYNC = {
    ("system", "schedule"): "schedule",
    ("system", "enqueuejob"): "enqueue",
    ("system", "schedulebatch"): "batch",
    ("database", "executebatch"): "batch",
}
_CALLOUT_TYPES = {"http", "httprequest", "httpresponse", "webservicecallout", "continuation"}
_ENTRY_ANNOTATIONS = {
    "auraenabled": "aura_enabled",
    "invocablemethod": "invocable",
    "restresource": "rest_resource",
    "httpget": "rest_resource",
    "httppost": "rest_resource",
    "httpput": "rest_resource",
    "httpdelete": "rest_resource",
    "httppatch": "rest_resource",
    "future": "future",
    "remoteaction": "remote_action",
    "istest": "test",
    "testsetup": "test",
}
_ENTRY_INTERFACES = {
    "batchable": "batchable",
    "database.batchable": "batchable",
    "schedulable": "schedulable",
    "queueable": "queueable",
    "triggerhandler": "trigger_handler",
    "itriggerhandler": "trigger_handler",
    "triggeraction": "trigger_handler",
    "messaging.inboundemailhandler": "inbound_email",
    "inboundemailhandler": "inbound_email",
    "process.plugin": "flow_plugin",
    "auth.registrationhandler": "auth_handler",
    "site.urlrewriter": "site_rewriter",
    "callable": "callable",
}
_PLATFORM_INTERFACES = {
    "schedulable",
    "queueable",
    "comparable",
    "callable",
    "batchable",
    "database.batchable",
    "database.stateful",
    "database.allowscallouts",
    "database.raisesplatformevents",
    "messaging.inboundemailhandler",
    "inboundemailhandler",
    "process.plugin",
    "auth.registrationhandler",
    "site.urlrewriter",
    "finalizer",
    "iterable",
    "iterator",
    "system.schedulable",
    "system.queueable",
    "system.comparable",
    "system.callable",
}
_BRANCH_KEYWORDS = {"if", "for", "while", "do", "switch", "catch"}
_KEYWORDS = {
    "abstract",
    "activate",
    "and",
    "any",
    "array",
    "as",
    "asc",
    "autonomous",
    "begin",
    "bigdecimal",
    "blob",
    "boolean",
    "break",
    "bulk",
    "by",
    "byte",
    "case",
    "cast",
    "catch",
    "char",
    "class",
    "collect",
    "commit",
    "const",
    "continue",
    "convertcurrency",
    "decimal",
    "default",
    "delete",
    "desc",
    "do",
    "double",
    "else",
    "end",
    "enum",
    "exception",
    "exit",
    "export",
    "extends",
    "false",
    "final",
    "finally",
    "float",
    "for",
    "from",
    "global",
    "goto",
    "group",
    "having",
    "hint",
    "if",
    "implements",
    "import",
    "in",
    "inner",
    "insert",
    "instanceof",
    "int",
    "integer",
    "interface",
    "into",
    "join",
    "last_90_days",
    "last_month",
    "last_n_days",
    "last_week",
    "like",
    "limit",
    "list",
    "long",
    "loop",
    "map",
    "merge",
    "new",
    "next_90_days",
    "next_month",
    "next_n_days",
    "next_week",
    "not",
    "null",
    "nulls",
    "number",
    "object",
    "of",
    "on",
    "or",
    "outer",
    "override",
    "package",
    "parallel",
    "pragma",
    "private",
    "protected",
    "public",
    "retrieve",
    "return",
    "returning",
    "rollback",
    "savepoint",
    "search",
    "select",
    "set",
    "short",
    "sort",
    "static",
    "stat",
    "super",
    "switch",
    "synchronized",
    "system",
    "testmethod",
    "then",
    "this",
    "this_month",
    "this_week",
    "throw",
    "today",
    "tolabel",
    "tomorrow",
    "transaction",
    "trigger",
    "true",
    "try",
    "type",
    "undelete",
    "update",
    "upsert",
    "using",
    "virtual",
    "void",
    "webservice",
    "when",
    "where",
    "while",
    "with",
    "without",
    "sharing",
    "inherited",
    "yesterday",
    "string",
    "id",
    "date",
    "datetime",
    "time",
    "sobject",
    "get",
}
_SYSTEM_NAMESPACES = {
    "system",
    "database",
    "schema",
    "math",
    "string",
    "integer",
    "decimal",
    "date",
    "datetime",
    "json",
    "test",
    "userinfo",
    "limits",
    "messaging",
    "approval",
    "http",
    "httprequest",
    "httpresponse",
    "url",
    "blob",
    "encodingutil",
    "crypto",
    "pattern",
    "matcher",
    "type",
    "label",
    "trigger",
    "list",
    "map",
    "set",
    "id",
    "boolean",
    "long",
    "double",
    "time",
    "apexpages",
    "page",
    "site",
    "auth",
    "cache",
    "connectapi",
    "flow",
    "eventbus",
    "queueable",
    "process",
    "reports",
    "search",
    "sobject",
    "exception",
    "dmlexception",
    "queryexception",
    "nullpointerexception",
    "typeexception",
    "calloutexception",
    "jsonexception",
    "stringexception",
    "mathexception",
    "aurahandledexception",
    "invalidparametervalueexception",
    "listexception",
    "sobjectexception",
    "customsettings",
    "custommetadata",
    "component",
    "quickaction",
    "richmessageing",
    "wave",
    "metadata",
    "packaging",
    "sfdc_checkout",
    "commerce",
    "datasource",
    "dom",
    "xml",
    "xmlstreamreader",
    "xmlstreamwriter",
    "csvimport",
    "kbmanagement",
    "territorymgmt",
    "twilio",
    "iterator",
    "iterable",
    "comparable",
    "assert",
    "formula",
    "invocable",
    "externalservice",
    "batchable",
    "schedulable",
    "continuation",
    "restcontext",
    "restrequest",
    "restresponse",
    "sandboxpostcopy",
    "schedulablecontext",
    "batchablecontext",
    "queueablecontext",
    "finalizer",
    "finalizercontext",
    "savepoint",
    "version",
    "package",
    "namespace",
    "picklist",
    "describefieldresult",
    "describesobjectresult",
    "sobjecttype",
    "sobjectfield",
    "picklistentry",
    "childrelationship",
    "displaytype",
    "recordtypeinfo",
    "fieldset",
    "fieldsetmember",
    "userrecordaccess",
    "system.schedule",
}


def is_sobject_name(name: str) -> bool:
    n = name.lower()
    return n in STANDARD_SOBJECTS or bool(_CUSTOM_SUFFIX.search(name))


def analyze(source: str, *, name_hint: str | None = None) -> ApexAnalysis:
    """Analyze one Apex class / interface / trigger body."""
    toks = tokenize(source)
    a = ApexAnalysis(name=name_hint or "", kind="unknown")
    a.lines = source.count("\n") + 1 if source else 0
    a.tokens = len(toks)

    _header(toks, a)
    decls = _declarations(toks)
    _scan(toks, a, decls)
    _derive_entry_points(a)
    _finalize(a)
    return a


# ---- header -----------------------------------------------------------------


def _header(toks: list[Tok], a: ApexAnalysis) -> None:
    i = 0
    n = len(toks)
    seen_annotations: list[str] = []
    while i < n:
        t = toks[i]
        if t.kind is Kind.ANNOTATION:
            seen_annotations.append(t.text[1:])
        elif t.kind is Kind.IDENT:
            low = t.lower()
            if low in {"global", "public", "private", "protected", "abstract", "virtual"}:
                a.modifiers.append(low)
            elif (
                low in {"with", "without", "inherited"}
                and i + 1 < n
                and toks[i + 1].lower() == "sharing"
            ):
                a.sharing = low
                i += 1
            elif low in {"class", "interface", "enum"}:
                a.kind = low
                if i + 1 < n and toks[i + 1].kind is Kind.IDENT:
                    a.name = a.name or toks[i + 1].text
                    a.name = toks[i + 1].text
                i = _class_clauses(toks, i + 2, a)
                break
            elif low == "trigger":
                a.kind = "trigger"
                if i + 1 < n and toks[i + 1].kind is Kind.IDENT:
                    a.name = toks[i + 1].text
                # trigger X on Obj (before insert, after update)
                j = i + 2
                if j < n and toks[j].lower() == "on" and j + 1 < n:
                    a.trigger_object = toks[j + 1].text
                    j += 2
                    if j < n and toks[j].text == "(":
                        j += 1
                        evt: list[str] = []
                        while j < n and toks[j].text != ")":
                            if toks[j].kind is Kind.IDENT:
                                evt.append(toks[j].lower())
                            j += 1
                        a.trigger_events = _pair_events(evt)
                break
            elif low in {"static", "final", "testmethod", "override"}:
                pass
            else:
                # Not a header token; a file that starts with statements (unlikely).
                pass
        elif t.text == "{":
            break
        i += 1
    a.annotations.extend(seen_annotations)
    if any(x.lower() == "istest" for x in seen_annotations):
        a.is_test = True


def _pair_events(words: list[str]) -> list[str]:
    out: list[str] = []
    timing = ""
    for w in words:
        if w in {"before", "after"}:
            timing = w
        elif w in {"insert", "update", "delete", "undelete"} and timing:
            out.append(f"{timing} {w}")
    return out


def _class_clauses(toks: list[Tok], i: int, a: ApexAnalysis) -> int:
    """Consume ``extends X implements A, B`` up to the opening brace."""
    n = len(toks)
    mode = ""
    while i < n and toks[i].text != "{":
        t = toks[i]
        low = t.lower()
        if low == "extends":
            mode = "extends"
        elif low == "implements":
            mode = "implements"
        elif t.kind is Kind.IDENT:
            name = _dotted(toks, i)
            i += name.count(".") * 2
            # skip generic args
            if i + 1 < n and toks[i + 1].text == "<":
                i = _skip_generic(toks, i + 1)
            if mode == "extends":
                a.extends = name
            elif mode == "implements":
                a.implements.append(name)
        i += 1
    return i


def _dotted(toks: list[Tok], i: int) -> str:
    parts = [toks[i].text]
    j = i + 1
    while j + 1 < len(toks) and toks[j].text == "." and toks[j + 1].kind is Kind.IDENT:
        parts.append(toks[j + 1].text)
        j += 2
    return ".".join(parts)


def _skip_generic(toks: list[Tok], i: int) -> int:
    depth = 0
    while i < len(toks):
        if toks[i].text == "<":
            depth += 1
        elif toks[i].text == ">":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return i


# ---- declarations -----------------------------------------------------------


def _declarations(toks: list[Tok]) -> dict[str, str]:
    """Map local/field variable names to declared types (``Lead l`` → l: Lead).

    Generic collections resolve to their element type: ``List<Lead> leads`` →
    leads: Lead; ``Map<Id, Account> m`` → m: Account.
    """
    decls: dict[str, str] = {}
    n = len(toks)
    i = 0
    while i < n - 2:
        t = toks[i]
        if t.kind is Kind.IDENT and t.lower() not in _KEYWORDS - {
            "list",
            "map",
            "set",
            "id",
            "string",
        }:
            j = i
            type_name = t.text
            # Dotted type (Approval.ProcessSubmitRequest, Database.QueryLocator): keep the namespace
            # so platform types are filtered out downstream.
            if i >= 2 and toks[i - 1].text == "." and toks[i - 2].kind is Kind.IDENT:
                type_name = f"{toks[i - 2].text}.{t.text}"
            # generic
            if j + 1 < n and toks[j + 1].text == "<":
                close = _skip_generic(toks, j + 1)
                inner = [x.text for x in toks[j + 2 : close] if x.kind is Kind.IDENT]
                # last identifier inside <...> is the element type
                elem = inner[-1] if inner else type_name
                type_name = elem
                j = close
            # array form Lead[]
            if j + 2 < n and toks[j + 1].text == "[" and toks[j + 2].text == "]":
                j += 2
            if (
                j + 2 < n
                and toks[j + 1].kind is Kind.IDENT
                and toks[j + 2].text in {"=", ";", ",", ")", ":"}
            ):
                var = toks[j + 1].text
                if toks[j + 1].lower() not in _KEYWORDS and t.lower() not in {
                    "return",
                    "new",
                    "throw",
                }:
                    decls[var.lower()] = type_name
                i = j + 2
                continue
        i += 1
    return decls


# ---- main scan --------------------------------------------------------------


def _scan(toks: list[Tok], a: ApexAnalysis, decls: dict[str, str]) -> None:
    n = len(toks)
    class_refs: set[str] = set()
    method_calls: set[str] = set()
    sobjects: set[str] = set()
    fields: set[str] = set()
    field_writes: set[str] = set()
    callouts: set[str] = set()
    named_creds: set[str] = set()
    labels: set[str] = set()
    settings: set[str] = set()
    forname: set[str] = set()
    annotations: list[str] = []
    depth = 0

    # sObject-typed variables from declarations
    for typ in decls.values():
        if is_sobject_name(typ):
            sobjects.add(typ)

    i = 0
    while i < n:
        t = toks[i]
        low = t.lower()

        if t.text == "{":
            depth += 1
        elif t.text == "}":
            depth -= 1
        elif t.text == "?":
            a.branches += 1

        if t.kind is Kind.ANNOTATION:
            annotations.append(t.text[1:])
            i += 1
            continue

        if t.kind is Kind.SOQL:
            a.soql.append(_parse_soql(t.text))
            i += 1
            continue

        if t.kind is Kind.STRING:
            lit = t.text[1:-1]
            if lit.lower().startswith("callout:"):
                named_creds.add(lit.split(":", 1)[1].split("/", 1)[0])
            i += 1
            continue

        if t.kind is Kind.IDENT:
            # Nested type declarations
            if (
                low in {"class", "interface", "enum"}
                and depth >= 1
                and i + 1 < n
                and toks[i + 1].kind is Kind.IDENT
            ):
                a.inner_types.append(toks[i + 1].text)

            # Branch counting / method counting
            if low in _BRANCH_KEYWORDS:
                a.branches += 1

            # DML statements: insert x; upsert x Field__c; delete [SELECT...]
            if (
                low in _DML_OPS
                and i + 1 < n
                and toks[i + 1].kind in {Kind.IDENT, Kind.SOQL}
                and i > 0
                and toks[i - 1].text != "."
            ):
                target_tok = toks[i + 1]
                if target_tok.kind is Kind.SOQL:
                    q = _parse_soql(target_tok.text)
                    a.dml.append(DmlRef(op=low, target="[SOQL]", sobject=q.sobject))
                elif target_tok.lower() != "new":
                    var = target_tok.text
                    a.dml.append(DmlRef(op=low, target=var, sobject=_resolve_type(var, decls)))
                else:
                    # insert new Lead(...)
                    if i + 2 < n:
                        a.dml.append(DmlRef(op=low, target="new", sobject=toks[i + 2].text))
                i += 1
                continue

            # Qualified access: A.b
            if i + 2 < n and toks[i + 1].text == "." and toks[i + 2].kind is Kind.IDENT:
                head = t.text
                member = toks[i + 2].text
                headl = head.lower()
                memberl = member.lower()
                is_call = i + 3 < n and toks[i + 3].text == "("
                prev_is_dot = i > 0 and toks[i - 1].text == "."

                if not prev_is_dot:
                    # Database.insert(...) etc.
                    if headl == "database" and memberl in _DATABASE_DML and is_call:
                        var = toks[i + 4].text if i + 4 < n else ""
                        a.dml.append(
                            DmlRef(
                                op=_DATABASE_DML[memberl],
                                target=var,
                                sobject=_resolve_type(var, decls),
                                via_database_class=True,
                            )
                        )
                    elif (
                        headl == "database"
                        and memberl in {"query", "querywithbinds", "getquerylocator", "countquery"}
                        and is_call
                    ):
                        lit = (
                            toks[i + 4].text
                            if i + 4 < n and toks[i + 4].kind is Kind.STRING
                            else ""
                        )
                        q = (
                            _parse_soql(lit[1:-1].replace("\\'", "'"))
                            if lit
                            else SoqlRef(sobject="", dynamic=True)
                        )
                        q.dynamic = True
                        a.soql.append(q)
                    elif (headl, memberl) in _ASYNC and is_call:
                        a.async_calls.append(
                            AsyncRef(
                                mechanism=_ASYNC[(headl, memberl)],
                                target_class=_find_new(toks, i + 3),
                            )
                        )
                    elif headl == "type" and memberl == "forname" and is_call:
                        lit = (
                            toks[i + 4].text
                            if i + 4 < n and toks[i + 4].kind is Kind.STRING
                            else ""
                        )
                        if lit:
                            forname.add(lit[1:-1])
                    elif headl == "label":
                        labels.add(member)
                    elif (
                        headl == "schema"
                        and memberl == "sobjecttype"
                        and i + 4 < n
                        and toks[i + 3].text == "."
                    ):
                        sobjects.add(toks[i + 4].text)
                    elif (
                        headl == "trigger" and memberl in {"new", "old", "newmap", "oldmap"}
                    ) or headl in _SYSTEM_NAMESPACES:
                        pass
                    elif is_sobject_name(head):
                        # Lead.Email (SObjectField) or Custom_Setting__c.getInstance()
                        if memberl in {"getinstance", "getvalues", "getall", "getorgdefaults"}:
                            settings.add(head)
                        elif memberl == "sobjecttype":
                            sobjects.add(head)
                        elif (not is_call and not member[0].islower()) or not is_call:
                            sobjects.add(head)
                            fields.add(f"{head}.{member}")
                    elif headl in decls:
                        typ = decls[headl]
                        if is_sobject_name(typ) and not is_call:
                            fields.add(f"{typ}.{member}")
                            if i + 3 < n and toks[i + 3].text == "=":
                                field_writes.add(f"{typ}.{member}")
                        elif (
                            not is_sobject_name(typ)
                            and typ[0].isupper()
                            and typ.lower() not in _KEYWORDS
                            and typ.lower() not in _SYSTEM_NAMESPACES
                            and typ.lower() not in _CALLOUT_TYPES
                        ):
                            class_refs.add(typ)
                            if is_call:
                                method_calls.add(f"{typ}.{member}")
                    elif head[0].isupper() and headl not in _KEYWORDS:
                        # Static call / constant on another class
                        class_refs.add(head)
                        if is_call:
                            method_calls.add(f"{head}.{member}")
                        elif memberl == "class":
                            pass
                i += 1
                continue

            # new X(
            if low == "new" and i + 1 < n and toks[i + 1].kind is Kind.IDENT:
                typ = toks[i + 1].text
                if is_sobject_name(typ):
                    sobjects.add(typ)
                    # new Lead(Id = x, OwnerId = y): named-argument constructor writes fields.
                    if i + 2 < n and toks[i + 2].text == "(":
                        j = i + 3
                        depth_p = 1
                        while j < n and depth_p > 0:
                            if toks[j].text == "(":
                                depth_p += 1
                            elif toks[j].text == ")":
                                depth_p -= 1
                            elif (
                                toks[j].kind is Kind.IDENT
                                and j + 1 < n
                                and toks[j + 1].text == "="
                                and depth_p == 1
                            ):
                                fields.add(f"{typ}.{toks[j].text}")
                                field_writes.add(f"{typ}.{toks[j].text}")
                            j += 1
                elif typ.lower() in _CALLOUT_TYPES:
                    callouts.add(typ)
                elif (
                    typ.lower() not in _KEYWORDS
                    and typ.lower() not in _SYSTEM_NAMESPACES
                    and typ[0].isupper()
                ):
                    class_refs.add(typ)
                i += 2
                continue

            # Bare type usages: declarations already handled; also `Lead` appearing as a type.
            # A token right after '.' is a member name, never a type.
            prev_dot = i > 0 and toks[i - 1].text == "."
            if prev_dot:
                pass
            elif low in _CALLOUT_TYPES and t.text[0].isupper():
                callouts.add(t.text)
            elif is_sobject_name(t.text) and low not in _KEYWORDS and low not in decls:
                sobjects.add(t.text)
            elif (
                t.text[0].isupper()
                and low not in _KEYWORDS
                and low not in _SYSTEM_NAMESPACES
                and low not in decls
                # Possible class reference used as a type (e.g. `TriggerAction handler`).
                and i + 1 < n
                and (toks[i + 1].kind is Kind.IDENT or toks[i + 1].text in {"<", ">", ",", ")"})
            ):
                class_refs.add(t.text)

            # Method definitions: <ret> <name> ( ... ) {   (approximate)
            if (
                i + 1 < n
                and toks[i + 1].text == "("
                and i > 0
                and toks[i - 1].kind is Kind.IDENT
                and toks[i - 1].lower()
                not in {"new", "return", "else", "if", "for", "while", "switch", "catch"}
                and depth == 1
            ):
                a.methods += 1

        i += 1

    # Interfaces resolved as class refs too (so the graph links to the interface class if it exists)
    for name in [a.extends, *a.implements]:
        if name:
            class_refs.add(name.split("<", 1)[0])

    # Trigger context sobject
    if a.trigger_object:
        sobjects.add(a.trigger_object)

    # Platform types are not org classes (framework interfaces like TriggerAction are).
    class_refs = {
        c
        for c in class_refs
        if c.split(".", 1)[0].lower() not in _SYSTEM_NAMESPACES
        and c.lower() not in _PLATFORM_INTERFACES
        and c.lower() not in _CALLOUT_TYPES
        and not is_sobject_name(c)
    }
    # Don't self-reference
    class_refs.discard(a.name)
    for inner in a.inner_types:
        class_refs.discard(inner)

    a.annotations.extend(x for x in annotations if x not in a.annotations)
    a.class_references = sorted(class_refs, key=str.lower)
    a.method_calls = sorted(method_calls, key=str.lower)
    for q in a.soql:
        if q.sobject:
            sobjects.add(q.sobject)
            for f in q.fields + q.where_fields:
                if "." not in f:
                    fields.add(f"{q.sobject}.{f}")
                else:
                    fields.add(f"{q.sobject}.{f}")
    for d in a.dml:
        if d.sobject:
            sobjects.add(d.sobject)
    a.sobject_references = sorted(sobjects, key=str.lower)
    a.field_references = sorted(fields, key=str.lower)
    a.field_writes = sorted(field_writes, key=str.lower)
    a.callouts = sorted(callouts, key=str.lower)
    a.named_credentials = sorted(named_creds)
    a.custom_labels = sorted(labels)
    a.custom_settings = sorted(settings)
    a.type_forname_literals = sorted(forname)


def _resolve_type(var: str, decls: dict[str, str]) -> str | None:
    typ = decls.get(var.lower())
    if typ and is_sobject_name(typ):
        return typ
    if is_sobject_name(var):
        return var
    return None


def _find_new(toks: list[Tok], i: int) -> str | None:
    """Inside a call starting at toks[i]=='(' find the first ``new X`` argument."""
    depth = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        if t.text == "(":
            depth += 1
        elif t.text == ")":
            depth -= 1
            if depth == 0:
                return None
        elif t.lower() == "new" and i + 1 < n and toks[i + 1].kind is Kind.IDENT:
            return toks[i + 1].text
        i += 1
    return None


# ---- SOQL -------------------------------------------------------------------

_SOQL_FROM = re.compile(r"\bfrom\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
_SOQL_SELECT = re.compile(r"\bselect\s+(.*?)\s+from\b", re.I | re.S)
_SOQL_WHERE = re.compile(
    r"\bwhere\s+(.*?)(?:\bgroup\s+by\b|\border\s+by\b|\blimit\b|\bfor\b|\bwith\b|\ball\s+rows\b|$)",
    re.I | re.S,
)
_SOQL_SUBQUERY = re.compile(
    r"\(\s*select\s+.*?\bfrom\s+([A-Za-z_][A-Za-z0-9_]*)[^)]*\)", re.I | re.S
)
_FIELD_TOKEN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\b")
_SOQL_KEYWORDS = {
    "select",
    "from",
    "where",
    "and",
    "or",
    "not",
    "in",
    "like",
    "limit",
    "order",
    "by",
    "asc",
    "desc",
    "nulls",
    "first",
    "last",
    "group",
    "having",
    "count",
    "sum",
    "avg",
    "min",
    "max",
    "null",
    "true",
    "false",
    "today",
    "yesterday",
    "tomorrow",
    "this_week",
    "last_week",
    "next_week",
    "this_month",
    "last_month",
    "next_month",
    "last_n_days",
    "next_n_days",
    "for",
    "update",
    "view",
    "reference",
    "with",
    "security_enforced",
    "all",
    "rows",
    "typeof",
    "when",
    "then",
    "else",
    "end",
    "offset",
    "includes",
    "excludes",
    "using",
    "scope",
    "toLabel".lower(),
    "convertcurrency",
    "format",
    "fields",
    "standard",
    "custom",
    "count_distinct",
    "calendar_year",
    "calendar_month",
    "fiscal_year",
    "day_only",
    "distance",
    "geolocation",
    "grouping",
    "hour_in_day",
    "week_in_year",
    "system_mode",
    "user_mode",
}


def _parse_soql(text: str) -> SoqlRef:
    """Extract object, selected fields, where fields, and subquery relationships."""
    src = text.strip()
    if src.lower().startswith("find"):
        # SOSL: FIND 'x' RETURNING Account(Name), Contact(Email)
        objs = re.findall(r"returning\s+(.*)", src, re.I | re.S)
        rel = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", objs[0]) if objs else []
        return SoqlRef(sobject=rel[0] if rel else "", relationships=rel[1:], raw=src[:200])
    # Strip subqueries first, but record their relationship names.
    subs = _SOQL_SUBQUERY.findall(src)
    flat = _SOQL_SUBQUERY.sub("__SUB__", src)
    m = _SOQL_FROM.search(flat)
    sobject = m.group(1) if m else ""
    fields: list[str] = []
    sm = _SOQL_SELECT.search(flat)
    if sm:
        for f in _FIELD_TOKEN.findall(sm.group(1)):
            fl = f.lower()
            if fl in _SOQL_KEYWORDS or fl == "__sub__" or fl.startswith(":"):
                continue
            if f.startswith("'") or re.fullmatch(r"\d+", f):
                continue
            fields.append(f)
    where_fields: list[str] = []
    wm = _SOQL_WHERE.search(flat)
    if wm:
        clause = re.sub(r":\s*[A-Za-z_][A-Za-z0-9_.()]*", " ", wm.group(1))  # drop bind vars
        clause = re.sub(r"'(?:\\.|[^'\\])*'", " ", clause)
        for f in _FIELD_TOKEN.findall(clause):
            fl = f.lower()
            if fl in _SOQL_KEYWORDS or re.fullmatch(r"\d+", f) or fl == "__sub__":
                continue
            where_fields.append(f)
    return SoqlRef(
        sobject=sobject,
        fields=sorted(set(fields), key=str.lower),
        where_fields=sorted(set(where_fields), key=str.lower),
        relationships=sorted(set(subs), key=str.lower),
        raw=src[:200],
    )


# ---- derived ----------------------------------------------------------------


def _derive_entry_points(a: ApexAnalysis) -> None:
    eps: set[str] = set()
    for ann in a.annotations:
        base = ann.split("(", 1)[0].lower()
        if base in _ENTRY_ANNOTATIONS:
            eps.add(_ENTRY_ANNOTATIONS[base])
    for iface in a.implements:
        key = iface.lower().split("<", 1)[0]
        if key in _ENTRY_INTERFACES:
            eps.add(_ENTRY_INTERFACES[key])
        elif key.split(".")[-1] in _ENTRY_INTERFACES:
            eps.add(_ENTRY_INTERFACES[key.split(".")[-1]])
    if "global" in a.modifiers and "webservice" in {t.lower() for t in a.modifiers}:
        eps.add("soap_webservice")
    if a.kind == "trigger":
        eps.add("trigger")
    if a.is_test:
        eps.add("test")
    a.entry_points = sorted(eps)


def _finalize(a: ApexAnalysis) -> None:
    a.annotations = sorted(set(a.annotations), key=str.lower)
    if not a.name:
        a.name = "Unknown"
