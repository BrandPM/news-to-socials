---
doc_id: IT_PROJ_NTS_094_LOG_images_on_demand_20260827
title: News-to-Socials — Disable auto cover generation, generate on manager demand (session log)
type: session_log
family: PROJECT
department: IT
subdivision: news_to_socials
category: project_documentation
status: delivered_flag_off
version: 1.0.0
executor: Claude Code (autonomous)
created: 2026-08-27
language: en
priority: P1
cross_refs:
  - IT_PROJ_NTS_094_P1_images_on_demand_prompt
  - IT_PROJ_NTS_094_images_on_demand
  - IT_PROJ_NTS_090_publish_completeness_guard_20260806
  - IT_PROJ_NTS_091_cover_sweep_and_button_20260806
  - IT_PROJ_NTS_069_shared_cover_image_20260622
---

# Summary

`pipeline_config.images_on_demand` now exists per brand. OFF (the default,
and the state production is in right now) the pipeline generates a cover per
topic exactly as it always has. ON, the run generates none — no scene prompt,
no Flux call, no asset upload — and each draft is written with
`coverImage: null` on purpose, for the manager to fill in on the one draft
they pick.

Tasks A–D are complete, both repos are pushed, and the migration is applied on
`nts-prod`. **The flag is left OFF.** Flipping it in Settings is Andriy's call
and is the moment the cost change starts.

What that change is worth, measured rather than estimated — run #117 (today
10:00 UTC, the last run before this deploy):

| operation | rows | USD |
|---|---|---|
| draft_eval | 45 | 0.7989 |
| **image_master** | **15** | **0.60** |
| translate | 24 | 0.2351 |
| polish | 8 | 0.0617 |
| topic_scoring | 265 | 0.0114 |
| image_prompt | 15 | 0.0011 |

15 covers a day at $0.04, ~$18/month, on a run that drafted 8 topics' worth of
articles — most of which will never be published. That is the line the flag
moves.

One thing the prompt did not ask for and I added anyway, flagged here because
it is a scope call: **the double-click fix is server-side, not only in the
button.** See "Deviations" below.

---

# Changed functions

## Backend — `news-to-socials`

| File | Change |
|---|---|
| `pipeline/admin/migrations/versions/017_images_on_demand.py` | **new.** Adds `pipeline_config.images_on_demand`, `BOOLEAN NOT NULL server_default '0'`. Idempotent both directions (an inspector check makes a re-run a no-op, so a partially applied deploy can be re-run without hand-editing `alembic_version`). |
| `pipeline/admin/models.py` | `PipelineConfig.images_on_demand`, `default=False`, `server_default=text("0")`. |
| `pipeline/admin/schemas.py` | `PipelineConfigOut.images_on_demand: bool`; `PipelineConfigUpdate.images_on_demand: bool \| None`. |
| `pipeline/admin/config_client.py` | `ConfigRecord.images_on_demand: bool = False`; `get_config()` reads it with a `getattr` fallback, like its neighbours. |
| `pipeline/run.py::_process_source` | New `images_on_demand: bool = False` kwarg. The per-topic image block became an `if/else`: ON logs `image.skipped_on_demand` at info, increments `stats["images_skipped"]`, sets `asset_id = None`. OFF runs the pre-existing `try/except` verbatim. |
| `pipeline/run.py::_process_source` | `stats` gains `"images_skipped": 0`. |
| `pipeline/run.py::run_pipeline` | Reads the flag off `ConfigRecord` (fail-safe default `False`), logs `image.on_demand_mode` once per run when ON, passes it to `_process_source`, seeds `aggregate_stats["images_skipped"]`, adds `covers_skipped=N` to the per-source log line. |
| `pipeline/monitoring/alerts.py::format_run_finished` | New `images_skipped: int = 0` param → `🖼 covers skipped: N (on demand)`, rendered only when > 0. |
| `pipeline/monitoring/alerts.py::_gather_run_events` | Feeds it from `stats["images_skipped"]`. |
| `pipeline/admin/jobs.py` | `ImageJob.dedup_key`; new `_IMAGE_JOBS_BY_KEY` index; new `register_image_job_for(key) -> (job, is_new)`; `_set_image_job` drops the key on a terminal state; `reset_image_jobs_for_tests` clears the index. |
| `pipeline/admin/routes/drafts.py::regenerate_image` | Normalises the id (`drafts.` stripped) and coalesces onto `register_image_job_for(f"{id}|{custom_prompt or ''}")`; schedules the background task only when the job is new, logs `image.regenerate_coalesced` otherwise. |
| `scripts/backfill_cover_images.py` | New `_images_on_demand(brand_slug)` (fail-safe `False`) and `_warn_on_demand()`. `_run` gained `brand_slug` / `images_on_demand` / `override_on_demand`; `_run_target` gained `blocked`. New CLI flag `--override-images-on-demand`. |

