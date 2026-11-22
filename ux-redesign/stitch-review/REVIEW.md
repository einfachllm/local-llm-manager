# Review — Stitch "Studio Hardware Rack" redesign package

> **Status: implemented.** The full Rack redesign now lives in the real app
> (`local_llm_manager/static/`, changelog under *[Unreleased]*): palette + typography + rack chrome
> in `style.css`, the guided wizard in `wizard.js`/`wizard.css`, vendored offline fonts in `fonts/`.
> Renders of the **real** app: [`img/rack-dark.png`](img/rack-dark.png),
> [`img/rack-light.png`](img/rack-light.png), [`img/rack-mobile.png`](img/rack-mobile.png),
> wizard steps [`1`](img/rack-wizard-1.png) [`2`](img/rack-wizard-2.png) [`3`](img/rack-wizard-3.png).
> Re-verify any time: `NODE_PATH=… node ../verify-live.cjs` — 40 state-driven, non-destructive checks.
> The original review of the design package follows.

> Source: `stitch_local_llm_ux_redesign.zip` (9 HTML screens + design-system spec).
> Renders of every screen (1600 px, headless Chromium, Tailwind CDN loaded) are in [`img/`](img);
> the token spec is copied to [`design-system.md`](design-system.md).
> Comparison baseline: the **current live app** ([`img/current-live.png`](img/current-live.png)),
> which already implements the earlier UX overhaul (`ux-redesign/RECOMMENDATIONS.md`, shipped in 0.4.0).

## What's in the package

Three explorations, one of them developed into a full 7-screen system:

| Exploration | Screens | Verdict |
|---|---|---|
| **Studio Hardware Rack** (warm stone, Braun/Rams-inspired) | Recipes, Hosts, Schedules, Activity, Images, Weights, Add-Recipe wizard | The intended direction — coherent, complete |
| Initial dark dashboard | 1 rich screen | Closest to today's app; richest *information design* of the three |
| Swiss editorial control plane | 1 screen | Editorial/typographic experiment; weakest fit for a control surface |

## Design system quality

The token spec ([`design-system.md`](design-system.md)) is unusually complete and implementation-ready:

- **Full light + dark palettes** (warm stone `#EDEBE4` / charcoal `#181715`, signal orange `#C2411D`/`#FF7A52`, LED green reserved strictly for *live serving* and healthy memory).
- **Three typefaces with a size/weight/line-height scale**: Space Grotesk (headings, telemetry), Inter (labels), JetBrains Mono (endpoints, ports, sizes). All numeric readouts `tabular-nums`.
- **Flat tactile panels**: 1 px hairline borders + tonal surface steps, zero shadows, 4–8 px radii, active bay framed by a 2 px border. 100 ms mechanical state changes, no bounce.
- **Content strategy**: plain English, one-line explanations, raw Docker/env/scripts hidden behind an explicit *Advanced* toggle. This matches the project's existing "no text walls" direction.

## Screen-by-screen

### Recipes — [`img/recipes_studio_hardware_rack.png`](img/recipes_studio_hardware_rack.png)
Horizontal segmented rack nav replaces the sidebar/topbar tabs; "Now serving" gets a 2 px-framed hero bay with endpoint + Copy; stopped models become compact rack rows with **Start / Switch** as the single primary action and a `⋯` overflow. Segmented LED-style VRAM meter up top. Structurally this is the *same* information architecture we already shipped (host strip → serving hero → library), re-skinned as hardware. Good: the LED segmentation reads instantly; risk: the light-stone default is a big brand departure from the current dark app.

### Hosts & endpoints — [`img/hosts_endpoints_studio_hardware_rack.png`](img/hosts_endpoints_studio_hardware_rack.png)
Per-host bay with memory meter, GPU load/temp/power, and the endpoint list beneath. Maps cleanly onto `/api/hosts`. The mock's "endpoints" table duplicates what the recipe rows already show — in the real app endpoints belong to recipes, so this screen should stay host-telemetry-first.

### Add-Recipe wizard — [`img/add_recipe_guided_wizard.png`](img/add_recipe_guided_wizard.png)
**The strongest new idea in the package.** 3 steps (Model & Weights → Engine & Port → Review), weights picker with size + **"Fits"** badges against free memory, engine choice with a *Recommended* marker, auto-derived endpoint preview, and the generated container command collapsed under *Advanced*. Today's editor is a full form dialog; this wizard would genuinely lower the barrier for non-developers. It reuses data we already have (`/api/weights`, est-memory parsing, engine templates).

### Schedules / Activity / Images / Weights — [`img/schedules…`](img/schedules_studio_hardware_rack.png), [`…activity…`](img/activity_studio_hardware_rack.png), [`…images…`](img/images_studio_hardware_rack.png), [`…weights…`](img/weights_studio_hardware_rack.png)
Consistent rack-list modules; all four map 1:1 to existing tabs and APIs. Activity keeps the inline cause→effect timeline we already have. Nothing structurally new, but the consistency proves the system scales past the dashboard.

### Dark dashboard exploration — [`img/explored_initial_dark_dashboard.png`](img/explored_initial_dark_dashboard.png)
Same IA again, but with details worth stealing regardless of skin: filter chips with counts (`All (6) / Running (1) / Stopped (5)`), "Replaces current" hint on every Switch button, live `tok/s` + `cache hit %` + uptime in the serving bay, and the explicit line *"Switching automatically stops the active model to free memory."*

## How it compares to the shipped UI

The current app ([`img/current-live.png`](img/current-live.png)) already has: host command strip, serving/library split, fit-aware Start/Switch, drawer, overflow menus, toolbar filters, undo. **The Stitch package does not add a new information architecture — it adds a new skin plus one new flow (the wizard).** That means adoption can be incremental and low-risk:

1. **Token swap** — port the palette/type/spacing tokens into `style.css` as a third theme ("Rack", light + dark variants) beside the existing themes. No JS changes; instant A/B.
2. **Wizard** — build the 3-step Add-Recipe flow on top of the existing editor dialog and weights API. Highest user value, independent of the skin decision.
3. **Rack chrome** (only if the skin is adopted) — segmented nav, LED memory meter, 2 px active-bay framing, Space Grotesk/JetBrains Mono via bundled fonts (the mocks load Tailwind + Google Fonts from CDNs; the real app must stay offline-capable).

## Risks / gaps to resolve before implementing

- **CDN dependence**: every mock uses `cdn.tailwindcss.com` + Google Fonts + Material Symbols. The manager runs on offline LAN boxes — all of it must be vendored/compiled.
- **Mock data ≠ real data**: mocks show tidy single-host numbers; real state has NULL `est_memory_gb`, multi-host agents, starting/crashed states, findings chips. The rack rows need the same state coverage the current cards have (the 36-check `verify-live.cjs` suite is the acceptance gate).
- **Light-first default**: the package's primary palette is warm light stone; current users know a dark app. Ship it as a theme, not a replacement, until preferred.
- **Icon set**: Material Symbols vs. the current inline SVGs — pick one; mixing looks broken.
- The wizard's "Background Agent" kind must map to the existing `kind` field semantics, not invent a new one.

## Recommendation

Adopt in the order **wizard → token theme → rack chrome**, gated by the existing live-verification suite. The package is a well-specified, coherent visual system — its main contribution is the tactile identity and the guided setup flow, not new structure.
