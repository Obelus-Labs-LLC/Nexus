"""Go language parser plugin for Nexus.

Extracts functions, types, methods, interfaces, and imports from Go source files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nexus.index.parser import Import, ParseResult, Symbol, _node_text, _child_text, _child_by_type

LANGUAGE = "go"
GRAMMAR = "go"
NAME = "Go (built-in)"


def extract(root: Any, source: bytes, path: Path) -> ParseResult:
    """Extract symbols and imports from a Go AST."""
    result = ParseResult()
    module_name = path.stem

    for node in root.children:
        if node.type == "function_declaration":
            result.symbols.append(_go_function(node, source, module_name))
        elif node.type == "method_declaration":
            result.symbols.append(_go_method(node, source, module_name))
        elif node.type == "type_declaration":
            for child in node.children:
                if child.type == "type_spec":
                    result.symbols.extend(_go_type_spec(child, source, module_name))
        elif node.type == "import_declaration":
            result.imports.extend(_go_imports(node, source))

    return result


def _go_function(node: Any, source: bytes, module: str) -> Symbol:
    name = _child_text(node, "name", source)
    params = _child_by_type(node, "parameter_list")
    result_type = _child_by_type(node, "result")

    sig = f"func {name}"
    if params:
        sig += _node_text(params, source)
    if result_type:
        sig += " " + _node_text(result_type, source)

    visibility = "public" if name and name[0].isupper() else "private"

    return Symbol(
        name=name,
        qualified=f"{module}.{name}",
        kind="function",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=sig,
        docstring=_go_doc_comment(node, source),
        body_text=_node_text(node, source),
        visibility=visibility,
    )


def _go_method(node: Any, source: bytes, module: str) -> Symbol:
    name = _child_text(node, "name", source)
    receiver = _child_by_type(node, "parameter_list")
    params_nodes = [c for c in node.children if c.type == "parameter_list"]

    # First parameter_list is receiver, second is params
    receiver_text = ""
    params_text = ""
    if len(params_nodes) >= 1:
        receiver_text = _node_text(params_nodes[0], source)
    if len(params_nodes) >= 2:
        params_text = _node_text(params_nodes[1], source)

    # Extract receiver type name
    type_name = module
    if receiver_text:
        # (r *MyType) -> MyType
        clean = receiver_text.strip("()")
        parts = clean.split()
        if len(parts) >= 2:
            type_name = parts[-1].lstrip("*")

    sig = f"func {receiver_text} {name}{params_text}"
    visibility = "public" if name and name[0].isupper() else "private"

    return Symbol(
        name=name,
        qualified=f"{module}.{type_name}.{name}",
        kind="method",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=sig,
        docstring=_go_doc_comment(node, source),
        body_text=_node_text(node, source),
        visibility=visibility,
    )


def _go_type_spec(node: Any, source: bytes, module: str) -> list[Symbol]:
    symbols = []
    name = _child_text(node, "name", source)
    if not name:
        return symbols

    visibility = "public" if name[0].isupper() else "private"

    # Determine type kind
    type_node = _child_by_type(node, "struct_type")
    if type_node:
        symbols.append(Symbol(
            name=name,
            qualified=f"{module}.{name}",
            kind="class",
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            signature=f"type {name} struct",
            docstring=_go_doc_comment(node, source),
            body_text=_node_text(node, source),
            visibility=visibility,
        ))
        return symbols

    iface_node = _child_by_type(node, "interface_type")
    if iface_node:
        symbols.append(Symbol(
            name=name,
            qualified=f"{module}.{name}",
            kind="class",
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            signature=f"type {name} interface",
            docstring=_go_doc_comment(node, source),
            body_text=_node_text(node, source),
            visibility=visibility,
        ))
        return symbols

    # Other type aliases
    symbols.append(Symbol(
        name=name,
        qualified=f"{module}.{name}",
        kind="class",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=f"type {name}",
        body_text=_node_text(node, source),
        visibility=visibility,
    ))
    return symbols


def _go_imports(node: Any, source: bytes) -> list[Import]:
    imports = []
    line = node.start_point[0] + 1

    for child in node.children:
        if child.type == "import_spec":
            path_node = _child_by_type(child, "interpreted_string_literal")
            if path_node:
                mod = _node_text(path_node, source).strip('"')
                # Get the package name (last segment of import path)
                pkg_name = mod.split("/")[-1] if "/" in mod else mod
                imports.append(Import(
                    module=mod,
                    names=[pkg_name],
                    line=child.start_point[0] + 1,
                    is_from=True,
                ))
        elif child.type == "import_spec_list":
            for spec in child.children:
                if spec.type == "import_spec":
                    path_node = _child_by_type(spec, "interpreted_string_literal")
                    if path_node:
                        mod = _node_text(path_node, source).strip('"')
                        pkg_name = mod.split("/")[-1] if "/" in mod else mod
                        imports.append(Import(
                            module=mod,
                            names=[pkg_name],
                            line=spec.start_point[0] + 1,
                            is_from=True,
                        ))

    return imports


def _go_doc_comment(node: Any, source: bytes) -> str | None:
    """Extract // doc comments above a Go declaration."""
    lines = source[:node.start_byte].decode("utf-8", errors="replace").split("\n")
    doc_lines: list[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("//"):
            doc_lines.insert(0, stripped[2:].strip())
        elif stripped == "":
            continue
        else:
            break
    return "\n".join(doc_lines) if doc_lines else None
