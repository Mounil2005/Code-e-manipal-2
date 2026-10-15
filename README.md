# resqnet — Multi-Agent Flood Rescue Coordination Digital Twin

**Code-E-Manipal 2.0 · PS 27 · Multi-Agent Systems / Logistics**
*Flood Rescue Dispatch Where Boats, Shelters and Roads Negotiate in Real Time*

> ⚠️ Research and educational simulation. Riverbend is a synthetic city; all incidents, reports and floods are generated from a seed. This is **not** an operational emergency-dispatch system.

Most emergency dashboards show you where the crisis is. resqnet shows how autonomous rescue resources negotiate, recover and coordinate after the original rescue plan stops working, and it lets you check whether a different decision would have done better.

## What it does

| | |
|---|---|
| **Deterministic digital twin** | Tick-based engine (1 tick = 1 simulated minute). Riverbend has 56 nodes, 92 road/river/bridge/jetty edges, 5 neighbourhoods, 3 bridges, 3 shelters, 5 boats and 4 ground teams. The flood rises and closes roads as it goes. |
| **Real multi-agent negotiation** | Incident, rescue, shelter and route agents work through contract-net bidding and deferred acceptance (Gale–Shapley). The protocol runs REQUEST → BID → PROVISIONAL_ACCEPT → RESERVE → SAFETY → COMMIT. Every message is logged. |
| **Partial information** | Decentralised agents get broadcasts after a comms delay and plan on their own, possibly stale, view of the network. They find closures physically and can lose comms completely during a blackout. |
| **Safety shield** | An independent validator checks every commit and every move against the true world state. It enforces invariants S1–S7: double booking, shelter capacity, prohibited edges, transport capacity, stale versions, reservation validity and operational status. |
| **Fault injection mid-run** | You can close a bridge, disable a boat, overflow a shelter, trigger a surge, black out a unit's comms, send contradictory SOS reports, or fire a one-click **triple failure** that picks its targets from live state. |
| **Uncertainty-aware SOS** | Duplicate, delayed, contradictory and false-alarm reports are merged using noisy-OR evidence. Low-confidence reports can still be dispatched provisionally under explicit rules E1–E4. |
| **Explainable decisions** | Each decision is stored as a structured record: evidence, candidate bids with their cost breakdown, rejections, the shelter negotiation, safety checks, and what would have changed the outcome. The explanation text is generated from that record. |
| **Counterfactual Decision Lab** | Fork the run at any past tick and change boats, bridges, shelter capacity, policy, comms delay or strategy. The lab then runs both futures under identical future events. |
| **Fair baseline** | A centralised Hungarian optimiser and a greedy dispatcher use the same cost function, the same shelters and the same shield as the multi-agent strategy. Benchmarks run over multiple paired seeds. |
| **Adversarial explorer** | A seeded, bounded search over failure combinations. It reports the worst cases it finds and opens any of them in the simulator. |
| **Event-sourced replay** | Any run can be rebuilt from scenario + seed + strategy + injected-event log, with hashes checked tick by tick. |
| **After-action report** | JSON and PDF export, including a strategy comparison on the same seed and the same events. |

## Quick start

Requirements: Python 3.11+ and Node 18+.

```bash
cd backend
pip install -r requirements.txt
python -m uvicorn app.api.main:app --port 8000
```

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. On Windows, `scripts/start.ps1` launches both. The app needs no paid APIs and no LLM.

### Tests and build

```bash
cd backend && python -m pytest tests -q -p no:asyncio
```

```bash
cd frontend && npx tsc -b && npx vite build
```

The suite has 42 tests: determinism, replay, rerouting, retreat on a closing edge, unreachable requests, double booking, stale state, expired reservations, capacity limits, boat failure, stranded passengers, shelter overflow, triple failure under all three strategies, contradictory and duplicate reports, delayed information, benchmarks, counterfactual sanity checks, policy-change renegotiation, the API, WebSocket resync and PDF export. Hypothesis property tests check the safety invariants under random failure sequences.

### Reproduce benchmarks

```bash
python scripts/run_benchmarks.py 10
```

This writes `docs/benchmark_results.md`.

## Real maps (Kerala)

Besides the synthetic Riverbend grid, the simulator runs on real geography: OpenStreetMap roads, bridges and rivers plus the 30 m Copernicus DEM (GLO-30) for terrain. Two snapshots ship in `backend/app/data/maps/`: **Aluva · Periyar River** and **Chengannur · Pamba River**. Pick one under *New run → Map*; Riverbend stays the default for tests and benchmarks.

Snapshots are committed JSON, so runs are deterministic and work offline. To rebuild or add an area (needs network access):

```bash
python scripts/build_map.py aluva        # areas are defined at the top of the script
```

