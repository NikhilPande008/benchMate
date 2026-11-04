"""Fault ledger: deterministic Bayes update over the pack's faults (ARCHITECTURE.md §6.2).

    P(e | F) = c * 1[class(e) = pred_F(m)] + (1 - c) / K_m
    faults with no prediction for m, and the "other" catch-all, use 1 / K_m

Only the latest reading of each measurement counts: a re-measure supersedes the earlier one
(repeat readings of one point are not independent evidence), and the posterior is recomputed
from the priors over the active readings, in arrival order. The language model never sees or
changes any of this; it gets the result as one JSON line.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from mcp_evidence.packs.schema import Pack

OTHER = "other"
STOP_TOP = 0.85  # top fault must be strictly above this...
STOP_SECOND = 0.10  # ...and the runner-up strictly below this
CONFLICT_OTHER = 0.4  # "other" strictly above this is a conflict
CONFLICT_CONFIDENCE = 0.9  # a reading at least this confident...
CONFLICT_DROP = 0.5  # ...that drops the leader by more than this fraction is a conflict
MISSING_PRIOR = 0.01  # a fault with no prior for the session's symptom


@dataclass(frozen=True)
class Observation:
    evidence_id: str
    measurement_id: str
    outcome: str
    confidence: float
    remeasure: bool = False  # the engine asked for this re-measurement to settle a conflict


@dataclass(frozen=True)
class Conflict:
    reason: str  # "other_high" | "leader_drop"
    remeasure_measurement: str | None  # most influential earlier reading
    remeasure_evidence: str | None


@dataclass
class Snapshot:
    index: int
    observation: Observation
    before: dict[str, float]
    after: dict[str, float]
    superseded: str | None = None  # evidence id this reading replaced
    stop: bool = False
    conflict: Conflict | None = None

    @property
    def deltas(self) -> dict[str, float]:
        return {k: self.after[k] - self.before[k] for k in self.after}

    @property
    def influence(self) -> float:
        """Total variation distance: how far this reading moved the distribution."""
        return 0.5 * sum(abs(d) for d in self.deltas.values())

    def moved(self, n: int = 3) -> list[tuple[str, float]]:
        return sorted(self.deltas.items(), key=lambda kv: (-abs(kv[1]), kv[0]))[:n]


def normalise(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    return {k: v / total for k, v in weights.items()}


@dataclass
class Ledger:
    pack: Pack
    symptom: str
    priors: dict[str, float] = field(init=False)
    active: list[Observation] = field(default_factory=list)
    history: list[Snapshot] = field(default_factory=list)

    def __post_init__(self) -> None:
        raw = {f.id: f.priors.get(self.symptom, MISSING_PRIOR) for f in self.pack.faults}
        other = self.pack.ledger.other_prior
        faults = normalise(raw)
        self.priors = {k: v * (1 - other) for k, v in faults.items()} | {OTHER: other}
        self._posterior = dict(self.priors)
        self._faults = {f.id: f for f in self.pack.faults}
        self._k = {m.id: len(m.classes) for m in self.pack.measurements}

    # --- maths --------------------------------------------------------------------------------

    def likelihood(self, hypothesis: str, measurement_id: str, outcome: str, confidence: float) -> float:
        k = self._k[measurement_id]
        if hypothesis == OTHER:
            return 1.0 / k
        predicted = self._faults[hypothesis].predicts.get(measurement_id)
        if predicted is None:
            return 1.0 / k
        return confidence * (1.0 if outcome == predicted else 0.0) + (1.0 - confidence) / k

    def _compute(self, observations: list[Observation]) -> dict[str, float]:
        weights = dict(self.priors)
        for obs in observations:
            for h in weights:
                weights[h] *= self.likelihood(h, obs.measurement_id, obs.outcome, obs.confidence)
        return normalise(weights)

    # --- state --------------------------------------------------------------------------------

    @property
    def posterior(self) -> dict[str, float]:
        return dict(self._posterior)

    def ranked(self) -> list[tuple[str, float]]:
        return sorted(self._posterior.items(), key=lambda kv: (-kv[1], kv[0]))

    def leader(self) -> tuple[str, float]:
        """Top real fault (never "other")."""
        return max(((h, p) for h, p in self._posterior.items() if h != OTHER), key=lambda kv: (kv[1], kv[0]))

    def measured(self) -> set[str]:
        return {o.measurement_id for o in self.active}

    def stop(self) -> bool:
        ranked = self.ranked()
        top, second = ranked[0], ranked[1]
        return top[0] != OTHER and top[1] > STOP_TOP and second[1] < STOP_SECOND

    def add(self, obs: Observation) -> Snapshot:
        if obs.measurement_id not in self._k:
            raise KeyError(f"unknown measurement {obs.measurement_id}")
        if obs.outcome not in self.pack.measurement(obs.measurement_id).classes:
            raise ValueError(f"{obs.outcome!r} is not a class of {obs.measurement_id}")
        before = self.posterior
        leader_before, p_leader_before = self.leader()
        replaced = next((o for o in self.active if o.measurement_id == obs.measurement_id), None)
        self.active = [o for o in self.active if o.measurement_id != obs.measurement_id] + [obs]
        self._posterior = self._compute(self.active)
        snap = Snapshot(
            index=len(self.history),
            observation=obs,
            before=before,
            after=self.posterior,
            superseded=replaced.evidence_id if replaced else None,
        )
        snap.stop = self.stop()
        snap.conflict = self._conflict(snap, leader_before, p_leader_before)
        if snap.conflict:
            snap.stop = False
        self.history.append(snap)
        return snap

    def _conflict(self, snap: Snapshot, leader: str, p_before: float) -> Conflict | None:
        """"other" too high, or a confident reading that knocks the leader down by more than half.

        A leader drop is a conflict only between readings: it needs an earlier reading to blame
        (the most influential one still active, else the reading this one replaced). A leader that
        only the priors put on top, and a re-measurement the engine asked for (the resolution of an
        earlier conflict), don't raise one."""
        obs = snap.observation
        target = self.most_influential(exclude=obs.evidence_id)
        if target is not None:
            blame = (target.observation.measurement_id, target.observation.evidence_id)
        elif snap.superseded is not None:
            blame = (obs.measurement_id, snap.superseded)
        else:
            blame = (None, None)
        reason = None
        if snap.after[OTHER] > CONFLICT_OTHER:
            reason = "other_high"
        elif (
            not obs.remeasure
            and blame[1] is not None
            and obs.confidence >= CONFLICT_CONFIDENCE
            and p_before > 0
            and (p_before - snap.after[leader]) / p_before > CONFLICT_DROP
        ):
            reason = "leader_drop"
        if reason is None:
            return None
        return Conflict(reason=reason, remeasure_measurement=blame[0], remeasure_evidence=blame[1])

    def most_influential(self, exclude: str | None = None) -> Snapshot | None:
        """The still-active earlier reading that moved the distribution the most."""
        active_ids = {o.evidence_id for o in self.active} - {exclude}
        candidates = [s for s in self.history if s.observation.evidence_id in active_ids]
        return max(candidates, key=lambda s: (s.influence, -s.index), default=None)
