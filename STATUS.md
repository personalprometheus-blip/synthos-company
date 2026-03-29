# COMPANY NODE STATUS

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

- [ ] **Step 4:** Move strongbox.py from retail repo (synthos/src/) to agents/ here — verify running
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
| strongbox | HIGH | strongbox.py in wrong repo — no backups running |

## Notes for AI Agents
- patches.py was killed for the current work session — restart at end: `nohup python3 /home/pi/synthos-company/agents/patches.py --mode continuous >> logs/bug_finder.log 2>&1 &`
- company.env contains secrets — never commit it (gitignored)
- Hardware: this node runs on Pi 4B locally but is not hardware-specific
- See CLAUDE.md for full session context
