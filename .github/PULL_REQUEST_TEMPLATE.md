<!--
Thanks for contributing to the Autoware Index!
For a package registration, the validate workflow checks schema conformance,
ros_distro/filename consistency, ref resolvability, and real maintainers.
Run `pre-commit run --all-files` locally to catch issues first.
-->

## What this changes

<!-- e.g. "Register autoware_my_package (jazzy, branch main)" or "Bump ref to tag 1.2.0" -->

## For package registrations / ref changes

- [ ] Edited `distributions/<distro>.yaml` (one entry per supported distro).
- [ ] The registered `ref` already exists upstream (`git ls-remote` resolves it).
- [ ] Maintainers are real — no `TBD` / `@example.com` placeholders.
- [ ] `pre-commit run --all-files` passes locally.

## Notes for reviewers

<!-- Anything reviewers should know: governance, why this ref, expected sweep result, etc. -->
