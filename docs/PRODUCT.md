# Benchmate: Product Document

**Voice-guided diagnosis for mobile phone board repair, in Hindi and English**

| | |
|---|---|
| Owner | Nikhil Pande |
| Status | Stage 1 (hackathon prototype) in build |
| Entry | AgentVerse '26, PS-03 Voice AI & Conversational Agents |
| Key dates | Prototype gate 10 Nov 2026 · Shop field test 17–21 Nov · Submit 28 Nov · Code freeze 30 Nov 2026, 00:08 IST |
| Companion doc | `Benchmate_Technical_Architecture.md` |
| Last updated | 3 Nov 2026 |

---

## 1. Summary

Benchmate is a voice partner for mobile phone board repair. It reads the shop's own bench power supply and multimeter through a camera, reasons from those readings to the most likely fault, and tells the technician, in Hindi or English, what to measure next, while both hands stay on the board.

It does three things that no tool found in our competitor scan does together:

1. **Closed-loop reasoning on real readings.** A ledger of possible faults is re-ranked after every reading. The next step is the measurement that best separates the faults still standing.
2. **Readings from cheap shop equipment.** A camera reads the supply's current and the meter's display; a spoken number also works. Every reading carries its source and confidence.
3. **Hands-free guidance in Hindi.** Board repair means probes or tweezers in both hands and eyes in a microscope. Voice is the only free channel.

Stage 1 targets **dead phones and phones that will not charge**, on **one popular Android model**, for **trainees at repair institutes and technicians in small shops**.

---

## 2. Problem

### 2.1 The diagnosis loop
Diagnosing a dead or non-charging phone board is a measurement loop:

1. Power the board from a bench supply through a boot cable, battery removed.
2. Read the current the board draws, with and without pressing power.
3. Interpret the pattern (no current, high current at once, current that rises and drops, normal start-up).
4. Take diode-mode readings on the battery line and key power rails; compare with a known-good board.
5. Narrow down to the faulty part; fix; confirm with a new reading.

Experienced technicians run this loop from memory. Trainees and newer technicians stall at step 3 and step 4: they can take a reading but do not know what it means or what to check next.

### 2.2 Why existing help falls short
- **Schematic and board-layout tools** (Borneo, ZXW) show where parts are and how they connect, but do not say what to measure next.
- **AI diagnosis tools** (BoardScope, Wrench Board) need typed symptoms and hand-entered readings, in English, aimed at experienced microsoldering technicians.
- **Training** is mostly on the job, in institutes or from YouTube videos; there is no one at the trainee's elbow when they are stuck.
- **Typing is the wrong interface**: hands hold probes, eyes are on the microscope.

### 2.3 Why now
- Real-time speech models now handle low-latency conversation, interruptions and tool calls.
- Vision models read seven-segment and LCD displays well enough to replace a computer port on cheap instruments.
- India's repair trade is large, growing and short of trained technicians.

---

## 3. Users

### 3.1 Primary personas

**Rohit, mobile repair trainee (primary)**
- 19–24, enrolled in a 2–6 month mobile repair course at a private institute.
- Can open a phone, use a multimeter and bench supply; unsure what readings mean.
- Learns mostly in Hindi; watches Hindi repair videos.
- Needs: "What do I check next, and why?" without waiting for the instructor.

**Sameer, small-shop technician (primary)**
- Runs or works in a 1–3 person shop; mostly screen, battery and charging-port jobs, some board work.
- Comfortable with common models; slows down on unfamiliar ones.
- Needs: a faster path through board faults so jobs are not turned away or sent to a bigger shop.

### 3.2 Secondary personas
- **Senior technician (author):** knows the common faults of popular models; can turn that knowledge into model packs (Stage 3).
- **Institute owner (buyer, Stage 2+):** many trainees, few instructors; wants guided practice and assessment records.

### 3.3 Non-users in Stage 1
- iPhone microsoldering specialists (deep niche, strong existing tools).
- Anyone repairing mains-powered devices (chargers, TVs, inverters) or EV batteries: out of scope for safety.

---

## 4. Market and positioning

