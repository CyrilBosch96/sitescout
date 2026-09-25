# Deploying to the GCP VM

Reference files for the always-on cloud setup — `admin.example.com` →
Cloudflare (Access + Tunnel) → this VM's dashboard, running entirely on
Google Cloud's Always Free e2-micro tier.

## Why Cloudflare Tunnel instead of a plain DNS A record

Originally planned as "A record → VM's public IP", but a **Tunnel** is
strictly better here: `cloudflared` runs on the VM and makes an
*outbound* connection to Cloudflare — nothing needs to be exposed on the
VM's firewall at all beyond SSH. No public IP ever gets pointed at
`admin.example.com`, no port needs opening for the dashboard. Pairs
directly with Cloudflare Access, which already handles the login gate.

## One-time setup on the VM

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv git cron

git clone <your private repo URL> sitescout
cd sitescout
python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env   # fill in real secrets — paste over SSH, never via git

# himalaya — binary install is fine to script, but Cyril runs `account
# configure` himself (real email credentials, never entered by an agent)
curl -sSL https://github.com/pimalaya/himalaya/releases/latest/download/himalaya.x86_64-linux.tgz | tar xz
sudo mv himalaya /usr/local/bin/
himalaya account configure   # walks through the same sitescout IMAP/SMTP setup as the Mac

# state.db copied once from the Mac (preserves run/settings history):
#   scp state.db <vm-user>@<vm-ip>:~/sitescout/state.db
```

### Dashboard — systemd (see `sitescout-dashboard.service`)

```bash
sudo cp deploy/sitescout-dashboard.service /etc/systemd/system/
sudo sed -i "s/REPLACE_WITH_VM_USERNAME/$(whoami)/g" /etc/systemd/system/sitescout-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now sitescout-dashboard
```

### Orchestrator — cron (every 10 min, `--live`; cron doesn't sleep, unlike the Mac)

```bash
crontab -e
# add (PATH line is required — cron's default PATH is just /usr/bin:/bin,
# which doesn't include /usr/local/bin where himalaya lives; found live
# 2026-08-27, every run silently failed on inbox_monitoring.py and
# ghost_template_request.py for ~13 hours before this was caught):
PATH=/usr/local/bin:/usr/bin:/bin
*/10 * * * * cd /home/YOUR_USER/sitescout && venv/bin/python3 scripts/orchestrator.py --live >> orchestrator.log 2>&1
```

### Cloudflare Tunnel

```bash
# install cloudflared (see Cloudflare's docs for the current apt repo/package)
cloudflared tunnel login
cloudflared tunnel create sitescout-admin
cloudflared tunnel route dns sitescout-admin admin.example.com
# config at ~/.cloudflared/config.yml:
#   tunnel: sitescout-admin
#   credentials-file: /home/YOUR_USER/.cloudflared/<tunnel-id>.json
#   ingress:
#     - hostname: admin.example.com
#       service: http://127.0.0.1:5051
#     - service: http_status:404
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

Then in the Cloudflare dashboard: Zero Trust → Access → Applications →
add `admin.example.com`, policy allowing only Cyril's email.

## Click-tracking Worker (`track.example.com`)

Source: `tracking-worker.js`. Blocked until the Cloudflare API token has
**Account > Workers Scripts > Edit** (Cloudflare dashboard → My Profile
→ API Tokens → edit the existing token, same one used for ghost sites).
Once that's granted:

```bash
# 1. Create the Worker and upload the script
curl -X PUT "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/workers/scripts/hp-click-tracking" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -F "metadata={\"main_module\":\"tracking-worker.js\",\"compatibility_date\":\"2026-08-25\"};type=application/json" \
  -F "tracking-worker.js=@deploy/tracking-worker.js;type=application/javascript+module"

# 2. Set the Notion API key as a Worker secret (never in the script itself)
curl -X PUT "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/workers/scripts/hp-click-tracking/secrets" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "Content-Type: application/json" \
  -d "{\"name\":\"NOTION_API_KEY\",\"text\":\"$NOTION_API_KEY\",\"type\":\"secret_text\"}"

# 3. Bind track.example.com to it (Workers Custom Domain — auto-provisions DNS + SSL)
curl -X PUT "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/workers/domains" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "Content-Type: application/json" \
  -d "{\"hostname\":\"track.example.com\",\"service\":\"hp-click-tracking\",\"zone_id\":\"$CLOUDFLARE_ZONE_ID\",\"environment\":\"production\"}"
```

Verify: `GET https://track.example.com/c?p=<a-real-test-lead-page-id>`
should update that lead's Click Count / Last Clicked Date / Ghost Site
Status in Notion and redirect to `welcome.example.com`.

## Files here

- `sitescout-dashboard.service` — systemd unit for the always-running Flask dashboard (`Restart=always`, the cloud equivalent of launchd's `KeepAlive=true`).
- `tracking-worker.js` — Cloudflare Worker source for click tracking, see above.
