.PHONY: setup lint format test check

setup: ## Install dev deps and git hooks
	uv sync
	uv run --with pre-commit pre-commit install

lint: ## Run all linters (same as CI)
	SKIP=no-commit-to-branch uvx pre-commit run --all-files

format: ## Auto-format Python
	uv run ruff check --fix .
	uv run ruff format .

test: ## Run tests (exit code 5 = no tests collected yet, treated as success)
	uv run pytest; rc=$$?; [ $$rc -eq 0 ] || [ $$rc -eq 5 ]

check: lint test
