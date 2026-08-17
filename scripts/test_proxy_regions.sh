#!/usr/bin/env bash
# test_proxy_regions.sh — Find a DO region where fstream.binance.com works.
#
# Usage:
#   chmod +x scripts/test_proxy_regions.sh
#   ./scripts/test_proxy_regions.sh <proxy_user> <proxy_pass>
#
# Requires: doctl (DigitalOcean CLI) installed and authenticated.
#   doctl auth init   # if not done yet
#
# What it does:
#   1. Creates a $4/mo Ubuntu droplet in each candidate region
#   2. Installs tinyproxy on each
#   3. Tests the fstream.binance.com WS handshake through each proxy
#   4. Reports which regions return 101 (connected) vs 000/403/451
#   5. Destroys all test droplets
#
# Expected non-restricted regions: São Paulo, Singapore, Bangalore, Toronto
# Expected restricted: Frankfurt, Amsterdam, London, New York (US OFAC)

set -euo pipefail

PROXY_USER="${1:-testuser}"
PROXY_PASS="${2:-testpass123}"

# DO regions to test (slug format)
# Skip: fra1 (Frankfurt/Germany), ams3 (Amsterdam/Netherlands), lon1 (London/UK)
REGIONS=("sfo3" "nyc3" "tor1" "blr1" "sgp1" "syd1" "ber1")
REGION_NAMES=("San Francisco" "New York" "Toronto" "Bangalore" "Singapore" "Sydney" "Berlin(skip)")

DROPLET_SIZE="s-1vcpu-512mb-10gb"  # $4/mo
IMAGE="ubuntu-24-04-x64"
SSH_KEY_IDS=$(doctl compute ssh-key list --format ID --no-header | paste -sd "," -)

TINYPROXY_CONF=$(cat <<'TPEOF'
Port 8888
Listen 0.0.0.0
Timeout 600
Allow 0.0.0.0/0
ConnectPort 443
ConnectPort 80
TPEOF
)

SETUP_SCRIPT=$(cat <<SETUPEOF
#!/bin/bash
apt-get update -qq
apt-get install -y -qq tinyproxy
cat > /etc/tinyproxy/tinyproxy.conf << 'CONF'
Port 8888
Listen 0.0.0.0
Timeout 600
Allow 0.0.0.0/0
ConnectPort 443
ConnectPort 80
BasicAuth ${PROXY_USER} ${PROXY_PASS}
CONF
systemctl restart tinyproxy
SETUPEOF
)

echo "=== Binance Futures WS Proxy Region Test ==="
echo "Testing regions: ${REGIONS[*]}"
echo "Creating droplets (this takes ~60s per region)..."
echo ""

DROPLET_IDS=()

# Create all droplets in parallel
for i in "${!REGIONS[@]}"; do
    region="${REGIONS[$i]}"
    name="binance-proxy-test-${region}"
    echo "Creating ${name} in ${region}..."
    id=$(doctl compute droplet create "${name}" \
        --size "$DROPLET_SIZE" \
        --image "$IMAGE" \
        --region "$region" \
        --ssh-keys "$SSH_KEY_IDS" \
        --user-data "$SETUP_SCRIPT" \
        --wait \
        --format ID \
        --no-header 2>/dev/null || echo "FAILED")
    DROPLET_IDS+=("$id")
    echo "  Created ${region}: ID=${id}"
done

echo ""
echo "Waiting 90s for tinyproxy to start..."
sleep 90

# Test each region
echo "=== Handshake Results ==="
PASSED=()

for i in "${!REGIONS[@]}"; do
    region="${REGIONS[$i]}"
    id="${DROPLET_IDS[$i]}"

    if [[ "$id" == "FAILED" || -z "$id" ]]; then
        echo "  ${region}: DROPLET CREATION FAILED"
        continue
    fi

    # Get IP
    ip=$(doctl compute droplet get "$id" --format PublicIPv4 --no-header 2>/dev/null)
    if [[ -z "$ip" ]]; then
        echo "  ${region}: no IP (wait longer?)"
        continue
    fi

    proxy_url="http://${PROXY_USER}:${PROXY_PASS}@${ip}:8888"

    # Test fstream WS handshake via Python (more reliable than curl for WS)
    result=$(python3 -c "
import urllib.request, urllib.error, time

proxy = '${proxy_url}'
url = 'https://fstream.binance.com/ws/btcusdt@markPrice@1s'

handler = urllib.request.ProxyHandler({'https': proxy, 'http': proxy})
opener = urllib.request.build_opener(handler)

req = urllib.request.Request(url, headers={
    'Connection': 'Upgrade',
    'Upgrade': 'websocket',
    'Sec-WebSocket-Version': '13',
    'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ==',
})
try:
    resp = opener.open(req, timeout=10)
    print(f'HTTP {resp.status}')
except urllib.error.HTTPError as e:
    print(f'HTTP {e.code}')
except Exception as ex:
    print(f'ERR {type(ex).__name__}')
" 2>&1)

    echo "  ${region} (${ip}): ${result}"

    if [[ "$result" == *"101"* ]]; then
        PASSED+=("${region}:${ip}")
        echo "    ✓ WORKS — fstream connected!"
    fi
done

echo ""
echo "=== Summary ==="
if [[ ${#PASSED[@]} -gt 0 ]]; then
    echo "Working regions:"
    for r in "${PASSED[@]}"; do
        reg="${r%%:*}"
        ip="${r##*:}"
        echo "  ${reg} → http://${PROXY_USER}:${PROXY_PASS}@${ip}:8888"
    done
    echo ""
    echo "Recommended: pick the lowest-latency option."
    echo "Next steps:"
    echo "  1. Keep one droplet and destroy the rest"
    echo "  2. Harden tinyproxy: restrict Allow to your server IP only"
    echo "  3. Update BINANCE_PROXY_URL in the Binance container env"
    echo "  4. Set FUTURES_WS_ENABLED=true"
    echo "  5. Verify: docker logs binance-recorder | grep 'fstream'"
else
    echo "No working regions found. Try:"
    echo "  - Mumbai (blr1) — India, generally unrestricted"
    echo "  - A commercial proxy with Colombia/LATAM exit nodes"
    echo "  Report findings to evaluate alternatives."
fi

# Cleanup
echo ""
read -r -p "Destroy all test droplets? [y/N] " confirm
if [[ "$confirm" =~ ^[Yy]$ ]]; then
    for i in "${!REGIONS[@]}"; do
        id="${DROPLET_IDS[$i]}"
        if [[ "$id" != "FAILED" && -n "$id" ]]; then
            doctl compute droplet delete "$id" --force
            echo "Destroyed ${REGIONS[$i]} (${id})"
        fi
    done
fi