## Frontend — `news-to-socials-admin`

| File | Change |
|---|---|
| `lib/types.ts` | `PipelineConfig.images_on_demand: boolean`. |
| `app/(admin)/settings/settings-client.tsx` | zod field, default value, and a new **Cover images 🖼️** section with the checkbox + a helper line stating what the *next run* will do in each position. |
| `lib/i18n/hints.ts` | New `publishGuardCoverOnlyTitle` / `publishGuardCoverOnly` (RU). `publishGuard` unchanged — it still owns the real-defect case. |
| `components/drafts/publish-guard.tsx` | When `coverImage` is the only missing component **and** a draft id is present, the banner switches to «Осталось сгенерировать обложку» in the warning tone with the on-demand explanation. Everything else stays red and worded as a defect. |
| `components/drafts/generate-cover-button.tsx` | `useRef` in-flight latch + stays disabled through the `router.refresh()` transition. |

**NTS_069 is intact.** The flag only decides *whether* `generate_image_for_topic`
is called; when it is, it is still called once per topic and the asset is still
shared across all four language siblings. `image_regenerate.py` was not touched.

**`draft_validation.py` was not touched.** Same codes, same 422, same block on
Approve. The banner tells a different story about the same block.

---

# Migration number actually used

**`017_images_on_demand`**, `down_revision = "016_draft_scores"`.

The head was verified rather than assumed — `alembic heads` reported
`016_draft_scores (head)` before writing, and the numeric filename ordering in
`versions/` is not the revision chain in this repo (004 chains after 005).

---

# Tests

Backend, run on this Mac and again on `nts-prod` against the deployed tree:

* baseline **667 passed**
* final **693 passed** (+26), exit 0 both places

New coverage, `tests/unit/test_images_on_demand_nts094.py` (19):

* migration 017 round trip: column present, `NOT NULL`, default `0`; re-running
  `upgrade head` is a no-op; downgrade drops it; re-upgrade restores it
* new config rows default to OFF at the ORM level too
* **flag OFF** — one image call per topic (not per language), every one of the
  8 drafts carries its topic's single shared asset, `images_skipped == 0`, and
  no `image.skipped_on_demand` is emitted
* **flag ON** — 8 drafts still produced, every one with
  `cover_image_asset_id is None`, and zero image calls. Proved by
  booby-trapping all three layers (`generate_image_for_topic`,
  `build_scene_prompt`, `ImageGenerator`) so *entering* any of them fails the
  test rather than by trusting one mock
* the skip logs `image.skipped_on_demand` at info, carries `topic`, and neither
  `image.failed` nor `image.unexpected_failure` appears
* `images_skipped == 2` per two topics (per topic, not per language) in
  `runs.stats`
* the count travels the whole chain — `_process_source` → `runs.stats` JSON →
  `_gather_run_events` → the run-finished pulse containing `covers skipped: 2`
* the summary gains no line when nothing was skipped
* **Task B end to end** — `PUT /api/v1/config` → `pipeline_config` →
  `ConfigRecord` → `run_pipeline` makes no image call; toggled back, generation
  returns
* the whole-form PUT shape the Settings page actually sends is accepted
  (`PipelineConfigUpdate` is `extra="forbid"`, which is the realistic way this
  ships broken), and a partial PUT does not reset its neighbours
