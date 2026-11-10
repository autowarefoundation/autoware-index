# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/autowarefoundation/autoware-index/security/advisories/new)
("Security" tab → "Report a vulnerability"). We will acknowledge the report and
coordinate a fix and disclosure with you.

## Threat surface specific to this registry

This repository is the source of truth that drives **automated CI clones and
builds of externally-hosted package source**:

- The sweep workflows fetch each registered `repository` at its `ref` two
  times. First, the `resolve` job of `.github/workflows/sweep-repository.yaml`
  fetches and checks out the repository and its index dependencies on the
  hosted runner, outside a container. This job runs no code from these
  sources. It reads `package.xml` and CMake files to select the container
  image, and its token has only `contents: read`. Then the build job clones
  the same commits and builds them inside an Autoware container, selected
  by its version tag. A malicious or compromised registered repository is
  therefore executed by our runners. Registrations are reviewed by maintainers
  before merge for exactly this reason.
- Validation results are recorded verbatim to the orphan `data` branch and
  rendered by the browse site. The reusable workflow that produces them,
  `.github/workflows/sweep-repository.yaml`, lives in this repository and is
  called at the caller's own commit. It calls two actions of
  [`autoware-index-github-actions`](https://github.com/autowarefoundation/autoware-index-github-actions):
  `detect-base-image-stage` selects the container image, and
  `latest-autoware-version` resolves the Autoware version. Both are still
  referenced at the moving `@main`; pinning them to an immutable ref (a
  release tag + SHA) is tracked hardening work.

If you find a way for a registration PR, a sweep, or the data-branch write path
to execute unintended code, exfiltrate secrets, or falsify history records,
please report it as above.

## Supported versions

This is a live registry, not a versioned product. Only the `main` branch is
supported; fixes land there.