### 4.1 Why phone repair in India
| Segment | Size in India | Safe for guided probing? | Need for hands-free voice | Decision |
|---|---|---|---|---|
| Mobile phone board repair | Largest repair trade; one report counts 266,182 repair shops (Sep 2025); estimates vary widely | Yes: 3.7–4.4 V battery, run from a current-limited supply | Very high | **Target** |
| EV / e-rickshaw controllers and batteries | Fast-growing; many courses | No: 48–72 V, high current | High | Later, with dedicated safety design |
| TVs, inverters, LED drivers, chargers | Large | No: mains | Medium | Excluded |
| ITI labs (Arduino and similar) | Small, slow buyers | Yes | Medium | Fallback and training add-on |

### 4.2 Competitive landscape
| Group | Examples | What they do | What they lack |
|---|---|---|---|
| Schematic and board-layout tools | Borneo (Android breadth), ZXW (iPhone depth, about ₹6,000–12,000 a year) | Schematics, board maps, component values | No reasoning about the next step; screen and mouse only |
| AI board-diagnosis tools | BoardScope (AI in Pro), Wrench Board | Typed symptoms and readings, AI suggests causes | Typed, English, readings entered by hand, built for experts |
| Instrument makers' AI connectors | R&S MXO AI Connect, Keysight MCP Server, PicoMCP | Natural-language control of costly connected instruments | Not used in phone shops; no diagnosis |
| Voice copilots in other trades | ONRAMP (automotive) | Hands-free guidance for car technicians | Not electronics; manuals, not measurements |

### 4.3 Positioning
- **Sits beside** the schematic tool a technician already uses; does not replace it.
- **Lasting asset:** the model knowledge pack, normal readings and known faults for each model, measured on a working reference board and improved by every recorded repair. Built from measurements, never from redistributed schematics.
- **Pitch line:** *"Schematic tools show you the board. Benchmate tells you, in Hindi, what to measure next, and reads the answer off your own power supply."*

---

## 5. Product principles

1. **Evidence before answers.** Every claim traces to a reading or a pack entry. The agent says what it does not know.
2. **One instruction at a time.** Name the meter mode, both probes and the point. Wait for "ready".
3. **Safety is code, not prompt.** Power steps are blocked until the camera confirms safe supply settings.
4. **Speak like a senior colleague.** Short, numeric, in the user's language; quiet unless there is something to say.
5. **Show, don't make them read.** The screen holds detail; the voice holds the next step.
6. **Teach while fixing.** "Why?" always gets a short, honest answer.

---

## 6. Stage 1 scope

### 6.1 Feature list (MoSCoW)

| # | Feature | Priority |
|---|---|---|
| F1 | Real-time voice conversation in Hindi, English and mixed speech | Must |
| F2 | Barge-in: interruption stops speech and cancels running reads | Must |
| F3 | Camera reading of the bench supply (set voltage, current limit, live current) | Must |
| F4 | Camera reading of the multimeter (value, unit, mode) | Must |
| F5 | Spoken readings as a fallback evidence source | Must |
| F6 | Boot-current watch and pattern classification | Must |
| F7 | General dead-phone flow (any model) | Must |
| F8 | One model pack with reference-board readings and known faults | Must |
| F9 | Fault ledger with next-best-step reasoning | Must |
| F10 | Safe-power check before every power instruction | Must |
| F11 | Companion screen: board view, supply/meter readings, ledger, evidence log, transcript | Must |
| F12 | Repair record with export (customer report / trainee assessment) | Must |
| F13 | Mentor mode: "why" explanations at trainee or expert depth | Should |
| F14 | Microscope view with point markers | Should |
| F15 | Probe-position check on the microscope view | Could (best effort) |
| F16 | Recall of past repairs | Should |
| F17 | Replay mode for demos | Should |
| F18 | Fault simulator for repeatable current patterns (labelled) | Could |
| F19 | Marathi | Won't (Stage 3) |
| F20 | Soldering / hot-air guidance | Won't |

### 6.2 Fault families in Stage 1
1. **Dead, high current at once** (short on the battery line).
2. **Dead, no current** (open path: connector, fuse/coil, power key line).
3. **Not charging** (charging port, charging-path parts).

---

## 7. User stories and acceptance criteria

