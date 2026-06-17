# `ragpicker`

Generate a self-contained, **filesystem-based** [agentskills.io](https://agentskills.io)
skill that wraps a [`haiku-rag`](https://github.com/ggozad/haiku.rag)-built LanceDB
RAG database.

Unlike `haiku-rag create-skill` (which emits a `haiku-skills` *entrypoint* package
consumed by haiku-rag's own pydantic-ai agent), the skill produced here is driven by
an **external** agent (e.g. Claude Code) that invokes a script. The generated skill
provides a PEP 723 wrapper, `scripts/haiku_rag.py`, exposing `search` and `cite`
subcommands implemented over the `haiku-rag` Python client — there is no `haiku-rag
cite` CLI subcommand to shell out to.

## Generate a skill

```bash
uv run ragpicker \
    --config path/to/haiku.rag.yaml \
    --db path/to/handbook.lancedb \
    --output ./skills \
    [--haiku-rag-version 0.48.1 | --version-from-project path/to/stack]
```

- The skill is named from the database directory stem: `handbook.lancedb` →
  `handbook-haiku-rag`.
- The template under `src/ragpicker/template/[dbname]-haiku-rag/` (bundled as
  package data) is copied with `[dbname]` substituted by the stem (in path names
  and text-file contents).
- Both the `.lancedb` database and the `haiku.rag.yaml` config are embedded under the
  skill's `assets/`.

### Version pinning

The wrapper pins an exact `haiku-rag-slim` version so the embedded database and the
runtime always agree. (Only `haiku-rag-slim` is supported — the full `haiku-rag` build's
dependency weight is prohibitive for a self-contained skill.)

- **Default — sniff:** the version is read from the database itself (the version that
  last wrote it, stored in its `settings` table) and pinned as-is. No migration is
  needed because the database already matches.
- **Minimum version:** the wrapper's `search`/`cite` rely on APIs whose surface is not
  guaranteed below `0.48.1`, so generation **refuses** to pin anything older. A database
  below that floor must be force-migrated up (see below).
- **`--haiku-rag-version X` — force:** `X` is pinned in the wrapper **and** the embedded
  copy of the database is migrated up to `X` (via `uv tool run --from
  haiku-rag-slim==X haiku-rag migrate`). The original database is left untouched.
  Downgrades are rejected (haiku-rag migrations only move forward).
- **`--version-from-project PATH` — discover + force:** read the effective
  `haiku-rag-slim` version from a target project (e.g. a Soliplex stack) and force it the
  same way. Looks first in the project's `.venv` (the *installed* version), then falls
  back to resolving the project's dependencies with `uv tree --package haiku-rag-slim
  --depth 0`. Either path finds `haiku-rag-slim` even though it is a *transitive*
  dependency of `soliplex` (not named directly in `pyproject.toml`). Mutually exclusive
  with `--haiku-rag-version`.

Forcing requires `uv` on `PATH` and network access to fetch the requested version; most
migrations are schema-only, but some may need model access.

## Using a generated skill

```bash
cd handbook-haiku-rag
uv run scripts/haiku_rag.py search "<query>" [--limit N] [--filter "<SQL>"]
uv run scripts/haiku_rag.py cite <chunk_id> [<chunk_id> ...]
```

`uv` resolves the wrapper's pinned `haiku-rag` dependency on first use. The wrapper
opens the bundled database **read-only**, so the embedded database and the pinned
version always agree by construction (the generator either sniffs or migrates to match).

### Embedding hosts (e.g. Soliplex)

There are two execution models, with different requirements:

- **Standalone (`uv run`)** — the model above. `uv` honors the wrapper's PEP 723 pin,
  so the runtime always matches the embedded database.
- **Embedding host (`sys.executable`)** — a host such as Soliplex runs the skill
  script with **its own Python interpreter, not `uv`** (e.g. `haiku.skills` uses
  `SCRIPT_RUNNERS[".py"] = (sys.executable,)`). The wrapper's pin is then *ignored*:
  the host's installed `haiku-rag` opens the database. The embedded database must
  match **that** version, not the wrapper's pin.

So when targeting such a host, generate with `--haiku-rag-version <the backend's
haiku-rag version>` so the embedded database is migrated to match the interpreter that
will open it. On a version mismatch the wrapper now exits non-zero with an actionable
`haiku-rag version mismatch` message (instead of failing silently and returning nothing
to the agent).

## Develop

```bash
uv run --with pytest --with pylance --with packaging pytest tests/
```
