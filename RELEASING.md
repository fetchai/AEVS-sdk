# Releasing

This document is for maintainers.

Releases are automated via GitHub Actions using PyPI Trusted Publishing (OIDC).
Do not publish from a laptop with long-lived PyPI API tokens.

## Versioning

This project follows [Semantic Versioning](https://semver.org/).

The version must be kept consistent in:

- `pyproject.toml` (`[tool.poetry].version`)
- `src/aevs/_version.py` (`__version__`)
- `CHANGELOG.md` (new release section)

CI enforces that `poetry version -s` matches `import aevs; aevs.__version__`.

## Release checklist

Before tagging:

- Ensure `CHANGELOG.md` has a section for the new version and date.
- Ensure the version is bumped in both `pyproject.toml` and `src/aevs/_version.py`.
- Run gates locally:

```bash
make check
make build
poetry run twine check --strict dist/*
```

## Create the release tag

Tags must be of the form `vX.Y.Z` and **must** match `poetry version -s`.

Example for `0.2.0`:

```bash
git checkout main
git pull

poetry version -s   # should print 0.2.0

git tag -a "v0.2.0" -m "v0.2.0"
git push origin "v0.2.0"
```

## What happens after tagging

Pushing the tag triggers `.github/workflows/release.yml`, which:

- Verifies the tag matches the `pyproject.toml` version.
- Installs dependencies and re-runs the same quality gates as CI (ruff, mypy, pytest).
- Builds the sdist and wheel.
- Validates the artifacts with `twine check --strict`.
- Publishes to PyPI using Trusted Publishing (OIDC).
- Creates a GitHub Release and uploads the artifacts.

## Pre-releases

Pre-release versions (e.g. `0.2.0a1`, `0.2.0b1`, `0.2.0rc1`) are supported.

The release workflow marks the GitHub Release as a pre-release when the version
contains one of: `a`, `b`, `rc`, `.dev`.

