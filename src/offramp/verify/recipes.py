"""Recipes for ``offramp verify``, generated from the schema and the process model.

A recipe says how to make one flow fire: the record to insert (and update)
for a record-triggered flow, the inputs for an autolaunched one. Hand-written
recipes (``config/verify_recipes.example.json``) stay authoritative; this
module fills in the rest so verification can cover every verifiable flow of
an org without anyone typing field values:

* required fields of the object get a value by type (schema snapshot);
* the flow's own entry conditions are satisfied (``EqualTo``, ``NotEqualTo``,
  ``IsNull``, numeric comparisons, ``Contains``…), on the update step when the
  flow only fires on update or requires the criteria to *become* true;
* ``ISBLANK(Field)`` terms of the object's validation rules get a value;
* other record-triggered flows on the same object are kept quiet where their
  entry conditions allow it, so one interview per record is traced;
* autolaunched flows get typed inputs; a ``recordId``-style input paired with a
  lookup step gets a fresh record and ``$record.Id``.

Everything a generator could not decide is listed under ``_needs`` so the
reviewer sees exactly what to fill in.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from offramp.core.models import SchemaNode, SchemaNodeKind, SchemaSnapshot
from offramp.core.process import Condition, ProcessDefinition, StepKind, TriggerKind, Variable

_FLOW_KINDS = {"record_triggered_flow", "autolaunched_flow"}
_SKIP_FIELDS = {"id", "ownerid", "createdbyid", "lastmodifiedbyid", "recordtypeid", "isdeleted"}
_UNFILLABLE_TYPES = {
    "lookup",
    "masterdetail",
    "reference",
    "id",
    "autonumber",
    "formula",
    "summary",
}
_TEXT = "Offramp verify"
_ISBLANK = re.compile(r"ISBLANK\(\s*(?:TEXT\(\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\)?\s*\)", re.I)


def _field_index(schema: SchemaSnapshot | None) -> dict[str, dict[str, SchemaNode]]:
    idx: dict[str, dict[str, SchemaNode]] = defaultdict(dict)
    for n in schema.nodes if schema else []:
        if n.kind is SchemaNodeKind.FIELD and "." in n.api_name:
            obj, fname = n.api_name.split(".", 1)
            idx[obj.lower()][fname.lower()] = n
    return idx


def value_for(node: SchemaNode) -> Any:
    """A plausible value for one field, by type; ``None`` when it cannot be synthesised."""
    t = (node.field_type or "").lower()
    if t in _UNFILLABLE_TYPES or node.formula:
        return None
    if t in {"picklist", "multiselectpicklist", "multipicklist"}:
        return node.picklist_values[0] if node.picklist_values else None
    if t in {"email"}:
        return "verify@example.com"
    if t in {"phone"}:
        return "+1 555 010 0100"
    if t in {"url"}:
        return "https://example.com/verify"
    if t in {"date"}:
        return datetime.now(UTC).date().isoformat()
    if t in {"datetime"}:
        return datetime.now(UTC).replace(microsecond=0).isoformat()
    if t in {"checkbox", "boolean"}:
        return False
    if t in {"number", "double", "int", "integer", "currency", "percent", "long"}:
        return 1
    if t in {
        "text",
        "textarea",
        "longtextarea",
        "string",
        "encryptedstring",
        "html",
        "richtextarea",
    }:
        return _TEXT
    if t in {"time"}:
        return "09:00:00.000Z"
    if t in {"location", "address", "base64", "blob", "json"}:
        return None
    return _TEXT


def _other_picklist_value(node: SchemaNode, avoid: str) -> str | None:
    for v in node.picklist_values:
        if v.lower() != avoid.lower():
            return v
    return None


def _number(value: Any) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


class _Builder:
    def __init__(self, obj: str, fields: dict[str, SchemaNode]) -> None:
        self.obj = obj
        self.fields = fields
        self.create: dict[str, Any] = {}
        self.update: dict[str, Any] = {}
        self.needs: list[str] = []
        self.why: list[str] = []

    def node(self, name: str) -> SchemaNode | None:
        return self.fields.get(name.split(".", 1)[-1].lower())

    def api(self, name: str) -> str:
        n = self.node(name)
        return n.api_name.split(".", 1)[1] if n else name.split(".", 1)[-1]

    def set(self, name: str, value: Any, *, on_update: bool = False) -> None:
        target = self.update if on_update else self.create
        target[self.api(name)] = value

    def has(self, name: str) -> bool:
        key = self.api(name)
        return key in self.create or key in self.update

    def required_fields(self) -> None:
        has_last_name = "lastname" in self.fields
        for n in self.fields.values():
            fname = n.api_name.split(".", 1)[1]
            if not n.required or fname.lower() in _SKIP_FIELDS or self.has(fname):
                continue
            if fname.lower() == "name" and has_last_name:
                continue  # compound person name (Lead, Contact): LastName carries it
            if n.raw.get("createable") is False or n.raw.get("updateable") is False:
                continue
            v = value_for(n)
            if v is None:
                self.needs.append(f"{fname}: required {n.field_type or 'field'} — supply a value")
            else:
                self.create[fname] = v

    def satisfy(self, c: Condition, *, on_update: bool) -> None:
        """Make one entry condition true (best effort); record what could not be decided."""
        if not c.left or c.expression:
            if c.expression:
                self.needs.append(f"entry condition is a formula: {c.expression[:120]}")
            return
        fname = self.api(c.left)
        node = self.node(c.left)
        op = (c.operator or "EqualTo").lower()
        right = c.right
        if isinstance(right, dict):
            self.needs.append(f"{fname} {c.operator} a reference ({right.get('ref')}) — decide")
            return
        if op == "equalto":
            self.set(fname, _coerce(node, right), on_update=on_update)
        elif op == "notequalto":
            if node is not None and node.picklist_values:
                alt = _other_picklist_value(node, str(right))
                if alt is not None:
                    self.set(fname, alt, on_update=on_update)
                    return
            self.set(
                fname,
                (value_for(node) if node else _TEXT)
                if str(right).lower() != _TEXT.lower()
                else "x",
                on_update=on_update,
            )
        elif op == "isnull":
            wants_null = str(right).lower() == "true"
            if wants_null:
                self.create.pop(fname, None)
                self.update.pop(fname, None)
            elif node is not None:
                v = value_for(node)
                if v is None:
                    self.needs.append(f"{fname} must be non-null — supply a value")
                else:
                    self.set(fname, v, on_update=on_update)
        elif op in {"greaterthan", "greaterthanorequalto"}:
            n = _number(right)
            self.set(
                fname,
                (n + 1 if op == "greaterthan" else n) if n is not None else 1,
                on_update=on_update,
            )
        elif op in {"lessthan", "lessthanorequalto"}:
            n = _number(right)
            self.set(
                fname,
                (n - 1 if op == "lessthan" else n) if n is not None else 0,
                on_update=on_update,
            )
        elif op in {"contains", "startswith", "endswith"}:
            self.set(fname, str(right), on_update=on_update)
        elif op == "ischanged":
            base = value_for(node) if node else _TEXT
            self.set(fname, base, on_update=False)
            self.set(fname, f"{base} changed" if isinstance(base, str) else 2, on_update=True)
        elif op == "wasset":
            v = value_for(node) if node else _TEXT
            self.set(fname, v, on_update=on_update)
        else:
            self.needs.append(f"{fname} {c.operator} {right!r} — operator not supported")


def _coerce(node: SchemaNode | None, value: Any) -> Any:
    if node is None:
        return value
    t = (node.field_type or "").lower()
    if t in {"checkbox", "boolean"}:
        return str(value).lower() == "true"
    if t in {"number", "double", "int", "integer", "currency", "percent", "long"}:
        n = _number(value)
        return int(n) if n is not None and n.is_integer() else (n if n is not None else value)
    return value


def _flow_definitions(definitions: list[ProcessDefinition]) -> list[ProcessDefinition]:
    return [d for d in definitions if d.kind in _FLOW_KINDS and d.active]


def _quiet_other_flows(b: _Builder, d: ProcessDefinition, others: list[ProcessDefinition]) -> None:
    """Choose values that keep sibling record-triggered flows from firing on the same record."""
    for o in others:
        if o.name == d.name or o.trigger.kind is not TriggerKind.RECORD_SAVE:
            continue
        if (o.trigger.object or "").lower() != b.obj.lower():
            continue
        conds = [c for c in o.trigger.when.conditions if c.left and not c.expression]
        if not conds or o.trigger.when.logic.lower() == "or":
            continue  # cannot silence: no conditions, or any one of several would do
        for c in conds:
            if (c.operator or "").lower() != "equalto" or isinstance(c.right, dict):
                continue
            fname = b.api(c.left)
            if b.has(fname):
                continue
            node = b.node(c.left)
            if node is not None and node.picklist_values:
                alt = _other_picklist_value(node, str(c.right))
                if alt is not None:
                    b.create[fname] = alt
                    b.why.append(f"{fname}={alt!r} keeps {o.name}'s entry condition false")
                    break
            elif node is not None and (node.field_type or "").lower() in {"text", "string"}:
                b.create[fname] = f"not {c.right}"[:80]
                b.why.append(f"{fname} set so {o.name}'s entry condition stays false")
                break


def _validation_guards(b: _Builder, definitions: list[ProcessDefinition]) -> None:
    for d in definitions:
        if d.kind != "validation_rule" or (d.trigger.object or "").lower() != b.obj.lower():
            continue
        for c in d.trigger.when.conditions:
            for fname in _ISBLANK.findall(c.expression or ""):
                node = b.node(fname)
                if node is None or b.has(fname):
                    continue
                v = value_for(node)
                if v is not None:
                    b.create[b.api(fname)] = v
                    b.why.append(f"{b.api(fname)} filled: {d.name} checks ISBLANK")


def _record_triggered(
    d: ProcessDefinition,
    fields: dict[str, dict[str, SchemaNode]],
    definitions: list[ProcessDefinition],
) -> dict[str, Any] | None:
    obj = d.trigger.object
    if not obj:
        return None
    b = _Builder(obj, fields.get(obj.lower(), {}))
    events = [e.lower() for e in d.trigger.events]
    # Only update-only flows need the criteria to be met by the update step; a flow that
    # also fires on create treats a new record meeting the criteria as "changed to meet".
    on_update = bool(events) and "create" not in events
    logic = d.trigger.when.logic.lower()
    conds = list(d.trigger.when.conditions)
    if logic == "or" and conds:
        conds = conds[:1]
        b.why.append("entry logic is OR: only the first condition is satisfied")
    elif logic not in {"and", "or"} and conds:
        b.why.append(f"entry logic is custom ({d.trigger.when.logic}): all conditions satisfied")
    for c in conds:
        b.satisfy(c, on_update=on_update)
    if on_update and not b.update:
        b.update = {}  # an update must still happen: touch a required text field below
    b.required_fields()
    _validation_guards(b, definitions)
    _quiet_other_flows(b, d, _flow_definitions(definitions))
    if on_update and not b.update:
        # nothing in the conditions moved to the update step: change any writable text field
        for n in b.fields.values():
            if (n.field_type or "").lower() in {"text", "string"} and not n.formula:
                fname = n.api_name.split(".", 1)[1]
                if fname.lower() not in _SKIP_FIELDS:
                    b.update[fname] = f"{_TEXT} updated"
                    b.why.append(f"flow fires on update only: {fname} changed after insert")
                    break
    if d.trigger.requires_change and on_update:
        b.why.append(
            "flow requires the criteria to become true: condition values are set on update"
        )
        for k in list(b.update):
            b.create.pop(k, None)
    recipe: dict[str, Any] = {"object": obj, "create": b.create}
    if b.update:
        recipe["update"] = b.update
    if b.why:
        recipe["_why"] = "; ".join(b.why)
    if b.needs:
        recipe["_needs"] = b.needs
    return recipe


def _input_value(v: Variable) -> Any:
    t = (v.type or "").lower()
    if t in {"string", "text", "id"}:
        return _TEXT
    if t in {"number", "currency", "double", "integer"}:
        return 1
    if t in {"boolean"}:
        return True
    if t in {"date"}:
        return datetime.now(UTC).date().isoformat()
    if t in {"datetime"}:
        return datetime.now(UTC).replace(microsecond=0).isoformat()
    return None


def _autolaunched(
    d: ProcessDefinition,
    fields: dict[str, dict[str, SchemaNode]],
    definitions: list[ProcessDefinition],
) -> dict[str, Any] | None:
    inputs_vars = [v for v in d.variables if v.is_input]
    recipe: dict[str, Any] = {"inputs": {}}
    needs: list[str] = []
    why: list[str] = []
    lookups = [s for s in d.steps if s.kind is StepKind.LOOKUP and s.object]
    for v in inputs_vars:
        t = (v.type or "").lower()
        if t in {"sobject", "apex"} or v.object:
            needs.append(f"input {v.name}: {v.type}{' ' + v.object if v.object else ''} — supply")
            continue
        if v.name.lower().endswith("id") and lookups and t in {"string", "id", "text"}:
            obj = lookups[0].object or ""
            b = _Builder(obj, fields.get(obj.lower(), {}))
            b.required_fields()
            _validation_guards(b, definitions)
            _quiet_other_flows(b, d, _flow_definitions(definitions))
            recipe["object"] = obj
            recipe["create"] = b.create
            recipe["inputs"][v.name] = "$record.Id"
            why.append(f"{v.name} receives a fresh {obj} (first lookup is on {obj})")
            why.extend(b.why)
            needs.extend(b.needs)
            continue
        val = _input_value(v)
        if val is None:
            needs.append(f"input {v.name}: {v.type or 'untyped'} — supply")
        else:
            recipe["inputs"][v.name] = val
    if why:
        recipe["_why"] = "; ".join(why)
    if needs:
        recipe["_needs"] = needs
    return recipe


def generate_recipes(
    definitions: list[ProcessDefinition],
    schema: SchemaSnapshot | None,
    *,
    existing: dict[str, Any] | None = None,
    only: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Recipes keyed by flow API name; hand-written ``existing`` entries win unchanged."""
    fields = _field_index(schema)
    out: dict[str, dict[str, Any]] = {}
    wanted = {n for n in only} if only else None
    for d in definitions:
        if d.kind not in _FLOW_KINDS or not d.active:
            continue
        if wanted is not None and d.name not in wanted:
            continue
        if existing and d.name in existing:
            out[d.name] = dict(existing[d.name])
            continue
        recipe: dict[str, Any] | None
        if d.trigger.kind is TriggerKind.RECORD_SAVE:
            recipe = _record_triggered(d, fields, definitions)
        elif d.trigger.kind is TriggerKind.INVOCATION:
            recipe = _autolaunched(d, fields, definitions)
        else:
            continue  # scheduled, screen, platform-event: not verifiable on demand
        if recipe is not None:
            recipe["_generated"] = True
            out[d.name] = recipe
    return out
