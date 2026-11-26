# Bang On Pro V1.2 — AI Production Department

The production department is a set of **14 permanent specialist agents** that run *inside* Bang On Pro
(`bangonpro/ai_engine/department/`). They extend the V1.1 orchestrator and engine stages rather than
replacing them, share one project state and one content-addressed analysis cache, and emit every edit as a
validated **Timeline Command API v1** batch so all work stays editable in the native Kdenlive timeline.

> These production agents are distinct from the development agents used to build the application.

## Agents

| # | Agent | Responsibility | Engine (reused / new) | Compute tier |
|---|---|---|---|---|
| 1 | Executive Editing Agent | Interpret brief, choose agents, plan, verify, summarise | `planner.interpret_instruction` + activation rules | deterministic (LLM only if configured, consented, in budget) |
| 2 | Learning Agent | Apply approved series preferences; record corrections; propose improvements | `department.learning` (new) | deterministic |
| 3 | Media Ingest Agent | Probe, hash, label roles, detect missing media / mixed fps / low storage | `ingest_sync` probe + SHA-256 (shared memo) | deterministic |
| 4 | Synchronisation Agent | Align cameras, mics, recorders; import media | `ingest_sync` GCC-PHAT | deterministic |
| 5 | Editorial Intelligence Agent | Transcript, diarisation, sections, highlights | `transcribe_diarise` (faster-whisper when installed, else VAD) + highlight scoring (new) | local neural optional |
| 6 | Multicamera Director | Speaker→camera map repair, reaction/wide rules, camera plan | new planner feeding the multicam engine | deterministic |
| 7 | Rough Cut Agent | Assembly, setup-delay/dead-air/silence removal, pacing | `rough_cut_multicam` | deterministic |
| 8 | Audio Engineer | High-pass, noise-floor expander or RNNoise, compression, mic matching, EBU R128 | `audio_graphics_qc` audio sub-emitter | deterministic / RNNoise optional |
| 9 | Colourist | Per-camera matching, exposure, approved LUT | new frame statistics → native `lift_gamma_gain` (+ `avfilter.lut3d`) | deterministic |
| 10 | Captions Agent | Subtitles on the edited timeline (`.srt`/`.ass`) | `audio_graphics_qc` caption sub-emitter | needs neural transcript |
| 11 | Graphics Agent | Lower thirds, title card, branding, intro/outro | `audio_graphics_qc` + native `kdenlivetitle` | deterministic |
| 12 | VFX Agent | Subject segmentation, background blur/removal through native masks | `roto_studio.mask_engine` (SAM2 / MobileSAM / labelled fallback) | local neural optional |
| 13 | Quality Control Agent | Technical + continuity, jump cuts, coverage, caption speed, sync | `run_technical_qc` + department checks (new) | deterministic |
| 14 | Distribution Agent | Editable project, master, 2-pass loudness, social 9:16, highlight reel, manifest | `project_builder` + FFmpeg derivatives (new) | deterministic |

`python -m bangonpro.ai_engine.department agents` prints the machine-readable capability matrix.

## Automation modes

| Mode | What runs | Edits |
|---|---|---|
| `manual` | Executive plan only; agents run on request (`rerun <agent>`) | none until requested |
| `assisted` | Only agents the brief needs | every batch staged **pending approval** |
| `autonomous_first_cut` | ingest → sync → editorial → director → rough cut → audio → QC → distribution | auto-accepted into an **isolated draft** |
| `full_production_draft` | all agents the profile/brief need (colour, captions, graphics, VFX, deliverables) | auto-accepted into an **isolated draft** |

Approval is always required to **replace an existing edit** (`promote --approve`, timestamped backup) and to
**publish**. Original source media are opened read-only, never written, and re-hashed at the end of every job.

## Jobs, checkpoints and control

Every job lives in `<jobs>/<job_id>/` (`job.json`, `plan.json`, `events.jsonl`, `checkpoints/`, `draft/`,
`summary.md`). The CLI and the Agent Control Centre support:

