# systemd units

All units run as the unprivileged `news-deploy` user. The deploy account
needs:

```bash
sudo useradd -r -m -d /opt/news-to-socials -s /bin/bash news-deploy
sudo usermod -aG docker news-deploy
sudo mkdir -p /var/lib/news-to-socials /var/log/news-to-socials
sudo chown -R news-deploy:news-deploy /var/lib/news-to-socials /var/log/news-to-socials
```

## Install

```bash
sudo cp news-*.service news-*.timer /etc/systemd/system/
sudo systemctl daemon-reload

sudo systemctl enable --now news-poll.timer
sudo systemctl enable --now news-dispatch.timer
sudo systemctl enable --now news-bot.service
sudo systemctl enable --now news-stale.timer
sudo systemctl enable --now news-summary.timer
```

## Verify

```bash
systemctl list-timers --all
journalctl -u news-poll.service -n 50 --no-pager
journalctl -u news-bot.service -n 50 --no-pager -f
```

## Update procedure

```bash
cd /opt/news-to-socials && git pull
.venv/bin/pip install -e ".[ml,api]"
sudo systemctl restart news-bot.service
# timers pick up new code on next tick — no restart needed
```