The builder simplifies the road graph, samples the river channel, ranks bridges by criticality, picks shelters near people and water, and scales elevation into the simulator's water-level frame. Incidents are weighted by building density. Flood depths remain simulated.

### Kerala 2018 flood replay

The **Kerala 2018 · real flood replay** scenario drives the river level from the real August 2018 river-discharge curve (GloFAS reanalysis) and shapes SOS arrivals with real rainfall (IMD gauge-based daily totals, hourly shape from ERA5), time-compressed onto 180 ticks. Data comes from the free Open-Meteo APIs and is stored as committed snapshots in `backend/app/data/events/`:

```bash
python scripts/build_flood_event.py kerala_aluva
```

Discharge and rainfall are real; inundation depths are still produced by the simulator's threshold model, so this is a replay of the flood's timing and intensity, not a reconstruction of observed flood extent. `scripts/validate_flood.py` compares the modelled flood footprint with a Sentinel-1 water mask from 21 Aug 2018; results and caveats are in `docs/flood_validation.md`.

### Demo

**Judge Mode** (`/judge`) tells the Aluva 2018 flood in five chapters, and each chapter drives the live engine:

1. The map opens on 14 Aug.
2. The river rises (15 Aug).
3. Three failures hit at the flood peak (16 Aug).
4. A decision record explains why the agents chose that plan.
5. The final count (17 Aug).

The camera flies to each failure. The run is seeded, so it ends the same way every time.

**Offline:** a service worker keeps every map tile and font the browser has loaded, so do one full rehearsal on wifi and the demo then works without a connection. It never bulk-downloads tiles, which the OpenStreetMap tile policy forbids. Views you have not opened before stay blank when offline. The backend runs locally and needs no internet.

## Deploy (Vercel + Render)

The backend runs simulations, the live feed and WebSockets, so it needs a long-running server (Render). The frontend is a static build (Vercel).

1. **Render (backend).** New → Blueprint → this repo. `render.yaml` creates `resqnet-api` from `backend/`. Note the URL, e.g. `https://resqnet-api.onrender.com`, and check `/api/health`.
2. **Vercel (frontend).** New Project → this repo, **Root Directory `frontend`**. Add the environment variable `VITE_API_URL` = the Render URL (no trailing slash), then deploy. `frontend/vercel.json` handles routing for the app's pages.

On Render's free plan the backend sleeps after about 15 idle minutes. The first visit then waits up to a minute while it wakes; the app retries and says so. Open the site once before a demo. Simulations live in memory, so a restart or wake-up starts fresh.

## Pages

| Route | Page |
|---|---|
| `/command` | Live Command Center: map, incident queue, agent activity, inspector, KPIs, one-click chaos |
| `/dispatch` | Fleet & Dispatch: unit cards, manual directives (validated by the shield), assignment log |
| `/agents` | Negotiation Lab: message graph per round, full message trace, agent knowledge, policy changes |
| `/scenarios` | Scenario & Chaos Lab: scenario editor, presets, scheduled events, targeted injection, recovery tracking |
| `/decision-lab` | Counterfactual Decision Lab: fork, change, run both futures, synchronised maps, measured deltas |
| `/arena` | Strategy Arena: multi-seed paired benchmarks with mean ± sd and per-seed runs |
| `/replay` | Mission Replay & Audit: timeline scrubber, before/after diffs, event filters, replay verification |
| `/safety` | Safety Shield log and Adversarial Explorer |
| `/reports` | After-Action Report, JSON/PDF export, run comparison |

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): system design, data flow, API
- [docs/AGENTS.md](docs/AGENTS.md): agents, negotiation protocol, tie-breaking, information model
- [docs/SIMULATION.md](docs/SIMULATION.md): tick pipeline, flood model, reconciliation, safety, fairness, metrics
- [docs/BENCHMARKS.md](docs/BENCHMARKS.md): methodology and measured results
- [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md): 5-minute judge demo

## Repository layout

```
resqnet/
  backend/app/
    simulation/   state, Riverbend world, flood model, report reconciliation, engine, serialisation
    routing/      NetworkX resource-specific routing + believed-network planning
    negotiation/  shared cost/commit machinery + decentralised protocol
    optimization/ centralised Hungarian + greedy baselines
    safety/       safety shield (commit + movement + invariant sweep)
    metrics/      metrics definitions
    analysis/     replay, counterfactual, benchmark, adversarial search
    persistence/  SQLite event-sourced store
    api/          FastAPI REST + WebSocket, report builder (JSON/PDF)
  backend/tests/
  frontend/src/   React + TS + Tailwind + Zustand + React Flow + Recharts
  docs/  scripts/
```
