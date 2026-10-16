
import argparse
import os
import random
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "")
SUP_PASSWORD = "Conc-Test-Pass-1!"

LOCATION_TYPES = ["HOME", "STARTING_POINT", "BOARDING", "RETURN_STARTING_POINT"]
FAILS: list[str] = []

def check(name: str, ok: bool, detail="") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"   -> {detail}"))
    if not ok:
        FAILS.append(name)


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def body(r: requests.Response):
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:300]}


def api(method: str, path: str, token: str | None = None, **kw) -> requests.Response:
    headers = kw.pop("headers", {})
    if token:
        headers.update(auth(token))
    return requests.request(method, BASE + path, headers=headers, timeout=60, **kw)


def fire(calls):
    """Run every call at the same moment. Each call is f(session) -> Response.
    Returns [(status_code, json_body), ...] in the same order."""
    barrier = threading.Barrier(len(calls))

    def run(call):
        session = requests.Session()
        session.get(f"{BASE}/health", timeout=10)  # open the TCP connection first
        barrier.wait()  # everyone is ready: go
        try:
            r = call(session)
            return r.status_code, body(r)
        except Exception as exc:  # network error, timeout...
            return -1, {"_error": str(exc)}

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def codes(results) -> list[int]:
    return sorted(c for c, _ in results)


def no_5xx(label: str, results) -> None:
    bad = [(c, b) for c, b in results if c >= 500 or c == -1]
    check(f"{label}: no 500s / network errors", not bad, bad[:2])


def login(username: str, password: str) -> str:
    r = api("POST", "/api/auth/login", json={"username": username, "password": password})
    r.raise_for_status()
    return r.json()["access_token"]


def setup_staff():
    admin = login(ADMIN_USER, ADMIN_PASS)
    sups = []
    for _ in range(2):
        username = f"conc_sup_{uuid.uuid4().hex[:8]}"
        r = api(
            "POST", "/api/auth/staff", admin,
            json={"name": "Concurrency Test", "username": username,
                  "password": SUP_PASSWORD, "role": "SUPERVISOR"},
        )
        r.raise_for_status()
        sups.append(login(username, SUP_PASSWORD))
    return admin, sups


def make_draft(employee_id: str | None = None) -> dict:
    employee_id = employee_id or f"CONC-{uuid.uuid4().hex[:10].upper()}"
    r = api("POST", "/api/submissions/", json={
        "employee_id": employee_id,
        "employee_name": "Concurrency Tester",
        "organization_name": "ConcOrg",
        "public_bus_id": "".join(random.choices("0123456789", k=16)),
        "bus_number_going": "A1",
        "bus_number_return": "A2",
    })
    r.raise_for_status()
    d = r.json()
    sub = {"pid": d["public_id"], "token": d["worker_token"], "employee_id": d["employee_id"]}
    for i, location_type in enumerate(LOCATION_TYPES):
        api("POST", f"/api/submissions/{sub['pid']}/locations", sub["token"], json={
            "location_type": location_type, "address_text": f"addr {location_type}",
            "latitude": 9.0 + i * 0.01, "longitude": 38.7 + i * 0.01,
        }).raise_for_status()
    return sub


def submit_call(sub: dict):
    return lambda s: s.post(f"{BASE}/api/submissions/{sub['pid']}/submit",
                            headers=auth(sub["token"]), timeout=60)


def make_submitted() -> dict:
    sub = make_draft()
    r = api("POST", f"/api/submissions/{sub['pid']}/submit", sub["token"])
    r.raise_for_status()
    sub["number"] = r.json()["submission_number"]
    return sub


def cr_create_call(sub: dict):
    return lambda s: s.post(
        f"{BASE}/api/submissions/{sub['pid']}/change-requests",
        headers=auth(sub["token"]),
        json={"submission_number": sub["number"], "worker_comment": "please let me fix this"},
        timeout=60,
    )


def review_call(token: str, cr_id: str, action: str, comment: str):
    return lambda s: s.post(f"{BASE}/api/change-requests/{cr_id}/{action}",
                            headers=auth(token), json={"review_comment": comment}, timeout=60)


def worker_requests(sub: dict) -> list[dict]:
    r = api("GET", f"/api/submissions/{sub['pid']}/change-requests", sub["token"])
    r.raise_for_status()
    return r.json()


def internal_id(admin: str, employee_id: str) -> int:
    r = api("GET", "/api/admin/submissions", admin,
            params={"search": employee_id, "page_size": 100})
    r.raise_for_status()
    items = [i for i in r.json()["items"] if i["employee_id"] == employee_id]
    return items[0]["id"]


def audit_total(admin: str, action: str, submission_id: int) -> int:
    r = api("GET", "/api/admin/audit-logs", admin,
            params={"action": action, "submission_id": submission_id, "page_size": 200})
    r.raise_for_status()
    return r.json()["total"]


