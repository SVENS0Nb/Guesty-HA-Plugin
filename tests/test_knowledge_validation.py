"""Regression tests for the repository knowledge-governance contract."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.validate_knowledge import validate_repository


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_valid_repository(root: Path) -> None:
    """Create the smallest repository accepted by the knowledge validator."""
    (root / "custom_components/guesty").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / ".github/workflows").mkdir(parents=True)
    (root / "custom_components/guesty/manifest.json").write_text(
        json.dumps({"version": "1.0.0"}), encoding="utf-8"
    )
    (root / "tests/test_sample.py").write_text(
        "def test_sample():\n    pass\n", encoding="utf-8"
    )
    (root / "AGENTS.md").write_text(
        "Run scripts/validate_knowledge.py through scripts/validate_project.py "
        "and maintain KNOWLEDGE_HISTORY.md.\n",
        encoding="utf-8",
    )
    (root / ".github/workflows/validate.yml").write_text(
        "run: python scripts/validate_project.py release\n", encoding="utf-8"
    )
    (root / ".github/pull_request_template.md").write_text(
        "- [ ] Knowledge impact reviewed\n", encoding="utf-8"
    )
    (root / "KNOWLEDGE_HISTORY.md").write_text(
        """# Knowledge Review History

## Review register

| Date | Scope | Result |
| --- | --- | --- |
| 2026-08-14 | Initial review | Validated structure |
""",
        encoding="utf-8",
    )
    (root / "KNOWLEDGE.md").write_text(
        """# Knowledge

## Review metadata

- Knowledge-base baseline: integration version 1.0.0
- Current status: no known provisional entries

## Knowledge index

