# Shared setup for the scripts. Sourced, not run.
repository=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ -f "$repository/.env" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "$repository/.env"
    set +a
fi

fail() {
    echo "$1" >&2
    exit 1
}

tizen_cli=$(command -v tizen || echo "$HOME/tizen-studio/tools/ide/bin/tizen")
[[ -x "$tizen_cli" ]] || fail "Put the Tizen CLI on PATH, or install Tizen Studio in ~/tizen-studio."
sdb=$(command -v sdb || echo "$(dirname "$tizen_cli")/../../sdb")

# The Tizen CLI reads signing passwords from the desktop keyring over the session bus,
# which a non-login shell may not have.
if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" && -S "/run/user/$(id -u)/bus" ]]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
fi
