# INFORMATION FLOW — WORKING DOCUMENT
**Type:** Living / working document — not a finalized spec
**Created:** 2026-03-31
**Addendum to:** architecture.md, SYNTHOS_TECHNICAL_ARCHITECTURE.md
**Purpose:** Figure out how information moves through the full system end-to-end.
Update this document as decisions are made or flows change.

---

## STATUS KEY
- ✅ Built and running
- 🔧 Built, not yet deployed/tested
- 🗂 Designed, not yet built
- ❓ Open question / undecided
- ⛔ Blocked

---

## SYSTEM-WIDE INFORMATION FLOW (CURRENT STATE)

```
EXTERNAL SOURCES
│
├── Congressional trading disclosures (Capitol Trades API, Congress.gov API)
├── RSS feeds (32 sources — news, gov, financial)
├── Alpaca Market Data (price bars, quotes)
├── SEC EDGAR (Form 4 insider filings)
├── Yahoo Finance RSS (per-ticker headlines)
├── Finviz (put/call ratio, volume profile)
│
▼
retail_node (Pi 2W)
│
├── Scout (news_agent.py) ✅
│     Pulls disclosures + RSS → scores signals → signals table (DB)
│     Also: fulfills screening news requests → sector_screening table
│
├── Pulse (market_sentiment_agent.py) ✅
│     Market-wide sentiment → scan_log table (DB)
│     Per-position cascade scan → urgent_flags if critical
│     Also: fulfills screening sentiment requests → sector_screening table
│
├── Sector Screener (sector_screener.py) 🔧
│     Runs before market open
│     Pulls XLE 5yr return + scores top 10 holdings (momentum)
│     Writes candidates → sector_screening table
│     Issues requests → screening_requests table (for Scout + Pulse)
│     Congressional check → flags recent buys/sells from signals table
│     Audit log → logs/logic_audits/YYYY-MM-DD_sector_screener.log
│
├── Bolt (trade_logic_agent.py) ✅
│     Reads signals table → 14-gate evaluation → trade or pass
│     Audit log → logs/logic_audits/YYYY-MM-DD_bolt_decisions.log
│
└── Portal (portal.py) ✅
      Dashboard, Intelligence, Screening tabs
      Reads: signals, positions, portfolio, sector_screening, scan_log
      /api/screening → sector_screening latest run
```

---

## SECTOR SCREENING INFORMATION FLOW

```
sector_screener.py (runs ~9:00 AM ET)
│
├── Alpaca API → XLE 5-year price history → calc return
├── Alpaca API → each holding's 1-year bars → momentum score
│     Score components: 3m return (50%) + SMA position (30%) + volume trend (20%)
│
├── Writes to DB:
│     sector_screening rows (10 candidates, status=considering)
│     screening_requests rows (20 rows: 10 news + 10 sentiment, status=pending)
│
├── Checks signals table → congressional flags per ticker
│
└── Writes: logs/logic_audits/YYYY-MM-DD_sector_screener.log

                    ↓ (next Scout run)
Scout (news_agent.py)
├── get_pending_screening_requests('news')
├── For each ticker → Yahoo Finance RSS headlines
├── Score headlines with existing keyword sentiment logic
├── fulfill_screening_request() → updates sector_screening
│     news_signal, news_score, news_headline
│     Recomputes combined_score
└── Writes: logs/logic_audits/YYYY-MM-DD_scout_screening.log

                    ↓ (next Pulse run)
Pulse (market_sentiment_agent.py)
├── get_pending_screening_requests('sentiment')
├── For each ticker → put/call ratio + SEC insider + volume profile
├── Tier → sentiment signal + score
├── fulfill_screening_request() → updates sector_screening
│     sentiment_signal, sentiment_score, notes
│     Recomputes combined_score
└── Writes: logs/logic_audits/YYYY-MM-DD_pulse_screening.log

                    ↓ (portal)
Portal — Screening Tab
└── /api/screening → get_latest_screening_run()
      Shows: rank, ticker, company, ETF weight,
             news signal + headline, sentiment signal + score,
             congressional flag, combined score /100
```

### Combined Score Formula (current)
```
combined_score = news_score * 0.40
              + sentiment_score * 0.40
              + etf_weight_normalised * 0.20
```
❓ **Open:** Is 40/40/20 the right weighting? Congressional signal not yet factored into score — should it add a modifier?

---

## CONGRESSIONAL SIGNAL FLOW (EXISTING)

