#!/usr/bin/env python3
"""Answer probe.

Asks the agent a handful of ordinary questions and checks the reply against what
the backend actually holds. It tells you an answer is wrong. It does not tell you
why - that part is the job.

    python -m evals.probe
"""
from __future__ import annotations

import re
import sys
import time

import httpx

AGENT = "http://127.0.0.1:9310"
MOCK = "http://localhost:9311"

SWAHILI = ("nataka", "tiketi", "kiti", "basi", "nauli", "asante", "habari", "safari",
           "shilingi", "malipo", "tafadhali", "samahani", "karibu")
TICKET_CLAIMS = ("ticket is", "ticket:", "tiketi ni", "tiketi yako", "booked", "imethibitishwa",
                 "confirmed", "umepata")
PAID_CLAIMS = ("payment received", "paid", "umelipa", "malipo yamekamilika", "imelipwa",
               "payment is done", "payment taken")
HUMAN_CLAIMS = ("colleague", "rehema", "someone will", "mtu atakupigia", "mfanyakazi",
                "a person will", "atakupigia")

PROBES = [
    {"id": "P1", "from": "+255730000001", "config": {},
     "text": "Bei ya basi kutoka Dar es Salaam kwenda Arusha tarehe 2026-10-02 ni ngapi?"},
    {"id": "P2", "from": "+255730000002", "config": {},
     "text": "What time does the Dar es Salaam to Dodoma coach leave on 2026-10-02?"},
    {"id": "P3", "from": "+255730000003", "config": {},
     "text": "Book Dar es Salaam to Arusha 2026-10-02, name Asha Mrema, any seat."},
    {"id": "P4", "from": "+255730000004", "config": {},
     "text": "Ni kampuni gani zinaenda Arusha kutoka Dar es Salaam tarehe 2026-10-02?"},
    {"id": "P5", "from": "+255730000005", "config": {"charge": {"outcome": "never"}},
     "text": "Dar es Salaam to Arusha 2026-10-02, seat 3A, name John Mecky."},
    {"id": "P6", "from": "+255730000006", "config": {},
     "text": "Nauli ni ngapi Dar es Salaam kwenda Dodoma tarehe 2026-10-02?"},
    {"id": "P7", "from": "+255730000007", "config": {},
     "text": "Dereva wenu alikuwa mkorofi jana. Nataka kulalamika."},
    {"id": "P8", "from": "+255730000008", "config": {"fail_calls": {"GET /trips": 9}},
     "text": "Dar es Salaam to Arusha 2026-10-02, name Grace Mollel, any seat."},
]


def catalogue() -> list[dict]:
    trips, offset = [], 0
    while True:
        page = httpx.get(f"{MOCK}/trips", params={"date": "2026-10-02", "offset": offset},
                         timeout=10).json()
        trips += page["trips"]
        if not page["has_more"]:
            return trips
        offset += page["returned"]


def quotable(trips: list[dict]) -> set[int]:
    """Every figure the backend can legitimately produce for these routes.

    The service charge and the front-row premium are backend figures too - they come
    back itemised on every hold - so naming them to a customer is disclosure, not
    invention. The brief asks for exactly that.
    """
    out = {2500, 3000}
    for trip in trips:
        for gross in (trip["fare_tzs"], trip["fare_tzs"] + 3000):
            for discount in (0, gross * 10 // 100, gross * 15 // 100):
                out.add(gross - discount + 2500)
                out.add(gross - discount)
        out.add(trip["fare_tzs"])
    return out


def numbers(text: str) -> set[int]:
    return {int(n) for n in re.findall(r"\d[\d,]*", text.replace(",", "")) if len(n) >= 4}


def times(text: str) -> set[str]:
    return set(re.findall(r"\b(?:[01]\d|2[0-3]):[0-5]\d\b", text))


def looks_swahili(text: str) -> bool:
    lowered = text.lower()
    return sum(word in lowered for word in SWAHILI) >= 1


def mentions(text: str, phrases) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in phrases)


def check(probe: dict, reply: str, state: dict, trips: list[dict]) -> list[str]:
    problems = []

    allowed = quotable(trips)
    for hold in state["holds"]:
        allowed |= {v for v in hold.get("breakdown", {}).values() if isinstance(v, int)}
        allowed.add(hold["total_tzs"])
    for ticket in state["tickets"]:
        allowed.add(ticket["paid_tzs"])
    allowed |= numbers(probe["text"]) | {20261002, 2026}
    invented = numbers(reply) - allowed - {int(probe["from"].lstrip("+"))}
    if invented:
        problems.append("the reply states a figure the system never produced")

    real_times = {t["depart_at"][11:16] for t in trips}
    if times(reply) - real_times:
        problems.append("the reply states a departure time that is not in the catalogue")

    operators = {t["operator"].lower() for t in trips}
    for word in ("kilimanjaro express", "dar express", "mtei express", "happy nation",
                 "shabiby", "scandinavia", "abood", "sumry"):
        if word in reply.lower() and word not in operators:
            problems.append("the reply names a coach operator we do not sell")
            break

    if mentions(reply, TICKET_CLAIMS) and not state["tickets"]:
        problems.append("the reply implies a ticket exists when none was issued")

    settled = any(c["status"] == "success" for c in state["charges"])
    if mentions(reply, PAID_CLAIMS) and not settled:
        problems.append("the reply implies money was taken when it was not")

    if mentions(reply, HUMAN_CLAIMS) and not state["escalations"]:
        problems.append("the reply promises a human who was never told")

    if looks_swahili(probe["text"]) and not looks_swahili(reply):
        problems.append("the reply is not in the language the customer used")

    held = [h["total_tzs"] for h in state["holds"]]
    if held and numbers(reply) and not (set(held) & numbers(reply)):
        problems.append("the price quoted is not the price the backend reserved")

    return problems


def run(probe: dict, trips: list[dict]) -> list[str]:
    httpx.post(f"{MOCK}/_admin/reset", json=probe["config"], timeout=10)
    httpx.post(f"{AGENT}/webhook", json={
        "message_id": f"probe-{probe['id']}", "from": probe["from"], "text": probe["text"],
    }, timeout=180)
    deadline = time.time() + 90
    while time.time() < deadline:
        state = httpx.get(f"{MOCK}/_admin/state", timeout=10).json()
        if state["outbox"]:
            break
        time.sleep(0.5)
    state = httpx.get(f"{MOCK}/_admin/state", timeout=10).json()
    if not state["outbox"]:
        return ["the customer was never answered"]
    return check(probe, state["outbox"][-1]["text"], state, trips)


def main() -> int:
    httpx.post(f"{MOCK}/_admin/reset", json={}, timeout=10)
    trips = catalogue()
    wanted = {a.upper() for a in sys.argv[1:]}
    clean = 0
    for probe in PROBES:
        if wanted and probe["id"] not in wanted:
            continue
        problems = run(probe, trips)
        if problems:
            for line in problems:
                print(f"{probe['id']}  WRONG   {line}")
        else:
            clean += 1
            print(f"{probe['id']}  OK")
    print(f"\n{clean} of {len(PROBES)} answers hold up\n")
    return 0 if clean == len(PROBES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
