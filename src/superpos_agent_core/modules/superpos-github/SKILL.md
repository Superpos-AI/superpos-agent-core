---
name: superpos-github
description: Call the GitHub REST API through the Superpos credential proxy — list/open PRs and issues, read repos, trigger Actions — without holding a GitHub token. Use for GitHub API work when you don't need a local git clone, or when no GITHUB_TOKEN is configured.
---

# GitHub via Superpos

The hive can register a **GitHub service connection**. This skill reaches the
GitHub REST API *through* that connection: every call is forwarded by the
Superpos proxy, which injects the stored credentials server-side. You never
see a token, and nothing has to be configured in this container.

There are two ways to touch GitHub from here — pick by the job:

- **This skill (proxy / API).** Best for API-shaped work: open or list PRs,
  comment, read files via the contents API, manage issues, dispatch GitHub
  Actions. No clone, no local token.
- **`git` + `gh` (direct).** Best for repository work: clone, branch, commit,
  push, then open a PR (the `github-pr` module). That path authenticates with
  `GITHUB_TOKEN` or a broker-minted installation token and is wired up at
  container start — use it when you need a working tree.

If a task says "open a PR for changes you just pushed", use `git`/`gh`. If it
says "what PRs are open?" or "comment on PR #42", this skill is faster.

## When to use it

- Reading or writing GitHub state through the API (PRs, issues, reviews,
  statuses, repo contents, Actions) without needing a local checkout.
- No `GITHUB_TOKEN` is set, but the hive has a GitHub connection — this is
  then the *only* GitHub path available.

## Tools

All commands are on PATH and print JSON to stdout (pipe through `jq`).

### `superpos-github connections`

List the GitHub service connections in the hive. Use this first if you're
unsure a connection exists or which one to target.

```bash
superpos-github connections
superpos-github connections --status all
```

Each row carries `name` (what `--service` expects), `id`, and
`metadata.auth_type` (`github_app` or `token`).

### `superpos-github api METHOD PATH`

Raw GitHub REST passthrough — anything in the GitHub API docs.

```bash
superpos-github api GET /user
superpos-github api GET /repos/acme/widgets/pulls --query '{"state":"open"}'
superpos-github api POST /repos/acme/widgets/issues/7/comments --body '{"body":"on it"}'
```

### Conveniences

```bash
superpos-github pr-list acme widgets --state open
superpos-github pr-create acme widgets --title "Fix login" --head fix/login --base main --body "…"
superpos-github issue-create acme widgets --title "Flaky test" --body "…"
```

## Targeting a connection

By default the first active GitHub connection is used. Override with
`--service <name>` or the `SUPERPOS_GITHUB_SERVICE` env var when the hive has
more than one.

## Limits

- The proxy forwards to whatever the connection is permitted to do; a `403`
  from GitHub usually means the connection's scopes/installation don't cover
  that repo or operation — not a bug in this skill.
- Large blobs come back as JSON GitHub gives them (often base64). Decode as
  needed.
