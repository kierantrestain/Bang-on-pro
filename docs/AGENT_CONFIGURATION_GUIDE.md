# Bang On Pro 1.2.2: agent configuration guide

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

Six built-in profiles, each tuned for its genre:

| Profile | Loudness target |
|---|---|
| `podcast_multicam` | −16 LUFS (online) |
| `gameshow_multicam` | −23 LUFS (EBU R128) |
| `sports_discussion` | −23 LUFS (EBU R128) |
| `daily_news` | −23 LUFS (EBU R128) |
| `interview_two_camera` | −16 LUFS |
| `social_short` | −14 LUFS |

Change a target if your delivery is online rather than broadcast. To make your own profile, start from a built-in one (*profile create --base*), validate it and save it to your profiles folder. It then appears in the Production panel.

Key sections: `cameras`, `camera_roles`, `speaker_camera_map` (speaker names drive lower thirds and captions), `min/max_shot_duration_sec`, `reaction_shot_frequency`, `wide_shot_interval_sec`, `audio_rules`, `caption_style`, `graphics_rules`/`graphics_templates`, `colour_rules`, `vfx_rules`, `distribution_rules`, `agent_config.disabled_agents`.

## New V1.2.2 settings (all optional; safe defaults per format)

| Setting | Agent | Meaning |
|---|---|---|
| `editing_rules.pause_policy` | Rough Cut | `compress` (default: long pauses shortened to a natural beat), `remove`, or `keep` |
| `editing_rules.natural_pause_sec`, `turn_pause_sec`, `min_pause_saving_sec`, `protected_pause_max_sec` | Rough Cut | Pause lengths kept inside a turn, between speakers, the smallest saving worth a cut, and how long a protected pause may stay |
| `editing_rules.protect_question_pauses`, `protect_punchline_pauses`, `protect_reaction_pauses` | Rough Cut | Keep the beat after a question, before a punchline, and during laughter or applause (default on) |
| `editing_rules.remove_false_starts`, `remove_stutters`, `remove_repeated_phrases`, `remove_fillers` | Rough Cut | Word-level clean-up (needs a Whisper transcript); "keep every word" in the brief turns these off |
| `caption_style.terminology` | Captions | Names and terms to enforce (e.g. `["Bang On Pro", "Arsenal"]`); speaker names are added automatically |
| `caption_style.max_chars_per_line`, `max_lines`, `max_chars_per_sec`, `min_duration_sec`, `speaker_change_marker` | Captions | Readability rules |
| `graphics_rules.lower_third_min_sec` / `max_sec` / `reshow_after_sec`, `topic_title_cards` | Graphics | Lower-third timing, re-show after absence, and topic cards (news) |
| `colour_rules.preserve_look`, `shot_level`, `reference_camera`, `correct_exposure` | Colourist | Keep an approved look; correct per shot when lighting changes |
| `audio_rules.declip`, `tone_match`, `enable_mic_level_matching` | Audio Engineer | Clipping repair and microphone matching |

## Quality loop (automatic, bounded)

After the rough cut, director, sync, audio and colour agents run, the department scores their result against defects an editor would have to fix. It can try **one or two** bounded corrections. A correction is kept only if it measurably improves the score, introduces **no new critical defect**, and an independent QC check does not get worse. Every attempt's time and cost counts toward the job. Judgement calls and low-confidence decisions go to **Needs your attention**.

## Learning and sharing preferences

Your accept and reject decisions and final edits produce *suggestions*. Nothing changes until you approve one, and each approval creates a new version you can roll back. **Export** writes a checksummed bundle of approved preferences for another editor, and **Import** adds them as pending suggestions, never applied automatically. Learning can never change code, models, approval rules or providers.

## External AI providers

They're off by default and offline operation is the norm. A provider only runs with explicit consent, within the job's budget. Every attempt, including rejected quality-loop corrections, is charged to the budget.
