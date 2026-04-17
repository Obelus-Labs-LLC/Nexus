"""Tree-sitter based symbol extraction from source files."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter_language_pack import get_parser

# Supported languages and their tree-sitter grammar names
_LANG_GRAMMAR: dict[str, str] = {
    "python": "python",
    "rust": "rust",
    "typescript": "typescript",
    "javascript": "javascript",
    "c": "c",
    "go": "go",
    "java": "java",
    # Extended language coverage (pitlane-mcp parity)
    "ruby": "ruby",
    "php": "php",
    "kotlin": "kotlin",
    "swift": "swift",
    "zig": "zig",
    "solidity": "solidity",
}


@dataclass
class Symbol:
    """Extracted symbol from source code."""
    name: str
    qualified: str
    kind: str  # function, class, method, module, import
    line_start: int
    line_end: int
    signature: str | None = None
    docstring: str | None = None
    body_text: str | None = None
    visibility: str = "public"
    decorators: str | None = None


@dataclass
class Import:
    """Extracted import statement."""
    module: str
    names: list[str]  # specific names imported, empty for bare import
    alias: str | None = None
    line: int = 0
    is_from: bool = False


@dataclass
class ParseResult:
    """Result of parsing a single file."""
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_file(path: Path, language: str, source: bytes | None = None) -> ParseResult:
    """Parse a source file and extract symbols and imports.

    Args:
        path: Path to the source file.
        language: Language identifier (e.g., "python").
        source: Optional pre-read source bytes. If None, reads from path.
    """
    grammar = _LANG_GRAMMAR.get(language)
    extractor = _EXTRACTORS.get(language)

    # Check plugin system for additional languages
    if not grammar or not extractor:
        from nexus.index.plugins import get_plugin
        plugin = get_plugin(language)
        if plugin:
            grammar = plugin["grammar"]
            extractor = plugin["extractor"]

    if not grammar:
        return ParseResult(errors=[f"Unsupported language: {language}"])

    if source is None:
        try:
            source = path.read_bytes()
        except Exception as e:
            return ParseResult(errors=[f"Read error: {e}"])

    try:
        parser = get_parser(grammar)
        tree = parser.parse(source)
    except Exception as e:
        return ParseResult(errors=[f"Parse error: {e}"])

    if not extractor:
        return ParseResult(errors=[f"No extractor for: {language}"])

    return extractor(tree.root_node, source, path)


# ---------------------------------------------------------------------------
# Python extractor
# ---------------------------------------------------------------------------

def _extract_python(root: Any, source: bytes, path: Path) -> ParseResult:
    """Extract symbols and imports from a Python AST."""
    result = ParseResult()
    module_name = path.stem

    for node in root.children:
        if node.type == "function_definition":
            result.symbols.append(_python_function(node, source, module_name))
        elif node.type == "decorated_definition":
            inner = _get_decorated_inner(node)
            if inner and inner.type == "function_definition":
                sym = _python_function(inner, source, module_name)
                sym.decorators = _get_decorators(node, source)
                result.symbols.append(sym)
            elif inner and inner.type == "class_definition":
                cls_syms = _python_class(inner, source, module_name)
                if cls_syms:
                    cls_syms[0].decorators = _get_decorators(node, source)
                result.symbols.extend(cls_syms)
        elif node.type == "class_definition":
            result.symbols.extend(_python_class(node, source, module_name))
        elif node.type in ("import_statement", "import_from_statement"):
            imp = _python_import(node, source)
            if imp:
                result.imports.append(imp)

    return result


def _python_function(node: Any, source: bytes, parent: str) -> Symbol:
    """Extract a top-level function."""
    name = _child_text(node, "name", source)
    params = _child_by_type(node, "parameters")
    return_type = _child_by_type(node, "type")

    sig = _node_text(params, source) if params else "()"
    if return_type:
        sig += f" -> {_node_text(return_type, source)}"

    visibility = "private" if name.startswith("_") else "public"

    return Symbol(
        name=name,
        qualified=f"{parent}.{name}",
        kind="function",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=f"def {name}{sig}",
        docstring=_python_docstring(node, source),
        body_text=_node_text(node, source),
        visibility=visibility,
    )


def _python_class(node: Any, source: bytes, module: str) -> list[Symbol]:
    """Extract a class and its methods."""
    symbols: list[Symbol] = []
    class_name = _child_text(node, "name", source)

    # Class bases
    superclasses = _child_by_type(node, "argument_list")
    sig = f"class {class_name}"
    if superclasses:
        sig += _node_text(superclasses, source)

    symbols.append(Symbol(
        name=class_name,
        qualified=f"{module}.{class_name}",
        kind="class",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=sig,
        docstring=_python_docstring(node, source),
        body_text=_node_text(node, source),
        visibility="private" if class_name.startswith("_") else "public",
    ))

    # Extract methods from the class body
    body = _child_by_type(node, "block")
    if body:
        for child in body.children:
            if child.type == "function_definition":
                method = _python_function(child, source, f"{module}.{class_name}")
                method.kind = "method"
                symbols.append(method)
            elif child.type == "decorated_definition":
                inner = _get_decorated_inner(child)
                if inner and inner.type == "function_definition":
                    method = _python_function(inner, source, f"{module}.{class_name}")
                    method.kind = "method"
                    method.decorators = _get_decorators(child, source)
                    symbols.append(method)

    return symbols


def _python_import(node: Any, source: bytes) -> Import | None:
    """Extract import information."""
    line = node.start_point[0] + 1

    if node.type == "import_statement":
        # import foo, import foo.bar
        names = []
        for child in node.children:
            if child.type == "dotted_name":
                names.append(_node_text(child, source))
            elif child.type == "aliased_import":
                dotted = _child_by_type(child, "dotted_name")
                if dotted:
                    names.append(_node_text(dotted, source))
        if names:
            return Import(module=names[0], names=names[1:], line=line, is_from=False)

    elif node.type == "import_from_statement":
        # from foo import bar, baz
        # AST: from, dotted_name(module), import, dotted_name(name1), dotted_name(name2)...
        module_name: str | None = None
        imported_names: list[str] = []
        past_import_keyword = False

        for child in node.children:
            if child.type == "import":
                past_import_keyword = True
            elif child.type == "relative_import":
                module_name = _node_text(child, source)
            elif child.type == "dotted_name":
                if not past_import_keyword and module_name is None:
                    module_name = _node_text(child, source)
                elif past_import_keyword:
                    imported_names.append(_node_text(child, source))
            elif child.type == "wildcard_import":
                imported_names.append("*")

        if module_name:
            return Import(
                module=module_name,
                names=imported_names,
                line=line,
                is_from=True,
            )

    return None


def _python_docstring(node: Any, source: bytes) -> str | None:
    """Extract docstring from a function or class."""
    body = _child_by_type(node, "block")
    if not body or not body.children:
        return None

    for child in body.children:
        # Docstring can be: expression_statement > string, or directly a string node
        if child.type == "expression_statement":
            expr = child.children[0] if child.children else None
            if expr and expr.type == "string":
                return _strip_docstring(expr, source)
            break
        elif child.type == "string":
            return _strip_docstring(child, source)
        elif child.type != "comment":
            break

    return None


def _strip_docstring(node: Any, source: bytes) -> str:
    """Strip quote delimiters from a docstring node."""
    text = _node_text(node, source)
    for q in ('"""', "'''"):
        if text.startswith(q) and text.endswith(q):
            return text[3:-3].strip()
    return text.strip("\"'").strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node_text(node: Any, source: bytes) -> str:
    """Get the text content of a node."""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _child_text(node: Any, field_name: str, source: bytes) -> str:
    """Get text of a named child field."""
    child = node.child_by_field_name(field_name)
    return _node_text(child, source) if child else ""


