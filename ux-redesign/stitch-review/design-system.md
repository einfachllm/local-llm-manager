---
name: "Local LLM Manager — Studio Hardware Rack"
description: "Tactile, Braun/Dieter-Rams-inspired warm stone hardware rack interface for managing local AI models without developer jargon or text walls."
---

## Overview

Local LLM Manager is a calm, physical-feeling control surface for people running local AI models on their own hardware. Inspired by Dieter Rams industrial design and studio audio hardware racks, it replaces typical dark-navy AI dashboards with warm anodized stone panels, crisp hairline borders, segmented LED-style memory meters, and plain-English controls.

## Colors

The palette uses warm stone and paper neutrals (`#EDEBE4`, `#F7F6F2`, `#E2DFD5`) paired with deep charcoal ink (`#1A1917`), Braun-inspired signal orange (`#D94E28`) for primary actions, and hardware LED green (`#15803D`) strictly for live serving models and healthy memory states.

```yaml
colors:
  light:
    primary: "#C2411D"
    on-primary: "#FFFFFF"
    primary-container: "#FDE8E1"
    on-primary-container: "#7C2D12"
    secondary: "#1A1917"
    on-secondary: "#F7F6F2"
    secondary-container: "#E2DFD5"
    on-secondary-container: "#1A1917"
    success: "#15803D"
    on-success: "#FFFFFF"
    success-container: "#DCFCE7"
    on-success-container: "#14532D"
    warning: "#B45309"
    on-warning: "#FFFFFF"
    warning-container: "#FEF3C7"
    on-warning-container: "#78350F"
    error: "#B91C1C"
    on-error: "#FFFFFF"
    error-container: "#FEE2E2"
    on-error-container: "#7F1D1D"
    background: "#EDEBE4"
    on-background: "#1A1917"
    surface: "#EDEBE4"
    surface-container-lowest: "#F7F6F2"
    surface-container-low: "#F7F6F2"
    surface-container: "#EAE8E1"
    surface-container-high: "#E2DFD5"
    surface-container-highest: "#D6D2C7"
    on-surface: "#1A1917"
    on-surface-variant: "#5C5951"
    outline: "#8C887E"
    outline-variant: "#CFCBC0"
  dark:
    primary: "#FF7A52"
    on-primary: "#2D0C03"
    primary-container: "#7C2D12"
    on-primary-container: "#FFDBD0"
    secondary: "#E2DFD5"
    on-secondary: "#1A1917"
    secondary-container: "#2C2A26"
    on-secondary-container: "#E2DFD5"
    success: "#4ADE80"
    on-success: "#052E16"
    success-container: "#14532D"
    on-success-container: "#BBF7D0"
    warning: "#FBBF24"
    on-warning: "#451A03"
    warning-container: "#78350F"
    on-warning-container: "#FEF3C7"
    error: "#FCA5A5"
    on-error: "#450A0A"
    error-container: "#7F1D1D"
    on-error-container: "#FECACA"
    background: "#181715"
    on-background: "#F2F0EA"
    surface: "#181715"
    surface-container-lowest: "#121110"
    surface-container-low: "#1F1E1B"
    surface-container: "#262421"
    surface-container-high: "#2E2C28"
    surface-container-highest: "#383530"
    on-surface: "#F2F0EA"
    on-surface-variant: "#A8A49B"
    outline: "#706C64"
    outline-variant: "#33312C"
```

## Typography

Three crisp technical typefaces give hardware-grade legibility:
- **Space Grotesk** (`headline`): Geometric hardware faceplate headings and numeric telemetry readouts.
- **Inter** (`body`): Clean, neutral UI labels and plain-English descriptions.
- **JetBrains Mono** (`mono`): Tabular monospace for API endpoints, memory sizes, and port numbers.

