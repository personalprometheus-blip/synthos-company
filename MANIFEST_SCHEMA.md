# Synthos Node Manifest Schema

Each Synthos node carries a self-description at `/home/pi/manifest.json`.
The architecture page joins this onto live heartbeat data to render the
topology — node identity comes from the heartbeat, descriptive content
comes from the manifest.

This file is the **contract between the universal installer and the
architecture page**. The installer writes the manifest at install time;
the architecture page reads it (locally on pi4b, via SSH on remote
nodes once Phase 5 ships).

## Path

`/home/pi/manifest.json` — fixed absolute path on every node, regardless
of which repo runs there. Same path for retail nodes (synthos repo),
company nodes (synthos-company repo), and any future role.

## Versioning

The `manifest_version` field is **semver-bumped on any structural
change**. Additive changes (new optional fields) bump minor; renames or
removals bump major. The architecture page renderer is tolerant — it
falls back gracefully when an expected field is missing, but the
contract is the floor below which no field can drop.

| Version | Date       | Change |
|---------|------------|--------|
| 1.0     | 2026-05-07 | Initial: identity-only (node_id, label, role, hardware, deployed_at) |
| 1.1     | 2026-05-07 | Add `mqtt_group` (string) + `agents` (array)                          |

## Schema (v1.1)

```json
{
  "manifest_version": "1.1",
  "node_id": "pi4b-company",
  "label": "Company Operations Node",
  "role": "company_ops",
  "hardware": "Raspberry Pi 4B",
  "deployed_at": "2026-04-15",
  "mqtt_group": "company",
  "agents": [
    {
      "name": "synthos_monitor",
      "purpose": "command portal HTTP server (:5050)",
      "cadence": "continuous"
    }
  ],
  "updated_at": "2026-05-07T17:00:00Z"
}
```

### Field reference

| Field              | Type    | Required | Description |
|--------------------|---------|----------|-------------|
| `manifest_version` | string  | yes      | Semver. Bump on any structural change. Renderer reads this to pick a fallback strategy. |
| `node_id`          | string  | yes      | **Must match the `pi_id` field in the node's heartbeat payload.** This is the join key. |
| `label`            | string  | yes      | Pretty name shown on the node card. Overrides the heartbeat's `label` if both present. |
| `role`             | string  | yes      | Stable role identifier. Drives the role-pill color on the card. Suggested values: `company_ops`, `retail`, `sentinel`. Unknown values render with a default purple pill. |
| `hardware`         | string  | yes      | Free-text hardware description, e.g. "Raspberry Pi 4B", "Pi 5 16GB / NVMe". |
| `deployed_at`      | string  | yes      | ISO date the node was provisioned. |
| `mqtt_group`       | string  | optional | The `<node>` segment in the MQTT topic `process/heartbeat/<node>/<agent>` used by this node's publishers. The architecture page uses this to look up live agent freshness from `/api/agents/status`. Omit if the node has no agents publishing MQTT heartbeats. |
| `agents`           | array   | optional | Agents this node owns. Each entry is `{name, purpose, cadence}`. The renderer joins each entry onto MQTT freshness via `mqtt_group` + `name`. |
| `updated_at`       | string  | yes      | ISO timestamp the manifest was last written. The installer sets this when it writes the file. |

### Agents array entries

| Field      | Type   | Required | Description |
|------------|--------|----------|-------------|
| `name`     | string | yes      | **Must match the `<agent>` segment in the agent's MQTT heartbeat topic.** Join key for live status. |
| `purpose`  | string | yes      | One-line human-readable description of what this agent does. |
| `cadence`  | string | optional | Free-text schedule descriptor, e.g. `continuous`, `daily 04:00 ET`, `intraday 09:30/13:30/16:00`. |

## Installer integration notes

- Each node's installer renders this file from a template that knows
  the node's role, expected agents, and MQTT group.
- The installer **must** set `node_id` to the same value the node's
  heartbeat publisher will send as `pi_id`. Mismatch = card shows up
  but stays `manifest: missing`.
- The installer writes `updated_at` at provision time. If the manifest
  is hand-edited later, whoever edits should update it.
- Manifest is NOT versioned in git on the deployed node — it lives at
  `/home/pi/manifest.json` outside any repo. Source-of-truth template
  belongs in the installer's own repo.

## Future schema additions (not yet contracted)

These are reserved for future phases — the installer should not write
them yet, and the renderer does not yet read them:

- `databases` (array) — Phase 4: each entry `{name, path, purpose, size_estimate_gb}`
- `externals` (array) — Phase 4: outbound services, e.g. Alpaca, GitHub, SMTP
- `data_flows` (array) — Phase 4+: declared edges between this node and others/externals
- `signed_at` / `signature` — future: optional manifest authenticity signing
