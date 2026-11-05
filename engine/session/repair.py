"""Repair session: state, evidence, ledger and safety behind the 8 exposed tools.

Every tool returns a small dict that the router turns into one short JSON line (rule 6). The
ledger decides faults and next steps (rule 1); the power gate decides power steps (rule 2).
Fine-grained operations (classification, read-back, approvals, watchdog) stay in here.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from engine.ledger import Ledger, Observation, Snapshot, Step, candidates, next_step as choose_next_step
from engine.safety import (
    APPROVED,
    DISCONNECT_NOW,
    REFUSE_DEVICE,
    STALE_S,
    STOP_MESSAGE,
    Approvals,
    AtLimitWatchdog,
    GateResult,
    battery_report,
    check_power,
    device_out_of_scope,
)
from engine.safety.approval import Approval, EndReason, settings_of
from engine.session.language import NOTE as LANGUAGE_NOTE, SessionLanguage, clear_language
from mcp_evidence.packs import classify_band, classify_keywords, load_pack, model_lookup
from mcp_evidence.packs.schema import Measurement, Pack
from mcp_evidence.schema import BootSample, EvidenceError, EvidenceRecord, new_id
from mcp_evidence.source import EvidenceSource
from mcp_evidence.spoken.parser import SpokenReading, parse, readback

Notify = Callable[[dict[str, Any]], Awaitable[None]]

CAMERA_MIN_CONFIDENCE = 0.9  # below this a camera read is "unclear": ask to re-read or say it
SPOKEN_CONFIDENCE = 0.7  # a spoken reading, until it is read back and confirmed
CONFIRMED_CONFIDENCE = 0.85
USE_TOOL = {  # the tool that records the answer to a step (in the step line, for the voice model)
    "point": "get_reading",  # or record_spoken_reading if the user says the value; not in the line (length)
    "observation": "record_spoken_reading",
    "boot_pattern": "watch_boot_current",
    "port_current": "get_reading",
}
SUPPLY_REFRESH_MARGIN_S = 5.0  # re-read the supply for a power step if the reading is older than 15 s
CONFIRM_WORDS = {"haan", "han", "haa", "yes", "yeah", "yep", "sahi", "correct", "right", "ji", "bilkul", "hm", "हाँ", "हां", "सही", "जी"}
CANCEL_WORDS = {"ruko", "rukiye", "stop", "wait", "ruk", "रुको", "रुकिए"}
PLAUSIBLE = {"diode": (0.0, 3.0), "dcv": (0.0, 30.0), "ohm": (0.0, 1e7), "current": (0.0, 5.0)}
OBSERVATION_WORDS = {
    "ok": {"en": "cable OK", "hi": "cable theek"},
    "faulty": {"en": "cable faulty", "hi": "cable kharab"},
    "none": {"en": "nothing hot", "hi": "kuch garam nahi"},
    "cap_hot": {"en": "capacitor hot", "hi": "capacitor garam"},
    "ic_hot": {"en": "IC hot", "hi": "IC garam"},
}
SYMPTOM_WORDS = {"not_charging": ["charg", "चार्ज"]}
VIEW_PROBLEMS = {  # BUILD_GUIDE corner cases: a hand or probe over the display; a dying meter battery
    "view_blocked": {"ask": "clear_view", "en": "Something is covering the meter display; please move it out of the way.",
                     "hi": "Meter display ke aage kuch aa raha hai; please hata do."},
    "meter_blank": {"ask": "check_meter_battery", "en": "The meter display is blank or very dim; please check the meter battery.",
                    "hi": "Meter ka display khaali ya bahut dhundhla hai; meter ki battery check karo."},
}
VISION_NOTE = {
    "full": "Camera readings are back to normal.",
    "reader_b_only": "Camera reading limited: only the local reader works. Camera values now come back as a read-back; confirm each with the user before going on.",
    "spoken_only": "The camera can't read the displays. Ask the user to read each value aloud and use record_spoken_reading.",
}


def _fits(m: Measurement, text: str | None) -> bool:
    """Whether the words can answer this measurement: an observation's keywords, or a number/OL."""
    if m.kind == "observation":
        return classify_keywords(m, text or "") is not None
    if m.kind == "boot_pattern":
        return False
    return parse(text or "").ok


def _names(m: Measurement, text: str | None) -> bool:
    """Whether the words name this measurement's point ("VBUS", "power key", "fuse")."""
    words = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    ident = m.point_id or m.id
    parts = [p for p in ident.split("_") if len(p) >= 4 and not p.isdigit()]
    return bool(words & set(parts)) or ident.replace("_", " ") in " ".join(sorted(words))


WORD_RE = r"[a-z]+|[\u0900-\u0963\u0966-\u097f]+"  # Latin or Devanagari words, without the danda (।)


def _is_confirmation(text: str | None) -> bool:
    """A plain yes ("haan", "haan sahi hai", "हाँ, सही है", "yes that's right")."""
    words = set(re.findall(WORD_RE, (text or "").lower()))
    return bool(words) and bool(words & CONFIRM_WORDS) and words <= CONFIRM_WORDS | {"hai", "ho", "he", "that", "s", "it", "is", "है"}


def _symptom(text: str | None) -> str:
    t = (text or "").lower()
    return "not_charging" if any(w in t for w in SYMPTOM_WORDS["not_charging"]) else "dead"


@dataclass
class Readback:
    record: EvidenceRecord
    measurement_id: str | None
    say: str
    user_confirmed: bool = False  # the user's own words after the read-back were a plain "yes"


