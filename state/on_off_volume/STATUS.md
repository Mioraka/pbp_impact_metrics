# On/off volume collection

Status: **retry needed**

Requests: **474/2,274 (20.8%)**

Completed batches: **208**

Next batch size: **2**; retry wait: **0.0 seconds**.

Updated (UTC): 2026-11-11T03:56:56.550411+00:00

[Workflow run](https://github.com/Mioraka/pbp_impact_metrics/actions/runs/42955996814)

Last error: Collector exited 1; see request errors in run logs. Next scheduled run resumes.

Each successful batch increases concurrency by one, up to 9. Wait increases by 5 seconds after failure and decreases by 5 seconds after success, to a floor of 0. The first failed pass holds concurrency; a second failed pass lowers it by one. Successful responses are retained.

The workflow checkpoints every 10 completed batches (and on errors or time limits). Live logs show every batch and request failure. Scheduled runs resume from the checkpoint.
