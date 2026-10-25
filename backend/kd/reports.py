"""Layer 5 · Reports: daily operations brief, management summary, reserve & exploration report.

Each report is built once as a structured document (KPIs, tables, charts, notes) from the live
models and stores, then rendered to:
  PDF   reportlab (A4, DejaVu Sans for full Unicode) with matplotlib charts and a real map figure
  XLSX  openpyxl: a summary sheet + one formatted sheet per table, native Excel charts
Generated files are archived under data/reports/ and listed by the API; scheduled runs email them.
"""
import io
import json
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import ingest, shortfall, storage
from .config import AS_OF, BOUNDS, DATA, MINES, MODELS

ARCHIVE = DATA / "reports"
_plot_lock = threading.Lock()  # matplotlib's pyplot state is not thread-safe

REPORTS = {
    "ops_brief": dict(title="Daily operations brief", perms=["production"], audiences=["operations", "management"],
                      desc="Month-end risk by mine, alerts, equipment, 30-day outlook"),
    "management": dict(title="Management summary", perms=["overview", "production"], audiences=["management"],
                       desc="FY to date, month risk, corrective-action plan, reserves and model health"),
    "reserves": dict(title="Reserve & exploration report", perms=["reserves"], audiences=["planners", "geology", "management"],
                     desc="Prospectivity map, probable zones, model validation, data provenance"),
}


# ---- helpers ---------------------------------------------------------------------------------
def _risk(p):
    return "High" if p >= 0.8 else "Medium" if p >= 0.4 else "Low"


def _card(name):
    return json.loads((MODELS / f"{name}_card.json").read_text())


def _provenance_notes():
    prov = storage.read_table("provenance")
    sim = prov[~prov.real.astype(bool)]
    real = prov[prov.real.astype(bool)]
    notes = [f"Real data feeds: {len(real)} ({', '.join(sorted(real.name)[:8])}{'…' if len(real) > 8 else ''})."]
    if len(sim):
        notes.append("Simulated (clearly flagged, to be replaced by real feeds): " + ", ".join(sorted(sim.name)) + ".")
    if any(n in set(sim.name) for n in ("mis_shift_production", "equipment_telemetry")):
        notes.append("Mine operations come from the virtual-mine devices through the ingestion API, calibrated to "
                     "MOIL's published company production; they are not MOIL's internal MIS data.")
    return notes


def _months_forecasts():
    return {m["id"]: shortfall.forecast(m["id"]) for m in MINES}


def _risk_table(fcs):
    rows = []
    for m in MINES:
        f = fcs[m["id"]]["month"]
        rows.append(dict(mine=m["name"], method=m["method"], target_t=f["target_t"], mtd_t=f["mtd_actual_t"],
                         projected_t=f["projected_t"], p10_t=f["p10_t"], p90_t=f["p90_t"], prob_miss=f["prob_miss"],
                         shortfall_t=f["expected_shortfall_t"], risk=_risk(f["prob_miss"])))
    return rows


RISK_COLS = [("mine", "Mine", "str"), ("method", "Method", "str"), ("target_t", "Target (t)", "t"), ("mtd_t", "MTD actual (t)", "t"),
             ("projected_t", "Projected (t)", "t"), ("p10_t", "P10 (t)", "t"), ("p90_t", "P90 (t)", "t"),
             ("prob_miss", "P(miss)", "pct"), ("shortfall_t", "Exp. shortfall (t)", "t"), ("risk", "Risk", "str")]


# ---- report builders -------------------------------------------------------------------------
def build(name):
    doc = dict(name=name, title=REPORTS[name]["title"], generated=datetime.now().isoformat(timespec="seconds"),
               as_of=str(AS_OF), sections=[])
    {"ops_brief": _ops_brief, "management": _management, "reserves": _reserves}[name](doc)
    doc["notes"] = doc.get("notes", []) + _provenance_notes()
    return doc


