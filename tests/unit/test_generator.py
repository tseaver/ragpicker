import contextlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from ragpicker import generator


@pytest.fixture
def fake_db(tmp_path):
    """A stand-in ``handbook.lancedb`` directory with no version metadata."""
    db = tmp_path / "handbook.lancedb"
    db.mkdir()
    (db / "data.bin").write_bytes(b"\x00\x01\x02")
    return db


@pytest.fixture
def make_unversioned_db(tmp_path):
    """Factory creating a ``<stem>.lancedb`` with no settings table version."""
    created: list[Path] = []

    def _make(stem: str) -> Path:
        lance = pytest.importorskip("lance")
        import pyarrow as pa

        parent = tmp_path / f"src{len(created)}"
        parent.mkdir()
        db = parent / f"{stem}.lancedb"
        db.mkdir()
        (db / "data.bin").write_bytes(b"\x00")
        table = pa.table(
            {
                "id": ["settings"],
                # "settings": [json.dumps({"not-version": "xxx"})],
            }
        )
        lance.write_dataset(table, str(db / "settings.lance"))
        created.append(db)
        return db

    return _make


@pytest.fixture
def make_versioned_db(tmp_path):
    """Factory creating a ``<stem>.lancedb`` with a settings table version."""
    created: list[Path] = []

    def _make(stem: str, version: str) -> Path:
        lance = pytest.importorskip("lance")
        import pyarrow as pa

        parent = tmp_path / f"src{len(created)}"
        parent.mkdir()
        db = parent / f"{stem}.lancedb"
        db.mkdir()
        (db / "data.bin").write_bytes(b"\x00")
        table = pa.table(
            {
                "id": ["settings"],
                "settings": [json.dumps({"version": version})],
            }
        )
        lance.write_dataset(table, str(db / "settings.lance"))
        created.append(db)
        return db

    return _make


@pytest.fixture
def fake_config(tmp_path):
    """A minimal ``haiku.rag.yaml`` config file."""
    config = tmp_path / "haiku.rag.yaml"
    config.write_text("storage:\n  data_dir: /tmp\n", encoding="utf-8")
    return config


