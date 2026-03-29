# CLAUDE.md — Company Node Context for AI Agents

## Project Name
Synthos — Company Node (synthos-company)

## What This Node Does
Operator-side intelligence layer: manages licensing, deployment, monitoring, alerting,
backups, and code quality for all Synthos retail nodes. Not customer-facing.

## Hardware Note
The company node is hardware-agnostic. It runs on any Linux host with Python 3.9+:
Raspberry Pi 4B/5, cloud VM (AWS/GCP/DigitalOcean), local server, or Docker container.
The only requirement is persistent storage for company.db and network access from retail nodes.

## Current Phase
Phase 3 — Normalization Sprint (same phase as retail repo)

## Companion Repo
Retail node code → https://github.com/personalprometheus-blip/synthos
Company node is the authority domain. Retail is the validated domain.

## Where To Find Things
- **Master project status** → synthos repo: PROJECT_STATUS.md (phases, cross-repo blockers, overall progress)
- **This node's status** → STATUS.md (company node operational health)
- Agent source code → agents/
- Shared DB utilities → utils/db_helpers.py
- Path resolution → utils/synthos_paths.py
- Installer → install_company.py
- Integrity gate spec → see retail repo: docs/governance/COMPANY_INTEGRITY_GATE_SPEC.md

## Agent Roster (agents/)
| File | Role | Write path |
|------|------|-----------|
| blueprint.py | Deploy approved suggestions | db_helpers.post_suggestion() |
| sentinel.py | Heartbeat monitor (port 5004) | db_helpers.post_suggestion() ✅ migrated |
| vault.py | License key management | db_helpers.post_suggestion() ✅ migrated |
| patches.py | Continuous code audit | db_helpers.post_suggestion() |
| librarian.py | CVE scanning, docs | db_helpers.post_suggestion() ✅ migrated |
| fidget.py | Feedback processing | db_helpers.post_suggestion() |
| scoop.py | Sole outbound email sender | db_helpers (scoop queue) |
| timekeeper.py | DB slot coordination | db_helpers (slots table) |
| strongbox.py | Automated backups | — (NEEDS MOVE from retail repo) |

## Critical Rules
- scoop.py is the ONLY permitted outbound email sender
- All suggestion writes go via db_helpers.post_suggestion() — never write suggestions.json directly
- All deploy watch writes go via db_helpers.post_deploy_watch() — never write post_deploy_watch.json directly
- COMPANY_MODE=true must be set in company.env — required by integrity gate
- Never import or call license_validator.py — that is retail-only

## Known Open Issues
- strongbox.py is currently in the retail repo (src/) — must be moved here (Step 4 of normalization)
- company.db schema is undocumented — needs PRAGMA table_info extraction (CL-012)
- TOOL_DEPENDENCY_ARCHITECTURE.md does not classify company agents (CL-009)

## How To Update Progress
When a task is complete:
1. Update STATUS.md in this repo (company node health)
2. Check off in PROJECT_STATUS.md in the retail repo (master tracker)
3. Commit both repos: `git commit -m "progress: [what was completed]"`
