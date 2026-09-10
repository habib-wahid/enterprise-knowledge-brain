# ECK project explainer

Enterprise Code Knowledge Platform — Inteacc HCM demo.
Live state as of 10 September 2026.

This repository is **not** the HCM application. It is a Python platform that
**reads** 15 Jmix modules plus a wiki, builds a traceable knowledge graph, and
answers questions with exact source locations.

---

## 1. What this system is

ECK turns a registered software estate into an evidence-anchored knowledge base.

Design split:

| Kind of knowledge | Where it comes from | Label |
|---|---|---|
| Derived structure | Java, extracted automatically | `derived` |
| Curated meaning | Wiki text written by people | `curated` |
| Inferred links | Heuristic call resolution | `inferred` |

Every claim must point at a file and line range. The platform would rather
return `unknown` than guess.

Built against `enterprise-code-platform-brd.pdf` v1.1.

| Milestone | Capability | State |
|---|---|---|
| M0 | Estate register (CAP-1) | Done |
| M1 | Structural Java extraction (CAP-2) | Done |
| M2 | Wiki ingest + hybrid retrieval (CAP-5) | Done |
| M3 | Wiki→code anchoring (CAP-3) | Infrastructure done; almost no trusted reviews |
| M4 | 15 answer services (CAP-7) | Done |
| M5 | MCP server (CAP-8) | Not started |
| M6 | Process + reference data (CAP-4, CAP-6) | Not started |
| M7 | Refresh + governance (CAP-9) | Partial |

---

## 2. How a rebuild works

`build/knowledge.db` is **not** created by clone, pip install, or `serve`.
It is created only by:

```sh
./eck-cli refresh
```

Work is written to `build/knowledge.db.new`, then atomically renamed over the
live file. A reader never sees a half-built graph. A failed validation (broken
approved anchors) leaves the previous database untouched.

### Step 1 — Register

`register/estate.yaml` is the only place that names source paths.

- 15 Java modules under `/Users/md.habiburrahman/inteacc-payroll`
- 1 wiki at `sources/hrishelp-wiki`
- Exclusions (`build/`, tests, generated code) each carry a reason

Missing paths abort before publish.

### Step 2 — Extract Java

`eck/ingest/java_extractor.py` parses in-scope `.java` files with tree-sitter.
This estate is **Jmix 2.8.1**, not Spring MVC: entry points are `@Route` views
and behaviour lives in `@Subscribe` handlers and `*ServiceBean` classes.

Pass 1 inventories types, methods, fields, entities, tables, views, roles.
Pass 2 resolves call sites:

- import-qualified → derived, high confidence
- same-package → derived
- ambiguous / class-level fallback → inferred
- unresolvable → a row in `unresolved_ref` (never silently dropped)

### Step 3 — Add meaning (chunks)

Two kinds of searchable **chunk** land in `knowledge.db`:

**Curated chunk** — a heading section from a wiki `.md` page. A person wrote
the words. ECK only splits on headings, prefixes the heading path, strips
images, and indexes the text. Example: `Attendance-management.md` section
“1.0 Outstation duty”.

**Derived chunk** — a Java method or type, cut at semantic boundaries, with a
short natural-language header so business questions can match code.

A curated chunk is **not** proof that the wiki sentence is implemented in Java.
That proof is an **anchor**.

### Step 4 — Embed

`BAAI/bge-small-en-v1.5` runs **on this machine** (~130 MB). Each chunk gets a
384-dimension vector stored as a float32 blob. At search time the **question**
is embedded with the same model. There is no cloud embedding API.

If the model is missing, meaning search fails on purpose (no keyword-only
pretend).

### Step 5 — Re-import approved anchors

Human decisions live in `curated/anchors.jsonl` so they survive DB rebuilds.
Refresh projects them into the `anchor` table and re-checks the code hash.

### Step 6 — Publish

`os.replace` swaps staging onto `build/knowledge.db`.

Last successful refresh on this machine: **10 Sep 2026, 05:13 UTC**
(146.6 MB, run `f3fe41c3950d`).

---

## 3. What an anchor is, and how it is done

An **anchor** is a signed claim:

> this wiki statement is implemented at this exact code location.

It is not a search hit. Search says “these texts look related.” An anchor says
a named reviewer confirmed the link.

### Propose (machine, never publishes)

```sh
./eck-cli anchors propose --page Payroll-management.md --asset HR --limit 30
```

For each wiki chunk, retrieve similar **code** chunks (cosine ≥ 0.55, up to 6).
Write `status: proposed` into `curated/candidates.jsonl`.

- `retrieval` proposer: cheap, noisy, no API key
- `llm` proposer: Claude keeps only real implementation sites; needs
  `ANTHROPIC_API_KEY`

Re-propose will not reopen a candidate already decided.

### Review (human)

```sh
./eck-cli anchors review    # http://127.0.0.1:8765/#review
```

Wiki paragraph beside Java span. Reviewer name is required. On approve, the
current code `span_sha` is stamped. The record moves to
`curated/anchors.jsonl`.

### Refresh (project + health check)

| State | Meaning | Publish? |
|---|---|---|
| resolved | Target exists, hash unchanged | Yes |
| stale | Same FQN, body edited after approval | Yes, flagged |
| broken | Target gone | **Abort**, old DB kept |
| orphaned | Wiki statement gone | Yes, flagged |

### Use

`explanation.of` returns only approved, still-valid anchors — attributed to
reviewer and date. Unanchored wiki chunks can still appear in search as
documentation.

---

## 4. How questions are answered (after the DB exists)

```sh
./eck-cli serve            # http://127.0.0.1:8800
./eck-cli search "…"
./eck-cli ask <service> key=value
```

### Search

