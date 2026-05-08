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
| 1.2     | 2026-05-07 | Add `databases` (array) + `externals` (array)                         |
| 1.3     | 2026-05-07 | Add `data_flows` (array) — declared outbound connections per node     |

## Schema (v1.3)

```json
{
  "manifest_version": "1.3",
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
  "databases": [
    {
      "name": "company.db",
      "path": "/home/pi/synthos-company/data/company.db",
      "purpose": "customer registry, scoop queue, audit trail"
    }
  ],
  "externals": [
    {
      "name": "GitHub",
      "kind": "vcs",
      "purpose": "code repository + system_architecture.json source"
    }
  ],
  "data_flows": [
    {
      "kind": "mqtt-pub",
      "to": "pi4b-company",
      "purpose": "agent heartbeats published on broker"
    },
    {
      "kind": "api-call",
      "to": "external:Alpaca",
      "purpose": "paper trades + price quotes"
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
| `databases`        | array   | optional | DBs hosted by this node. Each entry `{name, path, purpose}`. Renderer shows them inside the node card under a "Databases" section. Phase 4 schema. |
| `externals`        | array   | optional | Outbound services this node depends on. Each entry `{name, kind, purpose}`. `kind` is a free-text classifier (e.g. `vcs`, `mail`, `mqtt`, `api`) used to pick a pill color. Phase 4 schema. |
| `data_flows`       | array   | optional | Outbound connections from this node. Each entry `{kind, to, purpose}`. `to` is either another `node_id` or `external:<name>` (matches an externals entry). The architecture page joins these across all manifests to show inbound + outbound per node, and (Phase 12b) draws SVG edges between positioned nodes. Phase 12 schema. |

### Agents array entries

| Field      | Type   | Required | Description |
|------------|--------|----------|-------------|
| `name`     | string | yes      | **Must match the `<agent>` segment in the agent's MQTT heartbeat topic.** Join key for live status. |
| `purpose`  | string | yes      | One-line human-readable description of what this agent does. |
| `cadence`  | string | optional | Free-text schedule descriptor, e.g. `continuous`, `daily 04:00 ET`, `intraday 09:30/13:30/16:00`. |



### Databases array entries (v1.2)

| Field      | Type   | Required | Description |
|------------|--------|----------|-------------|
| `name`     | string | yes      | Filename or display name (e.g. `company.db`, `signals.db`). |
| `path`     | string | yes      | Absolute filesystem path on the node. |
| `purpose`  | string | yes      | One-line description of what the DB stores (tables / responsibility). |

### Externals array entries (v1.2)

| Field      | Type   | Required | Description |
|------------|--------|----------|-------------|
| `name`     | string | yes      | Display name of the external service. |
| `kind`     | string | yes      | Free-text classifier. Suggested values: `vcs`, `mail`, `mqtt`, `api`, `db`, `auth`. Drives pill color. Unknown values render with a default neutral pill. |
| `purpose`  | string | yes      | One-line description of why this node connects to it. |


## Peer discovery (Phase 5)

Pi4b's `/api/manifests` reads its own manifest from disk and fetches
remote-node manifests via SSH. Per-node access details live in
`synthos-company/peer_nodes.json`:

```json
{
  "synthos-pi-retail": {
    "ssh_target": "SentinelRetail",
    "manifest_path": "~/manifest.json"
  }
}
```

`ssh_target` is an SSH host alias defined in pi4b's `~/.ssh/config`.
The universal installer adds one entry per new node to both pi4b's
SSH config (with appropriate keys) and to `peer_nodes.json` —
together they form the discovery channel that turns "plug a new Pi
in" into a fully-decorated card on the architecture page.

Manifests are cached for 5 minutes per peer. SSH timeout is 10s with
`BatchMode=yes` so the endpoint never hangs on an unreachable peer.

## Path is per-user `$HOME/manifest.json`

The manifest file resolves to `~/manifest.json` on each node. On pi4b
this is `/home/pi/manifest.json`; on pi5 it's
`/home/pi516gb/manifest.json`. The convention is "in the deploying
user's home directory" rather than a literal `/home/pi/` path — that
way every node's installer can use `$HOME/manifest.json` without
caring about the username.



### Data flows array entries (v1.3)

| Field      | Type   | Required | Description |
|------------|--------|----------|-------------|
| `kind`     | string | yes      | Transport classifier. Suggested values: `mqtt-pub`, `mqtt-sub`, `http-post`, `http-fetch`, `api-call`, `ssh-exec`, `git-fetch`, `git-push`, `mail-out`, `db-read`, `db-write`. Drives the row icon and color. |
| `to`       | string | yes      | Target identifier. Either a `node_id` (matches another manifest) or `external:<name>` (matches an `externals[]` entry by name on this same manifest). |
| `purpose`  | string | yes      | One-line description of the flow. |

Each manifest declares only its **outbound** flows. The architecture page
infers inbound flows for a node by scanning all manifests and finding
`to` references back to it. So one declaration per side is enough.

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

- `signed_at` / `signature` — future: optional manifest authenticity signing
- `size_estimate_gb` (database field) — future: hint for capacity planning
