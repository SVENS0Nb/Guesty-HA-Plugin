"""Validate the repository's durable engineering knowledge contract.

The knowledge base is intentionally human-readable Markdown. This validator
checks the structural rules that are easy to forget during later feature work
without trying to decide whether prose is technically true.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
import sys


ENTRY_RE = re.compile(
    r'^<a id="(?P<anchor>kb-[a-z0-9]+-\d{3})"></a>\n\n'
    r"^### (?P<entry_id>KB-[A-Z0-9]+-\d{3}) — (?P<title>[^\n]+)\n"
    r"(?P<body>.*?)(?=^<a id=|^## |\Z)",
    re.MULTILINE | re.DOTALL,
)
ENTRY_HEADING_RE = re.compile(r"^### (KB-[A-Z0-9]+-\d{3}) — ", re.MULTILINE)
ANCHOR_RE = re.compile(r'^<a id="(kb-[a-z0-9]+-\d{3})"></a>$', re.MULTILINE)
INDEX_LINK_RE = re.compile(r"\[([A-Z][A-Z0-9]+-\d{3})\]\(#(kb-[a-z0-9]+-\d{3})\)")
KB_REFERENCE_RE = re.compile(r"\bKB-[A-Z0-9]+-\d{3}\b")
BACKTICK_RE = re.compile(r"`([^`]+)`")
METADATA_RE = re.compile(
    r"^- (?P<key>[A-Za-z][A-Za-z -]+):(?P<value>.*)$", re.MULTILINE
)
ALLOWED_STATUSES = {"Validated", "Provisional", "Retired"}
PATH_SUFFIXES = {".json", ".md", ".py", ".txt", ".yaml", ".yml"}


@dataclass(frozen=True, slots=True)
class KnowledgeEntry:
    """One parsed knowledge-base entry."""

    entry_id: str
    anchor: str
    title: str
    metadata: dict[str, str]
    prose: str


def _section(markdown: str, heading: str) -> str | None:
    """Return the contents of one level-two Markdown section."""
    match = re.search(
        rf"^{re.escape(heading)}\n(?P<body>.*?)(?=^## |\Z)",
        markdown,
        re.MULTILINE | re.DOTALL,
    )
    return match.group("body") if match else None


def _parse_entries(markdown: str) -> list[KnowledgeEntry]:
    """Parse entries whose fixed anchor immediately precedes their heading."""
    entries: list[KnowledgeEntry] = []
    for match in ENTRY_RE.finditer(markdown):
        raw_body = match.group("body").strip()
        metadata_block, separator, prose = raw_body.partition("\n\n")
        metadata: dict[str, str] = {}
        current_key: str | None = None
        for line in metadata_block.splitlines():
            item = METADATA_RE.fullmatch(line)
            if item:
                current_key = item.group("key")
                metadata[current_key] = item.group("value").strip()
            elif current_key and line.startswith("  "):
                metadata[current_key] = f"{metadata[current_key]} {line.strip()}"
        entries.append(
            KnowledgeEntry(
                entry_id=match.group("entry_id"),
                anchor=match.group("anchor"),
                title=match.group("title").strip(),
                metadata=metadata,
                prose=prose.strip() if separator else "",
            )
        )
    return entries


def _duplicates(values: list[str]) -> set[str]:
    """Return values occurring more than once."""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _validate_symbol(path: Path, symbol: str) -> bool:
    """Return whether a Python source file defines a named symbol."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return False
    return any(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == symbol
        for node in ast.walk(tree)
    )


def _validate_evidence(root: Path, entry: KnowledgeEntry, errors: list[str]) -> None:
    """Validate explicit repository paths and Python nodes in Evidence."""
    evidence = entry.metadata.get("Evidence", "")
    for reference in BACKTICK_RE.findall(evidence):
        if KB_REFERENCE_RE.fullmatch(reference):
            continue
        path_text, separator, symbol = reference.partition("::")
        path = Path(path_text)
        looks_like_path = "/" in path_text or path.suffix in PATH_SUFFIXES
        if not looks_like_path:
            continue
        if path.is_absolute() or ".." in path.parts:
            errors.append(
                f"{entry.entry_id}: Evidence path must stay repository-relative: "
                f"{reference}"
            )
            continue
        resolved = root / path
        if not resolved.exists():
            errors.append(
                f"{entry.entry_id}: Evidence path does not exist: {path_text}"
            )
            continue
        if separator and (
            resolved.suffix != ".py" or not _validate_symbol(resolved, symbol)
        ):
            errors.append(
                f"{entry.entry_id}: Evidence Python symbol does not exist: {reference}"
            )