def _child_by_type(node: Any, type_name: str) -> Any | None:
    """Find first child with the given type."""
    for child in node.children:
        if child.type == type_name:
            return child
    return None


def _get_decorated_inner(node: Any) -> Any | None:
    """Get the actual definition inside a decorated_definition."""
    for child in node.children:
        if child.type in ("function_definition", "class_definition"):
            return child
    return None


def _get_decorators(node: Any, source: bytes) -> str:
    """Get decorator text from a decorated_definition."""
    decorators = []
    for child in node.children:
        if child.type == "decorator":
            decorators.append(_node_text(child, source))
    return "\n".join(decorators) if decorators else ""


# ---------------------------------------------------------------------------
# Rust extractor
# ---------------------------------------------------------------------------

def _extract_rust(root: Any, source: bytes, path: Path) -> ParseResult:
    """Extract symbols and imports from a Rust AST."""
    result = ParseResult()
    module_name = path.stem

    for node in root.children:
        if node.type == "function_item":
            result.symbols.append(_rust_function(node, source, module_name))
        elif node.type == "struct_item":
            result.symbols.append(_rust_struct(node, source, module_name))
        elif node.type == "enum_item":
            result.symbols.append(_rust_enum(node, source, module_name))
        elif node.type == "trait_item":
            result.symbols.append(_rust_trait(node, source, module_name))
        elif node.type == "impl_item":
            result.symbols.extend(_rust_impl(node, source, module_name))
        elif node.type == "use_declaration":
            imp = _rust_import(node, source)
            if imp:
                result.imports.append(imp)
        elif node.type == "mod_item":
            name = _child_text(node, "name", source)
            if name:
                result.symbols.append(Symbol(
                    name=name,
                    qualified=f"{module_name}::{name}",
                    kind="module",
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    body_text=_node_text(node, source),
                ))

    return result


def _rust_function(node: Any, source: bytes, parent: str) -> Symbol:
    name = _child_text(node, "name", source)
    vis = _rust_visibility(node)
    params = _child_by_type(node, "parameters")
    ret = _child_by_type(node, "type_identifier") or _child_by_type(node, "generic_type")

    sig_parts = [f"fn {name}"]
    if params:
        sig_parts.append(_node_text(params, source))
    if ret:
        sig_parts.append(f" -> {_node_text(ret, source)}")

    return Symbol(
        name=name,
        qualified=f"{parent}::{name}",
        kind="function",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature="".join(sig_parts),
        docstring=_rust_doc_comment(node, source),
        body_text=_node_text(node, source),
        visibility=vis,
    )


def _rust_struct(node: Any, source: bytes, module: str) -> Symbol:
    name = _child_text(node, "name", source)
    return Symbol(
        name=name,
        qualified=f"{module}::{name}",
        kind="class",  # map struct -> class for uniformity
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=f"struct {name}",
        docstring=_rust_doc_comment(node, source),
        body_text=_node_text(node, source),
        visibility=_rust_visibility(node),
    )


