# Specialist instructions — verifiable excerpts and access limits

**Preservation status:** This file is **not** a complete export of the three chats' system/custom instructions or all messages. It saves the exact passages available from the ChatGPT Project Instructions and owner excerpts surfaced in accessible project conversation context. The master Chat Register contains the later authoritative workstream definitions. This file is **not** an active `AGENTS.md` and grants no development permission.

## Common standing instructions: copied verbatim from visible Project Instructions (22 November 2026)

> **Main Development Chat** owns whole-EFB architecture, shared UI/framework, standards, integration and release planning. **Black Box Core Dev** owns simulator/source technical research, Core subscriptions and data distribution; **Logbook Page Dev** owns full Logbook functionality AND UI/UX, including Flight Recorder. Follow the Chat Register. Keep minor related topics in context; hand off genuine shared decisions to Main once, concisely. Avoid handoff loops. Chats do not automatically synchronise; catch-up is ON DEMAND.

**Source:** `PROJECT_INSTRUCTIONS_ORIGINAL.md`, section 4. **Classification:** owner-supplied standing Project Instruction, dated 22 Nov 2026, later handled as historical roles after migration. **Supersession:** Development Chat Register v1.10's Codex migration section records old chat ownership as historical, not a new Codex team structure.

> **Do not write application code, run implementation tests, prepare build packages or initiate builds without my explicit authorisation.** Research, sketches, discussions and proposed milestones are not authorisation. Get approval before major architecture changes. Clearly separate proposed, approved, implemented and tested.

**Source:** `PROJECT_INSTRUCTIONS_ORIGINAL.md`, section 5. **Classification:** standing owner instruction. The project owner later narrowly approved Core component coding/testing (recorded in the masters) but has now halted all new implementation for migration.

## Main Development Chat

> "I’m moving the BBC Integrated Workstation and EFB for VATSIM project into a new Codex project in a clean folder."

> "This request authorises migration preparation only. Do not change the updated Project Sources, start implementation or testing, select a final dependency or baseline, or authorise a build."

**Date:** 23 November 2026. **Classification:** current owner task instruction / migration freeze, **not** a standing Codex coding rule. **Source:** current conversation, final migration handoff request; also Project Development Record v1.10 §30 and Chat Register v1.10 migration addendum. **Supersedes:** earlier ongoing Core correction/testing task scopes as active work while migration proceeds. They remain evidence of work already authorised and performed.

> "I approve controlled simulated offline testing of Black Box Core R3 using Codex. No installations, MSFS connections or EFB builds. Please prepare the Codex testing handoff."

**Date:** 23 November 2026. **Classification:** completed **one-off** test authorisation, not a standing permission for future tests. **Source:** accessible Main Development Chat messages; evidence captured in `01_CODEX_WORK_EVIDENCE/test_artifacts/`.

## Black Box Core Dev

> "DO NOT MAKE DECISIONS without my APPROVAL!"

**Date:** 22 November 2026 (shared project-conversation excerpt). **Classification:** owner-approved/owner-directed general approval principle in a specialist chat; preserved as a direct excerpt, not evidence that the entire conversation is exported. **Context:** owner requested a step-by-step decision process after a large technical handoff. Later authority: Project Instructions §5 and Development Chat Register v1.10.

> "Ok let's start with Sim connect first."

**Date:** 22 November 2026 (shared project-conversation excerpt). **Classification:** one-off research-sequencing instruction. **Current standing:** simconnect-H v0.7.2 is an evaluation candidate, not a final selection; see Integration Record v0.8.

## Logbook Page Dev

> "I want this chat to handle the complete Logbook design, including both functionality and UI/UX."

**Date:** 22 November 2026 (historical specialist handoff excerpt visible in shared project context). **Classification:** owner-approved ownership direction, now documented in Chat Register v1.10 and Logbook Design Record v1.2. **Supersedes:** older division where Main alone owned Logbook UI.

> "ok pmdg only to start with."

**Date:** 22 November 2026 (shared project-conversation excerpt). **Classification:** initial one-off **aircraft-specific research priority**, **not** implementation approval. **Later direction:** plugin work parked in Aircraft Plugin Framework v0.5 and Integration Record v0.8.

## What is NOT captured

- **Main Development Chat:** not an exported verbatim full transcript or hidden per-chat instructions; only messages visible in the present chat and included source records.
- **Black Box Core Dev:** no full original chat transcript, custom-instruction screen or full exact approval chronology inspected. Use the specialist master's change logs and any owner-exported transcript for additional exact quotations.
- **Logbook Page Dev:** no full original chat transcript or custom-instruction screen inspected; Q1–Q14 illustrated master and 22–23 Nov handoffs are included separately.
- **Historical earlier chats:** prior-project Main, earlier SimConnect and `zz - OLD - ...` records were not exhaustively inspected. They are historical, not implementation baselines.

**How owner can complete this later:** In the old ChatGPT Project, open each named conversation, copy/export any needed exact custom chat instructions or key decisions not present in masters, date and label the excerpts, and put them in `07_OWNER_SUPPLIED_MISSING_MATERIAL/` in the new workspace. Do not automatically make the excerpts authoritative over the nine confirmed masters.
