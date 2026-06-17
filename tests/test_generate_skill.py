import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

# Load the PEP 723 generator script as a module (it lives in scripts/, which is
# not an importable package).
_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "generate_skill.py"
_spec = importlib.util.spec_from_file_location("generate_skill", _SCRIPT)
generate_skill = importlib.util.module_from_spec(_spec)
sys.modules["generate_skill"] = generate_skill
_spec.loader.exec_module(generate_skill)


@pytest.fixture
def fake_db(tmp_path):
    """A stand-in ``handbook.lancedb`` directory with no version metadata."""
    db = tmp_path / "handbook.lancedb"
    db.mkdir()
    (db / "data.bin").write_bytes(b"\x00\x01\x02")
    return db


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


@pytest.mark.parametrize(
    "name, expected",
    [
        ("handbook.lancedb", "handbook"),
        ("acme-docs.lancedb", "acme-docs"),
        ("a.b.lancedb", "a.b"),
    ],
)
def test_db_stem_strips_lancedb_suffix(tmp_path, name, expected):
    db_path = tmp_path / name

    stem = generate_skill.db_stem(db_path)

    assert stem == expected


def test_sniff_db_version_returns_none_without_settings(fake_db):
    version = generate_skill.sniff_db_version(fake_db)

    assert version is None


def test_sniff_db_version_reads_stored_version(make_versioned_db):
    db = make_versioned_db("handbook", "0.40.0")

    version = generate_skill.sniff_db_version(db)

    assert version == "0.40.0"


def test_generate_skill_creates_named_skill_dir(
    fake_db, fake_config, output_dir
):
    target = generate_skill.generate_skill(
        fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )

    assert target == output_dir / "handbook-haiku-rag"
    assert target.is_dir()


def test_generate_skill_substitutes_placeholder_in_skill_md(
    fake_db, fake_config, output_dir
):
    target = generate_skill.generate_skill(
        fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )

    skill_md = (target / "SKILL.md").read_text(encoding="utf-8")
    assert "[dbname]" not in skill_md
    assert "name: handbook-haiku-rag" in skill_md


def test_generate_skill_substitutes_db_stem_in_wrapper(
    fake_db, fake_config, output_dir
):
    target = generate_skill.generate_skill(
        fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )

    wrapper = (target / "scripts" / "haiku_rag.py").read_text(encoding="utf-8")
    assert 'DB_STEM = "handbook"' in wrapper
    assert "[dbname]" not in wrapper


def test_generate_skill_pins_forced_version_in_wrapper(
    fake_db, fake_config, output_dir
):
    target = generate_skill.generate_skill(
        fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )

    wrapper = (target / "scripts" / "haiku_rag.py").read_text(encoding="utf-8")
    assert 'dependencies = ["haiku-rag-slim==0.48.1"]' in wrapper
    assert "[hr_requirement]" not in wrapper


def test_generate_skill_pins_custom_package_name(
    fake_db, fake_config, output_dir
):
    target = generate_skill.generate_skill(
        fake_db,
        fake_config,
        output_dir,
        haiku_rag_version="0.48.1",
        package_name="haiku-rag",
    )

    wrapper = (target / "scripts" / "haiku_rag.py").read_text(encoding="utf-8")
    assert 'dependencies = ["haiku-rag==0.48.1"]' in wrapper


def test_generate_skill_pins_sniffed_version_by_default(
    make_versioned_db, fake_config, output_dir
):
    db = make_versioned_db("handbook", "0.48.1")

    target = generate_skill.generate_skill(db, fake_config, output_dir)

    wrapper = (target / "scripts" / "haiku_rag.py").read_text(encoding="utf-8")
    assert 'dependencies = ["haiku-rag-slim==0.48.1"]' in wrapper


def test_generate_skill_migrates_embedded_copy_when_forcing_newer(
    make_versioned_db, fake_config, output_dir, monkeypatch
):
    db = make_versioned_db("handbook", "0.40.0")
    calls = []
    monkeypatch.setattr(
        generate_skill,
        "migrate_database",
        lambda db_path, cfg, req: calls.append((db_path, req)),
    )

    target = generate_skill.generate_skill(
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
        generate_skill,
        "migrate_database",
        lambda db_path, cfg, req: calls.append(req),
    )

    generate_skill.generate_skill(
        db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )

    assert calls == []


def test_generate_skill_rejects_downgrade(
    make_versioned_db, fake_config, output_dir
):
    db = make_versioned_db("handbook", "0.50.0")

    with pytest.raises(generate_skill.GenerateError, match="downgrade"):
        generate_skill.generate_skill(
            db, fake_config, output_dir, haiku_rag_version="0.49.0"
        )


def test_generate_skill_rejects_sniffed_below_minimum(
    make_versioned_db, fake_config, output_dir
):
    db = make_versioned_db("handbook", "0.40.0")

    with pytest.raises(generate_skill.GenerateError, match="minimum"):
        generate_skill.generate_skill(db, fake_config, output_dir)


