# systemd units

The active deployment (ADR-018 Sanity pivot; NTS_025/073/074) runs five units:

* `nts-admin-api.service` — the FastAPI admin API (uvicorn). Triggers pipeline
  runs as detached subprocesses (NTS_074), serves the operator UI's backend.
* `nts-monitor.timer` / `.service` — the Telegram failure + pipeline-visibility
  alerter (NTS_073/075), every 15 minutes.
* `nts-intake.timer` / `.service` — the v3 contour-1 intake (NTS_099/NTS_103),
  once a day at 06:10 UTC. **Reads `pipeline_config.intake_enabled`**: with the
  flag off (the shipped default) the run exits without fetching anything, so
  the unit can be enabled before the shadow week starts and the start itself is
  a Settings edit. It makes no generation call of any kind — one embedding plus
  one cheap guard completion per item.
* `nts-production.timer` / `.service` — the v3 contour-2 production run
  (NTS_100), Wednesdays and Sundays at 05:00 UTC. **Reads
  `pipeline_config.production_enabled`**, off by default like the intake flag:
  with it off the run writes a `cancelled` row and exits 0, so the unit is safe
  to enable before the first supervised run. This is the only unit that spends
  money on generation.
* `nts-portfolio-sweep.timer` / `.service` — the daily TTL / production-timeout
  / retention passes (NTS_100 §6), 04:40 UTC. Deliberately daily while
  production is twice a week, and deliberately **not** behind
  `production_enabled`: expiring a candidate and pruning a 30-day-old reject is
  hygiene, costs nothing, and must keep happening while generation is off.

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
        nts-portfolio-sweep.service nts-portfolio-sweep.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nts-admin-api.service
sudo systemctl enable --now nts-monitor.timer
sudo systemctl enable --now nts-intake.timer
sudo systemctl enable --now nts-production.timer
sudo systemctl enable --now nts-portfolio-sweep.timer
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