def _rust_enum(node: Any, source: bytes, module: str) -> Symbol:
    name = _child_text(node, "name", source)
    return Symbol(
        name=name,
        qualified=f"{module}::{name}",
        kind="class",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=f"enum {name}",
        docstring=_rust_doc_comment(node, source),
        body_text=_node_text(node, source),
        visibility=_rust_visibility(node),
    )


def _rust_trait(node: Any, source: bytes, module: str) -> Symbol:
    name = _child_text(node, "name", source)
    return Symbol(
        name=name,
        qualified=f"{module}::{name}",
        kind="class",
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
        signature=f"trait {name}",
        docstring=_rust_doc_comment(node, source),
        body_text=_node_text(node, source),
        visibility=_rust_visibility(node),
    )


def _rust_impl(node: Any, source: bytes, module: str) -> list[Symbol]:
    """Extract methods from an impl block."""
    symbols: list[Symbol] = []

    # Get the type being implemented
    type_name = ""
    trait_name = ""
    for child in node.children:
        if child.type == "type_identifier":
            if not type_name:
                # Could be trait or type depending on 'for' keyword
                type_name = _node_text(child, source)
            else:
                type_name = _node_text(child, source)
        elif child.type == "for":
            # Previous type_name was actually the trait
            trait_name = type_name
            type_name = ""

    parent = f"{module}::{type_name}" if type_name else module

    decl_list = _child_by_type(node, "declaration_list")
    if decl_list:
        for child in decl_list.children:
            if child.type == "function_item":
                method = _rust_function(child, source, parent)
                method.kind = "method"
                symbols.append(method)

    return symbols


def _rust_import(node: Any, source: bytes) -> Import | None:
    line = node.start_point[0] + 1
    text = _node_text(node, source)
    # Extract the path after 'use '
    # e.g., "use std::collections::HashMap;" -> "std::collections::HashMap"
    path = text.replace("use ", "").rstrip(";").strip()
    # Handle pub use
    path = path.replace("pub ", "")

    # Split into module and names
    if "::" in path:
        parts = path.rsplit("::", 1)
        module = parts[0]
        name = parts[1].strip("{} ")
        names = [n.strip() for n in name.split(",") if n.strip()]
        return Import(module=module, names=names, line=line, is_from=True)
    else:
        return Import(module=path, names=[], line=line, is_from=False)


def _rust_visibility(node: Any) -> str:
    for child in node.children:
        if child.type == "visibility_modifier":
            return "public"
    return "private"


def _rust_doc_comment(node: Any, source: bytes) -> str | None:
    """Extract /// doc comments above a node."""
    lines = source[:node.start_byte].decode("utf-8", errors="replace").split("\n")
    doc_lines: list[str] = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("///"):
            doc_lines.insert(0, stripped[3:].strip())
        elif stripped.startswith("//!"):
            doc_lines.insert(0, stripped[3:].strip())
        elif stripped == "":
            # Skip blank lines (trailing newlines, gaps between doc lines)
            continue
        else:
            break
    return "\n".join(doc_lines) if doc_lines else None


# ---------------------------------------------------------------------------
# TypeScript/JavaScript extractor
# ---------------------------------------------------------------------------

def _extract_typescript(root: Any, source: bytes, path: Path) -> ParseResult:
    """Extract symbols and imports from TypeScript/JavaScript AST."""
    result = ParseResult()
    module_name = path.stem

    for node in root.children:
        if node.type == "function_declaration":
            result.symbols.append(_ts_function(node, source, module_name))
        elif node.type == "class_declaration":
            result.symbols.extend(_ts_class(node, source, module_name))
        elif node.type == "interface_declaration":
            result.symbols.append(_ts_interface(node, source, module_name))
        elif node.type == "type_alias_declaration":
            name = _child_text(node, "name", source)
            result.symbols.append(Symbol(
                name=name, qualified=f"{module_name}.{name}", kind="class",
                line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                signature=f"type {name}", body_text=_node_text(node, source),
            ))
        elif node.type == "export_statement":
            # Unwrap export to get the actual declaration
            for child in node.children:
                if child.type == "function_declaration":
                    sym = _ts_function(child, source, module_name)
                    sym.visibility = "public"
                    result.symbols.append(sym)
                elif child.type == "class_declaration":
                    syms = _ts_class(child, source, module_name)
                    if syms:
                        syms[0].visibility = "public"
                    result.symbols.extend(syms)
                elif child.type == "interface_declaration":
                    sym = _ts_interface(child, source, module_name)
                    sym.visibility = "public"
                    result.symbols.append(sym)
                elif child.type == "type_alias_declaration":
                    name = _child_text(child, "name", source)
                    result.symbols.append(Symbol(
                        name=name, qualified=f"{module_name}.{name}", kind="class",
                        line_start=child.start_point[0] + 1, line_end=child.end_point[0] + 1,
                        signature=f"type {name}", body_text=_node_text(child, source),
                        visibility="public",
                    ))
                elif child.type == "lexical_declaration":
                    # export const foo = ...
                    for decl in child.children:
                        if decl.type == "variable_declarator":
                            sym = _ts_variable(decl, source, module_name)
                            if sym:
                                sym.visibility = "public"
                                result.symbols.append(sym)
        elif node.type == "import_statement":
            imp = _ts_import(node, source)
            if imp:
                result.imports.append(imp)
        elif node.type == "lexical_declaration":
            for decl in node.children:
                if decl.type == "variable_declarator":
                    sym = _ts_variable(decl, source, module_name)
                    if sym:
                        result.symbols.append(sym)

    return result


