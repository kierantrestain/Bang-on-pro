# V1.2.2 specialist work in progress (temporary, unreviewed)

`specialists.bundle` holds the three specialist branches (base `fb6b7b2`), stopped at low usage:

| Branch in bundle | Domain |
|---|---|
| `worktree-agent-a4876dec8ee16c178` | media processing (sync drift/segments, ingest, visual analysis, colour, ...) |
| `worktree-agent-a7670755ad67ffde0` | editorial intelligence (pacing, analysis, brief, director, ...) |
| `worktree-agent-a992b83155d350f18` | production readiness (queue, readiness, diagnostics, relink, measurement, learning, profiles, ops tests) |

Restore: `git fetch wip/v1.2.2/specialists.bundle 'refs/heads/*:refs/remotes/wip/*'`, then review and merge each
`wip/<branch>` into this branch. The last commit on each is an untested checkpoint. Delete this folder after integration.