@pytest.fixture
def output_dir(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    return out


@pytest.fixture
def make_project(tmp_path):
    """Factory for a project dir

    with an optional ``.venv`` and/or pyproject pin.
    """
    created: list[Path] = []

    def _make(
        *, venv_version: str | None = None, pyproject_req: str | None = None
    ):
        root = tmp_path / f"proj{len(created)}"
        root.mkdir()
        created.append(root)
        if venv_version is not None:
            site_packages = (
                root / ".venv" / "lib" / "python3.13" / "site-packages"
            )
            site_packages.mkdir(parents=True)
            (
                site_packages / f"haiku_rag_slim-{venv_version}.dist-info"
            ).mkdir()
        if pyproject_req is not None:
            (root / "pyproject.toml").write_text(
                '[project]\nname = "x"\nversion = "0"\n'
                f'dependencies = ["{pyproject_req}"]\n',
                encoding="utf-8",
            )
        return root

    return _make


# -- db_stem ----------------------------------------------------------------


@pytest.mark.parametrize(
    "name, expectation",
    [
        ("handbook.lancedb", contextlib.nullcontext("handbook")),
        ("acme-docs.lancedb", contextlib.nullcontext("acme-docs")),
        ("a.b.lancedb", contextlib.nullcontext("a.b")),
        ("not-a-lance-db", pytest.raises(generator.NotALanceDB)),
    ],
)
def test_db_stem_strips_lancedb_suffix(tmp_path, name, expectation):
    db_path = tmp_path / name

    with expectation as expected:
        stem = generator.db_stem(db_path)

    if not isinstance(expected, pytest.ExceptionInfo):
        assert stem == expected


# -- sniff_db_version -------------------------------------------------------


def test_sniff_db_version_returns_none_without_settings(fake_db):
    version = generator.sniff_db_version(fake_db)

    assert version is None


def test_sniff_db_version_returns_none_with_empty_settings(
    make_unversioned_db,
):
    db = make_unversioned_db("handbook")

    version = generator.sniff_db_version(db)

    assert version is None


def test_sniff_db_version_reads_stored_version(make_versioned_db):
    db = make_versioned_db("handbook", "0.40.0")

    version = generator.sniff_db_version(db)

    assert version == "0.40.0"


# -- _venv_site_packages ----------------------------------------------------


@pytest.mark.parametrize(
    "linux_versions, w_windows_version",
    [
        ((), False),
        ((), True),
        (["3.1415926"], False),
        (["3.1415926", "2.7182818"], False),
        (["3.1415926", "2.7182818"], True),
    ],
)
def test_venv_site_packages(
    tmp_path,
    linux_versions,
    w_windows_version,
):
    expected = []

    for l_version in sorted(linux_versions):
        l_venv = tmp_path / "lib" / f"python{l_version}" / "site-packages"
        l_venv.mkdir(parents=True)
        expected.append(l_venv)

    if w_windows_version:
        w_venv = tmp_path / "Lib" / "site-packages"
        w_venv.mkdir(parents=True)
        expected.append(w_venv)

    found = list(generator._venv_site_packages(tmp_path))

    assert found == expected


# -- _version_from_venv -----------------------------------------------------
def test_version_from_venv_no_site_packages(monkeypatch):
    monkeypatch.setattr(generator, "_venv_site_packages", lambda x: ())

    version = generator._version_from_venv(".venv", "test-pkg")

    assert version is None


def test_version_from_venv_w_site_packages_glob_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(generator, "_venv_site_packages", lambda x: [tmp_path])

    version = generator._version_from_venv(".venv", "test-pkg")

    assert version is None


def test_version_from_venv_w_site_packages_glob_hit(tmp_path, monkeypatch):
    (tmp_path / "test_pkg-1.2.3.dist-info").mkdir()
    monkeypatch.setattr(generator, "_venv_site_packages", lambda x: [tmp_path])

    version = generator._version_from_venv(".venv", "test-pkg")

    assert version == "1.2.3"


# -- _version_from_uv_tree --------------------------------------------------
def test_version_from_uv_tree_returns_none_on_no_uv(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        generator.subprocess,
        "run",
        lambda *a, **k: pytest.fail("no uv, no subprocess"),
    )

    version = generator._version_from_uv_tree(tmp_path, "test-pkg")

    assert version is None


def test_version_from_uv_tree_returns_none_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(
        generator.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 1, stdout="", stderr="x"
        ),
    )

    version = generator._version_from_uv_tree(tmp_path, "test-pkg")

    assert version is None


def test_version_from_uv_tree_returns_none_on_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(
        generator.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, stdout="not-test-pkg", stderr=""
        ),
    )

    version = generator._version_from_uv_tree(tmp_path, "test-pkg")

    assert version is None


def test_version_from_uv_tree_parses_resolved_version(tmp_path, monkeypatch):
    monkeypatch.setattr(generator.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(
        generator.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, stdout="test-pkg v0.51.0\n", stderr=""
        ),
    )

    version = generator._version_from_uv_tree(tmp_path, "test-pkg")

    assert version == "0.51.0"


# -- version_from_project ---------------------------------------------------
def test_version_from_project_errors_when_path_missing(tmp_path):
    missing = tmp_path / "nope"

    with pytest.raises(generator.ProjectDoesNotExist):
        generator.version_from_project(missing)


def test_version_from_project_reads_venv_dist_info(make_project):
    project = make_project(venv_version="0.49.2")

    version = generator.version_from_project(project)

    assert version == "0.49.2"


def test_version_from_project_version_from_venv_none_pyprj(
    monkeypatch,
    make_project,
):
    project = make_project(
        venv_version="0.49.2",
        pyproject_req="haiku-rag-slim>=0.48.1",
    )
    monkeypatch.setattr(
        generator,
        "_version_from_venv",
        lambda d, dist: None,
    )
    monkeypatch.setattr(
        generator, "_version_from_uv_tree", lambda d, dist: "0.50.0"
    )

    version = generator.version_from_project(project)

    assert version == "0.50.0"


