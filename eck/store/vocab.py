"""Central definition of the allowed knowledge vocabulary (BR-12).

Unknown kinds must fail visibly. This module is the single authority; the
SQL CHECK constraints in schema.sql mirror it, so a drift between the two
surfaces as a constraint violation on write rather than as silent data loss.
"""
from enum import Enum


class NodeKind(str, Enum):
    MODULE = "module"
    PACKAGE = "package"
    CLASS = "class"
    INTERFACE = "interface"
    ENUM = "enum"
    RECORD = "record"
    METHOD = "method"
    FIELD = "field"
    ENTITY = "entity"
    STORE = "store"
    VIEW = "view"
    ENTRY_POINT = "entry_point"
    SERVICE = "service"
    EVENT_HANDLER = "event_handler"
    SECURITY_ROLE = "security_role"
    CONFIG_PROPERTY = "config_property"
    INTEGRATION_POINT = "integration_point"


class EdgeKind(str, Enum):
    BELONGS_TO = "belongs_to"
    INVOKES = "invokes"
    READS = "reads"
    WRITES = "writes"
    EXPOSES = "exposes"
    EXTENDS = "extends"
    IMPLEMENTS = "implements"
    INJECTS = "injects"
    SUBSCRIBES = "subscribes"
    DECLARES = "declares"


class Origin(str, Enum):
    DERIVED = "derived"      # read directly out of the source
    CURATED = "curated"      # a human said so, and signed it
    INFERRED = "inferred"    # our best guess — BR-10 requires it be labelled


class UnknownKind(Exception):
    """Raised when something outside the vocabulary is emitted (BR-12)."""


def node_kind(value: str) -> NodeKind:
    try:
        return NodeKind(value)
    except ValueError as exc:
        raise UnknownKind(f"node kind {value!r} is not in the vocabulary") from exc


def edge_kind(value: str) -> EdgeKind:
    try:
        return EdgeKind(value)
    except ValueError as exc:
        raise UnknownKind(f"edge kind {value!r} is not in the vocabulary") from exc
