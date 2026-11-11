# On/off volume collection

Status: **running**

Requests: **562/2,274 (24.7%)**

Completed batches: **220**

Next batch size: **9**; retry wait: **30.0 seconds**.

Updated (UTC): 2026-11-11T04:04:28.279930+00:00

[Workflow run](https://github.com/Mioraka/pbp_impact_metrics/actions/runs/42956128578)

Each successful batch increases concurrency by one, up to 9. Wait increases by 5 seconds after failure and decreases by 5 seconds after success, to a floor of 0. The first failed pass holds concurrency; a second failed pass lowers it by one. Successful responses are retained.

The workflow checkpoints every 10 completed batches (and on errors or time limits). Live logs show every batch and request failure. Scheduled runs resume from the checkpoint.