def test_version_from_project_resolves_pyproject_via_uv_tree(
    make_project, monkeypatch
):
    project = make_project(pyproject_req="haiku-rag-slim>=0.48")
    monkeypatch.setattr(
        generator, "_version_from_uv_tree", lambda d, dist: "0.50.0"
    )

    version = generator.version_from_project(project)

    assert version == "0.50.0"


def test_version_from_project_prefers_venv_over_uv_tree(
    make_project, monkeypatch
):
    project = make_project(
        venv_version="0.49.2", pyproject_req="haiku-rag-slim==0.50.0"
    )
    monkeypatch.setattr(
        generator,
        "_version_from_uv_tree",
        lambda d, dist: pytest.fail(
            "uv tree should not run when .venv resolves"
        ),
    )

    version = generator.version_from_project(project)

    assert version == "0.49.2"


def test_version_from_project_errors_no_venv_version_from_uv_none(
    monkeypatch,
    make_project,
):
    project = make_project(pyproject_req="haiku-rag-slim>=0.48")
    monkeypatch.setattr(
        generator,
        "_version_from_uv_tree",
        lambda d, dist: None,
    )

    with pytest.raises(generator.UnknownProjectVersion):
        generator.version_from_project(project)


def test_version_from_project_errors_no_venv_no_pyproject(
    monkeypatch,
    make_project,
):
    project = make_project()

    with pytest.raises(generator.UnknownProjectVersion):
        generator.version_from_project(project)


# -- validate_inputs --------------------------------------------------------
no_raise = contextlib.nullcontext()
raises_no_db = pytest.raises(generator.RAG_DatabaseDoesNotExist)
raises_db_not_dir = pytest.raises(generator.RAG_DatabaseIsNotADirectory)
raises_no_config = pytest.raises(generator.ConfigDoesNotExist)
raises_config_not_file = pytest.raises(generator.ConfigIsNotAFile)
raises_target_exists = pytest.raises(generator.TargetDirectoryExists)


@pytest.mark.parametrize(
    "db_path_str, config_path_str, target_str, expectation",
    [
        ("nonesuch", "nonesuch", "nonesuch", raises_no_db),
        ("db_exists.txt", "nonesuch", "nonesuch", raises_db_not_dir),
        ("db_exists", "nonesuch", "nonesuch", raises_no_config),
        ("db_exists", "config_dir", "nonesuch", raises_config_not_file),
        ("db_exists", "config.yaml", "target.txt", raises_target_exists),
        ("db_exists", "config.yaml", "nonesuch", no_raise),
    ],
)
def test_validate_inputs(
    fake_db,
    fake_config,
    tmp_path,
    db_path_str,
    config_path_str,
    target_str,
    expectation,
):
    db_path = tmp_path / db_path_str
    if db_path_str != "nonesuch":
        if db_path_str.endswith(".txt"):
            db_path.write_text("bogus")
        else:
            db_path.mkdir()

    config_path = tmp_path / config_path_str
    if config_path_str != "nonesuch":
        if config_path_str.endswith(".yaml"):
            config_path.write_text("id: test")
        else:
            config_path.mkdir()

    target = tmp_path / target_str
    if target_str != "nonesuch":
        target.write_text("bad target")

    with expectation as _expected:
        generator.validate_inputs(db_path, config_path, target)


# -- migrate_database -------------------------------------------------------
def test_migrate_database_no_uv(tmp_path, monkeypatch, fake_db, fake_config):
    requirement = "haiku-rag-slim==0.51.0"
    monkeypatch.setattr(generator.shutil, "which", lambda x: None)
    monkeypatch.setattr(
        generator.subprocess,
        "run",
        lambda *a, **k: pytest.fail("no uv, no subprocess"),
    )

    with pytest.raises(generator.RAG_DatabaseMigrationRequires_UV):
        generator.migrate_database(
            fake_db,
            fake_config,
            requirement,
        )