* coalescing: identical in-flight requests share one job; a different custom
  prompt does not; a `done` or `error` job stops absorbing clicks; at the route
  level a second POST returns the running job and generates nothing, and the
  `post-x` / `drafts.post-x` id forms land on the same key

`tests/unit/test_backfill_cover_images.py` (+7):

* drafts dry-run warns loudly under the flag and still prints the candidate
  table
* `--apply` on drafts is refused without the override, writes nothing, exits 2
* the override lets a deliberate sweep through, and still prints the warning
* the published pass neither warns nor refuses
* `--target all --apply` repairs the live site and holds back only the drafts
  half
* flag OFF leaves the drafts sweep byte-identical
* an unreadable `admin.db` fails safe to `False`

Frontend: `tsc --noEmit` clean, `eslint` **0 errors / 3 warnings**, `next build`
compiled successfully. The 3 warnings are the pre-existing
`react-hooks/incompatible-library` notes on `react-hook-form`'s `watch()` — the
identical set was captured on a stashed tree before and after, so this change
adds none.

---

# Deploy status

`nts-prod` (161.35.70.83), 2026-08-27 ~12:30 UTC:

1. `admin.db` backed up **before** anything ran →
   `/opt/news-to-socials/backups/admin.db.pre-nts094.20260827T122937Z`
   (7,622,656 bytes), `PRAGMA integrity_check` → `ok`
2. `git pull --ff-only` → `0f1558e`
3. `alembic current` before: `016_draft_scores` → `alembic upgrade head` →
   after: `017_images_on_demand (head)`
4. `sudo systemctl restart nts-admin-api` → `active`
5. `/health` → **HTTP 200**, `{"status":"ok","version":"0.0.1"}`
   (port **8080**, not 8000 — worth remembering)
6. `GET /api/v1/config?brand_id=1` → 200, `images_on_demand = False`
7. full backend suite re-run on the deployed tree → **693 passed**, exit 0

