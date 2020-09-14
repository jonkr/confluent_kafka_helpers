unit-test:
	./scripts/unit-test.sh

unit-test-ci:
	./scripts/unit-test.sh ci

setup:
	./scripts/setup.sh

lint:
	./scripts/lint.sh

publish:
	./scripts/publish.sh

pip-update:
	./scripts/pip-update.sh

test: unit-test lint

.PHONY: test
