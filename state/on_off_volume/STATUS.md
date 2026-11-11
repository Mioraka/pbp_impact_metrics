# On/off volume collection

Status: **running**

Requests: **2,029/2,274 (89.2%)**

Completed batches: **383**

Next batch size: **8**; retry wait: **30 seconds** (maximum 30).

Updated (UTC): 2026-11-11T10:26:15.835951+00:00

[Workflow run](https://github.com/Mioraka/pbp_impact_metrics/actions/runs/42996535773)

Last error: Collector segment timed out; restarting from the latest checkpoint in this run.

Each successful batch increases concurrency by one, up to 9. Wait increases by 5 seconds after failure and decreases by 5 seconds after success, to a floor of 0. The first failed pass holds concurrency; a second failed pass lowers it by one. Successful responses are retained.

The workflow checkpoints every 10 completed batches (and on errors or time limits). Live logs show every batch and request failure. Scheduled runs resume from the checkpoint.
