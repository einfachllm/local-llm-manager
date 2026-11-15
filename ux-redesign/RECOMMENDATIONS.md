# Main dashboard — UX recommendations

> **Status: implemented.** The redesign below now lives in the real app (`local_llm_manager/static/`,
> changelog entry under *[Unreleased]*). Refresh the manager to see it. Renders of the **real** app:
> [`img/live-01-dashboard.png`](img/live-01-dashboard.png) … [`img/live-06-mobile.png`](img/live-06-mobile.png).
> Re-verify any time: `NODE_PATH=… node verify-live.cjs` — 36 state-driven, non-destructive checks against
> the running manager (it never confirms start/stop/switch/delete on live containers). The original
> concept prototype remains below for reference.

**Interactive concept:** [`prototype.html`](prototype.html) — open it in any browser (no server, no dependencies).
It is seeded with example recipes, ports, VRAM figures and boot times, and every
button works: start / stop / restart / switch, search, filters, sorting, card ↔ list view, detail drawer,
in-place edit, create, duplicate, delete + undo, bulk actions, saved views, drag-to-reorder, dark/light,
blur, mobile layout. State persists in `localStorage`; “Reset demo data” restores the seed.

**Renders:** [`img/`](img) (concept: `01…07`, real app with blur on: `live-*`)
**Verification:** `NODE_PATH=… node shoot.cjs` re-renders every image and runs 24 interaction checks (all pass, no JS errors).

---

## What hurts today (screenshot + `static/app.js` / `index.html`)

1. **Cards are ~460 px tall with dead space.** At 1080p you see 3½ models; the 4th is below the fold. Stopped cards waste ~150 px between facts and buttons.
2. **The machine’s state is the smallest thing on screen.** `115.1 / 130.6 GB · GPU 0%` lives in a top-right pill (`renderHostsStrip`, app.js:310). On a single-GPU box, free VRAM is *the* number every decision depends on.
3. **Running vs. stopped differ only by a border colour.** The core workflow — swapping models on one GPU — gets no visual priority; “Switch to” looks identical to “Logs”.
4. **Seven equal-weight buttons per card**, with Delete right next to Edit.
5. **A permanent 280 px filter rail** for one host and four recipes; the search placeholder truncates (“…host, ima”).
6. **Data you already compute is buried:** boot medians (`start_times`), KV %, tok/s, health findings, exit codes. `est_memory_gb` is NULL for every recipe in your DB, so memory never informs anything.
7. **Three recipes serve the same `qwen3.8` model** (vLLM :8101, SGLang :8101, TensorFold :8102) but the UI shows three unrelated cards. The stable-endpoint pattern is invisible.

## Recommendations — each one is implemented in the prototype

| # | Recommendation | See | Where it lands in the real app |
|---|---|---|---|
| 1 | **Host command strip**: full-width panel with a VRAM bar *segmented per running model*, free GB, GPU util/temp/power, next scheduled run. Answers “can I start X?” before you read a single card. | [01](img/01-dashboard.png) | move `renderHostsStrip` out of the topbar; segment data already in `state` |
| 2 | **Memory-fit aware primary action**: each stopped card shows `~112 GB · ~8m 20s · :8101` and a fit line. Button is **Start** when it fits in free VRAM, **Switch to** otherwise. Parse `--gpu-memory-utilization` / `--mem-fraction-static` from args when `est_memory_gb` is NULL. | [01](img/01-dashboard.png), [04](img/04-switch.png) | `cardHtml` (app.js:1029) + a `fitInfo()` helper |
| 3 | **Switch preview with the memory math**: numbered steps (stop X → frees 115 GB → drop cache → start, ~8m 20s), before/after VRAM bars, and — when port + served name match — a green **“Endpoint unchanged”** note so client apps keep working. | [04](img/04-switch.png) | new confirm dialog in front of the existing `switch` action |
| 4 | **Split “Now serving” from “Model library”**: running models get one wide hero row with a big copyable endpoint and live metrics; stopped models get compact ~230 px cards. Status is structure, not just colour. | [01](img/01-dashboard.png) | `renderRecipes` / `kindsHtml` |
| 5 | **One primary action + ⋯ overflow** (Logs, Edit, Duplicate, Pin, Delete). Delete moves away from Edit and is disabled while running. | [01](img/01-dashboard.png), [03](img/03-drawer.png) | `actions` block in `cardHtml` |
| 6 | **Toolbar instead of the rail**: search + kind + status chips with counts + group/sort selects + view toggle on one line; ~280 px returned to content. Saved views remember filter sets. | [01](img/01-dashboard.png) | `index.html` `.filter-rail` → `.toolbar` |
| 7 | **Detail drawer on click**: full facts, variants, live metrics, log tail, in-place edit, all actions. Cards stay short because depth is one click away, not always on screen. | [03](img/03-drawer.png) | new drawer component; reuse the recipe editor for “Edit” |
| 8 | **Variants = one model, several engines**: group by *served name*, show “⇄ qwen3.8 · 3 engines” on the card and a variant switcher in the drawer. Your vLLM/SGLang/TensorFold trio becomes one decision instead of three cards. | [03](img/03-drawer.png), [04](img/04-switch.png) | `variantsOf()` on `served` name; optional group mode |
| 9 | **Boot time as decision data + live progress**: `~10m 58s` on the card, elapsed-vs-median bar while starting (you already collect `start_times`). | [05](img/05-boot.png) | existing `startInfo`, promoted in size/placement |
| 10 | **Findings on the card**: nemotron’s exit 137 becomes an amber “OOM kill — lower gpu-memory-utilization” chip instead of a silent `Exited (137)`. | [01](img/01-dashboard.png) | existing `findingChips`, always visible |
| 11 | **Density + bulk**: list view with sortable columns, multi-select, bulk stop/delete; drag-to-reorder in “custom” sort. | [02](img/02-list.png) | new view mode beside `.grid` |
| 12 | **Undo + inline activity**: every stop/switch/delete offers Undo; the last four events sit under the grid so cause and effect stay on one screen. | [01](img/01-dashboard.png) | toast system + `events` you already store |
| 13 | **Mobile & light theme**: single column, reachable primary actions, no horizontal overflow at 420 px; light palette keeps the same hierarchy. | [06](img/06-mobile.png), [07](img/07-light.png) | existing theme vars + responsive rules |

## Suggested implementation order (cheap → structural)

1. Toolbar + rail removal (HTML/CSS only, immediate space win).
2. Compact cards + serving/library split (`cardHtml` rewrite).
3. Host command strip (move + segment existing data).
4. Fit logic + switch preview (est-memory parsing + one dialog).
5. Drawer (new component; editor dialog reused for edits).
6. List view, bulk actions, saved views.
7. Variant grouping by served name.

Steps 1–4 alone fix most of the “not user friendly” feeling; 5–7 make it scale past ~10 recipes.