Hybrid retrieval:

1. SQLite FTS5 / BM25 — exact words and identifiers
2. Dense cosine — business language
3. Reciprocal Rank Fusion

Verdicts: `strong` (≥ 0.70), `weak` (0.62–0.70), `none`. Similarity is not
treated as proof that a chunk *answers* the question.

### 15 services, one registry

`eck/services/registry.py` is the only definition. CLI, HTTP, and the web UI
all enumerate it.

**Atomic (11):** navigation, impact, flow, checks, effects, explanation,
placement, search, detail, configuration, status.

**Composite (4):** change impact, failure trace, input acceptance, process
description.

Every result is a structured envelope: `answered` / `partial` / `unknown`,
findings, evidence with file:lines, gaps, and presentation guidance.
**No LLM at answer time.** Narration is the consumer’s job.

Web views: Ask, Search, Estate, Coverage, Review, Audit.

---

## 5. File map

| Path | Role |
|---|---|
| `register/estate.yaml` | Only authority for what is in scope |
| `eck-cli` | Shell wrapper → `.venv` + `python -m eck.cli` |
| `eck/cli.py` | Commands: estate, refresh, search, anchors, services, serve |
| `eck/ingest/java_extractor.py` | Jmix-aware structural extraction |
| `eck/ingest/wiki_ingest.py` | Wiki → curated chunks |
| `eck/ingest/chunker.py` | Code → derived chunks |
| `eck/ingest/embed.py` | Local embeddings |
| `eck/ingest/anchors.py` | Propose candidates; never publishes |
| `eck/govern/refresh.py` | Rebuild coordinator |
| `eck/govern/anchor_check.py` | resolved / stale / broken / orphaned |
| `eck/store/schema.sql` | SQLite graph, chunks, vectors, failures |
| `eck/services/` | Retrieval, resolution, 15 services |
| `eck/api/http.py` | FastAPI + review write endpoint |
| `eck/api/static/index.html` | Browser UI |
| `curated/candidates.jsonl` | Live human-review queue |
| `curated/anchors.jsonl` | Durable approved/rejected decisions |
| `build/knowledge.db` | Generated graph (gitignored) |
| `sources/hrishelp-wiki/` | Wiki clone (gitignored) |
| `tests/test_services.py` | Service contracts (needs DB) |
| `tests/test_anchor_lifecycle.py` | Anchor lifecycle on a synthetic estate |

External code root (not in this repo):
`/Users/md.habiburrahman/inteacc-payroll`

---

## 6. Current live numbers

From `./eck-cli status` / `coverage` after the last refresh:

| Metric | Value |
|---|---|
| Assets | 16 (all `owner: UNASSIGNED`) |
| Nodes | 34,189 |
| Relationships | 62,330 stored (refresh notes said 63,780 before dedup) |
| Wiki pages | 20 |
| Chunks | 22,040 (21,382 code + 658 wiki), all embedded |
| Call sites resolved | 30.6% (30,330 of 98,969) |
| Unresolved calls | 68,639 (mostly chained receivers and types outside the estate) |
| Proposed candidates | 153 (Payroll-management.md → HR) |
| Approved anchors | 1 — **smoke-test only**, not a real human review |
| Wiki statements anchored | 1 of 658 (0.2%) |

Verified in this environment:

- Anchor lifecycle tests: 11 passed
- Service contracts: 18 passed with `HF_HUB_OFFLINE=1`

Without offline mode, first search may try Hugging Face even when the model is
already cached.

---

## 7. How to run it

```sh
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
# payroll repo + sources/hrishelp-wiki must exist at paths in estate.yaml
./eck-cli refresh
./eck-cli serve            # http://127.0.0.1:8800
```

Useful commands:

```sh
./eck-cli estate list
./eck-cli coverage
./eck-cli status
./eck-cli search "how do we stop paying someone who has left"
./eck-cli ask navigation.find element=SalaryPaymentSendBackServiceBean
./eck-cli anchors propose --page Payroll-management.md --asset HR --limit 30
./eck-cli anchors review
```

Anthropic is needed **only** for `--proposer llm`. Search and the 15 services
run locally.

---

## 8. Hosting

Local embeddings **do** work on a host. The model runs on the server, not in
the browser. You must ship:

1. Python 3.9+ venv (`requirements.txt` pulls PyTorch; ~650 MB)
2. `build/knowledge.db` or a refresh on that machine
3. Hugging Face cache for `BAAI/bge-small-en-v1.5` (or allow one download)
4. Java repo + wiki at the paths in `estate.yaml` (needed for “view source”)

```sh
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
./eck-cli serve --host 0.0.0.0 --port 8800
```

Default bind is localhost. `0.0.0.0` has **no auth**. `/api/source` reads
estate files. Treat as internal unless you add a reverse proxy and login.

A GitHub clone is not enough: `sources/`, `build/`, and the absolute payroll
path are outside the tracked tree.

---

## 9. Known limits

- 68,639 unresolved calls → impact/flow answers are incomplete
- 954 XML view descriptors and 72 Liquibase changelogs not parsed
- Process order (CAP-4) and live config values (CAP-6) not built
- MCP (CAP-8) not built
- HTTP audit log is in-memory
- No CI, lockfile, or packaged install
- `estate.yaml` uses a machine-specific absolute code path
- README still says “no anchors”; one smoke-test anchor exists

---

## 10. Bottom line

Rebuild: YAML scope → Java graph → wiki/code chunks → local vectors →
approved anchors checked → atomic SQLite publish.

Query: hybrid search + 15 read-only services over that DB, with evidence
locations, in CLI and a six-view web UI.

Trusted wiki→code meaning is still almost empty: 657 of 658 wiki chunks have
no real human-approved anchor.
