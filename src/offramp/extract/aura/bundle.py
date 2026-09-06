"""Aura bundle extractor.

Aura is the pre-LWC component model; orgs still ship it and App Builder still
places it. Easy Spaces wraps every LWC in an Aura component to expose it to
App Builder and launches its flows from Aura. What a bundle depends on lives in
three places, all read here by regex (no JS parser, mirroring the LWC lane):

* markup (``.cmp`` / ``.app`` / ``.evt`` / ``.intf``): ``controller="ApexClass"``,
  child components ``<c:name>``, events ``type="c:evt"`` / ``event="c:evt"``,
  ``<lightning:messageChannel type="X__c">``, ``objectApiName="Account"`` and
  ``fields="['Name']"`` on record-aware base components, ``$Label.c.X``;
* controller/helper/renderer JS: ``component.get("c.method")`` (Apex actions on
  the controller class), ``$A.createComponent("c:name")``, ``flow.startFlow("Name")``.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from offramp.core.models import CategoryName
from offramp.extract.categories.base import CategoryExtractor
from offramp.extract.pull.reconciler import ReconciledRecord

_CONTROLLER_RE = re.compile(
    r"""<aura:(?:component|application)\b[^>]*\bcontroller\s*=\s*["']([A-Za-z0-9_.]+)["']""", re.S
)
_CHILD_RE = re.compile(r"""<c:([A-Za-z][A-Za-z0-9_]*)\b""")
_EVENT_RE = re.compile(r"""\b(?:type|event)\s*=\s*["']c:([A-Za-z][A-Za-z0-9_]*)["']""")
_CREATE_RE = re.compile(r"""["']c:([A-Za-z][A-Za-z0-9_]*)["']""")
_APEX_ACTION_RE = re.compile(r"""\.get\(\s*["']c\.([A-Za-z_][A-Za-z0-9_]*)["']\s*\)""")
_FLOW_RE = re.compile(r"""startFlow\(\s*["']([A-Za-z0-9_]+)["']""")
_MSG_CHANNEL_RE = re.compile(
    r"""<lightning:messageChannel\b[^>]*\btype\s*=\s*["']([A-Za-z0-9_]+)["']"""
)
_OBJECT_RE = re.compile(
    r"""\b(?:objectApiName|sObjectName|sobjectType)\s*=\s*["']([A-Za-z][A-Za-z0-9_]*)["']"""
)
_FIELDS_RE = re.compile(r"""\bfields\s*=\s*["']\[([^\]]*)\]["']""")
_QUOTED_RE = re.compile(r"""["']([A-Za-z0-9_.]+)["']""")
_LABEL_RE = re.compile(r"""\$Label\.c\.([A-Za-z0-9_]+)""")
_MARKUP_SUFFIXES = (".cmp", ".app", ".evt", ".intf")


def _kind(files: dict[str, str]) -> str:
    for suffix, kind in (
        (".app", "application"),
        (".evt", "event"),
        (".intf", "interface"),
        (".cmp", "component"),
    ):
        if any(name.endswith(suffix) for name in files):
            return kind
    return "component"


class AuraBundleExtractor(CategoryExtractor):
    """Aura bundle -> canonical dict: references + a coarse classification."""

    category: ClassVar[CategoryName] = CategoryName.AURA_BUNDLE

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        files = record.payload.get("files", {})
        if not isinstance(files, dict):
            raise ValueError(f"Aura bundle {record.api_name} missing 'files' map")
        markup = "\n".join(v for k, v in files.items() if k.endswith(_MARKUP_SUFFIXES))
        js = "\n".join(v for k, v in files.items() if k.endswith(".js"))
        apex_classes = sorted({c.split(".")[-1] for c in _CONTROLLER_RE.findall(markup)})
        methods = sorted(set(_APEX_ACTION_RE.findall(js)))
        children = (
            set(_CHILD_RE.findall(markup))
            | set(_EVENT_RE.findall(markup))
            | set(_CREATE_RE.findall(js))
        )
        children.discard(record.api_name)
        objects = sorted(set(_OBJECT_RE.findall(markup)))
        fields: set[str] = set()
        if len(objects) == 1:
            for group in _FIELDS_RE.findall(markup):
                fields.update(f"{objects[0]}.{f}" for f in _QUOTED_RE.findall(group))
        lines = js.count("\n") + markup.count("\n") + 2
        if apex_classes or methods:
            classification = "mixed" if lines < 200 else "business_logic_heavy"
        else:
            classification = "ui_only"
        return {
            "kind": _kind(files),
            "files": sorted(files),
            "classification": classification,
            "lines": lines,
            "references": {
                "apex_classes": apex_classes,
                "apex_methods": [f"{c}.{m}" for c in apex_classes for m in methods],
                # LWC or Aura; markup does not say which, the graph resolves it.
                "lwc_bundles": sorted(children, key=str.lower),
                "flows": sorted(set(_FLOW_RE.findall(js))),
                "message_channels": sorted(set(_MSG_CHANNEL_RE.findall(markup))),
                "objects": objects,
                "fields": sorted(fields),
                "custom_labels": sorted(set(_LABEL_RE.findall(markup + js))),
            },
        }
