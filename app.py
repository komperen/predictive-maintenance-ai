"""
Predictive Maintenance AI - H2O Wave app (FinSight-style theme, OpenRouter AI)
===============================================================================
Replicates the "Manufacture - Telco Predictive Maintenance" solution slide:

  1. Real-time integrated IoT sensor monitoring
  2. LLM translation & alerting (raw sensor data -> plain-language preventive alerts)
  3. Automated preventive maintenance actions (work orders)
  4. Predictive Health - health score, failure forecast (time to threshold) and downtime avoided
  5. AI Copilot - ask the fleet anything in plain language

AI providers (Settings > AI Provider):
  * Simulation  - free, no key, rule-based text (default, always works)
  * OpenRouter  - free ":free" models via https://openrouter.ai/api/v1 (bring your own free key)
  * Custom      - any OpenAI-compatible endpoint (e.g. a model served on HPE MLIS)
Any AI failure (no key, rate limit, network) falls back to rule-based text, so a live
demo never breaks.

Look & feel follows the FinSight AI app: dark navy sidebar, light content area, white
cards, emerald accent with purple "AI" accents.

Package for H2O AI Cloud via My Apps > Upload App (zip root: app.py, app.toml,
requirements.txt, sample_data/). Remember to bump Version in app.toml for every upload.

NOTE: never use `key in q.page` (Wave's Page has no __contains__) - assign
`q.page[key] = ui.card(...)` or call `del q.page[key]` unconditionally.
"""

import asyncio
import base64
import json
import os
import random
import re
import time
from collections import deque
from datetime import datetime

import requests
from h2o_wave import Q, app, main, ui, on, run_on, data

# --------------------------------------------------------------------------------------
# Static config / sample data
# --------------------------------------------------------------------------------------

APP_DIR = os.path.dirname(os.path.abspath(__file__))
MACHINES_PATH = os.path.join(APP_DIR, "sample_data", "machines.json")

with open(MACHINES_PATH, "r") as f:
    MACHINE_DEFS = json.load(f)["machines"]

ENGINEERS_PATH = os.path.join(APP_DIR, "sample_data", "engineers.json")
with open(ENGINEERS_PATH, "r") as f:
    ENGINEER_DEFS = json.load(f)["engineers"]


def _load_json(name, key):
    with open(os.path.join(APP_DIR, "sample_data", name), "r") as fh:
        return json.load(fh)[key]


FAULT_KB = _load_json("fault_kb.json", "faults")     # SOP / LOTO / parts per failure mode
PARTS_DEFS = _load_json("parts.json", "parts")       # spare-parts inventory template
PM_DEFS = _load_json("pm_plan.json", "tasks")        # preventive-maintenance plan template

MACHINE_SHORT = {
    "cnc-01": "CNC #1",
    "conv-02": "Conveyor #2",
    "hyd-03": "Press #3",
    "arm-04": "Weld Arm #4",
    "comp-05": "Compressor #5",
}

HISTORY_LEN = 24          # points kept per sparkline
TICK_SECONDS = 3          # live-monitoring refresh interval
MAX_ALERTS = 30
MAX_ACTIONS = 30
MAX_CHAT = 20

SENSOR_LABELS = {
    "temperature": "Temperature",
    "vibration": "Vibration",
    "pressure": "Pressure",
    "rpm": "RPM",
}
CHART_SENSORS = ("temperature", "vibration", "pressure")

FAULT_ACTIONS = {
    "temperature": ("Overheating", "Schedule cooling-system inspection and reduce duty cycle", "Mechanical Team"),
    "vibration": ("Excessive vibration", "Inspect bearings / alignment and schedule balancing", "Mechanical Team"),
    "pressure": ("Pressure drop", "Check for seal / hose leaks and inspect hydraulic pump", "Hydraulics Team"),
    "rpm": ("Abnormal RPM", "Inspect drive motor and controller, verify load", "Electrical Team"),
}

# --- AI providers ---------------------------------------------------------------------

PROVIDER_LABELS = {
    "simulation": "Simulation (free, no key)",
    "openrouter": "OpenRouter (free models)",
    "custom": "HPE PCAI - Qwen on-prem (or any OpenAI-compatible endpoint)",
}
# HPE Private Cloud AI (MLIS) endpoint prefilled for the demo. The endpoint name and model are NOT secret;
# the access token is - it is never stored in the code, only entered in Settings or injected as LLM_API_KEY.
DEFAULT_PCAI_BASE_URL = "https://qwen27bver38.erwin-mustofa-i-c9a8725d.serving.ai-application.imid.local"
DEFAULT_PCAI_MODEL = "qwen3.8-27b"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "openrouter/free"
# OpenRouter free-model slugs change often - the model box is free text, this is just a
# convenient list. "openrouter/free" lets OpenRouter pick an available free model.
OPENROUTER_MODEL_PRESETS = [
    "openrouter/free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "openai/gpt-oss-120b:free",
    "openai/gpt-oss-20b:free",
    "google/gemma-4-31b-it:free",
    "z-ai/glm-4.5-air:free",
]

COPILOT_SUGGESTIONS = [
    "Summarize fleet health right now",
    "Which machine should we inspect first, and why?",
    "Explain the latest alert in plain language",
    "What preventive work orders were created?",
    "Which machine is most likely to fail next?",
    "Who is on duty and who should take the latest work order?",
    "Apa yang harus dilakukan untuk mesin dengan status critical?",
    "What is the plant OEE and which machine drags it down?",
    "Which spare parts are low and which PM tasks are overdue?",
]

# --- FinSight-style palette -------------------------------------------------------------

NAVY = "#0D1627"        # sidebar / primary
PAGE_BG = "#F5F6FA"     # content background
CARD_BG = "#FFFFFF"
TEXT = "#0F172A"
MUTED = "#64748B"
EMERALD = "#10B981"     # brand accent / normal
PURPLE = "#7C3AED"      # AI accents
SKY = "#0EA5E9"
AMBER = "#F59E0B"       # warning
RED = "#EF4444"         # critical
THEME_NAME = "predictive-finsight"

# Work-order lifecycle: Open (nobody assigned) -> Assigned -> In Progress -> Completed
WO_STATE_COLORS = {"Open": RED, "Assigned": SKY, "In Progress": PURPLE, "Completed": EMERALD}
WO_PRIORITY_COLORS = {"P1 - Critical": RED, "P2 - Warning": AMBER}
ACTIVE_STATES = ("Assigned", "In Progress")

CSS = """
.ms-Button--primary { background-color: #10B981 !important; border-color: #10B981 !important; }
.ms-Button--primary:hover { background-color: #059669 !important; border-color: #059669 !important; }
"""

_LOGO_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<defs><linearGradient id='g' x1='0' y1='0' x2='1' y2='1'>"
    "<stop offset='0' stop-color='#10B981'/><stop offset='1' stop-color='#047857'/></linearGradient></defs>"
    "<rect width='64' height='64' rx='14' fill='url(#g)'/>"
    "<rect x='15' y='34' width='9' height='15' rx='2' fill='white'/>"
    "<rect x='28' y='25' width='9' height='24' rx='2' fill='white'/>"
    "<rect x='41' y='16' width='9' height='33' rx='2' fill='white'/>"
    "</svg>"
)
LOGO_URI = "data:image/svg+xml;base64," + base64.b64encode(_LOGO_SVG.encode()).decode()

TAB_META = {
    "#overview": ("Overview", "Real-time machine health across all production lines"),
    "#alerts": ("Alerts & Work Orders", "Plain-language alerts, automatic work orders and engineer dispatch"),
    "#health": ("Predictive Health", "Health scores, failure forecast and estimated downtime avoided"),
    "#production": ("Production & OEE", "Output, OEE, downtime cost, energy use and the shift handover"),
    "#maintenance": ("Parts & PM Plan", "Preventive-maintenance schedule, spare-parts stock and readiness for at-risk machines"),
    "#copilot": ("AI Copilot", "Ask your fleet anything in plain language"),
    "#settings": ("Settings & Demo Controls", "Run the demo, inject faults and choose the AI provider"),
}
LIVE_TABS = {"#overview", "#alerts", "#health", "#production"}   # tabs refreshed by the live loop


# --------------------------------------------------------------------------------------
# Simulation helpers
# --------------------------------------------------------------------------------------

def _direction(sensor_cfg):
    """Return +1 if higher values are worse, -1 if lower values are worse."""
    return 1 if sensor_cfg["warning"] > sensor_cfg["baseline"] else -1


def sensor_applicable(cfg):
    """Sensors a machine does not have (e.g. a press has no RPM) are encoded with
    baseline == warning == critical and must be skipped in threshold logic."""
    return not (cfg["baseline"] == cfg["warning"] == cfg["critical"])


def new_machine_state(mdef):
    sensors = {}
    for sname, cfg in mdef["sensors"].items():
        val = cfg["baseline"]
        sensors[sname] = {
            "value": val,
            "history": deque([round(val * (1 + random.uniform(-0.03, 0.03)), 2) for _ in range(HISTORY_LEN)],
                             maxlen=HISTORY_LEN),
            "ticks": deque(range(-HISTORY_LEN + 1, 1), maxlen=HISTORY_LEN),
            "active_fault": False,
            "level": 0,                      # highest severity already alerted: 0 Normal, 1 Warning, 2 Critical
        }
    return {
        "id": mdef["id"],
        "name": mdef["name"],
        "line": mdef["line"],
        "sensors": sensors,
        "status": "Normal",
        "last_updated": datetime.now(),
    }


def sensor_status(value, cfg):
    if _direction(cfg) == 1:
        if value >= cfg["critical"]:
            return "Critical"
        if value >= cfg["warning"]:
            return "Warning"
        return "Normal"
    if value <= cfg["critical"]:
        return "Critical"
    if value <= cfg["warning"]:
        return "Warning"
    return "Normal"


def machine_overall_status(machine, mdef):
    worst = "Normal"
    order = {"Normal": 0, "Warning": 1, "Critical": 2}
    for sname, sstate in machine["sensors"].items():
        cfg = mdef["sensors"][sname]
        if not sensor_applicable(cfg):
            continue
        st = sensor_status(sstate["value"], cfg)
        if order[st] > order[worst]:
            worst = st
    return worst


def random_walk(value, cfg):
    """Small bounded random walk pulled back toward baseline."""
    pull = (cfg["baseline"] - value) * 0.08
    step = random.gauss(0, cfg["noise"]) + pull
    return round(value + step, 2)


# Business assumptions for the demo (edit freely): cost of one hour of unplanned downtime per machine
# in Rupiah, and the unplanned downtime a completed preventive work order is assumed to prevent.
DOWNTIME_COST_PER_HOUR = {"cnc-01": 45_000_000, "conv-02": 60_000_000, "hyd-03": 80_000_000,
                          "arm-04": 55_000_000, "comp-05": 35_000_000}
DOWNTIME_HOURS_AVOIDED = {"Critical": 4, "Warning": 2}
SIM_MIN_PER_TICK = 1        # one live tick represents this many plant minutes (demo time scale)
FORECAST_WINDOW = 12        # ticks used for the trend line
HORIZON_MIN = 20            # a machine is "at risk" when a breach is predicted within this many minutes
STABLE_MIN = 60             # beyond this the machine is shown as stable
TREND_T = 8.0              # minimum t-statistic of the fitted slope to call it a real trend
DRIFT_STEP = 0.10           # gradual-degradation speed (fraction of baseline->critical range per tick)


def sensor_stress(value, cfg):
    """0 = at baseline, 1 = at the critical threshold (direction aware)."""
    span = abs(cfg["critical"] - cfg["baseline"]) or 1
    return max(0.0, min(1.0, (value - cfg["baseline"]) * _direction(cfg) / span))


def trend_eta(sstate, cfg, target):
    """Minutes until the sensor trend reaches `target`, or None if it is not moving that way."""
    pts = list(sstate["history"])[-FORECAST_WINDOW:]
    n = len(pts)
    if n < 6:
        return None
    mean_x = (n - 1) / 2
    mean_y = sum(pts) / n
    var = sum((i - mean_x) ** 2 for i in range(n))
    slope = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(pts)) / var
    resid = sum((y - (mean_y + slope * (i - mean_x))) ** 2 for i, y in enumerate(pts)) / (n - 2)
    t_stat = slope / ((resid / var) ** 0.5 or 1e-9)         # how clearly the points follow a line
    if t_stat < TREND_T:                                    # random wobble, not a degradation trend
        return None
    d = _direction(cfg)
    rate = slope * d                                       # progress towards the threshold per tick
    if rate <= max(cfg["noise"] * 0.25, 1e-9):             # flat or noise-level drift -> not a trend
        return None
    if sensor_stress(sstate["value"], cfg) < 0.2:         # still hugging the baseline: just noise
        return None
    remaining = (target - sstate["value"]) * d
    if remaining <= 0:
        return 0.0
    return remaining / rate * SIM_MIN_PER_TICK


def machine_forecast(machine):
    """Health score (0-100), risk level and the earliest predicted threshold breach."""
    mdef = find_mdef(machine["id"])
    stresses, best = [], None
    for sname, sstate in machine["sensors"].items():
        cfg = mdef["sensors"][sname]
        if not sensor_applicable(cfg):
            continue
        stresses.append(sensor_stress(sstate["value"], cfg))
        status = sensor_status(sstate["value"], cfg)
        if status == "Critical":
            cand = (0.0, sname, "Critical")
        else:
            target_name = "Critical" if status == "Warning" else "Warning"
            eta = trend_eta(sstate, cfg, cfg["critical"] if status == "Warning" else cfg["warning"])
            cand = (eta, sname, target_name) if eta is not None else None
        if cand and (best is None or cand[0] < best[0]):
            best = cand
    worst = max(stresses) if stresses else 0
    health = max(0, min(100, round(100 * (1 - (0.7 * worst + 0.3 * (sum(stresses) / len(stresses)))))))
    eta = best[0] if best and best[0] <= STABLE_MIN else None
    status = machine["status"]
    if status == "Critical" or health < 50 or (eta is not None and eta <= 8):
        risk = "High"
    elif status == "Warning" or health < 75 or (eta is not None and eta <= HORIZON_MIN):
        risk = "Medium"
    else:
        risk = "Low"
    driver_sensor = best[1] if best and eta is not None else None
    if driver_sensor is None and stresses:                  # fall back to the most stressed sensor
        names = [k for k, c in mdef["sensors"].items() if sensor_applicable(c)]
        driver_sensor = names[max(range(len(stresses)), key=lambda i: stresses[i])]
    return dict(machine=machine["name"], id=machine["id"], health=health, risk=risk, status=status,
                eta=eta, target=best[2] if best and eta is not None else None,
                driver=SENSOR_LABELS.get(driver_sensor, driver_sensor or "-"), driver_key=driver_sensor)


def eta_text(f):
    if f["eta"] is None:
        return "Stable"
    if f["eta"] <= 0:
        return f"{f['target']} threshold crossed"
    return f"~{max(1, round(f['eta']))} min to {f['target']}"


