<!--
Thanks for contributing to the Autoware Index!
For a registration, the validate workflow checks schema conformance,
ros_distro/filename consistency, ref resolvability, URL/package-name
uniqueness, and real maintainers.
Run `pre-commit run --all-files` locally to catch issues first.
-->

## What this changes

<!-- e.g. "Register awesome_tools (jazzy, tag 1.2.0, packages autoware_a_filter + zz_planner_b)"
     or "Bump awesome_tools ref to tag 1.3.0" -->

## For registrations / ref changes

- [ ] Edited `distributions/<distro>.yaml` (one repository entry per supported distro).
- [ ] Every `packages:` key equals the package's `package.xml` `<name>` at the registered ref.
- [ ] The registered `ref` already exists upstream (`git ls-remote` resolves it).
- [ ] Maintainers are real — no `TBD` / `@example.com` placeholders.
- [ ] `pre-commit run --all-files` passes locally.

## Notes for reviewers

<!-- Anything reviewers should know: governance, why this ref, expected sweep result, etc. -->
