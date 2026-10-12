# Validation performed

Passed locally: chart archive checksum, Helm rendering of all three releases, generated YAML parsing and volume/placement checks, profile shell syntax, Terraform syntax/formatting, official signed provider installation and refreshed dependency checksums. Ten workflow tests cover promotion evidence, incorrect cluster/image/target, stale/future evidence, unreconciled writes, seed/DNS relationships, routing persistence and same-name/different-account context rejection.

Terraform schema validation cannot start its provider RPC listener in this environment: `listen unix /tmp/plugin...: socket: operation not permitted`. Run `make terraform-check` on the remote host after initialization. No successful Terraform schema validation or AWS plan is claimed.

The remote host must build the dev container and Ping images, supply AWS credentials and product license files, review the Terraform plan, and exercise the licensed profile/setup commands, bootstrap, AZ locations, TLS and Spot recovery. Prove cross-AZ acknowledged-write durability and connection draining before claiming RPO=0.

No product containers were run, no AWS resources were created, and no live data-loss or performance test was performed while creating this package.