def _validate_entry(
    root: Path,
    entry: KnowledgeEntry,
    known_ids: set[str],
    errors: list[str],
) -> None:
    """Validate metadata and lifecycle rules for one entry."""
    expected_anchor = entry.entry_id.lower()
    if entry.anchor != expected_anchor:
        errors.append(
            f"{entry.entry_id}: anchor must be {expected_anchor}, got {entry.anchor}"
        )

    status = entry.metadata.get("Status")
    if status not in ALLOWED_STATUSES:
        errors.append(
            f"{entry.entry_id}: Status must be one of {sorted(ALLOWED_STATUSES)}"
        )

    validated = entry.metadata.get("Last validated")
    try:
        if validated is None or date.fromisoformat(validated).isoformat() != validated:
            raise ValueError
    except ValueError:
        errors.append(f"{entry.entry_id}: Last validated must be an ISO date")

    if not entry.metadata.get("Evidence"):
        errors.append(f"{entry.entry_id}: Evidence is required")
    else:
        _validate_evidence(root, entry, errors)

    superseded = entry.metadata.get("Superseded by")
    replaces = entry.metadata.get("Replaces")
    if status == "Retired":
        if not superseded:
            errors.append(f"{entry.entry_id}: Retired entries require Superseded by")
        if replaces:
            errors.append(f"{entry.entry_id}: Retired entries may not use Replaces")
        if not entry.prose.startswith("Rejected assumption:"):
            errors.append(
                f'{entry.entry_id}: Retired prose must start with "Rejected assumption:"'
            )
    elif superseded:
        errors.append(f"{entry.entry_id}: only Retired entries may use Superseded by")

    if status == "Provisional" and not entry.metadata.get("Validation needed"):
        errors.append(
            f"{entry.entry_id}: Provisional entries require Validation needed"
        )

    external_status = entry.metadata.get("External status")
    external_checked = entry.metadata.get("Last externally checked")
    if bool(external_status) != bool(external_checked):
        errors.append(
            f"{entry.entry_id}: External status and Last externally checked must be paired"
        )
    if external_checked:
        try:
            if date.fromisoformat(external_checked).isoformat() != external_checked:
                raise ValueError
        except ValueError:
            errors.append(
                f"{entry.entry_id}: Last externally checked must be an ISO date"
            )

    for reference in KB_REFERENCE_RE.findall(
        "\n".join([*entry.metadata.values(), entry.prose])
    ):
        if reference not in known_ids:
            errors.append(f"{entry.entry_id}: unknown knowledge reference {reference}")


