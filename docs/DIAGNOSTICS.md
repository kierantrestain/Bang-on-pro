# Bang On Pro 1.2.2: diagnostics

## Export (no terminal)

The *Bang On Pro* menu → *System check…* → *Export diagnostics…*. Choose where to save the zip, then answer **Include media?** The default answer is No. Send the zip to the Bang On Pro team.

Command line (support only): `bangonpro-department diagnostics export --out diag.zip [--job <job folder>]`

## What is included

- App and runtime version, macOS and hardware (chip, memory, GPU, free space on the job and cache volumes).
- The system check: media tools, bundled Python runtime, local model status, cache size.
- For the selected or most recent jobs: `job.json`, `plan.json`, `events.jsonl` and the summary (agent states, confidence, quality-loop results, errors, timings).
- A `manifest.json` listing every included file and every exclusion rule applied.

## Always excluded, sanitised by default

- Passwords, API keys, tokens, provider and consent settings. Secret-looking keys **and** values are replaced by `[redacted]`.
- Camera originals, audio, frames, mattes, transcripts and captions, unless you answered **Yes** to *Include media?*
- Your home folder path is shortened to `~`.

These rules are covered by automated tests (`tests/bangonpro/test_v122_readiness.py`), which plant a fake key and a media file and check that neither leaves the machine.

## Reading a report (team)

Start with `summary.json`: the failing agent, its `error` and its `review_items`. Then check `events.jsonl` for the order of events (including `quality_correction_*`), and the readiness section for missing tools, models or space. Useful errors are mapped to plain fixes in `department/errors.py`.
