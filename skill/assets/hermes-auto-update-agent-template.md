---
title: Hermes Auto Update
trigger: schedule
rrule: |-
  DTSTART;TZID=America/New_York:20260101T060000
  RRULE:FREQ=DAILY;BYHOUR=6;BYMINUTE=0
model: "<your-model-id>"
backend: zo
notify_channel: discord/<your-channel>
notify: errors
active: false
---

Keep the local Hermes install at `<hermes-repo-path>` current. Update only when the local bridge service is idle and the update path is routine.

## Context

- Hermes repo: `<hermes-repo-path>`
- Bridge service ID: `<zo-service-id>`
- Bridge listens on `127.0.0.1:<port>`
- The bridge is a separate service that imports Hermes as a library. Updating Hermes code is not enough by itself; the service must be restarted to pick up the new code.
- `hermes update` can become interactive if upstream introduces config migrations or new required settings. If that happens, do not guess. Stop and notify.

## Decision rules

| Condition | Action |
|---|---|
| Hermes is up to date | Exit quietly. No notification. |
| Hermes is behind and the bridge is busy | Defer. Notify. |
| Hermes update requires input or looks ambiguous | Stop. Notify. |
| Hermes update succeeds and the bridge is idle | Restart the bridge service, health-check, then notify success. |
| Restart path or health check fails | Stop. Notify. |

Treat any established client connection to the bridge port as busy. Re-check immediately before restart.

Do not kill processes manually. Use the proper Zo service restart mechanism for the bridge service. If that tool is unavailable, stop and notify rather than improvising.

## Procedure

1. Determine whether Hermes is behind:
```bash
cd <hermes-repo-path> || exit 1
git fetch origin
branch="$(git rev-parse --abbrev-ref HEAD)"
git rev-parse --verify "origin/$branch" >/dev/null 2>&1 || branch="main"
behind="$(git rev-list "HEAD..origin/$branch" --count)"
printf '%s\n' "$behind"
```

2. If `behind` is `0`, stop immediately and return a short no-op result. Do not notify.

3. Check whether the bridge is idle. Use:
```bash
ss -tn state established '( sport = :<port> )'
```
If there are any established connections beyond the header line, treat the service as active.

4. If Hermes is behind but the bridge is busy, do not update. Notify that the run was deferred because the service was active, including the current behind count. Then stop.

5. If Hermes is behind and the bridge is idle, run the update non-interactively and capture the full output:
```bash
cd <hermes-repo-path> || exit 1
timeout 900 bash -lc 'hermes update </dev/null' > /tmp/hermes-update.log 2>&1
status=$?
printf '%s\n' "$status"
```
Then inspect `/tmp/hermes-update.log`.

6. If the update output contains signs of an interactive migration or config prompt, stop and notify without restarting the bridge. Treat any of these as manual intervention required:
- `Would you like to configure them now?`
- `EOFError`
- any other prompt asking for input

7. If the update command failed for any other reason, stop and notify with the exit code and the important tail of `/tmp/hermes-update.log`. Do not restart the bridge.

8. If the update succeeded, re-check that the bridge is still idle using the same `ss` command. If it became active in the meantime, do not restart it. Notify that Hermes code was updated but the bridge restart was deferred because live traffic appeared.

9. If the update succeeded and the bridge is still idle, restart the bridge service using the Zo service restart mechanism.

10. After restart, verify health with:
```bash
curl -sf http://127.0.0.1:<port>/health
```
If health fails, notify immediately with the failure details.

11. On successful update + restart + health check, send a notification summarizing:
- behind count before update
- that `hermes update` completed successfully
- that the bridge service was restarted
- that `/health` passed

## Guardrails

- Prefer simple shell checks over broad exploration.
- Do not modify Hermes config files as part of this run.
- Do not answer interactive prompts automatically.
- Do not restart the bridge while it appears busy.
- Do not use Hermes itself for notifications or orchestration during this run.
- If anything is ambiguous, notify and stop.
