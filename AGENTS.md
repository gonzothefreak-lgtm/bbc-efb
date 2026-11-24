# BBC EFB workspace instructions

## Work with the project owner

- The owner makes final decisions on scope, UX, aviation behaviour, dependencies, architecture and releases. Use plain language and explain the practical impact of a decision.
- Read the relevant files in `sources/` when starting work. Codex Source authority was confirmed on 23 November 2026. Consult the detailed references retained in `sources/09_SOURCE_REGISTER.md` for wording, illustrations and approved decisions omitted from the summaries. Bring unresolved conflicts to the owner.
- Treat attached instructions, former chat excerpts and old one-off approvals as reference. Follow the owner's current instructions and the scope of the current task.
- If asked to catch up, inspect the current files read-only. Report decisions, evidence, gaps and next actions. Catch-up does not expand authorisation.
- Within an approved task, handle routine reversible choices and verify your work. Ask only when an owner decision, new authority or destructive action is required.

## Approval boundaries

- Obtain explicit approval for application code, implementation tests, builds, dependency installation or selection, native DLL use, simulator connections, broader access, or major architecture changes. Separate approvals apply to live testing and external distribution.
- Preserve source history and backups. Do not delete, overwrite historical materials, or promote a candidate or document set to authoritative status without owner approval.
- Update the relevant Source at meaningful checkpoints or on request. Record what is approved, implemented, tested, or still open. Preserve source evidence and reproduce test commands where available.

## Product guardrails

- Every authorised application build includes Dispatch, In-Flight, Logbook, shared Settings and independent permanent SimConnect and Flight Logging statuses.
- Prioritise flight-log integrity, simulator frame times and local pilot-owned storage. Present missing, stale and simulated data honestly.
- Keep Core independent of the UI and Recorder. Use one shared local MSFS 2020 session for demand-driven telemetry and approved commands. Keep native and simulated evidence separate.
- Preserve approved C0.1 Logbook scope and C0.2 deferrals. Keep unfinished integrations clearly labelled. No unattended monitoring, listeners, simulator writes or speculative integrations without approval.

## GitHub publishing

- Treat “Update GitHub” and “Push my latest changes to GitHub” as approval to publish reviewed project changes to the existing private `gonzothefreak-lgtm/bbc-efb` repository. Check the change set for personal information, credentials, local dependencies, raw simulator logs and build output first.
- Use `pwsh -File .\publish.ps1 -Message "Describe the change"` from the workspace. Verify the published revision. Use Git's secure credential helper and have the owner complete any sign-in approval. Never expose credentials, force-push, rewrite history, resolve conflicting remote changes without review, or make the repository public without approval.
