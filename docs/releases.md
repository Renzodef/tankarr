---
description: How Tankarr is versioned, built and published - what each branch and tag produces, Docker image tags on GHCR, and the release checklist for maintainers.
---

# Releasing Tankarr

This page is for maintainers. Users only need [Upgrading](upgrading.md).

## Versions and channels

Tankarr follows [semantic versioning](https://semver.org). The version lives in
one place, `__version__` in `tankarr/__init__.py`; the Python package reads it
from there, and a release tag must match it exactly (`v0.9.0` for `0.9.0`).

| Event | Workflow | Result |
| --- | --- | --- |
| Pull request | `CI`, `Dependency review`, `Docs` | Lint, tests, browser tests, image build and smoke test. Nothing is published. |
| Push of any branch except `main` and Dependabot's | `Publish image` | `ghcr.io/renzodef/tankarr:branch-<name>` and `:sha-<commit>`, for `amd64` and `arm64` |
| Merge into `main` | `CI`, `Docs` | The documentation site is rebuilt when `docs/` changed |
| Tag `vX.Y.Z` on `main` | `Release` | `:X.Y.Z`, `:X.Y`, `:X` (from 1.0.0 on), `:latest`, `:sha-<commit>`, and a GitHub release |

A tag with a pre-release suffix (`v1.2.0-beta.1`) publishes `:1.2.0-beta.1`
only and marks the GitHub release as a pre-release; it never moves `latest`.

## Where development happens

Tankarr is developed in a private working repository and published here one
version at a time. Each version is a single commit on `main`, "Release vX.Y.Z",
carrying the whole tree of that version and listing what changed since the
previous one, followed by the tag. Between releases `main` also receives
contributors' pull requests; before the next release the maintainer applies
them to the working repository (`git format-patch <last release>..main` here,
`git am` there), so a release never drops a merged contribution.

`main` is protected: no direct pushes, no force pushes, no deletion, and every
change needs a pull request with passing checks. Release tags are immutable.

## Cutting a release

1. **Choose the version.** Patch for fixes only, minor for new features, major
   for breaking changes; before 1.0, a minor release may still break things,
   and the notes say so.
2. **Bring in what was merged here.** Every pull request merged into `main`
   since the last release must already be in the working repository.
3. **Bump the version** in the working repository: `__version__` in
   `tankarr/__init__.py`, `version` in `frontend/package.json` (with
   `npm version --no-git-tag-version X.Y.Z --prefix frontend`, which also
   updates the lockfile) and `appVersion` in `contrib/helm/tankarr/Chart.yaml`
   (a test checks that it matches).
   While there, decide whether the pinned Suwayomi-Server release
   (`PINNED_RELEASE` in `tankarr/suwayomi_runtime.py`) should move to the
   current upstream release: update its tag and the SHA-256 of the server JAR
   from the `Checksums.sha256` file of that release.
4. **Push the release branch.** A branch `release/vX.Y.Z` whose single commit
   "Release vX.Y.Z" holds the tree of that version, with the changes since the
   previous release in its message. Open a pull request against `main` with
   that message as its description. CI runs on it, and the image
   `branch-release-vX.Y.Z` can be tried on a real installation first.
5. **Squash-merge the pull request.** The repository uses the pull request's
   title and description as the commit message, so `main` gains exactly one
   commit, "Release vX.Y.Z".
6. **Tag it.** An annotated tag whose message is the release notes, on the
   merge commit:

   ```sh
   git fetch origin main
   git tag -a vX.Y.Z -F notes.md origin/main
   git push origin vX.Y.Z
   ```

7. **Watch the `Release` workflow.** It refuses a tag that does not match
   `__version__` or does not point at `main`, builds and pushes the images,
   then publishes the GitHub release with the tag message as its notes.

Tags are never moved or deleted. If a release is broken, fix it and publish
the next patch version.

## Hotfixes

For an urgent fix that cannot wait for the next version: fix it in the working
repository, bump the patch version and release it as above. A contributor's fix
merged here is released the same way after it has been brought into the
working repository.

## Trying a change before it is merged

Every branch pushed to this repository (except `main` and Dependabot's) gets
its own image, `ghcr.io/renzodef/tankarr:branch-<name>` with `/` replaced by
`-`, built for both architectures like the others. Run it on a real
installation while the pull request is open, for example with
`TANKARR_IMAGE_TAG=branch-fix-reader-zoom` in the `.env` next to the Compose
file, and go back to `latest` once the change is released. The image is
rebuilt on every push to the branch; images of deleted branches are removed by
a weekly cleanup, and images that only a `sha-` tag names are kept for a month.

## Container registry

Images are published to the GitHub Container Registry as
`ghcr.io/renzodef/tankarr`, built natively on `amd64` and `arm64` runners and
combined into one multi-architecture tag. The first time the package is
published, open its settings on GitHub and make sure it is **public** and
linked to this repository (the image's `org.opencontainers.image.source`
annotation links it automatically).

## Dependency updates

Dependabot opens grouped updates every week (Python, npm, GitHub Actions and
the Docker base images). A workflow merges each of them as soon as the
required checks pass. Two exceptions receive the `needs-review` label and
wait for a maintainer: a major update of a runtime dependency (judged per
dependency, so a group is not held back by a major bump of a development
tool) and a change of the Docker base images.
Merged updates are carried into the working repository before the next
release, like any other merged pull request.

## Pull request labels

Labels decide where a merged pull request appears in generated release notes:
`breaking-change`, `feature` or `enhancement`, `bug` or `fix`,
`documentation`; `skip-changelog` leaves it out. Unlabelled pull requests are
listed under "Other changes".
