from __future__ import annotations

from ..models.chat_message import ChatMessageModel
from .base import BaseRepository


class ChatMessageRepository(BaseRepository):
    __table__ = "chat_messages"
    model = ChatMessageModel
