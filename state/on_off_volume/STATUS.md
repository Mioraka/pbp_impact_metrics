# On/off volume collection

Status: **running**

Requests: **841/2,274 (37.0%)**

Completed batches: **251**

Next batch size: **9**; retry wait: **115.0 seconds**.

Updated (UTC): 2026-11-11T05:37:29.425773+00:00

[Workflow run](https://github.com/Mioraka/pbp_impact_metrics/actions/runs/42961444137)

Last error: Collector segment timed out; restarting from the latest checkpoint in this run.

Each successful batch increases concurrency by one, up to 9. Wait increases by 5 seconds after failure and decreases by 5 seconds after success, to a floor of 0. The first failed pass holds concurrency; a second failed pass lowers it by one. Successful responses are retained.

The workflow checkpoints every 10 completed batches (and on errors or time limits). Live logs show every batch and request failure. Scheduled runs resume from the checkpoint.
