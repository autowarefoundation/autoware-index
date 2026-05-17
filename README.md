# autoware-index — `data` branch

This is an **orphan branch** holding append-only validation history written by the sweep workflows on `main`. It shares no history with `main`.

> **Do not open PRs against this branch.** Sweep workflows push directly here, serialized via a concurrency group named `data-branch-write`. Manual edits will likely conflict with the next sweep.

## Layout

```
history/
  <ros-distro>/
    <package-name>.ndjson
```

Each `.ndjson` file is **NDJSON** (one JSON object per line), append-only. Each line records one sweep result: the registered ref tested, the resolved commit sha, the Autoware Core version, pass/fail status, timestamp, and a link to the Actions run that produced it.

## Record schema

Records conform to `schema/history-record.schema.json` on `main`:
<https://github.com/autowarefoundation/autoware-index/blob/main/schema/history-record.schema.json>

Example line:

```json
{"sweep_kind":"release","ref_at_test":{"kind":"tag","value":"0.2.1"},"resolved_sha":"abc1234567890123456789012345678901234567","autoware_version":"1.8.0","status":"pass","at":"2026-05-17T14:23:00Z","actions_run_url":"https://github.com/autowarefoundation/autoware-index/actions/runs/0"}
```

## Sweep modes

Three sweep workflows on `main` write here:

- **release** — fires when `autoware_versions` in any `distributions/<distro>.yaml` gains an entry. Validates every package's current ref against the new Autoware version.
- **eager** — fires when a package's `ref` field changes via PR merge. Validates that one package across the full `autoware_versions` list.
- **nightly** — daily UTC cron. Re-resolves every `kind: branch` entry and tests against the full `autoware_versions` list.

See the [`main` branch README](https://github.com/autowarefoundation/autoware-index/blob/main/README.md#validation-history) for the broader registry design.