def test_migrate_database_cmd_errors(
    tmp_path,
    monkeypatch,
    fake_db,
    fake_config,
):
    requirement = "haiku-rag-slim==0.51.0"
    monkeypatch.setattr(generator.shutil, "which", lambda name: "/usr/bin/uv")
    faux_subp_run = mock.Mock(
        spec_set=(),
        return_value=subprocess.CompletedProcess(
            "/usr/bin/uv", 1, stdout="haiku-rag-slim v0.51.0\n", stderr=""
        ),
    )
    monkeypatch.setattr(generator.subprocess, "run", faux_subp_run)

    with pytest.raises(generator.Failed_RAG_DatabaseMigration):
        generator.migrate_database(
            fake_db,
            fake_config,
            requirement,
        )

    faux_subp_run.assert_called_once_with(
        [
            "/usr/bin/uv",
            "tool",
            "run",
            "--from",
            requirement,
            "haiku-rag",
            "--config",
            str(fake_config),
            "migrate",
            "--db",
            str(fake_db),
        ]
    )


def test_migrate_database_success(tmp_path, monkeypatch, fake_db, fake_config):
    requirement = "haiku-rag-slim==0.51.0"
    faux_subp_run = mock.Mock(
        spec_set=(),
        return_value=subprocess.CompletedProcess(
            "/usr/bin/uv", 0, stdout="haiku-rag-slim v0.51.0\n", stderr=""
        ),
    )
    monkeypatch.setattr(generator.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(generator.subprocess, "run", faux_subp_run)

    generator.migrate_database(
        fake_db,
        fake_config,
        requirement,
    )

    faux_subp_run.assert_called_once_with(
        [
            "/usr/bin/uv",
            "tool",
            "run",
            "--from",
            requirement,
            "haiku-rag",
            "--config",
            str(fake_config),
            "migrate",
            "--db",
            str(fake_db),
        ]
    )


# -- generate_skill ---------------------------------------------------------


def test_generate_skill_creates_named_skill_dir(
    fake_db, fake_config, output_dir
):
    target = generator.generate_skill(
        fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )

    assert target == output_dir / "handbook-haiku-rag"
    assert target.is_dir()


def test_generate_skill_substitutes_placeholder_in_skill_md(
    fake_db, fake_config, output_dir
):
    target = generator.generate_skill(
        fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )

    skill_md = (target / "SKILL.md").read_text(encoding="utf-8")
    assert "[dbname]" not in skill_md
    assert "name: handbook-haiku-rag" in skill_md


def test_generate_skill_substitutes_db_stem_in_wrapper(
    fake_db, fake_config, output_dir
):
    target = generator.generate_skill(
        fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )

    wrapper = (target / "scripts" / "haiku_rag.py").read_text(encoding="utf-8")
    assert 'DB_STEM = "handbook"' in wrapper
    assert "[dbname]" not in wrapper


def test_generate_skill_pins_forced_version_in_wrapper(
    fake_db, fake_config, output_dir
):
    target = generator.generate_skill(
        fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )

    wrapper = (target / "scripts" / "haiku_rag.py").read_text(encoding="utf-8")
    assert 'dependencies = ["haiku-rag-slim==0.48.1"]' in wrapper
    assert "[hr_requirement]" not in wrapper


def test_generate_skill_pins_sniffed_version_by_default(
    make_versioned_db, fake_config, output_dir
):
    db = make_versioned_db("handbook", "0.48.1")

    target = generator.generate_skill(db, fake_config, output_dir)

    wrapper = (target / "scripts" / "haiku_rag.py").read_text(encoding="utf-8")
    assert 'dependencies = ["haiku-rag-slim==0.48.1"]' in wrapper


def test_generate_skill_migrates_embedded_copy_when_forcing_newer(
    make_versioned_db, fake_config, output_dir, monkeypatch
):
    db = make_versioned_db("handbook", "0.40.0")
    calls = []
    monkeypatch.setattr(
        generator,
        "migrate_database",
        lambda db_path, cfg, req: calls.append((db_path, req)),
    )

    target = generator.generate_skill(
        db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )

    embedded_db = target / "assets" / "handbook.lancedb"
    assert calls == [(embedded_db, "haiku-rag-slim==0.48.1")]


def test_generate_skill_does_not_migrate_when_version_matches(
    make_versioned_db, fake_config, output_dir, monkeypatch
):
    db = make_versioned_db("handbook", "0.48.1")
    calls = []
    monkeypatch.setattr(
        generator,
        "migrate_database",
        lambda db_path, cfg, req: calls.append(req),
    )

    generator.generate_skill(
        db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )

    assert calls == []


def test_generate_skill_rejects_downgrade(
    make_versioned_db, fake_config, output_dir
):
    db = make_versioned_db("handbook", "0.50.0")

    with pytest.raises(generator.GenerateError, match="downgrade"):
        generator.generate_skill(
            db, fake_config, output_dir, haiku_rag_version="0.49.0"
        )


def test_generate_skill_rejects_sniffed_below_minimum(
    make_versioned_db, fake_config, output_dir
):
    db = make_versioned_db("handbook", "0.40.0")

    with pytest.raises(generator.GenerateError, match="minimum"):
        generator.generate_skill(db, fake_config, output_dir)


def test_generate_skill_rejects_forced_below_minimum(
    fake_db, fake_config, output_dir
):
    with pytest.raises(generator.GenerateError, match="minimum"):
        generator.generate_skill(
            fake_db, fake_config, output_dir, haiku_rag_version="0.40.0"
        )


def test_generate_skill_errors_when_version_undeterminable(
    fake_db, fake_config, output_dir
):
    with pytest.raises(generator.GenerateError, match="could not determine"):
        generator.generate_skill(fake_db, fake_config, output_dir)


def test_generate_skill_embeds_database_under_assets(
    fake_db, fake_config, output_dir
):
    target = generator.generate_skill(
        fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )

    embedded_db = target / "assets" / "handbook.lancedb"
    assert embedded_db.is_dir()
    assert (embedded_db / "data.bin").read_bytes() == b"\x00\x01\x02"


def test_generate_skill_embeds_config_under_assets(
    fake_db, fake_config, output_dir
):
    target = generator.generate_skill(
        fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )

    embedded_config = target / "assets" / "haiku.rag.yaml"
    assert embedded_config.is_file()
    assert embedded_config.read_text(
        encoding="utf-8"
    ) == fake_config.read_text(encoding="utf-8")


def test_generate_skill_omits_pycache(fake_db, fake_config, output_dir):
    stray = generator.TEMPLATE_SKILL / "scripts" / "__pycache__"
    stray.mkdir(parents=True, exist_ok=True)
    (stray / "haiku_rag.cpython-313.pyc").write_bytes(b"\x00")

    try:
        target = generator.generate_skill(
            fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
        )
    finally:
        shutil.rmtree(stray)

    assert not list(target.rglob("__pycache__"))
    assert not list(target.rglob("*.pyc"))


def test_generate_skill_rejects_missing_db(fake_config, output_dir, tmp_path):
    missing_db = tmp_path / "missing.lancedb"

    with pytest.raises(generator.GenerateError, match="does not exist"):
        generator.generate_skill(
            missing_db, fake_config, output_dir, haiku_rag_version="0.48.1"
        )


def test_generate_skill_rejects_missing_config(fake_db, output_dir, tmp_path):
    missing_config = tmp_path / "absent.yaml"

    with pytest.raises(generator.GenerateError, match="config does not exist"):
        generator.generate_skill(
            fake_db, missing_config, output_dir, haiku_rag_version="0.48.1"
        )


def test_generate_skill_rejects_existing_target(
    fake_db, fake_config, output_dir
):
    (output_dir / "handbook-haiku-rag").mkdir()

    with pytest.raises(generator.GenerateError, match="already exists"):
        generator.generate_skill(
            fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
        )


def _load_wrapper(target: Path, name: str):
    """Import generated skill's ``scripts/haiku_rag.py`` as a fresh module"""
    wrapper_path = target / "scripts" / "haiku_rag.py"
    spec = importlib.util.spec_from_file_location(name, wrapper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_wrapper_has_version_mismatch_guard(
    fake_db, fake_config, output_dir
):
    target = generator.generate_skill(
        fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )

    wrapper = (target / "scripts" / "haiku_rag.py").read_text(encoding="utf-8")
    assert "class SkillError(Exception)" in wrapper
    assert "async def open_kb(" in wrapper
    assert "MigrationRequiredError" in wrapper
    assert "runtime haiku-rag version" in wrapper


def test_generated_wrapper_reports_version_mismatch(
    fake_db, fake_config, output_dir, monkeypatch, capsys
):
    import haiku.rag.client as client_mod
    from haiku.rag.store.exceptions import MigrationRequiredError

    target = generator.generate_skill(
        fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )
    wrapper = _load_wrapper(target, "wrapper_mismatch")
    monkeypatch.setattr(wrapper, "load_config", lambda config_path: None)
    monkeypatch.setattr(
        wrapper, "asset_paths", lambda *a, **k: (Path("db"), Path("cfg"))
    )

    class _RaisingHaikuRAG:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            raise MigrationRequiredError("test")

        async def __aexit__(self, *exc): ...  # '__aenter__' raises

    monkeypatch.setattr(client_mod, "HaikuRAG", _RaisingHaikuRAG)

    status = wrapper.main(["search", "anything"])

    captured = capsys.readouterr()
    assert status == 1
    assert "version mismatch" in captured.err
    assert "--haiku-rag-version" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


# -- build_parser -----------------------------------------------------------


def test_haiku_rag_version_help_mentions_backend():
    parser = generator.build_parser()

    help_text = parser.format_help().lower()
    assert "backend" in help_text
    assert "soliplex" in help_text


def test_version_args_are_mutually_exclusive():
    parser = generator.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--config",
                "c",
                "--db",
                "d",
                "--haiku-rag-version",
                "0.48.1",
                "--version-from-project",
                "/tmp/x",
            ]
        )


