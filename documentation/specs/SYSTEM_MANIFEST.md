# SYNTHOS — SYSTEM MANIFEST — RETIRED 2026-05-04

This manifest was formally retired on 2026-05-04 after a full audit
confirmed it had drifted from the live system in fundamental ways
(declared three-node architecture with cancelled process_node, listed
unused Redis as inter-node comms, FILE_REGISTRY referenced retired
agent filenames). The OUTDATED banner had been carrying the redirects
since 2026-04-23.

## Where to find current truth

- **System inventory + topology:**
  [`synthos_build/data/system_architecture.json`](../../synthos_build/data/system_architecture.json) (v3.29+)
- **Phase state + cross-repo blockers:**
  [`synthos_build/PROJECT_STATUS.md`](../../synthos_build/PROJECT_STATUS.md)
- **Retail node operational status:**
  [`synthos_build/STATUS.md`](../../synthos_build/STATUS.md)
- **Architecture maintenance contract:**
  [`synthos_build/CLAUDE.md`](../../synthos_build/CLAUDE.md) (Architecture Doc Maintenance section)
- **Distributed-trader operational docs:**
  [`synthos_build/docs/`](../../synthos_build/docs/) — CUTOVER_RUNBOOK, TRADER_GATE_IO_AUDIT, INSTALLER_PROFILES, MQTT_BROKER_OPERATIONS, WORK_PACKET_PROTOCOL, DISPATCH_MODE_GUIDE

## Still-valuable sections (where they ended up)

| §  | Topic | Now lives in |
|----|-------|---|
| §2 | SYSTEM_PATHS | INSTALLER_PROFILES.md (per-profile path scoping) |
| §7 | DEPENDENCY_GRAPH | derivable via grep / no canonical home; orchestration_master_plan memory has tier-by-tier component list |
| §8 | ENV_SCHEMA | install_retail.py + install_retail_node.py + install_company.py each carry their profile's required vars |
| §11 | UPGRADE_RULES | CUTOVER_RUNBOOK.md captures the customer-by-customer migration discipline; CLAUDE.md captures the architecture-sync contract |

## Historical version

The retired doc is preserved at
`documentation/archive/specs/SYSTEM_MANIFEST.md.retired_2026-05-04`.
