"""Tests for the shared project validation profiles."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.validate_project import (
    command_plan,
    repository_json_paths,
    validate_repository_json,
)


def test_focused_profile_infers_matching_regression_test() -> None:
    """A production module automatically selects its conventional test module."""
    plan = command_plan("focused", ["custom_components/guesty/api.py"])

    assert [sys.executable, "-m", "pytest", "-W", "error", "tests/test_api.py"] in plan
    assert [sys.executable, "scripts/validate_knowledge.py"] in plan
    assert ["git", "diff", "--check"] in plan


def test_focused_profile_preserves_explicit_pytest_node() -> None:
    """Agents can run one exact regression without losing static validation."""
    node = "tests/test_api.py::test_bounded_write_does_not_replay_after_rejected_token"

    plan = command_plan("focused", [node])

    assert [sys.executable, "-m", "pytest", "-W", "error", node] in plan


def test_explicit_node_replaces_broader_inferred_test() -> None:
    """Supplying one node keeps focused iteration from running its whole module."""
    node = "tests/test_api.py::test_bounded_write_does_not_replay_after_rejected_token"

    plan = command_plan("focused", ["custom_components/guesty/api.py", node])
    pytest_command = next(command for command in plan if "pytest" in command)

    assert pytest_command[-1] == node
    assert "tests/test_api.py" not in pytest_command


def test_focused_profile_rejects_missing_or_escaping_paths() -> None:
    """Focused validation cannot silently skip misspelled or external targets."""
    with pytest.raises(ValueError, match="requires at least one"):
        command_plan("focused")
    with pytest.raises(ValueError, match="does not exist"):
        command_plan("focused", ["tests/not_present.py"])
    with pytest.raises(ValueError, match="leaves the repository"):
        command_plan("focused", ["../outside.py"])


def test_standard_and_release_profiles_have_distinct_depth() -> None:
    """Only the release profile adds coverage and security/dependency audits."""
    standard = command_plan("standard")
    release = command_plan("release")
    standard_text = "\n".join(" ".join(command) for command in standard)
    release_text = "\n".join(" ".join(command) for command in release)

    assert "--cov-fail-under=80" not in standard_text
    assert "pip_audit" not in standard_text
    assert "--cov-fail-under=80" in release_text
    assert "pip_audit" in release_text
    assert "bandit" in release_text


def test_non_focused_profiles_reject_paths() -> None:
    """A stray argument cannot accidentally turn a complete check into a subset."""
    with pytest.raises(ValueError, match="does not accept focused paths"):
        command_plan("release", ["tests/test_api.py"])


def test_repository_json_selection_excludes_ignored_environment(tmp_path: Path) -> None:
    """JSON validation covers repository files without walking virtualenv caches."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (tmp_path / "tracked.json").write_text("{}", encoding="utf-8")
    (tmp_path / "new.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv/ignored.json").write_text("{}", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitignore", "tracked.json"], cwd=tmp_path, check=True
    )

    paths = repository_json_paths(tmp_path)

    assert paths == [tmp_path / "new.json", tmp_path / "tracked.json"]


def test_repository_json_validation_reports_invalid_file(tmp_path: Path) -> None:
    """Malformed tracked or new JSON produces a repository-relative error."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "valid.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    (tmp_path / "invalid.json").write_text("{", encoding="utf-8")

    errors = validate_repository_json(tmp_path)

    assert len(errors) == 1
    assert errors[0].startswith("invalid.json:")