def _ts_function(node: Any, source: bytes, parent: str) -> Symbol:
    name = _child_text(node, "name", source)
    params = _child_by_type(node, "formal_parameters")
    sig = f"function {name}"
    if params:
        sig += _node_text(params, source)

    return Symbol(
        name=name, qualified=f"{parent}.{name}", kind="function",
        line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
        signature=sig, body_text=_node_text(node, source),
        docstring=_ts_jsdoc(node, source),
    )


def _ts_class(node: Any, source: bytes, module: str) -> list[Symbol]:
    symbols: list[Symbol] = []
    class_name = _child_text(node, "name", source)

    symbols.append(Symbol(
        name=class_name, qualified=f"{module}.{class_name}", kind="class",
        line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
        signature=f"class {class_name}", body_text=_node_text(node, source),
        docstring=_ts_jsdoc(node, source),
    ))

    body = _child_by_type(node, "class_body")
    if body:
        for child in body.children:
            if child.type == "method_definition":
                mname = _child_text(child, "name", source)
                params = _child_by_type(child, "formal_parameters")
                sig = f"{mname}"
                if params:
                    sig += _node_text(params, source)
                symbols.append(Symbol(
                    name=mname, qualified=f"{module}.{class_name}.{mname}",
                    kind="method",
                    line_start=child.start_point[0] + 1, line_end=child.end_point[0] + 1,
                    signature=sig, body_text=_node_text(child, source),
                ))
            elif child.type == "public_field_definition":
                fname = _child_text(child, "name", source)
                if fname:
                    symbols.append(Symbol(
                        name=fname, qualified=f"{module}.{class_name}.{fname}",
                        kind="function",  # field
                        line_start=child.start_point[0] + 1, line_end=child.end_point[0] + 1,
                        body_text=_node_text(child, source),
                    ))

    return symbols


def _ts_interface(node: Any, source: bytes, module: str) -> Symbol:
    name = _child_text(node, "name", source)
    return Symbol(
        name=name, qualified=f"{module}.{name}", kind="class",
        line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
        signature=f"interface {name}", body_text=_node_text(node, source),
        docstring=_ts_jsdoc(node, source),
    )


def _ts_variable(decl: Any, source: bytes, module: str) -> Symbol | None:
    """Extract a variable declaration, skipping destructured patterns."""
    # Check if name is a plain identifier (not array/object pattern)
    name_node = decl.child_by_field_name("name")
    if not name_node:
        return None
    # Skip destructured patterns: array_pattern, object_pattern
    if name_node.type in ("array_pattern", "object_pattern"):
        return None

    name = _node_text(name_node, source)
    if not name or name.startswith("[") or name.startswith("{"):
        return None

    # Try to detect arrow functions: const foo = () => ...
    value = decl.child_by_field_name("value")
    sig = None
    kind = "function"
    if value and value.type == "arrow_function":
        params = _child_by_type(value, "formal_parameters")
        sig = f"const {name} = "
        if params:
            sig += _node_text(params, source) + " => ..."
        else:
            sig += "() => ..."
    elif value and value.type == "function":
        params = _child_by_type(value, "formal_parameters")
        sig = f"const {name} = function"
        if params:
            sig += _node_text(params, source)

    return Symbol(
        name=name, qualified=f"{module}.{name}",
        kind=kind, line_start=decl.start_point[0] + 1,
        line_end=decl.end_point[0] + 1,
        signature=sig, body_text=_node_text(decl, source),
    )


def _ts_import(node: Any, source: bytes) -> Import | None:
    line = node.start_point[0] + 1
    # Find the source string
    source_str = None
    names: list[str] = []

    for child in node.children:
        if child.type == "string":
            source_str = _node_text(child, source).strip("'\"")
        elif child.type == "import_clause":
            for ic in child.children:
                if ic.type == "named_imports":
                    for spec in ic.children:
                        if spec.type == "import_specifier":
                            n = _child_text(spec, "name", source)
                            if n:
                                names.append(n)
                elif ic.type == "identifier":
                    names.append(_node_text(ic, source))

    if source_str:
        return Import(module=source_str, names=names, line=line, is_from=True)
    return None


def _ts_jsdoc(node: Any, source: bytes) -> str | None:
    """Extract JSDoc comment before a node."""
    start_byte = node.start_byte
    prefix = source[:start_byte].decode("utf-8", errors="replace")
    lines = prefix.rstrip().split("\n")
    if lines and lines[-1].strip().endswith("*/"):
        doc_lines: list[str] = []
        for line in reversed(lines):
            stripped = line.strip()
            doc_lines.insert(0, stripped.lstrip("/* "))
            if stripped.startswith("/**"):
                break
        return "\n".join(l for l in doc_lines if l)
    return None


# ---------------------------------------------------------------------------
# C extractor
# ---------------------------------------------------------------------------

