# COMPANY NODE STATUS

> **REPO IDENTITY:** `personalprometheus-blip/synthos-company` — local: `/home/pi/synthos-company/`
> **This repo owns:** company_node (Pi 4B) — blueprint, sentinel, vault, patches, librarian, scoop, etc.
> **Companion:** `synthos` owns retail_node (Pi 2W) + master PROJECT_STATUS.md — do NOT put retail code here
> **Separate:** `Sentinel` repo is unrelated to Synthos

**Last Updated:** 2026-03-29
**Current Phase:** Phase 3 — Normalization Sprint
**Repo:** synthos-company (this repo)
**Companion:** synthos (retail node) — https://github.com/personalprometheus-blip/synthos

---

## ✅ Completed

- Company node agents operational: blueprint, sentinel, vault, patches, librarian, fidget, scoop, timekeeper
- patches.py bugs fixed (dry-run, timezone, continuous mode)
- **Suggestions pipeline migrated to DB:** sentinel.py, vault.py, librarian.py now write via `db_helpers.post_suggestion()` — no longer write directly to suggestions.json
- Repo initialized with professional structure (CLAUDE.md, STATUS.md, README.md, .gitignore)

---

## 🟡 In Progress

### Phase 3 — Normalization Sprint

- [x] **Step 4:** Move strongbox.py from retail repo (synthos/src/) to agents/ here — done
- [ ] **Step 5:** Document company.db schema (PRAGMA table_info) — currently undocumented (CL-012)

---

## 🔴 Not Started

### Phase 4 — Ground Truth Declaration
- Requires Phase 3 complete
- See retail repo STATUS.md for full milestone plan

---

## Blockers (company-side)

| ID | Severity | Description |
|----|----------|-------------|
| CL-009 | HIGH | Company agents not classified in TOOL_DEPENDENCY_ARCHITECTURE.md |
| CL-012 | HIGH | company.db schema undocumented |
| ~~strongbox~~ | ~~HIGH~~ | ~~strongbox.py in wrong repo~~ — RESOLVED (Step 4) |

## ⚠️ Security Note — Integrity Gate

The company integrity gate architecture is defined in `docs/governance/COMPANY_INTEGRITY_GATE_SPEC.md` (retail repo).

**Until the pre-release security phase is complete:**
- The integrity gate is enforced at setup time only (installer)
- There is NO boot-time gate — company agents start without a pre-flight integrity check
- A misconfigured or tampered environment will not be caught at runtime
- This is accepted for the current phase but must be resolved before live trading or adversarial deployment

Full boot-time enforcement is tracked in PROJECT_STATUS.md (retail repo) under Phase 6 — Pre-Release Security Hardening.

---

## Notes for AI Agents
- patches.py was killed for the current work session — restart at end: `nohup python3 /home/pi/synthos-company/agents/patches.py --mode continuous >> logs/bug_finder.log 2>&1 &`
- company.env contains secrets — never commit it (gitignored)
- Hardware: this node runs on Pi 4B locally but is not hardware-specific
- See CLAUDE.md for full session context
