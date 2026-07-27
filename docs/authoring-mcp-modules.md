# Authoring MCP modules

This is the canonical guide for shipping an [MCP](https://modelcontextprotocol.io/)
server to Superpos agents **via a module**. A module declares its MCP
servers in a top-level `mcp:` block; agent-core materialises that block on
boot and hands the merged set to the coding agent (Claude / Codex).

## The module `mcp:` block

A module's `mcp:` block is a **per-server map**: each key is a server name,
each value is that server's config. Two transports are supported:

- **remote-HTTP** — `url` (+ optional `headers`). The server runs somewhere
  else; the agent connects over HTTP/SSE. No process, no secret, in the
  container.
- **stdio** — `command`, `args`, optional `env`. The server runs as a local
  child process the agent spawns and talks to over stdin/stdout.

```yaml
# module.yaml
description: Example remote-HTTP MCP module (MCP-4 reference).
env: []
mcp:
  example-remote:                       # server name
    url: https://mcp.example.com/sse    # remote-HTTP transport
```

```yaml
# stdio transport (shape only)
mcp:
  example-stdio:
    command: some-mcp-server
    args: ["--flag"]
    env:
      SOME_TOKEN: "${SOME_TOKEN}"       # NAME only — see invariant below
```

## The NAMES-only invariant

**Never put a secret literal in a module.** Every value in an `env:` entry
or a `headers:`/`env:` map inside `mcp:` must be either:

- empty, or
- a single `${VAR}` placeholder naming an environment variable.

Module manifests are authored in the registry, served over `/registry/resolved`,
and written to disk in the agent container — none of those layers should
ever see a credential value. agent-core treats `env_keys` and any `${VAR}`
placeholders as **names only** and writes them through verbatim. Values are
resolved **server-side at boot** (see the broker below), never by the
overlay or the module loader.

## Which transport to use

- **Prefer remote-HTTP when the server needs no per-agent secret.** The URL
  is public-ish config, nothing sensitive lands in the container, and it
  works **today** end-to-end (overlay → discover → collect → agent).
- **Use stdio (with the broker) when a local process needs a secret.** This
  depends on boot-time credential injection, which is a **pending follow-up**
  (see below) — until it lands, a stdio server that references `${NAME}`
  receives the literal `${NAME}`, not the resolved value.

## The credential broker (MCP-3)

When a module's MCP server references credential names, the resolved values
come from the **server-side credential broker**: a `POST /api/v1/mcp/credentials`
endpoint that resolves NAMES → values from a linked `service_connection`'s
`auth_config.mcp_env`. The secret lives in the service connection, never in
the module or the registry payload.

> **Pending follow-up — not on `main`.** agent-core **boot-time injection**
> (`resolve_mcp_servers` / `resolve_mcp_credentials`, delivered by the
> `mcp_credentials` module) is a follow-up that is **not yet merged to
> `main`**. Until it lands:
>
> - **remote-HTTP servers with no secret work today.**
> - A **stdio server needing a secret** will receive the literal `${NAME}`
>   placeholder — the broker call that swaps it for the real value is the
>   pending piece.

## The full pipeline

```
module mcp: block (authored in the registry)
  → server persists + validates it            (ModuleMcpValidator)
  → GET /registry/resolved carries manifest.mcp
  → agent-core overlay materialises module.yaml   (registry_overlay._materialise_module)
  → module_loader.discover_modules reads mcp back  (ModuleInfo.mcp_config)
  → module_loader.collect_mcp_servers merges all   (one dict)
  → coding agent:
        Claude  → ClaudeAgentOptions.mcp_servers
        Codex   → ~/.codex/config.json  mcpServers
  → (pending) boot-time broker resolves ${NAME} → value
```

The overlay passes `manifest.mcp` through to the top-level `mcp:` key of
`module.yaml` **verbatim** — it never resolves, renames, or drops entries.
`collect_mcp_servers` merges every module's `mcp_config` into a single dict
(last module wins on a name collision).

## Testing locally

The end-to-end agent-side chain is exercised by
[`tests/test_mcp_module_e2e.py`](../tests/test_mcp_module_e2e.py). It builds
the canonical `example-remote-mcp` registry payload, runs the real overlay
into a tmp modules dir, reads the written `module.yaml` back, and asserts the
concrete `example-remote` server (name + url) survives `discover_modules` →
`collect_mcp_servers`. It also asserts a `${EXAMPLE_MCP_TOKEN}` header
placeholder survives **verbatim**, proving no value resolution happens
agent-side.

```bash
python -m pytest tests/test_mcp_module_e2e.py tests/test_registry_overlay.py -q
ruff check .
```

## Canonical example module

The reference module — works today, no secret:

```yaml
# module.yaml  (slug: example-remote-mcp)
description: Example remote-HTTP MCP module (MCP-4 reference).
env: []
mcp:
  example-remote:
    url: https://mcp.example.com/sse
```

The secret-bearing (broker) variant — **follow-up**, needs boot-time
injection that is **not yet on `main`**:

```yaml
# module.yaml  (secret-bearing — PENDING broker boot-injection)
description: Example remote-HTTP MCP module with an auth header.
env: []
mcp:
  example-remote:
    url: https://mcp.example.com/sse
    headers:
      Authorization: "${EXAMPLE_MCP_TOKEN}"   # NAME only; broker resolves at boot
```

Until boot-time injection lands, this variant reaches the agent with the
literal `${EXAMPLE_MCP_TOKEN}` string — do not ship a stdio/secret module
expecting a resolved value yet.
