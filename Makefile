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
validate:
	$(PYTHON) scripts/project.py validate
	$(PYTHON) -m unittest discover -s tests -v
terraform-init:
	terraform -chdir=terraform init
terraform-check:
	terraform -chdir=terraform fmt -check -recursive
	terraform -chdir=terraform validate
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