def _extract_c(root: Any, source: bytes, path: Path) -> ParseResult:
    """Extract symbols from C source."""
    result = ParseResult()
    module_name = path.stem

    for node in root.children:
        if node.type == "function_definition":
            result.symbols.append(_c_function(node, source, module_name))
        elif node.type == "struct_specifier":
            name = _child_text(node, "name", source)
            if name:
                result.symbols.append(Symbol(
                    name=name, qualified=f"{module_name}.{name}", kind="class",
                    line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                    signature=f"struct {name}", body_text=_node_text(node, source),
                ))
        elif node.type == "enum_specifier":
            name = _child_text(node, "name", source)
            if name:
                result.symbols.append(Symbol(
                    name=name, qualified=f"{module_name}.{name}", kind="class",
                    line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                    signature=f"enum {name}", body_text=_node_text(node, source),
                ))
        elif node.type == "declaration":
            # Could be a function declaration or typedef
            decl = _child_by_type(node, "function_declarator")
            if decl:
                name = _child_text(decl, "declarator", source)
                if not name:
                    name = _child_text(decl, "name", source)
                    if not name:
                        # Try first identifier child
                        for c in decl.children:
                            if c.type == "identifier":
                                name = _node_text(c, source)
                                break
                if name:
                    result.symbols.append(Symbol(
                        name=name, qualified=f"{module_name}.{name}", kind="function",
                        line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                        signature=_node_text(node, source).rstrip(";"),
                        body_text=_node_text(node, source),
                    ))
        elif node.type == "preproc_include":
            text = _node_text(node, source)
            # #include "foo.h" or #include <foo.h>
            for child in node.children:
                if child.type in ("string_literal", "system_lib_string"):
                    mod = _node_text(child, source).strip('"<>')
                    result.imports.append(Import(
                        module=mod, names=[], line=node.start_point[0] + 1, is_from=False,
                    ))
        elif node.type == "type_definition":
            # typedef struct { ... } Name;
            for child in node.children:
                if child.type == "type_identifier":
                    name = _node_text(child, source)
                    result.symbols.append(Symbol(
                        name=name, qualified=f"{module_name}.{name}", kind="class",
                        line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
                        signature=f"typedef {name}",
                        body_text=_node_text(node, source),
                    ))
                    break

    return result


def _c_function(node: Any, source: bytes, module: str) -> Symbol:
    # The declarator contains the function name and parameters
    declarator = _child_by_type(node, "function_declarator")
    name = ""
    sig = ""
    if declarator:
        # Name could be a plain identifier or pointer_declarator
        for child in declarator.children:
            if child.type == "identifier":
                name = _node_text(child, source)
            elif child.type == "pointer_declarator":
                for c in child.children:
                    if c.type == "identifier":
                        name = _node_text(c, source)
        sig = _node_text(declarator, source)

    if not name:
        name = module + "_unknown"

    return Symbol(
        name=name, qualified=f"{module}.{name}", kind="function",
        line_start=node.start_point[0] + 1, line_end=node.end_point[0] + 1,
        signature=sig, body_text=_node_text(node, source),
        visibility="public" if not name.startswith("_") else "private",
    )


# ---------------------------------------------------------------------------
# Extractor registry
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Ruby extractor
# ---------------------------------------------------------------------------

def _extract_ruby(root: Any, source: bytes, path: Path) -> ParseResult:
    """Extract symbols from Ruby."""
    result = ParseResult()
    module_name = path.stem

    def walk(node: Any, parent: str) -> None:
        for child in node.children:
            if child.type == "class":
                name = _child_text(child, "name", source)
                if not name:
                    # fallback: find first constant
                    for c in child.children:
                        if c.type == "constant":
                            name = _node_text(c, source)
                            break
                if name:
                    qual = f"{parent}::{name}" if parent else name
                    result.symbols.append(Symbol(
                        name=name, qualified=qual, kind="class",
                        line_start=child.start_point[0] + 1,
                        line_end=child.end_point[0] + 1,
                        signature=f"class {name}",
                        body_text=_node_text(child, source),
                    ))
                    # Recurse into class body for methods
                    body = _child_by_type(child, "body_statement")
                    if body:
                        walk(body, qual)
            elif child.type == "module":
                name = _child_text(child, "name", source)
                if not name:
                    for c in child.children:
                        if c.type == "constant":
                            name = _node_text(c, source)
                            break
                if name:
                    qual = f"{parent}::{name}" if parent else name
                    result.symbols.append(Symbol(
                        name=name, qualified=qual, kind="module",
                        line_start=child.start_point[0] + 1,
                        line_end=child.end_point[0] + 1,
                        signature=f"module {name}",
                        body_text=_node_text(child, source),
                    ))
                    body = _child_by_type(child, "body_statement")
                    if body:
                        walk(body, qual)
            elif child.type in ("method", "singleton_method"):
                name = _child_text(child, "name", source)
                if name:
                    params = _child_by_type(child, "method_parameters") or _child_by_type(child, "parameters")
                    sig = f"def {name}"
                    if params:
                        sig += _node_text(params, source)
                    qual = f"{parent}.{name}" if parent else name
                    kind = "method" if parent else "function"
                    result.symbols.append(Symbol(
                        name=name, qualified=qual, kind=kind,
                        line_start=child.start_point[0] + 1,
                        line_end=child.end_point[0] + 1,
                        signature=sig,
                        body_text=_node_text(child, source),
                        visibility="private" if name.startswith("_") else "public",
                    ))
            elif child.type == "call":
                # require "foo" / require_relative "foo"
                method = _child_text(child, "method", source)
                if method in ("require", "require_relative", "load"):
                    args = _child_by_type(child, "argument_list")
                    if args:
                        for arg in args.children:
                            if arg.type == "string":
                                mod = _node_text(arg, source).strip("\"'")
                                if mod:
                                    result.imports.append(Import(
                                        module=mod, names=[],
                                        line=child.start_point[0] + 1,
                                        is_from=(method == "require_relative"),
                                    ))

    walk(root, module_name)
    return result


