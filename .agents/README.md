# .agents/ — shared agent knowledge

This directory is the git-committed knowledge base for anyone (human or AI agent) working
on `rpc-state-indexer`. It exists so context survives across sessions and people: read it
on pickup, append to it as you learn. It is intentionally small and plain Markdown so it
is easy to keep current.

Root guidance lives in [`../AGENTS.md`](../AGENTS.md); this directory is the memory and
lessons it points to.

## Layout

```
.agents/
  README.md      <- you are here: the convention + how to update
  MEMORY.md      <- index of durable facts        -> memory/<slug>.md
  LESSONS.md     <- index of traps, symptom-first -> lessons/<slug>.md
  memory/        <- one durable fact per file
  lessons/       <- one lesson per file
  templates/     <- copy these to create new entries
```

## When to write which

- **Memory** = a durable, non-obvious *truth* about the system that a future agent needs
  to hold before touching related code (an invariant, a boundary value, an ops fact).
  Prefer citing the file/config that makes it true over restating docs.
- **Lesson** = a *trap*, indexed by the symptom you'd search for. A lesson usually exists
  because something broke, or because a guardrail was added to stop it breaking. Record
  the symptom, the root cause, the correct pattern, and how to avoid/detect it.

If a fact is already fully covered in `docs/`, link to it instead of copying.

## How to add an entry

1. Copy the matching template from [`templates/`](templates/) to
   `memory/<kebab-slug>.md` or `lessons/<kebab-slug>.md`.
2. Fill in the frontmatter and body. Keep it to one fact/lesson per file.
3. Add a one-line pointer to the top of the relevant index (`MEMORY.md` / `LESSONS.md`):
   - Memory: `- [<slug>](memory/<slug>.md) — <one-line hook>`
   - Lessons: `- [<symptom>](lessons/<slug>.md) — <one-line fix>`
4. Cross-link related entries in the body with `[[other-slug]]` (the file's `name:`),
   the same way the seeded entries do.
5. Set `updated:` to today's date (`YYYY-MM-DD`).

## How to keep it healthy

- When a lesson's guardrail lands or the trap is designed out, set `status: resolved` (or
  `mitigated`) rather than deleting — the history is the value.
- If an entry becomes wrong, fix it; don't leave stale facts. Recalled memory reflects
  what was true when written — verify a cited file/flag still exists before relying on it.
- Keep entries short. Long-form design belongs in `docs/`; link to it.

## Frontmatter reference

Memory (`memory/<slug>.md`):

```
---
name: <kebab-slug>          # matches the filename; the [[link]] target
type: invariant | project | ops | reference
updated: YYYY-MM-DD
---
```

Lesson (`lessons/<slug>.md`):

```
---
name: <kebab-slug>
symptom: <short phrase an agent would search by>
area: execution | discovery | census | migrations | config | rpc | storage | ops
status: active | resolved | mitigated
updated: YYYY-MM-DD
---
```
