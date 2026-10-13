# LDAP blue/green cutover test

This suite uses Apache JMeter 5.6.3, its bundled Groovy engine, and Java's LDAP client.
No JMeter plugins or Python dependencies beyond the project environment are needed.
Use Java 11 or newer and the official JMeter binary distribution. Put `bin` on PATH,
or pass `--jmeter C:/tools/apache-jmeter-5.6.3/bin/jmeter.bat` to each LDAP action.
JMeter's GUI can open generated JMX files, but run load in CLI mode.

Equivalent make targets are `loadtest-init`, `loadtest-seed`, `loadtest-run`,
`loadtest-reconcile`, `loadtest-report`, and `loadtest-cleanup`. Set `TEST_RUN`,
`JMETER`, and `PYTHON` as needed. Markers use the Python command below.

The suite never provisions AWS, changes routing, promotes a colour, deletes a PVC,
or creates promotion attestations. The previously tested AWS lab has been destroyed;
deploy a lab explicitly before live testing. Running this against LDAP creates data.

## Prepare

Copy `loadtest/config.example.json` to `loadtest/config.json` and edit it.
Paths to credentials and truststore are relative to the repository, not the config file.
The default is 10 threads, 10 aggregate workload samples/second, 300 seconds, a
5-second operation timeout, zero permitted errors, and p95 <= 1000 ms in each phase.
These are starting acceptance thresholds, not measured service capacity. The rate
is a target: slow responses can reduce actual throughput. A fresh sample includes
connection/bind overhead; a bind sample measures connection establishment and bind.

Each thread rotates search, bind, and add samples. Half the threads keep an admin
connection for searches/adds; the other half opens a connection for each sample.
User authentication samples always open a fresh connection. The suite does not hide
errors with retries. A subsequent sample may open a replacement connection after failure.
Writes are append-only unique entries, not repeated updates that overwrite evidence.

The requested deployment is a separate Linux load-generator host. Install Java 11+
and JMeter 5.6.3 there, clone this project, and create the Python virtual environment.
Securely provide the password and truststore files referenced by the configuration;
the load generator does not need the CA private key, server private key, or licenses.
Configure a reachable proxy LDAPS hostname covered by its certificate. Pod DNS names
normally resolve only inside Kubernetes; use approved private networking/DNS or direct
pod port forwards for reconciliation. Do not create a public LDAP endpoint for this test.

On the Linux host, from the repository root:

```bash
python3 -m venv .venv
. .venv/bin/activate
export PYTHON=python
export JMETER=/opt/apache-jmeter-5.6.3/bin/jmeter
cp loadtest/config.example.json loadtest/config.json
# Edit proxy_url, secret paths, and replica endpoints before LDAP actions.
make loadtest-init TEST_RUN=build/cutover-01
make loadtest-seed TEST_RUN=build/cutover-01
make loadtest-run TEST_RUN=build/cutover-01
```

In a second terminal, about 150 seconds into the run, record `promotion-start`,
perform the normal gated promotion, and record `promotion-end` using the commands
below with `python` in place of `& $py`. Promotion can run from a different authorized
workstation; record both markers on the Linux generator so the timestamps share its clock.
Do not copy the administrator password into shell arguments. After the load finishes:

```bash
make loadtest-reconcile TEST_RUN=build/cutover-01
make loadtest-report TEST_RUN=build/cutover-01
# Inspect build/cutover-01/summary.html and dashboard/index.html.
```

Cleanup remains an explicit `make loadtest-cleanup TEST_RUN=build/cutover-01` after
retaining the results. The rate is 10 total workload samples/second across all clients,
not 10 per client. Each fresh sample may also perform a connection and administrative bind.

The default proxy URL is the local port forward. This targets one proxy pod and is
appropriate for an initial backend cutover demonstration. To test proxy availability,
configure a stable LDAPS endpoint covering all proxies and run the generator outside
the failure domain you plan to interrupt. TLS trust and hostname verification remain enabled.

In PowerShell, from the repository root:

```powershell
Copy-Item loadtest/config.example.json loadtest/config.json
$py = '.venv/Scripts/python.exe'
$jm = 'C:/tools/apache-jmeter-5.6.3/bin/jmeter.bat'
& $py scripts/cutover_test.py init --run build/cutover-01
& $py scripts/cutover_test.py seed --run build/cutover-01 --jmeter $jm
```

`init` is local only and refuses an existing directory. `seed` creates a unique
`ou=cutover-<id>,dc=example,dc=com`, one user per client, and a group. The admin account
needs access to this OU; the development proxy identity needs `proxied-auth`.
Test-user passwords are generated under the ignored run directory. Never commit or
share raw run directories: they include test credentials and operational metadata.
Keep the generated root and sample users replicating to green before starting load.
Confirm replication on every server; a seed success through blue alone is insufficient.

## Run and promote

Terminal 1:

```powershell
& $py scripts/cutover_test.py run --run build/cutover-01 --jmeter $jm
```

