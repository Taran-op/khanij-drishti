"""MOIL Limited's PUBLISHED production figures, used to calibrate the virtual-mine telemetry.

Company-wide totals are real (sources below). What is NOT public, and is therefore an explicit
assumption: how output splits across individual mines, and the share our six mines take of the
company total. Both are named constants here and shown in the app.

Figures are lakh tonnes (1 lakh = 100,000 t). Periods are chained so they do not overlap; where a
period total was derived from two published numbers, the derivation is recorded.
"""
from datetime import date, timedelta

SOURCES = {
    "AR2025-26": "MOIL 64th Annual Report 2025-26, Directors' Report pp. 90–91 (backend.moil.nic.in)",
    "SO-FY25": "SteelOrbis, 'MOIL achieves highest-ever annual manganese ore output in FY 2024-25' (1.802 Mt, +2.7% y-o-y)",
    "MOIL-Q3FY26": "MOIL press release 5 Jan 2026 (moil.nic.in/upload_files/upload_news/fq3cseo.pdf): Q3 4.77, Apr–Dec 14.21",
    "HDFC-Q1FY26": "HDFC Sky, 'MOIL's manganese ore output in June quarter up 6.8%' (Q1 FY26 5.02; June 1.68)",
    "ANGEL-MAY25": "Angel One, 'MOIL begins FY26 with highest-ever May production' (May 2025 1.71)",
    "BS-OCT25": "Business Standard, 'MOIL achieves record production of 1.60 lakh tonnes in Oct'25'",
    "AM-Q1FY27": "Asian Metal, 'MOIL posts record 5.08 lakh MT output in Q1' (FY 2026-27)",
    "SO-SEP26": "SteelOrbis, 'MOIL sees manganese ore output rise 19% in September 2026' (181,000 t; Apr–Sep 961,000 t)",
}

# (start, end inclusive, lakh tonnes, how obtained, source keys)
PERIODS = [
    (date(2023, 1, 1), date(2023, 3, 31), None, "not found; filled at the FY 2023-24 daily rate", []),
    (date(2023, 4, 1), date(2024, 3, 31), 17.55, "FY 2024-25 18.02 ÷ 1.027 (reported +2.7% y-o-y)", ["SO-FY25"]),
    (date(2024, 4, 1), date(2024, 12, 31), 13.31, "Apr–Dec FY26 14.21 ÷ 1.068 (reported +6.8% y-o-y)", ["MOIL-Q3FY26"]),
    (date(2025, 1, 1), date(2025, 3, 31), 4.72, "FY 2024-25 18.03 − Apr–Dec 13.31", ["AR2025-26", "MOIL-Q3FY26"]),
    (date(2025, 4, 1), date(2025, 4, 30), 1.63, "Q1 FY26 5.02 − May 1.71 − Jun 1.68", ["HDFC-Q1FY26", "ANGEL-MAY25"]),
    (date(2025, 5, 1), date(2025, 5, 31), 1.71, "published", ["ANGEL-MAY25"]),
    (date(2025, 6, 1), date(2025, 6, 30), 1.68, "published", ["HDFC-Q1FY26"]),
    (date(2025, 7, 1), date(2025, 9, 30), 4.42, "Apr–Dec 14.21 − Q1 5.02 − Q3 4.77", ["MOIL-Q3FY26", "HDFC-Q1FY26"]),
    (date(2025, 10, 1), date(2025, 10, 31), 1.60, "published", ["BS-OCT25"]),
    (date(2025, 11, 1), date(2025, 12, 31), 3.17, "Q3 4.77 − Oct 1.60", ["MOIL-Q3FY26", "BS-OCT25"]),
    (date(2026, 1, 1), date(2026, 3, 31), 4.86, "FY 2025-26 19.07 − Apr–Dec 14.21", ["AR2025-26", "MOIL-Q3FY26"]),
    (date(2026, 4, 1), date(2026, 6, 30), 5.08, "published", ["AM-Q1FY27"]),
    (date(2026, 7, 1), date(2026, 8, 31), 2.80, "Apr–Sep 9.61 − Q1 5.08 − Sep 1.81 (rounded sources differ slightly)", ["SO-SEP26", "AM-Q1FY27"]),
    (date(2026, 9, 1), date(2026, 9, 30), 1.81, "published", ["SO-SEP26"]),
]
LATEST_PUBLISHED = date(2026, 9, 30)

# ---- explicit assumptions (not public) ------------------------------------------------------
SHARE_OF_COMPANY = 0.70  # our six mines' share of MOIL output (MOIL runs 10 mines; these six include the largest)
MINE_SPLIT = {"BLG": 0.33, "UKW": 0.15, "DGB": 0.17, "CHK": 0.12, "KDR": 0.11, "TRD": 0.12}
PLAN_GROWTH = 1.04  # monthly plan = same month a year earlier × 1.04 (MOIL's MoU targets are not published)


def daily_company_tonnes(d):
    """MOIL company output on day d (t/day), from the period it falls in."""
    for i, (s, e, lakh, _, _) in enumerate(PERIODS):
        if s <= d <= e:
            if lakh is None:  # unknown period → rate of the next known one
                s2, e2, l2, _, _ = PERIODS[i + 1]
                return l2 * 1e5 / ((e2 - s2).days + 1)
            return lakh * 1e5 / ((e - s).days + 1)
    # after the latest published month: hold the last published run-rate
    s, e, lakh, _, _ = PERIODS[-1]
    return lakh * 1e5 / ((e - s).days + 1)


def period_of(d):
    for s, e, lakh, how, src in PERIODS:
        if s <= d <= e:
            return (s, e)
    return (LATEST_PUBLISHED + timedelta(days=1), date.max)


def as_table():
    return [dict(start=str(s), end=str(e), lakh_t=lakh, derivation=how, sources=[SOURCES[k] for k in src])
            for s, e, lakh, how, src in PERIODS]
