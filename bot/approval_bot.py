"""Telegram approval bot (ADR-005).

Inline-button approval flow:

* ✅ Approve  → post.status = approved + enqueue for dispatch
* ✏️ Edit     → bot enters edit-mode for the post, user replies with new text
* ❌ Reject   → status=rejected (+ optional reason)
* ⏸️ Postpone → scheduled_at += 24h, stays pending_approval

Auto-publish: a separate cron job (stale_posts.py) flips overage to
approved automatically after 48h — this is the W5 bus-factor mitigation.
"""

from __future__ import annotations

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from pipeline.common.config import get_settings
from pipeline.common.logging import configure_logging, get_logger
from pipeline.common.models import PostStatus
from pipeline.publisher.directus import DirectusClient

log = get_logger(__name__)


def _kb(post_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{post_id}"),
                InlineKeyboardButton("✏️ Edit", callback_data=f"edit:{post_id}"),
            ],
            [
                InlineKeyboardButton("❌ Reject", callback_data=f"reject:{post_id}"),
                InlineKeyboardButton("⏸️ Postpone 24h", callback_data=f"postpone:{post_id}"),
            ],
        ]
    )


async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "News-to-Socials approval bot is online. "
        "I'll DM you when a post is awaiting review."
    )


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not query.data:
        return
    action, _, post_id = query.data.partition(":")
    directus = DirectusClient()

    if action == "approve":
        await directus.update_item(
            "posts", post_id, {"status": PostStatus.approved.value}
        )
        await query.edit_message_caption(
            caption=(query.message.caption or "") + "\n\n✅ <b>Approved</b>",
            parse_mode=ParseMode.HTML,
        )
    elif action == "reject":
        await directus.update_item(
            "posts", post_id, {"status": PostStatus.rejected.value}
        )
        await query.edit_message_caption(
            caption=(query.message.caption or "") + "\n\n❌ <b>Rejected</b>",
            parse_mode=ParseMode.HTML,
        )
    elif action == "postpone":
        # Bump scheduled_at by 24h; status remains pending_approval.
        from datetime import datetime, timedelta, timezone

        new_when = (datetime.now(tz=timezone.utc) + timedelta(hours=24)).isoformat()
        await directus.update_item("posts", post_id, {"scheduled_at": new_when})
        await query.edit_message_caption(
            caption=(query.message.caption or "") + "\n\n⏸️ <b>Postponed +24h</b>",
            parse_mode=ParseMode.HTML,
        )
    elif action == "edit":
        ctx.user_data["editing_post_id"] = post_id  # type: ignore[index]
        await query.message.reply_text(
            f"Reply to this message with the new body for post `{post_id}`.",
            parse_mode=ParseMode.MARKDOWN,
        )


async def on_edit_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    editing = ctx.user_data.get("editing_post_id")  # type: ignore[union-attr]
    if not editing or not update.message or not update.message.text:
        return
    directus = DirectusClient()
    await directus.update_item("posts", editing, {"content": update.message.text})
    ctx.user_data.pop("editing_post_id", None)  # type: ignore[union-attr]
    await update.message.reply_text(f"Updated post {editing}. Press Approve to publish.")


def build_app(token: str) -> Application:
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_edit_reply)
    )
    return application


def main() -> None:
    configure_logging()
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    application = build_app(settings.telegram_bot_token)
    log.info("approval_bot.starting")
    application.run_polling()


if __name__ == "__main__":
    main()


# Expose helper for the pipeline to push a new pending-approval post
# into the chat. The pipeline calls this via plain HTTP (sendPhoto/sendMessage)
# because the bot is in a different process; this stays here as documentation
# and a typed reference for the message shape.
async def _send_for_approval_doc(post_id: str, image_url: str, caption: str) -> None:
    """Reference implementation; the pipeline uses TelegramPublisher in practice."""
    raise NotImplementedError("See pipeline.publisher.telegram_bot.TelegramPublisher")


# Note: the keyboard helper is exported so the pipeline can attach it when
# sending the photo via Bot API. Telegram lets you pass ``reply_markup`` as
# part of sendPhoto.
__all__ = ["_kb", "build_app", "main"]
