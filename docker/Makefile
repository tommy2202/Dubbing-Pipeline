.PHONY: test docker-build sbom trivy

test:
	python -m pytest -q

docker-build:
	docker build -f docker/Dockerfile -t dubbing-pipeline:local .

sbom:
	syft dubbing-pipeline:local -o spdx-json > sbom.spdx.json

trivy:
	trivy image --severity HIGH,CRITICAL --ignore-unfixed dubbing-pipeline:local

