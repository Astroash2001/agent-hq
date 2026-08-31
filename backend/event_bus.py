import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional
from schemas import Event, EventType

logger = logging.getLogger("AgentHQ.EventBus")


class EventBus:
    """
    Central Asynchronous In-Memory Publish/Subscribe Event Bus.
    Decouples Orchestrator, Agents, and Tools from WebSocket and UI transport layers.
    """

    def __init__(self):
        self._subscribers: Dict[str, List[Callable[[Event], Any]]] = {}
        self._global_subscribers: List[Callable[[Event], Any]] = []
        self._history: List[Event] = []
        self._history_limit = 200

    def subscribe(self, event_type: EventType | str, callback: Callable[[Event], Any]):
        """Subscribe a handler callback to a specific event type."""
        key = event_type.value if isinstance(event_type, EventType) else str(event_type)
        if key not in self._subscribers:
            self._subscribers[key] = []
        if callback not in self._subscribers[key]:
            self._subscribers[key].append(callback)
            logger.debug(f"Subscribed callback {callback.__name__} to {key}")

    def unsubscribe(self, event_type: EventType | str, callback: Callable[[Event], Any]):
        """Remove a subscriber callback."""
        key = event_type.value if isinstance(event_type, EventType) else str(event_type)
        if key in self._subscribers and callback in self._subscribers[key]:
            self._subscribers[key].remove(callback)

    def subscribe_all(self, callback: Callable[[Event], Any]):
        """Subscribe a global handler (e.g. WebSocket broadcaster) to ALL events."""
        if callback not in self._global_subscribers:
            self._global_subscribers.append(callback)
            logger.debug(f"Subscribed global callback {callback.__name__}")

    def unsubscribe_all(self, callback: Callable[[Event], Any]):
        """Remove a global handler."""
        if callback in self._global_subscribers:
            self._global_subscribers.remove(callback)

    async def emit(self, event: Event):
        """
        Emit an event to all matching subscribers asynchronously without blocking.
        Also appends to rolling history.
        """
        # Append to rolling history
        self._history.append(event)
        if len(self._history) > self._history_limit:
            self._history.pop(0)

        event_key = event.type.value if isinstance(event.type, EventType) else str(event.type)
        callbacks = list(self._subscribers.get(event_key, [])) + list(self._global_subscribers)

        for cb in callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    asyncio.create_task(cb(event))
                else:
                    cb(event)
            except Exception as e:
                logger.error(f"Error dispatching event {event_key} to {cb}: {e}", exc_info=True)

    def get_history(self, limit: int = 50) -> List[Event]:
        """Return the most recent events."""
        return self._history[-limit:]

    def clear_history(self):
        """Reset event history."""
        self._history.clear()


# Global Singleton Event Bus instance
event_bus = EventBus()
