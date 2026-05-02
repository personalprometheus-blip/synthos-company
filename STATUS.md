# COMPANY NODE STATUS

> **⚠ This file is a historical Phase-1-through-5 snapshot (frozen
> 2026-04-05).** It captures the pre-Pi-5-deployment era. Everything
> below is preserved for audit trail; none of it is the live state.
>
> **Current state (2026-05-01):** pi4b is the live company node
> running synthos_monitor.py (the command portal) at
> `command.synth-cloud.com`, plus auditor / archivist / vault /
> librarian / sentinel / strongbox / fidget / heartbeat / monitor.
> The retail node (pi5) deployed 2026-04-18; it owns all trading
> agents and the customer portal.
>
> **Recent landmark changes on this repo (synthos-company):**
> - **company_auditor — SSH retry + cascade fix (2026-05-02)**:
>   `check_remote_health` was emitting one SERVICE_DOWN finding per
>   configured service when SSH itself failed — one unreachable scan
>   produced N findings. Now does a pre-flight `ssh host true` ping
>   at the top; if the ping fails (with the new 2-retry policy in
>   `_ssh_run`), early-returns empty issues[] so NODE_UNREACHABLE
>   from `scan_remote_logs` stays the single source of truth for
>   connectivity. _ssh_run gained 2 retries × 3s delay so the
>   auditor's 5-min cron no longer trips on the pi5-boot-window race
>   (auditor at 04:00:30 vs pi5 boot completion at 04:01:33).
> - **Audit page polish (2026-05-01)**: 5-min poll + silent refresh
>   so the synthos-pi-retail tab no longer blanks every cycle; honest
>   total/displayed math (was reporting len(issues) AFTER the [:200]
>   cap, which made the math look broken when underlying counts were
>   in the thousands); cache-age tag in subtitle when serving from
>   pi5's 60s in-memory scan cache. TODO widget poll bumped 30s →
>   5min same session.
> - **Approvals UI gains Type pill + why/how detail (2026-05-01 late PM)**:
>   companion to pi5's new `/request-access` public flow. Pending signups
>   now distinguished by `request_type` ('subscribe' for code-holders via
>   /signup, 'request_access' for uninvited users via /request-access).
>   Purple "Request" pill + inline why_interested + how_heard detail
>   card render for request_access rows so admin has context before
>   clicking Approve. Verified-email pill suppressed for request_access
>   since verification happens after approve. Both copies of
>   loadApprovals() updated (dashboard inline + standalone /approvals
>   page). XSS hardening: user-controlled fields now escapeHtml'd.
> - **Scoop Idempotency-Key ASCII-fold (2026-05-01 late PM)**: defensive
>   fix — em-dashes / unicode punctuation in subjects could fail Resend
>   dispatch with UnicodeEncodeError when the idem_key was built into
>   an HTTP header (latin-1). Now ASCII-replaces unencodable chars while
>   preserving uniqueness. Caught during /request-access smoke when
>   `[Synthos] New access request — Smoke Test ...` failed first send.
> - **Scoop notification pipeline rebuilt (2026-05-01)**: 11 silent
>   days ended. Diagnosed three structural bugs (schema mismatch
>   between strongbox writer + scoop drain; one-shot drain at startup;
>   dead-end portal-dispatch auth — `/api/notifications/send` requires
>   admin session not service token). Built new `agents/_shared_scoop.py`
>   as single enqueue source of truth — strongbox + sentinel both route
>   through it. Removed dead `vault._trigger_scoop`. Made scoop's drain
>   re-pollable + dual-schema (accepts both `delivered:false` legacy
>   strongbox writes AND `status:pending|retry`). Portal-dispatch
>   short-circuits cleanly when `PORTAL_TOKEN` missing — logs disabled
>   state once at startup. Triple-check passed: 3 emails delivered to
>   operator end-to-end. Service-token auth on retail_portal endpoints
>   queued separately for after-hours synthos repo work. Commit 5e8cb78.
> - **Stale branch + server cleanup (2026-05-01)**: synthos-company
>   branches 10 → 1 (just main); origin remote also swept.
>   `documentation/archive/specs/TRADER_RESTRUCTURE_PLAN.md` preserved
>   from abandoned patch/logic-review-2026-04-20 before deletion. pi4b
>   server cleanup: removed dead `synthos-login.service` /
>   `/home/pi/synthos_build/` / `/home/pi/synthos-process/`. Archived
>   `login_server/` → `documentation/archive/login_server/` (auth-only
>   Flask app superseded by synthos_monitor command portal).
> - **System-architecture pipeline page deferred-data-sources panel
>   (2026-05-01)**: added `deferred` gate flag (purple ⏸) for News
>   G12/G14/G15 + Sentiment G5/G7/G8/G10/G16. New panel below pipeline
>   grid summarizes paid-tier gaps + formally-deferred future phases.
>   Bias-gate scaffold flags removed (re-reading code: all 6 are fully
>   implemented; sample-size early-returns aren't stubs). Macro gate
>   names corrected — page had wrong gate order vs actual code.
> - **System-architecture pipeline page rewrite (2026-05-01)**: PIPELINE
>   constant in `templates/system_map.html` updated to match code reality
>   — gate counts corrected (Fault 4→8, Market State 5→4, Trader 14→13),
>   each gate annotated with foreign data sources (FRED / Alpaca / Yahoo /
>   News APIs), scaffolding gates flagged in amber, click-to-detail wired
>   on every gate + agent header (fixing the long-standing parity gap with
>   the topology page), Sector Screener restyled into the ingest column
>   alongside the other ingest agents. New `showPipeAgentDetail` /
>   `showPipeGateDetail` JS helpers prefer topology's richer agent entry
>   when available, otherwise synthesize from PIPELINE.
> - **Auditor auto-resolve sweep (2026-04-28)**: company_auditor now
>   auto-clears resolved conditions instead of leaving them flagged
>   forever. Two policies: customer_db::* conditions cleared on
>   re-evaluation (catches NEGATIVE_CASH self-correction directly),
>   and a 24h generic stale rule for log-scan issues (anything not
>   firing for a day gets resolved; fresh re-occurrences re-INSERT
>   via the existing dedup logic). Restart triggered an immediate
>   sweep that resolved 27 stale rows from this and prior weeks.
>   Operator-side bulk-ack cleared the remaining 14 today. Issue
>   queue went 41 → 0.
> - **Pill Usage telemetry panel (2026-04-27)**: new `/pill-usage`
>   page with `/api/proxy/pill-usage` proxy to retail. Renders
>   1d/7d/30d/90d window switcher + 4 totals tiles + ranked
>   by_pill_type / by_drawer / by_customer rollups. Drives the
>   prune decision for the Phase E+F drawer/screener pills landed
>   on the retail side same day — observe usage, drop the bottom
>   pill categories after ~2 weeks of data. Linked from both nav
>   menus (subpage + dashboard hmenu).
> - **Customer Activity Report engine (2026-04-27)**: new `/customer-activity`
>   page on the command portal with form (customer dropdown + date-range
>   picker + report-type selector). Hits a new `/api/proxy/activity-report`
>   endpoint that proxies to the retail portal's admin-side report
>   generator (Bearer MONITOR_TOKEN auth) and renders the JSON response
>   inline. Lets the operator pull a per-customer activity rollup
>   (signals seen, approvals queued, gates failed, trades opened/closed,
>   P&L) without SSH'ing to pi5.
> - Data Provenance tab on `/system-architecture` (2026-04-24 / 2026-04-25):
>   4th tab in synthos_monitor.py's system map, 3-banded
>   parallel-thread visualization showing where each trade signal's
>   data came from (Alpaca News / Sector Screener / SEC EDGAR).
>   Animated SVG with flowing-dot rendering, click-to-isolate
>   per-band. Templates in `templates/system_map.html`.
> - company_vault.py log-clarification patch (2026-04-24): when no
>   active customers exist, the daily backup loop logs "No ACTIVE
>   customers in fleet — backup is a no-op (correct behavior)"
>   instead of cryptic "0/0 succeeded".
> - Cloudflare tunnel ingress: `portal.synth-cloud.com` (→ pi5),
>   `command.synth-cloud.com` (→ pi4b:5050),
>   `monitor.synth-cloud.com` (→ pi2w_monitor:5000),
>   `ssh.synth-cloud.com` (→ pi4b:22). Configured in
>   `/etc/cloudflared/config.yml` on pi4b.
>
> **Companion-repo activity (synthos / pi5) on 2026-04-25** —
> entire customer-facing dashboard / news / intel pages overhauled
> in a one-day sprint (~20 commits, Phases 5–7L). Sparklines,
> four specialized slide-out drawers (Position / History /
> Approval / Planning), Signal Trust widget replacing internal-
> score leak, news + watchlist page redesigns, two new endpoints
> (`/api/ticker-news`, `/api/ticker-context`), three new schema
> columns (`positions.entry_pattern`, `positions.entry_thesis`,
> `pending_approvals.entry_pattern`), watchlist wiring fix that
> changed `/api/watchlist` to read the signals table instead of
> news_feed. No company-side changes required for any of it —
> all retail. See `synthos/synthos_build/PROJECT_STATUS.md` Phase
> 7+ section for the full breakdown.
>
> **Companion-repo activity (synthos / pi5) on 2026-04-27** —
> trader-visibility audit landed three changes verifying Gate 5
> actually consumes every screener input:
> sector_screener.combined_score re-weighted 40/40/0/20 → 30/30/30/10
> so momentum is included in candidate ranking; ret_3m raw 3-month
> return persisted on sector_screening + surfaced on portal screener
> page + planning drawer; trader gate5 emits a single consolidated
> decision_log entry containing every screener field considered
> (visibility-only, zero behavior change). Plus documented intentional
> sentiment dual-write (sector_screening per-ticker vs signals
> per-signal — same detect_cascade computation). Earlier same day:
> MRVL trail-stop -491% display bug, settlement-lag race in Gate 0
> orphan adoption, rotation-at-loss reversed (winners-only), BIL
> excluded from Gate 10, CBOE put_call_ratio caching + None-safe
> formatting (had been pinning every screener-sentiment fulfilment
> to 0.5 since CBOE Cloudflare block), P&L report polish. No
> company-side changes required except the customer-activity report
> page noted above.
>
> **Single source of truth for live operational state lives in the
> companion repo:** `synthos/synthos_build/data/system_architecture.json`
> (v3.26 as of 2026-05-02).
>
> **REPO IDENTITY:** `personalprometheus-blip/synthos-company` — local: `/home/pi/synthos-company/`
> **This repo owns:** company_node (Pi 4B) — synthos_monitor (command portal), auditor, archivist, vault, librarian, sentinel, strongbox, scoop
> **Companion:** `synthos` owns retail_node (Pi 5) + master PROJECT_STATUS.md — do NOT put retail code here
> **Separate:** `Sentinel` repo is unrelated to Synthos

---

## HISTORICAL SNAPSHOT (frozen 2026-04-05) — preserved for audit trail

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
