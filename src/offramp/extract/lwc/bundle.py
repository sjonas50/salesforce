"""LWC bundle extractor (regex-based classifier).

A tree-sitter pass is a possible upgrade; the regex pass is sufficient for:

* identifying ``@salesforce/apex/`` imports (high-confidence Apex links)
* counting business-logic density signals (LOC, conditionals, fetch calls)
* classifying components into :class:`LWCClassification`

Tests assert the classifier shape, so swapping the regex internals for a
tree-sitter implementation later is a localized change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from offramp.core.models import CategoryName
from offramp.extract.categories.base import CategoryExtractor
from offramp.extract.categories.xml_utils import keep_sources
from offramp.extract.pull.reconciler import ReconciledRecord


class LWCClassification(StrEnum):
    """Where the bundle's business logic lives."""

    UI_ONLY = "ui_only"
    MIXED = "mixed"
    BUSINESS_LOGIC_HEAVY = "business_logic_heavy"


@dataclass(frozen=True)
class JSAnalysis:
    """Regex-derived signals for one .js file in an LWC bundle."""

    filename: str
    lines: int
    apex_imports: tuple[str, ...]  # @salesforce/apex/ClassName.method
    wire_calls: int
    imperative_apex_calls: int
    fetch_calls: int
    conditionals: int
    classification: LWCClassification


_APEX_IMPORT_RE = re.compile(r"""@salesforce/apex/([A-Za-z0-9_]+\.[A-Za-z0-9_]+)""")
_SCHEMA_IMPORT_RE = re.compile(r"""@salesforce/schema/([A-Za-z0-9_]+(?:\.[A-Za-z0-9_.]+)?)""")
_MESSAGE_CHANNEL_RE = re.compile(r"""@salesforce/messageChannel/([A-Za-z0-9_]+)""")
_LABEL_IMPORT_RE = re.compile(r"""@salesforce/label/([A-Za-z0-9_.]+)""")
_UI_API_FIELDS_RE = re.compile(r"""fields\s*:\s*\[([^\]]*)\]""")
_HTML_RECORD_FORM_RE = re.compile(r"""object-api-name\s*=\s*["']([A-Za-z0-9_]+)["']""")
_HTML_FIELD_RE = re.compile(r"""field-name\s*=\s*["']([A-Za-z0-9_]+)["']""")
# Child components in templates: ``<c-error-panel>`` is the bundle ``errorPanel``.
# JS-side composition: ``import { reduceErrors } from "c/ldsUtils"``.
_JS_C_IMPORT_RE = re.compile(r"""from\s+["']c/([A-Za-z0-9_]+)["']""")
_HTML_CHILD_RE = re.compile(r"""<c-([a-z][a-z0-9]*(?:-[a-z0-9]+)*)[\s>/]""")


def _kebab_to_camel(name: str) -> str:
    head, *rest = name.split("-")
    return head + "".join(part.capitalize() for part in rest)


_WIRE_RE = re.compile(r"@wire\s*\(")
_IMPERATIVE_APEX_RE = re.compile(r"\b[A-Za-z_]\w*\s*\(\s*\{[^}]*\}\s*\)\s*\.then\b")
_FETCH_RE = re.compile(r"\bfetch\s*\(")
_COND_RE = re.compile(r"\b(if|else if|switch|case)\b")


def analyze_js(filename: str, source: str) -> JSAnalysis:
    """Run the regex pass + classifier against one .js file's source."""
    apex_imports = tuple(sorted(set(_APEX_IMPORT_RE.findall(source))))
    wire_calls = len(_WIRE_RE.findall(source))
    imperative_apex = len(_IMPERATIVE_APEX_RE.findall(source))
    fetch_calls = len(_FETCH_RE.findall(source))
    conditionals = len(_COND_RE.findall(source))
    lines = source.count("\n") + 1

    score = imperative_apex * 3 + wire_calls + conditionals + fetch_calls
    if score == 0 and lines < 50:
        cls = LWCClassification.UI_ONLY
    elif score >= 8 or lines >= 200:
        cls = LWCClassification.BUSINESS_LOGIC_HEAVY
    else:
        cls = LWCClassification.MIXED

    return JSAnalysis(
        filename=filename,
        lines=lines,
        apex_imports=apex_imports,
        wire_calls=wire_calls,
        imperative_apex_calls=imperative_apex,
        fetch_calls=fetch_calls,
        conditionals=conditionals,
        classification=cls,
    )


