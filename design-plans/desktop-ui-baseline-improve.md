# Desktop UI baseline audit and improvement plan

Written against: `agent-router` current working tree

## Design language

- Audited surface: `agent-router/desktop/web/index.html` rendered by Rust/Tauri via loopback UI
- Design sources: `baseline-ui`, `improve-ui`, `apple-liquid-glass` tokens/patterns
- Documented decisions: Apple light grey-white ground; unified white surfaces; glass only on sticky nav/overlay; restraint; hierarchy by weight/size/grayscale
- Governing owners and consumers: `desktop/web/styles.css`, `desktop/web/index.html`, `desktop/web/app.js`
- Explicit exceptions: None documented

## Findings

| # | Problem | Evidence | Proposed change | Scope | Confidence |
| --- | --- | --- | --- | --- | --- |
| 1 | Card buttons are not bottom-aligned across siblings; hero/server cards still drift vertically | `styles.css` sets `.card{height:auto}` utilities but individual card bodies and server-summary content change the available bottom space | Make every card a flex column and pin the final `.form-actions`/`.hero-actions` with `margin-top:auto` | Overview, worklog, tech-stack, settings cards | High |
| 2 | Dense settings rows have uneven touch targets and weak spacing scale | Switching CSS uses ad-hoc `11px/16px/18px` rows and small 38px buttons while baseline requires 44px hit targets and 4px grid spacing | Normalize rows to 44px minimum, keep only 4/8/12/16/24/32 spacing | Settings, worklog, local agent pages | High |
| 3 | Overview status blocks rely on several tinted fills and colored labels, reducing Apple restraint | Metrics, badges and summary items use multiple soft tint backgrounds; Apple rule prefers grayscale body and one accent | Keep body grayscale, reserve blue `#0071e3` for primary actions and green only for live online state | Overview, server, worklog, tech-stack | Medium |

## Improve first

Finding 1: equal-height card action baselines. It fixes the most visible misalignment in the screenshot and is low risk.

## Plan: equal-height card actions

Written against: current working tree

### Evidence chain

- Surface: `desktop/web/index.html` + `desktop/web/app.js` overview/worklog/tech-stack pages
- Problem: Same-row cards have different content heights, so their action buttons do not share a baseline.
- Design evidence: Apple `design-system.md` unified surfaces and panel behavior; baseline-ui 4px spacing and fixed layout discipline
- Owner: `desktop/web/styles.css`
- Scope and affected surfaces: `.card`, `.grid-hero`, `.grid-2`, `.grid-3`, `.card-body`, `.hero-card`, `.server-summary`, `.engine-grid`, `.plugin-grid`
- Uncertainty: none that blocks CSS-only alignment; visual check still required after build

### Design decision

Make every grid card a flex column, reserve the final action row at the bottom with `margin-top:auto`, and keep card borders/shadows unified. This preserves Apple restraint while making sibling cards end on the same baseline.

### Reuse

- `--sh-card`, `--sh-panel`, `--hairline`, `--text`, `--text-2`, `--accent`
- Exemplar: Apple unified panel list pattern in `components.md`

### Changes

1. `desktop/web/styles.css`
   - Change: `.card{display:flex;flex-direction:column}`; `.card-body{flex:1;display:flex;flex-direction:column}`
   - Change: pin final `.form-actions`/`.hero-actions` to bottom
   - Change: normalize `.btn-sm` to 36px minimum and input-group buttons to 38px
   - Preserve: functional behavior, text content, and routing
   - Verify: overview/worklog/tech-stack action buttons sit on the same row baseline in a 1400px viewport

### Scope

- Inherit: all `.card` consumers across pages
- Verify: `overview`, `local`, `worklog`, `techstack`, `engines`, `workspaces`, `server`, `settings`
- Exclude: Tauri/Rust behavior changes; no new dependencies or product logic

### Validation

- Product: user can read two same-row cards without one bottom edge visibly drifting
- Interface: desktop viewport 1400×900; narrow 900px fallback collapses to one column
- System: no new color tokens, no gradient, no parallel component pattern
- Repository: `node --check desktop/web/app.js` → pass; open local UI → cards/buttons aligned

### Stop conditions

- Stop if a page needs unequal card heights as intentional layout, or if a card has multiple action groups that cannot share one bottom baseline.
