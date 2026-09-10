"""ECK command line.

    eck estate list             what is in scope, and why (BR-05)
    eck refresh                 rebuild knowledge.db from the register (BR-65)
    eck coverage                what was interpreted, and what was not (BR-13)
    eck verify-determinism      prove a re-run over unchanged source is identical (BR-11)
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message=".*OpenSSL.*")

from . import config, register
from .config import ENV_FILE, answering_credential, load_env
from .govern import coverage, refresh
from .store.db import KnowledgeStore

# .env must be read before any path is resolved, because the paths themselves
# are configurable. Doing this at import time keeps every module-level
# constant below consistent with what the process will actually use.
load_env()

ROOT = config.project_root()
REGISTER = config.register_path()
DB = config.db_path()
BUILD_DIR = DB.parent


def cmd_estate_list(_args: argparse.Namespace) -> int:
    reg = register.load(REGISTER, ROOT)
    print(f"{reg.estate_name}  ({reg.estate_id})")
    print(f"platform: {reg.platform}")
    print(f"register: {REGISTER.relative_to(ROOT)}  sha256:{reg.register_sha[:16]}")
    print()
    print(f"{'ID':<8} {'NAME':<28} {'TECH':<10} {'OWNER':<12} SOURCE")
    for a in reg.assets:
        exists = "" if a.abs_path.exists() else "   [MISSING]"
        print(f"{a.id:<8} {a.name:<28} {a.tech:<10} {a.owner:<12} {a.abs_path}{exists}")
    print()
    ex = reg.assets[0].exclusions if reg.assets else []
    print("EXCLUSIONS (BR-04 — every exclusion carries a reason)")
    for e in ex:
        print(f"  {e.path_glob:<24} {e.reason}")
    problems = register.validate(reg)
    if problems:
        print("\nPROBLEMS (BR-70)")
        for p in problems:
            print(f"  - {p}")
        return 1
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    if getattr(args, "fetch", False):
        rc = cmd_sources_sync(argparse.Namespace(only=None, depth=1))
        if rc != 0:
            return rc
        print()
    reg = register.load(REGISTER, ROOT)
    print(f"refreshing {reg.estate_id} from {REGISTER.relative_to(ROOT)}")
    out = refresh.build(reg, Path(args.out or BUILD_DIR), verbose=True,
                        embed=not args.no_embed,
                        allow_broken=getattr(args, "allow_broken", False))
    size = out.stat().st_size / 1_048_576
    print(f"\npublished {out.relative_to(ROOT)}  ({size:.1f} MB)")
    print("run `eck coverage` for what was and was not interpreted")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Preflight: what this machine can and cannot do."""
    from .govern import doctor
    checks, profile = doctor.run(args.profile)
    print(doctor.report(checks, profile))
    return 1 if any(c["status"] == "fail" for c in checks) else 0


def cmd_sources_sync(args: argparse.Namespace) -> int:
    """Fetch every registered source from its remote (BR-01)."""
    from .ingest import fetch

    reg = register.load(REGISTER, ROOT)
    print(f"syncing sources for {reg.estate_id}")
    results = []
    for name, spec in reg.sources.items():
        if args.only and name != args.only:
            continue
        try:
            results.append(fetch.sync(name, spec, ROOT, verbose=True,
                                      depth=args.depth))
        except fetch.SyncRefused as exc:
            print(str(exc), file=sys.stderr)
            return 1

    print()
    for r in results:
        if r.action == "skipped":
            print(f"  {r.name:<6} skipped   {r.detail}")
        elif r.action == "cloned":
            print(f"  {r.name:<6} cloned    {(r.after or '')[:8]}  ({r.ref or 'default'})")
        elif r.moved:
            print(f"  {r.name:<6} updated   {r.before[:8]} -> {r.after[:8]}")
        else:
            print(f"  {r.name:<6} unchanged {(r.after or '')[:8]}")
    if any(r.moved or r.action == "cloned" for r in results):
        print("\nSource moved — run `eck refresh` to rebuild the knowledge base.")
    return 0