def _ops_brief(doc):
    fcs = _months_forecasts()
    hist = shortfall._ctx()[1]
    month = str(AS_OF)[:7]
    rt = _risk_table(fcs)
    tgt, proj = sum(r["target_t"] for r in rt), sum(r["projected_t"] for r in rt)
    al = storage.read_table("alerts")
    doc["subtitle"] = f"{month} · data through {hist.date.max()}"
    doc["kpis"] = [("Month target", f"{tgt:,.0f} t", month), ("Projected", f"{proj:,.0f} t", f"{proj / tgt:.0%} of target"),
                   ("Gap to target", f"{max(tgt - proj, 0):,.0f} t", "P50 projection"),
                   ("Mines at high risk", str(sum(r["risk"] == "High" for r in rt)), f"of {len(rt)}"),
                   ("Open high alerts", str(int(((al.status == "open") & (al.severity == "high")).sum())), f"{len(al)} alerts in total")]
    doc["sections"].append(dict(heading="Month-end risk by mine", table=dict(columns=RISK_COLS, rows=rt),
                                chart=dict(kind="bar", title=f"{month}: target vs projected (t)", x=[r["mine"] for r in rt],
                                           series={"Target": [r["target_t"] for r in rt], "Projected": [r["projected_t"] for r in rt]})))
    doc["sections"].append(dict(heading="Alerts", table=dict(
        columns=[("severity", "Severity", "str"), ("category", "Category", "str"), ("title", "Alert", "str"),
                 ("recommended_action", "Recommended action", "str")],
        rows=al.sort_values("severity", key=lambda s: s.map({"high": 0, "medium": 1, "info": 2})).to_dict("records"))))

    eq = []
    for m in MINES:
        h = hist[hist.mine_id == m["id"]].tail(7)
        last = h.iloc[-1]
        eq.append(dict(mine=m["name"], date=last.date, excavator_avail=last.excavator_avail, avail_7d=h.excavator_avail.mean(),
                       dumpers=f"{int(last.dumpers_available)}/{int(last.dumpers_total)}", downtime_hrs=last.downtime_hrs))
    live = ingest.live_equipment(minutes=60 * 24)
    sub = (f"Live telemetry: {len(live)} machines reporting in the last 24 h "
           f"({sum(x['status'] == 'working' for x in live)} working, {sum(x['status'] == 'breakdown' for x in live)} breakdown, "
           f"{sum(x['status'] == 'maintenance' for x in live)} maintenance).") if live else "No live heartbeats in the last 24 h."
    doc["sections"].append(dict(heading="Equipment", text=sub, table=dict(
        columns=[("mine", "Mine", "str"), ("date", "Last daily log", "str"), ("excavator_avail", "Excavator avail.", "pct"),
                 ("avail_7d", "7-day avg", "pct"), ("dumpers", "Dumpers avail./total", "str"), ("downtime_hrs", "Downtime (h)", "num1")],
        rows=eq)))

    tot = hist.groupby("date").actual_t.sum().tail(30)
    fc_tot = pd.DataFrame([d for f in fcs.values() for d in f["daily"]]).groupby("date")[["p10", "p50", "p90"]].sum().head(14)
    doc["sections"].append(dict(heading="Output: last 30 days and 14-day outlook (all mines)", chart=dict(
        kind="line", title="Daily output, t (actual, then P50 forecast with P10–P90 band)",
        x=list(tot.index) + list(fc_tot.index),
        series={"Actual": list(tot.values) + [None] * len(fc_tot), "Forecast P50": [None] * len(tot) + list(fc_tot.p50.values)},
        band=dict(lo=[None] * len(tot) + list(fc_tot.p10.values), hi=[None] * len(tot) + list(fc_tot.p90.values)))))
    nxt = []
    for m in MINES:
        r = dict(mine=m["name"])
        for d in fcs[m["id"]]["daily"][:7]:
            r[d["date"]] = d["p50"]
        nxt.append(r)
    days = [d["date"] for d in fcs[MINES[0]["id"]]["daily"][:7]]
    doc["sections"].append(dict(heading="Next 7 days: P50 forecast by mine (t)", table=dict(
        columns=[("mine", "Mine", "str")] + [(d, d[5:], "t") for d in days], rows=nxt)))
    card = _card("shortfall")
    doc["notes"] = [f"Forecaster: {card['algorithm']} (champion of {len([b for b in card.get('leaderboard', []) if b.get('version')])} "
                    f"families; 30-day backtest MAPE {card['backtest']['model_mape_30d']}% vs seasonal-naive {card['backtest']['naive_mape_30d']}%)."]


