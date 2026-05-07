# Releasing

This is the maintainer-only guide for cutting an AEVS SDK release. Most contributors do not need to read this.

## Prerequisites (one-time)

The repository's first release on the `fetchai` PyPI Organisation requires the following web-UI setup. Skip this section after the first publish.

### 1. PyPI

1. Sign in to <https://pypi.org/> with a Fetch.ai-affiliated account that is an Owner of the `fetchai` PyPI Organisation.
2. Open <https://pypi.org/manage/account/publishing/> → **Add a pending publisher** with:
   - **PyPI Project Name**: `aevs`
   - **Owner**: `fetchai`
   - **Repository name**: `AEVS-sdk`
   - **Workflow filename**: `release.yml`
   - **Environment name**: `pypi`
3. After the first successful publish, the project is created on PyPI and the pending publisher promotes to a regular Trusted Publisher automatically.

### 2. GitHub repository

1. **Create the `pypi` environment**: Settings → Environments → New environment → name `pypi` → Save.
2. **Add required reviewers** to the `pypi` environment (Settings → Environments → `pypi` → Required reviewers). Add at least two maintainers. Releases will pause for human approval here.
3. **Restrict the environment to release tags**: Settings → Environments → `pypi` → Deployment branches and tags → Selected tags → `v*`.
4. **Protect release tags**: Settings → Tags → New rule → pattern `v*` → only allow administrators (or named maintainers) to push tags matching this pattern.

No long-lived secrets are stored anywhere. Authentication to PyPI is short-lived OIDC tokens minted per workflow run.

## Cutting a Release

The whole release is driven by pushing one git tag. Order matters.

### 1. Confirm `main` is green

```bash
git checkout main
git pull
make check          # lint + typecheck + tests
```

CI on GitHub must also be green for the same SHA.

### 2. Decide the version bump

The project follows [SemVer](https://semver.org/). While in `0.x`, MINOR bumps are allowed to break compatibility — but document it.

| Change | Bump |
|--------|------|
| Bug fix only, no API change | `patch` (`0.1.0 → 0.1.1`) |
| New feature, backward-compatible | `minor` (`0.1.0 → 0.2.0`) |
| Wire-format / schema change, removed API | `minor` while in 0.x; `major` after 1.0.0 |

### 3. Bump and finalise the changelog

```bash
poetry version patch        # or minor / major / 0.1.2 explicitly
```

Edit `CHANGELOG.md`:

- Move every entry under `## [Unreleased]` into a new dated section, e.g. `## [0.1.1] - 2026-05-15`.
- Recreate an empty `## [Unreleased]` block above the new section so future PRs have somewhere to land.
- Update the compare-link footnote:

  ```text
  [Unreleased]: https://github.com/fetchai/AEVS-sdk/compare/v0.1.1...HEAD
  [0.1.1]: https://github.com/fetchai/AEVS-sdk/compare/v0.1.0...v0.1.1
  ```

### 4. Commit and tag

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "chore(release): X.Y.Z"
git tag -a vX.Y.Z -m "Release X.Y.Z"
git push --follow-tags
```

`--follow-tags` pushes the commit and the tag in one operation, so the `release.yml` trigger fires on a SHA that is already on `main`.

### 5. Approve the publish job

- Open <https://github.com/fetchai/AEVS-sdk/actions> and find the **Release** workflow run for the new tag.
- The `publish-pypi` job will be paused waiting for approval (because of the `pypi` environment's required reviewers).
- A second maintainer clicks **Review deployments → Approve**.
- The job mints an OIDC token, exchanges it with PyPI for a short-lived upload credential, uploads the wheel + sdist, and attaches a Sigstore attestation (PEP 740).
- A GitHub Release is created automatically with notes generated from the merged PRs since the previous tag.

### 6. Verify

```bash
pip install --upgrade aevs==X.Y.Z
python -c "import aevs; print(aevs.__version__)"
# X.Y.Z
```

Sanity-check the listing on <https://pypi.org/project/aevs/>.

## When something goes wrong

### Tag was pushed before the version bump

The `guard` job fails fast with a clear error. **Do not delete and re-push the tag** — that's a recipe for confusion. Instead:

1. Fix `pyproject.toml` to the intended version on `main`.
2. Bump to the *next* patch / minor and tag *that*.

Tags are immutable once pushed; treat them that way.

### Publish job failed mid-way

PyPI uploads are atomic per file but not per release. If the wheel uploaded but the sdist did not, you cannot re-upload the same filenames. Bump to the next patch and re-tag.

### Wrong things published

PyPI does **not** allow re-uploading the same version. Yank the bad release at <https://pypi.org/manage/project/aevs/release/X.Y.Z/> and publish a corrected `X.Y.(Z+1)`.

### Pre-releases

Tag `v0.2.0rc1` (or `a1`, `b1`, `.dev1`). The `release.yml` workflow treats these as pre-releases on GitHub automatically based on the suffix.

## Backports / hotfix line

To patch an old line (say `0.1.x` after `0.2.0` is out):

```bash
git checkout -b release/0.1.x v0.1.0
# cherry-pick fix(es)
poetry version 0.1.1
# update CHANGELOG.md, commit, tag v0.1.1, push --follow-tags
```

Backport branches follow the `release/X.Y.x` pattern.