After about two and a half minutes, in terminal 2:

```powershell
& $py scripts/cutover_test.py mark --run build/cutover-01 --event promotion-start
make promote TARGET=green EVIDENCE=build/green-evidence.json PYTHON=.venv/Scripts/python.exe
# Check promotion's exit status and proxy routing; do not mask a failed promotion.
& $py scripts/cutover_test.py mark --run build/cutover-01 --event promotion-end
```

Promotion still requires the existing fresh, truthful operator evidence and actual
candidate image tag in `config.json`. This test does not bypass that gate. The markers
record the promotion command window, not proof that every proxy changed at once or
that connections drained. If promotion fails, preserve that evidence and investigate.
Wait for the complete load run before reconciliation. Do not run cleanup concurrently.
Repeat in a new run directory for rollback testing; independently verify rollback safety.

Generate promotion evidence without editing template placeholders:

```bash
python scripts/promotion_evidence.py --target blue \
  --notes "Record the checks performed and the verification report paths here"
```

This read-only cluster check verifies the Kubernetes context, candidate image tag,
readiness, and three-AZ placement, then writes a draft with verification flags false.
After you have completed all four checks (all-base-DN replication, write reconciliation,
rollback compatibility, and direct-writer accounting), explicitly attest them:

```bash
python scripts/promotion_evidence.py --target blue --overwrite --attest-verified \
  --notes "Describe actual completed checks and report paths"
```

The target, cluster, configured image/base DN, local operator, and current UTC timestamp
are filled automatically. A mismatch between the configured image tag and candidate
pods fails generation; update `config.json` to the verified candidate tag. The default
output is `build/blue-evidence.json` (or green for `--target green`). Promotion still
must occur within five minutes. `--attest-verified` is a human attestation, not an
automated replication or durability test. Generation does not change routing.

## Reconcile all six replicas

After load stops, provide six direct LDAPS endpoints in `loadtest/config.json`:

```json
"replicas": [
  {"name":"blue-0", "url":"ldaps://localhost:17360"},
  {"name":"blue-1", "url":"ldaps://localhost:17361"},
  {"name":"blue-2", "url":"ldaps://localhost:17362"},
  {"name":"green-0", "url":"ldaps://localhost:18360"},
  {"name":"green-1", "url":"ldaps://localhost:18361"},
  {"name":"green-2", "url":"ldaps://localhost:18362"}
]
```

For workstation testing, open six separate terminals. Each runs a direct pod forward,
for example `kubectl -n identity-blue port-forward pod/pd-pingdirectory-0 17360:1636`.
Use the matching namespace, pod index, and local port for every entry above. Ensure
these really address six different replicas; distinct URL strings alone cannot establish
server identity. In-cluster testing can use each pod's full DNS name and port 1636.

```powershell
& $py scripts/cutover_test.py reconcile --run build/cutover-01 --jmeter $jm
& $py scripts/cutover_test.py report --run build/cutover-01
Start-Process build/cutover-01/summary.html
```

Reconciliation checks every acknowledged add and its unique operation marker on all
six endpoints. Timed-out or failed adds remain uncertain and are also checked for
presence. A missing acknowledged write, replica error, zero acknowledged writes,
uncertain outcome, missing promotion markers, empty phase, or exceeded threshold
prevents a passing final report. Requests spanning a marker count in the during phase.
The JMeter dashboard contains throughput, error, and response-time charts. The summary
contains before/during/after counts and p95 plus detailed reconciliation JSON.
The runner checks JTL failures because JMeter can exit successfully despite failed samples.

This is an observation for one configured base DN and a specific run. It does not
validate every replication domain, prove mandatory cross-AZ acknowledgment, establish
RPO=0, or demonstrate failure behavior that was not actually exercised. Retain the
original journals and JTL when investigating outcomes.

## Explicit cleanup

Only after inspecting and retaining the reports:

```powershell
& $py scripts/cutover_test.py cleanup --run build/cutover-01 --jmeter $jm
```

This deletes immediate children and the unique test OU named in that run's manifest.
It does not touch directory replicas, other base entries, PVCs, or AWS infrastructure.
Partial seed failures can also use cleanup if the OU was created. LDAP actions are
single-use per run so accidental reruns cannot overwrite the evidence.

## Local validation

The local smoke test used the official JMeter 5.6.3 binary archive (SHA-512 checked),
Java 11, and `tests/LocalLdapFixture.java` with UnboundID LDAP SDK 7.0.2 and a newly
generated localhost TLS certificate. It exercised seed, a 40-second mixed workload,
reconciliation, HTML/dashboard output, and cleanup. All 436 workload samples succeeded.
After cleanup, a separate negative check correctly reported missing acknowledged writes.
The fixture's seven listeners share one in-memory database: this validates driver and
report plumbing, not real replication, server identity, promotion, or resilience.
Fixture downloads, certificates, credentials, and reports remain under ignored `build/`.
