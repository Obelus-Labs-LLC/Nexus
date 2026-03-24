"""SCIP (Sourcegraph Code Intelligence Protocol) integration for compiler-accurate references.

Layer 2 enrichment: runs language-specific SCIP indexers to get precise
cross-file definitions, references, and type information. This supplements
the tree-sitter-based Layer 1 parsing with compiler-accurate data.

Supported indexers:
  - Python: scip-python (pip install scip-python)
  - Rust: rust-analyzer (cargo install rust-analyzer)
  - TypeScript: scip-typescript (npm install -g @sourcegraph/scip-typescript)
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nexus.store.db import NexusDB


@dataclass
class SCIPReference:
    """A cross-file reference found by SCIP."""
    symbol: str           # SCIP symbol string
    definition_file: str  # File where symbol is defined
    definition_line: int
    reference_file: str   # File where symbol is referenced
    reference_line: int
    kind: str             # "definition", "reference", "implementation"


@dataclass
class SCIPResult:
    """Result of SCIP enrichment."""
    references: list[SCIPReference] = field(default_factory=list)
    edges_added: int = 0
    errors: list[str] = field(default_factory=list)
    indexer_used: str = ""


def enrich_with_scip(
    project_root: Path,
    language: str,
    db: NexusDB,
) -> SCIPResult:
    """Run SCIP indexer and import results into the Nexus graph.

    This is an optional enrichment step — the project should already be
    indexed with tree-sitter before calling this.
    """
    result = SCIPResult()

    indexer = _get_indexer(language)
    if not indexer:
        result.errors.append(f"No SCIP indexer available for {language}")
        return result

    # Check if indexer is installed
    if not _is_installed(indexer["check_cmd"]):
        result.errors.append(
            f"SCIP indexer not installed: {indexer['install_hint']}"
        )
        return result

    result.indexer_used = indexer["name"]

    # Run the indexer
    scip_file = project_root / "index.scip"
    try:
        _run_indexer(project_root, language, indexer, scip_file)
    except subprocess.CalledProcessError as e:
        result.errors.append(f"Indexer failed: {e}")
        return result
    except FileNotFoundError as e:
        result.errors.append(f"Indexer not found: {e}")
        return result

    # Parse SCIP output and create edges
    if scip_file.exists():
        try:
            result = _parse_scip_and_enrich(scip_file, db, project_root, result)
        finally:
            # Clean up SCIP file
            scip_file.unlink(missing_ok=True)

    return result


def _get_indexer(language: str) -> dict[str, str] | None:
    """Get indexer config for a language."""
    indexers = {
        "python": {
            "name": "scip-python",
            "cmd": ["scip-python", "index", "."],
            "check_cmd": ["scip-python", "--version"],
            "install_hint": "pip install scip-python",
        },
        "rust": {
            "name": "rust-analyzer",
            "cmd": ["rust-analyzer", "scip", "."],
            "check_cmd": ["rust-analyzer", "--version"],
            "install_hint": "rustup component add rust-analyzer",
        },
        "typescript": {
            "name": "scip-typescript",
            "cmd": ["scip-typescript", "index"],
            "check_cmd": ["scip-typescript", "--version"],
            "install_hint": "npm install -g @sourcegraph/scip-typescript",
        },
    }
    return indexers.get(language)


def _is_installed(check_cmd: list[str]) -> bool:
    """Check if an indexer command is available."""
    try:
        subprocess.run(
            check_cmd,
            capture_output=True,
            timeout=10,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _run_indexer(
    project_root: Path,
    language: str,
    indexer: dict[str, str],
    output_path: Path,
) -> None:
    """Run the SCIP indexer for a project."""
    cmd = indexer["cmd"]

    subprocess.run(
        cmd,
        cwd=project_root,
        capture_output=True,
        timeout=300,  # 5 min max
        check=True,
    )


def _parse_scip_and_enrich(
    scip_file: Path,
    db: NexusDB,
    project_root: Path,
    result: SCIPResult,
) -> SCIPResult:
    """Parse SCIP index file and create edges in the database.

    SCIP files are protobuf-encoded. We use the scip CLI to convert to JSON
    for simpler parsing, or parse the protobuf directly if available.
    """
    # Try to convert SCIP to JSON using the scip CLI
    try:
        json_output = subprocess.run(
            ["scip", "print", str(scip_file), "--json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if json_output.returncode == 0:
            data = json.loads(json_output.stdout)
            return _process_scip_json(data, db, project_root, result)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Fallback: try protobuf parsing
    try:
        return _process_scip_protobuf(scip_file, db, project_root, result)
    except ImportError:
        result.errors.append(
            "Cannot parse SCIP output. Install 'scip' CLI (go install github.com/sourcegraph/scip/cmd/scip@latest) "
            "or protobuf library (pip install protobuf)"
        )
        return result


def _process_scip_json(
    data: dict[str, Any],
    db: NexusDB,
    project_root: Path,
    result: SCIPResult,
) -> SCIPResult:
    """Process SCIP JSON output and create cross-file reference edges."""
    # Build a symbol -> definition location map
    definitions: dict[str, tuple[str, int]] = {}  # symbol -> (file, line)

    for doc in data.get("documents", []):
        rel_path = doc.get("relativePath", "")
        for occ in doc.get("occurrences", []):
            symbol = occ.get("symbol", "")
            roles = occ.get("symbolRoles", 0)
            range_data = occ.get("range", [0, 0, 0, 0])
            line = range_data[0] + 1 if range_data else 0

            # Role 1 = definition
            if roles & 1:
                definitions[symbol] = (rel_path, line)

    # Now find references and create edges
    for doc in data.get("documents", []):
        ref_path = doc.get("relativePath", "")
        for occ in doc.get("occurrences", []):
            symbol = occ.get("symbol", "")
            roles = occ.get("symbolRoles", 0)
            range_data = occ.get("range", [0, 0, 0, 0])
            ref_line = range_data[0] + 1 if range_data else 0

            # Skip definitions, only care about references
            if roles & 1:
                continue

            if symbol in definitions:
                def_path, def_line = definitions[symbol]
                if def_path != ref_path:  # Cross-file reference
                    ref = SCIPReference(
                        symbol=symbol,
                        definition_file=def_path,
                        definition_line=def_line,
                        reference_file=ref_path,
                        reference_line=ref_line,
                        kind="reference",
                    )
                    result.references.append(ref)

                    # Create edge in the database
                    edge_added = _create_reference_edge(
                        db, def_path, def_line, ref_path, ref_line
                    )
                    if edge_added:
                        result.edges_added += 1

    return result


def _process_scip_protobuf(
    scip_file: Path,
    db: NexusDB,
    project_root: Path,
    result: SCIPResult,
) -> SCIPResult:
    """Parse SCIP protobuf directly."""
    # This requires the protobuf definitions for SCIP.
    # For now, we note this as a future enhancement.
    result.errors.append(
        "Direct protobuf parsing not yet implemented. "
        "Install scip CLI for JSON-based parsing."
    )
    return result


def _create_reference_edge(
    db: NexusDB,
    def_file: str,
    def_line: int,
    ref_file: str,
    ref_line: int,
) -> bool:
    """Create a 'references' edge between symbols in two files."""
    with db.connect() as conn:
        # Find the symbol at the definition location
        def_sym = conn.execute(
            "SELECT s.id FROM symbols s JOIN files f ON s.file_id = f.id "
            "WHERE f.path = ? AND s.line_start <= ? AND s.line_end >= ? "
            "ORDER BY (s.line_end - s.line_start) ASC LIMIT 1",
            (def_file, def_line, def_line),
        ).fetchone()

        # Find the symbol at the reference location
        ref_sym = conn.execute(
            "SELECT s.id FROM symbols s JOIN files f ON s.file_id = f.id "
            "WHERE f.path = ? AND s.line_start <= ? AND s.line_end >= ? "
            "ORDER BY (s.line_end - s.line_start) ASC LIMIT 1",
            (ref_file, ref_line, ref_line),
        ).fetchone()

        if def_sym and ref_sym:
            conn.execute(
                "INSERT OR IGNORE INTO edges (source_id, target_id, kind, weight) "
                "VALUES (?, ?, 'references', 1.5)",
                (ref_sym["id"], def_sym["id"]),
            )
            return True

    return False
