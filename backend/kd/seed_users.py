"""Demo accounts, seeded into data/auth.sqlite on first start (only when no users exist).

These are TEST credentials for the hackathon demo. They are shown on the login page while
KD_DEMO_LOGIN=1 (the default). For a real deployment set KD_DEMO_LOGIN=0, delete
data/auth.sqlite, and seed real accounts with strong passwords.
"""
DEMO_USERS = [
    dict(username="admin", name="Platform Admin", role="admin", password="KD-admin-2026"),
    dict(username="director", name="A. Deshmukh (Director, Mines)", role="mgmt", password="KD-board-2026"),
    dict(username="geologist", name="R. Iyer (Sr. Geologist)", role="planner", password="KD-geo-2026"),
    dict(username="operations", name="S. Patil (Shift In-charge)", role="ops", password="KD-ops-2026"),
]
