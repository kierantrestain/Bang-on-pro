# Connecting Claude Code, Codex, Grok Build or Antigravity to the production agents (V1.5)

Bang On Pro's 14 production agents use the reasoning of the AI client **you already have and are signed in to**.
Nothing is bundled, downloaded or billed by Bang On Pro: no model, no API key, no extra account.
Without a client the department works exactly as in V1.3 (offline, deterministic).

## 1. Connect a client to a production job

Every department job has a draft. The MCP server edits that draft (never your source media and never a promoted project).

**Claude Code**

```bash
python3 -m bangonpro.ai_engine.mcp config --client claude --job ~/BangOnPro/jobs/<job_id> --allow ~/Footage/MyShow/graphics --apply
```

This runs Claude Code's own `claude mcp add` for your user. Without `--apply`, it prints the command so you can run it yourself.
`--allow` names the folders whose B-roll and graphics the agents may place. Repeat it for more folders.

**Codex**

```bash
python3 -m bangonpro.ai_engine.mcp config --client codex --job ~/BangOnPro/jobs/<job_id> --allow ~/Footage/MyShow/graphics --apply
```

This writes a `[mcp_servers.bangonpro]` section to `~/.codex/config.toml` and leaves every other line as it was.

**Grok Build and Google Antigravity**

```bash
python3 -m bangonpro.ai_engine.mcp config --client grok --job ~/BangOnPro/jobs/<job_id> --allow ~/Footage/MyShow/graphics
```

Use `--client agy` for Antigravity. This prints a standard stdio `mcpServers` entry. Add it in that client's MCP settings. Bang On Pro does not edit those files, because their locations were not confirmed in official documentation.
The server runs locally over stdio, and nothing is opened to the network. **Grok Bot** (on the web or X) can only reach remote MCP servers. Because of that, it is not connected to the local editor, and Bang On Pro offers no public endpoint or tunnel.

A connected client controls its own session: its sign-in, limits and model. Bang On Pro never switches providers for it. Automatic failover (section 2) applies only to reasoning that Bang On Pro itself asks for.

Then ask the client in plain words. For example: *"In the guest's longest answer, cut to the host listening once, put the city B-roll near the end with a short fade, and title the opening 'Live from the studio'."*

The client inspects the timeline, looks at frames when it needs to, and edits as the agent that owns each change.
It asks Quality Control to verify the actual draft, and reports back. Read-only tools are marked as read-only, so your client can allow them without asking you.

## 2. Instructions typed in Bang On Pro, with automatic provider failover (optional)

The **Revise** field in the Agent Control Centre still uses the V1.3 rules first. Next, a **learned skill** that matches the instruction is tried. It runs without any reasoning call, Quality Control verifies it, and it is undone if it fails.
Only after that does the Executive ask an **authorised** CLI for a short plan, which the agents carry out and Quality Control verifies. You can authorise up to four providers, and all of them can be authorised at once:

```bash
python3 -m bangonpro.ai_engine.mcp authorise --client claude
```

```bash
python3 -m bangonpro.ai_engine.mcp authorise --client codex
```

```bash
python3 -m bangonpro.ai_engine.mcp authorise --client grok
```

```bash
python3 -m bangonpro.ai_engine.mcp authorise --client agy --priority claude codex grok agy
```

| Provider | Headless call Bang On Pro makes (your existing sign-in) |
|---|---|
| Claude Code | `claude -p … --output-format json --max-turns 1` |
| OpenAI Codex | `codex exec --skip-git-repo-check --sandbox read-only --output-last-message …` |
| Grok Build | `grok --no-auto-update -p … --output-format json --max-turns 1` (shell, edits, writes, web and subagents denied) |
| Google Antigravity | `agy -p … --output-format json` (falls back to a pseudo-terminal if the CLI prints nothing to a pipe) |

- **One provider at a time**, in your order. Show the providers, or change their order:

  ```bash
  python3 -m bangonpro.ai_engine.mcp providers --priority codex claude
  ```

- **Failover only for availability.** Exhausted usage, rate limits and temporary outages switch to the next provider. Authentication problems, a plan without access, and an incompatible CLI also switch, but that provider is then parked until you authorise it again. A bad request or an unusable answer does not switch.
- **Cooldowns follow the reported reset time.** Bang On Pro uses the reset time a provider reports (for example *"usage limit reached|…"*, *"try again in 2 hours"* or *Retry-After*), or a default otherwise. It never polls. The preferred provider is used again at the next task once its reset has passed.
- **Nothing is lost or repeated.** The task's plan, the steps already applied and the outstanding checks are saved before every switch and after every step. The next provider gets a compact hand-over of the project, what is already done and verified, and what failed. It never gets a conversation history.
- **When every provider is unavailable**, the task is saved as *blocked* and nothing is half-done. Continue it later, in any process:

  ```bash
  python3 -m bangonpro.ai_engine.mcp resume --job DIR --task ID
  ```

  An interrupted or crashed task resumes the same way. A step whose edit had already been written is recognised and never applied twice.