def audit_details_matching(admin: str, action: str, needle: str) -> int:
    r = api("GET", "/api/admin/audit-logs", admin, params={"action": action, "page_size": 200})
    r.raise_for_status()
    return sum(1 for i in r.json()["items"] if needle in (i.get("details") or ""))


def scenario_a(ctx, n: int) -> None:
    print(f"\n[A] Submission number uniqueness ({n} workers submit at once)")
    drafts = [make_draft() for _ in range(n)]
    res = fire([submit_call(d) for d in drafts])
    no_5xx("A1", res)
    check("A1 all submits succeed", codes(res) == [200] * n, codes(res))
    numbers = [b.get("submission_number") for c, b in res if c == 200]
    check("A1 every number is 9 digits, no leading zero",
          all(re.fullmatch(r"[1-9][0-9]{8}", x or "") for x in numbers), numbers)
    check("A1 NO duplicate submission numbers", len(set(numbers)) == len(numbers),
          sorted(numbers))

    print("[A2] Same draft submitted 8x at once (double-click / retry)")
    d = make_draft()
    res = fire([submit_call(d)] * 8)
    no_5xx("A2", res)
    c = codes(res)
    check("A2 exactly one 200, the rest 409", c.count(200) == 1 and c.count(409) == 7, c)
    got = api("GET", f"/api/submissions/{d['pid']}", d["token"]).json()
    winners = {b["submission_number"] for code, b in res if code == 200}
    check("A2 the stored number is the one the winner received",
          winners == {got["submission_number"]}, (winners, got["submission_number"]))


def scenario_b(ctx, n: int) -> None:
    admin, sups = ctx
    print("\n[B] Correction request limit")

    sub = make_submitted()
    res = fire([cr_create_call(sub)] * n)
    no_5xx("B1", res)
    c = codes(res)
    check(f"B1 {n} simultaneous requests -> exactly one 201", c.count(201) == 1, c)
    check("B1 only one request stored", len(worker_requests(sub)) == 1, worker_requests(sub))
    first = next(b for code, b in res if code == 201)

    api("POST", f"/api/change-requests/{first['public_id']}/reject", sups[0],
        json={"review_comment": "no"}).raise_for_status()

    print("[B2] One request remaining, two arrive at the same time")
    res = fire([cr_create_call(sub)] * 2)
    no_5xx("B2", res)
    c = codes(res)
    check("B2 exactly one 201 and one 409", c == [201, 409], c)
    check("B2 total stored == 2 (the limit)", len(worker_requests(sub)) == 2,
          len(worker_requests(sub)))
    second = next(b for code, b in res if code == 201)

    api("POST", f"/api/change-requests/{second['public_id']}/reject", sups[0],
        json={"review_comment": "no again"}).raise_for_status()

    print("[B3] Limit already used up, burst of requests")
    res = fire([cr_create_call(sub)] * n)
    no_5xx("B3", res)
    check("B3 every request refused with 409", codes(res) == [409] * n, codes(res))
    check("B3 total stays at 2", len(worker_requests(sub)) == 2, len(worker_requests(sub)))


def scenario_c(ctx) -> None:
    admin, sups = ctx
    print("\n[C] Two supervisors review the same change request")

    sub = make_submitted()
    cr = api("POST", f"/api/submissions/{sub['pid']}/change-requests", sub["token"],
             json={"submission_number": sub["number"], "worker_comment": "fix please"}).json()
    sid = internal_id(admin, sub["employee_id"])

    res = fire([review_call(sups[0], cr["public_id"], "approve", "approved-by-sup0"),
                review_call(sups[1], cr["public_id"], "reject", "rejected-by-sup1")])
    no_5xx("C1", res)
    check("C1 approve vs reject -> one 200, one 409", codes(res) == [200, 409], codes(res))

    final = worker_requests(sub)[0]
    winner_comment = next(b["review_comment"] for c, b in res if c == 200)
    check("C1 final status is APPROVED or REJECTED (not both)",
          final["status"] in ("APPROVED", "REJECTED"), final["status"])
    check("C1 review_comment belongs to the winner (loser did not overwrite)",
          final["review_comment"] == winner_comment, (final["review_comment"], winner_comment))
    if final["status"] == "APPROVED":
        check("C1 approved -> edit window set", final["edit_expires_at"] is not None, final)
    else:
        check("C1 rejected -> NO edit window", final["edit_expires_at"] is None, final)
    approved = audit_total(admin, "CHANGE_REQUEST_APPROVED", sid)
    rejected = audit_total(admin, "CHANGE_REQUEST_REJECTED", sid)
    check("C1 exactly one decision in the audit log, matching the final status",
          (approved, rejected) == ((1, 0) if final["status"] == "APPROVED" else (0, 1)),
          (approved, rejected))

    print("[C2] Eight reviewers approve the same request")
    sub = make_submitted()
    cr = api("POST", f"/api/submissions/{sub['pid']}/change-requests", sub["token"],
             json={"submission_number": sub["number"], "worker_comment": "fix please"}).json()
    sid = internal_id(admin, sub["employee_id"])
    res = fire([review_call(sups[i % 2], cr["public_id"], "approve", f"c{i}") for i in range(8)])
    no_5xx("C2", res)
    c = codes(res)
    check("C2 exactly one 200, seven 409", c.count(200) == 1 and c.count(409) == 7, c)
    check("C2 exactly one APPROVED audit row",
          audit_total(admin, "CHANGE_REQUEST_APPROVED", sid) == 1,
          audit_total(admin, "CHANGE_REQUEST_APPROVED", sid))


