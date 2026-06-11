# autoware-index — `data` branch

This is an **orphan branch** holding the sweep workflows' outputs: append-only validation history, the per-repository sweep cursors, and cached package metadata. It shares no history with `main`.

> **Do not open PRs against this branch.** Sweep record jobs push directly here, serialized via a concurrency group named `data-branch-write`. Manual edits will likely conflict with the next sweep.

## Layout

```
history/
  <ros-distro>/
    <package-name>.ndjson      # append-only validation records (never rewritten)
state/
  <ros-distro>/
    <repo-name>.json           # mutable per-repository sweep cursor
metadata/
  <ros-distro>/
    <package-name>.xml         # mutable cache: upstream package.xml (browse-card description)
```

Only `history/` is append-only. `state/` and `metadata/` are **mutable caches**, overwritten in place by the sweeps.

Sweeps run **per repository** (one clone + one build per registered repository, however many packages it hosts) but everything here stays **keyed by package**: a repository row fans out into one history record per registered package, each with an independently derived status.

## `history/` — validation records

Each `history/.../<package>.ndjson` file is **NDJSON** (one JSON object per line), append-only. Each line records one sweep result for that package: the repository and registered ref tested, the resolved commit sha, the Autoware Core version, pass/fail status, timestamp, and a link to the Actions run that produced it.

Two line formats coexist, distinguished by the `"schema"` field:

- **schema 2** (current) — conforms to `schema/history-record.schema.json` on `main`:
  <https://github.com/autowarefoundation/autoware-index/blob/main/schema/history-record.schema.json>.
  Carries `"schema": 2` plus the repository identity (`repository`, `repo_name`), so sibling records of one sweep (same repository, same `resolved_sha`, same `actions_run_url`) are exactly groupable and repository migrations stay recoverable.

  ```json
  {"schema":2,"sweep_kind":"nightly","ref_at_test":{"kind":"branch","value":"main"},"resolved_sha":"43df90e3333cd770bb29ae8892fe6259ab5fe615","autoware_version":"1.8.0","status":"pass","at":"2026-06-11T07:41:59Z","actions_run_url":"https://github.com/autowarefoundation/autoware-index/actions/runs/27331578407","repository":"https://github.com/autowarefoundation/autoware_livox_tag_filter","repo_name":"autoware_livox_tag_filter"}
  ```

- **schema 1** (legacy) — lines written before the repository-keyed cutover carry no `"schema"` field and no repository identity. They are never rewritten; readers treat a line without `"schema"` as schema 1.

Status semantics (schema 2): `pass` means the package's **own** dependency-closure build and its **own** tests succeeded at that repo@sha; `fail` means its closure failed to build or its own tests failed. A sibling package's tests never affect a record. Inconclusive outcomes (package absent from the tree, half-run jobs) are skipped loudly and never recorded.

## `state/` — per-repository sweep cursors

Each `state/.../<repo-name>.json` holds the `(url, ref, registered package set)` that was last **conclusively** recorded, plus `last_run_url` and `at`. It is written by the record job **in the same commit** as the history lines — and only when every registered package of the repository produced a conclusive record — so "state advanced" always implies "records landed".

The sweep discover jobs diff the registry on `main` against these cursors (level-triggering): any repository entry that differs — including a run that was cancelled or lost before recording — is simply re-swept by the next eager or nightly run. Deleting a state file forces a re-sweep of that repository; deleting the whole `state/` tree forces a full re-sweep.

## `metadata/` — cached package.xml

Each `metadata/.../<package>.xml` is the pristine upstream `package.xml` captured from the swept tree (shipped inside the sweep's result artifact); the browse site renders its `<description>` (an optional `description:` in the registry entry overrides it). Files are merged per sweep — a partial sweep never deletes other packages' cached files.

## Sweep modes

Two sweep workflows on `main` write here, their record jobs serialized via the `data-branch-write` concurrency group:

- **eager** — level-triggered on every push to `main` touching `distributions/` (plus manual dispatch). Sweeps every repository entry whose `(url, ref, registered package set)` differs from its `state/` cursor.
- **nightly** — daily UTC cron. Re-resolves every `kind: branch` repository at branch tip, plus the same state-diff as catch-up.

The registry does not track Autoware versions: each sweep resolves the newest release whose container image is already on GHCR at run time. (A former `release` sweep mode was removed — the runtime resolver subsumes it.)

See the [`main` branch README](https://github.com/autowarefoundation/autoware-index/blob/main/README.md#validation-history) for the broader registry design.
