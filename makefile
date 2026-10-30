
.PHONY: pypi help test build release

help:
	@echo "This is a makefile providing various convenient functions:"
	@echo " "
	@echo "help:     Prints this message."
	@echo "test:     Runs automated tests (needs a display or xvfb-run)."
	@echo "build:    Builds the sdist and wheel into dist/."
	@echo "pypi:     Push package to pypi (requires appropriate permissions)."
	@echo "release:  Tag v<version> and push it: make release REMOTE=<name|all>."
	@echo " "

test:
	uv run pytest -v

build: README.rst
	rm -rf dist
	uv build

pypi: test build  ## Push project to pypi
	uv publish

release:
	uv run ox_ui release $(REMOTE)

README.rst: README.org
	pandoc --from=org --to=rst --output=README.rst README.org
