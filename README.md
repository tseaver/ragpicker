# `gen-hr-filesystem-skill`

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
uv run scripts/generate_skill.py \
    --config path/to/haiku.rag.yaml \
    --db path/to/handbook.lancedb \
    --output ./skills \
    [--haiku-rag-version 0.48.1] \
    [--package-name haiku-rag-slim]
```

- The skill is named from the database directory stem: `handbook.lancedb` →
  `handbook-haiku-rag`.
- The template under `template/[dbname]-haiku-rag/` is copied with `[dbname]`
  substituted by the stem (in path names and text-file contents).
- Both the `.lancedb` database and the `haiku.rag.yaml` config are embedded under the
  skill's `assets/`.

### Version pinning

The wrapper pins an exact `haiku-rag` version so the embedded database and the runtime
always agree:

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
- **`--package-name`** selects the distribution to pin (default `haiku-rag-slim`; use
  `haiku-rag` for the full build).

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

## Develop

```bash
uv run --with pytest --with pylance --with packaging pytest tests/
```