| # | Story | Acceptance criteria |
|---|---|---|
| US1 | *Start:* "Redmi, dead, no vibration." | Model pack loaded or general flow announced; at most two clarifying questions; three ranked faults spoken and shown |
| US2 | *Safe power-up:* "Set 4 volts, limit 1 amp." | Camera reads both values; "connect" instruction only after both pass; mismatch produces a specific correction |
| US3 | *Current reading:* "Connected." | Current read within 3 s; pattern named with meaning ("0.8 A at once, short on the battery line") |
| US4 | *Guided probe* | Instruction names meter mode, both probes, point; marker shown; reading compared with the reference board; normal/abnormal stated with margin |
| US5 | *Spoken reading:* "Point zero zero two." | Parsed with units; read back; marked lower confidence |
| US6 | *Next best step:* "Ab kya check karun?" | One measurement with a one-line reason, in the language asked |
| US7 | *Mentor mode:* "Why that capacitor?" | Short explanation; depth matches the user's level setting |
| US8 | *Interrupt:* "Ruko, pehle charging check karo." | Speech stops within 0.5 s; switches task; offers to resume |
| US9 | *Recall:* "Last month's Redmi with the same current?" | Answer cites date and model from records, or says none found |
| US10 | *Close-out:* "Capacitor removed, short gone." | Asks for a confirming reading; saves the record; export available |

---

## 8. Key flows

