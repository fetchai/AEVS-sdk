# Contributing

Thanks for your interest in AEVS SDK! Contributions of all kinds are welcome — bug reports, fixes, features, documentation, and tests.

For security issues, please **do not open a public issue or PR** — see [SECURITY.md](SECURITY.md).

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md). In short:

- Be respectful and assume good faith.
- Critique code, not people.
- Harassment or abusive behavior is not tolerated.

## Questions

Use [GitHub Discussions](https://github.com/fetchai/AEVS-sdk/discussions) for questions and design discussion. Issues are reserved for bug reports and feature requests.

## Bugs

[Open an issue](https://github.com/fetchai/AEVS-sdk/issues) and include:

- The SDK version (`aevs.__version__`) and Python version.
- Steps to reproduce.
- Expected vs actual behaviour.

A pull request with a fix is even more welcome.

## Features

- **Small features**: open a PR directly.
- **Larger features**: open an issue first to discuss the design before writing code, so we can save you a wasted round trip.

## Submitting a pull request

1. Fork the repo and create a branch off `main`.
2. Set up locally:
   ```bash
   make install
   ```
3. Make your change. Add tests for new behaviour and bug fixes.
4. Update documentation (README, docstrings) where it's user-facing.
5. Run the gates:
   ```bash
   make check    # lint + typecheck + tests
   ```
6. Commit (see [commit conventions](#commit-messages)).
7. Open a PR against `main` describing what changed and why.

Smaller, focused PRs are easier to review and merge faster.

## Commit messages

This project follows [Conventional Commits v1.0.0](https://www.conventionalcommits.org/en/v1.0.0/). The PR's squash-merge subject becomes the commit on `main`, so make it a clean conventional commit.

Format:

```
<type>(<scope>): <imperative summary>
```

Allowed types: `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `style`, `build`, `ci`, `chore`, `revert`.

Examples:

```
feat(langchain): add streaming callback support
fix(drainer): respect Retry-After header on 429
docs: clarify session id behaviour in README
```

## Merging

PRs are merged by maintainers using **Squash and merge** so each PR lands as a single conventional commit on `main`. Keep the PR title in conventional-commit format — that's the message that ends up in `git log`.

## Releases

Maintainer-only — see [RELEASING.md](RELEASING.md).
