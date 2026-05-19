# ADR-005 — Telegram bot for approval, disable-able per channel

**Status:** Accepted
**Date:** 2026-05-11

## Context

For five brands × four channels we publish ~100-200 posts/month. Reviewing
every post takes ~30s if the UX is good. Two extreme paths exist:

1. **Always-on approval.** Slow, but bus-factor=1 means we miss nothing.
2. **Always-off approval.** Fast, but a single bad LLM run can spam four
   channels of five brands before anyone notices.

We took the middle path: approval is a per-channel toggle, ON by default,
operator decides where to relax it once trust is built.

## Decision

* `channels.approval_required: bool` flag. Default `true`.
* Approval through a Telegram bot — inline keyboard ✅✏️❌⏸️.
* If `approval_required=false`, posts skip the bot and go straight into
  the publish queue.
* W5 mitigation: posts in `pending_approval > 48h` auto-flip to
  `approved` and notify the monitoring channel. Operator can still reject
  after publish (manual cleanup procedure).

## Why Telegram specifically

* Andriy already lives in Telegram all day.
* Native rich previews (photo + caption) match the post format.
* Inline buttons → one tap to approve. No app to install.
* Bot-to-bot logging in a separate monitoring channel.

## Consequences

* **Pro:** approval friction is minimal — DM ping, one tap.
* **Pro:** entire approval state is in Directus (`posts.status`, `posts.approver_telegram_id`),
  not in bot memory — bot restarts are safe.
* **Pro:** edit-in-place via reply works (FSM in `bot/approval_bot.py`).
* **Con:** if Telegram is down, approval halts. Auto-publish-after-48h
  guarantees forward progress.
* **Con:** the bot is the only long-running service in the worker stack;
  if it dies, alerts arrive late. Mitigated by `Restart=always` and a
  separate `check_pipeline_alive` alert.
