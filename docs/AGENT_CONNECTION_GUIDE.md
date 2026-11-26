# AI Providers & Subscriptions in Bang On Pro V1.5.2

Open **AI Providers & Subscriptions** from the AI Assistant, the Agent Control Centre or the Bang On Pro menu. The subscription connection, privacy choices and usage limits apply to every production on this computer.

1. Use **Install instructions** on a missing client row. Bang On Pro detects official executables in common user-local, Homebrew and macOS app locations.
2. Choose **Connect**. Complete any sign-in in the official client or browser. **Check sign-in** checks for an account session and records Bang On Pro authorisation when the client can report one.
3. Select **Connected Subscriptions**, choose the project context you permit and save. Instructions are sent as text only when external reasoning is needed. Project metadata can include edit times, camera and speaker labels, graphics and verified editing lessons. Transcript excerpts require both data options. Media processing and full-resolution footage stay local.
4. Reopen the dialog and choose **Test** for an eligible provider. The test sends a small synthetic prompt and uses one call from the shared daily budget. Installation, account sign-in, app authorisation, subscription eligibility and the last successful test each have their own status.
5. Choose **Disconnect** to withdraw Bang On Pro authorisation while keeping the client's own account session. Choose **Local Only** and save to stop subsequent in-app external reasoning across all providers.

When upgrading from V1.5.1, the dialog retains your project metadata and transcript choices. The subscription workflow requires fresh consent because earlier consent referred to a configured endpoint. Unsupported audio and image consent controls have been removed.

| Client | Account and eligibility |
|---|---|
| Claude Code | The official client handles sign-in. App-driven routing is held until the integration can establish permitted usage and exclude usage-credit fallback. [Anthropic authentication](https://code.claude.com/docs/en/authentication) · [Usage and integration terms](https://code.claude.com/docs/en/legal-and-compliance) |
| OpenAI Codex | Supported where the official client reports a qualifying personal ChatGPT plan, available included usage, and zero additional credits. Bang On Pro verifies these before each request. [Codex authentication](https://learn.chatgpt.com/docs/auth) · [Account and rate limits](https://learn.chatgpt.com/docs/app-server) |
| Grok Build | Official sign-in is available. Account entitlement and top-up state remain unverified for app-driven routing. [Grok Build](https://docs.x.ai/build/overview) · [Additional usage](https://docs.x.ai/x-subscription/extra-usage) |
| Google Antigravity | Official sign-in is available. Subscription entitlement and the effective billing route remain unverified for app-driven routing. [CLI sign-in](https://antigravity.google/docs/cli-install) · [Plan overages](https://antigravity.google/docs/plans) |

## 1. Optional MCP connections for a client you control

This section applies when you deliberately open an external AI client and have it connect to a specific production. The in-app AI Assistant and production agents use the subscription settings above directly.

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

## 2. Reasoning, failover and usage

The AI Assistant and Agent Control Centre try fully understood local editing instructions first. The department also tries matching learned skills locally and checks their edits before asking an authorised provider. Creative instructions with unsupported requirements go to reasoning or are reported for clarification. The agents retain responsibility for applying and verifying the plan.

The default priority is **Claude Code → OpenAI Codex → Grok Build → Google Antigravity**. Move rows up or down and save to change it. With automatic failover disabled, only the first authorised provider is considered. With failover enabled, the router considers available providers in priority order and switches on availability failures. A malformed plan or a rejected editing request stops the attempt. Provider cooldowns use reported reset times when available.

Per-task and daily call limits use the same ledger across both app entry points. The daily limit resets at midnight UTC. Once a reasoning attempt begins it counts, even if the client fails. Account status and quota checks consume no reasoning calls. Dollar figures received from a provider are estimated usage. Bang On Pro uses the supported client and its account store, excludes inherited API credentials and blocks ambiguous billing configurations.

A blocked task keeps its verified edits and saved progress. The same provider preferences and remaining task budget apply when it resumes.

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
The Agent Control Centre opens the shared subscription dialog. Job activity shows the provider and learning summary when available. Strategies waiting for your approval appear under the Learning Agent in Review.

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
