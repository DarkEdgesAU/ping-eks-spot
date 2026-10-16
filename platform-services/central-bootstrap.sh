#!/usr/bin/env sh
set -eu
# Run after product bits and profile are copied, before license verification.
umask 077
if test -s /run/platform/pingcentral.lic; then
    cp /run/platform/pingcentral.lic "${SERVER_ROOT_DIR}/conf/pingcentral.lic"
fi
# Preserve H2, host keys and all application state on /opt/out.
cat >> "${SERVER_ROOT_DIR}/conf/application.properties" <<EOF
spring.datasource.password=$(cat /run/platform/db-password)
# PingCentral stores its server TLS key pair in its database. Import the prepared
# PKCS12 through Security > Server TLS Key Pair, then restart the application.
pingcentral.proxied=true
server.forward-headers-strategy=framework
server.ssl.trust-any=false
server.ssl.https.verify-hostname=true
EOF
