"""Power approval lifetime (CLAUDE.md rule 2).

An approval from the safety gate is bound to one power action and to the supply settings it was
checked against (set voltage and current limit). While it is valid, the gate's exact sentence may be
spoken again (the page's output guardrail allows repeats). It ends:

- when the user confirms the action ("lag gaya", "connected", "दबा दिया") or the measurement that
  needed it starts ("confirmed" / "used");
- after 20 s ("expired");
- when a new supply reading shows different settings, or settings that can't be read
  ("supply_changed");
- on barge-in ("barge_in");
- when a different power action is approved ("superseded");
- on a guardrail violation or a safety stop ("violation" / "stopped").

After that the sentence is blocked again and the step needs a fresh gate check.

One detail: a confirmation ends the right to *say* the sentence, but the measurement the approval
was for (the boot-current watch after "lag gaya") may still start for a short grace period, because
the action already happened under a valid approval. The page learns every ending from the engine's
`power.ended` event, and handles expiry and barge-in itself as well, so it never lags behind.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from engine.safety.scope import _hits, _negated, _words
from mcp_evidence.schema import EvidenceRecord, utcnow

APPROVAL_TTL_S = 20.0
CONFIRM_GRACE_S = 10.0  # after "lag gaya", the watch may still start this long

EndReason = Literal["confirmed", "used", "expired", "supply_changed", "barge_in", "superseded", "renewed", "violation", "stopped"]
Settings = tuple[float | None, float | None]  # (set_voltage, current_limit) as displayed

# What a user says once the action is done. Whole phrases only: "laga" alone is also "laga doon?".
_DONE_ANY = ["done", "ho gaya", "ho gya", "hogaya", "ho gayi", "kar diya", "kar di", "kar dia", "हो गया", "हो गई", "कर दिया", "कर दी"]
DONE_PHRASES: dict[str, list[str]] = {
    "connect_boot_cable": _DONE_ANY + [
        "connected", "plugged", "plugged in", "it is in", "its in", "attached", "lag gaya", "lag gya", "lag gayi",
        "laga diya", "laga di", "laga dia", "lagaya", "lagaa diya", "connect kiya", "connect ho gaya", "jod diya",
        "लग गया", "लग गई", "लगा दिया", "लगा दी", "लगाया", "जोड़ दिया", "कनेक्ट कर दिया", "कनेक्ट हो गया",
    ],
    "press_power_key": _DONE_ANY + [
        "pressed", "pressing", "holding", "dabaya", "daba diya", "daba di", "dab gaya", "press kiya", "press kar diya",
        "दबाया", "दबा दिया", "दबा दी", "दब गया", "प्रेस कर दिया",
    ],
}
DONE_PHRASES["connect_port_supply"] = DONE_PHRASES["connect_boot_cable"]


def action_done(text: str | None, step: str) -> bool:
    """Whether the user says the power action is done. Not a question, not negated
    ("abhi nahi lagaya", "not connected yet")."""
    if not text or text.strip().endswith("?"):
        return False
    words = _words(text)
    hits = _hits(words, DONE_PHRASES.get(step, _DONE_ANY))
    return any(not _negated(words, i) for i in hits)


def settings_of(rec: EvidenceRecord | None) -> Settings:
    if rec is None or not rec.fields:
        return (None, None)

    def val(name: str) -> float | None:
        f = rec.fields.get(name) if rec.fields else None
        return None if f is None or f.value is None else round(float(f.value), 3)

    return (val("set_voltage"), val("current_limit"))


@dataclass
class Approval:
    id: str
    step: str
    at: datetime
    supply: Settings = (None, None)
    confirmed_at: datetime | None = None

    def age_s(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.at).total_seconds()

    def line(self, ttl_s: float) -> dict:
        """For the companion screen's power banner."""
        return {"id": self.id, "step": self.step, "volts": self.supply[0], "limit_a": self.supply[1],
                "granted_ms": round(self.at.timestamp() * 1000), "ttl_s": ttl_s}


@dataclass
class Approvals:
    """At most one live approval (CLAUDE.md rule 2, approval lifetime)."""

    pending: Approval | None = None
    ttl_s: float = APPROVAL_TTL_S
    confirm_grace_s: float = CONFIRM_GRACE_S
    ended: list[tuple[Approval, str]] = field(default_factory=list)

    def _end(self, reason: EndReason) -> Approval | None:
        a, self.pending = self.pending, None
        if a:
            self.ended.append((a, reason))
        return a

    def grant(self, step: str, supply: Settings = (None, None), now: datetime | None = None) -> tuple[Approval, tuple[Approval, str] | None]:
        """A new approval; the previous one ends ("superseded" if it was for another action,
        "renewed" if the same action was checked again)."""
        before = None
        if self.pending:
            reason: EndReason = "renewed" if self.pending.step == step else "superseded"
            before = (self._end(reason), reason)
        self.pending = Approval(f"pw_{secrets.token_hex(3)}", step, now or utcnow(), supply)
        return self.pending, before  # type: ignore[return-value]

    def expire(self, now: datetime | None = None) -> Approval | None:
        p = self.pending
        if p and p.age_s(now) >= self.ttl_s and not self._in_grace(p, now):
            return self._end("expired")
        return None

    def _in_grace(self, p: Approval, now: datetime | None) -> bool:
        return p.confirmed_at is not None and ((now or utcnow()) - p.confirmed_at).total_seconds() < self.confirm_grace_s

    def speakable(self, now: datetime | None = None) -> Approval | None:
        """The approval whose sentence may be spoken right now, if any."""
        self.expire(now)
        p = self.pending
        return p if p and p.confirmed_at is None and p.age_s(now) < self.ttl_s else None

    def valid(self, step: str, now: datetime | None = None) -> bool:
        """Whether the measurement that needs this power step may start."""
        self.expire(now)
        p = self.pending
        return bool(p and p.step == step and (p.age_s(now) < self.ttl_s or self._in_grace(p, now)))

    def consume(self, step: str, now: datetime | None = None) -> bool:
        """The measurement that needed the step starts: single use."""
        if not self.valid(step, now):
            return False
        self._end("used")
        return True

    def confirm(self, text: str | None, now: datetime | None = None) -> Approval | None:
        """The user says the action is done: the sentence may no longer be spoken."""
        self.expire(now)
        p = self.pending
        if p and p.confirmed_at is None and action_done(text, p.step):
            p.confirmed_at = now or utcnow()
            self.ended.append((p, "confirmed"))
            return p
        return None

    def on_supply(self, supply: Settings) -> Approval | None:
        """A new supply reading: different (or unreadable) settings end the approval."""
        p = self.pending
        if p and supply != p.supply:
            return self._end("supply_changed")
        return None

    def end(self, reason: EndReason) -> Approval | None:
        """Barge-in, violation, safety stop."""
        return self._end(reason)
