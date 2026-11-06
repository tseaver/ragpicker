# Ingesting documents

`ragpicker` wraps an existing database; it converts, chunks and embeds
nothing itself. This page covers producing that database — the ingestion
side of `haiku.rag` — before handing it to the generator. It targets
haiku.rag **0.109.0** (current stable); younger features it relies on are
marked with the release that added them.

## One-shot: `haiku-rag add-src`

```bash
haiku-rag init --db handbook.lancedb
haiku-rag add-src /srv/handbook-src/
haiku-rag search "attention mechanism"
```

- `add-src` takes a file, a URL, a directory (walked recursively, ingesting
  every file whose extension docling or the text reader supports), or a
  `s3://` object (requires the `[s3]` extra) — anything the ingester can
  watch, without setting the ingester up.
- Re-ingesting an unchanged file is cheap: change detection by stored hashes
  skips the conversion/embedding entirely.

This is the whole story for a handbook that changes rarely or never.

## Continuous: `haiku-ingester`

For a corpus that keeps changing, a service watches sources and keeps the
database in step: retries with backoff, a dead-letter queue, a control plane
and a dashboard. Install the `[ingester]` extra (available since 0.50.0; the
`ingester.databases` shape below needs 0.99.0+ — a configuration carrying the
older `ingester.sources` fails to load on current releases):

```bash
pip install 'haiku.rag-slim[ingester]'   # or 'haiku.rag[ingester]'
```

Sources are configured under the database they feed:

```yaml
lancedb:
  databases:
    handbook: ${HAIKU_RAG_CONFIG_DIR}/handbook.lancedb

ingester:
  databases:
    handbook:
      sources:
        - type: fs
          id: handbook
          root: /srv/handbook-src
          delete_orphans: true
```

```bash
haiku-ingester serve          # watch + sweep; control plane at 127.0.0.1:8765
haiku-ingester run-batch      # one sweep + drain, then exit: CI or cron
```

- Source types: `fs`, `http`, `s3`, `webdav`, each with gitignore-style
  include/ignore filters and size limits. A per-source circuit breaker and a
  startup reconciliation between queue and store round out the machinery.
- `delete_orphans: true` drops documents whose file has disappeared from the
  source; without it, stale documents stay searchable.
- `run-batch --dry-run` writes a changeset manifest instead of ingesting;
  `--manifest` replays one frozen; `--retry dead` re-queues failures.
- The control plane requires `ingester.api.auth_token` when it is not bound
  to loopback.

### One writer per database

The ingester owns every database it writes; LanceDB allows exactly one
writing process and any number of read-only consumers. Everything else —
the MCP server, the generated skill — opens it read-only, and ragpicker's
skill embeds a *copy*, so it never contends with the writer.

## Picking and running an embedding model

The embedder is configuration — `embeddings.model` in `haiku.rag.yaml` —
and a plain CPU-only laptop is enough for any of these:

| Option | Shape | Example |
| --- | --- | --- |
| Ollama (default) | local server | `provider: ollama`, `name: qwen3-embedding:4b`, `vector_dim: 2560`; `ollama pull qwen3-embedding:4b` |
| In-process | no server | `provider: sentence-transformers`, `name: all-MiniLM-L6-v2`, `vector_dim: 384` (`cross-encoder` extra, on `haiku.rag`) |
| OpenAI-compatible | localhost server | `provider: openai`, `base_url: http://localhost:1234/v1` (LM Studio, vLLM) |

Rules that outlive the choice:

- `vector_dim` must be the model's true output dimension and is frozen at
  database creation: the database records provider, model name, dimension
  and the optional query/document prefixes, and checks them on every open.
  Changing the model later is a `haiku-rag rebuild`, not a config edit.
- The *URL* is not part of the recorded identity: pointing an existing
  database at the same model on a new host is a config edit alone.
- Hosted embedders (`voyageai`, `cohere`, public APIs) work, but whatever
  runs the skill must reach the embedder at *query* time. Favor a
  localhost model when the skill will travel to hosts you do not control.
- Only the `embeddings` section matters to a generated skill: its
  `search`/`cite` never calls the QA or reranking models.

## Converting with docling-serve

`docling-local`, the default converter, runs docling in-process and pulls
the `docling` extra plus its layout/OCR models on first use — a heavy
dependency for "Markdown in, search out". `docling-serve` offloads
conversion (and chunking) to a REST service, so `haiku.rag-slim` runs
without `docling` at all.

The upstream image runs docling on CPU, and Markdown/HTML corpora need no
OCR in the first place, so a CPU-only localhost deployment is the sweet
spot:

```bash
docker run -p 5001:5001 quay.io/docling-project/docling-serve:v1.38.2
```

```yaml
processing:
  converter: docling-serve
  chunker: docling-serve

providers:
  docling_serve:
    base_url: http://localhost:5001
```

- haiku.rag 0.109.0 is tested against docling-serve **v1.38.2**; pin the
  image tag to that release line.
- `do_ocr: false` (the docling-serve chunker sends it along) skips OCR
  model downloads entirely for corpora whose text is already embedded.
- `docling-local` remains the choice when pictures should come *from the
  web*: `fetch_remote_images` and its header controls exist only there —
  docling-serve stores `picture_data=NULL` for external `<img>` URLs.
- The reference deployment is haiku.rag's `examples/docker/
  docker-compose.yml`, which pairs the slim image
  (`ghcr.io/ggozad/haiku.rag-slim`, built with the ingester extra and
  without `docling`) with docling-serve replicas. Size
  `ingester.workers.worker_count` at 1–2× the number of `base_url`
  entries.

## Checkup

Before handing the database over — and any time it misbehaves — have
haiku.rag examine its own work:

```bash
haiku-rag info --db handbook.lancedb     # snapshot: counts, sizes,
                                         # embedder, pending migrations
haiku-rag doctor --db handbook.lancedb   # pass/warn/fail report
```

`doctor` checks table correspondence, orphaned chunks and references, chunk
vector dimensions, embedder identity agreement, index coverage and pending
migrations, and probes the endpoints the configuration names (Ollama models
present, docling-serve reachable, API keys set). Every finding names its
remedying command (`migrate`, `rebuild`, `create-index`, `vacuum`, ...).
`--json` prints a machine-readable report; `--skip-providers` skips both the
API-key check and network probes. The haiku.rag web app runs the same report
in its doctor page, for browser-only operators.

Details: <https://ggozad.github.io/haiku.rag/cli/> (Database lifecycle:
`info`, `doctor`).

## From database to skill

The skill embeds a **snapshot copy**: generation copies both the
`.lancedb` directory and the `haiku.rag.yaml` config under the skill's
`assets/`, leaving the ingester's database alone.

- Quiesce the writer first: the directory copy is made with ordinary file
  copies, not atomically across tables. Right after a successful
  `run-batch`, or with `serve` briefly stopped, is safe.
- Regeneration is the refresh path: when the ingester has ingested new
  content (or versions have moved), re-run [ragpicker](generating.md) —
  the skill's copy gets replaced wholesale.
- Generate with `--version-from-project` when the skill will run under an
  embedding host such as a Soliplex stack, so the embedded database
  matches the haiku-rag **that interpreter** will open it with (see
  [Generating](generating.md) / [Version pinning](versions.md)).
- Verify the embedded config's `embeddings.model` agrees with the
  database's recorded identity (provider, name, `vector_dim`): a vector
  dimension mismatch refuses to open; a same-dimension mismatch warns and
  degrades retrieval. `haiku-rag doctor` performs the same check.
