# Customer Portal Setup — Operator Runbook
**Version:** 1.0
**Date:** 2026-03-30
**Status:** Partially complete — steps marked ⬜ require manual action

---

## Overview

Customers log in at `portal.synth-cloud.com` (company node), receive a signed SSO token, and are redirected to their assigned retail node portal at `app.synth-cloud.com`. All auth flows through the company node. The retail portal is never accessed directly.

```
Customer → portal.synth-cloud.com → login_server/app.py (port 5050)
                                          │ SSO token (15 min)
                                          ▼
                               app.synth-cloud.com/sso?t=...
                               portal.py /sso endpoint
                                          │ session set
                                          ▼
                               Retail portal dashboard
```

---

## Prerequisites

- Company node (Pi 4B) running, `cloudflared` healthy
- Retail node (Pi 2W) running, portal.py reachable on port 5001
- Python packages on company node: `flask`, `werkzeug`, `itsdangerous`
- Python packages on retail node: `itsdangerous` (likely already present via Flask)
- Cloudflare account with `synth-cloud.com` zone

---

## Step 1 — Generate SSO Secret

Run on the company node. This value must be identical on both nodes.

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output. You will use it in Steps 2 and 3.

---

## Step 2 — Set SSO Secret on Company Node

Edit `/home/pi/synthos-company/company.env`:

```bash
nano /home/pi/synthos-company/company.env
```

Fill in the following lines:

```
SSO_SECRET=<value from Step 1>
LOGIN_SERVER_PORT=5050
LOGIN_SESSION_SECRET=<run: python3 -c "import secrets; print(secrets.token_hex(32))">
```

`company.env` is gitignored — changes are local only.

---

## Step 3 — Set SSO Secret on Retail Node ⬜

SSH to the retail Pi:

```bash
ssh pi@ssh2.synth-cloud.com
```

Edit the retail node's environment file:

```bash
nano /home/pi/synthos/synthos_build/user/.env
```

Add:

```
SSO_SECRET=<same value from Step 1>
LOGIN_SERVER_URL=https://portal.synth-cloud.com
```

Then restart the portal:

```bash
sudo systemctl restart portal   # or however portal.py is started
```

---

## Step 4 — Add DNS Records in Cloudflare ⬜

In the Cloudflare dashboard for `synth-cloud.com`, add two CNAME records:

| Name | Type | Target | Proxy |
|------|------|--------|-------|
| `portal` | CNAME | `9b277739-29ec-463f-86e4-13ea3fc4305c.cfargotunnel.com` | Proxied (orange) |
| `app` | CNAME | `419ec665-f5c2-4bc3-b338-fbc6d02094a9.cfargotunnel.com` | Proxied (orange) |

Alternatively, use the CLI from the company node:

```bash
cloudflared tunnel route dns 9b277739-29ec-463f-86e4-13ea3fc4305c portal.synth-cloud.com
cloudflared tunnel route dns 419ec665-f5c2-4bc3-b338-fbc6d02094a9 app.synth-cloud.com
```

---

## Step 5 — Update Retail Node Cloudflare Tunnel Config ⬜

SSH to the retail Pi:

```bash
ssh pi@ssh2.synth-cloud.com
```

Edit `/home/pi/.cloudflared/config.yml` and add the portal ingress rule **before** the catch-all:

```yaml
ingress:
  - hostname: app.synth-cloud.com
    service: http://localhost:5001
  - hostname: ssh2.synth-cloud.com
    service: ssh://localhost:22
  - service: http_status:404
```

Restart the tunnel:

```bash
sudo systemctl restart cloudflared
```

---

## Step 6 — Restart Company Node Tunnel ⬜

The company node tunnel config already has `portal.synth-cloud.com` added (Step done in code).
Restart it to pick up the new ingress rule:

```bash
sudo systemctl restart cloudflared
```

Verify tunnel is healthy:

```bash
sudo systemctl status cloudflared
cloudflared tunnel info 9b277739-29ec-463f-86e4-13ea3fc4305c
```

---

## Step 7 — Install itsdangerous on Retail Node (if needed) ⬜

```bash
ssh pi@ssh2.synth-cloud.com
pip3 install itsdangerous
```

Check if already present:

```bash
python3 -c "import itsdangerous; print('ok')"
```

---

## Step 8 — Add First Customer Account ⬜

Run on the company node:

```bash
cd /home/pi/synthos-company
python3 login_server/create_user.py --add \
  --email customer@example.com \
  --password <strong-password> \
  --node-url https://app.synth-cloud.com \
  --node-id retail_1
```

Verify the account was created:

```bash
python3 login_server/create_user.py --list
```

---

## Step 9 — Start Login Server ⬜

For testing, start manually:

```bash
cd /home/pi/synthos-company
python3 login_server/app.py
```

For production, add a systemd service (see Step 10).

---

## Step 10 — Add Login Server to systemd ⬜

Create `/etc/systemd/system/synthos-login.service`:

```ini
[Unit]
Description=Synthos Customer Login Server
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/synthos-company/login_server
EnvironmentFile=/home/pi/synthos-company/company.env
ExecStart=/usr/bin/python3 /home/pi/synthos-company/login_server/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable synthos-login
sudo systemctl start synthos-login
sudo systemctl status synthos-login
```

---

## End-to-End Verification

After all steps are complete, run through this checklist:

- [ ] Browse to `https://portal.synth-cloud.com` — login page loads over HTTPS
- [ ] Login with a valid account — dashboard appears, shows node ID
- [ ] Click "Launch Portal" — redirects to `app.synth-cloud.com/sso?t=...`
- [ ] Retail portal dashboard loads — portfolio/positions visible
- [ ] Login with wrong password — error message shown, no redirect
- [ ] Wait 15+ minutes, try to reuse a captured token URL — redirected back to login with `token_expired`
- [ ] Sign out — session cleared, redirected to login

---

## Adding a Second Customer (Future)

When a new customer's retail node is provisioned:

1. Set `SSO_SECRET` (same shared value) in their retail node's `user/.env`
2. Add HTTP ingress to their tunnel config: `customer2.synth-cloud.com → localhost:5001`
3. Restart their cloudflared
4. Add DNS: `cloudflared tunnel route dns <their-tunnel-id> customer2.synth-cloud.com`
5. Add their account: `python3 login_server/create_user.py --add --email ... --node-url https://customer2.synth-cloud.com --node-id retail_2`

No code changes required.

---

## User Management Reference

```bash
# Add user
python3 login_server/create_user.py --add --email x@x.com --password abc --node-url https://app.synth-cloud.com --node-id retail_1

# List all users
python3 login_server/create_user.py --list

# Deactivate user (blocks login, data preserved)
python3 login_server/create_user.py --deactivate --email x@x.com

# Reset password
python3 login_server/create_user.py --reset-password --email x@x.com --password newpass
```

---

## Key Files Reference

| File | Location | Purpose |
|------|----------|---------|
| `login_server/app.py` | company node | Flask login server |
| `login_server/create_user.py` | company node | User management CLI |
| `company.env` | company node | SSO_SECRET, LOGIN_SERVER_PORT |
| `portal.py` | retail node | `/sso` endpoint (added) |
| `user/.env` | retail node | SSO_SECRET, LOGIN_SERVER_URL |
| `/home/pi/.cloudflared/config.yml` | company node | portal.synth-cloud.com ingress (added) |
| `/home/pi/.cloudflared/config.yml` | retail node | app.synth-cloud.com ingress (⬜ to add) |
