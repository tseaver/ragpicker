# ragpicker

`ragpicker` generates a self-contained, **filesystem-based**
[agentskills.io](https://agentskills.io) skill that wraps a
[`haiku-rag`](https://github.com/ggozad/haiku.rag)-built LanceDB RAG database.

Unlike `haiku-rag create-skill` (which emits a `haiku-skills` *entrypoint*
package consumed by haiku-rag's own pydantic-ai agent), the skill produced here
is driven by an **external** agent (e.g. Claude Code) that invokes a script. The
generated skill provides a PEP 723 wrapper, `scripts/haiku_rag.py`, exposing
`search` and `cite` subcommands implemented over the `haiku-rag` Python client.

## How it works

Given a `haiku.rag.yaml` config and a `haiku-rag`-built LanceDB database
directory (`<stem>.lancedb`), `ragpicker`:

- names the skill from the database directory stem
  (`handbook.lancedb` → `handbook-haiku-rag`);
- copies the bundled template (shipped as package data under
  `src/ragpicker/template/[dbname]-haiku-rag/`), substituting `[dbname]` with
  the stem in path names and text-file contents;
- embeds both the `.lancedb` database and the `haiku.rag.yaml` config under the
  skill's `assets/`;
- pins an exact `haiku-rag-slim` version in the wrapper so the embedded database
  and the runtime always agree (see [Version pinning](versions.md)).

## Next steps

- **[Generating a skill](generating.md)** — the CLI and what it produces.
- **[Version pinning](versions.md)** — how the embedded database and runtime
  are kept in sync.