def test_generate_skill_rejects_forced_below_minimum(
    fake_db, fake_config, output_dir
):
    with pytest.raises(generate_skill.GenerateError, match="minimum"):
        generate_skill.generate_skill(
            fake_db, fake_config, output_dir, haiku_rag_version="0.40.0"
        )


def test_generate_skill_errors_when_version_undeterminable(
    fake_db, fake_config, output_dir
):
    with pytest.raises(
        generate_skill.GenerateError, match="could not determine"
    ):
        generate_skill.generate_skill(fake_db, fake_config, output_dir)


def test_generate_skill_embeds_database_under_assets(
    fake_db, fake_config, output_dir
):
    target = generate_skill.generate_skill(
        fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )

    embedded_db = target / "assets" / "handbook.lancedb"
    assert embedded_db.is_dir()
    assert (embedded_db / "data.bin").read_bytes() == b"\x00\x01\x02"


def test_generate_skill_embeds_config_under_assets(
    fake_db, fake_config, output_dir
):
    target = generate_skill.generate_skill(
        fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )

    embedded_config = target / "assets" / "haiku.rag.yaml"
    assert embedded_config.is_file()
    assert embedded_config.read_text(encoding="utf-8") == fake_config.read_text(
        encoding="utf-8"
    )


def test_generate_skill_omits_pycache(fake_db, fake_config, output_dir):
    stray = generate_skill.TEMPLATE_SKILL / "scripts" / "__pycache__"
    stray.mkdir(parents=True, exist_ok=True)
    (stray / "haiku_rag.cpython-313.pyc").write_bytes(b"\x00")

    try:
        target = generate_skill.generate_skill(
            fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
        )
    finally:
        shutil.rmtree(stray)

    assert not list(target.rglob("__pycache__"))
    assert not list(target.rglob("*.pyc"))


def test_generate_skill_rejects_db_without_lancedb_suffix(
    fake_config, output_dir, tmp_path
):
    bad_db = tmp_path / "handbook"
    bad_db.mkdir()

    with pytest.raises(generate_skill.GenerateError, match="lancedb"):
        generate_skill.generate_skill(
            bad_db, fake_config, output_dir, haiku_rag_version="0.48.1"
        )


def test_generate_skill_rejects_missing_db(fake_config, output_dir, tmp_path):
    missing_db = tmp_path / "missing.lancedb"

    with pytest.raises(generate_skill.GenerateError, match="does not exist"):
        generate_skill.generate_skill(
            missing_db, fake_config, output_dir, haiku_rag_version="0.48.1"
        )


def test_generate_skill_rejects_missing_config(fake_db, output_dir, tmp_path):
    missing_config = tmp_path / "absent.yaml"

    with pytest.raises(generate_skill.GenerateError, match="config does not exist"):
        generate_skill.generate_skill(
            fake_db, missing_config, output_dir, haiku_rag_version="0.48.1"
        )


def test_generate_skill_rejects_existing_target(
    fake_db, fake_config, output_dir
):
    (output_dir / "handbook-haiku-rag").mkdir()

    with pytest.raises(generate_skill.GenerateError, match="already exists"):
        generate_skill.generate_skill(
            fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
        )


def _load_wrapper(target: Path, name: str):
    """Import a generated skill's ``scripts/haiku_rag.py`` as a fresh module."""
    wrapper_path = target / "scripts" / "haiku_rag.py"
    spec = importlib.util.spec_from_file_location(name, wrapper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_wrapper_has_version_mismatch_guard(
    fake_db, fake_config, output_dir
):
    target = generate_skill.generate_skill(
        fake_db, fake_config, output_dir, haiku_rag_version="0.48.1"
    )

    wrapper = (target / "scripts" / "haiku_rag.py").read_text(encoding="utf-8")
    assert "class SkillError(Exception)" in wrapper
    assert "async def open_kb(" in wrapper
    assert "MigrationRequiredError" in wrapper
    assert "version mismatch" in wrapper


def test_generated_wrapper_reports_version_mismatch(
    fake_db, fake_config, output_dir, monkeypatch, capsys
):
    import haiku.rag.client as client_mod
    from haiku.rag.store.exceptions import MigrationRequiredError

    target = generate_skill.generate_skill(
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
            raise MigrationRequiredError(
                "Database requires migration from 0.48.1 to 0.51.0."
            )

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(client_mod, "HaikuRAG", _RaisingHaikuRAG)

    status = wrapper.main(["search", "anything"])

    captured = capsys.readouterr()
    assert status == 1
    assert "version mismatch" in captured.err
    assert "--haiku-rag-version" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_haiku_rag_version_help_mentions_backend():
    parser = generate_skill.build_parser()

    help_text = parser.format_help().lower()
    assert "backend" in help_text
    assert "soliplex" in help_text