# ---------------------------------------------------------------------------
# PHP extractor
# ---------------------------------------------------------------------------

def _extract_php(root: Any, source: bytes, path: Path) -> ParseResult:
    """Extract symbols from PHP."""
    result = ParseResult()
    module_name = path.stem

    def process_node(node: Any, parent: str) -> None:
        if node.type == "class_declaration":
            name = _child_text(node, "name", source)
            if not name:
                for c in node.children:
                    if c.type == "name":
                        name = _node_text(c, source)
                        break
            qual = f"{parent}\\{name}" if name else parent
            if name:
                result.symbols.append(Symbol(
                    name=name, qualified=qual, kind="class",
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    signature=f"class {name}",
                    body_text=_node_text(node, source),
                ))
                body = _child_by_type(node, "declaration_list")
                if body:
                    for member in body.children:
                        if member.type == "method_declaration":
                            mname = _child_text(member, "name", source)
                            if not mname:
                                for c in member.children:
                                    if c.type == "name":
                                        mname = _node_text(c, source)
                                        break
                            if not mname:
                                continue
                            mparams = _child_by_type(member, "formal_parameters")
                            msig = f"function {mname}"
                            if mparams:
                                msig += _node_text(mparams, source)
                            # Detect visibility modifier (public/private/protected)
                            mvis = "public"
                            for c in member.children:
                                if c.type == "visibility_modifier":
                                    vtext = _node_text(c, source).lower()
                                    if vtext in ("private", "protected"):
                                        mvis = vtext
                                    break
                            result.symbols.append(Symbol(
                                name=mname,
                                qualified=f"{qual}\\{mname}",
                                kind="method",
                                line_start=member.start_point[0] + 1,
                                line_end=member.end_point[0] + 1,
                                signature=msig,
                                body_text=_node_text(member, source),
                                visibility=mvis,
                            ))
        elif node.type == "interface_declaration":
            name = _child_text(node, "name", source)
            if not name:
                for c in node.children:
                    if c.type == "name":
                        name = _node_text(c, source)
                        break
            if name:
                result.symbols.append(Symbol(
                    name=name, qualified=f"{parent}\\{name}", kind="class",
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    signature=f"interface {name}",
                    body_text=_node_text(node, source),
                ))
        elif node.type == "function_definition":
            name = _child_text(node, "name", source)
            if not name:
                for c in node.children:
                    if c.type == "name":
                        name = _node_text(c, source)
                        break
            if name:
                params = _child_by_type(node, "formal_parameters")
                sig = f"function {name}"
                if params:
                    sig += _node_text(params, source)
                result.symbols.append(Symbol(
                    name=name, qualified=f"{parent}\\{name}", kind="function",
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    signature=sig,
                    body_text=_node_text(node, source),
                ))
        elif node.type == "namespace_definition":
            ns_name = ""
            for c in node.children:
                if c.type in ("namespace_name", "qualified_name"):
                    ns_name = _node_text(c, source)
                    break
            new_parent = ns_name or parent
            body = _child_by_type(node, "compound_statement") or _child_by_type(node, "declaration_list")
            if body:
                for child in body.children:
                    process_node(child, new_parent)
            else:
                # bare namespace: remaining top-level items belong to it
                pass
        elif node.type == "namespace_use_declaration":
            text = _node_text(node, source)
            # use Foo\Bar; or use Foo\{Bar, Baz};
            stripped = text.replace("use ", "").rstrip(";").strip()
            result.imports.append(Import(
                module=stripped, names=[],
                line=node.start_point[0] + 1,
                is_from=True,
            ))

    for child in root.children:
        process_node(child, module_name)

    return result


# ---------------------------------------------------------------------------
# Kotlin extractor
# ---------------------------------------------------------------------------

