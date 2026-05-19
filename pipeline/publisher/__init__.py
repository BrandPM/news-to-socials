"""Publishers: route a Post to the right external API.

* ``directus`` — writes posts (blog channel) into the Directus posts collection
* ``telegram_bot`` — Telegram channels via Bot API
* ``meta_graph`` — Facebook pages and Instagram via Graph API
* ``dispatcher`` — picks the right publisher by Post.channel
"""

from .dispatcher import Dispatcher

__all__ = ["Dispatcher"]
