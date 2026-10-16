Based on Ping Identity baseline/pingdirectory at 804766b0720d0bcdd2e0b0270b0e711d66e6f601.

Source: https://github.com/pingidentity/pingidentity-server-profiles
Demo secrets, private keys, binary applications and the unused auto-increment extension are excluded.
Local topology/TLS hooks override upstream defaults. LDAP demo passwords are runtime substitutions.

50-delegated-admin.dsconfig supplies adapted REST resources, permissions and PF
connected-identity composition. Signing-key trust and servlet activation are a
second phase after PF starts; see docs/PLATFORM-SERVICES.md. JWT signature
verification replaces upstream blind-trust introspection. The same-origin UI
proxy avoids wildcard CORS.

30-OAuthClients.ldif preserves all 14 upstream client definitions, with dadmin
updated for authorization code and initially disabled pending PF API PKCE setup.
Five secret hashes, three JWKS sets and the HS256 client's reversible secret use
runtime substitutions. oauth-clients.json maps IDs to private credential files.