def fleet_forecast(q):
    order = {"High": 0, "Medium": 1, "Low": 2}
    return sorted((machine_forecast(m) for m in q.client.machines.values()),
                  key=lambda f: (order[f["risk"]], f["health"]))


def forecast_lines(q):
    return [f"{f['machine']}: health {f['health']}, risk {f['risk']}, driver {f['driver']}, {eta_text(f)}"
            for f in fleet_forecast(q)]


def downtime_avoided(q):
    total = 0
    for a in q.client.actions:
        if a["state"] == "Completed":
            mid = next((m["id"] for m in MACHINE_DEFS if m["name"] == a["machine"]), None)
            total += DOWNTIME_COST_PER_HOUR.get(mid, 40_000_000) * DOWNTIME_HOURS_AVOIDED.get(a["severity"], 2)
    return total


def fmt_rupiah(v):
    if v >= 1_000_000_000:
        return f"Rp {v / 1_000_000_000:.1f} M".replace(".", ",")
    if v >= 1_000_000:
        return f"Rp {v / 1_000_000:.0f} jt"
    return "Rp 0"


# --- Production, OEE and energy (demo model: plant time runs faster than wall-clock time) -------
_PROD_RNG = random.Random()   # separate generator so production noise never disturbs the sensor simulation
PROD_MIN_PER_TICK = 5       # plant minutes of production simulated per live tick
PROD_START_MIN = 240        # the demo starts 4 hours into the shift so the numbers are not empty
OEE_TARGET = 0.85           # "world class" OEE benchmark
ENERGY_TARIFF = 1450        # Rp per kWh (demo assumption)
OEE_BASE = {"cnc-01": 1.0, "conv-02": 0.98, "hyd-03": 0.95, "arm-04": 0.97, "comp-05": 1.0}
AVAIL_BY_STATUS = {"Normal": 0.97, "Warning": 0.93, "Critical": 0.60}


def fmt_idr(v):
    """Rupiah with a unit that suits small and large amounts."""
    if 1000 <= v < 1_000_000:
        return f"Rp {v / 1000:.0f} rb"
    return fmt_rupiah(v)


def oee_parts(q, machine):
    """(availability, performance, quality) 0-1, driven by the live sensor stress and work-order state."""
    mdef = find_mdef(machine["id"])
    stresses = {k: sensor_stress(st["value"], mdef["sensors"][k])
                for k, st in machine["sensors"].items() if sensor_applicable(mdef["sensors"][k])}
    worst = max(stresses.values()) if stresses else 0.0
    quality_stress = max(stresses.get("temperature", 0.0), stresses.get("vibration", 0.0))
    repairing = any(a["machine"] == machine["name"] and a["state"] == "In Progress" for a in q.client.actions)
    avail = 0.40 if repairing else AVAIL_BY_STATUS.get(machine["status"], 0.97)
    perf = max(0.2, OEE_BASE.get(machine["id"], 1.0) * (0.93 - 0.40 * worst))
    qual = max(0.5, 0.985 - 0.25 * quality_stress)
    return avail, perf, qual


def _intensity(mdef, p):
    """kWh per unit produced at the current operating point."""
    return p["kw"] / max(1e-6, mdef["rate_per_min"] * 60 * max(p["oee"], 0.01))


def new_prod_state(mdef):
    a, pf, qu = 0.97, 0.93 * OEE_BASE.get(mdef["id"], 1.0), 0.985
    mins = PROD_START_MIN
    kw = mdef["power_kw"] * (0.30 + 0.70 * pf) * a
    p = dict(elapsed=mins, units=mdef["rate_per_min"] * mins * a * pf * qu, run_min=mins * a,
             down_min=mins * (1 - a), kw=kw, kwh=kw * mins / 60, a=a, p=pf, q=qu, oee=a * pf * qu)
    p["hist"] = deque([round(p["oee"] * 100, 1)] * HISTORY_LEN, maxlen=HISTORY_LEN)
    p["int0"] = _intensity(mdef, p)
    return p


def update_production(q, machine, mdef):
    """Advance one machine's production counters by one tick."""
    p = (q.client.prod or {}).get(machine["id"])
    if p is None:
        return
    a, pf, qu = oee_parts(q, machine)
    a = min(1.0, a + _PROD_RNG.uniform(-0.006, 0.006))
    pf = min(1.0, pf + _PROD_RNG.uniform(-0.006, 0.006))
    qu = min(1.0, qu + _PROD_RNG.uniform(-0.003, 0.003))
    mins = PROD_MIN_PER_TICK
    run = mins * a
    p["elapsed"] += mins
    p["run_min"] += run
    p["down_min"] += mins - run
    p["units"] += mdef["rate_per_min"] * run * pf * qu
    p["kw"] = mdef["power_kw"] * (0.30 + 0.70 * pf) * a + mdef["power_kw"] * 0.10 * (1 - a)
    p["kwh"] += p["kw"] * mins / 60
    p.update(a=a, p=pf, q=qu, oee=a * pf * qu)
    p["hist"].append(round(p["oee"] * 100, 1))


def plant_prod(q):
    """Plant-level production KPIs (OEE weighted by each machine's rated speed)."""
    ps = q.client.prod or {}
    rows = [(find_mdef(mid), p) for mid, p in ps.items()]
    if not rows:
        return dict(oee=0.0, attain=0.0, down_min=0.0, down_cost=0.0, kwh=0.0, energy_cost=0.0)
    wsum = sum(md["rate_per_min"] for md, _ in rows)
    attain = sum(p["units"] / (md["rate_per_min"] * p["elapsed"] * OEE_TARGET) for md, p in rows) / len(rows)
    down_cost = sum(p["down_min"] / 60 * DOWNTIME_COST_PER_HOUR.get(md["id"], 40_000_000) for md, p in rows)
    kwh = sum(p["kwh"] for _, p in rows)
    return dict(oee=sum(p["oee"] * md["rate_per_min"] for md, p in rows) / wsum, attain=attain,
                down_min=sum(p["down_min"] for _, p in rows), down_cost=down_cost,
                kwh=kwh, energy_cost=kwh * ENERGY_TARIFF)


# --- Spare parts and preventive-maintenance plan -------------------------------------------------

def part_status(p):
    if p.get("on_order"):
        return "On order"
    if p["stock"] <= 0:
        return "Out of stock"
    if p["stock"] <= p["min_stock"]:
        return "Reorder"
    return "OK"


def pm_status(t):
    d = t["due_in_days"]
    return "Overdue" if d < 0 else "Due soon" if d <= 7 else "On track"


def pm_due_text(t):
    d = t["due_in_days"]
    return f"overdue by {-d} d" if d < 0 else "due today" if d == 0 else f"in {d} d"


def find_part(q, part_id):
    return next((p for p in q.client.parts if p["id"] == part_id), None)


def maintenance_summary(q):
    overdue = [t for t in q.client.pm if t["due_in_days"] < 0]
    soon = [t for t in q.client.pm if 0 <= t["due_in_days"] <= 7]
    low = [p for p in q.client.parts if part_status(p) in ("Reorder", "Out of stock")]
    return overdue, soon, low


def consume_parts(q, wo):
    """Take the spare parts a repair normally uses out of stock when its work order completes."""
    used, short = [], []
    for it in (FAULT_KB.get(wo.get("sensor_key")) or {}).get("parts", []):
        p = find_part(q, it["part"])
        if p is None:
            continue
        if p["stock"] >= it["qty"]:
            p["stock"] -= it["qty"]
            used.append(f"{it['qty']} x {p['name']}")
        else:
            short.append(p["name"])
    wo["parts_used"], wo["parts_short"] = used, short
    return used, short


def shift_name(now=None):
    h = (now or datetime.now()).hour
    return "Shift 1 (07-15)" if 7 <= h < 15 else "Shift 2 (15-23)" if 15 <= h < 23 else "Shift 3 (23-07)"


def find_mdef(machine_id):
    for m in MACHINE_DEFS:
        if m["id"] == machine_id:
            return m
    return None


def alert_template(machine_name, sensor_label, value, unit, status, threshold):
    return (
        f"{status} alert on {machine_name}: {sensor_label} reading of {value}{unit} has crossed "
        f"the {status.lower()} threshold ({threshold}{unit}). Recommend inspection before the next shift "
        f"to avoid unplanned downtime."
    )


def evaluate_machine(q, machine, mdef, force_sensor=None):
    """Advance one machine by one tick, detect NEW threshold breaches and create the
    alert + automated preventive action for each. Alert text starts as the rule-based
    template; enrich_alerts() may replace it with AI-written text afterwards."""
    new_alerts = []
    new_actions = []

    for sname, sstate in machine["sensors"].items():
        cfg = mdef["sensors"][sname]
        if not sensor_applicable(cfg):
            continue
        if force_sensor == sname:
            sstate["value"] = cfg["critical"] + _direction(cfg) * abs(cfg["noise"]) * 2
        else:
            sstate["value"] = random_walk(sstate["value"], cfg)
            if (q.client.drift or {}).get(machine["id"]) == sname:      # gradual degradation demo
                span = abs(cfg["critical"] - cfg["baseline"])
                sstate["value"] = round(sstate["value"] + _direction(cfg) * span * DRIFT_STEP, 2)

        sstate["history"].append(sstate["value"])
        sstate["ticks"].append(q.client.tick_count)

        status = sensor_status(sstate["value"], cfg)
        rank = {"Normal": 0, "Warning": 1, "Critical": 2}[status]

        if rank > sstate["level"]:           # new fault, or an existing Warning escalating to Critical
            sstate["active_fault"] = True
            sstate["level"] = rank
            label = SENSOR_LABELS.get(sname, sname)
            unit = cfg["unit"]
            threshold = cfg["warning"] if status == "Warning" else cfg["critical"]
            if status == "Critical" and (q.client.drift or {}).get(machine["id"]) == sname:
                q.client.drift.pop(machine["id"], None)                  # degradation has run its course
            ctx = dict(machine=machine["name"], sensor=label, value=sstate["value"],
                       unit=unit, status=status, threshold=threshold)

            new_alerts.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "machine": machine["name"],
                "severity": status,
                "message": alert_template(machine["name"], label, sstate["value"], unit, status, threshold),
                "ai": "",
                "ctx": ctx,
            })

            fault_name, action_text, team = FAULT_ACTIONS.get(
                sname, ("Anomaly detected", "Inspect machine", "Maintenance Team"))
            q.client.wo_seq = (q.client.wo_seq or 1000) + 1
            new_actions.append({
                "id": f"WO-{q.client.wo_seq}",
                "time": datetime.now().strftime("%H:%M:%S"),
                "machine": machine["name"],
                "severity": status,
                "priority": "P1 - Critical" if status == "Critical" else "P2 - Warning",
                "sensor": label,
                "sensor_key": sname,
                "reading": f"{sstate['value']}{unit}",
                "limit": f"{threshold}{unit}",
                "issue": fault_name,
                "action": action_text,
                "team": team,
                "state": "Open",              # waiting for an engineer to be assigned
                "assignee": None,
                "assigned_at": None,
            })
            new_alerts[-1]["wo"] = f"WO-{q.client.wo_seq}"
        elif status == "Normal" and sstate["level"]:
            sstate["active_fault"] = False
            sstate["level"] = 0

    machine["status"] = machine_overall_status(machine, mdef)
    machine["last_updated"] = datetime.now()
    return new_alerts, new_actions


# --------------------------------------------------------------------------------------
# AI layer: provider config, one chat function, alert enrichment, copilot
# --------------------------------------------------------------------------------------

def ai_config(q):
    """Return the active provider's connection dict, or None (= simulation)."""
    provider = q.client.ai_provider
    if provider == "openrouter":
        key = (q.client.openrouter_key or "").strip()
        if not key:
            return None
        return dict(provider="openrouter", base=OPENROUTER_BASE, key=key,
                    model=(q.client.openrouter_model or DEFAULT_OPENROUTER_MODEL).strip(),
                    label="OpenRouter")
    if provider == "custom":
        base = (q.client.llm_base_url or "").strip()
        if not base:
            return None
        base = base.rstrip("/")
        if not base.endswith("/v1"):          # MLIS / OpenAI-compatible servers expose /v1/chat/completions
            base += "/v1"
        return dict(provider="custom", base=base, key=(q.client.llm_api_key or "").strip(),
                    model=(q.client.llm_model or "").strip() or DEFAULT_PCAI_MODEL, label="HPE PCAI",
                    verify=q.client.llm_verify_ssl is not False)
    return None


def ai_ready(q):
    return ai_config(q) is not None and time.time() >= (q.client.ai_cooldown_until or 0)


def _http_error_text(resp):
    code = resp.status_code
    hint = {
        401: "invalid, expired or missing API token",
        403: "access denied for this token / endpoint",
        402: "not enough credits - choose a model ending in :free",
        404: "model not found - check the model ID",
        429: "rate limit reached - free models are limited per minute and per day",
    }.get(code)
    if hint is None:
        try:
            hint = str(resp.json().get("error", {}).get("message", ""))[:120] or "request failed"
        except Exception:
            hint = "request failed"
    return f"{hint} ({code})"


THINK_RE = re.compile(r"<think>.*?(</think>|$)", re.S | re.I)


