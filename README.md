# Bang On Pro

**Bang On Pro** is an AI-native professional non-linear video editor built on **Kdenlive (`release/24.08` / `v24.08.3`)**, **Qt**, and **MLT (`>= 7.22.0`)**, distributed under the **GNU General Public License Version 3** (`GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL`).

It preserves Kdenlive's complete manual editing workflow, `.kdenlive` project compatibility, autosave, and crash recovery while adding:

- **Native AI Timeline Command API v1** (`src/bangonpro/timeline_api/`): single authorized transactional timeline writer with revision conflict detection, atomic rollback, approval staging, undo/redo, local Unix-domain IPC (`TimelineCommandServer`), and native `TimelineItemModel`/`DocUndoStack` bridge (`KdenliveTimelineBridge`).
- **Premiere Preset Compatibility Engine & Browser** (`bangonpro/premiere_compat/`, `src/bangonpro/effects_browser/`): hardened `.prfpset`/ZIP extractor (zip-slip/zip-bomb & XXE-disabled `defusedxml`), `254016000000` ticks/sec keyframe/easing conversion, MLT/FFmpeg `avfilter`/frei0r/Kdenlive `<effectgroup>` XML converter, honest 4-tier compatibility classification (`exact_validated`, `approximated`, `missing_dependency`, `unsupported`), deterministic `melt` before/after visual & audio verifier, and dockable C++ Qt preset browser.
- **Autonomous Multi-Agent Production Pipeline** (`bangonpro/ai_engine/`, `bangonpro/show_profiles/`): FFprobe verification, SHA-256 source-media immutability, GCC-PHAT audio cross-correlation multicamera sync, pluggable WhisperX/faster-whisper + deterministic VAD/multi-mic diarisation, rough-cut & speaker-aware camera switching, FFmpeg/MLT audio restoration, SRT/ASS captions & lower thirds, technical QC, OpenTimelineIO (`.otio`) & editable `.kdenlive` project emission, and 4 validated Show Profiles (`podcast_multicam`, `gameshow_multicam`, `sports_discussion`, `daily_news`).
- **Professional UI & Apple Silicon macOS Toolkit** (`src/bangonpro/ui/`, `bangonpro/macos/`): collapsible AI Assistant Dock (proposed timeline diff tree, Accept/Reject/Compare/Refine/Undo, live Agent Activity Log), Premiere-style workspace layout, VideoToolbox/ProRes profiles with CPU fallback, automatic proxy generator, low-storage LRU cache manager, consent-gated TLS remote GPU protocol, and `melt`/`ffmpeg` benchmark runner.

## Quick Commands

```bash
# 1. Install pinned Python runtime & verification dependencies
pip3 install -r requirements-bangonpro.txt

# 2. Import and audit a Premiere Pro preset archive (.zip / .prfpset)
python3 -m bangonpro.premiere_compat.cli import \
  --input "effects pr.zip" \
  --audit-json "Bang_on_pro_premiere_preset_audit.json" \
  --output-dir ./preset_audit_out \
  --verify-render

# 3. Run the autonomous multicamera first-cut pipeline
python3 -m bangonpro.ai_engine.cli \
  "Sync all four cameras, remove setup delays, build a first cut and add our standard graphics" \
  --profile podcast_multicam \
  --inputs cam_wide.mp4 cam_host.mp4 cam_guest.mp4 cam_react.mp4 \
  --output-dir ./first_cut_out \
  --export

# 4. Run the full automated QA, security, licence & integration suite
python3 tests/bangonpro/run_all_checks.py
```

See also:
- [Architecture & Module Boundaries](docs/BANG_ON_PRO_ARCHITECTURE.md)
- [Open-Source Component Matrix & Licensing](docs/OPEN_SOURCE_COMPONENTS.md)
- [Apple Silicon macOS Build, Signing & Installation Guide](docs/MACOS_BUILD_AND_INSTALL.md)

---

![](data/pics/kdenlive-logo.png)

# About Kdenlive (Upstream Foundation)

[Kdenlive](https://kdenlive.org) is a Free and Open Source video editing application, based on MLT Framework and KDE Frameworks 6. It is distributed under the [GNU General Public License Version 3](https://www.gnu.org/licenses/gpl-3.0.en.html) or any later version that is accepted by the KDE project.

# Building from source

[Instructions to build Kdenlive](dev-docs/build.md) are available in the dev-docs folder.

# Testing Kdenlive via Nightly Builds

- AppImage (Linux): https://cdn.kde.org/ci-builds/multimedia/kdenlive/master/linux/
- Flatpak (Linux):
  - Add the kde flatpak repository (if not already done) by typing `flatpak remote-add --if-not-exists kdeapps --from https://distribute.kde.org/kdeapps.flatpakrepo` on a command line. (This step may be optional in your version of Flatpak.)
  - Install kdenlive nightly with `flatpak install kdeapps org.kde.kdenlive`.
  - Use `flatpak update` to update if the nightly is already installed.
  - _Attention! If you use the stable kdenlive flatpak already, the `*.desktop` file (e.g. responsible for start menu entry) is maybe replaced by the nightly (and vice versa). You can still run the stable version with `flatpak run org.kde.kdenlive/x86_64/stable` and the nightly with `flatpak run org.kde.kdenlive/x86_64/master` (replace `x86_64` by `aarch64` or `arm` depending on your system)_
- Windows: https://cdn.kde.org/ci-builds/multimedia/kdenlive/master/windows/
- macOS x86: https://cdn.kde.org/ci-builds/multimedia/kdenlive/master/macos-x86_64/
- macOS ARM: https://cdn.kde.org/ci-builds/multimedia/kdenlive/master/macos-arm64/

*Note * - nightly/daily builds are not meant to be used in production.*

# Contributing to Kdenlive

Please note that Kdenlive's Github repo is just a mirror: read [this explanation for more details](https://community.kde.org/Infrastructure/Github_Mirror). 

The prefered way of submitting patches is a merge request on the [KDE GitLab on invent.kde.org](https://invent.kde.org/-/ide/project/multimedia/kdenlive): if you are not familar with the process there is a [step by step instruction on how to submit a merge reqest in KDE context](https://community.kde.org/Infrastructure/GitLab#Submitting_a_Merge_Request).

We welcome all feedback and offers for help!

* Talk about us!
* [Report bugs](https://kdenlive.org/en/bug-reports/) you encounter (if not already done)
* Help other users [on the forum](http://forum.kde.org/viewforum.php?f=262) and bug tracker
* [Help to fill the manual](https://community.kde.org/Kdenlive/Workgroup/Documentation)
* Complete and check [application and documentation translation](http://l10n.kde.org)
* Prepare video tutorials (intro, special tricks...) in your language
  and send us a link to add in homepage or doc
* Detail improvement suggestions
  we don't test every (any?) other video editor, so give precise explanations
* Code! Help fixing bugs, improving usability, optimizing, porting...
  register on KDE infrastructure, study its guidelines, and pick from roadmap. See [here](dev-docs/contributing.md) for more information
