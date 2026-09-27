#!/usr/bin/env python3
"""Synthetic reference checks for the pi-orderbook v2 design (stdlib only).

Checks the design's algorithm definitions on hand-built inputs. It does not
connect to IBKR, does not run Pi, and is not a production test of the package.
Run: python3 docs/l2-v2-reference-checks.py
"""
from __future__ import annotations

import unittest
from decimal import Decimal as D


def band_status(side, rows, requested_rows, lower, upper, complete_side_verified=False):
    """Return (status, observed, band_total) for one side of one returned depth view."""
    if side not in ("bid", "ask") or lower > upper:
        raise ValueError("invalid band")
    positive = [(p, q) for p, q in rows if q > 0]
    if not positive:
        return ("unavailable", None, None)
    prices = [p for p, _ in positive]
    if prices != sorted(prices, reverse=(side == "bid")):
        raise ValueError("returned rows are not ordered best to worst")
    observed = sum((q for p, q in positive if lower <= p <= upper), D(0))
    # Fewer rows than requested means the whole side only after this behaviour
    # has been verified for the mode and the initial burst has settled.
    if complete_side_verified and len(rows) < requested_rows:
        return ("determined_in_view", observed, observed)
    worst = prices[-1]
    if side == "ask":
        if upper < worst:
            return ("determined_in_view", observed, observed)
        return ("partial_at_tail", observed, None) if lower <= worst else ("beyond_view", None, None)
    if lower > worst:
        return ("determined_in_view", observed, observed)
    return ("partial_at_tail", observed, None) if upper >= worst else ("beyond_view", None, None)


def ofi_increment(prev, cur):
    """Cont-Kukanov-Stoikov (2014) e_n from (bid_px, bid_qty, ask_px, ask_qty)."""
    pb0, qb0, pa0, qa0 = prev
    pb1, qb1, pa1, qa1 = cur
    e = D(0)
    if pb1 >= pb0:
        e += qb1
    if pb1 <= pb0:
        e -= qb0
    if pa1 <= pa0:
        e -= qa1
    if pa1 >= pa0:
        e += qa0
    return e


def print_coverage(decrease, prints, lower, upper, view_venues):
    in_band = [(v, p, q) for v, p, q in prints if lower <= p <= upper]
    matched = sum((q for v, _, q in in_band if v in view_venues), D(0))
    other = sum((q for v, _, q in in_band if v not in view_venues), D(0))
    return {"matched": matched, "other_venues": other,
            "coverage": min(D(1), matched / decrease) if decrease > 0 else None,
            "excess_over_decrease": max(D(0), matched - decrease)}


def static_sweep(rows, quantity, side="buy", limit=None):
    if quantity <= 0 or side not in ("buy", "sell"):
        raise ValueError("invalid scenario")
    remaining, notional = quantity, D(0)
    for price, size in sorted(rows, key=lambda r: r[0], reverse=(side == "sell")):
        if limit is not None and ((side == "buy" and price > limit) or (side == "sell" and price < limit)):
            continue
        take = min(size, remaining)
        remaining -= take
        notional += price * take
        if remaining == 0:
            break
    covered = quantity - remaining
    return {"covered": covered, "uncovered": remaining,
            "covered_vwap": notional / covered if covered else None}


def validity_stats(intervals, window):
    valid = sum(b - a for a, b, v, _ in intervals if v)
    met = sum(b - a for a, b, v, c in intervals if v and c)
    run = longest = 0
    for a, b, v, c in intervals:
        run = run + (b - a) if (v and c) else 0
        longest = max(longest, run)
    return {"valid": valid, "fraction": D(met) / D(valid) if valid else None,
            "coverage": D(valid) / D(window), "longest": longest}


def k_and(values):
    if any(v is False for v in values):
        return False
    return True if all(v is True for v in values) else None


def k_or(values):
    if any(v is True for v in values):
        return True
    return False if all(v is False for v in values) else None


def evaluate(cond, leaves):
    if "all" in cond:
        return k_and([evaluate(c, leaves) for c in cond["all"]])
    if "any" in cond:
        return k_or([evaluate(c, leaves) for c in cond["any"]])
    return leaves[cond["leaf"]]


def validate_condition(cond, max_depth=2, max_leaves=6):
    def walk(node, depth):
        children = node.get("all") or node.get("any")
        if children is None:
            return 1
        if depth >= max_depth or not children:
            raise ValueError("combinator too deep or empty")
        return sum(walk(c, depth + 1) for c in children)
    if walk(cond, 0) > max_leaves:
        raise ValueError("too many leaves")


