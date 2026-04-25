# Synthos — Company Node

The operator-side intelligence layer for the Synthos trading assistant system. Manages licensing, deployment, monitoring, alerting, backups, and code quality for all retail nodes.

> **Current state (2026-04-25).** The sections below describe the
> repo's role generically. For the live operational map of what's
> running where, see the companion repo:
> `synthos/synthos_build/data/system_architecture.json` (v3.13).
> Today this repo deploys synthos_monitor.py (command portal at
> `command.synth-cloud.com`) plus auditor / archivist / vault /
> librarian / sentinel / strongbox / fidget on pi4b. The Cloudflare
> tunnel that fronts pi4b also routes `portal.synth-cloud.com` →
> pi5 (retail node).

## Status
See [STATUS.md](./STATUS.md) — historical Phase 1-5 record + current-state summary at the top.

## Hardware

The company node runs on any Linux host with Python 3.9+. It is not tied to specific hardware.

| Supported | Examples |
|-----------|---------|
| Single-board computers | Raspberry Pi 4B, Pi 5, Pi 400 |
| Cloud VMs | AWS EC2, GCP Compute, DigitalOcean Droplet |
| Local servers | Any Linux x86/ARM machine |
| Containers | Docker (Linux) — no Pi-specific dependencies |

The only constraint is persistent storage for `data/company.db` and network reachability from retail nodes to `MONITOR_URL`.

## What This Node Does

- **blueprint.py** — Deploys approved code suggestions to retail nodes via staging workflow
- **sentinel.py** — Receives heartbeats from retail Pis; alerts on silence (port 5004)
- **vault.py** — License key generation and management for retail nodes
- **patches.py** — Continuous code audit; finds bugs and posts suggestions
- **librarian.py** — Package CVE scanning and documentation management
- **fidget.py** — User feedback processing
- **scoop.py** — Sole outbound email/alert sender for the entire system
- **timekeeper.py** — DB write slot coordination across all agents
- **strongbox.py** — Automated backups of retail node state

Also runs **synthos_monitor.py** (port 5000) — heartbeat receiver and console UI.

## Structure

```
/agents      All company node agents
/utils       Shared DB helpers (db_helpers.py) and path resolution
/installers  Installer common components
/data        company.db and runtime data files (gitignored)
/logs        Agent log files (gitignored)
/config      Configuration files
```

## Relation to Retail Repo

This repo is the **authority domain**. It validates and manages retail nodes.
Retail node code lives in the separate [`synthos`](https://github.com/personalprometheus-blip/synthos) repo.

- Company validates retail — not the other way around
- No retail code belongs in this repo
- See `docs/governance/COMPANY_INTEGRITY_GATE_SPEC.md` in the retail repo for the company boot contract

## Setup

```bash
python3 install_company.py
```

Requires Python 3.9+. No Pi-specific packages. Runs on any Linux host.
