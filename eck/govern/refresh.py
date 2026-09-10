"""CAP-9 — build the knowledge base from the register (BR-65..BR-70).

Staged and restartable in later milestones; for M1 the stages are
register -> extract -> publish, and publish is an atomic rename so a reader
never observes a partial graph (BR-66).
"""
from __future__ import annotations

from pathlib import Path

from .. import curated
from ..ingest import chunker, wiki_ingest
from ..ingest.embed import LocalEmbedder, pack
from ..ingest.java_extractor import JavaExtractor
from ..register import Register, validate
from ..store.db import KnowledgeStore
from . import anchor_check

STAGES = ["register", "extract", "meaning", "anchors", "index", "publish"]


def build(reg: Register, out_dir: Path, live_name: str = "knowledge.db",
          verbose: bool = True, embed: bool = True,
          allow_broken: bool = False) -> Path:
    problems = validate(reg)
    if problems:
        # BR-70: fail clearly rather than quietly describing a partial estate.
        raise SystemExit("register validation failed:\n  - " + "\n  - ".join(problems))

    out_dir.mkdir(parents=True, exist_ok=True)
    staging = out_dir / f"{live_name}.new"
    live = out_dir / live_name

    store = KnowledgeStore.create(staging)
    run_id = store.start_run(reg.estate_id, reg.register_sha, stage="extract")

    for asset in reg.assets:
        store.add_asset(
            id=asset.id, name=asset.name, role=asset.role, owner=asset.owner,
            tech=asset.tech, source_kind=asset.source_kind,
            abs_path=str(asset.abs_path), rel_path=asset.rel_path,
            source_commit=asset.source_commit, run_id=run_id)
        store.add_exclusions([
            dict(asset_id=asset.id, path_glob=e.path_glob, reason=e.reason,
                 run_id=run_id) for e in asset.exclusions])
    store.commit()

    extractor = JavaExtractor(run_id)
    code_assets = [a for a in reg.assets if a.source_kind == "code"]

    for asset in code_assets:
        before = extractor.result.files_seen
        extractor.parse_asset(asset)
        if verbose:
            seen = extractor.result.files_seen - before
            print(f"  {asset.id:<8} {asset.name:<28} {seen:>5} files")

    if verbose:
        print(f"  resolving {len(extractor.pending):,} reference sites ...")
    extractor.resolve()

    res = extractor.result
    store.add_nodes(res.nodes)
    store.add_edges(res.edges)
    store.add_parse_failures(res.failures)
    store.add_unresolved(res.unresolved)
    store.commit()

    # ---- stage: meaning (CAP-3 source) ---------------------------------
    all_chunks: list = []
    for asset in reg.assets:
        if asset.source_kind != "wiki":
            continue
        docs, chunks, failures = wiki_ingest.ingest(asset, run_id)
        store.add_docs(docs)
        store.add_parse_failures(failures)
        all_chunks.extend(chunks)
        if verbose:
            print(f"  {asset.id:<8} {asset.name:<28} "
                  f"{len(docs):>5} pages -> {len(chunks)} chunks")

    # ---- stage: index (CAP-5) ------------------------------------------
    asset_paths = {a.id: a.abs_path for a in reg.assets}
    code_chunks, chunk_failures = chunker.chunk_code(store, asset_paths, run_id)
    store.add_parse_failures(chunk_failures)
    all_chunks.extend(code_chunks)
    if verbose:
        print(f"  code chunks {len(code_chunks):,}")

    store.add_chunks(all_chunks)
    store.commit()

    if embed:
        embedder = LocalEmbedder()
        if verbose:
            print(f"  embedding {len(all_chunks):,} chunks with "
                  f"{embedder.name} (local, NFR-10) ...")
        texts = [c["text"] for c in all_chunks]
        vectors = embedder.encode(texts, progress=verbose)
        store.add_vectors([(c["id"], embedder.dim, pack(v))
                           for c, v in zip(all_chunks, vectors)])
        store.record_embedding_model(run_id, embedder.name, embedder.dim, True)
    elif verbose:
        print("  embedding SKIPPED (--no-embed): meaning-based retrieval "
              "will be unavailable (BR-33)")

    # ---- stage: anchors (CAP-3) ----------------------------------------
    # Curated anchors are re-imported from the durable store on every build
    # and re-validated against the structure just derived (BR-18).
    approved = curated.load_approved()
    anchor_rows = anchor_check.validate(store, approved, run_id)
    if anchor_rows:
        store.add_anchors(anchor_rows)
        store.commit()
    if verbose:
        counts = anchor_check.summary(anchor_rows)
        print(f"  anchors {len(approved)} approved -> "
              + ", ".join(f"{v} {k}" for k, v in counts.items() if v))

    # This raises before publish, so a failed validation leaves the previously
    # published knowledge.db exactly as it was (BR-18, BR-70).
    anchor_check.enforce(anchor_rows, allow_broken=allow_broken)

    store.finish_run(run_id, "ok",
                     notes=f"{len(res.nodes)} nodes, {len(res.edges)} edges, "
                           f"{len(all_chunks)} chunks, "
                           f"{len(anchor_rows)} anchors")

    store.publish(live)          # atomic — BR-66
    return live
