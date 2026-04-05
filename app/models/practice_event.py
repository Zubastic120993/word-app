from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, JSON, String

from app.database import Base


class PracticeEvent(Base):
    __tablename__ = "practice_events"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    event_type = Column(String(50), nullable=False)
    theme = Column(String(50), nullable=True)
    payload = Column(JSON, nullable=False)
