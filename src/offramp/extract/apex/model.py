"""Output contract of the Apex analyzer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SoqlRef:
    """One SOQL / SOSL query found in the source."""

    sobject: str
    fields: list[str] = field(default_factory=list)  # bare or dotted ('Owner.Email')
    where_fields: list[str] = field(default_factory=list)
    relationships: list[str] = field(default_factory=list)  # subquery relationship names
    dynamic: bool = False  # Database.query('...') / string-built
    raw: str = ""


@dataclass
class DmlRef:
    """One DML statement or Database.* call."""

    op: str  # insert | update | upsert | delete | undelete | merge
    target: str  # the expression text (variable name usually)
    sobject: str | None = None  # resolved from local declarations when possible
    via_database_class: bool = False


@dataclass
class AsyncRef:
    """System.schedule / enqueueJob / executeBatch / scheduleBatch."""

    mechanism: str  # schedule | enqueue | batch | future
    target_class: str | None = None


@dataclass
class ApexAnalysis:
    """Everything the dependency builder needs from one Apex class or trigger."""

    name: str
    kind: str  # class | interface | enum | trigger | unknown
    sharing: str | None = None  # with | without | inherited
    modifiers: list[str] = field(default_factory=list)  # global public abstract virtual
    is_test: bool = False
    extends: str | None = None
    implements: list[str] = field(default_factory=list)
    inner_types: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    class_references: list[str] = field(default_factory=list)
    method_calls: list[str] = field(default_factory=list)  # 'Qualifier.method'
    sobject_references: list[str] = field(default_factory=list)
    field_references: list[str] = field(default_factory=list)  # 'Object.Field'
    field_writes: list[str] = field(default_factory=list)  # assigned fields ('Lead.OwnerId')
    soql: list[SoqlRef] = field(default_factory=list)
    dml: list[DmlRef] = field(default_factory=list)
    callouts: list[str] = field(default_factory=list)  # 'Http', 'WebServiceCallout', 'Continuation'
    named_credentials: list[str] = field(default_factory=list)
    async_calls: list[AsyncRef] = field(default_factory=list)
    type_forname_literals: list[str] = field(default_factory=list)
    dynamic_access: list[str] = field(default_factory=list)  # see _DYNAMIC_PATTERNS
    custom_labels: list[str] = field(default_factory=list)
    custom_settings: list[str] = field(default_factory=list)
    trigger_object: str | None = None
    trigger_events: list[str] = field(default_factory=list)
    lines: int = 0
    tokens: int = 0
    branches: int = 0  # if / else if / for / while / do / switch / ternary
    methods: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
