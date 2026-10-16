#!/bin/sh
set -eu
cp -R /usr/share/opensearch/config/. /work/
cp /poc/opensearch.yml /work/opensearch.yml
cp /auth/internal_users.yml /work/opensearch-security/internal_users.yml
cat >> /work/opensearch-security/roles.yml <<'EOF'

log_writer:
  cluster_permissions: [cluster_monitor]
  index_permissions:
    - index_patterns: ['logs-*']
      allowed_actions: [crud, create_index]
EOF
cat >> /work/opensearch-security/roles_mapping.yml <<'EOF'

log_writer:
  backend_roles: [log_writer]
EOF
