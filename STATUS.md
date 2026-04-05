# COMPANY NODE STATUS

> **REPO IDENTITY:** `personalprometheus-blip/synthos-company` — local: `/home/pi/synthos-company/`
> **This repo owns:** company_node (Pi 4B) — blueprint, sentinel, vault, patches, librarian, scoop, etc.
> **Companion:** `synthos` owns retail_node (Pi 5, incoming) + master PROJECT_STATUS.md — do NOT put retail code here
> **Separate:** `Sentinel` repo is unrelated to Synthos

**Last Updated:** 2026-04-05
**Current Phase:** Phase 5 complete — Pi 5 retail build pending
**Repo:** synthos-company (this repo)
**Companion:** synthos (retail node) — https://github.com/personalprometheus-blip/synthos

---

## ✅ Completed

- Company node agents operational: blueprint, sentinel, vault, patches, librarian, fidget, scoop, timekeeper
- patches.py bugs fixed (dry-run, timezone, continuous mode)
- **Suggestions pipeline migrated to DB:** sentinel.py, vault.py, librarian.py now write via `db_helpers.post_suggestion()` — no longer write directly to suggestions.json
- Repo initialized with professional structure (CLAUDE.md, STATUS.md, README.md, .gitignore)
- Phase 3 normalization complete: strongbox.py moved, company.db schema canonicalized (CL-012 RESOLVED), all suggestion pipeline migrations done
- Company agents classified in TOOL_DEPENDENCY_ARCHITECTURE.md (CL-009 RESOLVED)
- Phase 4 Ground Truth declared — docs/GROUND_TRUTH.md

---

## Blockers (company-side)

| ID | Severity | Description |
|----|----------|-------------|
| ~~CL-009~~ | ~~HIGH~~ | ~~Company agents not classified in TOOL_DEPENDENCY_ARCHITECTURE.md~~ — RESOLVED 2026-03-30 |
| ~~CL-012~~ | ~~HIGH~~ | ~~company.db schema undocumented~~ — RESOLVED: docs/specs/DATABASE_SCHEMA_CANONICAL.md |
| ~~strongbox~~ | ~~HIGH~~ | ~~strongbox.py in wrong repo~~ — RESOLVED |

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
- company.env contains secrets — never commit it (gitignored)
- Hardware: Pi 4B. Pi 2W is fully retired and no longer part of this system.
- See CLAUDE.md for full session context

---

## Addendum — v3 Portal Architecture Decision (2026-04-05)

The following architectural decisions were made and locked on 2026-04-05:

### What changed
- **login_server/ retired.** The node-picker SSO model (customer picks their Pi → SSO redirect) was
  the wrong design for v3. Customers do not have their own nodes. `synthos-login.service` is stopped
  and disabled. `login_server/` code is kept for reference but is no longer active.

- **company_server.py is internal API only.** Port 5010 on the Pi 4B is no longer publicly exposed.
  `admin.synth-cloud.com` DNS and Cloudflare Access app have been removed. The company server is
  a private backend API called by the Pi 5 retail portal over the local network.

- **Single portal model.** All web access — customers and admin — goes through the Pi 5 retail portal
  at `app.synth-cloud.com`. The Pi 4B exposes only SSH externally (`ssh.synth-cloud.com`).

### Correct v3 portal flow
```
portal.synth-cloud.com  →  redirect  →  app.synth-cloud.com (Pi 5, port 5001)
                                              │
                                   ┌──────────┴──────────┐
                                   │                     │
                              Customer login         Admin login (Patrick)
                              → their trading        → trading dashboard
                                dashboard             + Company Admin link
                                                      → calls Pi 4B API
                                                        (company_server :5010)
```

### Domain map (final)
| Domain | Destination | Auth |
|--------|-------------|------|
| `app.synth-cloud.com` | Pi 5 port 5001 | Portal login (auth.py) |
| `portal.synth-cloud.com` | redirect → app.synth-cloud.com | none |
| `ssh.synth-cloud.com` | Pi 4B port 22 | Cloudflare Access |
| `ssh2.synth-cloud.com` | Pi 5 port 22 | Cloudflare Access |
| ~~`admin.synth-cloud.com`~~ | ~~Pi 4B port 5010~~ | REMOVED |
