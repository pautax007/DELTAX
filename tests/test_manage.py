"""Exit management. The 50% exit is where the measured edge lives (E15)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from deltax.manage import (Managed, exit_limit, place_exit, manage,
                           TAKE_PROFIT_FRACTION, TIME_STOP_DTE,
                           past_contest_deadline, CONTEST_CLOSE,
                           CONTEST_CLOSE_HOUR_ET)
from datetime import datetime, timezone, timedelta
from deltax.execute import Leg

passed = failed = 0
def check(n, c, d=""):
    global passed, failed
    if c: passed += 1; print(f"  ✓ {n}")
    else: failed += 1; print(f"  ✗ {n}  {d}")

print("\n── exit price ──")
check("half of a 2.30 credit is 1.15", exit_limit(2.30) == 1.15, str(exit_limit(2.30)))
check("half of a 0.92 credit is 0.46", exit_limit(0.92) == 0.46, str(exit_limit(0.92)))
check("rounded to a tradeable tick", exit_limit(1.111) == round(1.111*0.5, 2))
check("target matches E5/E15", TAKE_PROFIT_FRACTION == 0.50)

print("\n── when to close ──")
check("56% captured closes", Managed("SPY",7,2.30,1.02,7).reason() is not None)
check("exactly 50% closes", Managed("SPY",7,2.00,1.00,7).reason() is not None)
check("49% holds", Managed("SPY",7,2.00,1.02,7).reason() is None)
check("12% holds", Managed("SPY",7,2.25,1.98,7).reason() is None)
# E22/E57: derive from the live constant. These hardcoded 2 and broke silently
# when E57 cut TIME_STOP_DTE to 1 - the value has moved twice now, so assert the
# BEHAVIOUR at the threshold rather than a particular number of days.
check("at the time stop, closes even on a small profit",
      "time stop" in (Managed("IWM",18,0.92,0.61,TIME_STOP_DTE).reason() or ""),
      f"TIME_STOP_DTE={TIME_STOP_DTE}")
check("inside the time stop, closes even at a LOSS",
      Managed("IWM",18,0.92,1.40,max(TIME_STOP_DTE-1,0)).reason() is not None)
check("one day outside the time stop, a small profit holds",
      Managed("IWM",18,0.92,0.85,TIME_STOP_DTE+1).reason() is None)
check("7 DTE with small profit holds", Managed("IWM",18,0.92,0.85,7).reason() is None)

print("\n── never guess a price ──")
m = Managed("MA",7,2.40,None,7)
check("unpriceable position reports no capture", m.captured is None)
check("unpriceable position is NOT closed", m.reason() is None)
check("zero entry credit does not divide by zero", Managed("X",1,0.0,1.0,7).captured is None)

print("\n── sweep ──")
out = manage([Managed("SPY",7,2.30,1.02,7),                 # target hit
              Managed("QQQ",7,2.25,1.98,7),                 # hold
              Managed("IWM",18,0.92,0.61,TIME_STOP_DTE),    # time stop
              Managed("MA",7,2.40,None,7)],                 # unpriceable
             dry_run=True)
check("closes the two that qualify", len(out["closed"]) == 2, str(out["closed"]))
check("holds the one that does not", out["held"] == ["QQQ"], str(out["held"]))
check("reports the unpriceable one rather than closing it",
      out["unpriceable"] == ["MA"], str(out["unpriceable"]))

print("\n── exit order shape ──")
legs = [Leg("SPY260911P00750000","sell"), Leg("SPY260911P00745000","buy")]
r = place_exit(legs, 7, 2.30, dry_run=True)
check("dry run does not place", "DRY_RUN" in r["result"])
check("limit is half the credit", r["limit_price"] == 1.15)
check("order rests GTC", "gtc" in r["command"])
check("sides are flipped to close", "buy_to_close" in r["command"] and "sell_to_close" in r["command"])
check("entry credit is recorded", r["entry_credit"] == 2.30)

print("\n── ledger ──")
class L:
    def __init__(self): self.rows=[]
    def record_raw(self, d): self.rows.append(d)
led = L(); place_exit(legs, 7, 2.30, ledger=led, dry_run=True)
check("exit order is recorded", len(led.rows) == 1 and led.rows[0]["action"] == "exit_order")
led2 = L(); manage([Managed("SPY",7,2.30,1.02,7)], ledger=led2, dry_run=True)
check("close is recorded with its reason",
      led2.rows and "target hit" in led2.rows[0]["reason"])

print("\n── E91: the contest deadline close ──")
# Found by MUTATION TESTING: deleting the `past_contest_deadline` branch from
# Managed.reason() broke NOT ONE test. It is the single most important control
# in the system right now - every open position expires AFTER Fri 4 Sep
# judging, so this branch is what flattens the book. Same shape as E43 and E74:
# a rule that is written down and enforced by nothing.
ET = timezone(timedelta(hours=-4))
# ABSOLUTE times, deliberately NOT derived from CONTEST_CLOSE_HOUR_ET: a test
# that builds its expectation out of the constant it is testing moves whenever
# that constant moves, and can never fail. Pin the judging moment instead.
_before = datetime(2026, 9, 4,  9, 59, tzinfo=ET)
_at     = datetime(2026, 9, 4, 10,  0, tzinfo=ET)
BEFORE_DEADLINE = datetime(2026, 9, 3, 12, 0, tzinfo=ET)
_after  = datetime(2026, 9, 5,  9,  0, tzinfo=ET)
check("E91 the judging date is pinned to Fri 4 Sep 2026",
      str(CONTEST_CLOSE) == "2026-09-04", str(CONTEST_CLOSE))
check("E91 the flatten hour is pinned to 10:00 ET",
      CONTEST_CLOSE_HOUR_ET == 10, str(CONTEST_CLOSE_HOUR_ET))
check("E91 before the cutoff hour it is not the deadline",
      past_contest_deadline(_before) is False, str(_before))
check("E91 at the cutoff hour it IS the deadline",
      past_contest_deadline(_at) is True, str(_at))
check("E91 the day after is past the deadline",
      past_contest_deadline(_after) is True, str(_after))

# reason() must fire on it - and must OUTRANK profit, because a position that
# has not reached target by judging never will: there is no time left.
_held = Managed(symbol="SPY260918P00760000", qty=1, entry_credit=2.00,
                current=1.90, dte=14)          # only 5% captured, far from target
check("E91 a position nowhere near target still closes at the deadline",
      "DEADLINE" in (_held.reason(_at) or ""), str(_held.reason(_at)))
check("E91 and the same position HOLDS before the deadline",
      _held.reason(_before) is None, str(_held.reason(_before)))
check("E91 the deadline outranks the take-profit reason",
      "DEADLINE" in (Managed(symbol="X", qty=1, entry_credit=2.0,
                             current=0.5, dte=14).reason(_at) or ""))
check("E91 the deadline outranks the time stop",
      "DEADLINE" in (Managed(symbol="X", qty=1, entry_credit=2.0,
                             current=1.9, dte=0).reason(_at) or ""))
check("E91 the deadline names the judging date",
      str(CONTEST_CLOSE) in (_held.reason(_at) or ""), str(_held.reason(_at)))

# and manage() must actually SUBMIT at the deadline, not merely report (E78)
import deltax.manage as _mg
_saved = _mg.past_contest_deadline
try:
    _mg.past_contest_deadline = lambda now=None: True
    _sent = []
    _out = _mg.manage([Managed(symbol="A", qty=1, entry_credit=2.0, current=1.9, dte=14),
                       Managed(symbol="B", qty=2, entry_credit=1.0, current=0.9, dte=9)],
                      dry_run=False,
                      closer=lambda s, q: _sent.append((s, q)) or {"result": "SUBMITTED"})
    check("E91 every position is closed at the deadline", len(_out["closed"]) == 2, str(_out))
    check("E91 and the closer was actually invoked for each", len(_sent) == 2, str(_sent))
    check("E91 none are left held", _out["held"] == [], str(_out["held"]))
    check("E91 none failed", _out["failed"] == [], str(_out["failed"]))
finally:
    _mg.past_contest_deadline = _saved
check("E91 the deadline function is restored",
      _mg.past_contest_deadline is _saved)

print("\n── E102: trailing take-profit ──")
# The operator's stock trailing stop, adapted to a credit spread. Measured 2
# Sep: the account peaked at $100,144 and closed at $99,559 - $585 of the day's
# loss was profit handed back, because there was a fixed 50% target and nothing
# at all between 0% and 50%.
from deltax.manage import (TRAIL_ARM_AT, TRAIL_GIVE_BACK, load_peaks,
                           update_peaks)
import tempfile as _tf, os as _os, json as _js

def _m(entry, now, peak=None, dte=9):
    return Managed(symbol="SPY260918P00760000", qty=1, entry_credit=entry,
                   current=now, dte=dte, peak_captured=peak)

# 2.00 credit now worth 1.40 => 30% captured, having peaked at 50%
_r = _m(2.00, 1.40, peak=0.50).reason(BEFORE_DEADLINE)
check("E102 a 20-point give-back from a 50% peak exits",
      "trailing exit" in (_r or ""), str(_r))
check("E102 the reason states peak, current and give-back",
      all(x in (_r or "") for x in ("peaked at", "gave back")), str(_r))
# same position, peak only 30% => give-back is 0 points, hold
check("E102 no give-back means hold",
      _m(2.00, 1.40, peak=0.30).reason(BEFORE_DEADLINE) is None)
# E109: a 10-point give-back now EXITS (arm 15, give-back 10); a 5-point one
# is still inside the friction band and holds
check("E109 a 10-point give-back from a 30% peak exits",
      "trailing exit" in (_m(2.00, 1.60, peak=0.30).reason(BEFORE_DEADLINE) or ""),
      str(_m(2.00, 1.60, peak=0.30).reason(BEFORE_DEADLINE)))
check("E109 a 5-point give-back still holds (inside friction)",
      _m(2.00, 1.50, peak=0.30).reason(BEFORE_DEADLINE) is None,
      str(_m(2.00, 1.50, peak=0.30).reason(BEFORE_DEADLINE)))
# never armed below the arm threshold, however large the retrace
check("E109 an unarmed position (peak 12%) never trails",
      _m(2.00, 2.00, peak=0.12).reason(BEFORE_DEADLINE) is None)
check("E109 arm threshold is 15%", abs(TRAIL_ARM_AT - 0.15) < 1e-9)
check("E109 give-back threshold is 10 points", abs(TRAIL_GIVE_BACK - 0.10) < 1e-9)
# the give-back must never sit inside the measured 9-15% round-trip band
check("E109 give-back is not below the friction floor (9 points)",
      TRAIL_GIVE_BACK >= 0.09, "a trail inside the bid/ask band fires on noise")
# ranking: the fixed target and the deadline both outrank the trail
check("E102 the 50% target still wins when both would fire",
      "target hit" in (_m(2.00, 0.90, peak=0.95).reason(BEFORE_DEADLINE) or ""))
check("E102 the contest deadline outranks the trail",
      "DEADLINE" in (_m(2.00, 1.40, peak=0.50).reason(_at) or ""))
# a missing peak must not crash or fire
check("E102 no recorded peak simply holds",
      _m(2.00, 1.40, peak=None).reason(BEFORE_DEADLINE) is None)
check("E102 an unpriceable position still reports no reason",
      _m(2.00, None, peak=0.50).reason(BEFORE_DEADLINE) is None)

print("\n── E102: peaks must survive across cycles ──")
# Each cycle is a fresh process. A peak held only in memory resets every five
# minutes and the trail could never fire.
_d = _tf.mkdtemp(); _p = _os.path.join(_d, "peaks.json")
check("E102 a missing peaks file reads as empty", load_peaks(_p) == {})
_a = Managed(symbol="AAA", qty=1, entry_credit=2.0, current=1.0, dte=9)   # 50%
update_peaks([_a], _p)
check("E102 a peak is persisted", abs(load_peaks(_p).get("AAA", 0) - 0.50) < 1e-9)
_b = Managed(symbol="AAA", qty=1, entry_credit=2.0, current=1.6, dte=9)   # 20%
update_peaks([_b], _p)
check("E102 peaks only ever rise, never fall",
      abs(load_peaks(_p).get("AAA", 0) - 0.50) < 1e-9, str(load_peaks(_p)))
_c = Managed(symbol="BBB", qty=1, entry_credit=2.0, current=1.5, dte=9)
update_peaks([_c], _p)
check("E102 a closed structure is dropped so a reopen starts clean",
      "AAA" not in load_peaks(_p) and "BBB" in load_peaks(_p), str(load_peaks(_p)))
open(_p, "w").write("{not json")
check("E102 a corrupt peaks file reads as empty rather than raising",
      load_peaks(_p) == {})
check("E102 writing to an unwritable path does not raise",
      update_peaks([_a], "/nonexistent-dir-e102/peaks.json") is not None)

print("\n── E119: exits are priced from the SUBMITTED credit, not the leg allocation ──")
# Alpaca paper allocates per-leg fill prices that do not sum to the multi-leg
# net. 3 Sep: UNH 390/380 put submitted and filled at a 1.83 credit limit, legs
# report 3.70 and 1.93 (1.77); QCOM 175/180 call submitted at 0.86, legs report
# 1.61 and 1.00 (0.61). The E118 healer rested the QCOM exit at 0.31 instead of
# 0.43, and the trail's `captured` was measured against a credit 29% too small.
# A credit-limit order cannot fill below its limit: the submitted credit is the
# floor of the true fill and the only number actually agreed with the broker.
from deltax.manage import (submitted_credits, entry_credit_for,
                           CREDIT_SOURCE_SUBMITTED, CREDIT_SOURCE_BASIS)
UNH_S, UNH_L = "UNH260918P00390000", "UNH260918P00380000"
QCOM_S, QCOM_L = "QCOM260911C00175000", "QCOM260911C00180000"

def _submit(seq, short, long, limit, result="SUBMITTED", action="submit",
            order_id="oid", short_intent="sell_to_open", long_intent="buy_to_open"):
    """A ledger entry shaped exactly like Ledger.record_raw(execute.submit(...))."""
    return {"kind": "event", "seq": seq, "ts_utc": "2026-09-03T16:50:34+00:00",
            "event": {"action": action, "qty": 6, "limit_price": limit,
                      "legs": [{"symbol": short, "side": "sell", "ratio_qty": "1",
                                "position_intent": short_intent},
                               {"symbol": long, "side": "buy", "ratio_qty": "1",
                                "position_intent": long_intent}],
                      "dry_run": False, "result": result, "order_id": order_id}}

_ledger = [
    {"kind": "event", "seq": 1, "event": {"action": "reconcile"}},        # noise
    {"seq": 2, "symbol": "SPY", "decision": "REFUSE", "gates": []},         # a decision, not an event
    _submit(4506, UNH_S, UNH_L, 1.83, order_id="4a1ee7c2"),
    _submit(4573, QCOM_S, QCOM_L, 0.86, order_id="ad3648fe"),
    # things that must NOT count as evidence of a fill:
    _submit(4580, "SPY260908P00770000", "SPY260908P00760000", 1.36,
            result="DRY_RUN — not submitted"),                              # dry run
    _submit(4581, "C260918P00135000", "C260918P00130000", 1.04,
            result="FAILED — ExecutionRefused"),                            # refused
    _submit(4582, "TQQQ260911P00070000", "TQQQ260911P00067000", 0.40,
            action="submit_close", short_intent="buy_to_close",
            long_intent="sell_to_close"),                                   # a close
    # a DEBIT call vertical (the E58 catalyst book): sells the FARTHER strike
    _submit(4583, "USO260918C00090000", "USO260918C00085000", 1.20),
]
_subm = submitted_credits(_ledger)

# (a) the submitted credit wins when present
_c, _src, _det = entry_credit_for(UNH_S, 3.70 - 1.93, _subm)
check("E119 (a) UNH: the submitted 1.83 wins over the legs' 1.77",
      abs(_c - 1.83) < 1e-9, f"{_c} {_src}")
check("E119 (a) and the source says so", _src == CREDIT_SOURCE_SUBMITTED, _src)
check("E119 (a) the detail carries BOTH numbers so the gap is visible",
      abs(_det["cost_basis_credit"] - 1.77) < 1e-9 and abs(_det["submitted_limit"] - 1.83) < 1e-9,
      str(_det))
check("E119 (a) and the order it came from", _det["order_id"] == "4a1ee7c2", str(_det))
_c, _src, _ = entry_credit_for(QCOM_S, 1.61 - 1.00, _subm)
check("E119 (a) QCOM: 0.86, not 0.61", abs(_c - 0.86) < 1e-9, str(_c))
check("E119 (a) so the healed exit rests at 0.43, not 0.31",
      exit_limit(_c) == 0.43 and exit_limit(1.61 - 1.00) == 0.31,
      f"{exit_limit(_c)} vs {exit_limit(0.61)}")
# the trail: QCOM marked 0.70 is 18.6% captured against 0.86 (armed) and
# -14.8% against 0.61 (never arms). This is the "arms late" consequence.
_right = Managed(QCOM_S, 12, 0.86, 0.70, 8, entry_credit_source=_src)
_wrong = Managed(QCOM_S, 12, 0.61, 0.70, 8)
check("E119 (a) the trail ARMS on the real credit",
      _right.captured is not None and _right.captured >= TRAIL_ARM_AT,
      f"captured={_right.captured}")
check("E119 (a) and could never arm on the allocated one",
      _wrong.captured is not None and _wrong.captured < 0, f"captured={_wrong.captured}")

# (b) the fallback is recorded as such, never silently
_c, _src, _det = entry_credit_for("MSFT260918P00500000", 2.10, _subm)
check("E119 (b) no submit record: cost basis is used", abs(_c - 2.10) < 1e-9, str(_c))
check("E119 (b) and the source names it", _src == CREDIT_SOURCE_BASIS, _src)
check("E119 (b) and the detail says why in words",
      "no SUBMITTED open" in _det.get("note", ""), str(_det))
check("E119 (b) the two sources are distinguishable strings",
      CREDIT_SOURCE_SUBMITTED != CREDIT_SOURCE_BASIS
      and isinstance(CREDIT_SOURCE_BASIS, str) and CREDIT_SOURCE_BASIS)
_mb = Managed("MSFT260918P00500000", 1, _c, 1.0, 8, entry_credit_source=_src)
check("E119 (b) the Managed record carries the source",
      _mb.entry_credit_source == CREDIT_SOURCE_BASIS, _mb.entry_credit_source)
check("E119 (b) a Managed built without one says 'unspecified', not nothing",
      Managed("X", 1, 2.0, 1.0, 7).entry_credit_source == "unspecified",
      Managed("X", 1, 2.0, 1.0, 7).entry_credit_source)
_led = L(); manage([_mb], ledger=_led, dry_run=True)          # 52% captured -> closes
check("E119 (b) the close record names the credit AND its source",
      _led.rows and _led.rows[0]["entry_credit_source"] == CREDIT_SOURCE_BASIS
      and abs(_led.rows[0]["entry_credit"] - 2.10) < 1e-9, str(_led.rows[:1]))
_led = L(); place_exit(legs, 7, 2.30, ledger=_led, dry_run=True,
                       entry_credit_source=CREDIT_SOURCE_SUBMITTED)
check("E119 (b) place_exit records the source it was given",
      _led.rows[0]["entry_credit_source"] == CREDIT_SOURCE_SUBMITTED, str(_led.rows[0]))
_led = L(); place_exit(legs, 7, 2.30, ledger=_led, dry_run=True)
check("E119 (b) place_exit without a source records 'unspecified'",
      _led.rows[0]["entry_credit_source"] == "unspecified", str(_led.rows[0]))

# what is NOT evidence of a fill
check("E119 a dry run is not a fill", "SPY260908P00770000" not in _subm, str(_subm.keys()))
check("E119 a refused order is not a fill", "C260918P00135000" not in _subm)
check("E119 a CLOSING order is not an entry", "TQQQ260911P00070000" not in _subm)
check("E119 a DEBIT vertical's limit is never read as a credit",
      "USO260918C00090000" not in _subm, str(_subm.keys()))
check("E119 exactly the two real entries were kept",
      set(_subm) == {UNH_S, QCOM_S}, str(sorted(_subm)))
# latest wins: a structure re-opened after a close is priced from ITS entry
_two = submitted_credits([_submit(20, UNH_S, UNH_L, 1.83), _submit(10, UNH_S, UNH_L, 1.50)])
check("E119 the latest SUBMITTED record wins, whatever the file order",
      abs(_two[UNH_S]["credit"] - 1.83) < 1e-9, str(_two))
# a four-leg condor's limit is the whole structure's credit, not one side's
_condor = _submit(30, UNH_S, UNH_L, 3.00)
_condor["event"]["legs"] += [{"symbol": "UNH260918C00420000", "position_intent": "sell_to_open"},
                             {"symbol": "UNH260918C00430000", "position_intent": "buy_to_open"}]
check("E119 a four-leg record is not attributed to one side", submitted_credits([_condor]) == {})
# malformed records are not evidence and do not raise
_bad = [None, {"kind": "event"}, {"kind": "event", "event": None},
        {"kind": "event", "seq": 1, "event": {"action": "submit", "result": "SUBMITTED",
                                              "limit_price": "n/a", "legs": []}},
        {"kind": "event", "seq": 2, "event": {"action": "submit", "result": "SUBMITTED",
                                              "limit_price": 1.0, "legs": [{"symbol": "??"}, 3]}},
        {"kind": "event", "seq": 3, "event": {"action": "submit", "result": "SUBMITTED",
                                              "limit_price": -1.0,
                                              "legs": _submit(0, UNH_S, UNH_L, 1)["event"]["legs"]}}]
check("E119 malformed or non-positive records are skipped, never raised on",
      submitted_credits(_bad) == {} and submitted_credits(None) == {})
check("E119 an empty ledger falls back cleanly",
      entry_credit_for(UNH_S, 1.77, {})[1] == CREDIT_SOURCE_BASIS
      and entry_credit_for(UNH_S, 1.77, None)[1] == CREDIT_SOURCE_BASIS)
# no threshold moved
check("E119 changes no threshold",
      TAKE_PROFIT_FRACTION == 0.50 and abs(TRAIL_ARM_AT - 0.15) < 1e-9
      and abs(TRAIL_GIVE_BACK - 0.10) < 1e-9 and TIME_STOP_DTE == 1)

print(f"\n{'='*52}\n  {passed} passed, {failed} failed\n{'='*52}")
sys.exit(1 if failed else 0)