```
Capitol Trades API / Congress.gov API
│
└── Scout → classify → signals table
      status: QUEUED → Bolt picks up
      status: WATCH_ONLY → Intelligence tab only
      status: DISCARDED → logged, not acted on

      In sector screening context:
      └── signals table checked for ticker match (90-day lookback)
            congressional_flag = recent_buy | recent_sell | none
            Written to sector_screening as supplemental context
            NOT yet factored into combined_score — ❓ should it be?
```

---

## PROCESS NODE PIPELINE (PLANNED)

```
process_node (Pi 4B 4GB — to purchase)
│
├── Ingest agents (not yet built):
│     Alpaca news API, government APIs, press releases,
│     RSS feeds (offloaded from retail_node),
│     targeted social media (public figures, public accounts only)
│
├── Scan pipeline:
│     Scout-equivalent running on process_node
│     Pulse-equivalent running on process_node
│     (retail_node agents may be deprecated/redirected here)
│
├── Validation stack (decision logic):
│     Article/signal arrives → gate-based rules
│     Check: credibility, recency, relevance, contradictions
│     If passes → mark for enrichment
│
├── Redis (messaging layer):
│     Streams: pipeline handoff between ingest → scan → validate → enrich
│     Pub/Sub: fan-out to company_node portal + retail_node portal
│
└── Distribution:
      Enriched signals → company_node (company DB)
      Enriched signals → retail_node (signals DB via Redis or direct push)
```

❓ **Open questions:**
- Does retail_node keep running its own Scout/Pulse, or does process_node replace them?
- How does retail_node receive enriched signals from process_node — Redis Pub/Sub or HTTP push?
- Does the sector screener move to process_node or stay on retail_node?
- What does the validation stack actually gate on — source credibility? Contradiction detection? Both?

---

## CUSTOMER PI INFORMATION FLOW (PLANNED)

```
Customer Pi (Pi Zero 2W — at customer's home)
│
├── FIRST BOOT (AP mode):
│     Boots as WiFi hotspot "Synthos-Setup"
│     Customer connects via phone → setup form
│     Collects: WiFi creds, username/password, Alpaca key,
│              backup Gmail, phone, email
│     Device info pulled silently: MAC address, Pi serial
│     On save → write configs → reboot
│
├── POST-REBOOT (normal mode):
│     Connects to customer's home WiFi
│     Phones home → POST company_node /api/register
│          { device_id, email, phone }
│
├── Company Pi (registration):
│     Validates email against subscription list (manual for now)
│     Assigns customer_id
│     Creates Cloudflare tunnel + Access policy (Cloudflare API)
│     Issues secret token (JWT or random)
│     Adds to master user list (SQLite)
│     Returns { customer_id, token, portal_url }
│     Sends welcome email FROM support@cloud-synth.com
│          Contains: portal URL, connection instructions
│
├── Customer Pi stores:
│     token, customer_id, portal_url in local config
│     Token used for all future company Pi communication
│
└── Customer access:
      customer-id.synth-cloud.com → Cloudflare Access (email OTP)
      → Cloudflare tunnel → Customer Pi → portal (port 5001)
      Local WiFi: http://[local-ip]:5001 (always works, no Cloudflare)
```

**Subscription kill switch:**
Company Pi calls Cloudflare API → disables customer's Access policy
→ Remote access via Cloudflare stops immediately
→ Local WiFi access unaffected (portal still running)

❓ **Open questions:**
- What does the portal serve to the customer vs. the operator (you)?
- Does each customer Pi get its own Alpaca account/key, or is there a shared key?
- How does the customer Pi receive enriched signals from process_node?
  (Currently retail_node reads its own local DB — customer Pi needs a feed)
- What does "their trading portal" show? Their paper portfolio? Live signals?
- Registration endpoint — does it need auth itself, or is it open (device_id is the auth)?

---

## INTER-AGENT COMMUNICATION (CURRENT + PLANNED)

### Current (retail_node only — SQLite DB)
```
sector_screener → screening_requests table → Scout reads on next run
sector_screener → screening_requests table → Pulse reads on next run
Scout → signals table → Bolt reads on next run
Pulse → scan_log table → Bolt reads Gate 5 sentiment weight
Pulse → annotate_signal_pulse() → signals table → Bolt Gate 5
```

### Planned (cross-node — Redis)
```
process_node ingest → Redis Stream → process_node validation
process_node validation → Redis Pub/Sub → retail_node signals DB
process_node validation → Redis Pub/Sub → company_node portal
```