def _management(doc):
    from . import optimize
    fcs = _months_forecasts()
    fy = shortfall.fiscal_summary()
    rt = _risk_table(fcs)
    plan = optimize.recommend(40.0)
    zones = storage.read_table("reserve_zones")
    rc, sc = _card("reserve"), _card("shortfall")
    ytd_a, ytd_t = sum(r["ytd_actual_t"] for r in fy["mines"]), sum(r["ytd_target_t"] for r in fy["mines"])
    fy_p, fy_t = sum(r["fy_projected_t"] for r in fy["mines"]), sum(r["fy_target_t"] for r in fy["mines"])
    doc["subtitle"] = f"{fy['fy']} · as of {AS_OF}"
    doc["kpis"] = [("FY to date", f"{ytd_a:,.0f} t", f"{ytd_a / ytd_t:.0%} of plan"),
                   ("FY projection", f"{fy_p:,.0f} t", f"target {fy_t:,.0f} t"),
                   ("Month exp. shortfall", f"{plan['expected_shortfall_before_t']:,} t", str(AS_OF)[:7]),
                   ("Recoverable (Rs 40 lakh)", f"{plan['recovered_t']:,} t", f"spend Rs {plan['spent_lakh']:.0f} lakh"),
                   ("Probable zones", str(len(zones)), f"~{zones.est_tonnes_mt.sum():,.0f} Mt indicative")]
    fyrows = [dict(mine=r["name"], ytd_actual_t=r["ytd_actual_t"], ytd_target_t=r["ytd_target_t"], attainment=r["attainment_ytd"],
                   fy_target_t=r["fy_target_t"], fy_projected_t=r["fy_projected_t"]) for r in fy["mines"]]
    doc["sections"].append(dict(heading=f"{fy['fy']}: year to date by mine", table=dict(columns=[
        ("mine", "Mine", "str"), ("ytd_actual_t", "YTD actual (t)", "t"), ("ytd_target_t", "YTD plan (t)", "t"),
        ("attainment", "Attainment", "pct"), ("fy_target_t", "FY plan (t)", "t"), ("fy_projected_t", "FY projection (t)", "t")], rows=fyrows),
        chart=dict(kind="bar", title="Year to date: actual vs plan (t)", x=[r["mine"] for r in fyrows],
                   series={"Plan": [r["ytd_target_t"] for r in fyrows], "Actual": [r["ytd_actual_t"] for r in fyrows]})))
    doc["sections"].append(dict(heading="This month: risk by mine", table=dict(columns=[c for c in RISK_COLS if c[0] in
                                ("mine", "target_t", "projected_t", "prob_miss", "shortfall_t", "risk")], rows=rt)))
    doc["sections"].append(dict(
        heading="Recommended corrective actions (budget Rs 40 lakh)",
        text=(f"Expected month-end shortfall {plan['expected_shortfall_before_t']:,} t → {plan['expected_shortfall_after_t']:,} t "
              f"after the plan (recovers {plan['recovered_t']:,} t for Rs {plan['spent_lakh']:.0f} lakh). Every action was "
              f"re-scored by the forecaster; constraints checked: {', '.join(plan['constraints_checked'])}.")
             + (" " + " ".join(plan["notes"]) if plan["notes"] else ""),
        table=dict(columns=[("step", "#", "int"), ("mine", "Mine", "str"), ("title", "Action", "str"), ("cost_lakh", "Cost (Rs lakh)", "num1"),
                            ("recovered_t", "Recovers (t)", "t"), ("t_per_lakh", "t per lakh", "num1"), ("prob_miss_after", "P(miss) after", "pct")],
                   rows=plan["plan"])))
    zs = zones.groupby("status").agg(zones=("zone_id", "count"), area_km2=("area_km2", "sum"), mt=("est_tonnes_mt", "sum")).reset_index()
    doc["sections"].append(dict(heading="Reserves", text=(
        f"Prospectivity model: {rc['algorithm']} (generalisation score "
        f"{next((b['gen_score'] for b in rc.get('leaderboard', []) if b.get('champion')), '—')}). "
        "Tonnages are indicative (UNFC G4) and rest on simulated boreholes until real drilling is uploaded."),
        table=dict(columns=[("status", "Zone type", "str"), ("zones", "Zones", "int"), ("area_km2", "Area (km²)", "num1"), ("mt", "Indicative Mt", "num1")],
                   rows=zs.to_dict("records"))))
    board = [dict(task="Reserves", model=b["name"], metric="gen. score", value=b.get("gen_score"), serving="yes" if b.get("champion") else "")
             for b in rc.get("leaderboard", [])] + \
            [dict(task="Shortfall", model=b["name"], metric="30-day MAPE %", value=b.get("mape_30d"), serving="yes" if b.get("champion") else "")
             for b in sc.get("leaderboard", [])]
    doc["sections"].append(dict(heading="Model health (latest training run)", table=dict(columns=[
        ("task", "Task", "str"), ("model", "Model", "str"), ("metric", "Metric", "str"), ("value", "Value", "num3"), ("serving", "Serving", "str")],
        rows=board)))


