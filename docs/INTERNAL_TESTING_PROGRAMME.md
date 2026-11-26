# Bang On Pro 1.2.2: internal testing programme

**Goal:** measure, with real editors and real footage, whether the department cuts *human* editing time by 60–85 % at equal or better quality. Nothing in this repository has measured it yet: synthetic benchmarks only measure machine behaviour.

## Design: paired sessions

For each genre (podcast, gameshow, sports discussion, daily news), pick **two comparable episodes** of the same show, or one episode cut twice by different editors.

| Session | Who | How |
|---|---|---|
| Manual baseline | Editor A | Cuts the deliverable without the department (timing: *Manual baseline*) |
| Agent-assisted | Editor B (or A on the twin episode) | Runs a production, reviews, corrects and approves (timing: *Agent-assisted*) |

Use the same profile, the same deliverable spec and a finished quality bar agreed beforehand. Rotate editors between the two conditions across shows, to cancel out skill differences. At least **2 pairs per genre** for the first read; 5+ before drawing conclusions.

## What to record (in the app)

- **Start timing** at the first action and **Finish** when the deliverable is approved. Pause for breaks.
- Setup, review, correction and approval time are logged automatically. Machine processing time is logged separately.
- Your **1–5 quality rating** at Finish. A second editor rates both versions blind where possible.
- Corrective work is captured automatically: rejected agents, reruns, op-level edits and review items cleared.

## Export and report

- *Measurement → Report* (or `measure report`) gives, per deliverable:
  - manual vs assisted human time and % saved;
  - accepted and rejected decisions, and corrective work;
  - quality ratings, processing time and external cost.
  - It writes JSON and CSV. Pairs that are incomplete are shown as **not measured**.
- Send the report plus **Export diagnostics…** for any job that misbehaved.

## Acceptance criteria for the beta

1. Mean human-time saving ≥ 60 % across measured pairs in each priority genre.
2. Assisted quality rating ≥ the manual rating (no genre more than 0.3 lower).
3. No source media modified (checked automatically every job), no lost work after an interruption.
4. Corrective work trending down across episodes of the same series, which shows the approved preferences are helping.

The modelled estimate in the app (operation counts × assumed task times) is a planning aid only. **Never report it as a measured saving.**
