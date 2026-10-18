ifeq ($(OS),Windows_NT)
PYTHON ?= py -3
else
PYTHON ?= python3
endif
TARGET ?= blue
EVIDENCE ?=
TEST_RUN ?= build/cutover
JMETER ?= jmeter
.PHONY: setup validate terraform-init terraform-check plan apply kubeconfig secrets build-images render deploy promote status forward
setup:
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -c "from pathlib import Path; import shutil; p = Path('config.json'); shutil.copyfile('config.example.json', p) if not p.exists() else None"
.PHONY: oauth-client-secrets
oauth-client-secrets:
	$(PYTHON) scripts/oauth_client_secrets.py
validate:
	$(PYTHON) scripts/project.py validate
	$(PYTHON) scripts/federate.py validate
	$(PYTHON) scripts/access.py validate
	$(PYTHON) scripts/gitops.py validate
	$(PYTHON) scripts/observability.py render
	$(PYTHON) scripts/platform_services.py render
	$(PYTHON) -m unittest discover -s tests -v

.PHONY: logging-prepare logging-enable logging-secrets logging-render logging-ingress logging-local-test logging-verify
.PHONY: platform-render platform-prepare platform-secrets platform-ingress metrics-enable delegator-enable central-enable
platform-render:
	$(PYTHON) scripts/platform_services.py render
platform-prepare:
	$(PYTHON) scripts/platform_services.py prepare
platform-secrets:
	$(PYTHON) scripts/platform_services.py secrets
platform-ingress:
	$(PYTHON) scripts/platform_services.py ingress
metrics-enable:
	$(PYTHON) scripts/platform_services.py enable --service metrics
delegator-enable:
	$(PYTHON) scripts/platform_services.py enable --service delegator
central-enable:
	$(PYTHON) scripts/platform_services.py enable --service pingcentral
.PHONY: platform-local-test
platform-local-test:
	$(PYTHON) scripts/platform_smoke.py
logging-prepare:
	$(PYTHON) scripts/observability.py prepare
logging-enable:
	$(PYTHON) scripts/observability.py enable
logging-secrets:
	$(PYTHON) scripts/observability.py secrets
logging-render:
	$(PYTHON) scripts/observability.py render
logging-ingress:
	$(PYTHON) scripts/observability.py ingress
logging-local-test:
	$(PYTHON) scripts/logging_smoke.py
logging-verify:
	$(PYTHON) scripts/logging_verify.py
terraform-init:
	terraform -chdir=terraform init
	terraform -chdir=terraform-federate init
terraform-check:
	terraform -chdir=foundation fmt -check -recursive
	terraform -chdir=foundation validate
	terraform -chdir=terraform fmt -check -recursive
	terraform -chdir=terraform validate
	terraform -chdir=terraform-federate fmt -check -recursive
	terraform -chdir=terraform-federate validate

.PHONY: pf-render pf-secrets pf-build-image pf-deploy pf-ingress pf-promote pf-status
pf-controller:
	$(PYTHON) scripts/federate.py install-controller
pf-render:
	$(PYTHON) scripts/federate.py render --target $(TARGET)
pf-secrets:
	$(PYTHON) scripts/federate.py secrets
pf-build-image:
	$(PYTHON) scripts/federate.py build-image --target $(TARGET)
pf-deploy:
	$(PYTHON) scripts/federate.py deploy --target $(TARGET)
pf-ingress:
	$(PYTHON) scripts/federate.py ingress
pf-promote:
	$(PYTHON) scripts/federate.py promote --target $(TARGET) --evidence "$(EVIDENCE)"
pf-status:
	$(PYTHON) scripts/federate.py status

.PHONY: pa-render pa-secrets pa-build-image pa-ingress pa-promote pa-status
pa-render:
	$(PYTHON) scripts/access.py render --target $(TARGET)
pa-secrets:
	$(PYTHON) scripts/access.py secrets
