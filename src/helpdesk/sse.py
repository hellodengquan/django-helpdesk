import json
import time
import threading
from collections import defaultdict, deque
import logging

logger = logging.getLogger("helpdesk.sse")

_listeners = defaultdict(list)
_lock = threading.Lock()


class UserSSEStream:
    def __init__(self, user_id, heartbeat_interval=15):
        self.user_id = user_id
        self.queue = deque()
        self.heartbeat_interval = heartbeat_interval
        self._active = True
        self._last_heartbeat = time.time()

    def __iter__(self):
        yield "retry: 5000\n\n"
        self._last_heartbeat = time.time()

        while self._active:
            try:
                with _lock:
                    if self.queue:
                        event = self.queue.popleft()
                    else:
                        event = None
            except Exception:
                event = None

            if event is not None:
                yield self._format_event(event["type"], event["data"])
                self._last_heartbeat = time.time()
            else:
                now = time.time()
                if now - self._last_heartbeat >= self.heartbeat_interval:
                    yield self._format_event("ping", {"ts": int(now * 1000)})
                    self._last_heartbeat = now
                time.sleep(0.5)

    def _format_event(self, event_type, data):
        data_str = json.dumps(data, ensure_ascii=False)
        lines = [f"event: {event_type}"]
        for line in data_str.splitlines():
            lines.append(f"data: {line}")
        lines.append("")
        lines.append("")
        return "\n".join(lines)

    def send(self, event_type, data):
        with _lock:
            self.queue.append({"type": event_type, "data": data})

    def close(self):
        self._active = False


def _get_user_streams(user_id):
    with _lock:
        return list(_listeners.get(user_id, []))


def subscribe(user_id):
    stream = UserSSEStream(user_id)
    with _lock:
        _listeners[user_id].append(stream)
    logger.debug("SSE stream added for user %s (total: %d)", user_id, len(_listeners.get(user_id, [])))
    return stream


def unsubscribe(stream):
    stream.close()
    with _lock:
        user_streams = _listeners.get(stream.user_id, [])
        if stream in user_streams:
            user_streams.remove(stream)
        if not user_streams and stream.user_id in _listeners:
            del _listeners[stream.user_id]
    logger.debug("SSE stream removed for user %s", stream.user_id)


def broadcast_to_user(user_id, event_type, data):
    streams = _get_user_streams(user_id)
    for stream in streams:
        stream.send(event_type, data)
    if streams:
        logger.debug("SSE %s sent to user %s (%d streams)", event_type, user_id, len(streams))


def broadcast_to_followers(ticket, event_type, data=None):
    from helpdesk.models import UserTicketFollow

    if data is None:
        data = {
            "ticket_id": ticket.id,
            "ticket_title": ticket.title,
            "status": ticket.get_status,
            "queue": ticket.queue.title,
            "url": ticket.get_absolute_url(),
            "priority": ticket.priority,
            "modified": ticket.modified.isoformat() if ticket.modified else None,
        }

    follower_ids = list(
        UserTicketFollow.objects.filter(ticket=ticket).values_list("user_id", flat=True)
    )
    for user_id in follower_ids:
        broadcast_to_user(user_id, event_type, data)
