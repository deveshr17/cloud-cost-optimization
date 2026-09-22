# cloud-cost-optimization - developer entrypoints
PYTHON ?= python3
FIXTURES := detectors/fixtures
OUT := out
TF_DIR := terraform/gke-cost-controls

.PHONY: help test report lint tf-validate yaml-check clean

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

test: ## Run unit tests for all detectors
	$(PYTHON) -m unittest discover -s detectors/tests -t . -v

report: ## Regenerate reports/ from fixtures (offline)
	mkdir -p $(OUT)
	$(PYTHON) -m detectors.idle_resources --from-json $(FIXTURES)/idle_resources.json --min-snapshot-age-days 90 --json-out $(OUT)/idle_resources.json --md-out reports/sample-report.md
	$(PYTHON) -m detectors.k8s_rightsizing --from-json $(FIXTURES)/prometheus_usage.json --vpa-json $(FIXTURES)/vpa_recommendations.json --md-out reports/rightsizing-sample.md --patch-out $(OUT)/rightsizing-patch.yaml
	$(PYTHON) -m detectors.bigquery_optimizer --from-json $(FIXTURES)/bigquery_jobs.json --md-out $(OUT)/bigquery-report.md
	$(PYTHON) -m detectors.storage_optimizer --from-json $(FIXTURES)/gcs_buckets.json --pricing detectors/pricing.example.json --md-out $(OUT)/storage-report.md
	$(PYTHON) -m detectors.redis_capacity --from-json $(FIXTURES)/redis_info.json --md-out $(OUT)/redis-report.md
	@echo "Reports written to reports/ and $(OUT)/"

lint: yaml-check ## Syntax-check bash, python and YAML/JSON
	for f in $$(find . -name '*.sh' -not -path './.terraform/*'); do bash -n "$$f" || exit 1; done
	$(PYTHON) -m compileall -q detectors cleanup
	@command -v ruff >/dev/null && ruff check detectors cleanup || echo "ruff not installed; skipping"
	@command -v sqlfluff >/dev/null && sqlfluff lint billing/queries --dialect bigquery || echo "sqlfluff not installed; skipping"

yaml-check: ## Parse every YAML and JSON file
	$(PYTHON) -c 'import glob,json,sys,yaml; \
	[list(yaml.safe_load_all(open(f))) for f in glob.glob("**/*.y*ml", recursive=True)]; \
	[json.load(open(f)) for f in glob.glob("**/*.json", recursive=True) if ".terraform" not in f]; \
	print("yaml/json ok")'

tf-validate: ## terraform fmt + validate for cost controls module
	terraform -chdir=$(TF_DIR) fmt -check -recursive
	terraform -chdir=$(TF_DIR) init -backend=false -input=false >/dev/null
	terraform -chdir=$(TF_DIR) validate

clean: ## Remove generated output
	rm -rf $(OUT) detectors/__pycache__ detectors/tests/__pycache__
