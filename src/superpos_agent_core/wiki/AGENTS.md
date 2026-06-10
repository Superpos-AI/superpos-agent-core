# Wiki bookkeeper — procedural schema

This file is the **procedural schema** the knowledge-wiki bookkeeper
follows. It is loaded by the agent at fillin time and is *not* sent on
every API call. The agent constructs its wiki-write operations by
following the rules below and emits them via the typed knowledge client
(`superpos_agent_core.knowledge.KnowledgeClient`): `create_page` /
`update_page` for pages, `ingest_source` for raw materials,
`synthesize_topic` to fan a set of sources into a new topic page.

It implements TASK-298 / Phase A3 of the Knowledge Wiki Redesign. For
the full design see superpos-app
`docs/proposals/knowledge-wiki-redesign.md` (§5 the Karpathy model, §6
the data model, §7 the bookkeeper, §8 the API/SDK).

## The three layers

The wiki is modelled on Karpathy's "LLM wiki as agent memory" pattern.
It has three layers:

| Layer | Role | Mutability | How the agent touches it |
|---|---|---|---|
| **raw** | Immutable source materials the agent reads but never edits | insert-only | `ingest_source(...)`, read via `get_source` / `list_sources` |
| **wiki** | The LLM-maintained, cross-linked knowledge base | read + replace (versioned) | `create_page` / `update_page`, read via `list_by_type` / `get_backlinks` |
| **this file** | The procedural schema (how the bookkeeper maintains the wiki) | human-edited, loaded at boot | n/a |

**Search is a navigator, not the memory.** FTS and embeddings index the
page `body`; they help the agent *find* the right page. The wiki is the
truth. Never treat a search hit as a substitute for reading and
maintaining the page.

## The five memory types (page `type`)

Every wiki page has a `type`. The valid values are:

| `type` | Write one when… |
|---|---|
| `entity` | A named thing (person, project, system, session, channel, agent) recurs across ≥2 sources and merits its own page. |
| `topic` | A synthesised meaning — a subject, design decision, or policy — appears across ≥2 entries and is worth summarising. (Semantic memory.) |
| `trend` | A pattern is stable enough to track over time; the page records dated observations. |
| `source_page` | A `knowledge_sources` row is ingested and you write a summary page that links back to the source ULID. (The `raw → summary` pipeline — the highest-value writer-side job.) |
| `log` | Something happened (an ingest, query, lint pass, or human edit) and should be recorded with a timestamp. (Episodic memory.) |
| `procedure` | A workflow is repeated often enough that codifying the how-to pays off. |

A page's `slug` conventionally encodes its type, e.g.
`entity:redis-cluster-prod`, `topic:auth-sprawl`, `source:01HX…`. The
slug character set is `[A-Za-z0-9:_\-\.]`.

## Frontmatter

`frontmatter` is a free JSON object validated per-type at write time.

- **Empty frontmatter is always a valid write** for every type — write
  a pure-body page and let the curator backfill metadata later.
- **Unknown keys are rejected (422).** The allowed set for a type is its
  `required` + `lint_required` + `optional` keys, plus the reserved
  system keys (`broken_links`, `kind`, `lint_notes`) which are managed
  by the curator/linter — never write those yourself.
- **`required` keys** are rejected at write time if missing.
- **`lint_required` keys** are NOT rejected; the linter flags the page
  (`lint_state = needs_attention`) until the curator backfills them.

Per-type guidance (see §6.4 of the proposal for the authoritative set):

- `entity` — `kind` is required; optional `aliases`, `status`, `owners`,
  `related_entity_slugs`.
- `topic` — optional `aliases`, `related_topic_slugs`, `superseded_by`;
  `summary` is lint-required.
- `trend` — `first_observed`, `last_refreshed`, `confidence` optional;
  `summary` is lint-required.
- `source_page` — `source_sha256` is lint-required; optional
  `authored_by`, `published_at`.
- `log` — `event_type` and `actor` are lint-required; optional
  `parent_log_slug`, `related_entry_slugs`.
- `procedure` — optional `inputs`, `outputs`, `superseded_by`,
  `review_after_days`.

## Body and `[[wikilinks]]`

`body` is verbatim markdown. It is the place for `[[wikilink]]`
references (Obsidian-flavored). A `[[topic:auth-sprawl]]` in a page body
creates an **authored** link to that page; the parser is exact (no fuzzy
match). Use `get_backlinks(entry_id)` to find every page that links *to*
a given page.

Link sources to the pages that summarise them: a `source_page` body
should `[[source:<ULID>]]` (or carry the ULID in `frontmatter.source_sha256`)
so the raw → summary chain is traceable.

## Source visibility (§6.8)

Raw sources are visible **only through a referencing page**. A source
appears in `list_sources` / `get_source` only if the caller can read at
least one page whose `source_ids` cites it. Consequences for the
bookkeeper:

- `get_source` raises `KnowledgeNotFound` (a 404, not a 403) when the
  source either does not exist or is not visible to you — existence is
  never leaked.
- Attaching a source to a page (`create_page(source_ids=[…])` /
  `update_page`) you cannot already see is a **403** and rolls back —
  you cannot widen a source's visibility.
- An **orphan** source you ingested yourself (zero citing pages) can be
  attached to a page by you, the originator.

## How to write — the standard flows

1. **Ingest then summarise (two-step).**
   `ingest_source(kind=…, uri=…, content_sha256=…)` returns a source
   (idempotent on content hash). Then
   `create_page(type="source_page", slug="source:…", body=…,
   source_ids=[source_id])` writes the summary and attaches the source.

2. **Ingest-and-attach (single call).** Pass the descriptor inline:
   `create_page(type=…, slug=…, body=…, sources=[{kind, uri,
   content_sha256, …}])`. Each descriptor is ingested **and** attached
   in the same transaction, satisfying the attach ACL directly.

3. **Update a page.** `update_page(entry_id, body="…")` replaces the page
   body (full replacement) and bumps the version.

4. **Synthesise a topic.** Given a set of source ULIDs you can read,
   `synthesize_topic(source_ids=[…], slug="topic:…")` dispatches an
   async task that writes a new `topic:` page and emits a `log:` entry.
   It returns a task descriptor — poll for completion.

## Deferred methods

`get_wiki_index()` and `get_wiki_log()` are listed in the proposal
(§8.5) but their endpoints are **not live yet** — the in-wiki `index`
and `log` pages are part of the bookkeeper rewrite (§7.3 / Phase B), not
the A3 read scope. The SDK methods raise `NotImplementedError` rather
than calling a route that would 404. Until they land, enumerate pages
with `list_by_type` (including `list_by_type("log")` for the episodic
log).
