# Connecting Claude Code or Codex to the production agents (V1.4)

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

Then ask the client in plain words. For example: *"In the guest's longest answer, cut to the host listening once, put the city B-roll near the end with a short fade, and title the opening 'Live from the studio'."*

The client inspects the timeline, looks at frames when it needs to, and edits as the agent that owns each change.
It asks Quality Control to verify the actual draft, and reports back. Read-only tools are marked as read-only, so your client can allow them without asking you.

## 2. Instructions typed in Bang On Pro (optional)

The **Revise** field in the Agent Control Centre still uses the V1.3 rules first.
If the rules don't understand an instruction, the Executive can ask your installed CLI for a short plan. Then the agents carry it out and Quality Control verifies it.
This only happens after you allow it once:

```bash
python3 -m bangonpro.ai_engine.mcp authorise --client claude
```

Use `--client codex` for Codex. You can cap the planning calls per instruction with `--max-calls 3`.

To withdraw it:

```bash
python3 -m bangonpro.ai_engine.mcp revoke
```

The CLI runs in its documented headless mode (`claude -p`, `codex exec`) with your existing sign-in. Bang On Pro never reads its credentials.

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