### 8.1 Dead phone, short on the battery line (happy path)
1. User names model and symptom → pack loads, ledger shows priors.
2. Agent: set supply voltage and current limit → camera confirms → "connect".
3. Camera watches current: high at once without pressing power → "short on battery line" rises to top.
4. Agent guides diode-mode check at the battery-line capacitor → reading near zero vs reference → confirmed.
5. Agent guides two more points (or narrates the technician's rosin/heat check) → one part isolated.
6. Technician removes the part → agent asks for a new power-up through the safety check → normal pattern → record saved.

### 8.2 Dead phone, no current
1. Power-up check → no current on connect or on power press.
2. Agent checks the boot cable and supply first (common false lead), then guides diode checks on the battery connector path and power-key line.
3. Narrows to an open path; records "needs technician fix" if repair is beyond scope.

### 8.3 Not charging
1. Agent asks for current draw while charging through the port vs through the boot cable.
2. Guides diode checks on charging-port pins and the charging path.
3. Separates "port damaged" from "charging-path part" from "board fine, cable/charger issue".

### 8.4 Unclear or conflicting evidence
- Low-confidence reading → agent asks to re-read or say the number.
- Two readings disagree with every known fault → agent says so, suggests re-measuring the earlier point, and records "unknown fault" if the conflict stays.

---

## 9. Voice and interaction design

### 9.1 Speaking style
- Lead with the answer, then one line of meaning: "0.8 amps. That's a short."
- Numbers always with units, rounded to what the display shows.
- Never read long logs aloud; point to the screen.
- Under about 15 seconds of speech unless asked to explain.

### 9.2 Language
- Replies in the language spoken to; handles Hindi-English mixing.
- Trade terms stay in English inside Hindi sentences (short, diode mode, boot current, milliamp), as technicians speak.

### 9.3 Turn-taking
- Always-listening with voice detection by default; push-to-talk (key or foot pedal) for noisy shops.
- Any read taking over a second gets a spoken holding line: "Reading the supply…"
- Long watches ("watch the current while I press power") run in the background; the agent speaks on an event or at the end.

### 9.4 Interruptions
- Any user speech stops the agent's speech.
- "Stop", "wait", "ruko" also cancel a running read or watch.
- After an interruption the agent answers the new request and offers the old one.

### 9.5 Guiding hands
- One instruction per step: meter mode, both probes, the point.
- The point is marked on the microscope view and the reference photo.
- The agent waits for "ready" (or a confirmed probe position) before reading.
- Power instructions always follow the safe order: set voltage → set current limit → camera check → connect.

### 9.6 Ambiguity
- One plausible referent → act and state the assumption.
- Two or more → one short question with the options.
- Beyond the equipment's resolution → say so plainly.

---

## 10. Companion screen

**Purpose:** voice is the input; the screen shows where to probe and why the agent believes what it says. Read at a glance from about 1 metre; not operated during a diagnosis.

**Layout (five zones)**
1. **Board view** (largest): live microscope feed beside the reference board photo, next point marked on both.
2. **Supply and meter:** latest camera reading of each display with confidence; live boot-current graph.
3. **Fault ledger:** ranked bars that move after each reading, each with the evidence that moved it.
4. **Evidence log:** every reading with source (Camera / Spoken), confidence, frame thumbnail.
5. **Conversation:** live transcript, language tag, interruptions marked.

**Rules**
- Large values, high contrast, no dense tables in the board view.
- Pending power steps show a full-width banner with the values the camera must confirm.
- Every reading opens its source frame in one click.
- Light and dark themes; status colours always paired with a word.
- After close-out the screen becomes the repair record, exportable as PDF.

---

## 11. Knowledge packs (product view)

### 11.1 General dead-phone flow
Boot-current patterns and their usual meaning, with the standard next checks. Used when no model pack exists; the agent says it is working without model data.

### 11.2 Model pack
- Top and bottom board photos with key points marked.
- Normal readings measured on a working reference phone (boot-current pattern, diode readings, rail voltages).
- Known faults with the readings each predicts.
- Sources: own reference measurements, field-test observations, public repair knowledge. **Never** redistributed schematics.

### 11.3 Growth path
- Stage 1: one model, hand-written.
- Stage 2: top 10 most-repaired models, with partner technicians.
- Stage 3: authoring tool for senior technicians; closed repairs update fault frequencies.

---

## 12. Safety and trust

| Instruction class | Rule |
|---|---|
| Measure or explain | Immediate |
| Power the board | Only after the camera reads the supply at the model's safe voltage (about 3.8–4.2 V) with a current limit set (about 1–2 A) |
| Out of scope | Battery connected while probing; swollen, punctured or hot battery; chargers and anything on mains → refused with the reason |

- **Battery always disconnected**; the board runs from the bench supply.
- **Current limit as protection:** if the supply sits at its limit for several seconds, the agent tells the user to disconnect.
- **Mishearing defence:** power steps are read back with values; mismatched replies are ignored.
- **No soldering guidance** in Stage 1.
- **No customer data** in Stage 1: only bought and scrap boards.
- **Honesty:** every reading shows its confidence; the README claims file marks each capability Proven, Stubbed or Assumed.

---

## 13. Success metrics

### 13.1 Stage 1 (hackathon)
| Metric | Target |
|---|---|
| End of speech → first spoken word | ≤ 1.0 s typical |
| Camera reading → spoken value | ≤ 3.0 s |
| Display-reading accuracy | ≥ 98% on own test set; ≥ 95% on shop equipment |
| Boot-current pattern classification | ≥ 9 of 10 recorded power-ups |
| Intent accuracy (Hindi/English/mixed) | ≥ 90% |
| Own dead boards reaching the right fault | 3 of 3, confirmed by fix or technician |
| Agreement with technician's next step (field test) | ≥ 70% of steps |
| Unsafe power instructions given | 0 |

### 13.2 Stage 2 (pilot)
- Time to fault vs unaided, for trainees and technicians.
- Share of sessions ending in a correct diagnosis.
- Weekly active benches per partner.
- Partner willingness to continue or pay.

---

## 14. Field test plan (17–21 Nov)

The prototype is built and proven alone; the shop visit validates it. Nothing in the build depends on it.

**Outreach message (send to 4–5 shops or institutes this week)**
> Namaste, main Pune ka ek software developer hoon. Main mobile board repair ke liye ek AI voice assistant bana raha hoon jo technician ko Hindi mein next measurement batata hai. 17–21 Nov ke beech kya aap 1 ghanta de sakte hain? Main sirf aapke scrap boards par test karunga, customer phones par nahi. Aapka naam credit mein dalunga aur tool free mein milega.

**Protocol (60–90 minutes)**
1. Baseline (20 min): technician diagnoses 1–2 scrap boards normally; note steps and time.
2. With Benchmate (20 min): a different scrap board with guidance; note agreement at each step.
3. Trainee (15 min, if available).
4. Five questions: next step right? Hindi natural? Camera correct on your displays? What was missing? Would you use or pay for it?
5. Footage only with consent; board and hands only.

**Bring:** laptop, headset, overhead camera, mount, ring light. Use the shop's own supply and meter.

**If no shop agrees:** test with ITI or polytechnic students on own boards; share recordings with a repair community; report it as a student test.

---

## 15. Roadmap

| Stage | When | Contents | Gate to next stage |
|---|---|---|---|
| 0 · Research | 30 Oct – 3 Nov 2026 | Event research, idea comparison, competitor scan, market choice | Done |
| 1 · Hackathon MVP | 3 – 28 Nov 2026 | One model, three dead boards, camera-read evidence, Hindi, field test | **A:** three boards diagnosed, latency and reading targets met, field findings logged |
| 2 · Field pilot | Dec 2026 – Jan 2027 | 2 repair institutes, 3 shops in Pune, top-10 model packs | **B:** users reach the fault faster than unaided; partners want to continue |
| 3 · Pack platform | Feb – Apr 2027 | Pack authoring tool, phone-only mode, Marathi | **C:** at least one paying pilot |
| 4 · Scale | From mid-2027 | Institute licences, repair-shop chains, links with schematic tools | — |

### Decision log (Stage 0)
| Date | Decision |
|---|---|
| 3 Nov 2026 | Build alone first, field-test 17–21 Nov; shop access not guaranteed |
| 3 Nov 2026 | Market: mobile phone board repair; EV and mains repair excluded for safety |
| 3 Nov 2026 | PCB options evaluated (Uno clones; custom fault board at about ₹600–800 for 5) |
| 3 Nov 2026 | Repositioned from "voice control for a bench" to closed-loop guided diagnosis |
| 3 Nov 2026 | PS-03 chosen over PS-01 |
| 1 Nov 2026 | Hardware direction chosen after comparing software ideas |

---

## 16. Business model (hypotheses to test in Stage 2)

These are hypotheses, not validated numbers.

| Buyer | Offer | Pricing hypothesis to test |
|---|---|---|
| Repair institutes | Per-bench licence with trainee assessment records | Annual per-bench licence, benchmarked against schematic-tool subscriptions |
| Small shops | Monthly subscription per technician | Low monthly price; free tier for one model |
| Senior technicians | Revenue share on packs they author | Share of pack revenue (Stage 3) |
| Schematic-tool vendors | Integration or white-label | Partnership (Stage 4) |

---

## 17. Risks and open questions

| Risk | Mitigation | Fallback |
|---|---|---|
| Shallow phone-repair knowledge | Reference-board measurements; three fault families only | Field corrections; claims file marks gaps |
| No shop agrees to test | Outreach to 4–5 places | Student test |
| Bought boards have unclear faults | Buy 3; check each first | Labelled simulator for one pattern |
| Camera misreads displays | Ring light, fixed mount, crop, confidence threshold | Spoken reading |
| Probe location unreliable at phone scale | Markers; "say ready" | Spoken confirmation only |
| Voice latency | Holding lines; short reads | Push-to-talk |
| Unsafe power-up | Supply check before every power step | Stop session |
| Schematic licensing | Packs from own measurements only | Point to licensed tools |
| Competitors add voice | Hindi, trainees, camera-read shop equipment, records | Partner with schematic tools |

**Open questions**
- [ ] Which phone model: the one shops see most, or the one with most public repair material?
- [ ] Does judging need a live demo or accept a video?
- [ ] Is a public repository required, and under whose organisation?
- [ ] How accurately does the vision model read the chosen supply and meter?
- [ ] Can the team pick be switched to PS-03, and does it lock?
- [ ] Which shop or institute replies first?

---

## 18. Glossary

| Term | Meaning |
|---|---|
| Boot cable | Cable that feeds the bench supply into the phone's battery connector |
| Boot current | Current the board draws from the supply when connected and when power is pressed |
| Battery line (VBAT) | The main supply line from the battery connector into the board |
| Diode mode | Multimeter mode that measures the voltage drop across a junction; used to find shorts and opens |
| Reference board | A working board of the same model used to record normal readings |
| Model pack | Benchmate's data for one model: board photos, marked points, normal readings, known faults |
| Fault ledger | The ranked list of possible faults, updated after each reading |
| Evidence record | One reading with value, units, point, mode, source, confidence and frame |

---

## Sources
- Verified Market Reports, smartphone repair market: https://www.verifiedmarketreports.com/product/smartphone-repair-market/
- Boot-loader, phone schematics and diagnostic tools guide: https://boot-loader.com/schematics-diagnostics/
- iTweak, boardview software and AI: https://www.itweak.in/blog/boardview-software-zxw-wuxinji-openboardview-and-ai
- GSM Guru, Borneo Schematics 2026 update: https://gsmguru.in/borneo-schematics-update-2026/
- BoardScope: https://www.boardscope.ca/
- Rohde & Schwarz MXO AI Connect: https://www.electronicsmedia.info/2026/11/02/rohde-schwarz-mxo-ai/
- Keysight MCP Server for Instrument Control: https://www.keysight.com/find/kmsic
- PicoScope AI oscilloscope: https://www.picotech.com/ai-oscilloscope
- ONRAMP for technicians: https://getonramp.io/technicians
