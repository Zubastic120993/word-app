from sqlalchemy import Column, Integer, Boolean, Text, DateTime
from sqlalchemy.types import JSON
from datetime import datetime

from app.database import Base


class ChatState(Base):
    __tablename__ = "chat_state"

    id = Column(Integer, primary_key=True)

    conversation_json = Column(JSON, nullable=True)
    explained_bases_json = Column(JSON, nullable=True)
    user_produced_json = Column(JSON, nullable=True)
    assistant_exposed_json = Column(JSON, nullable=True)

    session_vocab_json = Column(JSON, nullable=True)
    session_vocab_active = Column(Boolean, nullable=False, default=False)

    theme_user_messages = Column(Integer, nullable=False, default=0)
    current_theme = Column(Text, nullable=True)
    checkpoint_done = Column(Boolean, nullable=False, default=False)

    updated_at = Column(DateTime, default=datetime.utcnow)