def _reserves(doc):
    zones = storage.read_table("reserve_zones")
    rc = _card("reserve")
    oc = rc.get("occurrence_check", {})
    champ = next((b for b in rc.get("leaderboard", []) if b.get("champion")), {})
    doc["subtitle"] = f"Balaghat–Nagpur manganese belt · as of {AS_OF}"
    doc["kpis"] = [("Probable zones", str(len(zones)), f"{(zones.status == 'new').sum()} greenfield"),
                   ("Area", f"{zones.area_km2.sum():,.0f} km²", "zone cores"),
                   ("Indicative tonnage", f"{zones.est_tonnes_mt.sum():,.0f} Mt", "model estimate, simulated boreholes"),
                   ("Serving model", rc.get("champion", "—").replace("_", " "), f"gen. score {champ.get('gen_score', '—')}"),
                   ("Held-out occurrence AUC", str(oc.get("auc", "—")), f"{oc.get('n_occurrences', 0)} USGS sites never seen")]
    doc["sections"].append(dict(heading="Prospectivity map", chart=dict(kind="map", title=f"Mn prospectivity ({rc.get('champion', '')}) with probable zones")))
    doc["sections"].append(dict(heading="Probable reserve zones", table=dict(columns=[
        ("zone_id", "Zone", "str"), ("priority", "Priority", "str"), ("nearest_known", "Nearest known site", "str"), ("area_km2", "Area (km²)", "num1"),
        ("mean_prob", "Mean p", "num3"), ("confidence", "Confidence", "num3"), ("est_grade_pct", "Grade % Mn", "num1"),
        ("est_thickness_m", "Thickness (m)", "num1"), ("est_tonnes_mt", "Indicative Mt", "num1"), ("unfc", "UNFC", "str"),
        ("centroid_lat", "Lat", "num3"), ("centroid_lon", "Lon", "num3")], rows=zones.to_dict("records"))))
    doc["sections"].append(dict(heading="Model comparison", text=rc.get("validation", ""), table=dict(columns=[
        ("name", "Model", "str"), ("gen_score", "Gen. score", "num3"), ("cv_auc", "Spatial-CV AUC", "num3"), ("pr_auc", "PR-AUC", "num3"),
        ("occ_auc_far", "Occurrence AUC >6 km", "num3"), ("hit_rate", "Occurrences in a zone", "pct")], rows=rc.get("leaderboard", []))))
    if oc.get("by_distance"):
        doc["sections"].append(dict(heading="Held-out occurrence test by distance from training mines", text=oc.get("note", ""),
                                    table=dict(columns=[("band", "Distance", "str"), ("n", "n", "int"), ("auc", "AUC", "num3"),
                                                        ("hit_rate", "In a zone", "pct")], rows=oc["by_distance"])))
    prov = storage.read_table("provenance")
    doc["sections"].append(dict(heading="Data inputs", table=dict(columns=[
        ("name", "Dataset", "str"), ("real", "Real?", "bool"), ("source", "Source", "str"), ("date_range", "Dates", "str")],
        rows=prov.replace({np.nan: None}).to_dict("records"))))
    doc["notes"] = ["The UNFC column describes the ground (brownfield zones sit on explored ground around existing workings). "
                    "The grade, thickness and tonnage figures themselves are model estimates from simulated boreholes, i.e. "
                    "reconnaissance-level (G4) confidence: exploration targets, not reported reserves. Upload real drill logs "
                    "(Data & Models → Data uploads) to replace them."]


