---
name: make-interactive
description: Turn any HTML file into a live, comment-driven canvas. Triggers — "make-interactive", "rend cette page interactive", "comment on this page", "iterate on this HTML", "ouvre la page pour la commenter". Spins a local server, injects a Figma-style pin/selection overlay, and applies designer comments back into the HTML in real time.
argument-hint: <path/to/file.html> [--port 7321]
allowed-tools: Read, Edit, Write, Bash, Monitor, AskUserQuestion
---

# /make-interactive — Live HTML Iteration Canvas

Designer-focused workflow: open any HTML output (mockup, report, prototype, spec) in a local browser with a comment overlay. Each comment becomes an instruction Claude applies to the source file, and the page hot-reloads. Two intended use cases:

1. **Design iteration** — pin comments on UI elements (e.g. "make this CTA bigger and outlined", "switch to a 2-col layout", "explore a darker variant")
2. **Content review** — select text in reports/specs and ask/edit ("rewrite this clearer", "this seems wrong because…", "split into bullets")

## Inputs

`$ARGUMENTS` should be the path to an HTML file (relative or absolute). Optional `--port <n>` (default 7321).

If `$ARGUMENTS` is empty, ask the user for the file path. If the file doesn't exist, abort and tell them.

## Flow

### 1 — Boot the server and watch for comments (single Monitor call)

Run the server **with the Monitor tool**, `persistent: true`. Monitor IS the background mechanism — every line the server prints becomes a notification. Pipe through `grep --line-buffered` so only actionable events wake you up (skip `[reload]` and `[file]`):

```bash
python3 ~/.claude/commands/make-interactive/server.py <html-path> --port <port> 2>&1 | grep --line-buffered -E '\[ready\]|\[comment\]|\[batch\]|\[error\]|\[shutdown\]'
```

The server emits these structured lines on stdout:
- `[ready] http://localhost:<port>` — server is up
- `[file] <abs-path>` and `[queue] <abs-path>` — informational paths (queue lives next to the HTML file, not in cwd)
- `[comment] {json}` — a new comment was submitted (one per line, JSON payload)
- `[batch] count=<n> ids=<…>` — fires when several comments were sent together
- `[reload] mtime=<ts>` — HTML file changed on disk (you triggered it; ignore)
- `[error] <msg>` / `[shutdown]`

After the first `[ready]` line, open the URL in the user's browser:

```bash
open http://localhost:<port>
```

Then post a one-line note: `Canvas ready at http://localhost:<port>. Drop pins or select text — I'll watch for comments.`

### 2 — React to each [comment] / [batch] notification

When a `[comment]` line arrives:
1. Read the queue file. Its absolute path was printed by `[queue]` at boot — it lives next to the HTML file (`<html-dir>/.make-interactive-queue.json`), not in your cwd.
2. Find entries with `status: "pending"`.
3. For each pending entry, apply the change to the HTML file:
   - `mode: "pin"` → modify the element matched by `selector` (or its closest meaningful ancestor if the selector is too brittle). Use the `previewHTML` field to confirm you're editing the right thing. `xpath` is provided as a fallback.
   - `mode: "select"` → modify the highlighted text (`selectionText`) inside the matched element. Be careful to only change that fragment, not the whole element.
4. After editing, mark the entry `status: "resolved"` in the queue file. Add a one-line `appliedNote` summarizing what you did.
5. The server detects the HTML file change and pushes a reload to the browser automatically (no action needed from you).

When a `[batch]` line arrives, process all pending entries at once — the user dropped several pins on purpose; treat them as a coherent set and consider their interaction (e.g. "remove this section" + "expand that one" together).

After each batch, send a short summary in the chat:
- Number of comments applied
- One bullet per change (`Made the hero CTA orange and 1.5x wider`, `Rewrote intro paragraph to be one sentence shorter`)

Do **not** echo the raw comment JSON. Stay terse.

### 3 — Shutdown

Stop when the user says any of: "stop", "ferme", "done", "kill the server", or when they close the browser tab and ask to wrap up. Call `TaskStop` on the Monitor task and confirm.

## Designer quick-action chips

The overlay shows quick-action chips next to the comment box. When a comment's `quickAction` field is set, treat it as a stronger hint:

- `rewrite` → rewrite the targeted text/element cleaner, keep meaning
- `tighter` → make copy or layout more compact
- `clearer` → simplify language or structure
- `variants` → produce 2–3 visual variants of the targeted element (use `<details>` or side-by-side divs so the user can see them inline)
- `copy` → only change wording, don't touch layout/styles
- `layout` → only change layout/structure, keep wording
- `motion` → add a tasteful CSS transition/animation on the element
- `question` → the user is asking, not editing. Reply in an `<aside class="claude-reply">` block injected next to the element, don't modify anything else

If `quickAction` is empty, infer intent from the comment text.

## Constraints

- **Never** edit the queue file while the server is writing. Read it, then Edit only the `status` and `appliedNote` fields of resolved entries — do not rewrite the whole file.
- The queue file path is `<html-dir>/.make-interactive-queue.json` (next to the HTML, not in cwd). Use the absolute path from the `[queue]` line.
- Don't strip the `<script src="/__overlay.js">` or `<link href="/__overlay.css">` tags — the server injects them at serve time, they shouldn't be in the source. If you ever see them in the source file, remove them (they're stale from a manual save).
- Preserve indentation and structure of the HTML — the user reads the diff.
- If a selector no longer matches (the user kept commenting while you were editing), find the closest match using `previewHTML` and proceed. Note this in `appliedNote`.
- One Edit per comment by default. If a single comment implies multiple file regions, that's fine — but don't sneak in unrelated polish.
- Tailwind classes are fine; no design-system specifics by default.

## End-of-session

When stopping, leave the queue file intact (resolved entries become a free changelog the user can scroll through). Do not delete it.
