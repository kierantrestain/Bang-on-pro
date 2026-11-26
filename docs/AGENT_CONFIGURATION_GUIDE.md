# Bang On Pro 1.3: agent configuration guide

You configure the department through the **production profile**, using the profile editor or a JSON file. Per-job choices go in the Production panel. Approved series preferences sit on top of the profile.

## Modes

| Mode | Agents | Edits |
|---|---|---|
| Manual | Plan only; an agent runs when you rerun it | Nothing until requested |
| Assisted | Only what the brief needs | Every batch waits for approval |
| Autonomous first cut | Ingest → sync → editorial → director → rough cut → audio → QC → distribution | Auto-accepted into an isolated draft |
| Full production draft | Adds colour, captions, graphics, VFX and deliverables as the brief or profile asks | Auto-accepted into an isolated draft |

Replacing an existing edit and publishing always need approval. Source media is read-only and is checksummed after every job.

## Profiles

Six built-in profiles, each tuned for its genre and **all delivering to YouTube at −14 LUFS integrated, −1 dBTP** (V1.3): `podcast_multicam`, `gameshow_multicam`, `sports_discussion`, `daily_news`, `interview_two_camera`, `social_short`.

A custom profile or an approved series preference can still set its own target. To make your own profile, start from a built-in one (*profile create --base*), validate it and save it to your profiles folder. It then appears in the Production panel.

Key sections: `cameras`, `camera_roles`, `speaker_camera_map` (speaker names drive lower thirds and captions), `min/max_shot_duration_sec`, `reaction_shot_frequency`, `wide_shot_interval_sec`, `audio_rules`, `caption_style`, `graphics_rules`/`graphics_templates`, `colour_rules`, `vfx_rules`, `distribution_rules`, `agent_config.disabled_agents`.

## New V1.2.2 settings (all optional; safe defaults per format)

| Setting | Agent | Meaning |
|---|---|---|
| `editing_rules.pause_policy` | Rough Cut | `compress` (default: long pauses shortened to a natural beat), `remove`, or `keep` |
| `editing_rules.natural_pause_sec`, `turn_pause_sec`, `min_pause_saving_sec`, `protected_pause_max_sec` | Rough Cut | Pause lengths kept inside a turn, between speakers, the smallest saving worth a cut, and how long a protected pause may stay |
| `editing_rules.beat_pause_sec` (V1.3) | Rough Cut | A pause under 2 s inside one speaker's turn is a thinking beat. It is kept at this length (podcast 0.85 s, interview 0.9 s, gameshow and sports 0.75 s, news 0.5 s, social 0.35 s), even without a Whisper transcript |
| `editing_rules.protect_question_pauses`, `protect_punchline_pauses`, `protect_reaction_pauses` | Rough Cut | Keep the beat after a question, before a punchline, and during laughter or applause (default on) |
| `editing_rules.remove_false_starts`, `remove_stutters`, `remove_repeated_phrases`, `remove_fillers` | Rough Cut | Word-level clean-up (needs a Whisper transcript); "keep every word" in the brief turns these off |
| `caption_style.terminology` | Captions | Names and terms to enforce (e.g. `["Bang On Pro", "Arsenal"]`); speaker names are added automatically |
| `caption_style.max_chars_per_line`, `max_lines`, `max_chars_per_sec`, `min_duration_sec`, `speaker_change_marker` | Captions | Readability rules |
| `graphics_rules.lower_third_min_sec` / `max_sec` / `reshow_after_sec`, `topic_title_cards` | Graphics | Lower-third timing, re-show after absence, and topic cards (news) |
| `colour_rules.preserve_look`, `shot_level`, `reference_camera`, `correct_exposure` | Colourist | Keep an approved look; correct per shot when lighting changes |
| `audio_rules.declip`, `tone_match`, `enable_mic_level_matching` | Audio Engineer | Clipping repair and microphone matching |

## Conversational revisions (V1.3)

`Revise` (Production panel) or `bangonpro-department revise <job> "<instruction>" [--range a b | --range-timeline a b]`.

| Instruction (examples) | What changes | Reruns from |
|---|---|---|
| tighten / faster / trim (… but keep the natural conversation) | Pause, turn and beat lengths: full tighten, or a gentle one if the conversation is to be kept | Rough Cut |
| keep the natural conversation / looser | Longer pauses and beats | Rough Cut |
| preserve the suspense / reveals / dramatic pauses | Longer protected beats before reveals and reactions | Rough Cut |
| keep every word / remove fillers | Word-level clean-up off or on | Rough Cut |
| more / fewer reaction shots | `reaction_shot_frequency` ± 0.15 | Multicamera Director |
| fewer cuts / more dynamic / more wides | Minimum shot length, wide interval | Multicamera Director |
| graphics / lower thirds / captions | Graphics or Captions reran on the revised timeline | Graphics / Captions |
| *N* vertical clips | `distribution_rules.social_clip_count` | Editorial Intelligence (cached) |

How the scope works:

- A range such as "the opening", "the ending", "from 1:20 to 3:00" or the timeline selection limits the change. Outside it, and inside locked or earlier revised ranges, the previous edit is kept verbatim.
- `lock <job> a b` protects a range from all later revisions.
- `undo-revision` restores the previous draft exactly.
- Changes are recorded as corrections for series learning, but only approved suggestions change the series profile.

## Quality loop (automatic, bounded)

After the rough cut, director, sync, audio and colour agents run, the department scores their result against defects an editor would have to fix. It can try **one or two** bounded corrections. A correction is kept only if it measurably improves the score, introduces **no new critical defect**, and an independent QC check does not get worse. Every attempt's time and cost counts toward the job. Judgement calls and low-confidence decisions go to **Needs your attention**.

## Learning and sharing preferences

Your accept and reject decisions and final edits produce *suggestions*. Nothing changes until you approve one, and each approval creates a new version you can roll back. **Export** writes a checksummed bundle of approved preferences for another editor, and **Import** adds them as pending suggestions, never applied automatically. Learning can never change code, models, approval rules or providers.

## External AI providers

They're off by default and offline operation is the norm. A provider only runs with explicit consent, within the job's budget. Every attempt, including rejected quality-loop corrections, is charged to the budget.
