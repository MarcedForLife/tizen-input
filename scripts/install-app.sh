#!/usr/bin/env bash
# Install dist/TizenInput.wgt on the display and start it.
set -euo pipefail
source "$(dirname "$0")/environment.sh"

host=${1:-${DISPLAY_HOST:-}}
[[ -n "$host" ]] || fail "Set DISPLAY_HOST in .env, or pass the display's address."
[[ -f "$repository/dist/TizenInput.wgt" ]] || fail "Build the app first with scripts/build-app.sh."
device="$host:26101"

# SDB sometimes needs a second connect before the display reports as a device.
for attempt in 1 2; do
    "$sdb" connect "$device" >/dev/null || true
    "$sdb" devices | grep -q "^$device[[:space:]]*device" && break
    [[ $attempt == 2 ]] && fail "SDB can't reach $device. Check developer mode and its host IP."
    sleep 2
done

"$tizen_cli" install -n TizenInput.wgt -s "$device" -- "$repository/dist"
"$tizen_cli" run -p TizenInput.TizenInput -s "$device"
