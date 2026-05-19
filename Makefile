PYTHON ?= python3
REPOS_JSON ?= repos.json
PROGRAMS_DIR ?= programs

BUILD_REPOS = $(PYTHON) scripts/build_repos.py --repos-json $(REPOS_JSON) --programs-dir $(PROGRAMS_DIR)
REPO_ARG = $(if $(REPO),--repo "$(REPO)")

.PHONY: build build-all fetch list

build: build-all

build-all:
	$(BUILD_REPOS) build $(REPO_ARG)

fetch:
	$(BUILD_REPOS) fetch $(REPO_ARG)

list:
	$(BUILD_REPOS) list $(REPO_ARG)
