"""Run the repository's focused, standard, or release validation profile."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON_TARGETS = ("custom_components", "scripts", "tests")


def repository_json_paths(root: Path) -> list[Path]:
    """Return tracked and unignored untracked repository JSON files."""
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "*.json",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return sorted(
        root / Path(os.fsdecode(raw)) for raw in result.stdout.split(b"\0") if raw
    )


def _validate_json_paths(root: Path, paths: Sequence[Path]) -> list[str]:
    """Return parse errors for the supplied repository JSON files."""
    errors: list[str] = []
    for path in paths:
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
            errors.append(f"{path.relative_to(root)}: {err}")
    return errors


def validate_repository_json(root: Path) -> list[str]:
    """Return parse errors for repository-owned JSON files."""
    return _validate_json_paths(root, repository_json_paths(root))


def _path_without_pytest_node(raw: str) -> str:
    """Return a filesystem path from an optional pytest node ID."""
    return raw.partition("::")[0]


def _focused_plan(paths: Sequence[str]) -> list[list[str]]:
    """Build focused commands and infer matching tests for production files."""
    if not paths:
        raise ValueError("The focused profile requires at least one path or test node")

    python_paths: list[str] = []
    explicit_test_nodes: list[str] = []
    inferred_test_nodes: list[str] = []
    for raw in paths:
        raw_path = _path_without_pytest_node(raw)
        relative = Path(raw_path)
        absolute = (PROJECT_ROOT / relative).resolve()
        try:
            repo_relative = absolute.relative_to(PROJECT_ROOT)
        except ValueError as err:
            raise ValueError(f"Focused path leaves the repository: {raw}") from err
        if not absolute.exists():
            raise ValueError(f"Focused path does not exist: {raw}")
        if absolute.suffix == ".py":
            normalized = repo_relative.as_posix()
            if normalized not in python_paths:
                python_paths.append(normalized)
            if normalized.startswith("tests/"):
                node = f"{normalized}{raw[len(raw_path) :]}"
                if node not in explicit_test_nodes:
                    explicit_test_nodes.append(node)
            elif normalized.startswith(("custom_components/guesty/", "scripts/")):
                stem = "init" if absolute.stem == "__init__" else absolute.stem
                candidate = PROJECT_ROOT / "tests" / f"test_{stem}.py"
                inferred = candidate.relative_to(PROJECT_ROOT).as_posix()
                if candidate.exists() and inferred not in inferred_test_nodes:
                    inferred_test_nodes.append(inferred)

    commands: list[list[str]] = []
    test_nodes = explicit_test_nodes or inferred_test_nodes
    if test_nodes:
        commands.append([sys.executable, "-m", "pytest", "-W", "error", *test_nodes])
    if python_paths:
        commands.extend(
            [
                [sys.executable, "-m", "ruff", "check", *python_paths],
                [sys.executable, "-m", "ruff", "format", "--check", *python_paths],
                [sys.executable, "-m", "compileall", "-q", *python_paths],
            ]
        )
    commands.extend(
        [
            [sys.executable, "scripts/validate_knowledge.py"],
            ["git", "diff", "--check"],
        ]
    )
    return commands


def command_plan(profile: str, paths: Sequence[str] = ()) -> list[list[str]]:
    """Return the subprocess plan for one validation profile."""
    if profile == "focused":
        return _focused_plan(paths)
    if paths:
        raise ValueError(f"The {profile} profile does not accept focused paths")
    if profile not in {"standard", "release"}:
        raise ValueError(f"Unknown validation profile: {profile}")

    if profile == "release":
        pytest_command = [
            sys.executable,
            "-m",
            "pytest",
            "-W",
            "error",
            "--cov=custom_components/guesty",
            "--cov-report=term-missing",
            "--cov-fail-under=80",
        ]
    else:
        pytest_command = [sys.executable, "-m", "pytest", "-W", "error"]

    commands = [
        pytest_command,
        [sys.executable, "-m", "ruff", "check", *PYTHON_TARGETS],
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--check",
            *PYTHON_TARGETS,
        ],
        [sys.executable, "-m", "compileall", "-q", *PYTHON_TARGETS],
        [sys.executable, "scripts/validate_knowledge.py"],
    ]
    if profile == "release":
        commands.extend(
            [
                [
                    sys.executable,
                    "-m",
                    "bandit",
                    "-q",
                    "-r",
                    "custom_components/guesty",
                    "-ll",
                ],
                [
                    sys.executable,
                    "-m",
                    "pip_audit",
                    "-r",
                    "requirements-runtime.txt",
                ],
                [sys.executable, "-m", "pip", "check"],
            ]
        )
    commands.append(["git", "diff", "--check"])
    return commands


def run_profile(profile: str, paths: Sequence[str] = ()) -> None:
    """Execute one validation profile and fail on its first error."""
    commands = command_plan(profile, paths)
    if profile in {"standard", "release"}:
        json_paths = repository_json_paths(PROJECT_ROOT)
        errors = _validate_json_paths(PROJECT_ROOT, json_paths)
        if errors:
            joined = "\n".join(f"- {error}" for error in errors)
            raise RuntimeError(f"Repository JSON validation failed:\n{joined}")
        print(f"Validated {len(json_paths)} repository JSON files.", flush=True)

    for command in commands:
        print(f"+ {shlex.join(command)}", flush=True)
        subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=("focused", "standard", "release"))
    parser.add_argument(
        "paths",
        nargs="*",
        help="Existing repository paths or pytest nodes for the focused profile",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the selected profile with concise validation errors."""
    args = _parser().parse_args(argv)
    try:
        run_profile(args.profile, args.paths)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as err:
        print(f"Validation failed: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