def _extract_kotlin(root: Any, source: bytes, path: Path) -> ParseResult:
    """Extract symbols from Kotlin."""
    result = ParseResult()
    module_name = path.stem

    def find_name(node: Any) -> str:
        for c in node.children:
            if c.type in ("simple_identifier", "type_identifier"):
                return _node_text(c, source)
        return ""

    for node in root.children:
        if node.type == "class_declaration":
            name = find_name(node)
            if name:
                result.symbols.append(Symbol(
                    name=name, qualified=f"{module_name}.{name}", kind="class",
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    signature=f"class {name}",
                    body_text=_node_text(node, source),
                ))
                # Extract member functions
                body = _child_by_type(node, "class_body")
                if body:
                    for member in body.children:
                        if member.type == "function_declaration":
                            mname = find_name(member)
                            if mname:
                                params = _child_by_type(member, "function_value_parameters")
                                sig = f"fun {mname}"
                                if params:
                                    sig += _node_text(params, source)
                                result.symbols.append(Symbol(
                                    name=mname,
                                    qualified=f"{module_name}.{name}.{mname}",
                                    kind="method",
                                    line_start=member.start_point[0] + 1,
                                    line_end=member.end_point[0] + 1,
                                    signature=sig,
                                    body_text=_node_text(member, source),
                                ))
        elif node.type == "function_declaration":
            name = find_name(node)
            if name:
                params = _child_by_type(node, "function_value_parameters")
                sig = f"fun {name}"
                if params:
                    sig += _node_text(params, source)
                result.symbols.append(Symbol(
                    name=name, qualified=f"{module_name}.{name}", kind="function",
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    signature=sig,
                    body_text=_node_text(node, source),
                ))
        elif node.type == "object_declaration":
            name = find_name(node)
            if name:
                result.symbols.append(Symbol(
                    name=name, qualified=f"{module_name}.{name}", kind="class",
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    signature=f"object {name}",
                    body_text=_node_text(node, source),
                ))
        elif node.type == "import_list":
            for imp in node.children:
                if imp.type == "import_header":
                    text = _node_text(imp, source).replace("import ", "").strip(";").strip()
                    if text:
                        result.imports.append(Import(
                            module=text, names=[],
                            line=imp.start_point[0] + 1,
                            is_from=False,
                        ))

    return result


# ---------------------------------------------------------------------------
# Swift extractor
# ---------------------------------------------------------------------------

def _extract_swift(root: Any, source: bytes, path: Path) -> ParseResult:
    """Extract symbols from Swift."""
    result = ParseResult()
    module_name = path.stem

    def find_name(node: Any) -> str:
        for c in node.children:
            if c.type in ("type_identifier", "simple_identifier", "identifier"):
                return _node_text(c, source)
        return ""

    def extract_members(body: Any, parent_qual: str) -> None:
        for member in body.children:
            if member.type == "function_declaration":
                mname = find_name(member)
                if mname:
                    params = None
                    for c in member.children:
                        if c.type == "parameter_clause" or c.type.endswith("parameters"):
                            params = c
                            break
                    sig = f"func {mname}"
                    if params:
                        sig += _node_text(params, source)
                    result.symbols.append(Symbol(
                        name=mname,
                        qualified=f"{parent_qual}.{mname}",
                        kind="method",
                        line_start=member.start_point[0] + 1,
                        line_end=member.end_point[0] + 1,
                        signature=sig,
                        body_text=_node_text(member, source),
                    ))

    for node in root.children:
        if node.type == "class_declaration":
            name = find_name(node)
            if name:
                # Determine struct/class/actor/enum from keyword
                keyword = "class"
                for c in node.children:
                    if c.type in ("class", "struct", "actor", "enum"):
                        keyword = c.type
                        break
                qual = f"{module_name}.{name}"
                result.symbols.append(Symbol(
                    name=name, qualified=qual, kind="class",
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    signature=f"{keyword} {name}",
                    body_text=_node_text(node, source),
                ))
                # Extract methods
                for child in node.children:
                    if child.type in ("class_body", "declaration_list"):
                        extract_members(child, qual)
        elif node.type == "protocol_declaration":
            name = find_name(node)
            if name:
                result.symbols.append(Symbol(
                    name=name, qualified=f"{module_name}.{name}", kind="class",
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    signature=f"protocol {name}",
                    body_text=_node_text(node, source),
                ))
        elif node.type == "function_declaration":
            name = find_name(node)
            if name:
                sig = f"func {name}"
                for c in node.children:
                    if c.type == "parameter_clause" or c.type.endswith("parameters"):
                        sig += _node_text(c, source)
                        break
                result.symbols.append(Symbol(
                    name=name, qualified=f"{module_name}.{name}", kind="function",
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    signature=sig,
                    body_text=_node_text(node, source),
                ))
        elif node.type == "import_declaration":
            text = _node_text(node, source).replace("import ", "").strip()
            if text:
                result.imports.append(Import(
                    module=text, names=[],
                    line=node.start_point[0] + 1,
                    is_from=False,
                ))

    return result


# ---------------------------------------------------------------------------
# Zig extractor
# ---------------------------------------------------------------------------

def _extract_zig(root: Any, source: bytes, path: Path) -> ParseResult:
    """Extract symbols from Zig."""
    result = ParseResult()
    module_name = path.stem

    for node in root.children:
        if node.type != "Decl":
            continue

        # Look inside Decl for FnProto or VarDecl
        for inner in node.children:
            if inner.type == "FnProto":
                # Function declaration
                name = ""
                for c in inner.children:
                    if c.type == "IDENTIFIER":
                        name = _node_text(c, source)
                        break
                if name:
                    result.symbols.append(Symbol(
                        name=name, qualified=f"{module_name}.{name}", kind="function",
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                        signature=_node_text(inner, source),
                        body_text=_node_text(node, source),
                    ))
            elif inner.type == "VarDecl":
                # Could be: const X = struct {...}, const X = enum {...}, const Y = @import("..."), etc.
                name = ""
                value_text = ""
                for c in inner.children:
                    if c.type == "IDENTIFIER":
                        name = _node_text(c, source)
                    elif c.type == "ErrorUnionExpr":
                        value_text = _node_text(c, source)

                if not name:
                    continue

                lower = value_text.lower().lstrip()
                if lower.startswith("@import"):
                    # Extract module path from @import("foo")
                    import re as _re
                    m = _re.search(r'@import\("([^"]+)"\)', value_text)
                    if m:
                        result.imports.append(Import(
                            module=m.group(1), names=[],
                            line=node.start_point[0] + 1,
                            is_from=False,
                        ))
                elif lower.startswith("struct"):
                    result.symbols.append(Symbol(
                        name=name, qualified=f"{module_name}.{name}", kind="class",
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                        signature=f"const {name} = struct",
                        body_text=_node_text(node, source),
                    ))
                elif lower.startswith("enum"):
                    result.symbols.append(Symbol(
                        name=name, qualified=f"{module_name}.{name}", kind="class",
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                        signature=f"const {name} = enum",
                        body_text=_node_text(node, source),
                    ))
                elif lower.startswith("union"):
                    result.symbols.append(Symbol(
                        name=name, qualified=f"{module_name}.{name}", kind="class",
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                        signature=f"const {name} = union",
                        body_text=_node_text(node, source),
                    ))

    return result


