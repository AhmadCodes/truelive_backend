"""
Parser registry for inbound alerts.

Each parser receives a parsed `email.message.Message` and returns a `ParserResult`.
The registry dispatches on (sender, subject pattern, content fingerprint). If no
parser matches, the `unknown` fallback returns an `unparsed` result with the
original subject/body — even unparsed alerts get forwarded so the consumer can
review (spec §8 "even unparsed alerts get forwarded").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from email.message import Message
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class ParserResult:
    parser_id: str
    parser_version: int
    parser_confidence: str  # exact | heuristic | llm_generated | unparsed
    event_type: str         # motion | person | vehicle | intrusion | unknown
    event_subtype: Optional[str] = None
    confidence: Optional[float] = None
    detected_at: Optional[datetime] = None
    subject: Optional[str] = None
    body_text: Optional[str] = None
    extra: dict = field(default_factory=dict)


# (predicate, parser) — the first matching predicate wins.
_REGISTRY: list[tuple[Callable[[Message], bool], Callable[[Message], ParserResult]]] = []


def register(predicate, parser):
    """Append a (predicate, parser) pair to the registry.

    Predicates take the parsed Message and return bool. Parsers take the same
    Message and return a ParserResult. Order of registration = match priority.
    """
    _REGISTRY.append((predicate, parser))


def dispatch(msg: Message) -> ParserResult:
    """Find and run the first matching parser. Falls back to `unparsed`."""
    for predicate, parser in _REGISTRY:
        try:
            if predicate(msg):
                return parser(msg)
        except Exception:
            logger.exception("parser predicate raised; skipping")
    return unparsed_fallback(msg)


def unparsed_fallback(msg: Message) -> ParserResult:
    """Parser of last resort: pull subject + plain-text body."""
    from app.services.alert_parsers.helpers import extract_subject, extract_text_body
    return ParserResult(
        parser_id="unknown_v1",
        parser_version=1,
        parser_confidence="unparsed",
        event_type="unknown",
        subject=extract_subject(msg),
        body_text=extract_text_body(msg),
    )


# Import templates to register them. Done at module load so dispatch sees them.
from app.services.alert_parsers import calipsa  # noqa: E402,F401