def scenario_d(ctx, n: int) -> None:
    admin, sups = ctx
    print(f"\n[D] Duplicate employee ID ({n} different drafts, same ID, submitted at once)")
    emp = f"DUP-{uuid.uuid4().hex[:10].upper()}"
    drafts = [make_draft(emp) for _ in range(n)]
    res = fire([submit_call(d) for d in drafts])
    no_5xx("D1", res)
    check("D1 all submits succeed (duplicate is a warning, not an error)",
          codes(res) == [200] * n, codes(res))

    r = api("GET", "/api/admin/notifications/duplicates", sups[0],
            params={"notification_status": "ALL", "page_size": 100})
    r.raise_for_status()
    mine = [i for i in r.json()["items"] if i["employee_id"] == emp]
    check("D1 exactly ONE notification for this employee ID", len(mine) == 1, len(mine))
    if mine:
        check("D1 notification is ACTIVE", mine[0]["status"] == "ACTIVE", mine[0]["status"])
        check(f"D1 notification lists all {n} submissions",
              mine[0]["submission_count"] == n, mine[0]["submission_count"])
    detected = audit_details_matching(admin, "DUPLICATE_EMPLOYEE_ID_DETECTED", emp)
    check(f"D1 audit log: detected by every submit except the first ({n - 1})",
          detected == n - 1, detected)

    print("[D2] Two supervisors acknowledge the same notification at once")
    if not mine:
        return
    nid = mine[0]["id"]
    res = fire([
        lambda s, t=sups[0]: s.post(f"{BASE}/api/admin/notifications/duplicates/{nid}/acknowledge",
                                    headers=auth(t), timeout=60),
        lambda s, t=sups[1]: s.post(f"{BASE}/api/admin/notifications/duplicates/{nid}/acknowledge",
                                    headers=auth(t), timeout=60),
    ])
    no_5xx("D2", res)
    check("D2 one 200 and one 409", codes(res) == [200, 409], codes(res))
    acked = audit_details_matching(admin, "DUPLICATE_ACKNOWLEDGED", emp)
    check("D2 exactly one DUPLICATE_ACKNOWLEDGED audit row", acked == 1, acked)


def scenario_e(ctx) -> None:
    admin, sups = ctx
    print("\n[E] One approved edit, six simultaneous resubmits")
    sub = make_submitted()
    cr = api("POST", f"/api/submissions/{sub['pid']}/change-requests", sub["token"],
             json={"submission_number": sub["number"], "worker_comment": "fix"}).json()
    api("POST", f"/api/change-requests/{cr['public_id']}/approve", sups[0],
        json={}).raise_for_status()
    sid = internal_id(admin, sub["employee_id"])

    res = fire([submit_call(sub)] * 6)
    no_5xx("E1", res)
    c = codes(res)
    check("E1 exactly one resubmit accepted", c.count(200) == 1 and c.count(409) == 5, c)
    check("E1 exactly one WORKER_RESUBMIT audit row",
          audit_total(admin, "WORKER_RESUBMIT", sid) == 1, audit_total(admin, "WORKER_RESUBMIT", sid))
    check("E1 request is COMPLETED", worker_requests(sub)[0]["status"] == "COMPLETED",
          worker_requests(sub)[0]["status"])
    got = api("GET", f"/api/submissions/{sub['pid']}", sub["token"]).json()
    check("E1 submission number unchanged", got["submission_number"] == sub["number"],
          (got["submission_number"], sub["number"]))

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3, help="repeat everything (races are probabilistic)")
    parser.add_argument("--n", type=int, default=12, help="parallel requests in the burst scenarios")
    parser.add_argument("--only", default="ABCDE", help="letters of scenarios to run, e.g. AC")
    args = parser.parse_args()

    if not ADMIN_USER or not ADMIN_PASS:
        print("Set ADMIN_USERNAME and ADMIN_PASSWORD first.")
        return 2

    ctx = setup_staff()
    only = args.only.upper()

    for rnd in range(1, args.rounds + 1):
        print(f"\n================ ROUND {rnd}/{args.rounds} ================")
        if "A" in only: scenario_a(ctx, args.n)
        if "B" in only: scenario_b(ctx, args.n)
        if "C" in only: scenario_c(ctx)
        if "D" in only: scenario_d(ctx, args.n)
        if "E" in only: scenario_e(ctx)

    print("\n" + "=" * 50)
    if FAILS:
        print(f"{len(FAILS)} check(s) FAILED:")
        for name in sorted(set(FAILS)):
            print("  -", name)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())