"""Position management — the half of the strategy that was never built.

E5 has always said "every fill gets a GTC exit placed immediately (50% credit)",
and E15 measured WHY: held to expiry the condor scores -0.021 to +0.076, and
modelling the day-7 / 50% exit flips every configuration positive. The exit is
not risk management bolted onto the edge. The exit IS the edge.

execute.build_close_args() existed. Nothing called it. An agent that opens
positions and never closes them does not have the expectancy we backtested.

Two responsibilities:
  1. place_exit()   — called at fill time, rests a GTC buy-to-close at 50% of
                      credit. Fills unattended, needs nobody watching.
  2. manage()       — each cycle, reconciles live positions against the target
                      and closes anything the resting order missed (a gap
                      through the strike, a partial fill, a cancelled order).

Both refuse rather than guess: a position we cannot price is reported, never
closed at an invented limit.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from deltax import execute

from datetime import date, datetime, timezone, timedelta

TAKE_PROFIT_FRACTION = 0.50    # E5 / E15

# E102 — TRAILING TAKE-PROFIT.
#
# Measured 2 Sep: the account peaked at $100,144 at 10:45 ET and finished the
# session at $99,559. $585 of the day's loss was profit handed back. There was
# a fixed target at 50% and nothing whatsoever between 0% and 50%, so anything
# that rose to 40% and reversed captured nothing.
#
# This is the operator's trailing stop, adapted to a credit spread. Three
# differences from the stock version, and each one shapes the numbers:
#
#   1. The sign inverts. We SOLD the spread, so profit is its value FALLING.
#      What trails is the fraction of credit captured, never a price.
#   2. Friction dominates. A trailing stop on a share costs a cent to trigger;
#      our measured round trip is 9-15% of the credit (E74). A tight trail fires
#      on quote noise and pays that spread every time - so the give-back
#      threshold must sit well outside it.
#   3. Theta pays us to wait. Every hour moves a short spread in our favour,
#      which a stock position never does. Room is close to free here, so the
#      trail is deliberately loose.
#
# E109 (3 Sep 10:35, operator instruction: "aggressively low, quick in and
# out"). ARM 0.25 -> 0.15, GIVE BACK 0.15 -> 0.10. This is the tightest setting
# the measured friction permits: our round trip is 9-15% of credit (E74), so a
# give-back of 10 points sits at the top of that band and still means a real
# reversal. A give-back of 5 would sit INSIDE it - the trail would fire on the
# bid/ask wobbling, pay the spread, and re-enter nothing, which is not "quick
# profit", it is paying the market maker to churn. Held at 10 for that reason.
TRAIL_ARM_AT = 0.15            # start trailing once 15% of credit is captured
TRAIL_GIVE_BACK = 0.10         # exit after surrendering 10 points from the peak
# E57: 2 -> 1 so MIN_DTE (2) stays STRICTLY above it, preserving the E45
# invariant. A position opened 2 Sep now closes 3 Sep rather than being
# time-stopped on arrival. This DOES hold one day deeper into the gamma zone,
# which E55 measured as the worse half of a bad trade - accepted only because
# DEMONSTRATION_MODE caps the position at a single contract.
TIME_STOP_DTE = 1              # gamma zone — close regardless of profit

# HARD DEADLINE. The contest is judged Fri 4 Sep 11:00 ET; anything still open
# is marked at whatever it happens to be worth, mid-decay. A position whose
# profit arrives after that date cannot pay us, so every position closes before
# it regardless of P&L (E37).
CONTEST_CLOSE = date(2026, 9, 4)
CONTEST_CLOSE_HOUR_ET = 10          # 10:00 ET, an hour before submission


def past_contest_deadline(now: datetime | None = None) -> bool:
    """True once the book must be flat for judging."""
    et = timezone(timedelta(hours=-4))
    now = (now or datetime.now(et)).astimezone(et)
    if now.date() > CONTEST_CLOSE:
        return True
    return now.date() == CONTEST_CLOSE and now.hour >= CONTEST_CLOSE_HOUR_ET


@dataclass
class Managed:
    symbol: str
    qty: int
    entry_credit: float
    current: Optional[float]
    dte: Optional[int]
    # E102: highest fraction of credit this structure has EVER captured, carried
    # in from state. Each cycle is a fresh process, so a peak held only in memory
    # would reset every five minutes and the trail could never trigger.
    peak_captured: Optional[float] = None
    # E119: WHERE entry_credit came from. The broker's per-leg avg_entry_price
    # does not sum to the multi-leg net that actually filled, so every exit
    # priced from it is priced from a number nobody submitted. The source is
    # carried on the record and written to every ledger event that derives
    # from it, so a cost-basis fallback can never pass for a real fill.
    entry_credit_source: str = "unspecified"

    @property
    def captured(self) -> Optional[float]:
        if self.current is None or self.entry_credit <= 0:
            return None
        return (self.entry_credit - self.current) / self.entry_credit

    def reason(self, now: datetime | None = None) -> Optional[str]:
        """Why this position should close now, or None to hold.

        The deadline outranks profit: a position that has not reached target by
        judging never will, because there is no time left to reach it.
        """
        if past_contest_deadline(now):
            return f"CONTEST DEADLINE — flat for judging {CONTEST_CLOSE}"
        c = self.captured
        if c is not None and c >= TAKE_PROFIT_FRACTION:
            return f"target hit — {c*100:.0f}% of credit captured"
        # E102: trailing take-profit. Only armed once the position has actually
        # made something worth protecting, and only fires on a give-back larger
        # than the round-trip cost of reacting to it.
        if (c is not None and self.peak_captured is not None
                and self.peak_captured >= TRAIL_ARM_AT
                and (self.peak_captured - c) >= TRAIL_GIVE_BACK):
            return (f"trailing exit — peaked at {self.peak_captured*100:.0f}% "
                    f"of credit, now {c*100:.0f}%, gave back "
                    f"{(self.peak_captured - c)*100:.0f} points")
        if self.dte is not None and self.dte <= TIME_STOP_DTE:
            return f"time stop — {self.dte} DTE, gamma zone"
        return None


def exit_limit(entry_credit: float) -> float:
    """Buy back at half the credit received. Round to a tradeable tick."""
    return round(entry_credit * (1.0 - TAKE_PROFIT_FRACTION), 2)


# ── E119: the credit an exit is priced from ──────────────────────────────────
# Alpaca paper allocates per-leg fill prices that do NOT sum to the multi-leg
# net. Measured 3 Sep: the UNH 390/380 put spread was submitted and filled at
# a 1.83 net credit limit, but the legs report 3.70 and 1.93 (1.77); the QCOM
# 175/180 call spread was submitted at 0.86 and the legs report 1.61 and 1.00
# (0.61). The sweep built `credit = short_entry - long_entry` from those legs,
# so the E118 healer rested the QCOM exit at 0.31 (50% of 0.61) instead of
# 0.43, and the E102/E109 trail measured `captured` against a credit 29%
# too small, so it armed late or never.
#
# A credit-limit mleg order cannot fill BELOW its limit, so the submitted
# limit is the floor of the true fill and the only number in the system that
# was actually agreed with the broker. When the ledger holds a `submit`
# record with result SUBMITTED for the structure's short leg, that limit is
# the entry credit. Cost basis is the fallback only, and is recorded as such.
CREDIT_SOURCE_SUBMITTED = "submitted_limit"     # ledger submit, result SUBMITTED
CREDIT_SOURCE_BASIS = "cost_basis"              # broker per-leg avg_entry_price


def _credit_vertical_short(legs: list) -> Optional[str]:
    """The short leg's OCC symbol if `legs` is a two-leg CREDIT vertical.

    A credit vertical sells the strike nearer the money: put spread short
    strike > long strike, call spread short strike < long strike. Anything
    else - a debit vertical (the E58 catalyst book), a condor submitted as
    four legs, a single leg, an unparseable symbol - returns None, so its
    limit can never be mistaken for a credit.
    """
    from deltax.reconcile import parse_occ
    if not isinstance(legs, list) or len(legs) != 2:
        return None
    short = long = None
    for leg in legs:
        if not isinstance(leg, dict):
            return None
        intent = str(leg.get("position_intent") or "")
        side = str(leg.get("side") or "")
        occ = parse_occ(str(leg.get("symbol") or ""))
        if occ is None:
            return None
        if intent == "sell_to_open" or (not intent and side == "sell"):
            short = (leg["symbol"], occ)
        elif intent == "buy_to_open" or (not intent and side == "buy"):
            long = (leg["symbol"], occ)
        else:
            return None                     # a *_to_close leg: not an open
    if short is None or long is None:
        return None
    so, lo = short[1], long[1]
    if (so["underlying"], so["expiry"], so["right"]) != (
            lo["underlying"], lo["expiry"], lo["right"]):
        return None
    if so["right"] == "put" and so["strike"] > lo["strike"]:
        return short[0]
    if so["right"] == "call" and so["strike"] < lo["strike"]:
        return short[0]
    return None


def submitted_credits(entries) -> dict:
    """short-leg OCC symbol -> the SUBMITTED opening credit for that structure.

    Walks ledger entries (the dicts Ledger.entries() returns) and keeps, per
    short leg, the LATEST `submit` event whose result is SUBMITTED and whose
    legs form a credit vertical. Dry runs, refusals, failures and closing
    orders are ignored: only an order the broker accepted can have filled.
    Latest wins so a structure re-opened after a close is priced from its
    own entry, not an earlier one. Never raises on a malformed entry - an
    unreadable record is simply not evidence.
    """
    out: dict = {}
    for i, e in enumerate(entries or []):
        try:
            if not isinstance(e, dict) or e.get("kind") != "event":
                continue
            ev = e.get("event") or {}
            if ev.get("action") != "submit" or ev.get("result") != "SUBMITTED":
                continue
            limit = float(ev.get("limit_price"))
            if not limit > 0:
                continue
            short = _credit_vertical_short(ev.get("legs") or [])
            if short is None:
                continue
            seq = e.get("seq")
            order = float(seq) if isinstance(seq, (int, float)) else float(i)
            prev = out.get(short)
            if prev is not None and prev["_order"] > order:
                continue
            out[short] = {"credit": round(limit, 2), "order_id": ev.get("order_id"),
                          "ledger_seq": seq, "qty": ev.get("qty"),
                          "ts_utc": e.get("ts_utc"), "_order": order}
        except (TypeError, ValueError, AttributeError):
            continue
    for v in out.values():
        v.pop("_order", None)
    return out


def entry_credit_for(short_symbol: str, basis_credit: float,
                     submitted: Optional[dict]) -> tuple:
    """(credit, source, detail) for one structure.

    The submitted limit wins whenever the ledger has one. Cost basis is the
    fallback for a structure the agent has no submit record for - opened by
    hand, or before the ledger existed - and the detail says so in words.
    """
    rec = (submitted or {}).get(short_symbol)
    basis = round(float(basis_credit), 2)
    if rec is not None:
        return (float(rec["credit"]), CREDIT_SOURCE_SUBMITTED,
                {"submitted_limit": float(rec["credit"]),
                 "cost_basis_credit": basis,
                 "order_id": rec.get("order_id"),
                 "ledger_seq": rec.get("ledger_seq")})
    return (float(basis_credit), CREDIT_SOURCE_BASIS,
            {"cost_basis_credit": basis,
             "note": "no SUBMITTED open in the ledger for this short leg; "
                     "credit is the broker's per-leg allocation, which does "
                     "not sum to the multi-leg fill (E119)"})


def place_exit(legs: list, qty: int, entry_credit: float, *, ledger=None,
               dry_run: bool = True,
               entry_credit_source: Optional[str] = None) -> dict:
    """Rest a GTC closing order the moment a position is opened (E5).

    Placed at entry, not watched for later: an exit that depends on the agent
    being alive at the right minute is not an exit.
    """
    limit = exit_limit(entry_credit)
    args = execute.build_close_args(legs, qty, limit)
    record = {"action": "exit_order", "qty": qty, "limit_price": limit,
              "entry_credit": round(entry_credit, 2),
              # E119: which number the limit was derived from - never silent
              "entry_credit_source": entry_credit_source or "unspecified",
              "target_fraction": TAKE_PROFIT_FRACTION,
              "command": "alpaca " + " ".join(args), "dry_run": dry_run}
    if dry_run:
        record["result"] = "DRY_RUN — exit not placed"
    elif not execute.orders_enabled():
        record["result"] = "REFUSED — orders not enabled"
    else:
        try:
            execute.preflight()
            execute._run(args)
            record["result"] = "PLACED"
        except Exception as e:
            record["result"] = f"FAILED — {type(e).__name__}: {str(e)[:90]}"
    if ledger is not None:
        ledger.record_raw(record)
    return record


def manage(positions: list, *, ledger=None, dry_run: bool = True,
           closer=None) -> dict:
    """Sweep open positions and close anything that has met its exit rule.

    E78: this used to RECORD a close and never submit one. On a triggered stop
    or time stop it wrote `{"action": "close", "result": "SWEEP"}` to the
    ledger, appended the symbol to `closed`, and left the position open - the
    board then rendered "position closed" for something still live. A sweep
    that reports an action it did not take is worse than no sweep, because it
    removes the operator's reason to look.

    `closer` is the callable that actually submits: closer(symbol, qty) -> dict.
    Without one the sweep still reports, but every entry is marked explicitly
    as NOT closed so the distinction can never be lost again.
    """
    closed, held, unpriceable, failed = [], [], [], []
    for p in positions:
        why = p.reason()
        if p.captured is None:
            unpriceable.append(p.symbol)      # never closed at a guessed price
            continue
        if not why:
            held.append(p.symbol)
            continue
        rec = {"action": "close", "symbol": p.symbol, "qty": p.qty,
               "reason": why, "captured": round(p.captured, 4),
               # E119: `captured` is a fraction OF this credit; say which one
               "entry_credit": round(p.entry_credit, 2),
               "entry_credit_source": p.entry_credit_source,
               "dry_run": dry_run}
        if dry_run:
            # E116: a dry run used to skip the closer entirely, so no dry run
            # could ever exercise the exit path - the trail's replace logic was
            # unreachable in analysis and its failure went unseen for hours.
            # When a closer is wired, call it: it honours dry_run itself and
            # returns a DRY_RUN record, so the path is traced without an order.
            if closer is not None:
                try:
                    out = closer(p.symbol, p.qty)
                    rec["result"] = str(out.get("result", "DRY_RUN"))
                    rec["submitted"] = False
                except Exception as e:
                    rec["result"] = f"DRY_RUN CLOSE PATH FAILED — {type(e).__name__}: {str(e)[:100]}"
                    rec["submitted"] = False
                    failed.append((p.symbol, f"dry:{type(e).__name__}"))
                    if ledger is not None:
                        ledger.record_raw(rec)
                    continue
            else:
                rec["result"] = "DRY_RUN — not closed"
            closed.append((p.symbol, why))
        elif closer is None:
            # Report honestly: the rule fired and nothing acted on it.
            rec["result"] = "NOT CLOSED — no closer wired; resting GTC order is the only exit"
            rec["submitted"] = False
            failed.append((p.symbol, "no closer wired"))
        else:
            try:
                out = closer(p.symbol, p.qty)
                rec["result"] = str(out.get("result", "SUBMITTED"))
                rec["submitted"] = True
                closed.append((p.symbol, why))
            except Exception as e:
                rec["result"] = f"CLOSE FAILED — {type(e).__name__}: {str(e)[:120]}"
                rec["submitted"] = False
                failed.append((p.symbol, f"{type(e).__name__}"))
        if ledger is not None:
            ledger.record_raw(rec)
    return {"closed": closed, "held": held, "unpriceable": unpriceable,
            "failed": failed}


# ── E102: peak persistence ───────────────────────────────────────────────────
# A trailing exit needs to remember the best a position has ever been. Each
# cycle is a fresh process invoked by cron, so an in-memory high-water mark
# would reset every five minutes and the trail could never fire. Peaks live in
# state/peaks.json, keyed by the short leg's OCC symbol.
#
# Reading is fail-soft, unlike the freeze state: a lost peak means the trail
# simply does not fire and the fixed 50% target and time stop still stand, so
# the safe direction here is to carry on rather than to refuse.

import json as _json
import os as _os

PEAKS_PATH = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
    "state", "peaks.json")


def load_peaks(path: Optional[str] = None) -> dict:
    """Best captured fraction seen per structure. {} on any problem."""
    try:
        with open(path or PEAKS_PATH) as fh:
            d = _json.load(fh)
        return {k: float(v) for k, v in d.items()
                if isinstance(v, (int, float))}
    except Exception:
        return {}


def update_peaks(managed: list, path: Optional[str] = None) -> dict:
    """Raise each structure's high-water mark, then persist.

    Peaks only ever RISE while a position is open. A structure that has closed
    is dropped, so a symbol later reopened starts fresh rather than inheriting
    a stale peak from a different trade and triggering the trail immediately.
    """
    path = path or PEAKS_PATH
    peaks = load_peaks(path)
    live = set()
    for m in managed:
        c = m.captured
        if c is None:
            continue
        live.add(m.symbol)
        prev = peaks.get(m.symbol)
        peaks[m.symbol] = c if prev is None else max(prev, c)
    peaks = {k: v for k, v in peaks.items() if k in live}
    try:
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            _json.dump(peaks, fh, indent=1, sort_keys=True)
        _os.replace(tmp, path)          # atomic; a reader never sees a partial file
    except Exception:
        pass                            # fail soft - see the note above
    return peaks
