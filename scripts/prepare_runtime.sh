#!/usr/bin/env sh
# Runs only as an init container, before any directory process opens this PVC.
set -eu
umask 077
source_dir=${SECRET_SOURCE:-/run/projected-secrets}
destination=${SECRET_DEST:-/run/ping-secrets}
config=${SERVER_CONFIG:-/opt/out/instance/config/config.ldif}
mkdir -p "$destination"
for source in "$source_dir"/*; do
    test -f "$source" || continue
    cp -L "$source" "$destination/$(basename "$source")"
    chmod 640 "$destination/$(basename "$source")"
done
test -s "$destination/keystore.p12"
test -s "$destination/truststore.p12"
if test -f "$config"; then
    # Unfold LDIF before matching paths, since Ping may wrap long attribute lines.
    temporary="${config}.secret-path-repair.tmp"
    trap 'rm -f "$temporary"' EXIT HUP INT TERM
    awk '
    function emit() {
        if (line ~ /^ds-cfg-(key|trust)-store-(file|pin-file): /) {
            gsub(/\/run\/ping-secrets\/\.\.[0-9][0-9][0-9][0-9]_[^\/[:space:]]+\//, "/run/ping-secrets/", line)
        }
        print line
    }
    /^ / { line = line substr($0, 2); next }
    { if (NR > 1) emit(); line = $0 }
    END { if (NR > 0) emit() }
    ' "$config" > "$temporary"
    # Only replace configuration when an expired projected path was present.
    if grep -Eq '^ds-cfg-(key|trust)-store-(file|pin-file): /run/ping-secrets/\.\.' "$temporary"; then
        echo 'Unsupported projected Secret path remains; refusing startup' >&2
        exit 1
    fi
    if ! cmp -s "$config" "$temporary"; then
        cp -p "$config" "${config}.before-secret-path-repair.$(date +%s)"
        chmod 600 "$temporary"
        mv "$temporary" "$config"
        echo 'Normalized saved TLS paths; original configuration preserved on PVC'
    fi
fi