- **One shared budget.** `--max-calls` sets calls per task and `--max-calls-per-day` sets calls per day across all providers, agents and learning. Failed attempts count too. Every attempt is written to `~/.config/bangonpro/reasoning_ledger.jsonl` with the usage the CLI reported, or `unknown`.
- **No silent API billing.** API-key variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`, `GEMINI_API_KEY`, …) are removed from the CLI's environment. They are kept only for a provider you authorised with `--allow-api-billing`. Bang On Pro never reads or stores credentials.

To check each provider with one minimal call (this counts against the daily budget):

```bash
python3 -m bangonpro.ai_engine.mcp providers --test
```

To withdraw one provider:

```bash
python3 -m bangonpro.ai_engine.mcp revoke --client grok
```

Leave out `--client` to withdraw them all.

## 3. What the agents can do

| Need | Tools |
|---|---|
| See the project | `bop_status`, `bop_project` (paged), `bop_media`, `bop_transcript`, `bop_search` (project index), `bop_agents`, `bop_ops` |
| Look and listen | `bop_frames`: a contact sheet of source frames on screen, or rendered frames with `rendered=true`. Frames are cached, and notes are remembered. `bop_audio`: EBU R128 loudness. `bop_preview`: a rendered range, checked |
| Edit | `bop_switch_camera` (reactions, wides; the dialogue is untouched and the grade goes with the camera), `bop_remove_range` (all tracks, gap closed), `bop_place_asset` (B-roll or stills on V2: position, fades), `bop_add_title`, `bop_edit` (any native operation: trim, move, split, effects, keyframes, levels, dissolves, captions, masks) |
| Work as a team | `bop_task`: begin with checks that prove the result, lease a track range, share findings, complete. QC verifies the result and corrects high-confidence defects |
| Deliver | `bop_vertical` (an editable 9:16 project plus a checked proxy) and the department's Distribution Agent (`bop_run_agent`) |
| Undo and protect | `bop_history` (undo/redo/list) and `bop_lock` (protect a range or track; only you can unlock) |
| The open editor | `bop_live`: status, the live timeline including your manual changes, and proposals that you approve operation by operation in the AI Assistant dock |

## 3b. The department learns (V1.5)

The production intelligence belongs to Bang On Pro, not to a provider's chat history. It survives restarts, projects and provider changes.
It is stored locally in `~/.local/share/bangonpro/intelligence/knowledge.sqlite`.

- **Experience is captured automatically** when a task finishes. That covers the instruction, the tool calls, the verified native edits, QC findings and corrections, the checks, your approvals and rejections, draft-vs-final differences, reasoning usage and time.
  Credentials, prompts, model answers and media are never stored. Only what Quality Control verified counts as experience.
- **Lessons**: a mistake QC found and corrected becomes a lesson for that series. An example is *"titles under 2 s are flagged; hold them 2.3 s"*.
  Lessons go to the agents in every later task, within a small budget. Other series never see them. A lesson that appears independently in two series becomes knowledge for the whole format.
- **Skills**: a verified task made from the compound editing tools is generalised into a declarative workflow, for example *"middle of the guest's longest answer → host camera"*.
  The skill is tried on the next episode without reasoning. It becomes *validated* when QC verifies it on another production, and it is retired after repeated failures. A skill is data, never code.
- **Strategies**: a lesson proposes a change to a whitelisted, bounded setting of one specialist. Today those settings are Graphics strap duration, speaker alignment, default fade and position, and the Multicamera Director's shortest reaction.
  The proposal is tested against the active strategy on identical scenarios, in memory, across learning, validation and held-out sets, using the independent QC checks. It is promoted only if the held-out score improves with no critical regression and no unjustified extra work.
  Low-risk procedural settings are promoted automatically if you allow it. Subjective settings, such as strap position, wait for your approval. Promoted versions are monitored, and a confirmed regression is reverted.

Learning never changes application code, safeguards, permissions, billing or model weights.

Controls are available from the CLI, or as `bop_learn` for a connected client. For example:

```bash
python3 -m bangonpro.ai_engine.mcp learn --job DIR status
```

The available actions are `status`, `lessons`, `skills`, `strategies`, `performance`, `history`, `optimise`, `approve`, `reject`, `disable`, `enable`, `rollback`, `settings`, `export` and `forget`.
Options are `--kind knowledge|skill|strategy`, `--agent`, `--id`, `--version`, `--setting enabled|apply_strategies|auto_promote_procedural|trial_candidate_skills|retention_days|scopes --value JSON` and `--series`.
The Agent Control Centre header shows the provider order, the active provider, the last switch and a learning summary in its tooltip. Strategies waiting for your approval appear under the Learning Agent in Review.

## 4. What is protected

- **Your revision.** Every edit names the draft revision it was based on. An edit based on an older revision is rejected.
- **Your manual changes.** If you change the draft project by hand, agents stop editing it and say so. A later department rebuild keeps your version beside the new one.
- **Locks.** Locked ranges (`department lock` and `bop_lock`), locked tracks and edits you approved are not changed.
- **Each agent's own job.** Each agent may only perform its own operations on its own tracks. For example, the Colourist cannot delete clips and Quality Control cannot edit the cut.
- **No overlapping work.** Two agents in one task cannot change the same track range: a lease names its holder.
- **Verified edits only.** Every edit is checked in the written `.kdenlive` file. An edit the file doesn't show is undone and reported, never claimed.
- **No stale edits.** If a later rerun of an agent changes a clip that an agent edit touched, that edit is marked *stale*. It is never re-aimed at different footage.
- **Your files.** Source media are opened read-only. Supplied assets are only read from the footage folders and the folders you passed with `--allow`. Nothing is published.

The Agent Control Centre shows which client is connected and which agent is working on what. Issues the agents could not resolve appear in its Review list.

## 5. Troubleshooting

- **The client says the draft is at another revision.** It is working from an old view. Let it inspect again; the error tells it the current revision.
- **`MANUAL_EDIT_DRIFT`.** You edited the draft project by hand. Promote and lock that version, or discard your change, before the agents continue.
- **`RENDER_FAILED` on Linux without a display.** MLT's Qt module needs one. Run Bang On Pro, or the MCP server, under `xvfb-run`.
- **`APP_NOT_CONNECTED` from `bop_live`.** Bang On Pro isn't open. The draft tools still work.