# ---- formatting ------------------------------------------------------------------------------
def fmt(v, kind):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    if kind == "t":
        return f"{v:,.0f}"
    if kind == "pct":
        return f"{v:.0%}" if v < 0.995 or v >= 1 else ">99%"
    if kind == "num1":
        return f"{v:,.1f}"
    if kind == "num3":
        return f"{v:.3f}" if isinstance(v, (int, float)) else str(v)
    if kind == "int":
        return f"{int(v)}"
    if kind == "bool":
        return "real" if v else "simulated"
    return str(v)


XL_FMT = {"t": "#,##0", "pct": "0%", "num1": "#,##0.0", "num3": "0.000", "int": "0"}


# ---- charts (matplotlib) ---------------------------------------------------------------------
PALETTE = ["#1f6f8b", "#e07a3f", "#3a9d5d", "#8e5ea2", "#c94f6d", "#6c7a89"]


def _chart_png(ch, width=7.2, height=3.0):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    with _plot_lock:
        fig, ax = plt.subplots(figsize=(width, height if ch["kind"] != "map" else 4.6), dpi=150)
        if ch["kind"] == "bar":
            n, x = len(ch["series"]), np.arange(len(ch["x"]))
            w = 0.8 / n
            for i, (k, vals) in enumerate(ch["series"].items()):
                ax.bar(x + (i - (n - 1) / 2) * w, vals, w, label=k, color=PALETTE[i % len(PALETTE)])
            ax.set_xticks(x, [str(s).split(" (")[0] for s in ch["x"]], fontsize=8)
            ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
        elif ch["kind"] == "line":
            xs = pd.to_datetime(ch["x"])
            if ch.get("band"):
                lo = np.array([np.nan if v is None else v for v in ch["band"]["lo"]], float)
                hi = np.array([np.nan if v is None else v for v in ch["band"]["hi"]], float)
                ax.fill_between(xs, lo, hi, color=PALETTE[1], alpha=0.18, lw=0, label="P10–P90")
            for i, (k, vals) in enumerate(ch["series"].items()):
                ax.plot(xs, np.array([np.nan if v is None else v for v in vals], float), label=k, color=PALETTE[i % len(PALETTE)],
                        lw=1.8, ls="-" if i == 0 else "--")
            ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
            fig.autofmt_xdate()
        elif ch["kind"] == "map":
            _draw_map(ax)
        if ch["kind"] != "map":
            ax.spines[["top", "right"]].set_visible(False)
            ax.grid(axis="y", alpha=0.25)
            ax.legend(fontsize=8, frameon=False)
        ax.set_title(ch.get("title", ""), fontsize=9.5, loc="left")
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
    return buf.getvalue()


def _draw_map(ax):
    g = storage.read_table("grid_cells", storage.GEODB)
    H, W = int(g.row.max()) + 1, int(g.col.max()) + 1
    p = np.full((H, W), np.nan)
    p[g.row.values, g.col.values] = g.prospectivity.values
    ext = [BOUNDS["lon_min"], BOUNDS["lon_max"], BOUNDS["lat_min"], BOUNDS["lat_max"]]
    im = ax.imshow(p, extent=ext, cmap="magma", vmin=0, vmax=1, origin="upper", aspect="auto")
    for f in storage.read_vector("reserve_zones")["features"]:
        for ring in f["geometry"]["coordinates"][:1]:
            xy = np.array(ring)
            ax.plot(xy[:, 0], xy[:, 1], color="#22d3ee", lw=1.2)
        pr = f["properties"]
        ax.annotate(pr.get("zone_id", ""), (pr["centroid_lon"], pr["centroid_lat"]), color="white", fontsize=6.5, ha="center",
                    xytext=(0, 7), textcoords="offset points")
    for m in MINES:
        ax.plot(m["lon"], m["lat"], marker="^", color="#a3e635", mec="black", mew=0.5, ms=6)
        ax.annotate(m["name"].split(" (")[0], (m["lon"], m["lat"]), color="white", fontsize=6.5, xytext=(4, -9), textcoords="offset points")
    ax.set_xlabel("Longitude °E", fontsize=8)
    ax.set_ylabel("Latitude °N", fontsize=8)
    ax.tick_params(labelsize=7)
    cb = ax.figure.colorbar(im, ax=ax, fraction=0.025, pad=0.01)
    cb.ax.tick_params(labelsize=7)
    cb.set_label("prospectivity", fontsize=7)