```bash
python -m bangonpro.ai_engine.department run "Tight cut, match cameras, captions, social clips" \
  --mode full_production_draft --profile podcast_multicam --inputs cam_wide=wide.mp4 cam_host=host.mp4 \
  --series weekly_show --episode-title "Episode 12" --export
```

```bash
python -m bangonpro.ai_engine.department decide <job_dir> rough_cut reject --note "pacing too tight"
```

```bash
python -m bangonpro.ai_engine.department rerun <job_dir> colourist --profile-overrides '{"colour_rules":{"correct_exposure":false}}'
```

```bash
python -m bangonpro.ai_engine.department promote <job_dir> ~/Projects/episode12.kdenlive --approve
```

* **cancel / resume** – cooperative cancellation via the job's `CANCEL` file; resume restarts from the last
  checkpoint and never redoes completed agents.
* **rerun / refine** – re-executes an agent (and later agents) from the checkpoint before it, optionally with
  a validated profile overlay.
* **accept / reject** – per agent or per operation. Rejecting an *editorial* agent excludes dependent
  editorial/finishing work; QC and distribution are flagged *stale* for rerun (dependency-aware transactions).
* **rollback** – discard everything after an agent and restore its checkpoint.
* **Undo** – drafts sent to the timeline are applied through `TimelineCommandEngine` (native undo stack).

## Shared analysis and cost control

* SHA-256 and ffprobe results are memoised per (path, size, mtime) and persisted in the analysis cache;
  transcripts/diarisation are cached by media content hash + parameters, so re-edits reuse analysis
  (`cache stats|prune|clear`; LRU byte budget, default 2 GB, `BANGONPRO_CACHE_DIR`).
* Local Whisper is selected automatically when a runnable model is installed (`--asr-model` to choose).
* External providers are optional, consent-gated (V1.1 `ExternalConsentError`) and bounded by `--budget-usd`;
  every agent reports `est_external_cost_usd`.

## Reusable production intelligence

Profiles (`podcast_multicam`, `gameshow_multicam`, `sports_discussion`, `daily_news`,
`interview_two_camera`, `social_short`, custom) now carry `agent_config`, `colour_rules`, `vfx_rules`,
`distribution_rules` and `series`. The Learning Agent stores human corrections per profile/series
(`~/.local/share/bangonpro/preferences/`), proposes bounded suggestions (`learn suggest`, `learn diff
--ai-project --final-project`), and only applies them after approval as a new immutable version
(`learn approve|reject|versions|rollback|reset`). Learnable keys are whitelisted and clamped; approval
rules, providers, code and model weights can never be changed by learning.

## Agent Control Centre (Qt)

`src/bangonpro/ui/AgentControlCentre.{h,cpp}` — raised in the **AI First Cut** workspace. Mode, profile,
series, episode, brief, render and external budget; per-agent enable; live status, confidence, operation
count, engine (deterministic / neural / fallback) and reason per agent; *Needs your attention*
notifications; Accept / Reject / Rerun Agent; Cancel / Resume; Review Draft in Timeline (one reviewable,
undoable proposal with per-operation agent attribution); Open Draft Project; Promote (approval to replace).
After a restart the most recent job is recovered; an interrupted job is shown as resumable.

## V1.4: agents that edit

The 14 agents can now be driven by the editor's own Claude Code or Codex through the MCP adapter
(`python -m bangonpro.ai_engine.mcp`). They inspect footage (transcript, project index, contact
sheets, rendered previews, loudness), edit the draft with native operations as the agent that owns
each change, and finish a brief with `bop_task complete`, where Quality Control verifies the actual
draft and corrects high-confidence defects. Without a client nothing changes. See
[AGENT_CONNECTION_GUIDE.md](AGENT_CONNECTION_GUIDE.md) and [V1.4_RELEASE_REPORT.md](V1.4_RELEASE_REPORT.md).
