"""Per-channel content adapters.

Each adapter takes a :class:`~pipeline.common.models.Draft` and produces a
channel-ready :class:`~pipeline.common.models.Post`. The dispatcher chooses
the right adapter by the target channel.
"""
