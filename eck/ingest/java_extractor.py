"""CAP-2 — derives structural knowledge from Java source (BR-06..BR-13).

Tuned for Jmix 2.x, which is what this estate actually is. That matters:
this codebase has ZERO @RestController. Its entry points are 848 @Route
view controllers, and its behaviour lives in @Subscribe handlers and
*ServiceBean classes. A generic Spring MVC extractor would report an
estate with no way in.

Two passes:
  1. parse every file, emit type/member nodes, build a global FQN index
  2. resolve references against that index and emit edges

Anything pass 2 cannot resolve becomes an unresolved_ref row rather than a
missing edge (BR-13), and anything resolved by heuristic is labelled
'inferred' rather than 'derived' (BR-10).
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from ..register import Asset
from ..store.db import edge_id, node_id, sha256
from ..store.vocab import EdgeKind, NodeKind, Origin

JAVA = Language(tree_sitter_java.language())

TYPE_DECLS = {"class_declaration", "interface_declaration",
              "enum_declaration", "record_declaration"}

DECL_TO_KIND = {
    "class_declaration": NodeKind.CLASS,
    "interface_declaration": NodeKind.INTERFACE,
    "enum_declaration": NodeKind.ENUM,
    "record_declaration": NodeKind.RECORD,
}

# --- Jmix / Spring classification ------------------------------------------
# Ordered: the first matching rule wins, so the most specific sits first.

ENTITY_ANNOTATIONS = {"JmixEntity", "Entity"}
VIEW_ANNOTATIONS = {"ViewController", "Route"}
ROLE_ANNOTATIONS = {"ResourceRole", "RowLevelRole"}
SERVICE_ANNOTATIONS = {"Service", "Component", "Configuration"}
INJECT_ANNOTATIONS = {"Autowired", "Inject", "ViewComponent", "PersistenceContext"}
HANDLER_ANNOTATIONS = {"Subscribe", "EventListener", "Install", "Supply"}
INTEGRATION_HINTS = ("RestTemplate", "WebClient", "HttpClient", "JdbcTemplate",
                     "DataSource", "FeignClient")


@dataclass
class TypeInfo:
    """Everything pass 2 needs to know about a type it did not parse."""
    node_ref: str
    asset_id: str
    fqn: str
    kind: NodeKind
    methods: dict[str, list[str]] = field(default_factory=dict)  # name -> [node_id]
    fields: dict[str, str] = field(default_factory=dict)         # name -> simple type


@dataclass
class PendingRef:
    """A reference site awaiting the global type index."""
    asset_id: str
    src_id: str            # enclosing method node
    owner_fqn: str         # the type the reference was written in
    kind: str              # EdgeKind value we intend to emit
    receiver: str          # '' for a bare call
    member: str            # method or field name being referenced
    raw: str
    path: str
    line: int
    imports: dict[str, str]
    package: str


@dataclass
class ExtractResult:
    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    files_seen: int = 0
    files_parsed: int = 0


# --------------------------------------------------------------- tree helpers

def _text(node: Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


def _annotations(node: Node) -> dict[str, str]:
    """Annotation simple-name -> raw argument text ('' for marker annotations)."""
    found: dict[str, str] = {}
    for child in node.children:
        if child.type != "modifiers":
            continue
        for mod in child.children:
            if mod.type in ("marker_annotation", "annotation"):
                name = _text(mod.child_by_field_name("name")).split(".")[-1]
                found[name] = _text(mod.child_by_field_name("arguments"))
    return found


def _annotation_arg(raw: str, key: str) -> str | None:
    """Pull key = "value" out of an annotation argument list, cheaply."""
    if not raw:
        return None
    for part in raw.strip("()").split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            if k.strip() == key:
                return v.strip().strip('"')
        elif key == "value" and part.strip().startswith('"'):
            return part.strip().strip('"')
    return None


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _simple_type(type_text: str) -> str:
    """Strip generics and array markers: List<Foo> -> List, Foo[] -> Foo."""
    return type_text.split("<")[0].replace("[]", "").strip().split(".")[-1]


# --------------------------------------------------------------- classification

def classify(annotations: dict[str, str], class_name: str,
             decl_kind: NodeKind) -> tuple[NodeKind, dict[str, Any]]:
    """Map a Java type onto the knowledge vocabulary, Jmix-aware."""
    attrs: dict[str, Any] = {}

    if ENTITY_ANNOTATIONS & annotations.keys():
        table = _annotation_arg(annotations.get("Table", ""), "name")
        if table:
            attrs["table"] = table
        return NodeKind.ENTITY, attrs

    if VIEW_ANNOTATIONS & annotations.keys():
        route = _annotation_arg(annotations.get("Route", ""), "value")
        if route:
            attrs["route"] = route
        descriptor = _annotation_arg(annotations.get("ViewDescriptor", ""), "value")
        if descriptor:
            attrs["descriptor"] = descriptor
        entity = _annotation_arg(annotations.get("EditedEntityContainer", ""), "value")
        if entity:
            attrs["edits"] = entity
        return NodeKind.VIEW, attrs

    if ROLE_ANNOTATIONS & annotations.keys():
        code = _annotation_arg(annotations.get("ResourceRole", ""), "code") \
            or _annotation_arg(annotations.get("RowLevelRole", ""), "code")
        if code:
            attrs["role_code"] = code
        return NodeKind.SECURITY_ROLE, attrs

    # Jmix convention: the *ServiceBean suffix is the service implementation.
    if class_name.endswith(("ServiceBean", "Bean")) or SERVICE_ANNOTATIONS & annotations.keys():
        return NodeKind.SERVICE, attrs

    return decl_kind, attrs


# --------------------------------------------------------------- pass 1

class JavaExtractor:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.parser = Parser(JAVA)
        self.types: dict[str, TypeInfo] = {}     # fqn -> TypeInfo
        self.result = ExtractResult()
        # Reference sites collected in pass 1, resolved in pass 2 once the
        # global type index is complete.
        self.pending: list[PendingRef] = []

    # -- file selection ----------------------------------------------------

    @staticmethod
    def source_files(asset: Asset) -> list[Path]:
        excluded = [e.path_glob for e in asset.exclusions]
        files: list[Path] = []
        for path in asset.abs_path.rglob("*.java"):
            rel = str(path.relative_to(asset.abs_path))
            posix = path.as_posix()
            if any(fnmatch.fnmatch(posix, g) or fnmatch.fnmatch(rel, g)
                   for g in excluded):
                continue
            files.append(path)
        return sorted(files)  # BR-11: stable order in, stable output out

    # -- pass 1 ------------------------------------------------------------

    def parse_asset(self, asset: Asset) -> None:
        for path in self.source_files(asset):
            self.result.files_seen += 1
            rel = str(path.relative_to(asset.abs_path))
            try:
                src = path.read_bytes()
                tree = self.parser.parse(src)
            except Exception as exc:                     # pragma: no cover
                self.result.failures.append(dict(
                    asset_id=asset.id, path=rel, reason="read_or_parse_error",
                    detail=f"{type(exc).__name__}: {exc}", run_id=self.run_id))
                continue

            if tree.root_node.has_error:
                # Still extract what we can, but record it — BR-13.
                self.result.failures.append(dict(
                    asset_id=asset.id, path=rel, reason="syntax_error",
                    detail="tree-sitter reported ERROR nodes; partial extraction",
                    run_id=self.run_id))
            else:
                self.result.files_parsed += 1

            self._file(asset, rel, src, tree.root_node)

    def _file(self, asset: Asset, rel: str, src: bytes, root: Node) -> None:
        package = ""
        imports: dict[str, str] = {}
        for child in root.children:
            if child.type == "package_declaration":
                package = _text(child).replace("package", "").strip(" ;\n")
            elif child.type == "import_declaration":
                fqn = _text(child).replace("import", "").replace("static", "").strip(" ;\n")
                imports[fqn.split(".")[-1]] = fqn

        for node in root.children:
            if node.type in TYPE_DECLS:
                self._type(asset, rel, src, node, package, imports)

    def _type(self, asset: Asset, rel: str, src: bytes, node: Node,
              package: str, imports: dict[str, str], outer: str = "") -> None:
        name = _text(node.child_by_field_name("name"))
        if not name:
            return
        fqn = f"{outer}.{name}" if outer else (f"{package}.{name}" if package else name)

        annotations = _annotations(node)
        kind, attrs = classify(annotations, name, DECL_TO_KIND[node.type])
        attrs["annotations"] = sorted(annotations)
        if "Transactional" in annotations:
            attrs["transactional"] = True

        tid = node_id(asset.id, kind.value, fqn)
        self.result.nodes.append(dict(
            id=tid, asset_id=asset.id, kind=kind.value, name=name, fqn=fqn,
            signature=None, path=rel,
            start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
            span_sha=sha256(src[node.start_byte:node.end_byte]),
            origin=Origin.DERIVED.value, confidence=1.0,
            attrs=attrs, run_id=self.run_id))

        info = TypeInfo(node_ref=tid, asset_id=asset.id, fqn=fqn, kind=kind)
        self.types[fqn] = info

        # A Jmix entity with @Table also denotes a physical store (BR-07).
        if kind is NodeKind.ENTITY and attrs.get("table"):
            store_fqn = f"table:{attrs['table']}"
            sid = node_id(asset.id, NodeKind.STORE.value, store_fqn)
            self.result.nodes.append(dict(
                id=sid, asset_id=asset.id, kind=NodeKind.STORE.value,
                name=attrs["table"], fqn=store_fqn, signature=None, path=rel,
                start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
                span_sha=sha256(src[node.start_byte:node.end_byte]),
                origin=Origin.DERIVED.value, confidence=1.0,
                attrs={"physical_table": attrs["table"]}, run_id=self.run_id))
            self.result.edges.append(dict(
                id=edge_id(tid, sid, EdgeKind.DECLARES.value, node.start_point[0] + 1),
                src_id=tid, dst_id=sid, kind=EdgeKind.DECLARES.value, path=rel,
                start_line=node.start_point[0] + 1, origin=Origin.DERIVED.value,
                confidence=1.0, attrs={}, run_id=self.run_id))

        body = node.child_by_field_name("body")
        if body is None:
            return

        self._members(asset, rel, src, body, fqn, info, imports, package)

        for child in body.children:
            if child.type in TYPE_DECLS:
                self._type(asset, rel, src, child, package, imports, outer=fqn)

    def _members(self, asset: Asset, rel: str, src: bytes, body: Node,
                 owner_fqn: str, info: TypeInfo, imports: dict[str, str],
                 package: str) -> None:
        # Fields first: a method body may reference a field declared below it,
        # so the field-type map must be complete before any method is scanned.
        for child in body.children:
            if child.type == "field_declaration":
                self._field(asset, rel, src, child, owner_fqn, info, imports, package)
        for child in body.children:
            if child.type in ("method_declaration", "constructor_declaration"):
                self._method(asset, rel, src, child, owner_fqn, info,
                             imports, package)

    def _method(self, asset: Asset, rel: str, src: bytes, node: Node,
                owner_fqn: str, info: TypeInfo,
                imports: dict[str, str], package: str) -> None:
        name = _text(node.child_by_field_name("name")) or "<init>"
        params = _text(node.child_by_field_name("parameters"))
        signature = f"{name}{params}"
        fqn = f"{owner_fqn}#{signature}"

        annotations = _annotations(node)
        is_handler = bool(HANDLER_ANNOTATIONS & annotations.keys())
        kind = NodeKind.EVENT_HANDLER if is_handler else NodeKind.METHOD

        attrs: dict[str, Any] = {"annotations": sorted(annotations)}
        if "Transactional" in annotations:
            attrs["transactional"] = True          # feeds BR-28
        if "Subscribe" in annotations:
            target = _annotation_arg(annotations["Subscribe"], "id")
            if target:
                attrs["subscribes_to"] = target

        mid = node_id(asset.id, kind.value, fqn)
        self.result.nodes.append(dict(
            id=mid, asset_id=asset.id, kind=kind.value, name=name, fqn=fqn,
            signature=signature, path=rel,
            start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
            span_sha=sha256(src[node.start_byte:node.end_byte]),
            origin=Origin.DERIVED.value, confidence=1.0,
            attrs=attrs, run_id=self.run_id))

        self.result.edges.append(dict(
            id=edge_id(mid, info.node_ref, EdgeKind.BELONGS_TO.value,
                       node.start_point[0] + 1),
            src_id=mid, dst_id=info.node_ref, kind=EdgeKind.BELONGS_TO.value,
            path=rel, start_line=node.start_point[0] + 1,
            origin=Origin.DERIVED.value, confidence=1.0, attrs={},
            run_id=self.run_id))

        info.methods.setdefault(name, []).append(mid)

        if node.child_by_field_name("body") is not None:
            self._references(asset, rel, node, mid, owner_fqn, info,
                             imports, package)

    def _references(self, asset: Asset, rel: str, decl: Node, src_id: str,
                    owner_fqn: str, info: TypeInfo,
                    imports: dict[str, str], package: str) -> None:
        """Record every call site. Resolution happens in pass 2.

        Takes the whole method declaration, not just its body: formal
        parameters are a SIBLING of the body, so a body-only walk silently
        loses every call made on a parameter.
        """
        body = decl.child_by_field_name("body")
        # Parameter and local types widen what pass 2 can resolve; without
        # them a call on either goes to unresolved_ref.
        locals_: dict[str, str] = {}
        params = decl.child_by_field_name("parameters")
        if params is not None:
            for node in _walk(params):
                if node.type in ("formal_parameter", "spread_parameter"):
                    locals_[_text(node.child_by_field_name("name"))] = \
                        _simple_type(_text(node.child_by_field_name("type")))
        for node in _walk(body):
            if node.type == "local_variable_declaration":
                tname = _simple_type(_text(node.child_by_field_name("type")))
                for d in node.children:
                    if d.type == "variable_declarator":
                        locals_[_text(d.child_by_field_name("name"))] = tname

        for node in _walk(body):
            if node.type != "method_invocation":
                continue
            member = _text(node.child_by_field_name("name"))
            obj = node.child_by_field_name("object")
            receiver = _text(obj) if obj is not None else ""
            # Resolve the receiver expression to a type name where we can.
            recv_type = ""
            if receiver in ("", "this"):
                recv_type = owner_fqn
            elif receiver in info.fields:
                recv_type = info.fields[receiver]
            elif receiver in locals_:
                recv_type = locals_[receiver]
            elif receiver[:1].isupper():
                recv_type = receiver          # static call on a type name

            self.pending.append(PendingRef(
                asset_id=asset.id, src_id=src_id, owner_fqn=owner_fqn,
                kind=EdgeKind.INVOKES.value, receiver=recv_type, member=member,
                raw=f"{receiver}.{member}()" if receiver else f"{member}()",
                path=rel, line=node.start_point[0] + 1,
                imports=imports, package=package))

    def _field(self, asset: Asset, rel: str, src: bytes, node: Node,
               owner_fqn: str, info: TypeInfo, imports: dict[str, str],
               package: str) -> None:
        type_text = _text(node.child_by_field_name("type"))
        simple = _simple_type(type_text)
        annotations = _annotations(node)

        for declarator in node.children:
            if declarator.type != "variable_declarator":
                continue
            fname = _text(declarator.child_by_field_name("name"))
            fqn = f"{owner_fqn}.{fname}"
            info.fields[fname] = simple
            attrs: dict[str, Any] = {"type": simple,
                                     "annotations": sorted(annotations)}
            if any(h in type_text for h in INTEGRATION_HINTS):
                attrs["integration_hint"] = simple

            fid = node_id(asset.id, NodeKind.FIELD.value, fqn)
            self.result.nodes.append(dict(
                id=fid, asset_id=asset.id, kind=NodeKind.FIELD.value,
                name=fname, fqn=fqn, signature=type_text, path=rel,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                span_sha=sha256(src[node.start_byte:node.end_byte]),
                origin=Origin.DERIVED.value, confidence=1.0,
                attrs=attrs, run_id=self.run_id))
            self.result.edges.append(dict(
                id=edge_id(fid, info.node_ref, EdgeKind.BELONGS_TO.value,
                           node.start_point[0] + 1),
                src_id=fid, dst_id=info.node_ref,
                kind=EdgeKind.BELONGS_TO.value, path=rel,
                start_line=node.start_point[0] + 1,
                origin=Origin.DERIVED.value, confidence=1.0, attrs={},
                run_id=self.run_id))

    # -- pass 2 ------------------------------------------------------------

    def resolve(self) -> None:
        """Turn pending reference sites into edges (BR-08) or into visible
        unresolved rows (BR-13). Nothing is dropped in silence.

        Confidence and origin encode HOW we resolved, which is what makes
        BR-10 ("label indirect connections as inferred") mean something:

          import-qualified type + unique method   derived  0.95
          same-package type      + unique method  derived  0.90
          type resolved, method name ambiguous    inferred 0.60
          type resolved, method not found         inferred 0.50  (class-level)
          type not resolvable                     -> unresolved_ref
        """
        by_simple: dict[str, list[TypeInfo]] = {}
        for fqn, info in self.types.items():
            by_simple.setdefault(fqn.split(".")[-1], []).append(info)

        for ref in self.pending:
            target, how = self._resolve_type(ref, by_simple)
            if target is None:
                self.result.unresolved.append(dict(
                    asset_id=ref.asset_id, src_id=ref.src_id, kind=ref.kind,
                    raw=ref.raw, path=ref.path, start_line=ref.line,
                    reason=how, run_id=self.run_id))
                continue

            candidates = target.methods.get(ref.member, [])
            if len(candidates) == 1:
                dst, origin, conf = candidates[0], Origin.DERIVED.value, \
                    (0.95 if how == "import" else 0.90)
            elif len(candidates) > 1:
                dst, origin, conf = candidates[0], Origin.INFERRED.value, 0.60
            else:
                # Method not found on the type — inherited, or from a library
                # we do not parse. Record the class-level relationship.
                dst, origin, conf = target.node_ref, Origin.INFERRED.value, 0.50

            self.result.edges.append(dict(
                id=edge_id(ref.src_id, dst, ref.kind, ref.line),
                src_id=ref.src_id, dst_id=dst, kind=ref.kind, path=ref.path,
                start_line=ref.line, origin=origin, confidence=conf,
                attrs={"via": how, "member": ref.member}, run_id=self.run_id))

    def _resolve_type(self, ref: PendingRef,
                      by_simple: dict[str, list[TypeInfo]]
                      ) -> tuple[TypeInfo | None, str]:
        recv = ref.receiver
        if not recv:
            return None, "receiver_not_inferable"

        # Already a fully-qualified name we know (the this/self case).
        if recv in self.types:
            return self.types[recv], "self"

        simple = recv.split(".")[-1]

        # 1. An explicit import pins the type exactly.
        imported = ref.imports.get(simple)
        if imported and imported in self.types:
            return self.types[imported], "import"

        # 2. Same package, no import needed.
        same_pkg = f"{ref.package}.{simple}" if ref.package else simple
        if same_pkg in self.types:
            return self.types[same_pkg], "same_package"

        # 3. A unique simple-name match anywhere in the estate. This is a
        #    guess, and it is why the caller downgrades confidence.
        matches = by_simple.get(simple, [])
        if len(matches) == 1:
            return matches[0], "unique_simple_name"
        if len(matches) > 1:
            return None, "ambiguous_simple_name"

        return None, "type_outside_estate"
