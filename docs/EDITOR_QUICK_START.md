# Bang On Pro 1.3: editor quick start (internal beta)

No terminal is needed for anything on this page.

## 1. Install

1. Download the `BangOnPro-*.dmg` attached to the GitHub release [v1.3.0](https://github.com/kierantrestain/Bang-on-pro/releases/tag/v1.3.0). It needs an Apple Silicon Mac.
2. Open the DMG and drag **Bang On Pro** to Applications.
3. The beta is ad-hoc signed, not notarised. The first time, Control-click the app and choose **Open**, then **Open** again. If macOS still blocks it, go to System Settings → Privacy & Security → **Open Anyway**.

Rollback: V1.2.1 (stable) and V1.2.2 stay available as their own GitHub releases. Projects and preferences are compatible.

## 2. First launch: system check

The **System check** opens on first launch. You can reopen it later from the *Bang On Pro* menu → *System check…*. It shows your Mac, free space, the media tools, the bundled AI runtime, local models and cache size. Each line has a plain fix. Double-click a line to act on it.

- **Productions and cache:** choose where jobs, drafts and the analysis cache live. An external SSD is a good choice. Your camera originals are never written to.
- **Model Manager…:** install *Whisper small*, which gives word-level transcripts, real captions and the pause and filler decisions. Install *MobileSAM* too if you want background blur or masks. Without Whisper the department still cuts on speech regions, but it withholds captions and tells you so.

## 3. Your first production

1. Switch to the **AI First Cut** workspace (Alt+Shift+… or the workspace bar). The **Production** panel is on the right.
2. Pick the **mode**:
   - *Assisted*: every edit waits for your approval.
   - *Autonomous first cut*: a reviewable draft.
   - *Full production draft*: colour, captions, graphics and deliverables too.
3. Pick the **profile** (podcast, gameshow, sports discussion, daily news, interview, social or your own), the series and the episode title, then write the brief in plain words, e.g. "Conversational cut, keep the laughs, captions and two social clips".
4. Add your camera and microphone files to the project bin, then press **Start production**. To line up several episodes, tick **Queue** first; they run one after another and resume after a restart.
5. Watch the agent list. A tally lamp, state, confidence and a one-line result appear for each agent. **Needs your attention** lists what the department wants a human to check, such as low sync confidence, a judgement call or captions withheld.

## 4. Review and approve

- Select an agent to **Accept**, **Reject** or **Rerun** it. Rejecting an editorial agent removes the work that depends on it. Nothing else is redone.
- **Review in timeline** loads the draft as one undoable proposal. Every clip and effect shows which agent made it.
- **Promote** copies the draft over your edit. If a project already exists it asks for approval first and keeps a timestamped backup. Publishing always needs your explicit approval.
- Your accept and reject decisions are remembered. The department only *suggests* profile changes from them, and nothing changes until you approve it.

## 5. Ask for changes in plain words

When the draft is ready, type a change in the **Revise** box under the review list and press **Revise**:

- "Tighten the opening but keep the natural conversation"
- "Use more reaction shots" or "Fewer camera cuts"
- "Preserve the suspense before each reveal"
- "Add our graphics and lower thirds"
- "Create five strong standalone vertical clips"

What happens:

- **Only the affected work reruns.** Ingest, sync and transcript analysis are reused.
- **Selection only:** tick it to change just the range you selected on the timeline (set the zone with I / O first). Everything outside the range stays exactly as it was.
- **Earlier revisions are kept.** A later revision leaves the range of an earlier one alone.
- **Undo** restores the draft from before the last revision.
- **Unclear requests are explained.** If the department can't map a request to an edit, the status line says so and nothing changes.

**Vertical clips** open as normal editable 9:16 projects in `draft/vertical/`. They use the speaker's close-up, framed on the subject. A review proxy is rendered and checked automatically; the full-quality version renders with the approved export.

## 6. Timing sessions (for the beta measurement)

Use **Start timing** in the Production panel. Choose *Manual baseline* or *Agent-assisted* and name the deliverable. Setup, review, corrections and approval are logged automatically as you work, and **Pause** stops the clock. **Finish** asks for your 1–5 quality rating. Machine processing time is recorded separately and never counted as your time. See [INTERNAL_TESTING_PROGRAMME.md](INTERNAL_TESTING_PROGRAMME.md).

## 7. When something goes wrong

Error messages say what to do next, for example "free 4 GB on the production drive" or "install a Whisper model". If you need help, choose **Export diagnostics…** and send the zip. By default it contains no media, transcripts, passwords or keys; see [DIAGNOSTICS.md](DIAGNOSTICS.md).