class LWCBundleExtractor(CategoryExtractor):
    """LWC bundle → canonical dict with per-file analyses + bundle classification."""

    category: ClassVar[CategoryName] = CategoryName.LWC_BUNDLE

    def parse_payload(self, record: ReconciledRecord) -> dict[str, Any]:
        files = record.payload.get("files", {})
        if not isinstance(files, dict):
            raise ValueError(f"LWC bundle {record.api_name} missing 'files' map")
        analyses = [
            analyze_js(name, contents) for name, contents in files.items() if name.endswith(".js")
        ]
        if not analyses:
            return {
                "references": {
                    "apex_classes": [],
                    "apex_methods": [],
                    "objects": [],
                    "fields": [],
                    "custom_labels": [],
                },
                "files": list(files.keys()),
                "sources": keep_sources(files),
                "classification": LWCClassification.UI_ONLY.value,
                "apex_imports": [],
                "js_analyses": [],
            }
        # Bundle classification = the worst (most logic-heavy) file's class.
        order = {
            LWCClassification.UI_ONLY: 0,
            LWCClassification.MIXED: 1,
            LWCClassification.BUSINESS_LOGIC_HEAVY: 2,
        }
        worst = max(analyses, key=lambda a: order[a.classification]).classification
        all_imports = sorted({imp for a in analyses for imp in a.apex_imports})
        js_sources = "\n".join(v for k, v in files.items() if k.endswith(".js"))
        html_sources = "\n".join(v for k, v in files.items() if k.endswith(".html"))
        schema_refs = sorted(set(_SCHEMA_IMPORT_RE.findall(js_sources)))
        objects = sorted(
            {r.split(".", 1)[0] for r in schema_refs}
            | set(_HTML_RECORD_FORM_RE.findall(html_sources))
        )
        fields = sorted(r for r in schema_refs if "." in r)
        # <lightning-record-form object-api-name="Lead"> + field-name="Email"
        html_objs = _HTML_RECORD_FORM_RE.findall(html_sources)
        if len(html_objs) == 1:
            fields = sorted(
                set(fields) | {f"{html_objs[0]}.{f}" for f in _HTML_FIELD_RE.findall(html_sources)}
            )
        return {
            "references": {
                "apex_classes": sorted({imp.split(".", 1)[0] for imp in all_imports}),
                "lwc_bundles": sorted(
                    {_kebab_to_camel(m) for m in _HTML_CHILD_RE.findall(html_sources)}
                    | set(_JS_C_IMPORT_RE.findall(js_sources))
                ),
                "apex_methods": all_imports,
                "objects": objects,
                "fields": fields,
                "custom_labels": sorted(set(_LABEL_IMPORT_RE.findall(js_sources))),
                # Lightning Message Service couples components that never import each other.
                "message_channels": sorted(set(_MESSAGE_CHANNEL_RE.findall(js_sources))),
            },
            "files": sorted(files.keys()),
            "sources": keep_sources(files),
            "classification": worst.value,
            "apex_imports": all_imports,
            "js_analyses": [
                {
                    "filename": a.filename,
                    "lines": a.lines,
                    "apex_imports": list(a.apex_imports),
                    "wire_calls": a.wire_calls,
                    "imperative_apex_calls": a.imperative_apex_calls,
                    "fetch_calls": a.fetch_calls,
                    "conditionals": a.conditionals,
                    "classification": a.classification.value,
                }
                for a in analyses
            ],
        }
