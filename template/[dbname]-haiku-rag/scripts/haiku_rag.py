#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["[hr_requirement]"]
# ///
"""Search and cite the [dbname] knowledge base bundled with this skill.

This is the runnable surface of a filesystem skill that wraps a
``haiku-rag``-built LanceDB database. It re-implements the ``search`` and
``cite`` tools from ``haiku.rag.skills`` over the public ``HaikuRAG`` client,
because ``haiku-rag`` exposes those only as in-agent (pydantic-ai) tools, not
as CLI subcommands.

Usage (run with ``uv``, which resolves the pinned dependency on first use):

    uv run scripts/haiku_rag.py search "<query>" [--limit N] [--filter "<SQL>"]
    uv run scripts/haiku_rag.py cite <chunk_id> [<chunk_id> ...]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# The database stem is substituted by the generator from the source
# ``<stem>.lancedb`` directory name. Both the LanceDB database and the
# embedded ``haiku.rag.yaml`` live in the skill's ``assets/`` directory.
DB_STEM = "[dbname]"


def asset_paths(script_file: str = __file__) -> tuple[Path, Path]:
    """Return ``(db_path, config_path)`` for the bundled assets.

    Paths are resolved relative to this script so the skill works regardless
    of the caller's working directory.
    """
    assets = Path(script_file).resolve().parent.parent / "assets"
    return assets / f"{DB_STEM}.lancedb", assets / "haiku.rag.yaml"


def load_config(config_path: Path):
    """Build an ``AppConfig`` from the bundled ``haiku.rag.yaml``."""
    from haiku.rag.config import AppConfig, load_yaml_config

    return AppConfig.model_validate(load_yaml_config(config_path))


def format_citation(citation) -> str:
    """Render a resolved ``Citation`` as a full, human-readable record."""
    header = f"[{citation.chunk_id}]"
    if citation.index is not None:
        header = f"[{citation.index}] {header}"

    lines = [header]
    if citation.document_title:
        lines.append(f"Title: {citation.document_title}")
    if citation.document_uri:
        lines.append(f"URI: {citation.document_uri}")
    if citation.page_numbers:
        lines.append(
            "Pages: " + ", ".join(str(p) for p in citation.page_numbers)
        )
    if citation.headings:
        lines.append("Section: " + " > ".join(citation.headings))
    lines.append(f"Content:\n{citation.content}")
    return "\n".join(lines)


async def run_search(
    query: str, limit: int | None, filter: str | None
) -> str:
    """Hybrid-search the knowledge base and format results for an agent."""
    from haiku.rag.client import HaikuRAG

    db_path, config_path = asset_paths()
    config = load_config(config_path)
    async with HaikuRAG(db_path=db_path, config=config, read_only=True) as rag:
        results = await rag.search(query, limit=limit, filter=filter)
        results = await rag.expand_context(results)
        if not results:
            return "No results found."
        return "\n\n---\n\n".join(
            result.format_for_agent(rank=i + 1, total=len(results))
            for i, result in enumerate(results)
        )


async def run_cite(chunk_ids: list[str]) -> str:
    """Resolve chunk IDs against the database into full citation records.

    Unlike the in-agent ``cite`` tool, a filesystem-skill invocation has no
    prior in-memory search state, so every chunk ID is resolved directly from
    the database.
    """
    from haiku.rag.client import HaikuRAG
    from haiku.rag.store.models.chunk import SearchResult
    from haiku.rag.store.models.citation import resolve_citations

    db_path, config_path = asset_paths()
    config = load_config(config_path)
    async with HaikuRAG(db_path=db_path, config=config, read_only=True) as rag:
        normalized = [cid.strip("[]") for cid in chunk_ids]
        synthetic: list[SearchResult] = []
        doc_cache: dict[str, object] = {}
        for cid in normalized:
            chunk = await rag.get_chunk_by_id(cid)
            if chunk is None or not chunk.document_id:
                continue
            did = chunk.document_id
            if did not in doc_cache:
                doc_cache[did] = await rag.get_document_by_id(did)
            doc = doc_cache[did]
            chunk.document_uri = doc.uri if doc else None
            chunk.document_title = doc.title if doc else None
            synthetic.append(SearchResult.from_chunk(chunk, score=1.0))

        citations = resolve_citations(normalized, synthetic)
        for position, citation in enumerate(citations, start=1):
            if citation.index is None:
                citation.index = position

        resolved_ids = {c.chunk_id for c in citations}
        missing = [cid for cid in normalized if cid not in resolved_ids]

        blocks: list[str] = []
        if citations:
            blocks.append(
                "\n\n---\n\n".join(format_citation(c) for c in citations)
            )
        else:
            blocks.append(
                "No chunk IDs could be resolved against the database."
            )
        if missing:
            blocks.append("Unresolved chunk IDs: " + ", ".join(missing))
        return "\n\n".join(blocks)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="haiku_rag.py",
        description=f"Search and cite the {DB_STEM} knowledge base.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", help="Hybrid search the knowledge base")
    search.add_argument("query", help="The search query")
    search.add_argument(
        "--limit",
        "-l",
        type=int,
        default=None,
        help="Maximum number of results (default: config search limit)",
    )
    search.add_argument(
        "--filter",
        "-f",
        default=None,
        help="SQL WHERE clause to filter documents "
        "(e.g. \"uri LIKE '%%arxiv%%'\")",
    )

    cite = sub.add_parser(
        "cite", help="Resolve chunk IDs into full citation records"
    )
    cite.add_argument("chunk_ids", nargs="+", help="One or more chunk IDs")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "search":
        print(asyncio.run(run_search(args.query, args.limit, args.filter)))
    elif args.command == "cite":
        print(asyncio.run(run_cite(args.chunk_ids)))
    else:  # pragma: no cover - argparse enforces a valid subcommand
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
