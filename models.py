# -*- coding: utf-8 -*-
# models.py  � ShiftBot Portal data models
import datetime as dt
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

def datetime_utc() -> dt.datetime:
    """Return current UTC datetime (naive)."""
    return dt.datetime.utcnow()

# ---------------------------------------------------------------------------
# ORM classes
# ---------------------------------------------------------------------------

class User(UserMixin, db.Model):
    __tablename__ = "user"

    id                = db.Column(db.Integer, primary_key=True)
    email             = db.Column(db.String(120), unique=True, nullable=False)
    password_hash     = db.Column(db.String(256))
    duo_done          = db.Column(db.Boolean, default=False)
    monitoring_active = db.Column(db.Boolean, default=False, nullable=False)
    api_key           = db.Column(db.String(64), unique=True, nullable=False, index=True)

    configs = db.relationship(
        "ShiftConfig", backref="owner",
        cascade="all, delete", lazy="dynamic"
    )
    scraped_shifts = db.relationship(
        "ScrapedShift", backref="shift_owner",
        cascade="all, delete", lazy="dynamic"
    )

    # password helpers
    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        if self.password_hash is None:
            return False
        return check_password_hash(self.password_hash, password)


class ShiftConfig(db.Model):
    __tablename__ = "shift_config"

    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    target_date     = db.Column(db.String(255), nullable=True)   # comma-separated
    target_time     = db.Column(db.String(255), nullable=True)
    target_position = db.Column(db.String(255), nullable=True)

    created         = db.Column(db.DateTime, default=datetime_utc)
    def as_dict(self):
        return {
            "id": self.id,
            "target_date": self.target_date,
            "target_time": self.target_time,
            "target_position": self.target_position,
            "created": self.created.isoformat() if self.created else None
        }


class ScrapedShift(db.Model):
    __tablename__ = "scraped_shift"

    id                 = db.Column(db.Integer, primary_key=True)
    user_id            = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    raw_text           = db.Column(db.Text, nullable=False)
    shift_date_str     = db.Column(db.String(50), nullable=True)
    shift_time_str     = db.Column(db.String(50), nullable=True)
    shift_position_str = db.Column(db.String(100), nullable=True)

    scraped_at         = db.Column(db.DateTime, default=datetime_utc)
    is_new_for_user    = db.Column(db.Boolean, default=True, nullable=False)
    def as_dict(self):
        return {
            "id": self.id,
            "date": self.shift_date_str,
            "time": self.shift_time_str,
            "position": self.shift_position_str,
            "raw": self.raw_text,
            "scraped_at": self.scraped_at.isoformat() + "Z"
        }