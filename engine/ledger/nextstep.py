"""Next-best step: value of information per unit cost (ARCHITECTURE.md §6.3).

    IG(m) = H(F) - sum_o P(o) H(F | o),   P(o) = sum_i P(F_i) P(o | F_i)
    score(m) = IG(m) / cost(m)

Likelihoods use the pack's planning confidence. Ties (within 1e-9) go to the measurement that
comes first in the pack, which is the order an expert would follow.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .ledger import OTHER, Ledger, normalise

TIE_EPS = 1e-9
MIN_GAIN_BITS = 1e-6


def entropy(p: dict[str, float]) -> float:
    return -sum(v * math.log2(v) for v in p.values() if v > 0)


@dataclass(frozen=True)
class Step:
    measurement_id: str
    kind: str
    point_id: str | None
    mode: str | None
    requires_power: str | None
    gain_bits: float
    score: float
    separates: tuple[str, str] | None  # the two most likely faults this measurement tells apart

    def reason(self, ledger: Ledger, lang: str = "en") -> str:
        if not self.separates:
            return "narrows the remaining faults" if lang == "en" else "baaki faults ko kam karega"
        names = []
        for h in self.separates:
            if h == OTHER:
                names.append("an unknown fault" if lang == "en" else "koi aur fault")
            else:
                f = next(f for f in ledger.pack.faults if f.id == h)
                names.append(getattr(f.name, lang))
        a, b = names
        return f"separates {a} from {b}" if lang == "en" else f"{a} aur {b} mein fark karega"


def information_gain(ledger: Ledger, measurement_id: str, confidence: float) -> float:
    prior = ledger.posterior
    classes = ledger.pack.measurement(measurement_id).classes
    expected = 0.0
    for o in classes:
        joint = {h: p * ledger.likelihood(h, measurement_id, o, confidence) for h, p in prior.items()}
        p_o = sum(joint.values())
        if p_o > 0:
            expected += p_o * entropy(normalise(joint))
    return entropy(prior) - expected


def _separates(ledger: Ledger, measurement_id: str) -> tuple[str, str] | None:
    preds = {f.id: f.predicts.get(measurement_id) for f in ledger.pack.faults}
    ranked = [h for h, _ in ledger.ranked()]
    for i, a in enumerate(ranked):
        for b in ranked[i + 1 :]:
            pa, pb = preds.get(a), preds.get(b)  # "other" predicts nothing
            if (pa or pb) and pa != pb:
                return a, b
    return None


def candidates(ledger: Ledger, exclude: set[str] | None = None) -> list[Step]:
    """Every not-yet-measured measurement with its score, in pack order."""
    done = ledger.measured() | (exclude or set())
    c = ledger.pack.ledger.planning_confidence
    out = []
    for m in ledger.pack.measurements:
        if m.id in done:
            continue
        gain = information_gain(ledger, m.id, c)
        point = ledger.pack.point(m.point_id) if m.point_id else None
        out.append(
            Step(
                measurement_id=m.id,
                kind=m.kind,
                point_id=m.point_id,
                mode=point.mode if point else ("current" if m.kind in ("boot_pattern", "port_current") else None),
                requires_power=m.requires_power,
                gain_bits=gain,
                score=gain / m.cost,
                separates=_separates(ledger, m.id),
            )
        )
    return out


def next_step(ledger: Ledger, exclude: set[str] | None = None) -> Step | None:
    """Best measurement, or None when nothing left would tell the faults apart."""
    best: Step | None = None
    for step in candidates(ledger, exclude):  # pack order, so the first of equals wins
        if step.gain_bits < MIN_GAIN_BITS:
            continue
        if best is None or step.score > best.score + TIE_EPS:
            best = step
    return best
