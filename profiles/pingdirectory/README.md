# Directory profile

Adapted from the pinned Ping Identity baseline identified in UPSTREAM.md.
The profile includes PF schemas, users/groups, grants, sessions, consent, indexes,
encrypted exports, metrics and Delegated Admin resource/permission bootstrap.

Passwords, encryption passphrases and TLS stores are provided through
`/run/ping-secrets`. Do not copy upstream `.sec` or bundled keystores into Git.
The upstream auto-increment extension and demonstration echo hooks are not needed.

Delegated Admin starts in two phases:

1. Fresh Directory instances receive `50-delegated-admin.dsconfig`: connected
   identity composition, users/groups, displayed fields and administrator rights.
   The public dadmin OAuth seed is disabled and the HTTP servlet is not attached yet.
2. After PF is running, configure/enable its public authorization-code client with
   PKCE required, install the actual access-token signing public certificate and
   activate the servlet on every Directory replica. `make platform-render`
   generates separate common/signing files and a combined migration file.

This ordering avoids a Directory/PF startup dependency cycle. Existing retained
instances need explicit configuration; rebuilding their image does not replay the
fresh-install profile. Follow `docs/PLATFORM-SERVICES.md` from the repository root.
No Directory replica scaling, PVC replacement or replication reinitialization is
required to add this configuration.

The other 13 upstream OAuth client definitions are preserved. Run
`make oauth-client-secrets` locally before `make secrets` to generate replacement
client secrets/hashes and signing/encryption keys. Nothing private is baked into
the profile: `oauth-clients.json` maps substitutions to runtime Secret files.
The original demo grant types/redirects remain on those clients; only dadmin has
been updated for the new Delegator integration.
