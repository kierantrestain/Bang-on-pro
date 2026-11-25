# Bang On Pro — Unslop UI pass

Method adapted from [Unslop](https://github.com/heydamir/unslop) by Damir Sokolovsky (@heydamir), CC BY 4.0. No third-party runtime code added.

Baseline: `7ee771a85832b025b6f5718ab05f0773c138072f`. Reviewed the native Agent Control Centre screenshot and source of the shared Qt theme, workspace switcher and production controls. Direction: precise, quiet broadcast editing; graphite chrome; existing Bang On orange for deliberate actions; footage/timeline as the visual focus; thin orange workspace marker as signature.

Findings and changes: neutralise blue-tinted chrome, preserve functional blue selection with improved white-text contrast, retain orange brand accent with dark button text, remove the orange wordmark block and excessive pill corners, use more consistent 4/8px spacing, give the start action clear emphasis, shorten setup/action labels without changing IDs or signals, retain tooltips describing approval modes, constrain the form and agent table, and split draft actions across rows for narrower panels. Existing editing/agent logic is unchanged. Added a focused native regression test.

Verification required before merging: run existing Qt5/Qt6 and Python CI, visually inspect the actual native application with populated projects at 1280x800, 1440x900 and 1920x1080 (including macOS scaling). Verify all nine workspaces, keyboard focus, disabled controls, table overflow, production job setup, agent actions, draft promotion and saved layouts. Compare against the committed V1.2 screenshots. This source-only pass does not establish that the pre-existing whole-window overflow is fixed; no new screenshots or physical Mac tests were executed here. Do not merge until visual acceptance and tests pass. Keep release work separate.