@dataclass
class RepairSession:
    session_id: str
    bench: EvidenceSource
    notify: Notify | None = None  # UI events to the browser
    inject: Notify | None = None  # short engine messages into the voice conversation
    vision_timeout_s: float = 4.0
    persist: Callable[[str, dict[str, Any]], None] | None = None  # (table, row)

    pack: Pack | None = None
    ledger: Ledger | None = None
    symptom: str | None = None
    model_data: bool = False
    level: str = "trainee"
    state: str = "intake"  # intake, probing, fix, closed, unresolved, stopped
    battery: str = "unknown"  # unknown, removed, connected
    stopped: str | None = None
    supply: EvidenceRecord | None = None
    evidence: list[EvidenceRecord] = field(default_factory=list)
    approvals: Approvals = field(default_factory=Approvals)  # lifetime rules: engine/safety/approval.py
    pending_step: Step | None = None
    remeasure: str | None = None  # measurement the conflict rule asked to re-measure
    awaiting_spoken: str | None = None  # measurement to be read aloud after a camera failure
    readback_pending: Readback | None = None
    fix_fault: str | None = None
    fix_at: int | None = None  # len(evidence) when the fix was proposed
    fix_check: str | None = None  # measurement that must read healthy before "fixed" is accepted
    degraded: str | None = None  # "camera_unavailable": no power steps (the gate needs a camera supply read)
    conflicts: int = 0
    lang: SessionLanguage = field(default_factory=SessionLanguage)
    events: list[dict[str, Any]] = field(default_factory=list)
    _watch: asyncio.Task | None = None
    _power_sent: int = 0  # approvals.ended entries already sent as power.ended events
    last_gate: dict[str, Any] | None = None  # the safety gate's last answer, for the screen's power banner
    vision_level: tuple[str, str | None] = ("full", None)
    live_watch: list[list[float]] = field(default_factory=list)  # [t_s, current_a] of the running watch
    _sends: set[asyncio.Task] = field(default_factory=set)

    # --- plumbing -----------------------------------------------------------------------------

    @property
    def lng(self) -> str:
        return self.lang.lang

    def _event(self, name: str, **data: Any) -> None:
        ev = {"event": name, "t_ms": round(time.time() * 1000), **data}
        self.events.append(ev)
        if self.notify:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # called outside the event loop (tests, scripts): log only
                return
            task = loop.create_task(self.notify({"type": "ui", **ev}))
            self._sends.add(task)  # keep a reference until it is sent
            task.add_done_callback(self._sends.discard)

    def _persist(self, table: str, row: dict[str, Any]) -> None:
        if self.persist:
            self.persist(table, row)

    async def note(self, text: str, urgent: bool = False) -> None:
        """A short engine message into the voice conversation (never an instructions update, rule 7).
        Urgent ones interrupt and get an immediate reply; others are context for the next turn."""
        msg = {"type": "inject", "urgent": urgent, "text": f"[engine] {text}"}
        self._event("engine.note", urgent=urgent, text=msg["text"])
        if self.inject:
            await self.inject(msg)

    def _note_soon(self, text: str, urgent: bool = False) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.note(text, urgent))
        self._sends.add(task)
        task.add_done_callback(self._sends.discard)

    # --- persistence: state + ordered evidence rebuild the session after a restart --------------

    STATE_FIELDS = ("symptom", "model_data", "level", "state", "battery", "stopped", "remeasure", "awaiting_spoken",
                    "fix_fault", "fix_at", "fix_check", "degraded", "conflicts")

    def state_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "pack_id": self.pack.pack_id if self.pack else None,
            **{k: getattr(self, k) for k in self.STATE_FIELDS},
            "lang": self.lang.main,
            "lang_streak": self.lang._streak,
            "lang_seen": self.lang.seen,
            "lang_source": self.lang.source,
            "pending": self.pending_step.measurement_id if self.pending_step else None,
        }

    def _save(self) -> None:
        self._persist("repair_state", self.state_dict())

    @classmethod
    def restore(cls, state: dict[str, Any], evidence: list[tuple[EvidenceRecord, str | None]], bench: EvidenceSource,
                **kwargs: Any) -> "RepairSession":
        """Rebuild from stored state and evidence (in arrival order). The ledger is recomputed by
        replaying the readings it used; power approvals and read-backs are not restored, so any
        power step needs a fresh supply read and a new approval."""
        r = cls(state["session_id"], bench, **kwargs)
        if state.get("pack_id"):
            r.pack = load_pack(state["pack_id"])
            r.ledger = Ledger(r.pack, state["symptom"])
        for rec, use in evidence:
            r.evidence.append(rec)
            if rec.kind == "supply":
                r.supply = rec
            if use in ("ledger", "remeasure") and r.ledger:
                r._apply(rec, use == "remeasure")
        for k in cls.STATE_FIELDS:
            setattr(r, k, state.get(k))
        r.lang = SessionLanguage(state.get("lang"), _streak=state.get("lang_streak") or 0,
                                 seen=state.get("lang_seen", 2 if state.get("lang") else 0), source=state.get("lang_source"))
        if state.get("pending") and r.ledger:
            r.pending_step = next((c for c in candidates(r.ledger) if c.measurement_id == state["pending"]), None)
        return r

    def _top(self, n: int = 2) -> list[list[Any]]:
        assert self.ledger
        return [[h, round(p, 2)] for h, p in self.ledger.ranked()[:n]]

    def _fault_name(self, fid: str) -> str:
        assert self.pack
        if fid == "other":
            return "unknown fault" if self.lng == "en" else "koi aur fault"
        return getattr(next(f for f in self.pack.faults if f.id == fid).name, self.lng)

    def _measurement(self, mid: str) -> Measurement:
        assert self.pack
        return self.pack.measurement(mid)

    def _store(self, record: EvidenceRecord, ledger_use: str | None) -> None:
        self.evidence.append(record)
        self._persist("evidence", record.model_dump(mode="json") | {"seq": len(self.evidence) - 1, "ledger_use": ledger_use})

    def _ledger_use(self, record: EvidenceRecord) -> str | None:
        if not (self.ledger and record.measurement_id and record.outcome):
            return None
        if self.state == "fix":
            return "fix_check" if record.measurement_id == self.fix_check else "after_fix"
        return "remeasure" if self.remeasure == record.measurement_id else "ledger"

    def _add_evidence(self, record: EvidenceRecord) -> Snapshot | None:
        """Store a record; if it answers a measurement and has an outcome, update the ledger.
        Readings after a fix was proposed check the fix; they never move the diagnosis."""
        use = self._ledger_use(record)
        self._store(record, use)
        self._event("evidence.added", id=record.id, kind=record.kind, measurement=record.measurement_id,
                    value=record.value, open=record.open, outcome=record.outcome, conf=record.confidence,
                    source=record.source, simulated=record.simulated)
        if use == "fix_check":
            self._event("fix.check", measurement=record.measurement_id, outcome=record.outcome, passed=self._healthy(record))
        if use not in ("ledger", "remeasure"):
            self._save()
            return None
        snap = self._apply(record, use == "remeasure")
        self._persist("ledger_snapshots", {
            "session_id": self.session_id, "index": snap.index, "evidence_id": record.id,
            "after": snap.after, "deltas": snap.deltas, "stop": snap.stop,
            "conflict": snap.conflict.reason if snap.conflict else None,
        })
        self._event("ledger.updated", evidence=record.id, measurement=record.measurement_id, outcome=record.outcome,
                    top=self._top(3), moved=[[h, round(d, 3)] for h, d in snap.moved(3)], stop=snap.stop,
                    conflict=snap.conflict.reason if snap.conflict else None)
        self._save()
        return snap

    def _apply(self, record: EvidenceRecord, remeasure: bool) -> Snapshot:
        """The ledger update and its bookkeeping (live, and when replaying stored evidence)."""
        assert self.ledger and record.measurement_id and record.outcome
        snap = self.ledger.add(Observation(record.id, record.measurement_id, record.outcome, record.confidence, remeasure))
        if snap.superseded:
            old = next((e for e in self.evidence if e.id == snap.superseded), None)
            if old:
                old.superseded_by = record.id
        if self.remeasure == record.measurement_id:
            self.remeasure = None
        if snap.conflict:
            self.conflicts += 1
            self.remeasure = snap.conflict.remeasure_measurement
            if self.conflicts > 2:
                self.state = "unresolved"
        return snap

    def _healthy(self, record: EvidenceRecord) -> bool:
        assert self.pack and record.measurement_id
        return record.outcome == self._measurement(record.measurement_id).healthy and record.confidence >= CONFIRMED_CONFIDENCE

    def _ledger_line(self, snap: Snapshot | None, record: EvidenceRecord | None = None) -> dict[str, Any]:
        if self.state == "fix" and record is not None and record.measurement_id == self.fix_check:
            return {"fix_check": "pass" if self._healthy(record) else "fail"}
        if not snap or not self.ledger:
            return {}
        out: dict[str, Any] = {"top": self._top(2)}
        if snap.stop:
            out["stop"] = True
        if snap.conflict:
            out["conflict"] = True
        return out

    def _require_repair(self) -> dict[str, Any] | None:
        if self.stopped:
            return {"error": "session_stopped", "say": STOP_MESSAGE[self.lng]}
        if not self.ledger:
            return {"error": "start_repair_first"}
        return None

    # --- hooks from the conversation ----------------------------------------------------------

    async def on_user_transcript(self, text: str, lang_tag: str | None) -> None:
        changed = self.lang.observe(clear_language(text, lang_tag))
        if changed:
            await self._language_changed()
        report = battery_report(text)
        if report == "danger" and not self.stopped:
            await self.stop_session("battery or smoke reported")
        elif report in ("removed", "connected") and report != self.battery:
            self.battery = report
            self._event("battery", state=report)
            changed = True
        if changed:
            self._save()
        words = set(re.findall(WORD_RE, text.lower()))
        if words & CANCEL_WORDS and self._watch and not self._watch.done():
            self._watch.cancel()
        if self.readback_pending:
            self.readback_pending.user_confirmed = _is_confirmation(text)
        if self.approvals.confirm(text):
            self._power_events()

    async def set_language(self, lang: str) -> None:
        """The companion screen's language toggle."""
        if self.lang.toggle(lang):
            await self._language_changed()
            self._save()

    async def _language_changed(self) -> None:
        """Tell the screen (preamble clip) and the voice model (an engine note; instructions never change)."""
        self._event("session.lang", lang=self.lng, source=self.lang.source, decided=self.lang.decided)
        await self.note(LANGUAGE_NOTE[self.lng])

    def on_safety_violation(self, kind: str, text: str) -> None:
        """The page's output guardrail stopped a power instruction no live approval covered
        (CLAUDE.md rule 2 backstop). Logged, and any pending approval is withdrawn, so nothing the
        user does on the strength of it can power the board without a fresh gate check."""
        cancelled = self._end_approval("violation")
        self._event("safety.violation", kind=kind[:40], text=text[:200], cancelled=cancelled.step if cancelled else None)

    def on_barge_in(self) -> None:
        self._end_approval("barge_in")

    # --- power approval lifetime (CLAUDE.md rule 2; rules in engine/safety/approval.py) -------

    def _power_events(self) -> None:
        """One power.ended event (to the screen and the page's guardrail) per approval ending."""
        for a, reason in self.approvals.ended[self._power_sent:]:
            self._event("power.ended", id=a.id, step=a.step, reason=reason)
        self._power_sent = len(self.approvals.ended)

    def _end_approval(self, reason: EndReason) -> Approval | None:
        a = self.approvals.end(reason)
        self._power_events()
        return a

    def _consume(self, step: str) -> bool:
        ok = self.approvals.consume(step)
        self._power_events()
        return ok

    def _set_supply(self, rec: EvidenceRecord) -> None:
        """Every new supply reading: settings that differ from the ones an approval was checked
        against (or can't be read) end it."""
        self.supply = rec
        self.approvals.on_supply(settings_of(rec))
        self._power_events()

    def _expire_later(self) -> None:
        """Tell the screen and the page when the approval runs out (validity is also checked on use)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        def check() -> None:
            self.approvals.expire()
            self._power_events()

        loop.call_later(self.approvals.ttl_s + 0.05, check)

    async def stop_session(self, reason: str) -> None:
        self.stopped, self.state = reason, "stopped"
        self._end_approval("stopped")
        if self._watch and not self._watch.done():
            self._watch.cancel()
        self._event("safety.stop", reason=reason)
        self._save()
        await self.note(f"SAFETY STOP. Say exactly: {STOP_MESSAGE[self.lng]}", urgent=True)

    # --- tools --------------------------------------------------------------------------------

    async def start_repair(self, model: str | None, symptom: str | None, battery: str | None = None, device: str | None = None) -> dict[str, Any]:
        if self.stopped:
            return {"error": "session_stopped", "say": STOP_MESSAGE[self.lng]}
        refused = device_out_of_scope(device) or device_out_of_scope(model)
        if refused:
            self._event("scope.refused", device=refused)
            return {"refused": True, "reason": "device_out_of_scope", "say": REFUSE_DEVICE[self.lng]}
        if not (model or "").strip():
            # seen 4 Nov: the voice model dropped "demo phone" and got the general flow
            return {"ask": "phone_model", "hint": 'pass model "unknown" if the user does not know it'}
        self.pack, self.model_data = model_lookup(model)
        self.symptom = _symptom(symptom)
        self.ledger = Ledger(self.pack, self.symptom)
        self.state, self.fix_fault, self.fix_at, self.fix_check, self.remeasure, self.conflicts = "probing", None, None, None, None, 0
        if battery in ("removed", "connected"):
            self.battery = battery
        self._event("repair.started", pack=self.pack.pack_id, model_data=self.model_data, symptom=self.symptom, top=self._top(3))
        self._save()
        out: dict[str, Any] = {"pack": self.pack.pack_id, "model_data": self.model_data, "symptom": self.symptom, "top": self._top(3)}
        if self.battery != "removed":
            out["ask"] = "is_battery_removed"
        return out

    async def _timed(self, coro: Awaitable[EvidenceRecord | EvidenceError]) -> EvidenceRecord | EvidenceError:
        try:
            return await asyncio.wait_for(coro, timeout=self.vision_timeout_s)
        except asyncio.TimeoutError:
            return EvidenceError(error="vision_timeout", message="no reading within the time budget", retryable=True)

    def _camera(self, rec: EvidenceRecord | EvidenceError) -> None:
        """Track degraded mode: the camera is down until a camera read succeeds again."""
        down = isinstance(rec, EvidenceError) and rec.error == "camera_unavailable"
        if down and not self.degraded:
            self.degraded = "camera_unavailable"
            self._event("degraded", reason=self.degraded)
            self._note_soon("Camera unavailable: ask the user to read every value aloud. No power steps until it is back.")
            self._save()
        elif not isinstance(rec, EvidenceError) and self.degraded:
            self.degraded = None
            self._event("degraded", reason=None)
            self._note_soon("Camera is back.")
            self._save()

    def _read_aloud(self, measurement_id: str | None, err: str, **extra: Any) -> dict[str, Any]:
        self.awaiting_spoken = measurement_id
        self._event("reading.fallback", measurement=measurement_id, reason=err)
        return {"status" if err == "unclear" else "error": err, "point": measurement_id, "ask": "read_aloud", **extra}

    async def get_reading(self, kind: str, point_id: str | None = None) -> dict[str, Any]:
        if self.stopped:
            return {"error": "session_stopped", "say": STOP_MESSAGE[self.lng]}
        if kind == "supply":
            rec = await self._timed(self.bench.read_supply(self.session_id))
            self._camera(rec)
            if isinstance(rec, EvidenceError):
                return {"error": rec.error, "ask": "read_supply_aloud"}
            self._set_supply(rec)
            self._add_evidence(rec)
            assert rec.fields
            v, lim = rec.fields["set_voltage"], rec.fields["current_limit"]
            out: dict[str, Any] = {"kind": "supply", "volts": v.value, "limit_a": lim.value, "conf": round(rec.confidence, 2)}
            if self.pack:
                gate = check_power("connect_boot_cable", rec, self.pack, "removed")
                out["ok"] = gate.approved
                if gate.set_values:
                    out["set"] = gate.set_values
                elif not gate.approved:
                    out["reason"] = gate.reason
            return out

        if kind == "port_current":
            if err := self._require_repair():
                return err
            if not self._consume("connect_port_supply"):
                return {"error": "power_step_required", "step": "connect_port_supply"}
            rec = await self._timed(self.bench.read_port_current(self.session_id))
            self._camera(rec)
            if isinstance(rec, EvidenceError):
                return self._read_aloud("port_current", rec.error)
            m = self._measurement("port_current")
            rec.outcome = classify_band(m.bands or [], rec.value, False)
            snap = self._add_evidence(rec)
            return {"measurement": "port_current", "value": rec.value, "unit": "A", "class": rec.outcome,
                    "conf": round(rec.confidence, 2), **self._ledger_line(snap, rec)}

        # meter
        pid = point_id
        if pid is None and self.remeasure and self.pack:
            pid = self._measurement(self.remeasure).point_id
        if pid is None and self.state == "fix" and self.fix_check and self.pack:
            pid = self._measurement(self.fix_check).point_id
        if pid is None and self.pending_step and self.pending_step.point_id:
            pid = self.pending_step.point_id
        if pid and self.pack and pid not in {p.id for p in self.pack.points}:
            # not a pack point (the voice model made it up): read the point the engine asked for
            asked = self._target(None)
            if asked is None or not asked.point_id:
                return {"error": "unknown_point", "point": pid}
            pid = asked.point_id
        m = self.pack.measurement_for_point(pid) if (self.pack and pid) else None
        want = self.pack.point(pid).mode if (self.pack and pid) else None
        rec = await self._timed(self.bench.read_meter(self.session_id, pid, want))
        self._camera(rec)
        mid = m.id if m else None
        if isinstance(rec, EvidenceError):
            if rec.error == "mode_mismatch":
                return self._wrong_mode(rec, want)
            if rec.error == "unreadable" and rec.message in VIEW_PROBLEMS:
                self._event("reading.view", problem=rec.message, measurement=mid)
                return {"error": rec.message, "ask": VIEW_PROBLEMS[rec.message]["ask"], "say": VIEW_PROBLEMS[rec.message][self.lng]}
            return self._read_aloud(mid, rec.error)
        near = self._near_threshold(pid, rec)
        if rec.legible and rec.value is not None or rec.open:
            if rec.legible and (rec.needs_confirmation or near):
                return self._camera_readback(rec, m, pid, "reader_b_only" if rec.needs_confirmation else "near_threshold")
        if rec.confidence < CAMERA_MIN_CONFIDENCE or not rec.legible:
            self._store(rec, None)  # kept for the log, never for the ledger
            return self._read_aloud(mid, "unclear", conf=round(rec.confidence, 2))
        rec.measurement_id = mid
        if m and self.pack and pid:
            rec.outcome = classify_band(self.pack.point(pid).reference.bands, rec.value, rec.open)
        snap = self._add_evidence(rec)
        out = {"point": pid, "value": "OL" if rec.open else rec.value, "unit": "V" if rec.unit == "diode_V" else rec.unit,
               "mode": rec.mode, "conf": round(rec.confidence, 2)}
        if rec.outcome:
            out["class"] = rec.outcome
        return out | self._ledger_line(snap, rec)

    def _wrong_mode(self, err: EvidenceError, want: str | None) -> dict[str, Any]:
        """The meter's mode icon doesn't match the point's mode (BUILD_GUIDE corner case)."""
        try:
            on = json.loads(err.message).get("on")
        except ValueError:
            on = None
        words = {"diode": "diode mode", "dcv": "volts", "ohm": "ohms", "current": "amps"}
        say = {"en": f"Your meter is on {words.get(on, on or 'another mode')}; switch it to {words.get(want, want)}.",
               "hi": f"Meter abhi {words.get(on, on or 'dusre mode')} pe hai; {words.get(want, want)} pe karo."}[self.lng]
        self._event("reading.wrong_mode", on=on, want=want)
        return {"error": "wrong_mode", "meter_on": on, "set": want, "say": say}

    NEAR_THRESHOLD = 0.05  # within 5% of a band edge: a decimal or last-digit slip changes the class

    def _near_threshold(self, pid: str | None, rec: EvidenceRecord) -> bool:
        if not (self.pack and pid) or rec.open or rec.value is None:
            return False
        edges = {e for b in self.pack.point(pid).reference.bands for e in (b.lo, b.hi) if e}
        return any(abs(rec.value - e) <= self.NEAR_THRESHOLD * abs(e) for e in edges)

    def _camera_readback(self, rec: EvidenceRecord, m: Measurement | None, pid: str | None, why: str) -> dict[str, Any]:
        """A camera value that must be confirmed before it counts: read back from the local reader
        only (the user is the second reader, rule 3), or near a class boundary."""
        rec.measurement_id = m.id if m else None
        rec.needs_confirmation = True
        unit = {"diode_V": "volt", "V": "volt", "ohm": "ohm", "A": "amp"}.get(rec.unit or "", "")
        r = parse("OL" if rec.open else f"{rec.value} {unit}")
        say = readback(r) if r.ok else ("OL" if rec.open else str(rec.value))
        if pid and self.pack:
            say = f"{say}, {getattr(self.pack.point(pid).label, self.lng).split(',')[0]}"
        self.readback_pending = Readback(rec, rec.measurement_id, say)
        self._event("readback", measurement=rec.measurement_id, say=say, source="camera", why=why)
        return {"status": "readback", "say": say, "point": pid or rec.measurement_id, "conf": round(rec.confidence, 2), "ask": "confirm"}

    async def on_vision_level(self, level: str, reason: str | None) -> None:
        """The evidence server's degradation ladder changed (full → Reader B only → spoken only)."""
        self.vision_level = (level, reason)
        self._event("vision.level", level=level, reason=reason)
        await self.note(VISION_NOTE.get(level, VISION_NOTE["full"]))

    async def watch_boot_current(self, trigger: str = "on_connect") -> dict[str, Any]:
        if err := self._require_repair():
            return err
        waiting = self._target(None)
        if waiting is not None and waiting.id != "boot_pattern":
            # the approval is for another check (seen 4 Nov: the heat check's approval spent on a watch)
            return {"error": "not_this_step", "measurement": waiting.id, "use": USE_TOOL[waiting.kind]}
        step = "connect_boot_cable" if trigger == "on_connect" else "press_power_key"
        if not self._consume(step):
            return {"error": "power_step_required", "step": step}
        assert self.pack
        t = self.pack.boot.thresholds
        limit = (self.supply.fields or {}).get("current_limit") if self.supply else None
        limit_a = limit.value if limit and limit.value else self.pack.safe_power.current_limit_target
        dog = AtLimitWatchdog(limit_a=limit_a, fraction=t.at_limit_fraction, hold_s=t.at_limit_s)
        urgent = False

        self.live_watch = []

        async def on_sample(s: BootSample) -> bool:
            nonlocal urgent
            self.live_watch.append([s.t_s, s.current_a])
            self._event("watch.sample", t_s=s.t_s, current_a=s.current_a, limit_a=limit_a)
            if dog.feed(s.t_s, s.current_a):
                urgent = True
                self._event("urgent.disconnect", say=DISCONNECT_NOW[self.lng])
                return True
            return False

        self._watch = asyncio.ensure_future(self.bench.watch_boot_current(self.session_id, trigger, limit_a, on_sample))
        try:
            rec = await self._watch
        except asyncio.CancelledError:
            self._event("watch.cancelled")
            return {"status": "cancelled"}
        self._camera(rec)
        if isinstance(rec, EvidenceError):
            return {"error": rec.error, "ask": "describe_current"}
        assert rec.trace
        rec.outcome = "AT_LIMIT" if urgent else rec.trace.pattern
        snap = self._add_evidence(rec)
        meaning = next(p.meaning for p in self.pack.boot.patterns if p.id == rec.outcome)
        out: dict[str, Any] = {"pattern": rec.outcome, "peak_a": round(rec.trace.features.i_peak, 2) if rec.trace.features else None,
                               "meaning": getattr(meaning, self.lng)}
        if urgent:
            out = {"urgent": "disconnect_now", "say": DISCONNECT_NOW[self.lng], **out}
        return out | self._ledger_line(snap, rec)

    def _target(self, point_id: str | None) -> Measurement | None:
        """The measurement a spoken reading answers: a known point or measurement id, else the one
        the engine is waiting for. An id the pack doesn't know (the voice model made it up) falls
        back to that, and the read-back names the point, so the user can correct it."""
        if not self.pack:
            return None
        if point_id:
            if point_id in {m.id for m in self.pack.measurements}:
                return self._measurement(point_id)
            if point_id in {p.id for p in self.pack.points}:
                return self.pack.measurement_for_point(point_id)
        fix = self.fix_check if self.state == "fix" else None
        for mid in (self.awaiting_spoken, fix, self.remeasure, self.pending_step.measurement_id if self.pending_step else None):
            if mid:
                return self._measurement(mid)
        return None

    async def record_spoken_reading(self, text: str | None = None, point_id: str | None = None, confirm: bool = False) -> dict[str, Any]:
        if err := self._require_repair():
            return err
        if self.readback_pending and (confirm or _is_confirmation(text)):
            return self._confirm_readback()
        m = self._target(point_id)
        pending = self._target(None)
        if (m is not None and pending is not None and pending.id != m.id and _fits(pending, text)
                and (not _fits(m, text) or not _names(m, text))):
            # The engine chooses what is measured (rule 1): words that fit the waiting step answer it,
            # unless they name the other point. Seen 4 Nov: "0.02 volt aa raha hai" for the charging
            # IC input sent as vbus_port, and "capacitor garam" sent with the previous point's id.
            m = pending
        if m is None:
            return {"error": "which_point", "ask": "which_point"}
        if m.kind == "boot_pattern":
            return {"error": "use_watch", "ask": "watch_boot_current"}
        if m.kind == "observation":
            cls = classify_keywords(m, text or "")
            if cls is None:
                return {"status": "not_understood", "ask": "repeat", "options": m.classes}
            rec = EvidenceRecord(session_id=self.session_id, kind="observation", measurement_id=m.id, outcome=cls,
                                 source="spoken", confidence=SPOKEN_CONFIDENCE, needs_confirmation=True)
            say = OBSERVATION_WORDS.get(cls, {}).get(self.lng, cls)
        else:
            r: SpokenReading = parse(text or "")
            if not r.ok:
                return {"status": "not_understood", "ask": "repeat"}
            mode = self.pack.point(m.point_id).mode if (self.pack and m.point_id) else "current"
            lo, hi = PLAUSIBLE.get(mode, (0.0, 1e9))
            if r.value is not None and not lo <= r.value <= hi:
                return {"status": "check_value", "heard": readback(r), "ask": "repeat"}
            rec = EvidenceRecord(session_id=self.session_id, kind="spoken", measurement_id=m.id, point_id=m.point_id,
                                 mode=mode if mode in ("diode", "dcv", "ohm", "current") else None,  # type: ignore[arg-type]
                                 value=r.value, unit=r.base_unit, open=r.open, source="spoken",
                                 confidence=SPOKEN_CONFIDENCE, needs_confirmation=True)
            say = readback(r)
            if m.point_id and self.pack:  # name the point, so a wrong one is heard in the read-back
                say = f"{say}, {getattr(self.pack.point(m.point_id).label, self.lng).split(',')[0]}"
        self.readback_pending = Readback(rec, m.id, say)
        self._event("readback", measurement=m.id, say=say)
        return {"status": "readback", "say": say, "point": m.point_id or m.id, "conf": SPOKEN_CONFIDENCE, "ask": "confirm"}

    def _confirm_readback(self) -> dict[str, Any]:
        rb = self.readback_pending
        assert rb and self.pack
        self.readback_pending = None
        m = self._measurement(rb.measurement_id) if rb.measurement_id else None
        if m and m.requires_power and not self._consume(m.requires_power):
            return {"error": "power_step_required", "step": m.requires_power}
        rec = rb.record
        rec.confidence, rec.confirmed, rec.needs_confirmation = max(rec.confidence, CONFIRMED_CONFIDENCE), True, False
        if m and m.kind == "point" and m.point_id:
            rec.outcome = classify_band(self.pack.point(m.point_id).reference.bands, rec.value, rec.open)
        elif m and m.kind == "port_current":
            rec.outcome = classify_band(m.bands or [], rec.value, rec.open)
        if self.awaiting_spoken == rec.measurement_id:
            self.awaiting_spoken = None
        snap = self._add_evidence(rec)
        return {"recorded": rb.say, "class": rec.outcome, "conf": CONFIRMED_CONFIDENCE, **self._ledger_line(snap, rec)}

    async def next_step(self) -> dict[str, Any]:
        out = await self._next_step()
        self._save()
        return out

    async def _next_step(self) -> dict[str, Any]:
        if err := self._require_repair():
            return err
        assert self.ledger and self.pack
        if self.readback_pending and self.readback_pending.user_confirmed:
            # The user said "haan, sahi hai" but the voice model asked for the next step instead of
            # confirming (seen 4 Nov: a 15-turn loop). Their own words confirm it; record and go on.
            measurement = self.readback_pending.measurement_id
            done = self._confirm_readback()
            if "error" in done:
                return done
            self._event("readback.user_confirmed", measurement=measurement)
            nxt = await self._next_step()
            return {"recorded": done["recorded"], "class": done["class"]} | {k: v for k, v in nxt.items() if k != "reason"}
        if self.readback_pending:
            return {"step": "confirm_readback", "say": self.readback_pending.say, "use": "record_spoken_reading", "confirm": True}
        if self.state == "unresolved":
            return {"step": "unresolved", "top": self._top(2), "say": "evidence conflicts; record as unresolved"}
        if self.remeasure:
            self.pending_step = None
            reason = "an earlier reading looks wrong; measure it again" if self.lng == "en" else "pehle wali reading galat lag rahi hai; dobara measure karo"
            return await self._step_out(self.remeasure, "remeasure", reason)
        if self.ledger.stop():
            fid, p = self.ledger.ranked()[0]
            if self.fix_fault != fid:
                self.fix_fault, self.fix_at, self.state = fid, len(self.evidence), "fix"
                self.fix_check = self._choose_fix_check(fid)
                self._event("fix.proposed", fault=fid, p=round(p, 2), check=self.fix_check)
            fault = next(f for f in self.pack.faults if f.id == fid)
            return {"step": "fix", "fault": fid, "name": self._fault_name(fid), "p": round(p, 2),
                    "fix": getattr(fault.fix, self.lng), "then_check": self.fix_check}
        exclude = {m.id for m in self.pack.measurements if m.requires_power} if self.degraded else None
        step = choose_next_step(self.ledger, exclude)
        if step is None:
            self.state = "unresolved"
            out: dict[str, Any] = {"step": "unresolved", "top": self._top(2)}
            return out | ({"degraded": self.degraded} if self.degraded else {})
        self.pending_step = step
        out = await self._step_out(step.measurement_id, "measure", step.reason(self.ledger, self.lng))
        return out | ({"degraded": self.degraded} if self.degraded else {})

    async def _step_out(self, mid: str, kind: str, reason: str) -> dict[str, Any]:
        """The step line; for a step that needs power, with the safety gate's answer already in it.
        The voice model then has the gate's exact sentence (or the block and what to set) instead
        of a step name it might turn into its own power instruction (seen 4 Nov: "boot cable
        connect karo" with no approval). The approval is the gate's, as for request_power_step."""
        out = self._step_line(mid, kind, reason)
        if out["step"] != "power":
            return out
        gate = await self._gate(out["power_step"])
        del out["reason"], out["set"]  # explain("why") still gives the reason; the gate says what to set
        out |= {"approved": gate.approved, "say": gate.say}
        if gate.approved and self.approvals.pending:
            out["approval"] = self.approvals.pending.id
        if gate.reason:
            out["blocked"] = gate.reason
        if gate.set_values:
            out["set"] = gate.set_values
        return out

    def _choose_fix_check(self, fault_id: str) -> str:
        """The abnormal reading that did most to point at the fault; after the fix it must read
        healthy. Falls back to the fault's first abnormal prediction."""
        assert self.ledger and self.pack
        fault = next(f for f in self.pack.faults if f.id == fault_id)
        active = {o.evidence_id for o in self.ledger.active}
        support = [
            s for s in self.ledger.history
            if s.observation.evidence_id in active
            and fault.predicts.get(s.observation.measurement_id) == s.observation.outcome
            and s.observation.outcome != self._measurement(s.observation.measurement_id).healthy
        ]
        best = max(support, key=lambda s: (s.influence, -s.index), default=None)
        if best:
            return best.observation.measurement_id
        return next((mid for mid, cls in fault.predicts.items() if cls != self._measurement(mid).healthy), "boot_pattern")

    def _step_line(self, mid: str, kind: str, reason: str) -> dict[str, Any]:
        assert self.pack
        m = self._measurement(mid)
        out: dict[str, Any] = {"step": kind, "measurement": mid, "reason": reason}
        if m.kind != "point":
            out["use"] = USE_TOOL[m.kind]
        if m.point_id:
            p = self.pack.point(m.point_id)
            out |= {"point": p.id, "mode": p.mode, "where": getattr(p.label, self.lng), "probes": getattr(p.probe_note, self.lng)}
        elif m.kind == "observation":
            out |= {"ask": getattr(m.label, self.lng), "options": m.classes}
        if m.requires_power:
            sp = self.pack.safe_power
            out |= {"step": "power", "then": kind, "power_step": m.requires_power, "set": {"volts": sp.voltage_target, "limit_a": sp.current_limit_target}}
        self._event("step.proposed", **{k: v for k, v in out.items() if k in ("step", "measurement", "point", "power_step")})
        return out

    async def request_power_step(self, step: str) -> dict[str, Any]:
        if self.pack is None:
            return {"error": "start_repair_first"}
        res = await self._gate(step)
        out = res.line()
        if res.approved and self.approvals.pending:
            out["approval"] = self.approvals.pending.id  # the page's guardrail ties endings to it
        return out

    async def _gate(self, step: str) -> GateResult:
        """The safety gate (CLAUDE.md rule 2), with a fresh supply read if needed. An approval is
        bound to this step and the supply settings checked; its lifetime is in approval.py."""
        assert self.pack
        await self._fresh_supply(step)
        res = check_power(step, self.supply, self.pack, self.battery, self.stopped)  # type: ignore[arg-type]
        self.last_gate = res.line() | {"t_ms": round(time.time() * 1000)}
        if res.approved:
            approval, _ = self.approvals.grant(step, settings_of(self.supply))
            self._power_events()  # the one it replaced
            self._event("power.approved", **approval.line(self.approvals.ttl_s), say=res.say)
            self._expire_later()
        else:
            self._event("power.blocked", step=step, reason=res.reason)
        return res

    async def _fresh_supply(self, step: str) -> None:
        """The gate needs a camera supply reading younger than 20 s. If there is none, or it would
        go stale before the user acts, read the supply now instead of making the voice model ask
        for it. The gate itself is unchanged: it still checks the reading's age and confidence."""
        if self.stopped or self.battery != "removed" or step not in APPROVED:
            return
        if self.supply is not None and self.supply.age_s() < STALE_S - SUPPLY_REFRESH_MARGIN_S:
            return
        rec = await self._timed(self.bench.read_supply(self.session_id))
        self._camera(rec)
        if isinstance(rec, EvidenceRecord):
            self._set_supply(rec)
            self._add_evidence(rec)

    async def explain(self, topic: str, level: str | None = None) -> dict[str, Any]:
        lvl = level if level in ("trainee", "expert") else self.level
        pack = self.pack or model_lookup(None)[0]
        t = topic.lower().strip().replace(" ", "_")
        text: tuple[str, str] | None = None  # (en, hi)
        for f in pack.faults:
            if t in (f.id, f.name.en.lower().replace(" ", "_")) or f.id in t:
                e = f.explain[lvl]  # type: ignore[index]
                text = (e.en, e.hi)
        for key, entry in pack.glossary.items():
            if text is None and (key == t or key in t):
                text = (entry.en, entry.hi)
        for p in pack.boot.patterns:
            if text is None and p.id.lower() == t:
                text = (p.meaning.en, p.meaning.hi)
        if text is None and t in ("why", "reason", "next", "why_this_step") and self.pending_step and self.ledger:
            text = (self.pending_step.reason(self.ledger, "en"), self.pending_step.reason(self.ledger, "hi"))
        if text is None:
            return {"text": None, "ask": "which_topic"}
        # Both languages: the model answers in the latest turn's language, which can differ from
        # the session's (seen 4 Nov: English questions answered in Hinglish from a Hindi-only text).
        return {"text": {"en": text[0][:150], "hi": text[1][:150]}}

    async def close_repair(self, result: str, fault_id: str | None = None, note: str | None = None) -> dict[str, Any]:
        if self.ledger is None or self.pack is None:
            return {"error": "start_repair_first"}
        if result == "fixed":
            after = self.evidence[self.fix_at:] if self.fix_at is not None else []
            check = self.fix_check or "boot_pattern"
            if not any(e.measurement_id == check and self._healthy(e) for e in after):
                m = self._measurement(check)
                out: dict[str, Any] = {"status": "confirm_first", "measure": check, "ask": "confirming_reading"}
                if m.point_id:
                    out["point"] = m.point_id
                if m.requires_power:
                    out["power_step"] = m.requires_power
                return out
        fid = fault_id or self.fix_fault or self.ledger.leader()[0]
        record = {
            "id": new_id("rr"), "session_id": self.session_id, "pack_id": self.pack.pack_id, "symptom": self.symptom,
            "outcome": result, "fault_id": fid, "posterior": self.ledger.posterior,
            "evidence": [e.id for e in self.evidence], "note": (note or "")[:500],
        }
        self.state = "closed"
        self._persist("repair_records", record)
        self._save()
        self._event("repair.closed", outcome=result, fault=fid, record=record["id"])
        return {"record": record["id"], "outcome": result, "fault": fid}

    # --- the companion screen ------------------------------------------------------------------

    def screen_state(self) -> dict[str, Any]:
        """Everything the companion screen shows, in one snapshot (pushed after every change, so a
        reload or a replay always shows the whole picture)."""
        pack = self.pack
        loc = lambda x: {"en": x.en, "hi": x.hi} if x is not None else None  # noqa: E731
        out: dict[str, Any] = {
            "session_id": self.session_id, "state": self.state, "symptom": self.symptom, "lang": self.lng,
            "lang_decided": self.lang.decided, "battery": self.battery, "stopped": self.stopped, "degraded": self.degraded,
            "vision": {"level": self.vision_level[0], "reason": self.vision_level[1]},
            "pack": {"id": pack.pack_id, "name": loc(pack.name), "model_data": self.model_data, "placeholder": pack.placeholder} if pack else None,
            "readback": self.readback_pending.say if self.readback_pending else None,
        }
        # faults, ranked, each with the readings that moved it most
        faults: list[dict[str, Any]] = []
        if self.ledger and pack:
            moved: dict[str, list[dict[str, Any]]] = {}
            active = {o.evidence_id for o in self.ledger.active}
            for snap in self.ledger.history:
                ob = snap.observation
                if ob.evidence_id not in active:
                    continue
                for h, d in snap.deltas.items():
                    if abs(d) >= 0.03:
                        moved.setdefault(h, []).append({"evidence": ob.evidence_id, "measurement": ob.measurement_id,
                                                        "outcome": ob.outcome, "delta": round(d, 3)})
            names = {f.id: loc(f.name) for f in pack.faults}
            for h, prob in self.ledger.ranked():
                faults.append({"id": h, "name": names.get(h) or {"en": "unknown fault", "hi": "koi aur fault"}, "p": round(prob, 3),
                               "moved": sorted(moved.get(h, []), key=lambda m: -abs(m["delta"]))[:3]})
        out["faults"] = faults
        # the next step, with where to probe on the reference board
        step = None
        if self.pending_step and pack and self.state == "probing":
            m = self._measurement(self.pending_step.measurement_id)
            step = {"measurement": m.id, "kind": m.kind, "label": loc(m.label), "power_step": m.requires_power}
            if m.point_id:
                pt = pack.point(m.point_id)
                step |= {"point": pt.id, "point_label": loc(pt.label), "mode": pt.mode, "probes": loc(pt.probe_note),
                         "side": pt.side, "xy": list(pt.xy) if pt.xy else None}
        out["step"] = step
        out["fix"] = {"fault": self.fix_fault, "check": self.fix_check} if self.state == "fix" else None
        a = self.approvals.pending
        out["approval"] = (a.line(self.approvals.ttl_s) | {"speakable": self.approvals.speakable() is a,
                                                          "say": (self.last_gate or {}).get("say")}) if a else None
        out["gate"] = self.last_gate
        if pack:
            sp = pack.safe_power
            out["safe_power"] = {"volts": sp.voltage_target, "limit_a": sp.current_limit_target, "v_min": sp.voltage_min,
                                 "v_max": sp.voltage_max, "limit_max": sp.current_limit_max}
        # readings
        sup = next((e for e in reversed(self.evidence) if e.kind == "supply"), None)
        out["supply"] = ({"fields": {k: {"value": v.value, "unit": v.unit, "conf": v.confidence} for k, v in (sup.fields or {}).items()},
                          "source": sup.source, "conf": sup.confidence, "legible": sup.legible, "t_ms": round(sup.created_at.timestamp() * 1000),
                          "simulated": sup.simulated} if sup else None)
        met = next((e for e in reversed(self.evidence) if e.kind in ("meter", "spoken") and e.point_id), None)
        out["meter"] = self._evidence_line(met) if met else None
        boot = next((e for e in reversed(self.evidence) if e.kind == "boot_trace" and e.trace), None)
        watching = bool(self._watch and not self._watch.done())
        if watching or boot:
            samples = self.live_watch if watching else [[s.t_s, s.current_a] for s in boot.trace.samples]  # type: ignore[union-attr]
            limit = boot.trace.limit_a if (boot and not watching) else (pack.safe_power.current_limit_target if pack else 1.0)
            out["boot"] = {"samples": samples, "pattern": None if watching else boot.outcome, "limit_a": limit, "live": watching}  # type: ignore[union-attr]
        else:
            out["boot"] = None
        out["evidence"] = [self._evidence_line(e) for e in self.evidence[-40:]]
        return out

    def _evidence_line(self, e: EvidenceRecord) -> dict[str, Any]:
        label = None
        if self.pack and e.measurement_id:
            try:
                m = self._measurement(e.measurement_id)
                label = {"en": m.label.en, "hi": m.label.hi}
            except (KeyError, StopIteration):
                label = None
        return {"id": e.id, "kind": e.kind, "measurement": e.measurement_id, "point": e.point_id, "label": label,
                "value": e.value, "unit": e.unit, "open": e.open, "mode": e.mode, "outcome": e.outcome,
                "conf": round(e.confidence, 2), "source": e.source, "confirmed": e.confirmed, "simulated": e.simulated,
                "legible": e.legible, "frame": f"/screen/evidence/{self.session_id}/{e.id}.jpg" if e.frame_paths else None,
                "pattern": e.trace.pattern if e.trace else None, "t_ms": round(e.created_at.timestamp() * 1000)}

    # --- for the dev endpoint and tests -------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "state": self.state,
            "pack": self.pack.pack_id if self.pack else None,
            "symptom": self.symptom,
            "battery": self.battery,
            "stopped": self.stopped,
            "lang": self.lng,
            "top": self._top(3) if self.ledger else [],
            "pending": self.pending_step.measurement_id if self.pending_step else None,
            "pending_kind": self.pending_step.kind if self.pending_step else None,
            "pending_power": self.pending_step.requires_power if self.pending_step else None,
            "remeasure": self.remeasure,
            "awaiting_spoken": self.awaiting_spoken,
            "readback": self.readback_pending.say if self.readback_pending else None,
            "approval": self.approvals.pending.step if self.approvals.pending else None,
            "fix": self.fix_fault,
            "fix_check": self.fix_check,
            "degraded": self.degraded,
            "conflicts": self.conflicts,
            "evidence": len(self.evidence),
            "measured": sorted(self.ledger.measured()) if self.ledger else [],
        }
