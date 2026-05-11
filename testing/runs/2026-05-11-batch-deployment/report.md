# Test Run: Batch Pipeline Deployment
Date: 2026-05-11 | Tester: Claude claude-sonnet-4-6 | Scenario: batch-deployment

## Outcome: FAILURE

Phase 1 surfaced a Blocking schema finding. Phase 2 surfaced a Blocking runtime finding.
Phases 3 and 4 could not be reached — expected, as this scenario tests unimplemented code.

## Success Criteria

- [ ] Phase 1: `pvc validate github_repos` accepts a `deploy: { schedule: "0 8 * * *" }` block
  - PARTIAL — validate did not error, but it also did not validate the block (silently ignored)
  - → F-030: `deploy:` block not modeled in Pipeline schema; Pydantic drops it without validation
- [ ] Phase 1: `pvc validate` rejects an invalid cron expression with a clear error message
  - FAIL — `schedule: "not a cron"` passed validate without any error
  - → F-030 (same root cause)
- [x] Phase 1: `pvc validate` on a pipeline without `deploy:` is unaffected (no regression)
  - PASS — `pvc validate craigslist_apts` returned OK with no issues
- [ ] Phase 2: `pvc deploy github_repos` completes without error
  - FAIL — command does not exist: "No such command 'deploy'"
  - → F-031
- [ ] Phase 2: Cloud Composer DAG named `github_repos` is visible after deploy
  - NOT REACHED
- [ ] Phase 2: Cloud Run job for the pipeline exists after deploy
  - NOT REACHED
- [ ] Phase 2: `project.yml` records `deployments.github_repos` with schedule, dag_id, cloud_run_job
  - NOT REACHED
- [ ] Phase 2: `pvc deploy` on a pipeline with no `deploy:` block exits with a clear error
  - NOT REACHED
- [ ] Phase 2: `pvc deploy` without `catalog: gcp` in `project.yml` exits with a clear error
  - NOT REACHED
- [ ] Phase 3: DAG run completes successfully (no Airflow task failures)
  - NOT REACHED
- [ ] Phase 3: Parquet files appear in `gs://<warehouse-bucket>/github_repos/github_repos/data/`
  - NOT REACHED
- [ ] Phase 3: Warehouse query returns rows (data is correct and readable)
  - NOT REACHED
- [ ] Phase 4: Second `pvc deploy` produces exactly one DAG (idempotent)
  - NOT REACHED
- [ ] Phase 4: `pvc undeploy github_repos` removes the DAG and Cloud Run job
  - NOT REACHED (command also does not exist — tested: "No such command 'undeploy'")
- [ ] Phase 4: GCS data files are untouched after `pvc undeploy`
  - NOT REACHED

## What Worked

- No regression on existing pipelines: `pvc validate craigslist_apts` passed cleanly
- `github_repos.yml` with `deploy:` block was accepted by validate (does not crash pvc)

## What Failed

- `pvc validate` does not recognize or validate the `deploy:` block — silently drops it via Pydantic's extra-field behavior. An invalid cron expression (`"not a cron"`) passes without any error.
  [→ F-030: Blocking / Schema]

- `pvc deploy <name>` and `pvc undeploy <name>` are not implemented — both exit with "No such command".
  [→ F-031: Blocking / Runtime]

## Friction Points

None beyond the expected pre-identified blockers.

## Pipeline Produced

```yaml
version: 1
name: github_repos
description: GitHub public repositories for the apache organization

source:
  type: http
  url: https://api.github.com/orgs/apache/repos
  method: GET
  params:
    - name: per_page
      type: integer
      value: 100
    - name: type
      type: string
      value: public

schema:
  columns:
    - name: id
      path: id
      type: integer
    - name: name
      path: name
      type: string
    - name: full_name
      path: full_name
      type: string
    - name: description
      path: description
      type: string
    - name: html_url
      path: html_url
      type: string
    - name: language
      path: language
      type: string
    - name: stargazers_count
      path: stargazers_count
      type: integer
    - name: forks_count
      path: forks_count
      type: integer
    - name: created_at
      path: created_at
      type: timestamp
    - name: updated_at
      path: updated_at
      type: timestamp
    - name: owner_login
      path: owner.login
      type: string

build:
  strategy: incremental
  primary_key: id

deploy:
  schedule: "0 8 * * *"
```

## Proposed Fixes

1. **F-030**: Add `Deploy` model to `pvc/config/models.py` with a required `schedule: str` field and an optional `paused: bool` field. Add `deploy: Optional[Deploy] = None` to the `Pipeline` model. In `pvc validate`, after loading the pipeline, if `pipeline.deploy` is set, validate that `schedule` is a valid cron expression (5 fields, valid ranges) — use `croniter` or a simple regex. Emit a clear error naming the invalid field.

2. **F-031**: Implement `pvc deploy <name>` and `pvc undeploy <name>` as new Typer commands (or a `deploy_app` sub-app following the `gcp_app` pattern) in `pvc/cli.py`. Deploy reads the pipeline's `deploy:` block, errors if missing or if `catalog != gcp`, then invokes the new Terraform module at `pvc/infra/modules/gcp/batch_pipeline/`. Undeploy tears down the Cloud Run job and removes the DAG from Composer without touching warehouse data.
