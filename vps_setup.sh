#!/usr/bin/env bash
# One-shot VPS setup for Polymarket CLOB relay.
# Run this on a fresh Ubuntu 24.04 VPS (Hetzner Falkenstein/Helsinki) as root.
#
# Usage:
#   1. scp clob_relay.py root@<VPS_IP>:/root/
#   2. scp vps_setup.sh  root@<VPS_IP>:/root/
#   3. ssh root@<VPS_IP> 'bash /root/vps_setup.sh'
#
# At the end you will get a stable HTTPS URL to paste into Railway as POLYMARKET_HOST.

set -euo pipefail

echo "==> Step 1/6: verifying VPS can reach Polymarket"
code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 https://clob.polymarket.com/markets?limit=1 || echo "000")
if [ "$code" != "200" ]; then
  echo "FAIL: VPS cannot reach clob.polymarket.com (got HTTP $code). Destroy and try another region."
  exit 1
fi
echo "OK (HTTP 200 from Polymarket)"

echo "==> Step 2/6: installing python3 + curl + cloudflared"
apt-get update -y >/dev/null
apt-get install -y python3 curl ca-certificates >/dev/null
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(. /etc/os-release && echo "$VERSION_CODENAME") main" \
  | tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
apt-get update -y >/dev/null
apt-get install -y cloudflared >/dev/null
echo "OK"

echo "==> Step 3/6: installing relay to /opt/clob-relay"
mkdir -p /opt/clob-relay
if [ ! -f /root/clob_relay.py ]; then
  echo "FAIL: /root/clob_relay.py not found. scp it from your Mac first."
  exit 1
fi
cp /root/clob_relay.py /opt/clob-relay/clob_relay.py
chmod +x /opt/clob-relay/clob_relay.py

cat >/etc/systemd/system/clob-relay.service <<'EOF'
[Unit]
Description=Polymarket CLOB relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/clob-relay/clob_relay.py
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now clob-relay
sleep 2
systemctl is-active --quiet clob-relay || { echo "FAIL: relay service not running"; journalctl -u clob-relay --no-pager -n 30; exit 1; }
echo "OK (relay listening on :8765)"

echo "==> Step 4/6: smoke-testing relay locally"
code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 10 http://127.0.0.1:8765/markets?limit=1 || echo "000")
[ "$code" = "200" ] || { echo "FAIL: local relay returned HTTP $code"; exit 1; }
echo "OK (relay -> Polymarket returned 200)"

echo "==> Step 5/6: starting cloudflared quick tunnel as a service"
# Quick tunnel (no Cloudflare account needed). Hostname is *.trycloudflare.com but
# stays the same as long as the systemd service is not restarted.
cat >/etc/systemd/system/cloudflared-tunnel.service <<'EOF'
[Unit]
Description=Cloudflare quick tunnel for CLOB relay
After=clob-relay.service
Requires=clob-relay.service

[Service]
Type=simple
ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8765 --logfile /var/log/cloudflared.log
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now cloudflared-tunnel

echo "    waiting up to 30s for tunnel URL..."
URL=""
for i in $(seq 1 30); do
  sleep 1
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /var/log/cloudflared.log 2>/dev/null | head -n1 || true)
  [ -n "$URL" ] && break
done

if [ -z "$URL" ]; then
  echo "FAIL: could not extract tunnel URL. Check: journalctl -u cloudflared-tunnel"
  exit 1
fi
echo "OK"

echo "==> Step 6/6: end-to-end verification via public tunnel"
sleep 3
code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 15 "${URL}/markets?limit=1" || echo "000")
[ "$code" = "200" ] || { echo "FAIL: public tunnel returned HTTP $code"; exit 1; }
echo "OK (public URL -> relay -> Polymarket returned 200)"

echo ""
echo "============================================================"
echo " DONE. Set this in Railway -> amta-backend-live -> Variables:"
echo ""
echo "   POLYMARKET_HOST=${URL}"
echo ""
echo " Then redeploy backend and restart the live bot."
echo "============================================================"
