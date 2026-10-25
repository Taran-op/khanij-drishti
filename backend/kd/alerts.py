"""Early warning: turns model outputs into alerts and queues notifications.

Delivery goes through kd/notify.py: each alert fans out to the recipients whose audience and
minimum severity match, is de-duplicated over 24 h, and is sent by SMTP / SMS gateway when those
are configured (otherwise held in the outbox until they are).
"""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from . import notify, shortfall, storage
from .config import AS_OF, FESTIVALS, MINES

def generate():
    now = datetime.now().isoformat(timespec="seconds")
    hist = storage.read_table("production_daily")
    fc = storage.read_table("weather_forecast")
    alerts = []

    def add(severity, category, title, message, action, audience, ref):
        alerts.append(dict(alert_id=f"AL-{len(alerts)+1:03d}", ts=now, severity=severity, category=category,
                           ref=ref, title=title, message=message, recommended_action=action,
                           audience=",".join(audience), status="open"))

    for m in MINES:
        f = shortfall.forecast(m["id"])["month"]
        gap = f["target_t"] - f["projected_t"]
        if f["prob_miss"] >= 0.4:
            sev = "high" if f["prob_miss"] >= 0.8 else "medium"
            risk = ">99%" if f["prob_miss"] >= 0.995 else f"{f['prob_miss']:.0%}"
            add(sev, "Production shortfall", f"{m['name']}: {risk} risk of missing {f['month']} target",
                f"Projected {f['projected_t']:,.0f} t vs target {f['target_t']:,.0f} t "
                f"(gap {max(gap, 0):,.0f} t, P10-P90 {f['p10_t']:,.0f}-{f['p90_t']:,.0f} t).",
                "Open Optimisation to see the corrective-action plan.", ["operations", "planners", "management"], m["id"])

        h = hist[hist.mine_id == m["id"]]
        last7 = h.tail(7).excavator_avail.mean()
        if last7 < 0.75:
            add("high", "Equipment downtime", f"{m['name']}: excavator availability {last7:.0%} (7-day)",
                f"Down from a 90-day norm of {h.tail(90).excavator_avail.mean():.0%}; "
                f"{h.tail(7).downtime_hrs.sum():.0f} h downtime this week.",
                "Expedite maintenance; consider re-deploying dumpers.", ["operations"], m["id"])
        tail = h.tail(180)
        slope = np.polyfit(np.arange(len(tail)), tail.excavator_avail.values, 1)[0] * 30
        if slope < -0.008:
            add("medium", "Equipment degradation", f"{m['name']}: availability falling {abs(slope):.1%} per month",
                "Sustained decline over 6 months suggests ageing fleet, not one-off breakdowns.",
                "Plan overhaul / replacement in next capex cycle.", ["planners", "management"], m["id"])

        if m["method"] == "opencast":
            st = storage.read_table("mines").set_index("id").loc[m["id"], "station"]
            w = fc[(fc.station == st) & (fc.date <= str(AS_OF + timedelta(days=10)))]
            heavy = w[w.rain_mm >= 35]
            if len(heavy):
                add("medium", "Weather", f"{m['name']}: heavy rain forecast ({heavy.rain_mm.max():.0f} mm on {heavy.date.iloc[heavy.rain_mm.argmax()]})",
                    f"{len(heavy)} day(s) ≥35 mm in the next 10 days (IMD extended range). Opencast pit, so high impact.",
                    "Pre-position dewatering pumps; bring forward blasting to dry days.", ["operations"], m["id"])

    upcoming = sorted(d for d in FESTIVALS if str(AS_OF) <= d <= str(AS_OF + timedelta(days=30)))
    if upcoming:
        add("info", "Manpower", f"Festival days ahead: {', '.join(upcoming)}",
            "Attendance typically drops ~25% on these days; already included in forecasts.",
            "Roster backfill / overtime planning.", ["operations"], "ALL")

    for z in storage.read_table("reserve_zones").to_dict("records"):
        if z["status"] == "new" and z["priority"] == "High":
            add("info", "Reserve discovery", f"{z['zone_id']}: probable Mn zone near {z['nearest_known']}",
                f"{z['area_km2']} km², mean prospectivity {z['mean_prob']:.2f}, confidence {z['confidence']:.2f}, "
                f"indicative {z['est_tonnes_mt']} Mt @ {z['est_grade_pct']}% Mn (UNFC {z['unfc']}).",
                "Schedule ground truthing + scout drilling.", ["geology", "management"], z["zone_id"])

    order = {"high": 0, "medium": 1, "info": 2}
    alerts.sort(key=lambda a: order[a["severity"]])
    for i, a in enumerate(alerts):
        a["alert_id"] = f"AL-{i+1:03d}"
    storage.write_table("alerts", pd.DataFrame(alerts))
    dispatch(alerts)
    storage.log_run(5, "alerts", total=len(alerts), high=sum(a["severity"] == "high" for a in alerts))
    return alerts


def dispatch(alerts):
    q = notify.enqueue_alerts(alerts)
    q.update(notify.flush())
    storage.log_run(5, "notifications", **q)
    return q
