<!--
Bundled MEMORY snapshot — the doubling FLOOR (AG-10, issue #193).

Read-only default rules served by persona_overlay.read_memory() when Superpos is
unreachable and no workspace snapshot has been re-synced. The authoritative
MEMORY document lives in Superpos and is re-synced into the workspace snapshot
on every reachable read. Memory WRITES are Superpos-only and fail loudly during
an outage — never written here.
-->

# MEMORY (bundled fallback — read-only defaults)

These are the default operating rules that apply when the live MEMORY document
cannot be fetched from Superpos.

## Defaults

- The Superpos control plane is the single source of truth for memory. This
  bundled copy is a fallback only.
- During a Superpos outage, memory is **read-only**. Memory writes
  (`superpos-task memory`) require Superpos and will fail loudly — do not work
  around this with local writes.
- Agent-local memory (`~/.claude/.../memory/`) is a separate path and is never a
  fallback for Superpos persona memory. Never double-write the same rule to both
  layers.
- On recovery, the live MEMORY document re-syncs automatically; anything you
  learned during the outage should be written to Superpos once it is reachable.
