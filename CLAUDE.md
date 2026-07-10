# CLAUDE.md — Agent instructions for this repo

## Task memory: use Beads (`bd`), not scattered markdown

This project uses **Beads** as its task-tracking memory. Beads is a dependency-aware
issue graph stored in a `.beads/` directory (a version-controlled Dolt SQL DB) at the
repo root. It replaces ad-hoc `TODO.md` / `PLAN.md` / `DESIGN_*.md` planning files:
work items, their dependencies, and their status live in the graph so they survive
context compaction and are shared with any other agent (e.g. Antigravity) pointed at
this repo.

**Rule of thumb:** if you're about to write a new markdown file to plan or track work,
create a beads issue instead.

---

## One-time setup (do this first if it isn't done)

Check whether beads is initialized here:

```powershell
bd version            # confirm the CLI is installed (should be v1.1.0+)
Test-Path .beads      # if False, initialize:
bd init               # creates .beads/ in this repo
bd setup claude       # installs the canonical Beads agent instructions + /beads:* slash commands + MCP server for Claude Code
```

`bd setup claude` drops the **authoritative** usage guide into the project. Treat that
file as the source of truth for exact commands and flags; the workflow below is the
orientation, not the full reference. When unsure of a flag, run `bd --help` or
`bd <command> --help` — do **not** guess.

### Migrate existing plans

This repo already has planning content in `KNOWN_ISSUES.md`,
`DESIGN_persistence_aware_icon_vote.md`, and `pipeline_report.md`. On first adoption,
fold their open items into beads issues (one issue per discrete task, with dependencies
where one blocks another), then leave the original docs as historical design notes —
don't keep both as live trackers.

---

## Daily workflow

```powershell
bd ready                              # show only UNBLOCKED work — start here every session
bd show <id>                          # read an issue's full detail before working it
bd create "title" -d "description"    # log a new task  (verify flags with bd --help)
bd update <id> ...                    # change status / fields as you work
bd close <id>                         # mark complete when done
bd blocked                            # see what's waiting on dependencies
bd stats                              # project state at a glance
```

Prefer the **`/beads:*` slash commands** (`/beads:ready`, `/beads:create`, `/beads:show`,
…) when running interactively — they're installed by `bd setup claude`. Use the raw
`bd` CLI with `--json` when you need to parse output programmatically.

### Logging discovered work

When you uncover new work mid-task, record it immediately and link it to its origin so
the graph stays connected:

```powershell
bd create "fix knock reclassification edge case" --deps discovered-from:<current-id>
```

Dependency types: `blocks`, `parent-child`, `discovered-from`, `related`. Use them so
`bd ready` can correctly hide work that isn't actionable yet.

### Syncing

Beads auto-commits each change into its Dolt DB. If a Dolt remote has been configured
for this repo, run `bd dolt push` after closing issues so other machines/agents see the
update. If no remote is set up, the `.beads/` DB is local memory only — no push needed.

---

## Guardrails

- Beads is for **task tracking only**. It does not run, modify, or reason about this
  project's code, models, or databases (`elo.db`, `killfeed.db`, etc.). Keep it in its
  lane.
- It's a young, fast-moving tool. If a `bd` command behaves unexpectedly, stop and
  report it rather than forcing a workaround that could corrupt the issue graph.
- `.beads/` is managed by the `bd` tool — don't hand-edit files inside it.


<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:970c3bf2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->