# ---- PDF -------------------------------------------------------------------------------------
_font = {}


def _fonts():
    if not _font:
        from matplotlib import font_manager
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        reg = font_manager.findfont("DejaVu Sans")
        bold = font_manager.findfont(font_manager.FontProperties(family="DejaVu Sans", weight="bold"))
        pdfmetrics.registerFont(TTFont("DejaVu", reg))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", bold))
        _font.update(regular="DejaVu", bold="DejaVu-Bold")
    return _font


def to_pdf(doc, path):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    f = _fonts()
    NAVY, TEAL, MUTED = colors.HexColor("#0f2a3d"), colors.HexColor("#1f6f8b"), colors.HexColor("#5b6b79")
    st = dict(
        h1=ParagraphStyle("h1", fontName=f["bold"], fontSize=17, leading=21, textColor=colors.white),
        sub=ParagraphStyle("sub", fontName=f["regular"], fontSize=9.5, leading=12, textColor=colors.HexColor("#cfe3ee")),
        h2=ParagraphStyle("h2", fontName=f["bold"], fontSize=11.5, leading=15, textColor=NAVY, spaceBefore=10, spaceAfter=4, keepWithNext=1),
        body=ParagraphStyle("body", fontName=f["regular"], fontSize=8.6, leading=11.5, textColor=colors.HexColor("#1d2731")),
        cell=ParagraphStyle("cell", fontName=f["regular"], fontSize=7.4, leading=9.2),
        head=ParagraphStyle("head", fontName=f["bold"], fontSize=7.4, leading=9.2, textColor=colors.white),
        note=ParagraphStyle("note", fontName=f["regular"], fontSize=7.4, leading=9.6, textColor=MUTED),
        kv=ParagraphStyle("kv", fontName=f["bold"], fontSize=13, leading=16, textColor=NAVY),
        kl=ParagraphStyle("kl", fontName=f["regular"], fontSize=7.2, leading=9, textColor=MUTED),
    )
    wide = doc["name"] == "reserves"
    size = landscape(A4) if wide else A4
    width = size[0] - 30 * mm

    def page(canvas, d):
        canvas.saveState()
        canvas.setFont(f["regular"], 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(15 * mm, 9 * mm, f"KhanijDrishti · {doc['title']} · generated {doc['generated'].replace('T', ' ')}")
        canvas.drawRightString(size[0] - 15 * mm, 9 * mm, f"page {d.page}")
        canvas.restoreState()

    story = []
    banner = Table([[Paragraph(doc["title"], st["h1"])], [Paragraph(doc.get("subtitle", ""), st["sub"])]], colWidths=[width])
    banner.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), NAVY), ("LEFTPADDING", (0, 0), (-1, -1), 10),
                                ("TOPPADDING", (0, 0), (-1, 0), 9), ("BOTTOMPADDING", (0, -1), (-1, -1), 9)]))
    story += [banner, Spacer(1, 8)]
    if doc.get("kpis"):
        row = [[Paragraph(v, st["kv"]), Paragraph(f"{k}<br/>{s}", st["kl"])] for k, v, s in doc["kpis"]]
        kt = Table([[Table([[a], [b]], colWidths=[width / len(row) - 6]) for a, b in row]], colWidths=[width / len(row)] * len(row))
        kt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#d5dee6")),
                                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d5dee6")),
                                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f4f8fb")), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story += [kt, Spacer(1, 4)]
    for sec in doc["sections"]:
        block = [Paragraph(sec["heading"], st["h2"])]
        if sec.get("text"):
            block += [Paragraph(sec["text"], st["body"]), Spacer(1, 4)]
        if sec.get("chart"):
            png = _chart_png(sec["chart"], width=(width / 72) * (1.0 if sec["chart"]["kind"] != "map" else 0.92))
            from reportlab.lib.utils import ImageReader
            iw, ih = ImageReader(io.BytesIO(png)).getSize()
            w = width * (1.0 if sec["chart"]["kind"] != "map" else 0.92)
            block += [Image(io.BytesIO(png), width=w, height=w * ih / iw), Spacer(1, 4)]
        if sec.get("table") and sec["table"]["rows"]:
            cols = sec["table"]["columns"]
            data = [[Paragraph(lbl, st["head"]) for _, lbl, _ in cols]]
            for r in sec["table"]["rows"]:
                data.append([Paragraph(fmt(r.get(k), kind), st["cell"]) for k, _, kind in cols])
            weights = [3.2 if kind == "str" and k in ("title", "recommended_action", "source", "detail") else
                       2.0 if kind == "str" else 1.3 for k, _, kind in cols]
            cw = [width * w / sum(weights) for w in weights]
            t = Table(data, colWidths=cw, repeatRows=1)
            style = [("BACKGROUND", (0, 0), (-1, 0), TEAL), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                     ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#dfe6ec")),
                     ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]
            for i in range(2, len(data), 2):
                style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#f6f9fb")))
            for j, (k, _, kind) in enumerate(cols):
                if kind not in ("str", "bool"):
                    style.append(("ALIGN", (j, 0), (j, -1), "RIGHT"))
            t.setStyle(TableStyle(style))
            block.append(t)
        if len(sec.get("table", {}).get("rows", [])) < 14:
            story.append(KeepTogether(block))
        else:  # long tables split across pages (header row repeats); the heading stays with the table start
            story += block
    if doc.get("notes"):
        story += [Paragraph("Notes and data provenance", st["h2"])] + [Paragraph("• " + n, st["note"]) for n in doc["notes"]]
    SimpleDocTemplate(str(path), pagesize=size, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=13 * mm, bottomMargin=15 * mm,
                      title=doc["title"], author="KhanijDrishti").build(story, onFirstPage=page, onLaterPages=page)
    return path


