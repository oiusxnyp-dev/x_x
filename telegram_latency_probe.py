import time
from collections import OrderedDict

from telethon import events
from telethon.tl import types


class TelegramLatencyProbe:
    def __init__(self, max_events=1000):
        self.events = OrderedDict()
        self.max_events = max_events

    @staticmethod
    def _message_key(message, event_type):
        peer = getattr(message, "peer_id", None)
        message_id = getattr(message, "id", None)

        if peer is None or message_id is None:
            return None

        return (
            type(peer).__name__,
            getattr(peer, "channel_id", None),
            getattr(peer, "chat_id", None),
            getattr(peer, "user_id", None),
            int(message_id),
            event_type,
        )

    async def raw_handler(self, update):
        if not isinstance(
            update,
            (
                types.UpdateNewChannelMessage,
                types.UpdateEditChannelMessage,
                types.UpdateNewMessage,
                types.UpdateEditMessage,
            ),
        ):
            return

        message = getattr(update, "message", None)
        if message is None:
            return

        event_type = (
            'EDIT' if isinstance(update, (
                types.UpdateEditChannelMessage,
                types.UpdateEditMessage,
            )) else 'NEW'
        )
        key = self._message_key(message, event_type)
        if key is None:
            return

        self.events[key] = time.perf_counter_ns()
        self.events.move_to_end(key)

        while len(self.events) > self.max_events:
            self.events.popitem(last=False)

    def measure(self, message, event_type):
        key = self._message_key(message, event_type)
        if key is None:
            return None

        started = self.events.pop(key, None)
        if started is None:
            return None

        return round(
            (time.perf_counter_ns() - started) / 1_000_000,
            3,
        )
