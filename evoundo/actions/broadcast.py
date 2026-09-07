"""Compensatable Broadcast Channel (Gap 17 Formalization).

Encapsulates external broadcast channels (e.g. public status pages, Slack announcements,
email blasts). External communications are inherently irreversible in the real world
(ActionClass.IRREVERSIBLE); direct reversion is fenced and raises
IrreversibleActionBlockedError. Reversal requests require publishing forward compensating
notices (e.g. RESOLVED, CORRECTION) that causally back-reference original messages.
"""

from __future__ import annotations
from dataclasses import dataclass, field
import logging
import time
from typing import Any, Dict, List, Optional

from evoundo.actions.classifier import ActionClass, IrreversibleActionBlockedError

logger = logging.getLogger("evoundo.actions.broadcast")


@dataclass(frozen=True)
class BroadcastMessage:
    broadcast_id: str
    channel: str
    message: str
    sender: str = "system"
    timestamp: float = field(default_factory=time.time)
    is_compensating: bool = False
    replaces_broadcast_id: Optional[str] = None
    action_class: ActionClass = ActionClass.IRREVERSIBLE
    metadata: Dict[str, Any] = field(default_factory=dict)


class CompensatableBroadcastChannel:
    """Manages irreversible broadcast channels with forward compensating updates."""

    def __init__(self) -> None:
        self._messages: Dict[str, BroadcastMessage] = {}
        self._channel_messages: Dict[str, List[str]] = {}

    def publish(
        self,
        channel: str,
        broadcast_id: str,
        message: str,
        sender: str = "system",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> BroadcastMessage:
        """Publish a message to a broadcast channel. Marked ActionClass.IRREVERSIBLE."""
        msg = BroadcastMessage(
            broadcast_id=broadcast_id,
            channel=channel,
            message=message,
            sender=sender,
            is_compensating=False,
            replaces_broadcast_id=None,
            action_class=ActionClass.IRREVERSIBLE,
            metadata=metadata or {},
        )
        self._messages[broadcast_id] = msg
        if channel not in self._channel_messages:
            self._channel_messages[channel] = []
        self._channel_messages[channel].append(broadcast_id)
        logger.info("Published broadcast %s on channel %s: %s", broadcast_id, channel, message)
        return msg

    def compensate(
        self,
        original_broadcast_id: str,
        compensation_id: str,
        compensating_message: str,
        sender: str = "system",
        notice_type: str = "RESOLUTION",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> BroadcastMessage:
        """Publish a forward compensating message referencing an earlier broadcast."""
        orig = self.get_message(original_broadcast_id)
        if not orig:
            raise KeyError(f"Original broadcast {original_broadcast_id} not found to compensate")

        meta = dict(metadata or {})
        meta["notice_type"] = notice_type
        meta["compensated_message_id"] = original_broadcast_id

        comp_msg = BroadcastMessage(
            broadcast_id=compensation_id,
            channel=orig.channel,
            message=compensating_message,
            sender=sender,
            is_compensating=True,
            replaces_broadcast_id=original_broadcast_id,
            action_class=ActionClass.IRREVERSIBLE,
            metadata=meta,
        )
        self._messages[compensation_id] = comp_msg
        self._channel_messages[orig.channel].append(compensation_id)
        logger.info(
            "Published compensating notice %s on channel %s (referencing %s): %s",
            compensation_id, orig.channel, original_broadcast_id, compensating_message
        )
        return comp_msg

    def get_message(self, broadcast_id: str) -> Optional[BroadcastMessage]:
        """Retrieve a specific broadcast by ID."""
        return self._messages.get(broadcast_id)

    def get_messages(self, channel: Optional[str] = None) -> List[BroadcastMessage]:
        """Retrieve all broadcasts, optionally filtered by channel."""
        if channel:
            ids = self._channel_messages.get(channel, [])
            return [self._messages[mid] for mid in ids if mid in self._messages]
        return list(self._messages.values())

    def attempt_revert(self, broadcast_id: str) -> None:
        """Direct revert attempt on irreversible broadcast fails closed."""
        msg = self.get_message(broadcast_id)
        channel = msg.channel if msg else "unknown"
        raise IrreversibleActionBlockedError(
            f"IRREVERSIBLE_ACTION: Broadcast {broadcast_id} on channel {channel} cannot be unsent. "
            f"Use forward compensation notice via compensate() instead."
        )
