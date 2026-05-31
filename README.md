# autoware-index — `data` branch

This is an **orphan branch** holding append-only validation history and cached package metadata written by the sweep workflows on `main`. It shares no history with `main`.

> **Do not open PRs against this branch.** Sweep workflows push directly here, serialized via a concurrency group named `data-branch-write`. Manual edits will likely conflict with the next sweep.

## Layout

```
history/
  <ros-distro>/
    <package-name>.ndjson      # append-only validation records
metadata/
  <ros-distro>/
    <package-name>.xml         # cached upstream package.xml (browse-card description)
```

Each `history/.../<package>.ndjson` file is **NDJSON** (one JSON object per line), append-only. Each line records one sweep result: the registered ref tested, the resolved commit sha, the Autoware Core version, pass/fail status, timestamp, and a link to the Actions run that produced it.

Each `metadata/.../<package>.xml` is the upstream `package.xml` captured at the swept ref; the browse site renders its `<description>` (an optional `description:` in the registry entry overrides it).

## Record schema

Records conform to `schema/history-record.schema.json` on `main`:
<https://github.com/autowarefoundation/autoware-index/blob/main/schema/history-record.schema.json>

Example line:

```json
{"sweep_kind":"nightly","ref_at_test":{"kind":"branch","value":"main"},"resolved_sha":"6d9ff35ad8119fa5d05c26e3ae7c9a39d1a28199","autoware_version":"1.8.0","status":"pass","at":"2026-05-31T11:34:01Z","actions_run_url":"https://github.com/autowarefoundation/autoware-index/actions/runs/26711409656"}
```

## Sweep modes

Two sweep workflows on `main` write here, both serialized via the `data-branch-write` concurrency group:

- **eager** — fires when a package's `ref` field changes via PR merge. Validates that one package against the freshest published Autoware Core release.
- **nightly** — daily UTC cron. Re-resolves every `kind: branch` entry at branch tip and validates it against the freshest published Autoware Core release.

The registry does not track Autoware versions: each sweep resolves the newest release whose container image is already on GHCR at run time. (A former `release` sweep mode was removed — the runtime resolver subsumes it.)

See the [`main` branch README](https://github.com/autowarefoundation/autoware-index/blob/main/README.md#validation-history) for the broader registry design.
