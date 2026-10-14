# systemd units

The active deployment (ADR-018 Sanity pivot; NTS_025/073/074) runs these
units. **Every calendar is in `Europe/Madrid`** (NTS_133 P5 Ф1) — the brand's
`brand_timezone` — so a run stays at the same Madrid hour across DST instead
of drifting an hour twice a year.

| Unit | When (Madrid) | What |
|---|---|---|
| `nts-admin-api.service` | always on | FastAPI admin API (uvicorn); triggers detached runs (NTS_074) |
| `nts-monitor.timer` | every 15 min | Telegram alerts (NTS_073/075). **Quiet hours** in code: 20:00–08:00 and all weekend nothing is sent except the dead-man switch and the spend kill-switch at 100%; held messages go out on the first pass after 08:00 Mon–Fri |
| `nts-portfolio-sweep.timer` | Mon–Fri 10:40 | TTL / production-timeout / retention passes (NTS_100 §6); not behind `production_enabled` |
| `nts-autopublish.timer` | Mon–Fri 10:00 | publishes clean drafts on their `publication_slots` day; obeys `autopublish_enabled` (OFF) |
| `nts-intake.timer` | Mon–Fri 11:40 | contour-1 intake (NTS_099/103); obeys `intake_enabled` |
| `nts-production.timer` | Tue, Thu 12:00 | contour-2 production, `production_batch_size` (2) per run, `weekly_draft_budget` (4) per week; obeys `production_enabled`. The only unit that spends on generation; sends one summary to Telegram when done |
| `nts-backup.timer` (`infra/backup`) | 03:00 UTC daily | admin.db backup (NTS_088), unchanged |

`Persistent=true` on every timer: a run missed while the VPS was down is caught
up at boot. The daytime timers all start at 10:00 Madrid or later, so a
catch-up after a daytime reboot lands in working hours; after a night reboot it
runs at boot and quiet hours hold its messages until 08:00.

The dead-man switch fires at **13:00** Madrid on weekdays (after the 11:40
intake heartbeat), never at weekends.

**`nts-run.service` / `nts-run.timer` (v2, NTS_004 era) are retired.** They are
not in this directory and nothing in the code calls them: the admin API's
manual runs are transient `systemd-run --scope --unit=nts-run-<id>` units
(`pipeline/admin/jobs.py`), a different name. Remove them from the VPS — see
"Decommission" below.

The legacy Directus-era units (`news-poll`, `news-dispatch`, `news-stale`,
`news-summary`, `news-bot`) were removed in NTS_076 — dead since the ADR-018
pivot from Directus to Sanity. If a VPS still has them enabled from an old
install, disable + remove them (see "Decommission" below).

All units run as the unprivileged service user with access to
`/var/lib/news-to-socials` and `/var/log/news-to-socials`.

## Install

```bash
sudo cp nts-admin-api.service nts-monitor.service nts-monitor.timer \
        nts-intake.service nts-intake.timer \
        nts-production.service nts-production.timer \
        nts-portfolio-sweep.service nts-portfolio-sweep.timer \
        nts-autopublish.service nts-autopublish.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nts-admin-api.service
sudo systemctl enable --now nts-monitor.timer
sudo systemctl enable --now nts-intake.timer
sudo systemctl enable --now nts-production.timer
sudo systemctl enable --now nts-portfolio-sweep.timer
sudo systemctl enable --now nts-autopublish.timer
```

Enabling the two new timers does **not** start producing: `production_enabled`
is off until it is switched on in Settings. Order matters only in that the
timer should exist before the flag is flipped, not after.

## Verify

```bash
systemctl list-timers --all
systemctl status nts-admin-api.service
journalctl -u nts-monitor.service -n 50 --no-pager
```

## Update procedure

```bash
cd /opt/news-to-socials/repo && git pull
.venv/bin/pip install -e ".[ml,api]"
sudo systemctl restart nts-admin-api.service
# nts-monitor.timer picks up new code on its next tick — no restart needed
```

## Decommission (legacy Directus units, one-time)

```bash
for u in news-poll news-dispatch news-stale news-summary; do
  sudo systemctl disable --now "$u.timer" 2>/dev/null || true
done
sudo systemctl disable --now news-bot.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/news-{poll,dispatch,stale,summary,bot}.{service,timer}
sudo systemctl daemon-reload
```

## Decommission (v2 `nts-run`, NTS_133 P5)

```bash
grep -rn "nts-run\.service\|nts-run\.timer" /etc/systemd/system /etc/cron* 2>/dev/null  # expect only the two files
sudo systemctl disable --now nts-run.timer nts-run.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/nts-run.service /etc/systemd/system/nts-run.timer
sudo systemctl daemon-reload && sudo systemctl reset-failed
```