pa-build-image:
	$(PYTHON) scripts/access.py build-image --target $(TARGET)
pa-ingress:
	$(PYTHON) scripts/access.py ingress
pa-promote:
	$(PYTHON) scripts/access.py promote --target $(TARGET) --evidence "$(EVIDENCE)"
pa-status:
	$(PYTHON) scripts/access.py status

.PHONY: pa-identity-plan pa-identity-apply pa-identity-verify
pa-identity-plan:
	$(PYTHON) scripts/access_config.py --target $(TARGET)
pa-identity-apply:
	$(PYTHON) scripts/access_config.py --target $(TARGET) --apply
pa-identity-verify:
	$(PYTHON) scripts/access_config.py --target $(TARGET) --verify

.PHONY: gitops-export argocd-render argocd-secrets argocd-bootstrap argocd-ingress argocd-applications
gitops-export:
	$(PYTHON) scripts/gitops.py export
argocd-render:
	$(PYTHON) scripts/gitops.py render
argocd-secrets:
	$(PYTHON) scripts/gitops.py secrets
argocd-bootstrap:
	$(PYTHON) scripts/gitops.py bootstrap
argocd-ingress:
	$(PYTHON) scripts/gitops.py bootstrap --public
argocd-applications:
	$(PYTHON) scripts/gitops.py applications
plan:
	$(PYTHON) -c "from pathlib import Path; Path('build').mkdir(exist_ok=True)"
	terraform -chdir=terraform plan -out=../build/infrastructure.tfplan
apply:
	terraform -chdir=terraform apply ../build/infrastructure.tfplan
kubeconfig:
	$(PYTHON) scripts/project.py kubeconfig
secrets:
	$(PYTHON) scripts/project.py secrets
build-images:
	$(PYTHON) scripts/project.py build-images
render:
	$(PYTHON) scripts/project.py render --target $(TARGET)
deploy:
	$(PYTHON) scripts/project.py deploy --target $(TARGET)
promote:
	$(PYTHON) scripts/project.py promote --target $(TARGET) --evidence '$(EVIDENCE)'
status:
	$(PYTHON) scripts/project.py status
forward:
	kubectl -n identity-proxy port-forward service/pd-pingdirectoryproxy 1636:636

.PHONY: lifecycle-plan lifecycle-backup lifecycle-shutdown lifecycle-setup
BACKUP_ID ?=
lifecycle-plan:
	$(PYTHON) scripts/lifecycle.py plan --backup-id "$(BACKUP_ID)"
lifecycle-backup:
	$(PYTHON) scripts/lifecycle.py backup --backup-id "$(BACKUP_ID)"
lifecycle-shutdown:
	$(PYTHON) scripts/lifecycle.py shutdown --backup-id "$(BACKUP_ID)"
lifecycle-setup:
	$(PYTHON) scripts/lifecycle.py setup --backup-id "$(BACKUP_ID)"

.PHONY: loadtest-init loadtest-seed loadtest-run loadtest-reconcile loadtest-report loadtest-cleanup
loadtest-init:
	$(PYTHON) scripts/cutover_test.py init --run "$(TEST_RUN)"
loadtest-seed:
	$(PYTHON) scripts/cutover_test.py seed --run "$(TEST_RUN)" --jmeter "$(JMETER)"
loadtest-run:
	$(PYTHON) scripts/cutover_test.py run --run "$(TEST_RUN)" --jmeter "$(JMETER)"
loadtest-reconcile:
	$(PYTHON) scripts/cutover_test.py reconcile --run "$(TEST_RUN)" --jmeter "$(JMETER)"
loadtest-report:
	$(PYTHON) scripts/cutover_test.py report --run "$(TEST_RUN)"
loadtest-cleanup:
	$(PYTHON) scripts/cutover_test.py cleanup --run "$(TEST_RUN)" --jmeter "$(JMETER)"