def cmd_sources_status(_args: argparse.Namespace) -> int:
    """What the register points at, and what is on disk."""
    from .ingest import fetch

    reg = register.load(REGISTER, ROOT)
    print(f"{'SOURCE':<7} {'KIND':<5} {'REF':<14} {'LOCAL':<10} ORIGIN")
    for name, spec in reg.sources.items():
        root = Path(spec["root"])
        if not root.is_absolute():
            root = (ROOT / root).resolve()
        local = fetch.head(root) if root.exists() else None
        state = (local[:8] if local else ("missing" if not root.exists()
                                          else "not-git"))
        print(f"{name:<7} {spec.get('kind','dir'):<5} "
              f"{str(spec.get('ref') or '-'):<14} {state:<10} "
              f"{spec.get('origin') or root}")
        if root.exists() and spec.get("origin"):
            actual = fetch.origin_of(root)
            if actual and not fetch._same_remote(actual, spec["origin"]):
                print(f"{'':<7} MISMATCH on disk: {actual}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    if not DB.exists():
        print("no knowledge.db — run `eck refresh` first", file=sys.stderr)
        return 1
    import uvicorn

    from .api.http import create_app
    from .services.registry import catalogue

    print(f"{len(catalogue())} read-only services on "
          f"http://{args.host}:{args.port}/   (ctrl-c to stop)")
    uvicorn.run(create_app(DB), host=args.host, port=args.port,
                log_level="warning")
    return 0


def cmd_services(args: argparse.Namespace) -> int:
    """BR-47/BR-52 — the service catalogue, from the single registry."""
    from .services.registry import catalogue
    import json as _json

    cat = catalogue()
    if args.json:
        print(_json.dumps(cat, indent=2))
        return 0
    for kind, label in ((False, "ATOMIC SERVICES (BR-48)"),
                        (True, "COMPOSITE SERVICES (BR-49)")):
        print(label)
        for s in [c for c in cat if c["composite"] is kind]:
            print(f"  {s['id']}")
            print(f"      Q: {s['question']}")
            print(f"      when: {s['when_to_use']}")
            if s["inputs"]:
                for k, v in s["inputs"].items():
                    req = "required" if k in s["required"] else "optional"
                    print(f"        {k:<10} ({req}) {v}")
            print()
    print(f"{len(cat)} services. All read-only (BR-53).")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """Invoke one service (CAP-7)."""
    if not DB.exists():
        print("no knowledge.db — run `eck refresh` first", file=sys.stderr)
        return 1
    import json as _json

    from .services.registry import get, load_all
    from .services.resolve import Context

    load_all()
    try:
        spec = get(args.service)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    kwargs = {}
    for pair in args.args:
        if "=" not in pair:
            print(f"arguments must be key=value, got {pair!r}", file=sys.stderr)
            return 1
        k, _, v = pair.partition("=")
        kwargs[k.strip()] = v.strip()

    missing = [r for r in spec.required if r != "ctx" and r not in kwargs]
    if missing:
        print(f"missing required argument(s): {', '.join(missing)}",
              file=sys.stderr)
        print(f"usage: eck ask {spec.signature()}", file=sys.stderr)
        return 1

    ctx = Context(DB)
    try:
        result = spec.fn(ctx, **kwargs)
    finally:
        ctx.close()

    if args.json:
        print(_json.dumps(result.to_dict(), indent=2, default=str))
        return 0

    print(f"SERVICE   {result.service}")
    print(f"QUESTION  {result.question}")
    print(f"OUTCOME   {result.outcome.upper()}")
    print()
    for f in result.findings:
        if "section" in f:
            print(f"-- {f['section']}  [{f['outcome']}]")
            for d in f["detail"][:12]:
                print(f"     {_json.dumps(d, default=str)[:150]}")
            print()
        else:
            print(f"  {_json.dumps(f, default=str)[:220]}")
    if result.needed:
        print("\nWHAT WOULD BE NEEDED (BR-55)")
        for n in result.needed:
            print(f"  - {n}")
    if result.evidence:
        print(f"\nEVIDENCE (BR-54) — {len(result.evidence)} reference(s)")
        for e in result.evidence[:12]:
            print(f"  [{e.origin}] {e.ref()}")
            print(f"           {e.fqn[:90]}")
    if result.gaps:
        print("\nGAPS (BR-72)")
        for g in result.gaps:
            print(f"  - {g}")
    print("\nPRESENTATION GUIDANCE (BR-50)")
    print(f"  {result.presentation_guidance}")
    return 0


def cmd_anchors_propose(args: argparse.Namespace) -> int:
    """CAP-3 — generate candidates for human review. Never publishes (BR-21)."""
    if not DB.exists():
        print("no knowledge.db — run `eck refresh` first", file=sys.stderr)
        return 1
    from . import curated
    from .ingest import anchors as anchor_ingest
    from .services.retrieval import Retriever

    r = Retriever(DB)
    where, params = ["source = 'wiki'"], []
    if args.page:
        where.append("path = ?")
        params.append(args.page)
    rows = r.store.query(
        f"SELECT id, text, path, start_line, end_line FROM chunk"
        f" WHERE {' AND '.join(where)} ORDER BY path, start_line", tuple(params))
    chunks = [dict(x) for x in rows][:args.limit] if args.limit else [dict(x) for x in rows]

    if not chunks:
        print(f"no wiki chunks matched (page={args.page!r})", file=sys.stderr)
        r.close()
        return 1

    print(f"proposing anchors for {len(chunks)} statement(s) "
          f"using the {args.proposer} proposer")
    if args.proposer == "llm":
        def progress(chunk, picked):
            print(f"  {chunk['path']}:{chunk['start_line']:<5} -> {picked} selected")
        try:
            proposed = anchor_ingest.propose_by_llm(
                r, chunks, asset=args.asset, progress=progress)
        except Exception as exc:
            print(f"\nLLM proposer failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            print(f"Set ANTHROPIC_API_KEY in {ENV_FILE} (copy .env.example),"
                  f" or use --proposer retrieval.", file=sys.stderr)
            r.close()
            return 1
    else:
        proposed = anchor_ingest.propose_by_retrieval(r, chunks, asset=args.asset)

    added, skipped = curated.merge_candidates(proposed)
    r.close()
    print(f"\n{len(proposed)} proposed, {added} new, {skipped} already known")
    print(f"queue: {curated.CANDIDATES.relative_to(ROOT)}")
    print("\nNothing is published. Review with:  ./eck-cli anchors review")
    return 0


def cmd_anchors_list(args: argparse.Namespace) -> int:
    from . import curated

    if args.state in ("proposed", None):
        rows = [(a, "proposed") for a in curated.load_candidates()]
    else:
        rows = []
    if args.state != "proposed":
        decided = curated._read(curated.ANCHORS)
        rows += [(a, a.status) for a in decided
                 if args.state is None or a.status == args.state]

    if not rows:
        print(f"no anchors with state={args.state or 'any'}")
        return 0

    print(f"{'ID':<18} {'STATE':<9} {'CONF':>5}  {'ASSET':<7} TARGET")
    for a, state in sorted(rows, key=lambda t: (t[1], -t[0].confidence)):
        print(f"{a.id:<18} {state:<9} {a.confidence:>5.3f}  "
              f"{a.target_asset:<7} {a.target_fqn[:64]}")
        print(f"{'':<18} {'':<9} {'':>5}  from {a.doc_path}:"
              f"{a.doc_start_line}-{a.doc_end_line}")
    print(f"\n{len(rows)} anchor(s)")
    return 0


def cmd_anchors_validate(_args: argparse.Namespace) -> int:
    """BR-18 check against the live graph, without a full rebuild."""
    if not DB.exists():
        print("no knowledge.db — run `eck refresh` first", file=sys.stderr)
        return 1
    from . import curated
    from .govern import anchor_check

    store = KnowledgeStore(DB)
    approved = curated.load_approved()
    rows = anchor_check.validate(store, approved, run_id="validate-only")
    counts = anchor_check.summary(rows)
    store.close()

    print(f"{len(approved)} approved anchor(s)")
    for state in ("resolved", "stale", "broken", "orphaned"):
        print(f"  {state:<10} {counts.get(state, 0)}")
    for r in rows:
        if r["state"] != "resolved":
            print(f"\n  [{r['state'].upper()}] {r['target_asset']} "
                  f"{r['target_fqn']}")
            print(f"    statement {r['doc_path']}:{r['doc_start_line']}-"
                  f"{r['doc_end_line']}  approved by {r['reviewed_by']}")
    if counts.get("broken"):
        print("\nA refresh will ABORT while these are broken (BR-18).")
        return 1
    return 0


def cmd_anchors_review(args: argparse.Namespace) -> int:
    if not DB.exists():
        print("no knowledge.db — run `eck refresh` first", file=sys.stderr)
        return 1
    import uvicorn

    from . import curated
    from .api.http import create_app

    n = len(curated.load_candidates())
    print(f"anchor review — {n} candidate(s) awaiting a decision")
    print(f"open http://127.0.0.1:{args.port}/#review   (ctrl-c to stop)")
    uvicorn.run(create_app(DB), host="127.0.0.1", port=args.port,
                log_level="warning")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    """BR-69 / BR-74 — freshness, size and where the models ran."""
    if not DB.exists():
        print("no knowledge.db — run `eck refresh` first", file=sys.stderr)
        return 1
    s = KnowledgeStore(DB)
    run = s.query("SELECT * FROM refresh_run ORDER BY started_at DESC LIMIT 1")[0]
    print(f"estate        {run['estate_id']}")
    print(f"last refresh  {run['started_at']}  ({run['status']})")
    print(f"register      sha256:{run['register_sha'][:16]}")
    print()
    print("SIZE (NFR-3)")
    for label, sql in (("assets", "SELECT COUNT(*) FROM asset"),
                       ("nodes", "SELECT COUNT(*) FROM node"),
                       ("edges", "SELECT COUNT(*) FROM edge"),
                       ("wiki pages", "SELECT COUNT(*) FROM doc"),
                       ("chunks", "SELECT COUNT(*) FROM chunk"),
                       ("vectors", "SELECT COUNT(*) FROM chunk_vec")):
        print(f"  {label:<14} {s.scalar(sql):>8,}")
    print(f"  {'db size':<14} {DB.stat().st_size / 1_048_576:>8.1f} MB")
    print()
    print("SOURCE FRESHNESS")
    for a in s.query("SELECT id,name,source_commit FROM asset ORDER BY id"):
        print(f"  {a['id']:<8} {a['name']:<28} {(a['source_commit'] or '-')[:8]}")
    print()
    print("MODELS (NFR-10)")
    rows = s.query("SELECT * FROM embedding_model ORDER BY rowid DESC LIMIT 1")
    if rows:
        m = rows[0]
        where = "inside the boundary" if m["local"] else "EXTERNAL"
        print(f"  embedding   {m['name']}  dim={m['dim']}  ran {where}")
    else:
        print("  embedding   NONE — meaning-based retrieval unavailable (BR-33)")
    have, detail = answering_credential()
    print(f"  answering   {'credential present — ' + detail if have else 'NO CREDENTIAL — ' + detail}")
    if not have:
        print(f"              set ANTHROPIC_API_KEY in {ENV_FILE.name}"
              f" (copy .env.example) to enable --proposer llm")
    s.close()
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    if not DB.exists():
        print("no knowledge.db — run `eck refresh` first", file=sys.stderr)
        return 1
    from .services.retrieval import Retriever, verdict

    question = " ".join(args.question)
    r = Retriever(DB)
    hits = r.search(question, limit=args.limit, source=args.source,
                    asset_id=args.asset)

    print(f'Q: "{question}"')
    if args.source or args.asset:
        print(f"   filtered: source={args.source or 'any'} asset={args.asset or 'any'}")
    print()
    v = verdict(hits)
    if v == "none":
        # BR-55 in miniature: say so plainly rather than return noise.
        print("No relevant knowledge found.")
        print()
        print("Nothing in the indexed estate is a close enough match. Either")
        print("the question is outside what this platform covers, or the")
        print("material is not in scope yet — check `eck coverage`.")
        r.close()
        return 0

    if v == "weak":
        print("WEAK MATCH — the results below may not answer this question.")
        print("Nothing cleared the confidence threshold; treat as leads, not")
        print("as an answer. (BR-55: the platform does not guess.)")
        print()

    for i, h in enumerate(hits, 1):
        tag = "curated doc" if h.source == "wiki" else "derived code"
        sim = f"cos {h.similarity:.3f}" if h.similarity is not None else "keyword only"
        print(f"{i:>2}. [{tag}] {h.asset_id}  ({h.matched_by}, {sim})")
        print(f"    {h.heading[:96]}")
        print(f"    {h.path}:{h.start_line}-{h.end_line}")   # BR-35
        snippet = " ".join(h.text.split())[:150]
        print(f"    {snippet}...")
        print()

    if args.full and hits:
        span = r.get_span(hits[0].chunk_id)
        print("-" * 72)
        print(f"FULL SPAN (BR-38) — {span['path']}:"
              f"{span['start_line']}-{span['end_line']}")
        print("-" * 72)
        print(span["text"])
    r.close()
    return 0


def cmd_coverage(_args: argparse.Namespace) -> int:
    if not DB.exists():
        print("no knowledge.db — run `eck refresh` first", file=sys.stderr)
        return 1
    print(coverage.report(DB))
    return 0


def _fingerprint(db_path: Path) -> str:
    """Hash the knowledge content, excluding run metadata (which is time-based)."""
    s = KnowledgeStore(db_path)
    h = hashlib.sha256()
    for table, cols in (
        ("node", "id,asset_id,kind,name,fqn,signature,path,start_line,end_line,"
                 "span_sha,origin,confidence,attrs"),
        ("edge", "id,src_id,dst_id,kind,path,start_line,origin,confidence,attrs"),
        ("chunk", "id,asset_id,source,doc_id,node_id,heading,text,path,"
                  "start_line,end_line,span_sha,origin"),
    ):
        for row in s.query(f"SELECT {cols} FROM {table} ORDER BY id"):
            h.update("|".join("" if v is None else str(v) for v in row).encode())
    s.close()
    return h.hexdigest()


def cmd_verify_determinism(_args: argparse.Namespace) -> int:
    """BR-11 — processing an unchanged source twice must produce the same result."""
    reg = register.load(REGISTER, ROOT)
    tmp = BUILD_DIR / "determinism"
    if tmp.exists():
        shutil.rmtree(tmp)

    print("build 1 of 2 ...")
    a = refresh.build(reg, tmp, live_name="a.db", verbose=False)
    print("build 2 of 2 ...")
    b = refresh.build(reg, tmp, live_name="b.db", verbose=False)

    fa, fb = _fingerprint(a), _fingerprint(b)
    print(f"\n  build A  sha256:{fa[:32]}")
    print(f"  build B  sha256:{fb[:32]}")
    if fa == fb:
        print("\nPASS — identical knowledge from unchanged source (BR-11)")
        return 0
    print("\nFAIL — non-deterministic extraction (BR-11 violated)", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eck", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_estate = sub.add_parser("estate", help="the asset register (CAP-1)")
    estate_sub = p_estate.add_subparsers(dest="sub", required=True)
    estate_sub.add_parser("list").set_defaults(func=cmd_estate_list)

    p_refresh = sub.add_parser("refresh", help="rebuild the knowledge base")
    p_refresh.add_argument("--out", help="output directory")
    p_refresh.add_argument("--no-embed", action="store_true",
                           help="skip embeddings (disables meaning search)")
    p_refresh.add_argument("--allow-broken", action="store_true",
                           help="publish despite broken anchors (BR-18 override)")
    p_refresh.add_argument("--fetch", action="store_true",
                           help="sync sources from their remotes first")
    p_refresh.set_defaults(func=cmd_refresh)

    p_search = sub.add_parser("search", help="ask in ordinary language (CAP-5)")
    p_search.add_argument("question", nargs="+")
    p_search.add_argument("--source", choices=["wiki", "code"],
                          help="restrict to one kind of knowledge (BR-34)")
    p_search.add_argument("--asset", help="restrict to one asset (BR-36)")
    p_search.add_argument("-n", "--limit", type=int, default=8)
    p_search.add_argument("--full", action="store_true",
                          help="print the full span of the top hit (BR-38)")
    p_search.set_defaults(func=cmd_search)

    sub.add_parser("coverage", help="interpretation coverage").set_defaults(
        func=cmd_coverage)
    sub.add_parser("status", help="freshness and size (BR-69)").set_defaults(
        func=cmd_status)

    p_services = sub.add_parser("services", help="the service catalogue (CAP-7)")
    p_services.add_argument("--json", action="store_true")
    p_services.set_defaults(func=cmd_services)

    p_ask = sub.add_parser("ask", help="invoke an answer service (CAP-7)")
    p_ask.add_argument("service")
    p_ask.add_argument("args", nargs="*", help="key=value arguments")
    p_ask.add_argument("--json", action="store_true")
    p_ask.set_defaults(func=cmd_ask)

    p_doc = sub.add_parser("doctor", help="preflight checks for this machine")
    p_doc.add_argument("--profile", choices=["auto", "builder", "server"],
                       default="auto")
    p_doc.set_defaults(func=cmd_doctor)

    p_sources = sub.add_parser("sources", help="fetch registered source (CAP-1)")
    s_sub = p_sources.add_subparsers(dest="sub", required=True)
    s_sync = s_sub.add_parser("sync", help="clone or update from the remote")
    s_sync.add_argument("--only", help="sync just one source (code, wiki)")
    s_sync.add_argument("--depth", type=int, default=1,
                        help="clone depth; 0 for full history")
    s_sync.set_defaults(func=cmd_sources_sync)
    s_sub.add_parser("status", help="what the register points at").set_defaults(
        func=cmd_sources_status)

    p_serve = sub.add_parser("serve", help="serve the answer services over HTTP")
    p_serve.add_argument("--port", type=int, default=8800)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.set_defaults(func=cmd_serve)

    p_anchors = sub.add_parser("anchors", help="curated meaning (CAP-3)")
    a_sub = p_anchors.add_subparsers(dest="sub", required=True)

    a_prop = a_sub.add_parser("propose", help="generate candidates for review")
    a_prop.add_argument("--page", help="restrict to one wiki page")
    a_prop.add_argument("--asset", help="restrict targets to one asset")
    a_prop.add_argument("--limit", type=int, help="max statements to process")
    a_prop.add_argument("--proposer", choices=["retrieval", "llm"],
                        default="retrieval")
    a_prop.set_defaults(func=cmd_anchors_propose)

    a_list = a_sub.add_parser("list", help="show anchors and their state")
    a_list.add_argument("--state", choices=["proposed", "approved", "rejected"])
    a_list.set_defaults(func=cmd_anchors_list)

    a_sub.add_parser("validate", help="BR-18 check").set_defaults(
        func=cmd_anchors_validate)

    a_rev = a_sub.add_parser("review", help="serve the review UI")
    a_rev.add_argument("--port", type=int, default=8765)
    a_rev.set_defaults(func=cmd_anchors_review)
    sub.add_parser("verify-determinism", help="BR-11 check").set_defaults(
        func=cmd_verify_determinism)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
