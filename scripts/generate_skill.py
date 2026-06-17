#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pylance", "packaging"]
# ///
"""Generate a filesystem-based agentskills.io skill around a haiku-rag database.

Given a ``haiku.rag.yaml`` config and a ``haiku-rag``-built LanceDB database
directory (``<stem>.lancedb``), this scaffolds a self-contained skill named
``<stem>-haiku-rag`` from the bundled ``template/`` tree and embeds both the
database and the config under the skill's ``assets/`` directory.

The generated skill's wrapper pins an exact ``haiku-rag`` version so the
embedded database and the runtime always agree:

* By default the version is **sniffed** from the database itself (the version
  that last wrote it) and pinned as-is — no migration required.
* With ``--haiku-rag-version X`` the version is **forced**: it is pinned in the
  wrapper and the embedded copy of the database is migrated up to ``X``.

Usage:

    uv run scripts/generate_skill.py \\
        --config path/to/haiku.rag.yaml \\
        --db path/to/handbook.lancedb \\
        [--output DIR] \\
        [--haiku-rag-version 0.48.1] \\
        [--package-name haiku-rag-slim]
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

PLACEHOLDER = "[dbname]"
REQUIREMENT_PLACEHOLDER = "[hr_requirement]"
LANCEDB_SUFFIX = ".lancedb"
DEFAULT_PACKAGE = "haiku-rag-slim"

# The minimum haiku-rag version the bundled wrapper supports. The wrapper relies
# on APIs (expand_context, resolve_citations, format_for_agent, ...) whose stable
# surface is not guaranteed below this release, so we refuse to pin anything
# older and direct the user to force-migrate the embedded database up instead.
MINIMUM_VERSION = "0.48.1"

# The placeholder skill directory inside the repo's template tree.
TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "template"
TEMPLATE_SKILL = TEMPLATE_ROOT / f"{PLACEHOLDER}-haiku-rag"

# Suffixes whose contents get placeholder substitution; everything else is
# copied verbatim.
TEXT_SUFFIXES = {".md", ".py", ".txt", ".yaml", ".yml", ".toml"}


class GenerateError(Exception):
    """A user-facing generation error (bad inputs, conflicting output)."""


def db_stem(db_path: Path) -> str:
    """Return the skill stem derived from a ``<stem>.lancedb`` directory name."""
    name = db_path.name
    if not name.endswith(LANCEDB_SUFFIX) or name == LANCEDB_SUFFIX:
        raise GenerateError(
            f"database directory must be named '<stem>{LANCEDB_SUFFIX}', "
            f"got: {db_path.name}"
        )
    return name[: -len(LANCEDB_SUFFIX)]


def sniff_db_version(db_path: Path) -> str | None:
    """Return the haiku-rag version stored in the database, or None.

    haiku-rag records the writing version under the ``version`` key of the
    JSON ``settings`` row in the ``settings`` table. We read that Lance dataset
    directly (no ``HaikuRAG`` open, which would refuse on a version mismatch).
    """
    import lance

    settings_dataset = db_path / "settings.lance"
    if not settings_dataset.exists():
        return None
    rows = lance.dataset(str(settings_dataset)).to_table().to_pylist()
    for row in rows:
        if row.get("id") == "settings" and row.get("settings"):
            version = json.loads(row["settings"]).get("version")
            return version or None
    return None


def validate_inputs(db_path: Path, config_path: Path, target: Path) -> None:
    """Validate the database, config, and (non-existent) output target."""
    if not db_path.exists():
        raise GenerateError(f"database does not exist: {db_path}")
    if not db_path.is_dir():
        raise GenerateError(f"database is not a directory: {db_path}")
    if not config_path.exists():
        raise GenerateError(f"config does not exist: {config_path}")
    if not config_path.is_file():
        raise GenerateError(f"config is not a file: {config_path}")
    if not TEMPLATE_SKILL.is_dir():
        raise GenerateError(f"template skill not found: {TEMPLATE_SKILL}")
    if target.exists():
        raise GenerateError(f"target directory already exists: {target}")


def resolve_version(
    db_path: Path, forced: str | None
) -> tuple[str, str | None]:
    """Return ``(target_version, sniffed_version)`` for the skill.

    ``target_version`` is what gets pinned in the wrapper: the forced version
    if given, otherwise the version sniffed from the database. Raises when no
    version can be determined.
    """
    sniffed = sniff_db_version(db_path)
    target = forced or sniffed
    if target is None:
        raise GenerateError(
            "could not determine the database's haiku-rag version "
            "(no readable 'settings' table); pass --haiku-rag-version explicitly"
        )
    return target, sniffed


def render_tree(
    template_root: Path, target: Path, substitutions: dict[str, str]
) -> None:
    """Copy the template tree to ``target``, applying placeholder substitutions.

    Each ``substitutions`` key is replaced with its value in both path names
    and the contents of text files; binary and unknown files are copied
    verbatim. Executable bits are preserved.
    """

    def substitute(text: str) -> str:
        for token, value in substitutions.items():
            text = text.replace(token, value)
        return text

    for src in sorted(template_root.rglob("*")):
        rel = src.relative_to(template_root)
        # Never propagate Python bytecode caches into the generated skill.
        if "__pycache__" in rel.parts or src.suffix == ".pyc":
            continue
        dest = target / Path(*[substitute(part) for part in rel.parts])
        if src.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix in TEXT_SUFFIXES:
            dest.write_text(
                substitute(src.read_text(encoding="utf-8")), encoding="utf-8"
            )
            shutil.copymode(src, dest)
        else:
            shutil.copy2(src, dest)


def migrate_database(db_path: Path, config_path: Path, requirement: str) -> None:
    """Migrate ``db_path`` up to the version pinned by ``requirement``.

    Runs the requested haiku-rag version ephemerally via ``uv tool run`` so the
    generator does not need it installed.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise GenerateError(
            "migration needs 'uv' on PATH to run the requested haiku-rag version"
        )
    cmd = [
        uv,
        "tool",
        "run",
        "--from",
        requirement,
        "haiku-rag",
        "--config",
        str(config_path),
        "migrate",
        "--db",
        str(db_path),
    ]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise GenerateError(
            f"migration to '{requirement}' failed (exit {result.returncode})"
        )


