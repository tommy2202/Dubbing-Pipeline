.PHONY: test docker-build sbom trivy

test:
	python -m pytest -q

docker-build:
	docker build -f docker/Dockerfile -t anime-v2:local .

sbom:
	syft anime-v2:local -o spdx-json > sbom.spdx.json

trivy:
	trivy image --severity HIGH,CRITICAL --ignore-unfixed anime-v2:local

