# make-interactive

> Turn any HTML file into a live, comment-driven canvas inside Claude Code.

A Claude Code skill that wraps any HTML output (mockup, report, prototype, spec) in a local server with a Figma-style comment overlay. Drop pins on UI elements or highlight text in reports — each comment becomes an instruction Claude applies to the source file, and the page hot-reloads.

Built for designers, PMs, and engineers who iterate on HTML deliverables and want a tight feedback loop without leaving the browser — like Google Docs comments, but for any HTML page.

![status](https://img.shields.io/badge/status-stable-green) ![license](https://img.shields.io/badge/license-MIT-blue) ![requires](https://img.shields.io/badge/requires-Claude%20Code-orange)

---

## What it does

Two use cases, one skill:

**1. Design iteration** — Pin comments on UI elements
- Hover any element → click → comment ("make this CTA bigger and orange", "switch to a 2-col layout", "explore a darker variant")
- Claude edits the HTML, browser hot-reloads, you see the change immediately

**2. Content review** — Highlight text in reports/specs
- Select any text → floating "Comment" button → comment ("rewrite this clearer", "this seems wrong because…", "split into bullets")
- Same loop: Claude edits, page reloads

---

## Install

### One-liner (curl)

```bash
curl -fsSL https://raw.githubusercontent.com/seant-ctrl/make-interactive/main/install.sh | bash
```

### Manual (git clone)

```bash
git clone https://github.com/seant-ctrl/make-interactive.git
cd make-interactive
./install.sh
```

Both methods install to `~/.claude/commands/make-interactive/`. Restart Claude Code (or reload skills) after install.

**Requires**: Claude Code, Python 3.7+ (stdlib only — no pip deps), and a browser.

---

## Usage

In Claude Code:

```
/make-interactive path/to/your-file.html
```

Optional `--port 8000` if 7321 is taken.

Claude will boot the server, open the URL in your browser, and watch for comments. Drop pins, highlight text, send. Claude applies each comment to the source HTML; the page reloads automatically.

When you're done: `stop`, `done`, or close the browser tab.

---

## Features

### Pin mode (UI iteration)
- Hover outline follows your cursor
- Click any element to pin a comment, anchored to the element's top-right corner
- Multiple pins on the same element cascade horizontally (no stacking)
- Pin badges color-coded: gray=draft, orange=pending, green=resolved

### Select mode (content review)
- Highlight any text in the page → floating "💬 Comment" button
- Modal opens with the selection preserved as context

### 8 designer quick-action chips
Tell Claude what kind of edit you want without spelling it out:
- **Rewrite** • **Tighter** • **Clearer** • **Variants**
- **Copy only** • **Layout only** • **Add motion** • **Ask, don't edit**

### Batch workflow
- "Add to batch" lets you drop several pins before sending
- Claude processes them as a coherent set (so "remove this" + "expand that" interact correctly)
- Send anytime via the toolbar's "Send (N)" button

### Live reload
- Server-Sent Events stream pushes a reload to your browser whenever Claude edits the file
- Pins persist across reloads, restored from the queue file

### Persistence
- Comments stored next to the HTML in `.make-interactive-queue.json`
- Survives server restarts — becomes a free changelog of your iteration
- Each entry: selector, xpath, preview, comment, quick-action, viewport, status, applied note

### Robust by design
- Shadow DOM isolation — your page's CSS can't bleed into the overlay
- `composedPath()` event detection — bulletproof across React/Vue/etc.
- `mousedown` + `click` interception in pin mode — page handlers can't react before Claude does
- Python stdlib only on the server side — zero dependencies

### Keyboard
- `C` — toggle comment mode
- `Esc` — close modal / exit mode
- `Cmd/Ctrl+Enter` — send the current comment immediately

---

## How it works

```
┌──────────────┐   POST /api/comments    ┌─────────────────┐
│   Browser    │ ──────────────────────► │  Python server  │
│  (overlay)   │ ◄── SSE /api/events ─── │   (stdlib)      │
└──────────────┘                         └────────┬────────┘
                                                  │ stdout
                                                  ▼
                                         ┌─────────────────┐
                                         │  Claude Code    │
                                         │  (Monitor tool) │
                                         └────────┬────────┘
                                                  │ Edit
                                                  ▼
                                         ┌─────────────────┐
                                         │   HTML file     │ ──┐
                                         └─────────────────┘   │
                                                  ▲            │ mtime
                                                  │            ▼
                                                  └──── file watcher
```

The server emits structured stdout lines (`[ready]`, `[comment] {json}`, `[batch]`, `[reload]`) that Claude Code watches via the Monitor tool. Each comment triggers Claude to read the queue file, edit the HTML, and the browser hot-reloads.

---

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/seant-ctrl/make-interactive/main/uninstall.sh | bash
```

Or just `rm -rf ~/.claude/commands/make-interactive/`.

---

## Author

Made by [@seant-ctrl](https://github.com/seant-ctrl). Bug reports and PRs welcome.

---

## License

[MIT](LICENSE) — fork, modify, share.
