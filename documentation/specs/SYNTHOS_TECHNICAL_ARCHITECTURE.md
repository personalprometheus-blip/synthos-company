# SYNTHOS TECHNICAL ARCHITECTURE — RETIRED 2026-05-04

This document was formally retired on 2026-05-04 after a full audit
confirmed it had drifted from the live system in fundamental ways
(referenced cancelled process_node, unused Redis, retired agent
filenames, Pi 2W as retail). The OUTDATED banner had been carrying
the redirects since 2026-04-23 but the doc was kept around for
historical context.

The 2026-05-04 distributed-trader migration (Tiers 1-7) added enough
new components that maintaining the v4.0 spec parallel to reality
created more confusion than value. Decision made + tracked in
synthos/TODO.md and the orchestration_master_plan memory.

## Where to find current truth

- **System inventory + topology + gates + modes + tiers + telemetry:**
  [`synthos_build/data/system_architecture.json`](../../synthos_build/data/system_architecture.json) (v3.29+)
- **Operational migration playbook:**
  [`synthos_build/docs/CUTOVER_RUNBOOK.md`](../../synthos_build/docs/CUTOVER_RUNBOOK.md)
- **Per-gate I/O catalog:**
  [`synthos_build/docs/TRADER_GATE_IO_AUDIT.md`](../../synthos_build/docs/TRADER_GATE_IO_AUDIT.md)
- **Installer profiles:**
  [`synthos_build/docs/INSTALLER_PROFILES.md`](../../synthos_build/docs/INSTALLER_PROFILES.md)
- **MQTT broker operations:**
  [`synthos_build/docs/MQTT_BROKER_OPERATIONS.md`](../../synthos_build/docs/MQTT_BROKER_OPERATIONS.md)
- **HTTP work-packet protocol:**
  [`synthos_build/docs/WORK_PACKET_PROTOCOL.md`](../../synthos_build/docs/WORK_PACKET_PROTOCOL.md)
- **Per-customer dispatch mode:**
  [`synthos_build/docs/DISPATCH_MODE_GUIDE.md`](../../synthos_build/docs/DISPATCH_MODE_GUIDE.md)
- **Network config:**
  [`synthos/.claude/NETWORK_CONFIG.md`](../../../synthos/.claude/NETWORK_CONFIG.md) (v2 with AS-BUILT corrections)

## Historical version

The retired doc is preserved at
`documentation/archive/specs/SYNTHOS_TECHNICAL_ARCHITECTURE.md.retired_2026-05-04`.
Read it for context only; do NOT use for architectural decisions.