| Area | Entries |
| --- | --- |
| Architecture | [ARCH-001](#kb-arch-001), [ARCH-002](#kb-arch-002) |

## Architecture

<a id="kb-arch-001"></a>

### KB-ARCH-001 — First fact

- Status: Validated
- Last validated: 2026-08-14
- Evidence: `tests/test_sample.py::test_sample`

The first validated fact.

<a id="kb-arch-002"></a>

### KB-ARCH-002 — Second fact

- Status: Validated
- Last validated: 2026-08-14
- Evidence: `tests/test_sample.py`

The second validated fact.

## Review history

See [KNOWLEDGE_HISTORY.md](KNOWLEDGE_HISTORY.md).
""",
        encoding="utf-8",
    )


def test_repository_knowledge_contract_is_valid() -> None:
    """The checked-in knowledge, evidence, workflow, and index stay aligned."""
    assert validate_repository(PROJECT_ROOT) == []


def test_index_and_anchor_drift_are_rejected(tmp_path: Path) -> None:
    """A stale index cannot silently omit or misroute an entry."""
    _write_valid_repository(tmp_path)
    path = tmp_path / "KNOWLEDGE.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[ARCH-002](#kb-arch-002)", "[ARCH-002](#kb-arch-999)"
        ),
        encoding="utf-8",
    )

    errors = validate_repository(tmp_path)

    assert (
        "Knowledge index links must target each ID's fixed lowercase anchor" in errors
    )


def test_entry_numbering_must_remain_sorted(tmp_path: Path) -> None:
    """Adding an entry in the wrong location is detected deterministically."""
    _write_valid_repository(tmp_path)
    path = tmp_path / "KNOWLEDGE.md"
    markdown = path.read_text(encoding="utf-8")
    first = markdown.index('<a id="kb-arch-001"></a>')
    second = markdown.index('<a id="kb-arch-002"></a>')
    review = markdown.index("## Review history")
    reordered = markdown[:first] + markdown[second:review] + markdown[first:second]
    reordered += markdown[review:]
    reordered = reordered.replace(
        "[ARCH-001](#kb-arch-001), [ARCH-002](#kb-arch-002)",
        "[ARCH-002](#kb-arch-002), [ARCH-001](#kb-arch-001)",
    )
    path.write_text(reordered, encoding="utf-8")

    errors = validate_repository(tmp_path)

    assert "KB-ARCH: entries must remain in ascending numeric order" in errors


def test_retired_entry_requires_explicit_rejected_assumption(tmp_path: Path) -> None:
    """Retired prose cannot read like an active rule or lose its replacement."""
    _write_valid_repository(tmp_path)
    path = tmp_path / "KNOWLEDGE.md"
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace("- Status: Validated", "- Status: Retired", 1)
        .replace(
            "- Evidence: `tests/test_sample.py::test_sample`",
            "- Evidence: `tests/test_sample.py::test_sample`\n- Replaces: KB-ARCH-002",
        )
        .replace("The first validated fact.", "This still sounds authoritative."),
        encoding="utf-8",
    )

    errors = validate_repository(tmp_path)

    assert "KB-ARCH-001: Retired entries require Superseded by" in errors
    assert "KB-ARCH-001: Retired entries may not use Replaces" in errors
    assert 'KB-ARCH-001: Retired prose must start with "Rejected assumption:"' in errors


def test_provisional_entry_requires_validation_plan(tmp_path: Path) -> None:
    """An unverified idea must say how it will become evidence-backed."""
    _write_valid_repository(tmp_path)
    path = tmp_path / "KNOWLEDGE.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "- Status: Validated", "- Status: Provisional", 1
        ),
        encoding="utf-8",
    )

    assert "KB-ARCH-001: Provisional entries require Validation needed" in (
        validate_repository(tmp_path)
    )


def test_missing_evidence_path_and_symbol_are_rejected(tmp_path: Path) -> None:
    """Evidence citations must resolve to the named repository artifact."""
    _write_valid_repository(tmp_path)
    path = tmp_path / "KNOWLEDGE.md"
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace(
            "`tests/test_sample.py::test_sample`",
            "`tests/test_sample.py::test_missing`",
        )
        .replace("`tests/test_sample.py`", "`tests/missing.py`"),
        encoding="utf-8",
    )

    errors = validate_repository(tmp_path)

    assert any("Evidence Python symbol does not exist" in error for error in errors)
    assert any("Evidence path does not exist" in error for error in errors)


def test_manifest_and_knowledge_baseline_must_match(tmp_path: Path) -> None:
    """A release version bump also advances the reviewed knowledge baseline."""
    _write_valid_repository(tmp_path)
    (tmp_path / "custom_components/guesty/manifest.json").write_text(
        json.dumps({"version": "1.0.1"}), encoding="utf-8"
    )

    errors = validate_repository(tmp_path)

    assert any("baseline version does not match" in error for error in errors)


def test_external_contract_metadata_is_atomic(tmp_path: Path) -> None:
    """External contract status cannot be recorded without a check date."""
    _write_valid_repository(tmp_path)
    path = tmp_path / "KNOWLEDGE.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "- Evidence: `tests/test_sample.py::test_sample`",
            "- Evidence: `tests/test_sample.py::test_sample`\n"
            "- External status: Deprecated compatibility endpoint",
        ),
        encoding="utf-8",
    )

    assert (
        "KB-ARCH-001: External status and Last externally checked must be paired"
        in validate_repository(tmp_path)
    )


def test_history_references_and_dates_are_validated(tmp_path: Path) -> None:
    """The audit trail cannot point to unknown facts or lose chronology."""
    _write_valid_repository(tmp_path)
    path = tmp_path / "KNOWLEDGE_HISTORY.md"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "| 2026-08-13 | Later insertion | See `KB-MISSING-999` |\n",
        encoding="utf-8",
    )

    errors = validate_repository(tmp_path)

    assert (
        "KNOWLEDGE_HISTORY.md contains unknown knowledge reference KB-MISSING-999"
        in errors
    )
    assert "KNOWLEDGE_HISTORY.md review rows must be chronological" in errors