def _validate_governance_files(root: Path, errors: list[str]) -> None:
    """Ensure the linter is wired into contributor workflow and CI."""
    required_content = {
        Path("AGENTS.md"): (
            "scripts/validate_knowledge.py",
            "scripts/validate_project.py",
            "KNOWLEDGE_HISTORY.md",
        ),
        Path(".github/workflows/validate.yml"): ("scripts/validate_project.py",),
        Path(".github/pull_request_template.md"): ("Knowledge impact",),
    }
    for relative, needles in required_content.items():
        path = root / relative
        if not path.is_file():
            errors.append(f"Required governance file does not exist: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle not in text:
                errors.append(f"{relative}: missing required reference {needle!r}")


def _validate_history(history: str, known_ids: set[str], errors: list[str]) -> None:
    """Validate the separated chronological review register."""
    if "## Review register" not in history:
        errors.append("KNOWLEDGE_HISTORY.md is missing its Review register")
    if ENTRY_HEADING_RE.search(history) or ANCHOR_RE.search(history):
        errors.append("KNOWLEDGE_HISTORY.md must not contain active knowledge entries")
    for reference in KB_REFERENCE_RE.findall(history):
        if reference not in known_ids:
            errors.append(
                f"KNOWLEDGE_HISTORY.md contains unknown knowledge reference {reference}"
            )
    raw_dates = re.findall(r"^\| (\d{4}-\d{2}-\d{2}) \|", history, re.MULTILINE)
    parsed_dates: list[date] = []
    for raw_date in raw_dates:
        try:
            parsed_dates.append(date.fromisoformat(raw_date))
        except ValueError:
            errors.append(f"KNOWLEDGE_HISTORY.md contains invalid date {raw_date}")
    if parsed_dates != sorted(parsed_dates):
        errors.append("KNOWLEDGE_HISTORY.md review rows must be chronological")


def validate_repository(root: Path) -> list[str]:
    """Return every knowledge-governance error found below *root*."""
    root = Path(root)
    knowledge_path = root / "KNOWLEDGE.md"
    errors: list[str] = []
    if not knowledge_path.is_file():
        return ["KNOWLEDGE.md does not exist"]

    markdown = knowledge_path.read_text(encoding="utf-8")
    entries = _parse_entries(markdown)
    parsed_ids = [entry.entry_id for entry in entries]
    heading_ids = ENTRY_HEADING_RE.findall(markdown)
    anchors = ANCHOR_RE.findall(markdown)

    for duplicate in sorted(_duplicates(heading_ids)):
        errors.append(f"Duplicate knowledge ID: {duplicate}")
    for duplicate in sorted(_duplicates(anchors)):
        errors.append(f"Duplicate knowledge anchor: {duplicate}")
    if len(entries) != len(heading_ids) or len(entries) != len(anchors):
        errors.append(
            "Every knowledge heading must have one immediately preceding anchor"
        )

    index = _section(markdown, "## Knowledge index")
    if index is None:
        errors.append("KNOWLEDGE.md is missing the Knowledge index section")
        index_items: list[tuple[str, str]] = []
    else:
        index_items = INDEX_LINK_RE.findall(index)
    index_ids = [f"KB-{short_id}" for short_id, _anchor in index_items]
    index_anchors = [anchor for _short_id, anchor in index_items]
    for duplicate in sorted(_duplicates(index_ids)):
        errors.append(f"Duplicate knowledge index ID: {duplicate}")
    if index_ids != heading_ids:
        errors.append("Knowledge index IDs must exactly match entry order")
    expected_index_anchors = [entry_id.lower() for entry_id in index_ids]
    if index_anchors != expected_index_anchors:
        errors.append(
            "Knowledge index links must target each ID's fixed lowercase anchor"
        )

    by_area: dict[str, list[int]] = {}
    for entry_id in parsed_ids:
        area, number = entry_id.rsplit("-", 1)
        by_area.setdefault(area, []).append(int(number))
    for area, numbers in by_area.items():
        if numbers != sorted(numbers):
            errors.append(f"{area}: entries must remain in ascending numeric order")

    known_ids = set(parsed_ids)
    for entry in entries:
        _validate_entry(root, entry, known_ids, errors)

    provisional_ids = [
        entry.entry_id
        for entry in entries
        if entry.metadata.get("Status") == "Provisional"
    ]
    current_status = re.search(
        r"^- Current status: (?P<status>.+)$", markdown, re.MULTILINE
    )
    if current_status is None:
        errors.append("KNOWLEDGE.md is missing Current status review metadata")
    elif provisional_ids:
        status_text = current_status.group("status")
        if "no known provisional entries" in status_text.lower() or any(
            entry_id not in status_text for entry_id in provisional_ids
        ):
            errors.append("Current status must list every Provisional knowledge entry")
    elif current_status.group("status") != "no known provisional entries":
        errors.append(
            'Current status must be "no known provisional entries" when applicable'
        )

    history_path = root / "KNOWLEDGE_HISTORY.md"
    if not history_path.is_file():
        errors.append("KNOWLEDGE_HISTORY.md does not exist")
    else:
        _validate_history(history_path.read_text(encoding="utf-8"), known_ids, errors)
        if "## Review register" in markdown:
            errors.append(
                "The chronological review register belongs in KNOWLEDGE_HISTORY.md"
            )
        if "](KNOWLEDGE_HISTORY.md)" not in markdown:
            errors.append("KNOWLEDGE.md must link to KNOWLEDGE_HISTORY.md")

    manifest_path = root / "custom_components/guesty/manifest.json"
    try:
        manifest_version = json.loads(manifest_path.read_text(encoding="utf-8"))[
            "version"
        ]
    except (OSError, KeyError, json.JSONDecodeError, TypeError):
        errors.append("Unable to read the integration version from manifest.json")
    else:
        baseline = re.search(
            r"^- Knowledge-base baseline: integration version ([^\s]+)$",
            markdown,
            re.MULTILINE,
        )
        if baseline is None:
            errors.append("KNOWLEDGE.md is missing the knowledge-base baseline version")
        elif baseline.group(1) != manifest_version:
            errors.append(
                "Knowledge-base baseline version does not match manifest.json: "
                f"{baseline.group(1)} != {manifest_version}"
            )

    _validate_governance_files(root, errors)
    return errors


def main() -> int:
    """Run repository validation from the current project root."""
    errors = validate_repository(Path.cwd())
    if errors:
        print("Knowledge validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Knowledge validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
