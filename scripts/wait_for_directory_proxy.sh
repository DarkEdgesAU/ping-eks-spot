#!/usr/bin/env sh
set -eu
host=${PROXY_HOST:-pd-pingdirectoryproxy.identity-proxy.svc.cluster.local}
port=${PROXY_PORT:-636}
ca=${PROXY_CA_FILE:-/run/ping-secrets/ca.crt}
attempts=${PROXY_WAIT_ATTEMPTS:-60}
delay=${PROXY_WAIT_INTERVAL_SECONDS:-5}
connect_timeout=${PROXY_CONNECT_TIMEOUT_SECONDS:-10}
command -v openssl >/dev/null
command -v timeout >/dev/null
test -s "$ca"
attempt=1
while test "$attempt" -le "$attempts"; do
    # The Service routes to ready proxy endpoints. Verify its TLS identity;
    # merely opening a TCP socket would also accept an untrusted server.
    if timeout "$connect_timeout" openssl s_client -brief \
        -connect "${host}:${port}" -servername "$host" \
        -verify_hostname "$host" -verify_return_error -CAfile "$ca" \
        </dev/null >/dev/null 2>&1; then
        echo "Directory Proxy LDAPS endpoint is reachable with trusted TLS"
        exit 0
    fi
    echo "Waiting for Directory Proxy LDAPS (${attempt}/${attempts})"
    if test "$attempt" -lt "$attempts"; then sleep "$delay"; fi
    attempt=$((attempt + 1))
done
echo 'Directory Proxy did not become reachable with trusted TLS; blocking PF startup' >&2
exit 1
