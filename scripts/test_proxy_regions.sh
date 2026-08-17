#!/usr/bin/env bash
# test_proxy_regions.sh — Find a DO region where fstream.binance.com delivers real data.
#
# Usage:
#   chmod +x scripts/test_proxy_regions.sh
#   ./scripts/test_proxy_regions.sh [proxy_user] [proxy_pass]
#
# Requires: doctl (DigitalOcean CLI) installed and authenticated.
#   doctl auth init   # if not done yet
#
# What it does:
#   1. Creates a $4/mo Ubuntu droplet in each non-restricted region
#   2. Installs tinyproxy on each
#   3. Tests the fstream.binance.com WS stream — RECEIVES ACTUAL DATA, not just 101
#   4. Reports which regions return live messages
#   5. Destroys ALL created droplets (via trap — runs even if script fails mid-way)
#
# Geo-restrictions confirmed:
#   - EU: Germany, Netherlands, Italy — Binance does not offer derivatives there
#   - US: New Jersey/NYC server confirmed: 101 but zero data (same as DE)
#   - UK: Binance restricted since 2021
#
# Expected working regions: Bangalore (blr1), Singapore (sgp1), Sydney (syd1)
# Canada (tor1) is NOT US-restricted; worth testing.
# DigitalOcean does NOT have a São Paulo or Brazil region (sfo3 = San Francisco, US).
#
# Success criterion: receive at least one WS message frame within 8 seconds.
# 101 handshake alone is NOT sufficient — DE and US IPs both get 101 then zero data.

set -euo pipefail

PROXY_USER="${1:-testuser}"
PROXY_PASS="${2:-testpass123}"

# Regions to test — US, EU, and UK excluded:
# sfo3 (San Francisco/US) ❌  nyc3 (New York/US) ❌  ber1 (Berlin/DE) ❌  lon1 (London/UK) ❌
# Remaining candidates outside restricted jurisdictions:
REGIONS=("blr1" "sgp1" "syd1" "tor1")
REGION_NAMES=("Bangalore" "Singapore" "Sydney" "Toronto")

DROPLET_SIZE="s-1vcpu-512mb-10gb"  # $4/mo
IMAGE="ubuntu-24-04-x64"
SSH_KEY_IDS=$(doctl compute ssh-key list --format ID --no-header | paste -sd "," -)

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

# Track all created droplet IDs so trap can destroy them
CREATED_IDS=()

