# Recover an empty, initialized PingFederate admin

**Do not submit the generated draft to bulk/import.** Ping's bulk-import tool
documents that existing configuration is cleared before import. Excluding
protected objects from the payload does not establish preservation. The draft
is for reference while constructing individual resource API requests; the
reference data must never be submitted as a bulk import.

The runtime license Secret is not an installed PingFederate license. The bootstrap
hook now stages it into `instance/server/default/conf/pingfederate.lic` before
product setup and leaves a console-uploaded retained license untouched. The
incorrect `LICENSE_DIR` override has been removed. Export updated GitOps manifests,
review and publish them, then sync the candidate when ready. Existing instances
must not automatically bulk-import on restart: doing so overwrites managed settings.

Blue's manual wizard completion does not install the baseline identity objects.
Prepare a recovery payload locally with:

```powershell
py -3 scripts/federate_recovery.py --target green
```

The payload is under `.secrets/recovery/`, contains passwords and signing key
material, and must never be committed or copied into logs. This command sends no
API requests. It excludes administrative accounts, license acceptance, licenses,
SSL key pairs/settings, system keys and all configStore entries. It includes
baseline identity configuration and a shared bootstrap signing key. Review these
changes before import; use only an empty inactive candidate, never a populated
admin or a serving colour. The original upstream export is version 12 and must
be checked against the licensed version 13 import response, including all per-item
errors, before considering recovery successful.

Before recovery, export the candidate's configuration archive and preserve its
master key, license and admin credentials. Verify its API certificate and
confirm the license and administrator account work. Add missing objects through
individual resource APIs after checking that each object is absent. Never print
secret-bearing requests or responses. Use `scripts/federate_repair.py` for individual-resource recovery. It checks a
configuration ZIP, authenticates over a direct IPv4 port-forward using the live
certificate retrieved through authenticated kubectl, saves a private API export,
and checks each method against the running server OpenAPI schema. It skips
existing collection IDs and stops on API errors. Singleton baseline settings
are updated intentionally. No request targets protected security resources.

```powershell
py -3 scripts/federate_repair.py --target blue --backup "C:\Users\nirvi\Downloads\pingfederate-data-10-14-2026.zip"
# Apply only during the agreed maintenance window:
py -3 scripts/federate_repair.py --target blue --backup "C:\Users\nirvi\Downloads\pingfederate-data-10-14-2026.zip" --apply --maintenance
```

Use `--resume` only after a partially completed repair; its private journal
requires the same payload hash and skips confirmed operations. The script does
not replicate configuration to engines. Verify protected files/settings and
LDAP before requesting replication. Do not use it to overwrite a populated
identity configuration. The baseline allowed origins are deduplicated after
substitution, and federation URLs use the public engine hostname.

This is a recovery draft, not an automatic deployment. The existing Terraform
SSL settings reference `sslservercert`, which recovery intentionally does not
replace or create. Before running Terraform, reconcile that reference with the
candidate's actual certificate ID and renew its internal SANs if necessary.
Likewise confirm the expected CA and LDAP store IDs exist before Terraform imports.
Then plan/apply only the inactive candidate, replicate its configuration, verify
LDAP and an actual login, and promote through the normal evidence gate. Kubernetes
readiness alone does not prove that a baseline or valid license is installed.
