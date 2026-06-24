# systemd units

The active deployment (ADR-018 Sanity pivot; NTS_025/073/074) runs two units:

* `nts-admin-api.service` — the FastAPI admin API (uvicorn). Triggers pipeline
  runs as detached subprocesses (NTS_074), serves the operator UI's backend.
* `nts-monitor.timer` / `.service` — the Telegram failure + pipeline-visibility
  alerter (NTS_073/075), every 15 minutes.

The legacy Directus-era units (`news-poll`, `news-dispatch`, `news-stale`,
`news-summary`, `news-bot`) were removed in NTS_076 — dead since the ADR-018
pivot from Directus to Sanity. If a VPS still has them enabled from an old
install, disable + remove them (see "Decommission" below).

All units run as the unprivileged service user with access to
`/var/lib/news-to-socials` and `/var/log/news-to-socials`.

## Install

```bash
sudo cp nts-admin-api.service nts-monitor.service nts-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nts-admin-api.service
sudo systemctl enable --now nts-monitor.timer
```

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