# ---------------------------------------------------------------------------
# Solidity extractor
# ---------------------------------------------------------------------------

def _extract_solidity(root: Any, source: bytes, path: Path) -> ParseResult:
    """Extract symbols from Solidity."""
    result = ParseResult()
    module_name = path.stem

    def find_name(node: Any) -> str:
        for c in node.children:
            if c.type == "identifier":
                return _node_text(c, source)
        return ""

    def extract_body(container: Any, parent_qual: str) -> None:
        for member in container.children:
            if member.type == "function_definition":
                mname = find_name(member)
                if mname:
                    sig = f"function {mname}"
                    for c in member.children:
                        if c.type == "parameter":
                            sig += _node_text(c, source)
                    result.symbols.append(Symbol(
                        name=mname,
                        qualified=f"{parent_qual}.{mname}",
                        kind="method",
                        line_start=member.start_point[0] + 1,
                        line_end=member.end_point[0] + 1,
                        signature=sig,
                        body_text=_node_text(member, source),
                    ))
            elif member.type == "constructor_definition":
                result.symbols.append(Symbol(
                    name="constructor", qualified=f"{parent_qual}.constructor",
                    kind="method",
                    line_start=member.start_point[0] + 1,
                    line_end=member.end_point[0] + 1,
                    signature="constructor",
                    body_text=_node_text(member, source),
                ))
            elif member.type == "event_definition":
                mname = find_name(member)
                if mname:
                    result.symbols.append(Symbol(
                        name=mname, qualified=f"{parent_qual}.{mname}",
                        kind="function",
                        line_start=member.start_point[0] + 1,
                        line_end=member.end_point[0] + 1,
                        signature=f"event {mname}",
                        body_text=_node_text(member, source),
                    ))
            elif member.type == "modifier_definition":
                mname = find_name(member)
                if mname:
                    result.symbols.append(Symbol(
                        name=mname, qualified=f"{parent_qual}.{mname}",
                        kind="function",
                        line_start=member.start_point[0] + 1,
                        line_end=member.end_point[0] + 1,
                        signature=f"modifier {mname}",
                        body_text=_node_text(member, source),
                    ))

    for node in root.children:
        if node.type == "contract_declaration":
            name = find_name(node)
            if name:
                qual = f"{module_name}.{name}"
                result.symbols.append(Symbol(
                    name=name, qualified=qual, kind="class",
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    signature=f"contract {name}",
                    body_text=_node_text(node, source),
                ))
                body = _child_by_type(node, "contract_body")
                if body:
                    extract_body(body, qual)
        elif node.type == "interface_declaration":
            name = find_name(node)
            if name:
                qual = f"{module_name}.{name}"
                result.symbols.append(Symbol(
                    name=name, qualified=qual, kind="class",
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    signature=f"interface {name}",
                    body_text=_node_text(node, source),
                ))
                body = _child_by_type(node, "contract_body")
                if body:
                    extract_body(body, qual)
        elif node.type == "library_declaration":
            name = find_name(node)
            if name:
                qual = f"{module_name}.{name}"
                result.symbols.append(Symbol(
                    name=name, qualified=qual, kind="class",
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    signature=f"library {name}",
                    body_text=_node_text(node, source),
                ))
                body = _child_by_type(node, "contract_body")
                if body:
                    extract_body(body, qual)
        elif node.type == "import_directive":
            text = _node_text(node, source)
            # import "./foo.sol";  or  import {X} from "./foo.sol";
            import re as _re
            m = _re.search(r'["\']([^"\']+)["\']', text)
            if m:
                result.imports.append(Import(
                    module=m.group(1), names=[],
                    line=node.start_point[0] + 1,
                    is_from=False,
                ))

    return result


# ---------------------------------------------------------------------------
# Extractor registry
# ---------------------------------------------------------------------------

_EXTRACTORS = {
    "python": _extract_python,
    "rust": _extract_rust,
    "typescript": _extract_typescript,
    "javascript": _extract_typescript,  # same AST structure
    "c": _extract_c,
    "ruby": _extract_ruby,
    "php": _extract_php,
    "kotlin": _extract_kotlin,
    "swift": _extract_swift,
    "zig": _extract_zig,
    "solidity": _extract_solidity,
}