```yaml
typography:
  display-lg:
    fontFamily: "Space Grotesk"
    fontSize: 36px
    fontWeight: 700
    lineHeight: 44px
    letterSpacing: -0.5px
  display-lg-mobile:
    fontFamily: "Space Grotesk"
    fontSize: 28px
    fontWeight: 700
    lineHeight: 34px
    letterSpacing: -0.25px
  headline-lg:
    fontFamily: "Space Grotesk"
    fontSize: 24px
    fontWeight: 700
    lineHeight: 32px
    letterSpacing: -0.25px
  headline-md:
    fontFamily: "Space Grotesk"
    fontSize: 18px
    fontWeight: 600
    lineHeight: 24px
    letterSpacing: 0px
  headline-sm:
    fontFamily: "Space Grotesk"
    fontSize: 15px
    fontWeight: 600
    lineHeight: 20px
    letterSpacing: 0px
  body-lg:
    fontFamily: "Inter"
    fontSize: 16px
    fontWeight: 400
    lineHeight: 24px
    letterSpacing: 0px
  body-md:
    fontFamily: "Inter"
    fontSize: 14px
    fontWeight: 400
    lineHeight: 20px
    letterSpacing: 0px
  body-sm:
    fontFamily: "Inter"
    fontSize: 12px
    fontWeight: 400
    lineHeight: 16px
    letterSpacing: 0px
  label-lg:
    fontFamily: "Inter"
    fontSize: 14px
    fontWeight: 600
    lineHeight: 20px
    letterSpacing: 0px
  label-md:
    fontFamily: "Inter"
    fontSize: 12px
    fontWeight: 600
    lineHeight: 16px
    letterSpacing: 0.2px
  label-sm:
    fontFamily: "Inter"
    fontSize: 11px
    fontWeight: 600
    lineHeight: 14px
    letterSpacing: 0.4px
  mono-md:
    fontFamily: "JetBrains Mono"
    fontSize: 13px
    fontWeight: 500
    lineHeight: 18px
    letterSpacing: 0px
  mono-sm:
    fontFamily: "JetBrains Mono"
    fontSize: 12px
    fontWeight: 500
    lineHeight: 16px
    letterSpacing: 0px
```

## Layout & Spacing

A single-column centered hardware console (`max-width: 1240px`) with a horizontal segmented rack navigation bar at the top—avoiding cluttered sidebars and symmetric card grids in favor of crisp horizontal rack modules.

```yaml
spacing:
  gutter-xs: 4px
  gutter-sm: 8px
  gutter-md: 16px
  gutter-lg: 24px
  gutter-xl: 32px
  section: 28px
  page-margin: 24px
```

## Elevation & Depth

Flat, tactile hardware panels separated by `1px` `outline-variant` borders and tonal surface steps (`surface` -> `surface-container-low` -> `surface-container-high`), with zero drop shadows. The active model bay is framed by a `2px` `secondary` border to anchor visual focus.

## Shapes

Restrained, precision-machined corners (`4px`–`8px`) rather than pill-heavy SaaS cards.

```yaml
rounded:
  sm: 4px
  DEFAULT: 6px
  md: 8px
  lg: 12px
  full: 9999px
```

## Motion & Micro-interaction

Immediate, mechanical state switches (`100ms` ease-out) with no decorative loops or bounce.

```yaml
motion:
  state-change:
    duration: 100ms
    delay: 0ms
    timingFunction: ease-out
  surface-enter:
    duration: 200ms
    delay: 0ms
    timingFunction: cubic-bezier(0.2, 0, 0, 1)
```

## Voice & Content Strategy

- **Tone:** Direct, calm, and physical. Explain what an action does in one short line.
- **No Text Walls:** Hide raw Docker commands, environment variables, and bash scripts behind an explicit `Advanced` toggle.
- **Numbers:** Always `tabular-nums` for memory, speed, ports, and temperatures.

## Components

- `components/button.html` — Primary (signal orange), secondary (hardware panel), destructive, and icon buttons
- `components/card.html` — Hardware telemetry bay, active unit bay, and rack list module
- `components/badge.html` — LED status pills and numeric host counters
- `components/searchbar.html` — Rack filter input
