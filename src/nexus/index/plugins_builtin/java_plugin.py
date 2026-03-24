"""Java language parser plugin for Nexus.

Extracts classes, interfaces, methods, fields, and imports from Java source files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nexus.index.parser import Import, ParseResult, Symbol, _node_text, _child_text, _child_by_type

LANGUAGE = "java"
GRAMMAR = "java"
NAME = "Java (built-in)"


def extract(root: Any, source: bytes, path: Path) -> ParseResult:
    """Extract symbols and imports from a Java AST."""
    result = ParseResult()
    module_name = path.stem

    # Extract package name if present
    package = ""
    for node in root.children:
        if node.type == "package_declaration":
            for child in node.children:
                if child.type == "scoped_identifier" or child.type == "identifier":
                    package = _node_text(child, source)
            break

    qualified_prefix = f"{package}.{module_name}" if package else module_name

    for node in root.children:
        if node.type == "class_declaration":
            result.symbols.extend(_java_class(node, source, qualified_prefix))
        elif node.type == "interface_declaration":
            result.symbols.extend(_java_interface(node, source, qualified_prefix))
        elif node.type == "enum_declaration":
            result.symbols.append(_java_enum(node, source, qualified_prefix))
        elif node.type == "import_declaration":
            imp = _java_import(node, source)
            if imp:
                result.imports.append(imp)
        elif node.type == "record_declaration":
            result.symbols.append(_java_record(node, source, qualified_prefix))

    return result


def _java_class(node: Any, source: bytes, parent: str) -> list[Symbol]:
    symbols = []
    name = _child_text(node, "name", source)
    if not name:
        return symbols

    vis = _java_visibility(node)
    superclass = _child_by_type(node, "superclass")
    interfaces = _child_by_type(node, "super_interfaces")

    sig = f"class {name}"
    if superclass:
        sig += f" extends {_node_text(superclass, source).replace('extends ', '')}"
    if interfaces:
        sig += f" implements {_node_text(interfaces, source).replace('implements ', '')}"

    symbols.append(Symbol(
        name=name,
        qualified=f"{parent}.{name}",
        kind="class",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=sig,
        docstring=_java_javadoc(node, source),
        body_text=_node_text(node, source),
        visibility=vis,
        decorators=_java_annotations(node, source),
    ))

    # Extract methods and fields from class body
    body = _child_by_type(node, "class_body")
    if body:
        for child in body.children:
            if child.type == "method_declaration":
                symbols.append(_java_method(child, source, f"{parent}.{name}"))
            elif child.type == "constructor_declaration":
                symbols.append(_java_constructor(child, source, f"{parent}.{name}", name))
            elif child.type == "field_declaration":
                syms = _java_field(child, source, f"{parent}.{name}")
                symbols.extend(syms)
            elif child.type == "class_declaration":
                # Inner class
                symbols.extend(_java_class(child, source, f"{parent}.{name}"))
            elif child.type == "interface_declaration":
                symbols.extend(_java_interface(child, source, f"{parent}.{name}"))
            elif child.type == "enum_declaration":
                symbols.append(_java_enum(child, source, f"{parent}.{name}"))

    return symbols


def _java_interface(node: Any, source: bytes, parent: str) -> list[Symbol]:
    symbols = []
    name = _child_text(node, "name", source)
    if not name:
        return symbols

    vis = _java_visibility(node)

    symbols.append(Symbol(
        name=name,
        qualified=f"{parent}.{name}",
        kind="class",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=f"interface {name}",
        docstring=_java_javadoc(node, source),
        body_text=_node_text(node, source),
        visibility=vis,
        decorators=_java_annotations(node, source),
    ))

    body = _child_by_type(node, "interface_body")
    if body:
        for child in body.children:
            if child.type == "method_declaration":
                symbols.append(_java_method(child, source, f"{parent}.{name}"))
            elif child.type == "constant_declaration":
                syms = _java_field(child, source, f"{parent}.{name}")
                symbols.extend(syms)

    return symbols


def _java_enum(node: Any, source: bytes, parent: str) -> Symbol:
    name = _child_text(node, "name", source)
    return Symbol(
        name=name,
        qualified=f"{parent}.{name}",
        kind="class",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=f"enum {name}",
        docstring=_java_javadoc(node, source),
        body_text=_node_text(node, source),
        visibility=_java_visibility(node),
    )


def _java_record(node: Any, source: bytes, parent: str) -> Symbol:
    name = _child_text(node, "name", source)
    return Symbol(
        name=name,
        qualified=f"{parent}.{name}",
        kind="class",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=f"record {name}",
        docstring=_java_javadoc(node, source),
        body_text=_node_text(node, source),
        visibility=_java_visibility(node),
    )


def _java_method(node: Any, source: bytes, parent: str) -> Symbol:
    name = _child_text(node, "name", source)
    params = _child_by_type(node, "formal_parameters")
    return_type = _child_by_type(node, "type_identifier") or _child_by_type(node, "void_type")

    sig_parts = []
    if return_type:
        sig_parts.append(_node_text(return_type, source))
    sig_parts.append(name)
    if params:
        sig_parts.append(_node_text(params, source))

    return Symbol(
        name=name,
        qualified=f"{parent}.{name}",
        kind="method",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=" ".join(sig_parts),
        docstring=_java_javadoc(node, source),
        body_text=_node_text(node, source),
        visibility=_java_visibility(node),
        decorators=_java_annotations(node, source),
    )


def _java_constructor(node: Any, source: bytes, parent: str, class_name: str) -> Symbol:
    name = _child_text(node, "name", source) or class_name
    params = _child_by_type(node, "formal_parameters")

    sig = name
    if params:
        sig += _node_text(params, source)

    return Symbol(
        name=name,
        qualified=f"{parent}.{name}",
        kind="method",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=sig,
        docstring=_java_javadoc(node, source),
        body_text=_node_text(node, source),
        visibility=_java_visibility(node),
    )


def _java_field(node: Any, source: bytes, parent: str) -> list[Symbol]:
    symbols = []
    vis = _java_visibility(node)

    for child in node.children:
        if child.type == "variable_declarator":
            name = _child_text(child, "name", source)
            if not name:
                for c in child.children:
                    if c.type == "identifier":
                        name = _node_text(c, source)
                        break
            if name:
                symbols.append(Symbol(
                    name=name,
                    qualified=f"{parent}.{name}",
                    kind="function",  # field mapped to function for uniformity
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    signature=_node_text(node, source).rstrip(";"),
                    body_text=_node_text(node, source),
                    visibility=vis,
                ))

    return symbols


def _java_import(node: Any, source: bytes) -> Import | None:
    line = node.start_point[0] + 1
    text = _node_text(node, source).strip().rstrip(";")
    # import foo.bar.Baz or import static foo.bar.Baz
    text = text.replace("import ", "").replace("static ", "").strip()

    if "." in text:
        parts = text.rsplit(".", 1)
        module = parts[0]
        name = parts[1]
        return Import(module=module, names=[name], line=line, is_from=True)
    else:
        return Import(module=text, names=[], line=line, is_from=False)


def _java_visibility(node: Any) -> str:
    for child in node.children:
        if child.type == "modifiers":
            text = _node_text(child, node.parent.end_byte if hasattr(node, 'parent') else b"")
            # Read modifiers from source directly
            mod_text = ""
            for mc in child.children:
                t = mc.type
                if t in ("public", "protected", "private"):
                    return t
    return "package"


def _java_annotations(node: Any, source: bytes) -> str | None:
    annotations = []
    for child in node.children:
        if child.type == "modifiers":
            for mc in child.children:
                if mc.type == "marker_annotation" or mc.type == "annotation":
                    annotations.append(_node_text(mc, source))
    return "\n".join(annotations) if annotations else None


def _java_javadoc(node: Any, source: bytes) -> str | None:
    """Extract Javadoc comment above a declaration."""
    lines = source[:node.start_byte].decode("utf-8", errors="replace").split("\n")
    doc_lines: list[str] = []
    in_doc = False

    for line in reversed(lines):
        stripped = line.strip()
        if stripped.endswith("*/"):
            in_doc = True
            content = stripped.rstrip("*/").strip()
            if content:
                doc_lines.insert(0, content.lstrip("* "))
        elif in_doc:
            if stripped.startswith("/**"):
                content = stripped[3:].strip().lstrip("* ")
                if content:
                    doc_lines.insert(0, content)
                break
            elif stripped.startswith("*"):
                content = stripped[1:].strip()
                doc_lines.insert(0, content)
            else:
                break
        elif stripped == "":
            continue
        else:
            break

    return "\n".join(doc_lines) if doc_lines else None