Frontend deploys from `main` via Vercel (per `.env.local`: "Production values
are set in Vercel"). The push is the deploy; I have no Vercel credentials here,
so **that half is unverified from this session** — worth a look at
`/settings` once Vercel reports green.

The `admin.db` path in the prompt was right: `/opt/news-to-socials/repo/admin.db`
is the live 7.6 MB file. `/opt/news-to-socials/admin.db` is a 0-byte leftover
from July — ignore it.

---

# Verification evidence, both flag states

**Flag ON** — covered entirely by the test suite, deliberately not exercised
against production. The proof is stronger than a live run would be: the ON path
runs with every generation layer booby-trapped, so "zero Replicate calls" is
enforced by the harness rather than observed once and hoped for.

**Flag OFF** — three independent proofs, none of which cost a Flux call:

1. `alembic upgrade head` on production left `brand 1 → images_on_demand = 0`.
   Every existing row keeps generating covers.
2. The deployed tree's own test suite passes, including
   `test_flag_off_still_generates_one_cover_per_topic` and
   `test_flag_off_emits_no_skip_event`.
3. Run **#118**, a `--dry-run` pass through the deployed code on production,
   completed clean (exit 0) with:

   ```json
   {"processed": 5, "brand": "icon", "status": "dry_run",
    "stats": {"fetched": 553, "scored": 6, "drafted": 0, "errors": 0,
              "deduped": 0, "images_skipped": 0},
    "event": "pipeline.done"}
   ```

   The log carries five `image.dry_run` events, one per topic, and **zero**
   `image.skipped_on_demand`. That matters more than the counter: `image.dry_run`
   is emitted from *inside* `generate_image_for_topic`, so its presence proves
   the run took the unchanged `else` branch and entered the generation helper —
   the flag did not divert it. Only the paid Flux call itself was short-circuited,
   by `dry_run`, exactly as it was before this change. `runs.stats` for #118 also
   confirms the new key is being written by the deployed code.

I deliberately did **not** trigger a paid production run to watch covers appear.
The hard constraint was "no paid Flux calls beyond what the acceptance run
genuinely needs", and the definitive production evidence — `image_master` rows
still landing with the flag off — arrives for free from the **10:00 UTC cron
run on 2026-08-28** (`nts-run.timer`). The comparison to make is against run
#117 above: same shape, ~15 `image_master` rows, `images_skipped: 0`.

---

# Deviations from the prompt

**1. The double-click fix went server-side as well as into the button.**
Task C said to check the disabled state holds and fix it if it did not. It
mostly held, and I closed two real gaps in it (a ref that latches in the same
tick, and staying disabled through the `router.refresh()` window where
`isPending` had already dropped while the banner was still on screen). But the
button cannot cover a second browser tab or an impatient retry, and
`POST /drafts/{id}/regenerate-image` had **no** server-side guard at all — every
POST registered a fresh job and paid Replicate again. Since the stated goal is
one charge per article, I put the guarantee next to the spend: identical
in-flight requests coalesce onto one job. The dedup key spans the document *and*
the custom prompt, so re-prompting a Regenerate is still its own request, and
only pending jobs absorb clicks, so a failed attempt stays retryable.

**2. The banner copy is conditional on the missing-set, not on the flag.**
The prompt framed the copy change around the flag being on. The banner has no
access to `pipeline_config` — plumbing the flag into every draft read would be
a much larger change for no gain, because the condition that actually matters
is the same one either way: *is the cover the only thing missing, and can it be
fixed here?* When yes it is a step, whatever the flag says; when the body or
slug is also missing, it is a generation defect and stays red. This also means
a pre-existing cover-less draft reads correctly today, before the flag is
flipped.

**3. `MissingComponentsBadge` was left alone.** The compact list-row badge still
shows `⚠️ обложка` in danger for a cover-only draft. It is out of the named
scope (Task C says the banner) and it has no draft context to know whether the
fix is one click away. If the on-demand flow becomes the norm, that badge is the
obvious next thing to soften — noted rather than done.

**4. Session-log location.** The prompt's `03_session_logs/` does not exist on
this machine (the only vault directory here is `04_session_logs` under a
different project's backup). I created `03_session_logs/` at the root of the
`news-to-socials` repo so the log ships with the work. Move it if the vault is
the intended home.

---

# Commit SHAs

`news-to-socials` — pushed to `main` (`2ec17e1..0f1558e`):

| SHA | Subject |
|---|---|
| `ecab829` | feat(pipeline): NTS_094 Task A — images_on_demand skips cover generation |
| `fbcea3c` | feat(settings): NTS_094 Task B — images_on_demand toggle reaches the runtime |
| `a4a5f7e` | feat(drafts): NTS_094 Task C — one click, one Flux charge |
| `0f1558e` | feat(scripts): NTS_094 Task D — backfill can't undo the cost change by accident |

`news-to-socials-admin` — pushed to `main` (`d4aceea..8339bf6`):

| SHA | Subject |
|---|---|
| `65516dc` | feat(settings): NTS_094 Task B — «Cover images» toggle |
| `8339bf6` | feat(drafts): NTS_094 Task C — a cover-less draft reads as a step, not a loss |

---

# Handover — what Andriy does next

1. Confirm Vercel has deployed `8339bf6` and `/settings` shows **Cover images 🖼️**.
2. Optionally wait for the 10:00 UTC run on 28 Aug and confirm it still shows
   ~15 `image_master` rows and `images_skipped: 0` — the free flag-off proof.
3. Flip **Generate covers on demand** on in Settings. That is the moment the
   spend changes.
4. The first run after the flip should report `🖼 covers skipped: N (on demand)`
   in the Telegram summary and write **no** `image_master` rows.
5. New drafts will show the yellow «Осталось сгенерировать обложку» banner and
   Approve will stay blocked until the cover is generated. **That is the
   intended flow.** One click patches all four language siblings with one
   shared asset and unblocks Approve in place.
6. If a batch of drafts is stranded cover-less from *before* the flip,
   `scripts/backfill_cover_images.py --target drafts` is still the right tool —
   it will warn, and needs `--override-images-on-demand` to apply. Read the
   candidate table first; every `generate` row is $0.04.
