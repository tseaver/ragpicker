"""Generate a filesystem-based skill around a haiku-rag database.

Spec for skill from agentskills.io.

Given a ``haiku.rag.yaml`` config and a ``haiku-rag``-built LanceDB database
directory (``<stem>.lancedb``), this scaffolds a self-contained skill named
``<stem>-haiku-rag`` from the bundled ``template/`` tree and embeds both the
database and the config under the skill's ``assets/`` directory.

The generated skill's wrapper pins an exact ``haiku-rag`` version so the
embedded database and the runtime always agree:

* By default the version is **sniffed** from the database itself (the version
  that last wrote it) and pinned as-is -- no migration required.
* With ``--haiku-rag-version X`` the version is **forced**: it is pinned in the
  wrapper and the embedded copy of the database is migrated up to ``X``.
* With ``--version-from-project PATH`` the version is **discovered** from a
  target project (e.g. a Soliplex stack) -- its ``.venv`` or ``pyproject.toml``
  -- and forced the same way, so the embedded database matches the runtime that
  will open it.

Usage:

    ragpicker \\
        --config path/to/haiku.rag.yaml \\
        --db path/to/handbook.lancedb \\
        [--output DIR] \\
        [--haiku-rag-version 0.48.1 | --version-from-project path/to/stack]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import packaging.version

PLACEHOLDER = "[dbname]"
REQUIREMENT_PLACEHOLDER = "[hr_requirement]"
LANCEDB_SUFFIX = ".lancedb"

# The only distribution the generated wrapper pins. The full ``haiku-rag``
# build is intentionally unsupported -- its dependency weight is prohibitive
# for a self-contained skill -- so the slim repackaging is hardcoded.
PACKAGE = "haiku-rag-slim"

# The minimum haiku-rag version the bundled wrapper supports. The wrapper
# relies on APIs (expand_context, resolve_citations, format_for_agent, ...)
# whose stable surface is not guaranteed below this release, so we refuse to
# pin anything older and direct the user to force-migrate the embedded
# database up instead.
MINIMUM_VERSION = "0.48.1"

# The placeholder skill directory inside the bundled template tree. The
# template ships as package data alongside this module (see pyproject's hatch
# config), so it is found relative to ``__file__`` and travels with an
# installed wheel.
TEMPLATE_ROOT = Path(__file__).resolve().parent / "template"
TEMPLATE_SKILL = TEMPLATE_ROOT / f"{PLACEHOLDER}-haiku-rag"

# Suffixes whose contents get placeholder substitution; everything else is
# copied verbatim.
TEXT_SUFFIXES = {".md", ".py", ".txt", ".yaml", ".yml", ".toml"}


class GenerateError(Exception):
    """A user-facing generation error (bad inputs, conflicting output)."""


class NotALanceDB(GenerateError):
    def __init__(self, db_path):
        self.db_path = db_path
        super().__init__(
            f"database directory must be named '<stem>{LANCEDB_SUFFIX}', "
            f"got: {db_path.name}"
        )


class ProjectDoesNotExist(GenerateError):
    def __init__(self, project_dir):
        self.project_dir = project_dir
        super().__init__(
            f"--version-from-project path does not exist: {project_dir}"
        )


class UnknownProjectVersion(GenerateError):
    def __init__(self, dist, project_dir):
        self.dist = dist
        self.project_dir = project_dir
        super().__init__(
            f"could not determine an effective '{dist}' version from "
            f"{project_dir} (looked for a '.venv' with it installed, then "
            f"resolved the project's dependencies with 'uv tree'); "
            "pass --haiku-rag-version explicitly"
        )


class RAG_DatabaseDoesNotExist(GenerateError):
    def __init__(self, db_path):
        self.db_path = db_path
        super().__init__(f"database does not exist: {db_path}")


class RAG_DatabaseIsNotADirectory(GenerateError):
    def __init__(self, db_path):
        self.db_path = db_path
        super().__init__(f"database is not a directory: {db_path}")


class ConfigDoesNotExist(GenerateError):
    def __init__(self, config_path):
        self.config_path = config_path
        super().__init__(f"config does not exist: {config_path}")


class ConfigIsNotAFile(GenerateError):
    def __init__(self, config_path):
        self.config_path = config_path
        super().__init__(f"config is not a file: {config_path}")


class TargetDirectoryExists(GenerateError):
    def __init__(self, target):
        self.target = target
        super().__init__(f"target directory already exists: {target}")


class Unknown_RAG_DatabaseVersion(GenerateError):
    def __init__(self):
        super().__init__(
            "could not determine the database's haiku-rag version "
            "(no readable 'settings' table); pass --haiku-rag-version "
            "explicitly"
        )


class RAG_DatabaseMigrationRequires_UV(GenerateError):
    def __init__(self):
        super().__init__(
            "migration needs 'uv' on PATH to run the requested "
            "haiku-rag version"
        )


class Failed_RAG_DatabaseMigration(GenerateError):
    def __init__(self, requirement, returncode):
        self.requirement = requirement
        self.returncode = returncode
        super().__init__(
            f"migration to '{requirement}' failed (exit {returncode})"
        )


class InvalidForcedVersion(GenerateError):
    def __init__(self, version):
        self.version = version
        super().__init__(
            f"--haiku-rag-version {version!r} is not a valid version"
        )


class Haiku_RAG_VersionTooOld(GenerateError):
    def __init__(self, version):
        self.version = version
        super().__init__(
            f"--haiku-rag-version {version} is below the minimum supported "
            f"by this wrapper ({MINIMUM_VERSION})"
        )


class RAG_DatabaseVersionTooOld(GenerateError):
    def __init__(self, version):
        self.version = version
        super().__init__(
            f"database version {version} is below the minimum supported by "
            f"this wrapper ({MINIMUM_VERSION}); regenerate with "
            f"--haiku-rag-version {MINIMUM_VERSION} (or newer) to migrate "
            f"the embedded copy up"
        )


class CannotDowngrade_RAG_Database(GenerateError):
    def __init__(self, sniffed, version):
        self.sniffed = sniffed
        self.version = version
        super().__init__(
            f"cannot downgrade the embedded database from {sniffed} "
            f"to {version}; haiku-rag migrations only move forward"
        )


def db_stem(db_path: Path) -> str:
    """Skill stem derived from a ``<stem>.lancedb`` directory name"""
    name = db_path.name

    if not name.endswith(LANCEDB_SUFFIX) or name == LANCEDB_SUFFIX:
        raise NotALanceDB(db_path)

    return name[: -len(LANCEDB_SUFFIX)]


def sniff_db_version(db_path: Path) -> str | None:
    """haiku-rag version stored in the database, or None.

    haiku-rag records the writing version under the ``version`` key of the
    JSON ``settings`` row in the ``settings`` table. We read that Lance
    dataset directly (no ``HaikuRAG`` open, which would refuse on a version
    mismatch).
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