class WatchMachine:
    """armed -> pending(hold) -> fire -> cooldown(requires exit) -> armed; unknown suspends."""

    def __init__(self, hold_s, cooldown_s, expires_at, max_fires):
        self.hold_s, self.cooldown_s, self.expires_at, self.max_fires = hold_s, cooldown_s, expires_at, max_fires
        self.state, self.pending_since, self.fires, self.last_fire, self.exited = "armed", None, 0, None, False

    def step(self, t, value):
        if self.state in ("expired", "done"):
            return None
        if t >= self.expires_at:
            self.state = "expired"
            return None
        if value is None:
            if self.state in ("armed", "pending"):
                self.state = "suspended_data"
            self.pending_since = None
            return None
        if self.state == "suspended_data":
            self.state = "armed"
        if self.state == "cooldown":
            if not value:
                self.exited = True
            if not (self.exited and t - self.last_fire >= self.cooldown_s):
                return None
            self.state = "armed"
        if self.state == "armed":
            if not value:
                return None
            self.state, self.pending_since = "pending", t
        if not value:
            self.state, self.pending_since = "armed", None
            return None
        if t - self.pending_since >= self.hold_s:
            self.fires, self.last_fire, self.exited = self.fires + 1, t, False
            self.state = "done" if self.fires >= self.max_fires else "cooldown"
            return "fire"
        return None


class WakeBudget:
    def __init__(self, per_hour, urgent_per_hour, min_gap_s):
        self.limits = {False: per_hour, True: urgent_per_hour}
        self.sent = {False: [], True: []}
        self.min_gap_s, self.last_normal = min_gap_s, None

    def allow(self, t, urgent=False):
        bucket = self.sent[urgent]
        while bucket and t - bucket[0] >= 3600:
            bucket.pop(0)
        if len(bucket) >= self.limits[urgent]:
            return False
        if not urgent and self.last_normal is not None and t - self.last_normal < self.min_gap_s:
            return False
        bucket.append(t)
        if not urgent:
            self.last_normal = t
        return True


PRIORITY = {"pinned": 3, "task": 2, "promoted": 1, "ephemeral": 0}


def choose_victim(request_class, held):
    """Only lower, reclaimable classes; pinned and watch-bound task subscriptions are never taken."""
    candidates = [h for h in held if h["cls"] in ("promoted", "ephemeral")
                  and PRIORITY[h["cls"]] < PRIORITY[request_class]]
    if not candidates:
        return None
    return min(candidates, key=lambda h: (PRIORITY[h["cls"]], h["last_used"]))["symbol"]


ASK = [(D("100.02"), D(400)), (D("100.04"), D(500)), (D("100.06"), D(600))]
BID = [(D("100.00"), D(100)), (D("99.99"), D(200)), (D("99.98"), D(300))]


