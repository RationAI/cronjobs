# RationAI CronJobs

Kubernetes CronJobs and scripts that keep the RationAI cluster tidy —
reports, cleanups, and other small scheduled jobs. One repository for
all of them, one folder per job.

## ⚠️ This repository is PUBLIC — never commit secrets

Everything here is visible to the world. Before every commit, remember:

- **No tokens, passwords, API keys, or private URLs** belong in this repo.
- Scripts read credentials **only from environment variables** (provided
  by Kubernetes Secrets at runtime).
- Each job folder contains a secrets **template** with `TODO` values.
  The real filled-in copy is named `*.local.yml` — that pattern is
  **gitignored** and must never be pushed.
- If you accidentally commit a secret anyway: rotating the credential is
  the only real fix — deleting the commit does not unpublish it.

## Layout

Each scheduled job lives in its own folder under the tool it uses:

```
<tool>/<job-name>/
├── <job-name>.py             # the actual script (fetched from GitHub at runtime)
├── <job-name>_cronjob.yml    # the Kubernetes CronJob manifest
└── <job-name>_secrets.yml    # Secret template with TODO values (safe to commit)
```

Example — the monthly cluster usage report:

```
grafana/cluster_stats/
├── cluster_stats.py
├── cluster_stats_cronjob.yml
└── cluster_stats_secrets.yml
```

The CronJob pods fetch the script straight from this repository on every
run (via `raw.githubusercontent.com`), so **updating a job is just a
`git push`** — no Docker images, no rebuilds, no redeploys.

## Deploying a job

```bash
# 1. Create the Secret (once per job, from your machine — never commit this file)
cp grafana/cluster_stats/cluster_stats_secrets.yml grafana/cluster_stats/cluster_stats_secrets.local.yml
$EDITOR grafana/cluster_stats/cluster_stats_secrets.local.yml      # fill in the TODOs
kubectl apply -f grafana/cluster_stats/cluster_stats_secrets.local.yml

# 2. Apply the CronJob
kubectl apply -f grafana/cluster_stats/cluster_stats_cronjob.yml

# 3. Test it now instead of waiting for the schedule
kubectl -n rationai-jobs-ns create job --from=cronjob/cluster-stats cluster-stats-test
kubectl -n rationai-jobs-ns logs -f job/cluster-stats-test
```

## Adding a new job

1. Create a folder: `<tool>/<job-name>/`
2. Add the three files (script, cronjob manifest, secrets template).
   Point the runtime fetch at your script's `raw.githubusercontent.com`
   URL and read all credentials from environment variables.
3. Commit, push — done. The next scheduled run picks up the new code.

## House rules

- Kubernetes cron schedules are in **UTC** (`0 8 1 * *` = 08:00 UTC).
- Generated outputs (`*.png`, `*.html`) are gitignored — they can
  contain cluster data and user job names.
- Keep jobs idempotent and small; the cluster is not a toy.