def generate_skill(
    db_path: Path,
    config_path: Path,
    output_dir: Path,
    *,
    haiku_rag_version: str | None = None,
    package_name: str = DEFAULT_PACKAGE,
) -> Path:
    """Generate the skill and return the path to the created skill directory."""
    from packaging.version import InvalidVersion, Version

    stem = db_stem(db_path)
    target = output_dir / f"{stem}-haiku-rag"

    validate_inputs(db_path, config_path, target)

    version, sniffed = resolve_version(db_path, haiku_rag_version)
    requirement = f"{package_name}=={version}"

    # Enforce the wrapper's minimum supported version: the API surface it uses
    # is not guaranteed below MINIMUM_VERSION.
    try:
        below_minimum = Version(version) < Version(MINIMUM_VERSION)
    except InvalidVersion:
        below_minimum = False  # non-PEP440 tag; cannot compare, allow through
    if below_minimum:
        if haiku_rag_version:
            raise GenerateError(
                f"--haiku-rag-version {version} is below the minimum supported "
                f"by this wrapper ({MINIMUM_VERSION})"
            )
        raise GenerateError(
            f"database version {version} is below the minimum supported by this "
            f"wrapper ({MINIMUM_VERSION}); regenerate with "
            f"--haiku-rag-version {MINIMUM_VERSION} (or newer) to migrate the "
            f"embedded copy up"
        )

    if sniffed and version != sniffed:
        try:
            if Version(version) < Version(sniffed):
                raise GenerateError(
                    f"cannot downgrade the embedded database from {sniffed} "
                    f"to {version}; haiku-rag migrations only move forward"
                )
        except InvalidVersion:
            pass  # non-PEP440 tag; let migration decide what is possible
        print(
            f"Database version {sniffed} -> migrating embedded copy to {version}"
        )

    render_tree(TEMPLATE_SKILL, target, {PLACEHOLDER: stem, REQUIREMENT_PLACEHOLDER: requirement})

    assets = target / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    embedded_db = assets / f"{stem}{LANCEDB_SUFFIX}"
    embedded_config = assets / "haiku.rag.yaml"
    shutil.copytree(db_path, embedded_db)
    shutil.copy2(config_path, embedded_config)

    # Forcing a newer version: bring the embedded copy up to it so the wrapper's
    # pinned runtime can open the database without a migration prompt.
    if sniffed and version != sniffed:
        migrate_database(embedded_db, embedded_config, requirement)

    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate_skill.py",
        description="Generate a filesystem skill around a haiku-rag database.",
    )
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help="Path to the haiku.rag.yaml config to embed",
    )
    parser.add_argument(
        "--db",
        required=True,
        type=Path,
        help="Path to the <stem>.lancedb database directory to embed",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("."),
        help="Directory to create the skill in (default: current directory)",
    )
    parser.add_argument(
        "--haiku-rag-version",
        default=None,
        help="Force this haiku-rag version: pin it in the wrapper and migrate "
        "the embedded database to it (default: sniff the database's version)",
    )
    parser.add_argument(
        "--package-name",
        default=DEFAULT_PACKAGE,
        help=f"Distribution to pin in the wrapper (default: {DEFAULT_PACKAGE})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        target = generate_skill(
            args.db,
            args.config,
            args.output,
            haiku_rag_version=args.haiku_rag_version,
            package_name=args.package_name,
        )
    except GenerateError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Skill generated: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