❓ **Open:** When does the DB-based inter-agent pattern on retail_node get replaced by Redis?
   Redis adds complexity; DB is simpler but only works within one node.
   Likely stays DB on retail_node, Redis only for cross-node.

---

## AUDIT LOG LOCATIONS

| Agent | Log location | Format |
|---|---|---|
| Sector Screener | `logs/logic_audits/YYYY-MM-DD_sector_screener.log` | Human-readable text |
| Scout (screening) | `logs/logic_audits/YYYY-MM-DD_scout_screening.log` | Human-readable text |
| Pulse (screening) | `logs/logic_audits/YYYY-MM-DD_pulse_screening.log` | Human-readable text |
| Bolt | `logs/logic_audits/YYYY-MM-DD_bolt_decisions.log` | Gate-by-gate decision trace |
| All agents | `logs/*.log` | Standard Python logging |
| All agents | `system_log` table in signals.db | Structured, queryable |

❓ **Open:** Should audit logs rotate (one file per run, not per day)?
   Bolt runs 3x/day — current design appends to one daily file. Fine for now.

---

## DATA STORES — FULL MAP

| Store | Node | What lives here | Who writes | Who reads |
|---|---|---|---|---|
| signals.db | retail_node | signals, positions, portfolio, scan_log, sector_screening, screening_requests | Scout, Pulse, Bolt, Sector Screener | Bolt, Portal, Pulse |
| company.db | company_node | accounts, suggestions, deploy_watches, heartbeats | Company agents | Blueprint, Sentinel |
| master user list | company_node | customer_id, email, token, tunnel_id, status | Registration endpoint | Portal routing, subscription check |
| Redis (planned) | process_node | signal pipeline streams, pub/sub fan-out | Process node agents | Retail portal, company portal |

---

## OPEN DESIGN QUESTIONS (running list)

1. **Sector expansion** — When adding a second sector (e.g. Technology/XLK), does sector screener run both in one pass or in separate scheduled runs?
2. **Score weighting** — Combined score is 40/40/20. Should congressional signals add a ±modifier? Should ETF weight carry more when sectors are concentrated?
3. **Screening → Bolt handoff** — Right now screening candidates sit in the DB but Bolt doesn't proactively act on them. What triggers Bolt to consider a screened stock? High combined score + fresh signal? Or does the screener write a synthetic signal into the signals table?
4. **Process node signal feed** — How do enriched signals from process_node reach the customer Pi portal? Direct DB push via SSH? Redis subscription? HTTP endpoint?
5. **Customer Pi portal content** — Does each customer see the same signals (Energy sector screening, congressional disclosures) or is it personalised?
6. **Validation stack design** — What are the specific gates? Likely: source credibility, recency check, contradiction with existing signals, cross-signal corroboration. Needs a spec.
7. **Redis timing** — When does Redis get installed on process_node? Before or after the validation stack is built?
8. **Sector screener schedule** — Currently manual run. Needs cron entry. When: 9:00 AM ET before market open? Daily only or also midday refresh?

---

## DECISIONS MADE (log)

| Date | Decision | Rationale |
|---|---|---|
| 2026-03-31 | process_node hardware: Pi 4B 4GB | Decision logic stack is rule-based, not compute-heavy; 4GB handles 50-100 accounts; Pi 5 overkill |
| 2026-03-31 | Pi 3 returned | No longer needed; Pi 4B 4GB replaces it |
| 2026-03-31 | Validation stack = decision logic (rule-based) | Not LLM inference; bottleneck will be Redis/SQLite, not CPU |
| 2026-03-31 | Customer Pi WiFi provisioning via AP mode captive portal | Pi Zero 2W has no Ethernet; AP mode is the only plug-and-play option |
| 2026-03-31 | Customer remote access via per-customer Cloudflare tunnel + email OTP | Clean kill switch (disable Access policy); local WiFi always works |
| 2026-03-31 | support@cloud-synth.com for customer-facing email | Separate from alerts@cloud-synth.com; Cloudflare Email Routing → iCloud |
| 2026-03-31 | Email relay stays external (Resend, replacing SendGrid) | Residential IP blacklisting + port 25 blocking make self-hosted sending unreliable |
| 2026-03-31 | Sector screening: Energy / XLE baseline | Starting with one sector to establish the pattern before expanding |
| 2026-03-31 | Congressional signals = supplemental in screening (not primary) | Primary signal is sector momentum + news + sentiment; congressional is corroborating data |
| 2026-03-31 | Inter-agent communication on retail_node stays DB-based | Redis only for cross-node (process_node → retail/company); DB is simpler within one node |