# ---- XLSX ------------------------------------------------------------------------------------
def to_xlsx(doc, path):
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, LineChart, Reference
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    navy, teal = "0F2A3D", "1F6F8B"
    ws["A1"] = doc["title"]
    ws["A1"].font = Font(bold=True, size=16, color=navy)
    ws["A2"] = doc.get("subtitle", "")
    ws["A3"] = f"Generated {doc['generated'].replace('T', ' ')} · KhanijDrishti"
    ws["A2"].font = ws["A3"].font = Font(color="5B6B79", size=9)
    r = 5
    for k, v, s in doc.get("kpis", []):
        ws.cell(r, 1, k).font = Font(color="5B6B79")
        ws.cell(r, 2, v).font = Font(bold=True, size=12, color=navy)
        ws.cell(r, 3, s).font = Font(color="5B6B79", size=9)
        r += 1
    r += 1
    for n in doc.get("notes", []):
        ws.cell(r, 1, n).alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        ws.row_dimensions[r].height = 30
        r += 1
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width, ws.column_dimensions["C"].width = 30, 22, 30
    for c in "DEF":
        ws.column_dimensions[c].width = 18

    used = set()
    for sec in doc["sections"]:
        name = "".join(ch for ch in sec["heading"] if ch not in '[]:*?/\\')[:28].strip() or "Sheet"
        base, i = name, 2
        while name in used:
            name, i = f"{base[:25]} {i}", i + 1
        used.add(name)
        if not sec.get("table") and sec.get("chart", {}).get("kind") == "map":
            sh = wb.create_sheet(name)
            sh["A1"] = sec["heading"]
            sh["A1"].font = Font(bold=True, size=13, color=navy)
            img = XLImage(io.BytesIO(_chart_png(sec["chart"], width=10)))
            img.width, img.height = 900, 560
            sh.add_image(img, "A3")
            continue
        sh = wb.create_sheet(name)
        sh["A1"] = sec["heading"]
        sh["A1"].font = Font(bold=True, size=13, color=navy)
        row0 = 3
        if sec.get("text"):
            sh["A2"] = sec["text"]
            sh["A2"].alignment = Alignment(wrap_text=True)
            sh.row_dimensions[2].height = 45
            sh.merge_cells("A2:H2")
        ch = sec.get("chart")
        if sec.get("table") and sec["table"]["rows"]:
            cols = sec["table"]["columns"]
            for j, (_, lbl, _) in enumerate(cols, 1):
                c = sh.cell(row0, j, lbl)
                c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor=teal)
                c.alignment = Alignment(wrap_text=True, vertical="center")
            for i, rr in enumerate(sec["table"]["rows"], row0 + 1):
                for j, (k, _, kind) in enumerate(cols, 1):
                    v = rr.get(k)
                    if isinstance(v, (np.floating, np.integer)):
                        v = v.item()
                    if kind == "bool":
                        v = "real" if v else "simulated"
                    if isinstance(v, float) and np.isnan(v):
                        v = None
                    c = sh.cell(i, j, v)
                    if kind in XL_FMT:
                        c.number_format = XL_FMT[kind]
            last = row0 + len(sec["table"]["rows"])
            sh.freeze_panes = sh.cell(row0 + 1, 1)
            sh.auto_filter.ref = f"A{row0}:{get_column_letter(len(cols))}{last}"
            for j, (k, lbl, kind) in enumerate(cols, 1):
                width = max([len(lbl)] + [len(fmt(rr.get(k), kind)) for rr in sec["table"]["rows"][:200]])
                sh.column_dimensions[get_column_letter(j)].width = min(max(10, width + 2), 60)
            anchor_row = last + 3
        else:
            anchor_row = row0
        if ch and ch["kind"] in ("bar", "line"):
            # chart data block to the right of the table, referenced by a native Excel chart
            c0 = (len(sec["table"]["columns"]) + 2) if sec.get("table") else 1
            sh.cell(row0, c0, "x")
            for j, k in enumerate(ch["series"], 1):
                sh.cell(row0, c0 + j, k)
            for i, xv in enumerate(ch["x"], row0 + 1):
                sh.cell(i, c0, str(xv).split(" (")[0])
                for j, vals in enumerate(ch["series"].values(), 1):
                    v = vals[i - row0 - 1]
                    sh.cell(i, c0 + j, None if v is None else float(v)).number_format = "#,##0"
            n = len(ch["x"])
            chart = BarChart() if ch["kind"] == "bar" else LineChart()
            chart.title, chart.height, chart.width = ch.get("title", ""), 8, 18
            chart.add_data(Reference(sh, min_col=c0 + 1, max_col=c0 + len(ch["series"]), min_row=row0, max_row=row0 + n), titles_from_data=True)
            chart.set_categories(Reference(sh, min_col=c0, min_row=row0 + 1, max_row=row0 + n))
            sh.add_chart(chart, f"A{anchor_row}")
    wb.save(path)
    return path


