# Version pinning

The wrapper pins an exact `haiku-rag-slim` version so the embedded database and
the runtime always agree. (Only `haiku-rag-slim` is supported — the full
`haiku-rag` build's dependency weight is prohibitive for a self-contained skill.)

- **Default — sniff:** the version is read from the database itself (the version
  that last wrote it, stored in its `settings` table) and pinned as-is. No
  migration is needed because the database already matches.
- **Minimum version:** the wrapper's `search`/`cite` rely on APIs whose surface
  is not guaranteed below `0.48.1`, so generation **refuses** to pin anything
  older. A database below that floor must be force-migrated up.
- **`--haiku-rag-version X` — force:** `X` is pinned in the wrapper **and** the
  embedded copy of the database is migrated up to `X` (via `uv tool run --from
  haiku-rag-slim==X haiku-rag migrate`). The original database is left untouched.
  Downgrades are rejected (haiku-rag migrations only move forward).
- **`--version-from-project PATH` — discover + force:** read the effective
  `haiku-rag-slim` version from a target project (e.g. a Soliplex stack) and
  force it the same way. Looks first in the project's `.venv` (the *installed*
  version), then falls back to resolving the project's dependencies with `uv tree
  --package haiku-rag-slim --depth 0`. Either path finds `haiku-rag-slim` even
  though it is a *transitive* dependency of `soliplex` (not named directly in
  `pyproject.toml`). Mutually exclusive with `--haiku-rag-version`.

Forcing requires `uv` on `PATH` and network access to fetch the requested
version; most migrations are schema-only, but some may need model access.
