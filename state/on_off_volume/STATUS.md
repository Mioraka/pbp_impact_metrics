# On/off volume collection

Status: **running**

Requests: **470/2,274 (20.7%)**

Completed batches: **207**

Next batch size: **4**; retry wait: **5 seconds**.

Updated (UTC): 2026-11-11T03:56:52.470679+00:00

[Workflow run](https://github.com/Mioraka/pbp_impact_metrics/actions/runs/42955996814)

Each successful batch increases concurrency by one, up to 9. Wait increases by 5 seconds after failure and decreases by 5 seconds after success, to a floor of 0. The first failed pass holds concurrency; a second failed pass lowers it by one. Successful responses are retained.

The workflow checkpoints every 10 completed batches (and on errors or time limits). Live logs show every batch and request failure. Scheduled runs resume from the checkpoint.
