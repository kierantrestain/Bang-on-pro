# Bang On Pro V1.2.1 — Unslop design pass (native Qt)

Method: [unslop](https://github.com/heydamir/unslop) by Damir Sokolovsky ([@heydamir](https://x.com/heydamir)),
CC BY 4.0, adapted from web to native Qt/C++. The tokens became one QSS template plus a QPalette, and the
components became property roles. No web technology and no new dependencies were added. PR #5 (graphite
tokens, shorter Control Centre labels, primary-button property) was kept and built on.

## Direction

- **Product:** a native multicam editor in which a department of production agents assembles shows that editors correct and approve.
- **Character:** exact, unhurried, broadcast-grade.
- **World:** a broadcast control gallery at night, with graphite desks, monitor walls and tally lights.
- **Moments:** the footage in the monitors, the timeline and waveforms, the scopes, and the live agent run.
- **Signature:** tally indicators for agent and job state, plus mono tabular figures (job IDs, confidence, operation counts, summaries).

## Audit (before, real app, 1920×1080, populated project)

| Sign | Where | Fix |
|---|---|---|
| Window forced to 3163×1237 px on a 1920×1080 screen; right panels and the bottom of the timeline off-screen | All workspaces | Shared scroll container for dense docks; screen-fit clamp for restored geometry |
| AI Assistant dock minimum height 782 px; Premiere Presets minimum width 664 px | Dock content | `makeScrollableDockContent()` for AI Assistant, Transcript, Presets, Roto, Control Centre |
| Two competing stylesheets (global + `brandStyleSheet`) with different button rules | Theme, AI dock, workspace bar | One token-driven stylesheet; `brandStyleSheet()` is now an alias of the component layer |
| Orange "BANG ON PRO" tile with uppercase text | Workspace bar | Wordmark in sentence case with a 3 px accent mark |
| `AI PRO` badge, coloured status pills | AI Assistant header | Badge removed; status as tone-coloured text |
| Inline colour styles (`#FFB07C` at weight 700, `#22A06B` at weight 600, …) | Consent dialog, profile editor, shortcut presets, banners | `setTone()` roles; banners use a 2 px semantic left rule |
| Weights 600–700 on labels and buttons; 10–11 px controls | Global QSS | Weights 400/500, 12 px body, 11 px meta, 13 px titles |
| Blue selection colour beside the orange accent (two accents) | Palette | One accent family; selection is a darker accent shade, list rows use a neutral selected surface |
| Heavy orange-brown band on the selected track | Timeline QML | Selected-track tint 0.2 → 0.1 |
| Dock titles show accelerator markers ("C&lip Monitor") | Styled dock title bars | Markers stripped when a workspace is applied |
| Every column workspace collapsed into one tab group (V1.2 fix kept) | Workspaces | Anchors split before tabifying; screen-aware merge |
| Alt+Shift+1…9 workspace shortcuts never bound | Startup order | Menu actions created before the shortcut preset is applied |
| Title Case buttons, `+` joined labels, repeated "Bang On Pro —" in dock titles | Bang On Pro panels | Sentence case, plain words, short dock titles |
| Uppercase QC codes on markers (`QC [MIXED_FRAME_RATES]`) | Timeline markers | `QC · <message>`; code kept in marker parameters |
| Control Centre too short in AI First Cut; primary action below the fold | AI First Cut | Full-height right column; setup collapses during review; one primary action at a time |

## Passes

1. **Spacing.** 4 px grid tokens (`StudioMetrics`), 28 px controls and rows, and consistent 8 px gaps inside groups and 12 px around panels.
2. **Colour.** One graphite ramp from deepest to raised (`#0F0F10`, `#111113`, `#18181A`, `#222225`, `#2A2A2E`). Hairline `#2A2A2D`, three text roles (`#EDEDED`, `#A1A1A6`, `#85858B`), one accent (`#FF5A1F`) with dark text on it (5.8:1), and toned semantic colours (`#4CC38A`, `#D9A23A`, `#E5484D`).
3. **Icons.** Native Kdenlive and Breeze icons kept. The one new mark is the 12 px tally lamp (`tallyIcon()`).
4. **Copy.** Sentence case, no badges, short placeholders ("Series", "Episode title"), and plain review messages.
5. **Type.** The platform UI family at weights 400/500, with a mono partner for job IDs, revision and summary.
6. **Layout.** AI First Cut puts the production department in a full-height right column, every workspace fits 1280×800, and layouts persist through the existing workspace save/restore.
7. **Details.** Tabs use a 2 px accent underline, group boxes become hairline sections, checkboxes are 14 px, scrollbars are 10 px, focus outlines use the accent shade, and the progress bar is a 4 px line.
8. **Visuals.** Nothing decorative was added. Footage, timeline, waveforms and scopes carry the colour.
9. **Motion.** No animation was added. Native Qt feedback states only.

## Agent Control Centre

The panel now follows the workflow: **setup → start → monitor → review → approve**.

- **Header:** "Production", the job ID in mono and "New production".
- **Setup:** mode, profile, series, episode, brief, render and external budget, with **Start production** as the only primary action.
- **Run:** a 4 px progress line, the current step, and Cancel or Resume as appropriate.
- **Agents:** tally lamp, enable check (the Executive is fixed on), state in plain words ("Done · applied", "Waiting", "Done · needs approval"), right-aligned confidence and operation counts, engine (Rules / Neural / Fallback) and result.
- **Review:** Accept (approval tone), Reject (danger tone), Rerun, a "Review · N" list with tally markers, and the summary.
- **Approve:** once a draft exists, **Review in timeline** becomes the single primary action, and the setup collapses so review and approval stay in view.

## Verification

- Real application under Xvfb, with the populated V1.2 production draft open (4 cameras, graphics, colour, masks, QC markers).
- All nine workspaces captured at 1920×1080, 1440×900 and 1280×800, plus 2× scale (2560×1600 at `QT_SCALE_FACTOR=2`). In every capture the window equals the screen size.
- Layout diagnostics (`BANGONPRO_LAYOUT_DEBUG=1`): before, the window was 2587–3163 px wide with a 1022–1050 px minimum height; after, the minimum is 866×≤471 px.
- New regression tests:
  - single-source design system and tally colours;
  - dense docks can shrink, and the window fits the screen;
  - AI First Cut gets a full-height production column;
  - dock titles are free of accelerator markers;
  - the primary action hands over from Start to Review, and the setup collapses and reopens.
- Existing suites: C++ CTest 6/6 and Python QA 95/0/1.

Screenshots are in `docs/screenshots/v1.2.1/`: `before/`, `after_1920x1080/`, `after_1440x900/`, `after_1280x800/`, `after_2x/` and `agent_control_centre_detail.png`.

## Not changed or still open

- Timeline QML clip colours, monitor overlays and scope rendering are upstream Kdenlive drawing and were left as they are.
- Retina was verified with Qt scale factor 2 under Xvfb, not on a physical Retina display.
- Under a WM-less Xvfb, synthetic key events do not reach Kdenlive actions. The shortcut-binding fix is covered by code order and by the preset manager tests, not by a GUI key test.