# -- validate_forced_version ------------------------------------------------


def test_validate_forced_version_accepts_valid():
    result = generator.validate_forced_version("0.51.0")

    assert result is None


def test_validate_forced_version_rejects_invalid():
    with pytest.raises(generator.InvalidForcedVersion):
        generator.validate_forced_version("weird-tag")


def test_validate_forced_version_rejects_below_minimum():
    with pytest.raises(generator.Haiku_RAG_VersionTooOld):
        generator.validate_forced_version("0.40.0")


# -- main -------------------------------------------------------------------


def test_main_version_from_project_forces_discovered_version(
    fake_db, fake_config, output_dir, make_project, capsys
):
    project = make_project(venv_version="0.49.2")

    status = generator.main(
        [
            "--config",
            str(fake_config),
            "--db",
            str(fake_db),
            "--output",
            str(output_dir),
            "--version-from-project",
            str(project),
        ]
    )

    assert status == 0
    wrapper = (
        output_dir / "handbook-haiku-rag" / "scripts" / "haiku_rag.py"
    ).read_text(encoding="utf-8")
    assert 'dependencies = ["haiku-rag-slim==0.49.2"]' in wrapper


def test_main_version_from_project_raises(
    fake_db, fake_config, output_dir, make_project, capsys
):
    project = make_project(venv_version="0.22.2")

    status = generator.main(
        [
            "--config",
            str(fake_config),
            "--db",
            str(fake_db),
            "--output",
            str(output_dir),
            "--version-from-project",
            str(project),
        ]
    )

    assert status == 1


def test_main_haiku_rag_version(fake_db, fake_config, output_dir, capsys):
    status = generator.main(
        [
            "--config",
            str(fake_config),
            "--db",
            str(fake_db),
            "--output",
            str(output_dir),
            "--haiku-rag-version",
            "0.51.3",
        ]
    )

    assert status == 0


def test_main_rejects_invalid_forced_version(
    fake_db, fake_config, output_dir, capsys
):
    status = generator.main(
        [
            "--config",
            str(fake_config),
            "--db",
            str(fake_db),
            "--output",
            str(output_dir),
            "--haiku-rag-version",
            "weird-tag",
        ]
    )

    assert status == 1
    assert "not a valid version" in capsys.readouterr().err


def test_main_sniffs_db_version_without_options(
    make_versioned_db, fake_config, output_dir, capsys
):
    db = make_versioned_db("handbook", "0.51.0")

    status = generator.main(
        [
            "--config",
            str(fake_config),
            "--db",
            str(db),
            "--output",
            str(output_dir),
        ]
    )

    assert status == 0
