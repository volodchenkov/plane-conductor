# Contributing

Thanks for your interest in Plane Conductor.

## Development setup

```bash
git clone https://github.com/volodchenkov/plane-conductor.git
cd plane-conductor
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
pre-commit install
```

## Running checks locally

```bash
ruff check .
ruff format --check .
mypy
pytest
```

`pytest --cov=plane_conductor --cov-report=term-missing` to view coverage.

## Pull requests

- Branch from `main`. Keep PRs focused — one concern per PR.
- Conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
- Update `CHANGELOG.md` under `[Unreleased]` with a one-liner.
- New behavior needs tests. Bug fixes should add a regression test.
- The CI matrix is Python 3.11 / 3.12 / 3.13 — code must pass on all three.

## Reporting bugs / requesting features

Open a GitHub issue using one of the templates. For security issues, please
email the maintainer directly rather than opening a public ticket.

## Code of conduct

This project follows the [Contributor Covenant](./CODE_OF_CONDUCT.md). By
participating you agree to abide by it.