def _venv_site_packages(venv: Path):
    """``site-packages`` directories of a virtualenv (POSIX + Windows).

    The POSIX matches are sorted: ``Path.glob`` yields in arbitrary
    filesystem order, so sorting gives stable, reproducible iteration.
    """
    yield from sorted(venv.glob("lib/python*/site-packages"))

    windows = venv / "Lib" / "site-packages"

    if windows.is_dir():
        yield windows


def _version_from_venv(venv: Path, dist: str) -> str | None:
    """Get ``dist``'s installed version in ``venv`` from its ``.dist-info``.

    Reads the canonical ``<escaped_name>-<version>.dist-info`` directory name,
    so it does not need to run the (possibly foreign) venv's interpreter.
    """
    escaped = dist.replace("-", "_")

    for site_packages in _venv_site_packages(venv):
        for dist_info in site_packages.glob(f"{escaped}-*.dist-info"):
            stem = dist_info.name[: -len(".dist-info")]
            return stem[len(escaped) + 1 :]

    return None


def _version_from_uv_tree(project_dir: Path, dist: str) -> str | None:
    """Return ``dist``'s resolved version via ``uv tree`` in ``project_dir``.

    ``uv tree --package <dist> --depth 0`` renders the package's resolved node
    as ``<dist> v<version>``. This works even when ``dist`` is a *transitive*
    dependency (e.g. ``haiku-rag-slim`` pulled in by ``soliplex``) and
    resolves version ranges to the concrete version uv would install. Returns
    None when ``uv`` is unavailable or the package is absent from the
    resolution.
    """
    uv = shutil.which("uv")

    if uv is None:
        return None

    result = subprocess.run(
        [uv, "tree", "--package", dist, "--depth", "0"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        match = re.match(rf"{re.escape(dist)}\s+v(\S+)", line.strip())

        if match:
            return match.group(1)

    return None


def version_from_project(project_dir: Path, dist: str = PACKAGE) -> str:
    """Effective ``dist`` version discovered from a target project directory.

    Prefers an installed ``.venv`` (the *effective* version actually present);
    falls back to resolving the project's dependencies with ``uv tree`` (which
    handles ``dist`` being a transitive dependency or pinned to a range).
    Raises ``GenerateError`` when neither yields a concrete version.
    """
    if not project_dir.exists():
        raise ProjectDoesNotExist(project_dir)

    venv = project_dir / ".venv"

    if venv.is_dir():
        version = _version_from_venv(venv, dist)

        if version:
            return version

    pyproject = project_dir / "pyproject.toml"

    if pyproject.is_file():
        version = _version_from_uv_tree(project_dir, dist)

        if version:
            return version

    raise UnknownProjectVersion(dist, project_dir)


def validate_inputs(db_path: Path, config_path: Path, target: Path) -> None:
    """Validate the database, config, and (non-existent) output target."""
    if not db_path.exists():
        raise RAG_DatabaseDoesNotExist(db_path)

    if not db_path.is_dir():
        raise RAG_DatabaseIsNotADirectory(db_path)

    if not config_path.exists():
        raise ConfigDoesNotExist(config_path)

    if not config_path.is_file():
        raise ConfigIsNotAFile(config_path)

    if target.exists():
        raise TargetDirectoryExists(target)


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
        raise Unknown_RAG_DatabaseVersion()

    return target, sniffed


def render_tree(
    template_root: Path, target: Path, substitutions: dict[str, str]
) -> None:
    """Copy template tree to ``target``, applying placeholder substitutions.

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


def migrate_database(
    db_path: Path, config_path: Path, requirement: str
) -> None:
    """Migrate ``db_path`` up to the version pinned by ``requirement``.

    Runs the requested haiku-rag version ephemerally via ``uv tool run`` so
    the generator does not need it installed.
    """
    uv = shutil.which("uv")

    if uv is None:
        raise RAG_DatabaseMigrationRequires_UV()

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
        raise Failed_RAG_DatabaseMigration(requirement, result.returncode)


def generate_skill(
    db_path: Path,
    config_path: Path,
    output_dir: Path,
    *,
    haiku_rag_version: str | None = None,
) -> Path:
    """Generate the skill

    Return the path to the created skill directory.
    """
    stem = db_stem(db_path)
    target = output_dir / f"{stem}-haiku-rag"

    validate_inputs(db_path, config_path, target)

    version, sniffed = resolve_version(db_path, haiku_rag_version)
    requirement = f"{PACKAGE}=={version}"

    # The pinned version must meet the wrapper's minimum supported API surface.
    # A user-forced version is already validated in main(); this guards a
    # database whose own (sniffed) version predates MINIMUM_VERSION. Every
    # version reaching here is written by haiku-rag or already validated, so it
    # parses cleanly -- no InvalidVersion handling needed.
    pv_version = packaging.version.Version(version)

    if pv_version < packaging.version.Version(MINIMUM_VERSION):
        raise RAG_DatabaseVersionTooOld(version)

    if sniffed and version != sniffed:
        if pv_version < packaging.version.Version(sniffed):
            raise CannotDowngrade_RAG_Database(sniffed, version)

        print(
            f"Database version {sniffed} -> migrating embedded copy "
            f"to {version}"
        )

    render_tree(
        TEMPLATE_SKILL,
        target,
        {PLACEHOLDER: stem, REQUIREMENT_PLACEHOLDER: requirement},
    )

    assets = target / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    embedded_db = assets / f"{stem}{LANCEDB_SUFFIX}"
    embedded_config = assets / "haiku.rag.yaml"
    shutil.copytree(db_path, embedded_db)
    shutil.copy2(config_path, embedded_config)

    # Forcing a newer version: bring the embedded copy up to it so th
    # wrapper's pinned runtime can open the database without a migration
    # prompt.
    if sniffed and version != sniffed:
        migrate_database(embedded_db, embedded_config, requirement)

    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ragpicker",
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
    version_source = parser.add_mutually_exclusive_group()
    version_source.add_argument(
        "--haiku-rag-version",
        default=None,
        help="Force this haiku-rag version: pin it in the wrapper and migrate "
        "the embedded database to it (default: sniff the database's version). "
        "Set this to the haiku-rag version installed on the "
        "DEPLOYMENT BACKEND when targeting an embedding host such as "
        "Soliplex: it runs the skill script with the backend's Python "
        "interpreter (not uv), so the embedded database must match that "
        "version rather than the wrapper's pin.",
    )
    version_source.add_argument(
        "--version-from-project",
        type=Path,
        default=None,
        metavar="PATH",
        help="Discover the haiku-rag version to force from a target project "
        "(e.g. a Soliplex stack): reads its '.venv' (the effective installed "
        "version), else resolves its dependencies with 'uv tree'. A "
        "convenient way to match the deployment backend without typing the "
        "version by hand.",
    )
    return parser


def validate_forced_version(version: str) -> None:
    """Validate a user-supplied ``--haiku-rag-version``.

    The CLI value is the only version that is not already trusted -- the
    database records its own version and ``MINIMUM_VERSION`` is ours -- so it
    is checked here, up front: it must parse as PEP 440 and meet the wrapper's
    minimum.
    """
    try:
        pv_version = packaging.version.Version(version)
    except packaging.version.InvalidVersion as exc:
        raise InvalidForcedVersion(version) from exc

    if pv_version < packaging.version.Version(MINIMUM_VERSION):
        raise Haiku_RAG_VersionTooOld(version)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    forced = args.haiku_rag_version

    try:
        if forced is None and args.version_from_project is not None:
            forced = version_from_project(args.version_from_project)

        if forced is not None:
            validate_forced_version(forced)  # raise and exit

        target = generate_skill(
            args.db,
            args.config,
            args.output,
            haiku_rag_version=forced,
        )
    except GenerateError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Skill generated: {target}")

    version, _ = resolve_version(args.db, forced)

    print(f"""\
Embedded database pinned to haiku-rag {version}. When deploying to an
embedding host such as Soliplex, ensure the backend's installed
haiku-rag matches this version: the host opens the database with its
own interpreter (not uv), so a mismatch makes the skill fail.
""")
    return 0


if __name__ == "__main__":  # pragma: NO COVER
    sys.exit(main())
