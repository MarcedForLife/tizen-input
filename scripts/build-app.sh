#!/usr/bin/env bash
# Package and sign the Tizen app as dist/TizenInput.wgt.
set -euo pipefail
source "$(dirname "$0")/environment.sh"

profile=${1:-${TIZEN_SECURITY_PROFILE:-}}
[[ -n "$profile" ]] || fail "Set TIZEN_SECURITY_PROFILE in .env, or pass the profile name."

staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
cp "$repository"/tizen-app/* "$staging/"
mkdir -p "$repository/dist"
"$tizen_cli" package -t wgt -s "$profile" -o "$repository/dist" -- "$staging"
# The display's installer splits file names that contain spaces.
mv -f "$repository/dist/Tizen Input.wgt" "$repository/dist/TizenInput.wgt"
