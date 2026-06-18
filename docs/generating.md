# Generating a skill

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
- Both the `.lancedb` database and the `haiku.rag.yaml` config are embedded under
  the skill's `assets/`.

## Using a generated skill

```bash
cd handbook-haiku-rag
uv run scripts/haiku_rag.py search "<query>" [--limit N] [--filter "<SQL>"]
uv run scripts/haiku_rag.py cite <chunk_id> [<chunk_id> ...]
```

`uv` resolves the wrapper's pinned `haiku-rag` dependency on first use. The
wrapper opens the bundled database **read-only**, so the embedded database and
the pinned version always agree by construction (the generator either sniffs or
migrates to match).

### Embedding hosts (e.g. Soliplex)

There are two execution models, with different requirements:

- **Standalone (`uv run`)** — `uv` honors the wrapper's PEP 723 pin, so the
  runtime always matches the embedded database.
- **Embedding host (`sys.executable`)** — a host such as Soliplex runs the skill
  script with **its own Python interpreter, not `uv`** (e.g. `haiku.skills` uses
  `SCRIPT_RUNNERS[".py"] = (sys.executable,)`). The wrapper's pin is then
  *ignored*: the host's installed `haiku-rag` opens the database. The embedded
  database must match **that** version, not the wrapper's pin.

So when targeting such a host, generate with `--haiku-rag-version <the backend's
haiku-rag version>` (or `--version-from-project`) so the embedded database is
migrated to match the interpreter that will open it. On a version mismatch the
wrapper exits non-zero with an actionable `haiku-rag version mismatch` message
instead of failing silently.