class V2Checks(unittest.TestCase):
    def test_01_better_than_best_is_determined_zero(self):
        self.assertEqual(band_status("ask", ASK, 3, D("100.00"), D("100.01")), ("determined_in_view", 0, 0))
        self.assertEqual(band_status("bid", BID, 3, D("100.01"), D("100.02")), ("determined_in_view", 0, 0))

    def test_02_band_straddling_best_is_determined(self):
        self.assertEqual(band_status("ask", ASK, 3, D("100.01"), D("100.03")), ("determined_in_view", 400, 400))
        self.assertEqual(band_status("ask", ASK, 3, D("100.03"), D("100.03")), ("determined_in_view", 0, 0))

    def test_03_tail_and_beyond(self):
        self.assertEqual(band_status("ask", ASK, 3, D("100.05"), D("100.07")), ("partial_at_tail", 600, None))
        self.assertEqual(band_status("ask", ASK, 3, D("100.07"), D("100.08")), ("beyond_view", None, None))
        self.assertEqual(band_status("bid", BID, 3, D("99.97"), D("99.99")), ("partial_at_tail", 500, None))
        self.assertEqual(band_status("bid", BID, 3, D("99.95"), D("99.97")), ("beyond_view", None, None))
        same_price_tail = ASK + [(D("100.06"), D(700))]
        self.assertEqual(band_status("ask", same_price_tail, 4, D("100.06"), D("100.06")), ("partial_at_tail", 1300, None))

    def test_04_short_side_only_after_verification(self):
        short = ASK[:2]
        self.assertEqual(band_status("ask", short, 10, D("100.07"), D("100.08")), ("beyond_view", None, None))
        self.assertEqual(band_status("ask", short, 10, D("100.07"), D("100.08"), complete_side_verified=True),
                         ("determined_in_view", 0, 0))

    def test_05_ofi_signs(self):
        base = (D("100.00"), D(500), D("100.02"), D(300))
        self.assertEqual(ofi_increment(base, (D("100.00"), D(700), D("100.02"), D(300))), 200)
        self.assertEqual(ofi_increment(base, (D("100.00"), D(500), D("100.02"), D(100))), 200)
        self.assertEqual(ofi_increment(base, (D("100.01"), D(200), D("100.02"), D(300))), 200)
        self.assertEqual(ofi_increment(base, (D("100.00"), D(500), D("100.01"), D(150))), -150)
        self.assertEqual(ofi_increment(base, (D("99.99"), D(800), D("100.02"), D(300))), -500)

    def test_06_print_coverage_filters_venues(self):
        prints = [("NASDAQ", D("231.02"), D(1000)), ("IEX", D("231.03"), D(500)),
                  ("NYSE", D("231.02"), D(2000)), ("NASDAQ", D("231.08"), D(400)), ("ADF", D("231.02"), D(300))]
        r = print_coverage(D(3300), prints, D("231.00"), D("231.05"), {"NASDAQ", "IEX"})
        self.assertEqual((r["matched"], r["other_venues"], r["excess_over_decrease"]), (1500, 2300, 0))
        self.assertEqual(r["coverage"], D(1500) / D(3300))
        self.assertEqual(print_coverage(D(400), prints, D("231.00"), D("231.05"), {"NASDAQ", "IEX"})["excess_over_decrease"], 1100)

    def test_07_static_sweep(self):
        asks = [(D("100.02"), D(400)), (D("100.03"), D(400))]
        self.assertEqual(static_sweep(asks, D(1000)), {"covered": 800, "uncovered": 200, "covered_vwap": D("100.025")})
        self.assertEqual(static_sweep(asks, D(1000), limit=D("100.02"))["uncovered"], 600)
        bids = [(D("100.00"), D(100)), (D("99.99"), D(200))]
        self.assertEqual(static_sweep(bids, D(250), "sell", D("99.99"))["covered_vwap"], D("99.994"))

    def test_08_validity_integrals_split_on_gap(self):
        r = validity_stats([(0, 2, True, True), (2, 4, False, False), (4, 7, True, True), (7, 10, True, False)], 10)
        self.assertEqual((r["valid"], r["fraction"], r["coverage"], r["longest"]), (8, D("0.625"), D("0.8"), 3))

    def test_09_three_valued_logic_and_limits(self):
        self.assertIsNone(evaluate({"all": [{"leaf": "a"}, {"leaf": "u"}]}, {"a": True, "u": None}))
        self.assertFalse(evaluate({"all": [{"leaf": "f"}, {"leaf": "u"}]}, {"f": False, "u": None}))
        self.assertTrue(evaluate({"any": [{"leaf": "a"}, {"leaf": "u"}]}, {"a": True, "u": None}))
        self.assertIsNone(evaluate({"any": [{"leaf": "f"}, {"leaf": "u"}]}, {"f": False, "u": None}))
        validate_condition({"all": [{"any": [{"leaf": "a"}, {"leaf": "b"}]}, {"leaf": "c"}]})
        with self.assertRaises(ValueError):
            validate_condition({"all": [{"any": [{"all": [{"leaf": "a"}]}]}]})
        with self.assertRaises(ValueError):
            validate_condition({"any": [{"leaf": str(i)} for i in range(7)]})

    def test_10_watch_hold_gap_cooldown_ttl(self):
        w = WatchMachine(hold_s=3, cooldown_s=10, expires_at=1000, max_fires=2)
        self.assertEqual([w.step(t, True) for t in range(4)], [None, None, None, "fire"])
        self.assertEqual([w.step(t, True) for t in range(4, 21)], [None] * 17)
        self.assertIsNone(w.step(21, False))
        self.assertEqual([w.step(t, True) for t in range(22, 26)], [None, None, None, "fire"])
        self.assertEqual(w.state, "done")
        g = WatchMachine(hold_s=3, cooldown_s=10, expires_at=1000, max_fires=1)
        seq = [g.step(0, True), g.step(1, None)] + [g.step(t, True) for t in range(2, 6)]
        self.assertEqual(seq, [None, None, None, None, None, "fire"])
        e = WatchMachine(hold_s=3, cooldown_s=10, expires_at=2, max_fires=1)
        self.assertEqual([e.step(t, True) for t in range(4)], [None, None, None, None])
        self.assertEqual(e.state, "expired")

    def test_11_wake_budget(self):
        b = WakeBudget(per_hour=3, urgent_per_hour=2, min_gap_s=20)
        self.assertEqual([b.allow(t) for t in (0, 10, 20, 40, 60)], [True, False, True, True, False])
        self.assertTrue(b.allow(61, urgent=True))
        self.assertTrue(b.allow(3600))

    def test_12_capacity_victims_never_pinned_or_task(self):
        held = [{"symbol": "A", "cls": "pinned", "last_used": 0}, {"symbol": "B", "cls": "ephemeral", "last_used": 10},
                {"symbol": "C", "cls": "ephemeral", "last_used": 5}, {"symbol": "P", "cls": "promoted", "last_used": 1}]
        self.assertEqual(choose_victim("pinned", held), "C")
        self.assertEqual(choose_victim("task", held[:1] + held[3:]), "P")
        self.assertIsNone(choose_victim("ephemeral", held))
        self.assertIsNone(choose_victim("pinned", [{"symbol": "T", "cls": "task", "last_used": 0}]))

    def test_13_known_at_cut(self):
        records = [{"seq": 9, "source_time": 99}, {"seq": 11, "source_time": 98}]
        self.assertEqual([r for r in records if r["seq"] <= 10], [{"seq": 9, "source_time": 99}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