# ---- generation + archive --------------------------------------------------------------------
def generate(name, formats=("pdf",), by="on demand"):
    """Build once, render to each format, archive. Returns list of archive entries."""
    doc = build(name)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = ARCHIVE / name
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for f in formats:
        p = d / f"{name}_{stamp}.{f}"
        (to_pdf if f == "pdf" else to_xlsx)(doc, p)
        entry = dict(id=f"{name}/{p.name}", report=name, title=doc["title"], format=f, created=doc["generated"], by=by,
                     size=p.stat().st_size, subtitle=doc.get("subtitle", ""))
        p.with_suffix(p.suffix + ".json").write_text(json.dumps(entry))
        out.append(entry | dict(path=str(p)))
    return out, doc


def archive(limit=60):
    rows = []
    for meta in ARCHIVE.glob("*/*.json"):
        try:
            e = json.loads(meta.read_text())
        except ValueError:
            continue
        if Path(str(meta)[:-5]).exists():
            rows.append(e)
    return sorted(rows, key=lambda e: e["created"], reverse=True)[:limit]


def archive_path(entry_id):
    p = (ARCHIVE / entry_id).resolve()
    if ARCHIVE.resolve() not in p.parents or not p.exists() or p.suffix not in (".pdf", ".xlsx"):
        raise FileNotFoundError(entry_id)
    return p


def summary_line(doc):
    return " · ".join(f"{k}: {v}" for k, v, _ in doc.get("kpis", [])[:3])


if __name__ == "__main__":
    import sys
    for n in (sys.argv[1:] or list(REPORTS)):
        entries, _ = generate(n, ("pdf", "xlsx"))
        for e in entries:
            print(e["path"], e["size"])