def _read_completion(resp):
    """Pull (text, finish_reason, error) out of a /chat/completions response."""
    try:
        data = resp.json()
    except Exception:
        return "", None, "unexpected response format"
    if isinstance(data, dict) and data.get("error"):                  # some gateways answer 200 + error body
        err = data["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return "", None, f"provider error: {str(msg)[:120]}"
    try:
        choice = data["choices"][0]
        content = (choice.get("message") or {}).get("content")
    except Exception:
        return "", None, "unexpected response format"
    if isinstance(content, list):                                      # content parts -> plain text
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    text = THINK_RE.sub("", content or "").strip()                     # drop inline <think> blocks
    return text, choice.get("finish_reason"), None


def llm_chat(cfg, messages, max_tokens=300, temperature=0.2, timeout=25):
    """Blocking OpenAI-compatible /chat/completions call. Returns (text, error).

    Free "reasoning" models can spend the whole token budget thinking and return an empty answer
    (finish_reason == "length"); in that case the call is retried once with a bigger budget."""
    headers = {"Content-Type": "application/json"}
    if cfg["key"]:
        headers["Authorization"] = f"Bearer {cfg['key']}"
    if cfg["provider"] == "openrouter":
        headers["HTTP-Referer"] = "https://h2o.ai"
        headers["X-Title"] = "Predictive Maintenance Copilot"
    finish = None
    for attempt in range(2):
        budget = max_tokens if attempt == 0 else min(max_tokens * 4, 1500)
        payload = {"model": cfg["model"], "messages": messages,
                   "temperature": temperature, "max_tokens": budget}
        if cfg["provider"] == "openrouter":
            payload["reasoning"] = {"effort": "low"}                  # ignored by models without reasoning
        try:
            resp = requests.post(cfg["base"].rstrip("/") + "/chat/completions",
                                 headers=headers, json=payload, timeout=timeout + 10 * attempt,
                                 verify=cfg.get("verify", True))
        except requests.exceptions.SSLError:
            return None, ("TLS certificate not trusted - untick 'Verify TLS certificate' in Settings "
                          "for lab endpoints that use a self-signed CA")
        except requests.RequestException as e:
            return None, f"network error ({type(e).__name__}) - check the endpoint URL and that this pod can reach it"
        if resp.status_code != 200:
            return None, _http_error_text(resp)
        text, finish, err = _read_completion(resp)
        if err:
            return None, err
        if text:
            return text, None
        if finish != "length":
            break
    if finish == "length":
        return None, ("the model used all its tokens on thinking and returned no answer - "
                      "pick a non-reasoning :free model in the Model box")
    return None, "empty response from the model - it may be busy, try again or choose another :free model"


async def ai_chat(q, messages, max_tokens=300):
    """Run llm_chat off the event loop, count the call and apply a cool-down after a
    rate-limit / credit error so a demo does not hammer the free tier."""
    cfg = ai_config(q)
    if cfg is None:
        return None, "AI provider not configured"
    if time.time() < (q.client.ai_cooldown_until or 0):
        return None, "cooling down after a rate-limit response"
    q.client.ai_calls = (q.client.ai_calls or 0) + 1
    text, err = await q.run(llm_chat, cfg, messages, max_tokens)
    q.client.ai_last_error = err
    if err and any(f"({c})" in err for c in (401, 402, 403, 429)):
        q.client.ai_cooldown_until = time.time() + 60
    return text, err


async def enrich_alerts(q, alerts):
    """Replace the template text of new alerts with AI-written text (max 2 per tick to
    protect the free-tier quota). Silent no-op without a working provider."""
    if not alerts or not ai_ready(q):
        return
    label = ai_config(q)["label"]
    for a in alerts[:2]:
        c = a["ctx"]
        messages = [
            {"role": "system", "content": (
                "You are a factory predictive-maintenance assistant. Turn a raw sensor breach into "
                "ONE short plain-language alert (max 2 sentences) a floor supervisor can act on. "
                "Use only the facts given.")},
            {"role": "user", "content": (
                f"Machine: {c['machine']}\nSensor: {c['sensor']}\nValue: {c['value']}{c['unit']}\n"
                f"Severity: {c['status']}\nThreshold crossed: {c['threshold']}{c['unit']}")},
        ]
        text, err = await ai_chat(q, messages, 250)
        if text:
            a["message"] = text
            a["ai"] = label
        if err:
            break   # do not keep retrying while the provider is failing


# --------------------------------------------------------------------------------------
# Telegram notifications (Bot API)
# --------------------------------------------------------------------------------------
# A Telegram bot cannot message a phone number. It needs a bot token (from @BotFather) and a
# chat ID (the person presses Start in the bot once). "Detect chat ID" in Settings finds it.

TELEGRAM_API = "https://api.telegram.org"
TG_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
TG_CHAT_RE = re.compile(r"^(-?\d{4,}|@[A-Za-z][A-Za-z0-9_]{4,})$")
MAX_TG_PER_TICK = 3                       # keeps a burst of faults under Telegram's rate limit
_BG_TASKS = set()                         # keeps fire-and-forget tasks alive until they finish

TG_TEXT = {
    "id": {
        "alert": "<b>{icon} {sev} - {machine}</b>\n{sensor}: <b>{reading}</b> (batas {limit})\n{message}\n\n"
                 "Work order <b>{wo}</b> menunggu engineer ({team}).",
        "assign": "<b>Work order {wo} ditugaskan</b>\n{machine} - {issue}\nEngineer: <b>{engineer}</b> "
                  "({team}, {shift})\nTindakan: {action}",
        "start": "<b>{wo} dikerjakan</b> oleh {engineer} - {machine}.",
        "complete": "<b>{wo} selesai</b> oleh {engineer}. {machine} kembali beroperasi.",
        "predict": "<b>Peringatan dini - {machine}</b>\nTren {driver} naik: diperkirakan mencapai {target} dalam "
                   "sekitar <b>{eta} menit</b> (health {health}/100). Disarankan inspeksi sebelum alert berbunyi.",
        "test": "Tes notifikasi dari Predictive Maintenance AI. Koneksi Telegram berhasil.",
    },
    "en": {
        "alert": "<b>{icon} {sev} - {machine}</b>\n{sensor}: <b>{reading}</b> (limit {limit})\n{message}\n\n"
                 "Work order <b>{wo}</b> is waiting for an engineer ({team}).",
        "assign": "<b>Work order {wo} assigned</b>\n{machine} - {issue}\nEngineer: <b>{engineer}</b> "
                  "({team}, {shift})\nAction: {action}",
        "start": "<b>{wo} in progress</b> - {engineer} started on {machine}.",
        "complete": "<b>{wo} completed</b> by {engineer}. {machine} is back in service.",
        "predict": "<b>Early warning - {machine}</b>\n{driver} is trending up: predicted to reach {target} in about "
                   "<b>{eta} min</b> (health {health}/100). Inspect before the alert fires.",
        "test": "Test notification from Predictive Maintenance AI. Telegram is connected.",
    },
}


def tg_ready(q):
    return bool(q.client.tg_enabled and q.client.tg_token and q.client.tg_chat_id)


def tg_text(q, kind, **ctx):
    ctx = {k: _esc(str(v)) for k, v in ctx.items()}
    return TG_TEXT.get(q.client.tg_lang, TG_TEXT["id"])[kind].format(**ctx)


def _tg_error(resp):
    code = resp.status_code
    return {
        401: "bot token is invalid",
        400: "chat not found - open your bot in Telegram and press Start first",
        403: "the bot cannot message this chat - press Start in the bot (or unblock it)",
        404: "bot token is invalid",
        429: "Telegram rate limit - slow down and retry",
    }.get(code, "request failed") + f" ({code})"


def tg_send_blocking(token, chat_id, text):
    """Blocking sendMessage call. Returns (ok, error). Error text never contains the token
    (requests' own exception messages include the URL, so only the exception type is reported)."""
    try:
        resp = requests.post(f"{TELEGRAM_API}/bot{token}/sendMessage", timeout=8, json={
            "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
    except requests.RequestException as e:
        return False, f"network error ({type(e).__name__}) - the app needs outbound access to api.telegram.org"
    if resp.status_code != 200:
        return False, _tg_error(resp)
    return True, None


def tg_detect_blocking(token):
    """Find the chat ID of the latest person who messaged the bot. Returns (chat_id, name, error)."""
    try:
        resp = requests.get(f"{TELEGRAM_API}/bot{token}/getUpdates", timeout=8)
    except requests.RequestException as e:
        return None, None, f"network error ({type(e).__name__}) - the app needs outbound access to api.telegram.org"
    if resp.status_code != 200:
        return None, None, _tg_error(resp)
    try:
        updates = resp.json().get("result", [])
    except Exception:
        return None, None, "unexpected response from Telegram"
    for u in reversed(updates):
        msg = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat and chat.get("id") is not None:
            name = chat.get("first_name") or chat.get("title") or chat.get("username") or "chat"
            return str(chat["id"]), name, None
    return None, None, "no messages found - open your bot in Telegram, press Start (or send any text), then try again"


async def tg_notify(q, text, chat_id=None):
    """Send one Telegram message without blocking the Wave event loop. Silent no-op when off."""
    if not tg_ready(q):
        return
    target = chat_id or q.client.tg_chat_id
    if q.client.tg_lock is None:
        q.client.tg_lock = asyncio.Lock()
    loop = asyncio.get_running_loop()
    async with q.client.tg_lock:                    # FIFO: messages arrive in the order they were raised
        ok, err = await loop.run_in_executor(None, tg_send_blocking, q.client.tg_token, target, text)
    if ok:
        q.client.tg_sent = (q.client.tg_sent or 0) + 1
        q.client.tg_last_error = None
    else:
        q.client.tg_last_error = err


def tg_fire(q, text, chat_id=None):
    """Fire-and-forget version for UI handlers, so a slow network never freezes the screen."""
    if tg_ready(q):
        task = asyncio.create_task(tg_notify(q, text, chat_id))
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)


def tg_alert_text(q, a, wo):
    icon = "\U0001F534" if a["severity"] == "Critical" else "\U0001F7E0"
    return tg_text(q, "alert", icon=icon, sev=a["severity"].upper(), machine=a["machine"], sensor=a["ctx"]["sensor"],
                   reading=wo["reading"], limit=wo["limit"], message=a["message"], wo=wo["id"], team=wo["team"])


async def notify_new_alerts(q, alerts):
    """One Telegram message per new alert / work order (called after AI enrichment so the text is final)."""
    if not (tg_ready(q) and q.client.tg_on_alert) or not alerts:
        return
    wanted = [a for a in alerts if q.client.tg_min_sev == "Warning" or a["severity"] == "Critical"]
    for a in wanted[:MAX_TG_PER_TICK]:
        wo = find_wo(q, a.get("wo"))
        if wo:
            await tg_notify(q, tg_alert_text(q, a, wo))
    extra = len(wanted) - MAX_TG_PER_TICK
    if extra > 0:
        await tg_notify(q, f"+{extra} more alert(s) - open the app / buka aplikasinya.")


async def notify_predictions(q):
    """Early warning: once per machine when the forecast turns High risk before the alert has fired."""
    if not (tg_ready(q) and q.client.tg_on_predict):
        return
    sent = q.client.tg_pred_sent
    for f in fleet_forecast(q):
        if f["risk"] == "Low":
            sent.discard(f["id"])
        elif (f["risk"] == "High" and f["status"] != "Critical" and f["eta"] is not None
              and f["id"] not in sent and len(sent) < 50):
            sent.add(f["id"])
            await tg_notify(q, tg_text(q, "predict", machine=f["machine"], driver=f["driver"],
                                       target=f["target"], eta=max(1, round(f["eta"])), health=f["health"]))


def tg_wo_text(q, kind, wo):
    eng = find_engineer(q, wo.get("assignee"))
    return tg_text(q, kind, wo=wo["id"], machine=wo["machine"], issue=wo["issue"], action=wo["action"],
                   engineer=eng["name"] if eng else "-", team=eng["team"] if eng else wo["team"],
                   shift=eng["shift"] if eng else "-")


# --------------------------------------------------------------------------------------
# Work orders & engineer roster
# --------------------------------------------------------------------------------------

def find_wo(q, wo_id):
    for a in q.client.actions:
        if a["id"] == wo_id:
            return a
    return None


def find_engineer(q, eng_id):
    for e in q.client.engineers:
        if e["id"] == eng_id:
            return e
    return None


def engineer_name(q, eng_id):
    e = find_engineer(q, eng_id) if eng_id else None
    return e["name"] if e else ""


def engineer_load(q, eng_id):
    """Number of work orders an engineer is currently working on."""
    return sum(1 for a in q.client.actions if a.get("assignee") == eng_id and a["state"] in ACTIVE_STATES)


def on_duty_engineers(q, wo=None):
    """Engineers on duty. With a work order: matching team first, then lowest workload."""
    people = [e for e in q.client.engineers if e["on_duty"]]
    if wo is not None:
        people.sort(key=lambda e: (e["team"] != wo["team"], engineer_load(q, e["id"]), e["name"]))
    return people


def recommend_engineer(q, wo):
    people = on_duty_engineers(q, wo)
    return people[0] if people else None


def short_team(team):
    return team.replace(" Team", "")


def wo_counts(q):
    acts = list(q.client.actions)
    return dict(
        total=len(acts),
        open=sum(1 for a in acts if a["state"] == "Open"),
        active=sum(1 for a in acts if a["state"] in ACTIVE_STATES),
        done=sum(1 for a in acts if a["state"] == "Completed"),
    )


def production_lines(q):
    pp = plant_prod(q)
    lines = [f"Plant OEE {pp['oee'] * 100:.1f}% (target {OEE_TARGET * 100:.0f}%), output {pp['attain'] * 100:.0f}% of plan, "
             f"downtime {pp['down_min']:.0f} min (cost {fmt_idr(pp['down_cost'])}), energy {pp['kwh']:.0f} kWh "
             f"({fmt_idr(pp['energy_cost'])})"]
    for mid, p in sorted((q.client.prod or {}).items(), key=lambda kv: kv[1]["oee"]):
        md = find_mdef(mid)
        lines.append(f"{md['name']}: OEE {p['oee'] * 100:.0f}% (A {p['a'] * 100:.0f} / P {p['p'] * 100:.0f} / "
                     f"Q {p['q'] * 100:.0f}), {p['kw']:.0f} kW")
    return lines


def maintenance_lines(q):
    overdue, soon, low = maintenance_summary(q)
    by_id = {m["id"]: m["name"] for m in MACHINE_DEFS}
    return ([f"PM overdue: {by_id.get(t['machine'], t['machine'])} - {t['task']} ({pm_due_text(t)})" for t in overdue]
            + [f"PM due soon: {by_id.get(t['machine'], t['machine'])} - {t['task']} ({pm_due_text(t)})" for t in soon]
            + [f"Spare part {part_status(p)}: {p['name']} (stock {p['stock']}, min {p['min_stock']}, "
               f"lead time {p['lead_days']} d)" for p in low])


def fleet_snapshot(q):
    """Compact, read-only picture of the plant for the copilot (and rule-based answers)."""
    machines = []
    for m in q.client.machines.values():
        mdef = find_mdef(m["id"])
        sensors = {}
        for sname, sstate in m["sensors"].items():
            cfg = mdef["sensors"][sname]
            if sensor_applicable(cfg):
                sensors[sname] = f"{sstate['value']}{cfg['unit']} ({sensor_status(sstate['value'], cfg)}; " \
                                 f"warn {cfg['warning']}, crit {cfg['critical']})"
        machines.append({"machine": m["name"], "line": m["line"], "status": m["status"], "sensors": sensors})
    return {
        "machines": machines,
        "recent_alerts": [f"{a['time']} {a['severity']} {a['machine']}: {a['message']}"
                          for a in list(q.client.alerts)[:5]],
        "recent_work_orders": [f"{a['id']} {a['machine']}: {a['issue']} -> {a['action']} "
                               f"({a['team']}, {a['state']}, assigned to "
                               f"{engineer_name(q, a.get('assignee')) or 'nobody yet'})"
                               for a in list(q.client.actions)[:5]],
        "engineers_on_duty": [f"{e['name']} ({e['team']}, {e['shift']}, {engineer_load(q, e['id'])} active WO)"
                              for e in q.client.engineers if e["on_duty"]],
        "failure_forecast": forecast_lines(q),
        "downtime_cost_avoided_estimate": fmt_rupiah(downtime_avoided(q)),
        "production": production_lines(q),
        "maintenance": maintenance_lines(q),
    }


COPILOT_SYSTEM = (
    "You are Maintenance Copilot for a manufacturing plant (HPE Private Cloud AI demo). "
    "Answer ONLY from the JSON fleet snapshot provided; never invent readings, machines or "
    "work orders - if the data does not contain the answer, say so. Be concise (max about "
    "150 words), use short bullet points when listing, and reply in the language of the "
    "question (English or Bahasa Indonesia). You are read-only: you cannot start work orders "
    "or change machines, only explain and recommend. The snapshot also holds production / OEE / energy "
    "figures and the preventive-maintenance and spare-parts status."
)


def rule_based_answer(q, question):
    """Deterministic fallback used in Simulation mode and whenever the AI call fails."""
    snap = fleet_snapshot(q)
    qn = question.lower()
    machines = snap["machines"]
    bad = [m for m in machines if m["status"] != "Normal"]
    crit = [m for m in machines if m["status"] == "Critical"]

    def machine_lines(ms):
        return "\n".join(f"- **{m['machine']}** ({m['line']}): {m['status']}" for m in ms)

    if any(k in qn for k in ("oee", "production", "produksi", "output", "energy", "energi", "kwh", "throughput")):
        pl = "\n".join(f"- {x}" for x in snap["production"])
        return f"Production and OEE (live demo model):\n{pl}"
    if any(k in qn for k in ("spare", "part", "stok", "stock", "inventory", "pm plan", "pm task", "jadwal pm", "overdue")):
        ml = "\n".join(f"- {x}" for x in snap["maintenance"]) or "- nothing overdue and all parts in stock"
        return f"Maintenance plan and spare parts:\n{ml}"
    if any(k in qn for k in ("predict", "forecast", "fail", "risk", "prediksi", "risiko", "rusak", "cost", "biaya")):
        fc = "\n".join(f"- {f}" for f in snap["failure_forecast"])
        return (f"Failure forecast (health score, risk, time to threshold):\n{fc}\n\n"
                f"Estimated downtime cost avoided so far: **{snap['downtime_cost_avoided_estimate']}** "
                f"(demo assumptions).")
    if any(k in qn for k in ("on duty", "on-duty", "engineer", "teknisi", "siapa", "who")):
        eng = "\n".join(f"- {e}" for e in snap["engineers_on_duty"]) or "- nobody is on duty"
        wo = "\n".join(f"- {w}" for w in snap["recent_work_orders"][:3]) or "- no work orders yet"
        return f"Engineers on duty:\n{eng}\n\nLatest work orders:\n{wo}"
    if any(k in qn for k in ("work order", "preventive", "action", "maintenance", "tindakan")):
        wo = "\n".join(f"- {w}" for w in snap["recent_work_orders"]) or "- none yet"
        return f"Recent automatically created work orders:\n{wo}"
    if any(k in qn for k in ("alert", "latest", "explain", "peringatan")):
        al = "\n".join(f"- {a}" for a in snap["recent_alerts"][:3]) or "- no alerts yet"
        return f"Latest alerts:\n{al}"
    if bad:
        first = crit[0] if crit else bad[0]
        return (f"**{len(bad)} of {len(machines)}** machines need attention "
                f"({len(crit)} critical):\n{machine_lines(bad)}\n\n"
                f"Inspect **{first['machine']}** first: it has the highest severity. "
                f"Check its work order in *Alerts & Maintenance*.")
    return (f"All **{len(machines)}** machines are Normal. No alerts or work orders are pending. "
            f"Use *Inject Anomaly* in Settings to simulate a fault.")


async def copilot_answer(q, question):
    """Return (answer_markdown, source_label)."""
    if ai_ready(q):
        snap = json.dumps(fleet_snapshot(q), ensure_ascii=False)
        history = [{"role": m["role"], "content": m["text"]}
                   for m in list(q.client.chat)[:-2][-6:] if not m.get("pending")]
        messages = ([{"role": "system", "content": COPILOT_SYSTEM + "\n\nFLEET SNAPSHOT:\n" + snap}]
                    + history + [{"role": "user", "content": question}])
        text, err = await ai_chat(q, messages, 800)
        if text:
            return text, f"{ai_config(q)['label']} - {ai_config(q)['model']}"
        return (rule_based_answer(q, question) +
                f"\n\n> AI unavailable ({err}). Showing the rule-based answer instead."), "Rule-based fallback"
    note = ""
    cfg_missing = q.client.ai_provider in ("openrouter", "custom") and ai_config(q) is None
    if cfg_missing:
        note = "\n\n> No API key / endpoint set yet - add it in *Settings* to use AI. Showing the rule-based answer."
    elif q.client.ai_provider in ("openrouter", "custom"):
        note = "\n\n> AI is cooling down after a rate limit. Showing the rule-based answer."
    return rule_based_answer(q, question) + note, "Rule-based (simulation)"


# --------------------------------------------------------------------------------------
# Layout / theme
# --------------------------------------------------------------------------------------

def finsight_theme():
    return ui.theme(name=THEME_NAME, primary=NAVY, text=TEXT, card=CARD_BG, page=PAGE_BG)


def _shell(main_zones):
    """Dark sidebar on the left, everything else in the 'main' column."""
    return [ui.layout(
        breakpoint="xs",
        zones=[ui.zone("body", direction="row", zones=[
            ui.zone("sidebar", size="260px", zones=[ui.zone("nav"), ui.zone("side_status")]),
            ui.zone("main", size="1", zones=[ui.zone("header"), ui.zone("banner")] + main_zones),
        ])],
    )]


def layout_for(kind):
    if kind == "overview":
        return _shell([
            ui.zone("kpis", direction="row", wrap="between"),
            ui.zone("charts", direction="row", wrap="start"),
            ui.zone("table"),
        ])
    if kind == "copilot":
        return _shell([
            ui.zone("columns", direction="row", zones=[
                ui.zone("left", size="68%"),
                ui.zone("right", size="32%"),
            ]),
        ])
    if kind == "workorders":
        return _shell([
            ui.zone("kpis", direction="row", wrap="between"),
            ui.zone("columns", direction="row", zones=[
                ui.zone("left", size="25%"),
                ui.zone("right", size="75%"),
            ]),
            ui.zone("full"),
        ])
    if kind == "production":
        return _shell([
            ui.zone("kpis", direction="row", wrap="between"),
            ui.zone("columns", direction="row", zones=[
                ui.zone("left", size="64%"),
                ui.zone("right", size="36%"),
            ]),
            ui.zone("full"),
        ])
    if kind == "maintenance":
        return _shell([
            ui.zone("kpis", direction="row", wrap="between"),
            ui.zone("columns", direction="row", zones=[
                ui.zone("left", size="50%"),
                ui.zone("right", size="50%"),
            ]),
            ui.zone("full"),
        ])
    if kind == "health":
        return _shell([
            ui.zone("kpis", direction="row", wrap="between"),
            ui.zone("columns", direction="row", zones=[
                ui.zone("left", size="64%"),
                ui.zone("right", size="36%"),
            ]),
        ])
    return _shell([   # settings
        ui.zone("kpis", direction="row", wrap="between"),
        ui.zone("columns", direction="row", zones=[
            ui.zone("left", size="50%"),
            ui.zone("right", size="50%"),
        ]),
        ui.zone("full"),
    ])


def set_layout(q: Q, kind: str):
    """Re-issue the meta card with the zone layout the active tab needs (Wave diffs it)."""
    q.page["meta"] = ui.meta_card(
        box="",
        title="Predictive Maintenance AI",
        theme=THEME_NAME,
        themes=[finsight_theme()],
        stylesheet=ui.inline_stylesheet(CSS),
        layouts=layout_for(kind),
        dialog=wo_dialog(q) if q.client.dialog_wo else None,
    )


def provider_status(q):
    """(dot colour, headline, footnote) for the sidebar status card and header pill."""
    p = q.client.ai_provider
    if p == "openrouter":
        if ai_config(q) is None:
            return AMBER, "OpenRouter - no key", "Add a free API key in Settings"
        if not ai_ready(q):
            return AMBER, "OpenRouter - cooling down", "Rate limit hit, using rule-based text"
        return EMERALD, f"OpenRouter - {ai_config(q)['model']}", "Prompts are sent to OpenRouter (external)"
    if p == "custom":
        if ai_config(q) is None:
            return AMBER, "PCAI endpoint - not set", "Add the endpoint URL in Settings"
        if not (q.client.llm_api_key or "").strip() and q.client.llm_base_url == DEFAULT_PCAI_BASE_URL:
            return AMBER, "PCAI - token needed", "Paste the MLIS access token in Settings"
        if "(401)" in (q.client.ai_last_error or "") or "(403)" in (q.client.ai_last_error or ""):
            return AMBER, "PCAI - token rejected", "Token invalid or expired, using rule-based text"
        if not ai_ready(q):
            return AMBER, "PCAI - paused", "Last call failed, using rule-based text for a moment"
        return EMERALD, f"PCAI - {ai_config(q)['model']}", "On-prem model: prompts stay inside your PCAI"
    return PURPLE, "Simulation mode", "Runs on HPE Private Cloud AI, no data leaves the plant"


# --------------------------------------------------------------------------------------
# Shared chrome: sidebar, header
# --------------------------------------------------------------------------------------

def render_nav(q: Q):
    q.page["nav"] = ui.nav_card(
        box="nav",
        title="Predictive Maintenance",
        subtitle="Manufacturing Intelligence",
        image=LOGO_URI,
        color="primary",
        value=q.client.active_tab,
        items=[ui.nav_group("Menu", items=[
            ui.nav_item(name="#overview", label="Overview", icon="ViewDashboard"),
            ui.nav_item(name="#production", label="Production & OEE", icon="Manufacturing"),
            ui.nav_item(name="#alerts", label="Alerts & Work Orders", icon="Warning"),
            ui.nav_item(name="#health", label="Predictive Health", icon="Health"),
            ui.nav_item(name="#maintenance", label="Parts & PM Plan", icon="Repair"),
            ui.nav_item(name="#copilot", label="AI Copilot (GenAI)", icon="Robot"),
            ui.nav_item(name="#settings", label="Settings & Demo", icon="Settings"),
        ])],
    )


def render_side_status(q: Q):
    color, head, foot = provider_status(q)
    q.page["side_status"] = ui.form_card(box="side_status", items=[
        ui.markup(
            f"<div style='font-size:12px;line-height:1.5'>"
            f"<div style='font-weight:600'><span style='color:{color}'>&#9679;</span> {head}</div>"
            f"<div style='color:{MUTED};margin-top:2px'>{foot}</div>"
            f"<div style='margin-top:10px;padding-top:8px;border-top:1px solid #E2E8F0'>"
            f"<div style='font-weight:600'>{shift_name()}</div>"
            f"<div style='color:{MUTED}'>{sum(1 for e in q.client.engineers if e['on_duty'])} engineers on duty "
            f"&middot; {len(q.client.machines)} machines</div></div></div>"
        ),
    ])


def render_header(q: Q):
    title, subtitle = TAB_META.get(q.client.active_tab, TAB_META["#overview"])
    live_color = EMERALD if q.client.running else MUTED
    live_text = "LIVE" if q.client.running else "PAUSED"
    color, head, _ = provider_status(q)
    pill = ("display:inline-block;padding:3px 10px;border-radius:999px;font-size:11px;"
            "font-weight:600;border:1px solid #E2E8F0;margin-left:8px;background:#fff")
    q.page["header"] = ui.form_card(box="header", items=[
        ui.markup(
            f"<div style='display:flex;justify-content:space-between;align-items:center'>"
            f"<div><div style='font-size:20px;font-weight:700'>{title}</div>"
            f"<div style='font-size:12px;color:{MUTED};margin-top:2px'>{subtitle}</div></div>"
            f"<div><span style='{pill}'><span style='color:{live_color}'>&#9679;</span> {live_text}</span>"
            f"<span style='{pill}'><span style='color:{color}'>&#9679;</span> {head}</span></div></div>"
        ),
    ])


def render_banner(q: Q):
    """Red call-to-action under the header while any work order still has no engineer."""
    waiting = [a for a in q.client.actions if a["state"] == "Open"]
    if not waiting:
        del q.page["banner"]
        return
    top = sorted(waiting, key=lambda a: a["severity"] != "Critical")[0]      # newest critical first
    color = RED if top["severity"] == "Critical" else AMBER
    more = f" &nbsp;<span style='color:{MUTED}'>(+{len(waiting) - 1} more waiting)</span>" if len(waiting) > 1 else ""
    q.page["banner"] = ui.form_card(box="banner", items=[
        ui.inline(justify="between", items=[
            ui.markup(
                f"<div style='border-left:4px solid {color};padding:2px 12px;font-size:13px;line-height:1.5'>"
                f"<b style='color:{color}'>{top['severity'].upper()}</b> &nbsp;{_esc(top['machine'])} - "
                f"{_esc(top['issue'])} &nbsp;<span style='color:{MUTED}'>({_esc(top['sensor'])} {_esc(top['reading'])}, "
                f"limit {_esc(top['limit'])})</span><br>"
                f"Work order <b>{top['id']}</b> was created automatically and is waiting for an engineer.{more}</div>"),
            ui.button(name="open_wo", label="Assign engineer", value=top["id"], primary=True, icon="Contact"),
        ]),
    ])


def render_chrome(q: Q):
    render_nav(q)
    render_side_status(q)
    render_header(q)
    render_banner(q)


# --------------------------------------------------------------------------------------
# Tab renderers
# --------------------------------------------------------------------------------------

def clear_body(q: Q):
    keys = ["alert_feed", "action_log", "duty_roster", "health_table", "health_insights", "machine_table", "settings_sim", "settings_tg",
            "settings_ai", "copilot_chat", "copilot_tryit", "copilot_how", "prod_table", "prod_trend", "prod_energy",
            "handover", "pm_card", "parts_card", "readiness"]
    keys += [f"kpi_{i}" for i in range(6)]
    for key in keys + list(q.client.chart_keys):
        del q.page[key]


def kpi_card(q, idx, label, value, caption, icon, color, width="15%"):
    q.page[f"kpi_{idx}"] = ui.form_card(
        box=ui.box("kpis", width=width),
        items=[ui.stats(items=[ui.stat(label=label, value=str(value), caption=caption,
                                       icon=icon, icon_color=color)])],
    )


def render_overview(q: Q):
    set_layout(q, "overview")
    clear_body(q)
    machines = q.client.machines
    n = len(machines)
    crit = sum(1 for m in machines.values() if m["status"] == "Critical")
    warn = sum(1 for m in machines.values() if m["status"] == "Warning")

    kpi_card(q, 0, "Machines", n, "monitored", "Manufacturing", SKY)
    kpi_card(q, 1, "Normal", n - crit - warn, "healthy", "CheckMark", EMERALD)
    kpi_card(q, 2, "Warning", warn, "needs watching", "Info", AMBER)
    kpi_card(q, 3, "Critical", crit, "act now", "Warning", RED)
    oee = plant_prod(q)["oee"]
    kpi_card(q, 4, "Plant OEE", f"{oee * 100:.0f}%", f"target {OEE_TARGET * 100:.0f}%", "Manufacturing",
             EMERALD if oee >= OEE_TARGET else AMBER if oee >= 0.7 else RED)
    wc = wo_counts(q)
    kpi_card(q, 5, "Work orders", wc["total"], f"{wc['open']} waiting for engineer", "Repair", SKY)

    status_color = {"Normal": EMERALD, "Warning": AMBER, "Critical": RED}
    charts = []
    for sname in CHART_SENSORS:                     # one row per sensor, one card per machine
        for m in machines.values():
            sstate = m["sensors"][sname]
            cfg = find_mdef(m["id"])["sensors"][sname]
            charts.append(ui.small_series_stat_card(
                box=ui.box("charts", width="18%", height="130px"),
                title=f"{MACHINE_SHORT.get(m['id'], m['name'])} - {SENSOR_LABELS[sname]}",
                value=f"{sstate['value']} {cfg['unit']}",
                plot_type="area",
                plot_category="t",
                plot_value="v",
                plot_color=status_color[sensor_status(sstate["value"], cfg)],
                plot_data=data(fields="t v",
                               rows=[[t, v] for t, v in zip(sstate["ticks"], sstate["history"])],
                               pack=True),
                plot_curve="linear",
            ))
    for i, c in enumerate(charts):
        q.page[f"chart_{i}"] = c
    q.client.chart_keys = [f"chart_{i}" for i in range(len(charts))]

    rows = []
    for m in machines.values():
        s = m["sensors"]
        u = find_mdef(m["id"])["sensors"]
        rows.append(ui.table_row(name=m["id"], cells=[
            m["name"], m["line"], m["status"], f"{q.client.prod[m['id']]['oee'] * 100:.0f}%",
            f"{s['temperature']['value']} {u['temperature']['unit']}",
            f"{s['vibration']['value']} {u['vibration']['unit']}",
            f"{s['pressure']['value']} {u['pressure']['unit']}",
            m["last_updated"].strftime("%H:%M:%S"),
        ]))
    q.page["machine_table"] = ui.form_card(
        box=ui.box("table", height="360px"),
        items=[
            ui.inline(justify="between", items=[
                ui.text_l("Machine Fleet Status"),
                ui.button(name="ask_ai_fleet", label="Ask AI", icon="Robot", primary=True),
            ]),
            ui.table(
                name="machine_table_tbl",
                columns=[
                    ui.table_column(name="name", label="Machine", min_width="220px"),
                    ui.table_column(name="line", label="Line"),
                    ui.table_column(name="status", label="Status", cell_type=ui.tag_table_cell_type(
                        name="status_tag",
                        tags=[ui.tag(label="Normal", color="$green"),
                              ui.tag(label="Warning", color="$orange"),
                              ui.tag(label="Critical", color="$red")])),
                    ui.table_column(name="oee", label="OEE"),
                    ui.table_column(name="temp", label="Temperature"),
                    ui.table_column(name="vib", label="Vibration"),
                    ui.table_column(name="pres", label="Pressure"),
                    ui.table_column(name="ts", label="Last Updated"),
                ],
                rows=rows,
            ),
        ],
    )


def _tag_cell(name, colors):
    return ui.tag_table_cell_type(name=name, tags=[ui.tag(label=k, color=v) for k, v in colors.items()])


def render_alerts(q: Q):
    set_layout(q, "workorders")
    clear_body(q)
    alerts = list(q.client.alerts)
    actions = list(q.client.actions)
    wc = wo_counts(q)

    kpi_card(q, 0, "Alerts", len(alerts), "plain-language, this session", "Warning", AMBER, "23%")
    kpi_card(q, 1, "Waiting for engineer", wc["open"], "click a work order to assign", "Contact",
             RED if wc["open"] else EMERALD, "23%")
    kpi_card(q, 2, "In the field", wc["active"], "assigned or in progress", "Repair", SKY, "23%")
    kpi_card(q, 3, "Completed", wc["done"], "work orders closed", "CheckMark", EMERALD, "23%")

    if alerts:
        content = "\n\n".join(
            f"**[{a['time']}] {a['severity'].upper()} - {a['machine']}**"
            + (f"  _(AI: {a['ai']})_" if a.get("ai") else "") + f"  \n{a['message']}"
            for a in alerts[:15]
        )
    else:
        content = "_No alerts yet. Enable Live Monitoring or use Inject Anomaly in Settings to generate one._"
    q.page["alert_feed"] = ui.markdown_card(
        box=ui.box("left", height="520px"),
        title="Alerts (most recent first)",
        content=content,
    )

    ordered = sorted(actions, key=lambda a: a["state"] == "Completed")      # unfinished first, newest first
    rows = [ui.table_row(name=a["id"], cells=[
        a["id"], a["state"], a["priority"], a["machine"], a["issue"], a["action"],
        engineer_name(q, a.get("assignee")) or "- unassigned -",
    ]) for a in ordered[:20]]
    items = [ui.text_l("Work Orders"),
             ui.text_s("Created automatically for every alert. Click a work order ID to choose the engineer on duty.")]
    flash = q.client.wo_flash
    if flash and time.time() < flash[2]:
        items.append(ui.message_bar(type=flash[0], text=flash[1]))
    items.append(
        ui.table(
            name="wo_table",
            columns=[
                ui.table_column(name="id", label="Work order", link=True, min_width="85px"),
                ui.table_column(name="state", label="Status", min_width="90px", cell_type=_tag_cell("state_tag", WO_STATE_COLORS)),
                ui.table_column(name="prio", label="Priority", min_width="100px", cell_type=_tag_cell("prio_tag", WO_PRIORITY_COLORS)),
                ui.table_column(name="machine", label="Machine", min_width="135px"),
                ui.table_column(name="issue", label="Issue", min_width="105px"),
                ui.table_column(name="action", label="Recommended action", min_width="180px", cell_overflow="wrap"),
                ui.table_column(name="who", label="Assigned to", min_width="120px"),
            ],
            rows=rows,
            height="390px",
        ) if rows else ui.text_m("_No work orders yet. A work order is created automatically for every alert._")
    )
    q.page["action_log"] = ui.form_card(box=ui.box("right", height="520px"), items=items)

    roster_rows = [ui.table_row(name=e["id"], cells=[
        e["name"], short_team(e["team"]), e["role"], e["shift"],
        "ON DUTY" if e["on_duty"] else "OFF DUTY", str(engineer_load(q, e["id"])),
    ]) for e in q.client.engineers]
    q.page["duty_roster"] = ui.form_card(box="full", items=[
        ui.text_l("Engineer Duty Roster"),
        ui.text_s("Template roster (sample_data/engineers.json). Click an engineer to switch on/off duty - "
                  "only engineers on duty can be assigned to a work order."),
        ui.table(
            name="roster_table",
            columns=[
                ui.table_column(name="name", label="Engineer", link=True, min_width="160px"),
                ui.table_column(name="team", label="Team", min_width="110px"),
                ui.table_column(name="role", label="Role", min_width="200px"),
                ui.table_column(name="shift", label="Shift", min_width="110px"),
                ui.table_column(name="duty", label="Duty", min_width="110px", cell_type=_tag_cell(
                    "duty_tag", {"ON DUTY": EMERALD, "OFF DUTY": MUTED})),
                ui.table_column(name="load", label="Active WOs", min_width="90px"),
            ],
            rows=roster_rows,
        ),
    ])


def wo_dialog(q: Q):
    """Pop-up to pick (or change) the engineer for one work order and move it through its lifecycle."""
    wo = find_wo(q, q.client.dialog_wo)
    if wo is None:
        return None
    state = wo["state"]
    sev_color = RED if wo["severity"] == "Critical" else AMBER
    st_color = WO_STATE_COLORS.get(state, MUTED)
    who = engineer_name(q, wo.get("assignee"))
    pill = "display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:700;color:#fff;background:"
    info = (
        f"<div style='font-size:13px;line-height:1.7'>"
        f"<span style='{pill}{sev_color}'>{_esc(wo['priority'])}</span> "
        f"<span style='{pill}{st_color}'>{_esc(state)}</span><br>"
        f"<b>Machine:</b> {_esc(wo['machine'])}<br>"
        f"<b>Reading:</b> {_esc(wo['sensor'])} {_esc(wo['reading'])} "
        f"<span style='color:{MUTED}'>(limit {_esc(wo['limit'])})</span><br>"
        f"<b>Issue:</b> {_esc(wo['issue'])}<br>"
        f"<b>Recommended action:</b> {_esc(wo['action'])}<br>"
        f"<b>Team:</b> {_esc(wo['team'])}"
        + (f"<br><b>Assigned to:</b> {_esc(who)}" if who else "") + "</div>"
    )
    items = [ui.markup(info)]
    kb = FAULT_KB.get(wo.get("sensor_key"))
    if kb:
        mid = next((m["id"] for m in MACHINE_DEFS if m["name"] == wo["machine"]), None)
        cost = DOWNTIME_COST_PER_HOUR.get(mid, 40_000_000) * kb["est_hours"]
        items.append(ui.markup(
            f"<div style='font-size:12px;color:{MUTED};margin:2px 0 6px'>Typical repair <b>{kb['est_hours']} h</b> "
            f"&middot; downtime if it fails unplanned about <b>{fmt_idr(cost)}</b></div>"))
        part_lines = []
        for it in kb["parts"]:
            p = find_part(q, it["part"])
            if p:
                st = part_status(p)
                part_lines.append(f"- {it['qty']} x {p['name']} - stock {p['stock']} ({st}), bin {p['location']}")
        items += [
            ui.expander(name="wo_kb_safety", label="Safety & lockout/tagout (LOTO)", expanded=True,
                        items=[ui.text(f"**{kb['safety']}**")]),
            ui.expander(name="wo_kb_causes", label="Likely causes", expanded=False,
                        items=[ui.text("\n".join(f"- {c}" for c in kb["causes"]))]),
            ui.expander(name="wo_kb_sop", label="Step-by-step procedure (SOP)", expanded=False,
                        items=[ui.text("\n".join(f"{i}. {x}" for i, x in enumerate(kb["sop"], 1)))]),
            ui.expander(name="wo_kb_parts", label="Spare parts for this repair", expanded=False,
                        items=[ui.text("\n".join(part_lines) or "No parts listed.")]),
        ]
        diag = wo.get("diag")
        if diag:
            items += [ui.separator(label="AI diagnosis"), ui.text(diag[0]), ui.text_s(f"_{diag[1]}_")]
        if wo.get("parts_used"):
            items.append(ui.text_s("Parts used: " + ", ".join(wo["parts_used"])))
    buttons = []
    if kb and state != "Completed":
        buttons.append(ui.button(name="wo_diag", value=wo["id"], label="AI diagnosis", icon="Robot"))

    if state in ("Open", "Assigned"):
        people = on_duty_engineers(q, wo)
        rec = people[0] if people else None
        if people:
            choices = [ui.choice(
                e["id"],
                f"{e['name']} - {short_team(e['team'])} - {e['shift']} - {engineer_load(q, e['id'])} active"
                + ("  (recommended)" if e is rec else ""))
                for e in people]
            ids = [e["id"] for e in people]
            items.append(ui.choice_group(
                name="engineer", label="Select an engineer on duty",
                value=wo["assignee"] if wo.get("assignee") in ids else rec["id"], choices=choices))
        else:
            items.append(ui.message_bar(type="warning", text=(
                "Nobody is on duty. Switch an engineer on duty in the Engineer Duty Roster first.")))
        buttons.append(ui.button(name="wo_assign", value=wo["id"], primary=state == "Open",
                                 label="Assign" if state == "Open" else "Reassign", disabled=not people))
    if state == "Assigned":
        buttons.append(ui.button(name="wo_start", value=wo["id"], label="Start work", primary=True))
    if state in ("Assigned", "In Progress"):
        buttons.append(ui.button(name="wo_complete", value=wo["id"], label="Mark completed",
                                 primary=state == "In Progress"))
    buttons.append(ui.button(name="wo_close", label="Close"))
    items.append(ui.buttons(items=buttons, justify="end"))

    return ui.dialog(title=f"{wo['id']} - {wo['issue']}", name="wo_dialog", width="640px",
                     closable=True, blocking=True, items=items, events=["dismissed"])


RISK_COLORS = {"Low": EMERALD, "Medium": AMBER, "High": RED}


def render_health(q: Q):
    set_layout(q, "health")
    clear_body(q)
    fc = fleet_forecast(q)
    n = len(fc)
    avg = round(sum(f["health"] for f in fc) / n) if n else 100
    at_risk = [f for f in fc if f["risk"] != "Low"]
    soon = sorted((f for f in fc if f["eta"] is not None), key=lambda f: f["eta"])
    avoided = downtime_avoided(q)

    kpi_card(q, 0, "Fleet health", f"{avg}/100", f"average of {n} machines", "Health",
             EMERALD if avg >= 80 else AMBER if avg >= 60 else RED, "23%")
    kpi_card(q, 1, "Machines at risk", len(at_risk),
             "flagged by the forecast" if at_risk else "all machines stable", "Warning",
             RED if any(f["risk"] == "High" for f in at_risk) else AMBER if at_risk else EMERALD, "23%")
    if soon:
        top = soon[0]
        kpi_card(q, 2, "Next predicted breach", MACHINE_SHORT.get(top["id"], top["machine"]),
                 eta_text(top) + f" ({top['driver']})", "Clock", AMBER if top["eta"] > 8 else RED, "23%")
    else:
        kpi_card(q, 2, "Next predicted breach", "None", "no sensor trending to a threshold", "Clock", EMERALD, "23%")
    kpi_card(q, 3, "Downtime avoided", fmt_rupiah(avoided),
             "estimate from completed work orders", "Savings", SKY, "23%")

    rows = [ui.table_row(name=f["id"], cells=[
        f["machine"], f"{f['health'] / 100:.2f}", f["risk"], f["driver"], eta_text(f),
    ]) for f in fc]
    q.page["health_table"] = ui.form_card(box=ui.box("left"), items=[
        ui.text_l("Failure Forecast"),
        ui.text_s("Health score and trend-based time to threshold for every machine, highest risk first."),
        ui.table(
            name="health_tbl",
            columns=[
                ui.table_column(name="machine", label="Machine", min_width="170px"),
                ui.table_column(name="health", label="Health", min_width="130px",
                                cell_type=ui.progress_table_cell_type(name="health_bar", color=SKY)),
                ui.table_column(name="risk", label="Risk", min_width="90px",
                                cell_type=_tag_cell("risk_tag", RISK_COLORS)),
                ui.table_column(name="driver", label="Main driver", min_width="110px"),
                ui.table_column(name="eta", label="Predicted", min_width="190px"),
            ],
            rows=rows,
        ),
    ])

    if at_risk:
        lines = []
        for f in at_risk[:3]:
            if f["risk"] == "High" and f["status"] == "Critical":
                act = "Critical now - make sure its work order has an engineer."
            elif f["eta"] is not None:
                act = f"{f['driver']} is trending up: inspect within about {max(1, round(f['eta']))} min, before it reaches {f['target']}."
            else:
                act = f"Health is {f['health']}: schedule a check on {f['driver']}."
            lines.append(f"**{f['machine']}** ({f['risk']} risk) - {act}")
        insights = "\n\n".join(lines)
    else:
        insights = ("All machines are healthy and no sensor is trending towards a threshold. "
                    "In Settings, start a **gradual degradation** to watch the forecast react before an alert fires.")
    q.page["health_insights"] = ui.form_card(box=ui.box("right"), items=[
        ui.text_l("Predictive Insights"),
        ui.text(insights),
        ui.button(name="ask_ai_risk", label="Explain with AI", primary=True, icon="Robot"),
        ui.separator(),
        ui.text_s(
            "**How it works.** Health = 100 minus sensor stress (how far each reading moved from baseline "
            "towards critical). The forecast fits a line through the latest readings and projects when it "
            "will cross the warning or critical threshold, on a demo time scale of 1 tick = 1 plant minute. "
            f"Downtime avoided = hours of unplanned stop each completed work order is assumed to prevent x "
            f"the machine's hourly downtime cost (demo assumptions, editable in the code)."),
    ])


PM_COLORS = {"Overdue": RED, "Due soon": AMBER, "On track": EMERALD}
PART_COLORS = {"OK": EMERALD, "Reorder": AMBER, "Out of stock": RED, "On order": SKY}


def handover_text(q):
    """Rule-based shift handover note built from live data (an AI rewrite is optional)."""
    pp = plant_prod(q)
    wc = wo_counts(q)
    at_risk = [f for f in fleet_forecast(q) if f["risk"] != "Low"]
    overdue, soon, low = maintenance_summary(q)
    by_id = {m["id"]: m["name"] for m in MACHINE_DEFS}
    lines = [
        f"**{shift_name()} handover** - generated {datetime.now():%H:%M}",
        "",
        f"**Production** - OEE {pp['oee'] * 100:.1f}% (target {OEE_TARGET * 100:.0f}%), output {pp['attain'] * 100:.0f}% of plan, "
        f"downtime {pp['down_min']:.0f} min ({fmt_idr(pp['down_cost'])}), energy {pp['kwh']:.0f} kWh ({fmt_idr(pp['energy_cost'])}).",
        f"**Maintenance** - {wc['open']} work orders waiting for an engineer, {wc['active']} in the field, {wc['done']} completed.",
        "",
        "**Priorities for the next shift**",
    ]
    todo = []
    for a in q.client.actions:
        if a["state"] == "Open":
            todo.append(f"Assign an engineer to {a['id']} ({a['machine']} - {a['issue']}).")
    for f in at_risk[:3]:
        todo.append(f"Watch {f['machine']}: {f['risk']} risk, {f['driver']} - {eta_text(f)}.")
    for t in overdue[:3]:
        todo.append(f"Overdue PM: {t['task']} on {by_id.get(t['machine'], t['machine'])} ({pm_due_text(t)}).")
    if low:
        todo.append("Reorder spare parts: " + ", ".join(p["name"] for p in low[:4]) + ".")
    if not todo:
        todo.append("No open items. Keep monitoring.")
    lines += [f"{i}. {t}" for i, t in enumerate(todo[:7], 1)]
    return "\n".join(lines)


async def handover_ai(q):
    """Ask the model to turn the data-driven draft into a short, readable handover note."""
    draft = handover_text(q)
    if not ai_ready(q):
        return None, "AI not available - showing the data-driven note"
    messages = [
        {"role": "system", "content": (
            "You are a plant shift supervisor writing the handover note for the next shift. Rewrite the draft "
            "as a clear note of at most 140 words in short bullets. Use ONLY the facts in the draft and the JSON "
            "snapshot; do not invent numbers. Reply in English.")},
        {"role": "user", "content": "DRAFT:\n" + draft + "\n\nSNAPSHOT:\n" + json.dumps(fleet_snapshot(q), ensure_ascii=False)},
    ]
    text, err = await ai_chat(q, messages, 700)
    return (text, f"{ai_config(q)['label']} - {ai_config(q)['model']}") if text else (None, f"AI unavailable ({err})")


async def diagnose_wo(q, wo):
    kb = FAULT_KB.get(wo.get("sensor_key")) or {}
    causes = kb.get("causes", [])
    sop = kb.get("sop", [])
    if ai_ready(q):
        mach = next((m for m in q.client.machines.values() if m["name"] == wo["machine"]), None)
        ctx = dict(work_order=dict(id=wo["id"], machine=wo["machine"], issue=wo["issue"], reading=wo["reading"],
                                   limit=wo["limit"], severity=wo["severity"]),
                   likely_causes=causes, standard_checks=sop[:4],
                   current_status=mach["status"] if mach else "unknown")
        messages = [
            {"role": "system", "content": (
                "You are a reliability engineer. Given a work order and its knowledge-base entry, reply with: "
                "1) the most likely root cause, 2) two quick checks to confirm it, 3) whether the machine can keep "
                "running. Use ONLY the given context. Max 90 words, English.")},
            {"role": "user", "content": json.dumps(ctx, ensure_ascii=False)},
        ]
        text, err = await ai_chat(q, messages, 600)
        if text:
            return text, f"{ai_config(q)['label']} - {ai_config(q)['model']}"
    keep = ("Stop the machine safely and repair now." if wo["severity"] == "Critical"
            else "It can keep running under close watch until the planned stop.")
    if not causes:
        return "No knowledge-base entry for this fault yet. Follow the recommended action.", "Rule-based"
    return (f"**Most likely cause:** {causes[0]}.\n\n**Confirm with:** {sop[1] if len(sop) > 1 else sop[0]}; "
            f"then {sop[2].lower() if len(sop) > 2 else 'inspect visually'}.\n\n**Running:** {keep}"), "Rule-based (knowledge base)"


def render_production(q: Q):
    set_layout(q, "production")
    clear_body(q)
    pp = plant_prod(q)
    ok = pp["oee"] >= OEE_TARGET
    kpi_card(q, 0, "Plant OEE", f"{pp['oee'] * 100:.1f}%",
             f"target {OEE_TARGET * 100:.0f}% - " + ("on target" if ok else f"{(OEE_TARGET - pp['oee']) * 100:.1f} pts below"),
             "Manufacturing", EMERALD if ok else AMBER if pp["oee"] >= 0.7 else RED, "23%")
    kpi_card(q, 1, "Output vs plan", f"{pp['attain'] * 100:.0f}%", "units produced vs the OEE-target plan",
             "CheckMark", EMERALD if pp["attain"] >= 1 else AMBER if pp["attain"] >= 0.85 else RED, "23%")
    kpi_card(q, 2, "Downtime", f"{pp['down_min']:.0f} min", f"cost {fmt_idr(pp['down_cost'])} this shift",
             "Clock", AMBER, "23%")
    kpi_card(q, 3, "Energy", f"{pp['kwh']:,.0f} kWh", f"{fmt_idr(pp['energy_cost'])} at Rp {ENERGY_TARIFF:,}/kWh",
             "Savings", SKY, "23%")

    rows = []
    for mid, p in sorted(q.client.prod.items(), key=lambda kv: kv[1]["oee"]):
        md = find_mdef(mid)
        rows.append(ui.table_row(name=mid, cells=[
            MACHINE_SHORT.get(mid, md["name"]), md["line"], f"{p['oee']:.2f}",
            f"{p['a'] * 100:.0f}%", f"{p['p'] * 100:.0f}%", f"{p['q'] * 100:.0f}%",
            f"{p['units']:,.0f} {md['unit']}", f"{p['kw']:.0f} kW",
        ]))
    q.page["prod_table"] = ui.form_card(box=ui.box("left"), items=[
        ui.text_l("OEE by Machine"),
        ui.text_s("OEE = Availability x Performance x Quality. Lowest first. A degrading sensor lowers speed and "
                  "quality, and a fault lowers availability, so the predictive view and the production view agree."),
        ui.table(
            name="prod_tbl",
            columns=[
                ui.table_column(name="machine", label="Machine", min_width="105px"),
                ui.table_column(name="line", label="Line", min_width="105px", cell_overflow="wrap"),
                ui.table_column(name="oee", label="OEE", min_width="100px",
                                cell_type=ui.progress_table_cell_type(name="oee_bar", color=EMERALD)),
                ui.table_column(name="a", label="Avail.", min_width="60px"),
                ui.table_column(name="p", label="Perf.", min_width="60px"),
                ui.table_column(name="q", label="Qual.", min_width="60px"),
                ui.table_column(name="out", label="Output", min_width="95px"),
                ui.table_column(name="kw", label="Power", min_width="60px"),
            ],
            rows=rows,
        ),
    ])

    hist = [p["hist"] for p in q.client.prod.values()]
    n = min(len(h) for h in hist)
    weights = [find_mdef(mid)["rate_per_min"] for mid in q.client.prod]
    series = [round(sum(h[i] * w for h, w in zip(hist, weights)) / sum(weights), 1) for i in range(n)]
    q.page["prod_trend"] = ui.plot_card(
        box=ui.box("right", height="230px"),
        title="Plant OEE trend (%, live)",
        data=data(fields="t oee", rows=[[i, v] for i, v in enumerate(series)], pack=True),
        plot=ui.plot([ui.mark(type="area", x="=t", y="=oee", y_min=0, y_max=100, curve="smooth",
                              color=EMERALD)]),
    )

    total = sum(p["kwh"] for p in q.client.prod.values()) or 1
    bars = []
    for mid, p in sorted(q.client.prod.items(), key=lambda kv: -kv[1]["kwh"]):
        md = find_mdef(mid)
        high = _intensity(md, p) > 1.2 * p["int0"]
        color = AMBER if high else SKY
        flag = f" <span style='color:{AMBER};font-weight:600'>high energy per unit</span>" if high else ""
        bars.append(
            f"<div style='margin:7px 0;font-size:12px'><div style='display:flex;justify-content:space-between'>"
            f"<span>{_esc(MACHINE_SHORT.get(mid, md['name']))}{flag}</span><span style='color:{MUTED}'>"
            f"{p['kwh']:.0f} kWh &middot; {fmt_idr(p['kwh'] * ENERGY_TARIFF)}</span></div>"
            f"<div style='background:#E2E8F0;border-radius:4px;height:7px;margin-top:3px'>"
            f"<div style='width:{p['kwh'] / total * 100:.0f}%;height:7px;border-radius:4px;background:{color}'></div></div></div>")
    q.page["prod_energy"] = ui.form_card(box=ui.box("right"), items=[
        ui.text_l("Energy by Machine"),
        ui.text_s("Share of plant kWh. Amber = more energy per unit than normal, an early sign of friction or leaks."),
        ui.markup("".join(bars)),
    ])

    items = [
        ui.text_l("Shift Handover"),
        ui.text_s("Generated from live production, work-order, forecast, PM and spare-parts data."),
        ui.text(handover_text(q)),
    ]
    ai = q.client.handover_ai
    if ai:
        items += [ui.separator(label="AI version"), ui.text(ai[0] or ai[1]), ui.text_s(f"_{ai[1]}_")]
    items.append(ui.button(name="handover_ai", label="Rewrite with AI", icon="Robot", primary=True))
    q.page["handover"] = ui.form_card(box="full", items=items)


def render_maintenance(q: Q):
    set_layout(q, "maintenance")
    clear_body(q)
    overdue, soon, low = maintenance_summary(q)
    value = sum(p["stock"] * p["unit_cost"] for p in q.client.parts)
    kpi_card(q, 0, "PM overdue", len(overdue), "preventive tasks past due", "Warning",
             RED if overdue else EMERALD, "23%")
    kpi_card(q, 1, "PM due in 7 days", len(soon), "plan the work now", "Clock", AMBER if soon else EMERALD, "23%")
    kpi_card(q, 2, "Parts to reorder", len(low), "at or below minimum stock", "Repair",
             AMBER if low else EMERALD, "23%")
    kpi_card(q, 3, "Spares value", fmt_idr(value), "current stock at cost", "Savings", SKY, "23%")

    by_id = {m["id"]: m for m in MACHINE_DEFS}
    flash = q.client.mt_flash
    flash_item = [ui.message_bar(type=flash[0], text=flash[1])] if flash and time.time() < flash[2] else []

    pm_rows = [ui.table_row(name=t["id"], cells=[
        t["task"], MACHINE_SHORT.get(t["machine"], t["machine"]), pm_due_text(t), pm_status(t), f"{t['interval_days']} d",
    ]) for t in sorted(q.client.pm, key=lambda t: t["due_in_days"])]
    q.page["pm_card"] = ui.form_card(box=ui.box("left"), items=[
        ui.text_l("Preventive Maintenance Plan"),
        ui.text_s("Template plan (sample_data/pm_plan.json), most urgent first. Click a task to mark it done."),
        *flash_item,
        ui.table(
            name="pm_tbl",
            columns=[
                ui.table_column(name="task", label="Task", link=True, min_width="190px", cell_overflow="wrap"),
                ui.table_column(name="machine", label="Machine", min_width="100px"),
                ui.table_column(name="due", label="Due", min_width="95px"),
                ui.table_column(name="status", label="Status", min_width="85px", cell_type=_tag_cell("pm_tag", PM_COLORS)),
                ui.table_column(name="every", label="Every", min_width="55px"),
            ],
            rows=pm_rows,
            height="420px",
        ),
    ])

    order = {"Out of stock": 0, "Reorder": 1, "On order": 2, "OK": 3}
    part_rows = [ui.table_row(name=p["id"], cells=[
        p["name"], f"{p['stock']} (min {p['min_stock']})", part_status(p),
        f"{p['lead_days']} d", p["location"],
    ]) for p in sorted(q.client.parts, key=lambda p: (order[part_status(p)], p["name"]))]
    q.page["parts_card"] = ui.form_card(box=ui.box("right"), items=[
        ui.text_l("Spare-Parts Inventory"),
        ui.text_s("Click a part to order its reorder quantity; click it again when the delivery arrives. "
                  "Completing a work order consumes the parts that repair normally needs."),
        ui.table(
            name="parts_tbl",
            columns=[
                ui.table_column(name="part", label="Part", link=True, min_width="190px", cell_overflow="wrap"),
                ui.table_column(name="stock", label="Stock", min_width="85px"),
                ui.table_column(name="status", label="Status", min_width="90px", cell_type=_tag_cell("part_tag", PART_COLORS)),
                ui.table_column(name="lead", label="Lead", min_width="50px"),
                ui.table_column(name="loc", label="Bin", min_width="60px"),
            ],
            rows=part_rows,
            height="420px",
        ),
    ])

    rrows = []
    for f in fleet_forecast(q):
        kb = FAULT_KB.get(f.get("driver_key")) or {}
        need = [(find_part(q, it["part"]), it["qty"]) for it in kb.get("parts", [])]
        need = [(p, n) for p, n in need if p]
        short = [p for p, n in need if p["stock"] < n]
        ordered = [p for p in short if p.get("on_order")]
        ready = "Ready" if not short else "On order" if len(ordered) == len(short) else "Short"
        rrows.append(ui.table_row(name=f["id"], cells=[
            f["machine"], f["risk"], f["driver"], kb.get("issue", "-"),
            ", ".join(f"{n} x {p['name']} ({p['stock']} in stock)" for p, n in need) or "-", ready,
        ]))
    q.page["readiness"] = ui.form_card(box="full", items=[
        ui.text_l("Spare-Parts Readiness by Machine"),
        ui.text_s("Links the failure forecast to the warehouse: for each machine's main risk driver, are the parts "
                  "its repair normally needs in stock?"),
        ui.table(
            name="ready_tbl",
            columns=[
                ui.table_column(name="machine", label="Machine", min_width="190px", cell_overflow="wrap"),
                ui.table_column(name="risk", label="Risk", min_width="80px", cell_type=_tag_cell("rk_tag", RISK_COLORS)),
                ui.table_column(name="driver", label="Main driver", min_width="100px"),
                ui.table_column(name="mode", label="Likely failure mode", min_width="150px"),
                ui.table_column(name="parts", label="Parts normally needed", min_width="320px", cell_overflow="wrap"),
                ui.table_column(name="ready", label="Readiness", min_width="95px", cell_type=_tag_cell(
                    "ready_tag", {"Ready": EMERALD, "Short": RED, "On order": SKY})),
            ],
            rows=rrows,
        ),
    ])


def render_copilot(q: Q):
    set_layout(q, "copilot")
    clear_body(q)
    chat = list(q.client.chat)[-8:]

    items = []
    if not chat:
        items.append(ui.markup(
            f"<div style='text-align:center;padding:36px 12px 20px'>"
            f"<div style='display:inline-block;width:46px;height:46px;border-radius:12px;"
            f"background:linear-gradient(135deg,{PURPLE},{EMERALD});color:#fff;font-size:22px;"
            f"line-height:46px'>&#10022;</div>"
            f"<div style='font-size:18px;font-weight:700;margin-top:10px'>AI Copilot</div>"
            f"<div style='font-size:12px;color:{MUTED};margin-top:6px'>Ask about machine health, alerts, "
            f"work orders or failure risk. Answers use the live fleet data.</div></div>"
        ))
    for m in chat:
        if m["role"] == "user":
            items.append(ui.markup(
                f"<div style='display:flex;justify-content:flex-end;margin:8px 0'>"
                f"<div style='background:{NAVY};color:#fff;padding:8px 14px;border-radius:14px;"
                f"font-size:13px;max-width:80%'>{_esc(m['text'])}</div></div>"))
        else:
            items.append(ui.text(m["text"]))
            if m.get("src"):
                items.append(ui.text_s(f"_{m['src']}_"))
    items += [
        ui.separator(),
        ui.inline(items=[
            ui.textbox(name="copilot_input", placeholder="Ask about machines, alerts, work orders or failure risk...",
                       width="62%"),
            ui.button(name="copilot_send", label="Send", primary=True, icon="Send"),
            ui.button(name="copilot_clear", label="Clear"),
        ]),
        ui.text_s("Answers are generated from a read-only snapshot of the fleet. The copilot cannot change machines."),
    ]
    q.page["copilot_chat"] = ui.form_card(box=ui.box("left", height="640px"), items=items)

    q.page["copilot_how"] = ui.markdown_card(
        box=ui.box("right"),
        title="How it works",
        content=(
            "1. **Your question** - natural language, English or Bahasa\n"
            "2. **Live context** - sensors, alerts, work orders, failure forecast\n"
            "3. **LLM** - OpenRouter free model, your own endpoint, or simulation\n"
            "4. **Guard rails** - read-only snapshot, no actions executed\n"
            "5. **Answer** - short, grounded in the data, with the source shown"
        ),
    )
    q.page["copilot_tryit"] = ui.form_card(box=ui.box("right"), items=[
        ui.text_l("Try these"),
        *[ui.button(name="copilot_suggest", label=s, value=s, link=True) for s in COPILOT_SUGGESTIONS],
    ])


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def render_settings(q: Q):
    set_layout(q, "columns")
    clear_body(q)
    machine_choices = [ui.choice(m["id"], m["name"]) for m in MACHINE_DEFS]
    sensor_choices = [ui.choice(k, v) for k, v in SENSOR_LABELS.items()]

    q.page["settings_sim"] = ui.form_card(
        box=ui.box("left", height="560px"),
        items=[
            ui.text_l("Demo Controls"),
            ui.toggle(name="toggle_live", label="Live Monitoring (auto-refresh every 3s)",
                      value=q.client.running, trigger=True),
            ui.separator(),
            ui.text_m("Inject an anomaly on demand for a reliable, presenter-controlled demo:"),
            ui.dropdown(name="inject_machine", label="Machine", choices=machine_choices,
                        value=q.client.inject_machine),
            ui.dropdown(name="inject_sensor", label="Sensor", choices=sensor_choices,
                        value=q.client.inject_sensor),
            ui.buttons(items=[
                ui.button(name="inject_anomaly", label="Inject Anomaly", primary=True),
                ui.button(name="reset_sim", label="Reset Simulation"),
            ]),
            ui.separator(),
            ui.text_m("Or let the selected sensor degrade slowly and watch the forecast warn you first:"),
            ui.buttons(items=[
                ui.button(name="start_drift", label="Start gradual degradation", icon="Timer"),
                ui.button(name="stop_drift", label="Stop", disabled=not q.client.drift),
            ]),
            ui.text_s(("Degrading now: " + ", ".join(
                f"{find_mdef(m)['name']} / {SENSOR_LABELS.get(sn, sn)}" for m, sn in q.client.drift.items()))
                if q.client.drift else "No degradation running. It starts Live Monitoring if it is off."),
        ],
    )

    provider = q.client.ai_provider
    ai_items = [
        ui.text_l("AI Provider"),
        ui.dropdown(name="ai_provider", label="Provider", trigger=True, value=provider,
                    choices=[ui.choice(k, v) for k, v in PROVIDER_LABELS.items()]),
    ]
    if provider == "openrouter":
        ai_items += [
            ui.textbox(name="openrouter_key", label="OpenRouter API key", password=True,
                       value=q.client.openrouter_key or "", placeholder="sk-or-v1-..."),
            ui.combobox(name="openrouter_model", label="Model (type any ID, free ones end in :free)",
                        choices=OPENROUTER_MODEL_PRESETS,
                        value=q.client.openrouter_model or DEFAULT_OPENROUTER_MODEL),
            ui.link(label="Get a free OpenRouter API key", path="https://openrouter.ai/keys", target="_blank"),
        ]
    elif provider == "custom":
        ai_items += [
            ui.textbox(name="llm_base_url", label="Endpoint URL (/v1/chat/completions is appended)",
                       value=q.client.llm_base_url or "", placeholder="https://<model>.serving.ai-application.<domain>"),
            ui.textbox(name="llm_model", label="Model name", value=q.client.llm_model or "",
                       placeholder=DEFAULT_PCAI_MODEL),
            ui.textbox(name="llm_api_key", label="Access token (MLIS / API key)", password=True,
                       value=q.client.llm_api_key or "", placeholder="paste the endpoint token here"),
            ui.checkbox(name="llm_verify_ssl", label="Verify TLS certificate (untick for lab endpoints with a self-signed CA)",
                        value=q.client.llm_verify_ssl is not False),
        ]
    if provider in ("openrouter", "custom"):
        ai_items.append(ui.buttons(items=[
            ui.button(name="save_ai", label="Save", primary=True),
            ui.button(name="ai_test", label="Test connection"),
        ]))
    res = q.client.ai_flash
    if res:
        ai_items.append(ui.message_bar(type=res[0], text=res[1]))
        q.client.ai_flash = None
    if provider == "openrouter":
        ai_items.append(ui.message_bar(type="info", text=(
            "Free models are rate limited (about 20 requests/min and 50/day, more after a one-time credit "
            "top-up) and model IDs change - use openrouter/free to let OpenRouter pick one. "
            "The key is kept in memory for this browser session only.")))
    elif provider == "custom":
        ai_items.append(ui.message_bar(type="info", text=(
            "Prompts go to your own PCAI / MLIS endpoint, so plant data stays on-prem. Lab endpoints with a "
            "self-signed certificate need 'Verify TLS certificate' unticked. The token is kept in memory for this "
            "browser session only; for a permanent setup inject it as the LLM_API_KEY secret (Helm chart does this).")))
    elif provider == "simulation":
        ai_items.append(ui.message_bar(type="info", text=(
            "Simulation is free and needs no key. Choose HPE PCAI (your on-prem Qwen model) or OpenRouter "
            "to get AI-written alerts and the AI Copilot.")))
    ai_items.append(ui.text_s(f"AI calls this session: {q.client.ai_calls or 0}"))
    q.page["settings_ai"] = ui.form_card(box=ui.box("right", height="560px"), items=ai_items)

    tg_items = [
        ui.text_l("Telegram Notifications"),
        ui.toggle(name="tg_enabled", label="Send real notifications to Telegram", value=bool(q.client.tg_enabled),
                  trigger=True),
        ui.inline(items=[
            ui.textbox(name="tg_token", label="Bot token (from @BotFather)", password=True, width="42%",
                       value=q.client.tg_token or "", placeholder="123456789:AAH..."),
            ui.textbox(name="tg_chat_id", label="Chat ID", width="22%",
                       value=q.client.tg_chat_id or "", placeholder="e.g. 123456789"),
            ui.button(name="tg_detect", label="Detect chat ID", icon="Search"),
        ], justify="start", align="end"),
        ui.inline(items=[
            ui.checkbox(name="tg_on_alert", label="New alert / work order", value=bool(q.client.tg_on_alert)),
            ui.checkbox(name="tg_on_assign", label="Engineer assigned", value=bool(q.client.tg_on_assign)),
            ui.checkbox(name="tg_on_progress", label="Work started / completed", value=bool(q.client.tg_on_progress)),
            ui.checkbox(name="tg_on_predict", label="Early warning (forecast)", value=bool(q.client.tg_on_predict)),
        ], justify="start"),
        ui.inline(items=[
            ui.dropdown(name="tg_min_sev", label="Alert severity", width="220px", value=q.client.tg_min_sev,
                        choices=[ui.choice("Warning", "Warning and Critical"), ui.choice("Critical", "Critical only")]),
            ui.dropdown(name="tg_lang", label="Message language", width="220px", value=q.client.tg_lang,
                        choices=[ui.choice("id", "Bahasa Indonesia"), ui.choice("en", "English")]),
        ], justify="start"),
        ui.buttons(items=[
            ui.button(name="tg_save", label="Save", primary=True),
            ui.button(name="tg_test", label="Send test message"),
        ]),
    ]
    if q.client.tg_flash:
        tg_items.append(ui.message_bar(type=q.client.tg_flash[0], text=q.client.tg_flash[1]))
        q.client.tg_flash = None
    tg_items.append(ui.text_s(
        "**How to set it up.** 1) In Telegram open @BotFather, send /newbot and copy the bot token. "
        "2) Open your new bot and press Start. 3) Paste the token here and click Detect chat ID (a bot cannot "
        "use a phone number, it needs this ID). 4) Click Send test message. Engineers can also get their own "
        "message: add their chat ID as \"telegram_chat_id\" in sample_data/engineers.json. The token is kept in "
        "memory for this browser session only. The app needs outbound access to api.telegram.org."
        + (f" Messages sent this session: {q.client.tg_sent or 0}." if q.client.tg_sent else "")
        + (f" Last error: {q.client.tg_last_error}." if q.client.tg_last_error else "")))
    q.page["settings_tg"] = ui.form_card(box="full", items=tg_items)


async def render_active_tab(q: Q):
    tab = q.client.active_tab
    render_chrome(q)
    if tab == "#alerts":
        render_alerts(q)
    elif tab == "#health":
        render_health(q)
    elif tab == "#production":
        render_production(q)
    elif tab == "#maintenance":
        render_maintenance(q)
    elif tab == "#copilot":
        render_copilot(q)
    elif tab == "#settings":
        render_settings(q)
    else:
        render_overview(q)
    await q.page.save()


# --------------------------------------------------------------------------------------
# Live monitoring loop
# --------------------------------------------------------------------------------------

def apply_tick(q: Q, force_machine=None, force_sensor=None):
    """Advance every machine one tick (or inject a fault) and file alerts / work orders.
    Returns the new alerts so the caller can enrich them with AI text."""
    q.client.tick_count += 1
    new_alerts = []
    for mid, machine in q.client.machines.items():
        mdef = find_mdef(mid)
        fs = force_sensor if (force_machine == mid) else None
        if force_machine is not None and force_machine != mid:
            continue                                 # injection touches one machine only
        alerts, actions = evaluate_machine(q, machine, mdef, force_sensor=fs)
        update_production(q, machine, mdef)
        for a in alerts:
            a["id"] = f"AL-{int(time.time() * 1000) % 1000000}-{mid}"
            q.client.alerts.appendleft(a)
        for act in actions:
            q.client.actions.appendleft(act)
        new_alerts += alerts
    return new_alerts


async def live_loop(q: Q):
    while q.client.running:
        try:
            new_alerts = apply_tick(q)
            await enrich_alerts(q, new_alerts)
            await notify_new_alerts(q, new_alerts)
            await notify_predictions(q)
            if q.client.dialog_wo:                   # a work-order dialog is open: keep the UI still
                pass
            elif q.client.active_tab in LIVE_TABS:
                await render_active_tab(q)
            else:                                    # do not rebuild forms the user may be typing in
                render_header(q)
                render_banner(q)
                await q.page.save()
            await asyncio.sleep(TICK_SECONDS)
        except asyncio.CancelledError:
            break
        except Exception:
            break                                    # client disconnected; stop quietly
    q.client.running = False


# --------------------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------------------

@on("#overview")
@on("#alerts")
@on("#health")
@on("#production")
@on("#maintenance")
@on("#copilot")
@on("#settings")
async def on_tab(q: Q):
    # q.args['#'] arrives WITHOUT the leading '#'; tab names use the '#'-prefixed form.
    if q.args["#"]:
        q.client.active_tab = "#" + q.args["#"]
    await render_active_tab(q)


@on("toggle_live")
async def toggle_live(q: Q):
    q.client.running = bool(q.args.toggle_live)
    if q.client.running:
        asyncio.create_task(live_loop(q))
    await render_active_tab(q)


def _save_ai_fields(q: Q):
    """Copy whatever AI fields are present in the submitted form into client state."""
    for arg, attr in (("openrouter_key", "openrouter_key"), ("openrouter_model", "openrouter_model"),
                      ("llm_base_url", "llm_base_url"), ("llm_model", "llm_model"),
                      ("llm_api_key", "llm_api_key"), ("llm_verify_ssl", "llm_verify_ssl")):
        if q.args[arg] is not None:
            setattr(q.client, attr, q.args[arg])


@on("ai_provider")
async def on_ai_provider(q: Q):
    _save_ai_fields(q)
    if q.args.ai_provider in PROVIDER_LABELS:
        q.client.ai_provider = q.args.ai_provider
        q.client.ai_cooldown_until = 0
    await render_active_tab(q)


@on("save_ai")
async def save_ai(q: Q):
    _save_ai_fields(q)
    q.client.ai_cooldown_until = 0
    q.client.ai_flash = ("success", "AI settings saved.")
    await render_active_tab(q)


@on("ai_test")
async def ai_test(q: Q):
    _save_ai_fields(q)
    q.client.ai_cooldown_until = 0
    if ai_config(q) is None:
        q.client.ai_flash = ("warning", "Enter the API key / endpoint first.")
    else:
        text, err = await ai_chat(q, [{"role": "user", "content": "Reply with the single word: OK"}], 300)
        q.client.ai_flash = (("success", f"Connected. Model replied: {text[:60]}") if text
                             else ("error", f"Connection failed: {err}"))
    await render_active_tab(q)


def _save_tg_fields(q: Q):
    """Copy the Telegram form fields from the submission into client state (validated)."""
    for arg in ("tg_on_alert", "tg_on_assign", "tg_on_progress", "tg_on_predict"):
        if q.args[arg] is not None:
            setattr(q.client, arg, bool(q.args[arg]))
    if q.args.tg_min_sev in ("Warning", "Critical"):
        q.client.tg_min_sev = q.args.tg_min_sev
    if q.args.tg_lang in TG_TEXT:
        q.client.tg_lang = q.args.tg_lang
    problems = []
    if q.args.tg_token is not None:
        token = q.args.tg_token.strip()
        if token and not TG_TOKEN_RE.match(token):
            problems.append("The bot token does not look right (expected 123456789:AAH...).")
        else:
            q.client.tg_token = token
    if q.args.tg_chat_id is not None:
        chat = q.args.tg_chat_id.strip()
        if chat and not TG_CHAT_RE.match(chat):
            problems.append("The chat ID should be a number such as 123456789 (or @channelname).")
        else:
            q.client.tg_chat_id = chat
    return problems


@on("tg_enabled")
async def tg_toggle(q: Q):
    _save_tg_fields(q)
    q.client.tg_enabled = bool(q.args.tg_enabled)
    if q.client.tg_enabled and not (q.client.tg_token and q.client.tg_chat_id):
        q.client.tg_flash = ("warning", "Add the bot token and chat ID, then click Send test message.")
    await render_active_tab(q)


@on("tg_save")
async def tg_save(q: Q):
    problems = _save_tg_fields(q)
    q.client.tg_flash = ("warning", " ".join(problems)) if problems else ("success", "Telegram settings saved.")
    await render_active_tab(q)


@on("tg_detect")
async def tg_detect(q: Q):
    problems = _save_tg_fields(q)
    if problems or not q.client.tg_token:
        q.client.tg_flash = ("warning", " ".join(problems) or "Paste the bot token first.")
    else:
        loop = asyncio.get_running_loop()
        chat_id, name, err = await loop.run_in_executor(None, tg_detect_blocking, q.client.tg_token)
        if chat_id:
            q.client.tg_chat_id = chat_id
            q.client.tg_flash = ("success", f"Found chat ID {chat_id} ({name}). Click Send test message to confirm.")
        else:
            q.client.tg_flash = ("error", f"Could not detect the chat ID: {err}")
    await render_active_tab(q)


@on("tg_test")
async def tg_test(q: Q):
    problems = _save_tg_fields(q)
    if problems or not (q.client.tg_token and q.client.tg_chat_id):
        q.client.tg_flash = ("warning", " ".join(problems) or "Enter the bot token and chat ID first.")
    else:
        loop = asyncio.get_running_loop()
        ok, err = await loop.run_in_executor(None, tg_send_blocking, q.client.tg_token, q.client.tg_chat_id,
                                             tg_text(q, "test"))
        if ok:
            q.client.tg_sent = (q.client.tg_sent or 0) + 1
            q.client.tg_enabled = True
            q.client.tg_flash = ("success", "Test message sent - check your Telegram. Notifications are now ON.")
        else:
            q.client.tg_flash = ("error", f"Telegram said: {err}")
    await render_active_tab(q)


@on("inject_anomaly")
async def inject_anomaly(q: Q):
    q.client.inject_machine = q.args.inject_machine or q.client.inject_machine
    q.client.inject_sensor = q.args.inject_sensor or q.client.inject_sensor
    new_alerts = apply_tick(q, force_machine=q.client.inject_machine, force_sensor=q.client.inject_sensor)
    await enrich_alerts(q, new_alerts)
    await notify_new_alerts(q, new_alerts)
    q.client.active_tab = "#alerts"
    await render_active_tab(q)


def _wo_flash(q, kind, text):
    q.client.wo_flash = (kind, text, time.time() + 12)


@on("open_wo")
async def open_wo(q: Q):
    wo_id = q.args.open_wo
    if isinstance(wo_id, str) and find_wo(q, wo_id):
        q.client.dialog_wo = wo_id
    q.client.active_tab = "#alerts"
    await render_active_tab(q)


@on("wo_table")
async def on_wo_table(q: Q):
    sel = q.args.wo_table or []
    if sel and find_wo(q, sel[0]):
        q.client.dialog_wo = sel[0]
    await render_active_tab(q)


@on("wo_assign")
async def wo_assign(q: Q):
    wo = find_wo(q, q.args.wo_assign)
    eng = find_engineer(q, q.args.engineer)
    if wo is None or wo["state"] in ("In Progress", "Completed"):
        pass
    elif eng is None or not eng["on_duty"]:
        _wo_flash(q, "warning", "Pick an engineer who is on duty.")
    else:
        wo["assignee"] = eng["id"]
        wo["assigned_at"] = datetime.now().strftime("%H:%M:%S")
        wo["state"] = "Assigned"
        sent_to = "Telegram notification sent" if tg_ready(q) else "Notification sent to the engineer (simulated)"
        _wo_flash(q, "success", f"{wo['id']} assigned to {eng['name']} ({eng['team']}, {eng['shift']}). {sent_to}.")
        if q.client.tg_on_assign:
            text = tg_wo_text(q, "assign", wo)
            own = (eng.get("telegram_chat_id") or "").strip()
            if own and TG_CHAT_RE.match(own):
                tg_fire(q, text, own)                       # straight to the engineer's own Telegram
            if not own or own != q.client.tg_chat_id:
                tg_fire(q, text)                            # and to the supervisor / demo chat
    q.client.dialog_wo = None
    await render_active_tab(q)


@on("wo_start")
async def wo_start(q: Q):
    wo = find_wo(q, q.args.wo_start)
    if wo and wo["state"] == "Assigned":
        wo["state"] = "In Progress"
        _wo_flash(q, "info", f"{wo['id']} is in progress - {engineer_name(q, wo['assignee'])} started work on {wo['machine']}.")
        if q.client.tg_on_progress:
            tg_fire(q, tg_wo_text(q, "start", wo))
    q.client.dialog_wo = None
    await render_active_tab(q)


@on("wo_complete")
async def wo_complete(q: Q):
    wo = find_wo(q, q.args.wo_complete)
    if wo and wo["state"] in ("Assigned", "In Progress"):
        wo["state"] = "Completed"
        used, short = consume_parts(q, wo)
        extra = (f" Parts used: {', '.join(used)}." if used else "") + \
                (f" Not enough stock: {', '.join(short)} - order it in Parts & PM Plan." if short else "")
        _wo_flash(q, "warning" if short else "success",
                  f"{wo['id']} completed by {engineer_name(q, wo['assignee'])}. {wo['machine']} is back in service.{extra}")
        if q.client.tg_on_progress:
            tg_fire(q, tg_wo_text(q, "complete", wo))
    q.client.dialog_wo = None
    await render_active_tab(q)


@on("wo_close")
async def wo_close(q: Q):
    q.client.dialog_wo = None
    await render_active_tab(q)


@on("wo_dialog.dismissed")
async def wo_dialog_dismissed(q: Q):
    q.client.dialog_wo = None
    await render_active_tab(q)


@on("wo_diag")
async def on_wo_diag(q: Q):
    wo = find_wo(q, q.args.wo_diag)
    if wo:
        wo["diag"] = await diagnose_wo(q, wo)
    await render_active_tab(q)


@on("handover_ai")
async def on_handover_ai(q: Q):
    text, src = await handover_ai(q)
    q.client.handover_ai = (text, src)
    await render_active_tab(q)


@on("pm_tbl")
async def on_pm_tbl(q: Q):
    sel = q.args.pm_tbl or []
    t = next((x for x in q.client.pm if sel and x["id"] == sel[0]), None)
    if t:
        t["due_in_days"] = t["interval_days"]
        q.client.mt_flash = ("success", f"PM done: {t['task']} on {MACHINE_SHORT.get(t['machine'], t['machine'])}. "
                                        f"Next due in {t['interval_days']} days.", time.time() + 12)
    await render_active_tab(q)


@on("parts_tbl")
async def on_parts_tbl(q: Q):
    sel = q.args.parts_tbl or []
    p = find_part(q, sel[0]) if sel else None
    if p:
        if p.get("on_order"):
            p["stock"] += p["reorder_qty"]
            p["on_order"] = False
            q.client.mt_flash = ("success", f"Delivery received: {p['reorder_qty']} x {p['name']}. Stock is now {p['stock']}.",
                                 time.time() + 12)
        else:
            p["on_order"] = True
            q.client.mt_flash = ("info", f"Ordered {p['reorder_qty']} x {p['name']} - expected in {p['lead_days']} days "
                                         f"(click the part again to simulate the delivery).", time.time() + 12)
    await render_active_tab(q)


@on("roster_table")
async def on_roster_table(q: Q):
    sel = q.args.roster_table or []
    eng = find_engineer(q, sel[0]) if sel else None
    if eng:
        eng["on_duty"] = not eng["on_duty"]
        _wo_flash(q, "info", f"{eng['name']} is now {'ON' if eng['on_duty'] else 'OFF'} duty.")
    await render_active_tab(q)


@on("ask_ai_risk")
async def ask_ai_risk(q: Q):
    await ask_copilot(q, "Which machine is most likely to fail next, why, and what should we do first?")


@on("start_drift")
async def start_drift(q: Q):
    q.client.inject_machine = q.args.inject_machine or q.client.inject_machine
    q.client.inject_sensor = q.args.inject_sensor or q.client.inject_sensor
    mdef = find_mdef(q.client.inject_machine)
    if mdef and sensor_applicable(mdef["sensors"][q.client.inject_sensor]):
        q.client.drift = {q.client.inject_machine: q.client.inject_sensor}
    q.client.active_tab = "#health"
    if not q.client.running:
        q.client.running = True
        asyncio.create_task(live_loop(q))
    await render_active_tab(q)


@on("stop_drift")
async def stop_drift(q: Q):
    q.client.drift = {}
    await render_active_tab(q)


@on("reset_sim")
async def reset_sim(q: Q):
    init_client_state(q, force=True)
    await render_active_tab(q)


async def ask_copilot(q: Q, question):
    question = (question or "").strip()
    if not question:
        return
    q.client.active_tab = "#copilot"
    q.client.chat = (list(q.client.chat) + [
        {"role": "user", "text": question},
        {"role": "assistant", "text": "_Thinking..._", "src": "", "pending": True},
    ])[-MAX_CHAT:]
    await render_active_tab(q)                       # show the question + placeholder at once
    answer, src = await copilot_answer(q, question)
    q.client.chat[-1] = {"role": "assistant", "text": answer, "src": src}
    await render_active_tab(q)


@on("copilot_send")
async def copilot_send(q: Q):
    await ask_copilot(q, q.args.copilot_input)


@on("copilot_suggest")
async def copilot_suggest(q: Q):
    await ask_copilot(q, q.args.copilot_suggest)


@on("ask_ai_fleet")
async def ask_ai_fleet(q: Q):
    await ask_copilot(q, "Summarize fleet health right now and tell me what to inspect first.")


@on("copilot_clear")
async def copilot_clear(q: Q):
    q.client.chat = []
    await render_active_tab(q)


# --------------------------------------------------------------------------------------
# Init
# --------------------------------------------------------------------------------------

def init_client_state(q: Q, force=False):
    if q.client.initialized and not force:
        return
    q.client.initialized = True
    q.client.active_tab = "#overview"
    q.client.running = False
    q.client.tick_count = 0
    q.client.machines = {m["id"]: new_machine_state(m) for m in MACHINE_DEFS}
    q.client.alerts = deque(maxlen=MAX_ALERTS)
    q.client.actions = deque(maxlen=MAX_ACTIONS)
    q.client.wo_seq = 1000
    q.client.dialog_wo = None
    q.client.wo_flash = None
    q.client.engineers = [dict(e) for e in ENGINEER_DEFS]
    q.client.drift = {}
    q.client.tg_pred_sent = set()
    q.client.inject_machine = MACHINE_DEFS[0]["id"]
    q.client.inject_sensor = "temperature"
    q.client.chart_keys = []
    q.client.chat = []
    q.client.prod = {m["id"]: new_prod_state(m) for m in MACHINE_DEFS}
    q.client.parts = [dict(p, on_order=False) for p in PARTS_DEFS]
    q.client.pm = [dict(t) for t in PM_DEFS]
    q.client.mt_flash = None
    q.client.handover_ai = None
    q.client.shift_start = datetime.now()
    # AI settings survive "Reset Simulation" - only set defaults the first time.
    if q.client.ai_provider is None:
        # Optional deployment defaults (e.g. an HPE MLIS endpoint set via the Helm chart / app env).
        q.client.llm_base_url = os.environ.get("LLM_BASE_URL", "").strip() or DEFAULT_PCAI_BASE_URL
        q.client.llm_model = os.environ.get("LLM_MODEL", "").strip() or DEFAULT_PCAI_MODEL
        q.client.llm_api_key = os.environ.get("LLM_API_KEY", "").strip()
        q.client.llm_verify_ssl = os.environ.get("LLM_VERIFY_SSL", "true").strip().lower() not in ("0", "false", "no")
        q.client.ai_provider = "custom" if q.client.llm_api_key else "simulation"   # token present -> use PCAI model
        q.client.openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
        q.client.openrouter_model = DEFAULT_OPENROUTER_MODEL
        q.client.ai_calls = 0
        q.client.ai_cooldown_until = 0
        # Telegram: optional deployment defaults via env, everything else editable in Settings
        q.client.tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        q.client.tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        q.client.tg_enabled = bool(q.client.tg_token and q.client.tg_chat_id)
        q.client.tg_on_alert = q.client.tg_on_assign = q.client.tg_on_progress = q.client.tg_on_predict = True
        q.client.tg_min_sev = "Warning"
        q.client.tg_lang = "id"
        q.client.tg_sent = 0
        q.client.tg_last_error = None
        q.client.tg_flash = None
        q.client.ai_last_error = None
        q.client.ai_flash = None


@app("/")
async def serve(q: Q):
    if not q.client.initialized:
        init_client_state(q)

    handled = await run_on(q)
    if not handled:
        await render_active_tab(q)