# Destroy all created droplets on exit (including errors/SIGINT).
# A forgotten droplet bills until someone remembers it.
cleanup() {
    if [[ ${#CREATED_IDS[@]} -gt 0 ]]; then
        echo ""
        echo "Cleaning up ${#CREATED_IDS[@]} droplets…"
        for id in "${CREATED_IDS[@]}"; do
            doctl compute droplet delete "$id" --force 2>/dev/null && echo "  Destroyed $id"
        done
    fi
}
trap cleanup EXIT

# Python snippet used to test each proxy — receives actual data, not just handshake
read -r -d '' TEST_PY << 'PYEOF' || true
import socket, ssl, base64, sys, struct, time

PROXY_HOST = sys.argv[1]
PROXY_PORT = int(sys.argv[2])
PROXY_USER = sys.argv[3]
PROXY_PASS = sys.argv[4]
WS_HOST    = "fstream.binance.com"
WS_PATH    = "/ws/btcusdt@markPrice@1s"
TIMEOUT    = 8   # seconds to wait for at least one data frame

creds = base64.b64encode(f"{PROXY_USER}:{PROXY_PASS}".encode()).decode()

try:
    sock = socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=TIMEOUT)

    # HTTP CONNECT tunnel through proxy to fstream.binance.com:443
    connect_req = (
        f"CONNECT {WS_HOST}:443 HTTP/1.1\r\n"
        f"Host: {WS_HOST}:443\r\n"
        f"Proxy-Authorization: Basic {creds}\r\n\r\n"
    ).encode()
    sock.sendall(connect_req)

    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(1024)
        if not chunk:
            break
        resp += chunk
    status_line = resp.split(b"\r\n")[0].decode()
    if "200" not in status_line:
        print(f"TUNNEL_FAILED: {status_line}")
        sys.exit(2)

    # Upgrade to TLS
    ctx = ssl.create_default_context()
    tls = ctx.wrap_socket(sock, server_hostname=WS_HOST)

    # WebSocket upgrade
    key = base64.b64encode(b"testkey_16bytes!").decode()
    ws_req = (
        f"GET {WS_PATH} HTTP/1.1\r\n"
        f"Host: {WS_HOST}\r\n"
        f"Connection: Upgrade\r\nUpgrade: websocket\r\n"
        f"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n"
    ).encode()
    tls.sendall(ws_req)

    hs = b""
    while b"\r\n\r\n" not in hs:
        hs += tls.recv(1024)
    hs_line = hs.split(b"\r\n")[0].decode()
    if "101" not in hs_line:
        print(f"WS_REJECTED: {hs_line}")
        sys.exit(3)

    # Wait for an actual WebSocket data frame (not just the 101 handshake)
    tls.settimeout(TIMEOUT)
    try:
        frame = tls.recv(512)
        if len(frame) > 2:
            print(f"OK: received {len(frame)} bytes — stream is live!")
            sys.exit(0)
        else:
            print(f"EMPTY_FRAME: {len(frame)} bytes — likely geo-restricted")
            sys.exit(4)
    except socket.timeout:
        print(f"TIMEOUT: 101 but zero data in {TIMEOUT}s — geo-restricted (like DE/US)")
        sys.exit(5)

except Exception as e:
    print(f"ERR: {type(e).__name__}: {e}")
    sys.exit(1)
PYEOF

echo "=== Binance Futures WS Proxy Region Test ==="
echo "Regions: ${REGIONS[*]}"
echo "Excluded: sfo3 (US), nyc3 (US), ber1 (DE), lon1 (UK)"
echo "Success criterion: receive actual WS data frame (101 alone is not enough)"
echo ""
echo "Creating droplets (parallel)…"

DROPLET_IPS=()

# Create all droplets in parallel
for i in "${!REGIONS[@]}"; do
    region="${REGIONS[$i]}"
    name="binance-proxy-test-${region}-$$"
    id=$(doctl compute droplet create "${name}" \
        --size "$DROPLET_SIZE" \
        --image "$IMAGE" \
        --region "$region" \
        --ssh-keys "$SSH_KEY_IDS" \
        --user-data "$SETUP_SCRIPT" \
        --wait \
        --format ID \
        --no-header 2>/dev/null || echo "FAILED")
    if [[ "$id" != "FAILED" && -n "$id" ]]; then
        CREATED_IDS+=("$id")
        ip=$(doctl compute droplet get "$id" --format PublicIPv4 --no-header 2>/dev/null)
        DROPLET_IPS+=("${region}:${id}:${ip}")
        echo "  ${REGION_NAMES[$i]} ($region): ID=$id IP=$ip"
    else
        echo "  ${REGION_NAMES[$i]} ($region): CREATION FAILED"
        DROPLET_IPS+=("${region}:FAILED:")
    fi
done

echo ""
echo "Waiting 90s for tinyproxy to start on all droplets…"
sleep 90

echo ""
echo "=== Testing — waiting up to 8s for actual stream data ==="
PASSED=()

for entry in "${DROPLET_IPS[@]}"; do
    region="${entry%%:*}"
    rest="${entry#*:}"
    id="${rest%%:*}"
    ip="${rest#*:}"

    [[ "$id" == "FAILED" || -z "$ip" ]] && continue

    result=$(python3 -c "$TEST_PY" "$ip" 8888 "$PROXY_USER" "$PROXY_PASS" 2>&1)
    echo "  ${region} (${ip}): ${result}"
    if echo "$result" | grep -q "^OK:"; then
        PASSED+=("${region}:${ip}")
    fi
done

echo ""
echo "=== Summary ==="
if [[ ${#PASSED[@]} -gt 0 ]]; then
    echo "Working regions (stream delivers real data):"
    for r in "${PASSED[@]}"; do
        reg="${r%%:*}"
        ip="${r##*:}"
        echo "  ${reg} → BINANCE_PROXY_URL=http://${PROXY_USER}:${PROXY_PASS}@${ip}:8888"
    done
    echo ""
    echo "Next steps:"
    echo "  1. KEEP the best-latency droplet — delete the others manually with doctl"
    echo "  2. Harden tinyproxy: restrict Allow to 192.81.216.49 only"
    echo "  3. Update BINANCE_PROXY_URL in the Binance container env"
    echo "  4. Set FUTURES_WS_ENABLED=true in container env"
    echo "  5. Restart container and verify: docker logs binance-recorder | grep fstream"
    echo ""
    echo "  The trap will destroy ALL test droplets. To keep one:"
    echo "  1. Copy its ID from the list above"
    echo "  2. Remove it from CREATED_IDS before this script exits, OR press Ctrl+C and"
    echo "     re-run 'doctl compute droplet delete ID' for all EXCEPT the one you want"
else
    echo "No working regions found."
    echo "Try: a commercial proxy with LATAM or Asia exit nodes (Colombia, Argentina, India)"
fi

echo ""
read -r -p "All droplets will be destroyed by the trap on exit. Press Enter to exit." _
# trap fires automatically on EXIT
