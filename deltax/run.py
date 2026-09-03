"""End-to-end strategy run: screen -> gate -> log -> execute (dry by default).

This is the whole agent in one entry point. Direction-neutral income core per
E11/E12; both sides of a condor are nominated and gated independently.

  python3 -m deltax.run             # dry run, nothing submitted
  python3 -m deltax.run --live      # requires DELTAX_ORDERS_ALLOWED=1 as well
"""

from datetime import date, datetime, timezone
import sys

from deltax import execute
from deltax.calendar import entry_allowed
from deltax.permission import Evidence, recommend_state, gate_permission
from deltax.gates import DEMONSTRATION_MODE, demo_cap, demo_permits
from deltax.manage import place_exit
from deltax.reconcile import reconcile, safe_to_open
from deltax import report
from deltax import news_gate
from deltax import gamma as gamma_mod
from deltax.manage import manage, Managed, CREDIT_SOURCE_SUBMITTED
from deltax import blocklist
from deltax.feeds import AlpacaFeed
from deltax.ledger import Ledger
from deltax.screener import (
    directional_bias,INCOME_UNIVERSE, DEFAULT_WIDTH, TARGET_DELTA_BY_WEAK,
                             rank_by_vol_premium, vol_premium,
                             assess_regime, select_vertical, choose_expiry,
                             realized_vol_20,
                             BENCHMARKS)
from deltax.gates import evaluate, MIN_DTE, MAX_DTE
from deltax import gates as _G

# E50: four concurrent positions, chosen from eight ranked candidates. E44
# measured that a capped-payoff short-premium book gets WORSE with more names -
# each one adds breach risk without adding upside - so the cap holds the tail
# while the ranking improves what fills it.
MAX_CONCURRENT = 8          # E111: 4 -> 8 entries per cycle


def run(feed, ledger, *, equity: float, today: date, dry_run: bool = True,
        force_window: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    # --force is an ANALYSIS switch: it relaxes the calendar window and the
    # permission state so the pipeline can be exercised outside session
    # hours. It is inert whenever real orders are possible.
    analysis = force_window and dry_run
    # E75: a transient network failure must not kill the cycle. On 2 Sep a TLS
    # handshake timeout on `alpaca clock` raised FeedError out of run() and the
    # whole scheduled run died with a traceback - no reconciliation, no exit
    # sweep, no ledger entry, and the next five minutes were blind. Every
    # feed call in the pre-trade path is now caught, logged, and FAILS CLOSED:
    # if the market state is unreadable we do not trade, but we exit cleanly
    # and the following cycle proceeds normally.
    try:
        clock = feed.clock()
    except Exception as e:
        ledger.record_raw({"action": "clock_unreadable", "failing": "closed",
                           "error": f"{type(e).__name__}: {str(e)[:160]}"})
        return {"traded": [], "refused": [], "committed": 0.0,
                "skipped": f"market clock unreadable ({type(e).__name__}) "
                           f"- failing closed, no orders this cycle"}
    allowed, why = entry_allowed(now, bool(clock.get("is_open")))
    if not allowed and not analysis:
        ledger.record_raw({"action": "skip", "reason": why})
        return {"traded": [], "refused": [], "skipped": why}

    try:
        snaps = feed.snapshots(BENCHMARKS)
    except Exception as e:
        ledger.record_raw({"action": "benchmarks_unreadable", "failing": "closed",
                           "error": f"{type(e).__name__}: {str(e)[:160]}"})
        return {"traded": [], "refused": [], "committed": 0.0,
                "skipped": f"benchmark snapshots unreadable ({type(e).__name__}) "
                           f"- failing closed"}
    regime = assess_regime(snaps)
    target = TARGET_DELTA_BY_WEAK[min(regime.weak_count, 3)]

    # Global permission sits ABOVE strategy - no candidate can override it.
    perm = recommend_state(Evidence(
        benchmarks_weak=regime.weak_count,
        data_stale=not regime.complete,
        market_open=bool(clock.get("is_open")),
        drawdown_pct=0.0,
    ))
    # --force is an ANALYSIS switch. It may relax permission only while
    # dry-running; with real orders enabled the state is absolute.
    perm_override = analysis
    ledger.record_raw({"action": "permission", "state": perm.state,
                       "reasons": perm.reasons, "overridden": perm_override})
    if perm.policy["size_factor"] <= 0 and not perm_override:
        return {"regime": regime, "traded": [], "refused": [], "committed": 0.0,
                "permission": perm.state, "advisory_only": False,
                "skipped": f"{perm.state}: {perm.reasons[0]}"}

    gte = str(today.fromordinal(today.toordinal() + MIN_DTE))
    # E69: the search window is MAX_DTE, no longer clamped to the contest close.
    # E41 clamped it because gate_contest_window would refuse anything later
    # anyway, so searching further was wasted work. E68 changed that gate -
    # expiries past judging are marked to market and their partial decay counts
    # - so the clamp became the thing that hid every tradeable chain. Measured
    # 2 Sep: the 4 Sep book fails on credit against a benchmark built from
    # 11-18 DTE quotes, while 11 and 18 Sep pass every gate with 0-4% spreads
    # and thousands of contracts of open interest. Searching only to the close
    # returned nothing at all, which read on the board as "no action".
    lte = str(today.fromordinal(today.toordinal() + MAX_DTE))

    # What do we ALREADY hold? Without this, every cycle believes the book is
    # empty and re-opens the same positions - 96 times a day on the live
    # schedule. The risk cap only means something across cycles if committed
    # risk is seeded from the broker, not from this run's fills.
    try:
        # Positions AND working orders — an unfilled order is still risk (E36).
        book = reconcile(feed.positions(), feed.open_orders())
    except Exception as e:
        ledger.record_raw({"action": "reconcile_failed", "error": str(e)[:200]})
        return {"regime": regime, "traded": [], "refused": [], "committed": 0.0,
                "permission": perm.state, "advisory_only": False,
                "skipped": f"cannot read open positions - refusing to trade blind: {e}"}
    ok, why = safe_to_open(book)
    # E86: reconcile() has always collected `equities`, and NOTHING read it -
    # computed on every cycle and thrown away, the same dead-value shape as
    # E79. It matters because an equity position can appear with no order
    # placed at all: an assigned short option becomes stock overnight, which
    # bypasses the E82 rule-3 guard entirely (that guard sits on the order
    # path, and assignment places no order). On 2 Sep $19,830 of equity sat in
    # the submission account undetected; the difference now is that it would be
    # ~$76,000 of SPY from a single assigned put, unhedged, and a rule-3 breach
    # at judging. It is NOT blocked here - E72 was exactly the deadlock where an
    # equity holding refused every options trade - but it can never be silent.
    _eq = book.get("equities") or []
    if _eq:
        ledger.record_raw({"action": "UNEXPECTED_EQUITY", "symbols": _eq,
                           "likely_cause": "option assignment (places no order, "
                                           "so the E82 rule-3 guard cannot see it)",
                           "hackathon_rule": "rule 3 - all strategies must "
                                             "incorporate options trading",
                           "action_required": "close or overlay before judging"})
    ledger.record_raw({"action": "reconcile", "open_positions": book["count"],
                       "working_orders": book.get("pending_orders", 0),
                       "committed": round(book["committed"], 2),
                       "held": sorted(f"{u}/{s_}" for u, s_ in book["held"]),
                       "equities": _eq,
                       "safe_to_open": ok, "note": why})
    if not ok:
        return {"regime": regime, "traded": [], "refused": [],
                "committed": book["committed"], "permission": perm.state,
                "advisory_only": False, "skipped": why, "equities": _eq}

    # Exits run BEFORE entries every cycle. Closing a position frees risk budget
    # the gates would otherwise refuse the next candidate for, so an entry-first
    # order silently caps the book at whatever was opened earliest (E39).
    # E84: every key manage() can return is initialised here. "failed" was
    # missing, so any consumer reading swept["failed"] raised KeyError on a
    # cycle where the sweep never ran - a crash caused purely by the sweep
    # having nothing to do.
    swept = {"closed": [], "held": [], "unpriceable": [], "failed": [],
             "dropped": []}
    sweep_dropped = []                      # E84: defined before the try, so the
    try:                                    # merge below cannot NameError
        # E77: pair the legs. This previously modelled each SHORT leg alone -
        # entry_credit was the short leg's own price and `current` its own mark,
        # with the long leg ignored entirely. Measured live on 2 Sep the error
        # was 54 percentage points: the sweep believed SMH was -5.0% when the
        # spread was -58.7%. Take-profit fires at 50% CAPTURED, so it was
        # deciding on a number that is not the position's P&L.
        #
        # A vertical's real credit is short_entry - long_entry, and its real
        # value now is short_mark - long_mark. dte was hard-coded None, which
        # also meant TIME_STOP_DTE could never fire for the income book.
        from deltax.reconcile import parse_occ as _parse
        # E102: peaks are read BEFORE this cycle's marks are folded in, so a
        # structure is compared against its best PREVIOUS reading rather than
        # against itself - otherwise peak == current on every cycle and the
        # give-back is always zero.
        from deltax.manage import load_peaks as _load_peaks, update_peaks as _update_peaks
        _peaks = _load_peaks()
        # E119: the credit every exit is priced from. The broker's per-leg
        # avg_entry_price does not sum to the multi-leg net that filled - on
        # 3 Sep the UNH 390/380 put spread was submitted at 1.83 and the legs
        # say 1.77; the QCOM 175/180 call spread was submitted at 0.86 and the
        # legs say 0.61. `credit = se - le` below was built from those legs,
        # so the E118 healer rested the QCOM exit at 0.31 instead of 0.43 and
        # the E102/E109 trail measured `captured` against a credit 29% too
        # small. A credit-limit mleg order cannot fill BELOW its limit, so the
        # SUBMITTED limit in the ledger is the floor of the true fill and wins
        # whenever it exists. Cost basis is the fallback, recorded as such.
        from deltax.manage import (submitted_credits as _submitted_credits,
                                   entry_credit_for as _entry_credit_for)
        try:
            _subm = (_submitted_credits(ledger.entries())
                     if hasattr(ledger, "entries") else {})
        except Exception as _le:
            _subm = {}
            ledger.record_raw({"action": "submitted_credits_unreadable",
                               "error": f"{type(_le).__name__}: {str(_le)[:120]}",
                               "consequence": "every structure is priced from "
                                              "cost-basis credit this cycle"})
        legs = {}
        for p_ in feed.positions():
            sym = p_.get("symbol", "")
            occ = _parse(sym)
            if occ is None:
                continue                          # equity: managed elsewhere
            try:
                q = int(float(p_.get("qty") or 0))
                entry = abs(float(p_.get("avg_entry_price") or 0))
                mark = abs(float(p_.get("current_price") or 0))
            except (TypeError, ValueError) as _pe:
                # E84: this `continue` dropped the position from the exit sweep
                # ENTIRELY - it then appeared in none of closed/held/
                # unpriceable/failed, so a holding with unreadable numbers
                # simply vanished from the sweep and from the board with
                # nobody told. A position the sweep cannot see is a position
                # that never closes. Record it and surface it.
                sweep_dropped.append(sym)
                ledger.record_raw({"action": "sweep_drop", "symbol": sym,
                                   "error": f"{type(_pe).__name__}: {str(_pe)[:100]}",
                                   "consequence": "position excluded from the exit "
                                                  "sweep - no time stop, no deadline close"})
                continue
            if q == 0:
                continue
            key = (occ["underlying"], occ["right"], occ["expiry"])
            side = "short" if q < 0 else "long"
            legs.setdefault(key, {})[side] = (abs(q), entry, mark, sym)

        live = []
        _credit_rows = []                   # E119: provenance, one row per structure
        for (und, right, exp), v in legs.items():
            if "short" not in v:
                continue                          # a long-only leg carries no credit
            sq, se, sm, ssym = v["short"]
            lq, le, lm, _ = v.get("long", (sq, 0.0, 0.0, None))
            basis_credit = se - le
            # E119: submitted limit when the ledger has one, else cost basis
            credit, _src, _src_detail = _entry_credit_for(ssym, basis_credit, _subm)
            # E83: this was `now = sm - lm`, which SHADOWED the cycle's UTC
            # timestamp (set once at the top of run) with the spread's current
            # mark - a float. Every later use of `now` then operated on a
            # number instead of a datetime. Specifically, the bar-age
            # computation feeding gate_listed does
            #     bar_age = (now - bar_t).total_seconds() / 86400.0
            # inside `except (ValueError, TypeError)`, so float - datetime
            # raised TypeError, was swallowed, and bar_age became None. With
            # tradable=True and age None, gate_listed takes its fail-closed
            # branch: "listing status unknown". The delisting guard (E25) was
            # therefore DEAD from the first cycle that held a position - and it
            # reported healthy, liquid ETFs as "likely delisted" in the ledger.
            # Named `mark_now` so it cannot collide again.
            mark_now = sm - lm
            if credit <= 0:
                continue                          # debit structure: not this sweep
            try:
                d = (datetime.strptime(exp, "%y%m%d").date() - today).days
            except (ValueError, TypeError) as _de:
                # E84: dte=None silently DISABLES the gamma-zone time stop for
                # this position - Managed.reason() only applies it when dte is
                # not None. Degrading a stop to "no stop" without a word is the
                # same failure shape as E83.
                d = None
                sweep_dropped.append(f"{ssym}(dte)")
                ledger.record_raw({"action": "sweep_dte_unreadable",
                                   "symbol": ssym, "expiry_raw": str(exp)[:20],
                                   "error": f"{type(_de).__name__}: {str(_de)[:100]}",
                                   "consequence": "TIME STOP DISABLED for this position"})
            live.append(Managed(symbol=ssym, qty=min(sq, lq),
                                entry_credit=credit,
                                current=mark_now if mark_now > 0 else None,
                                dte=d,
                                # E102: the high-water mark from previous cycles
                                peak_captured=_peaks.get(ssym),
                                # E119: never silent about which credit this is
                                entry_credit_source=_src))
            _credit_rows.append({"symbol": ssym, "qty": min(sq, lq),
                                 "credit": round(credit, 2), "source": _src,
                                 **_src_detail})
        if _credit_rows:
            # E119: written every cycle so the number the trail and the healer
            # act on is auditable, and a cost-basis fallback is named, not
            # buried in a per-leg price nobody reads.
            ledger.record_raw({"action": "sweep_credit", "structures": _credit_rows,
                               "fallbacks": [r["symbol"] for r in _credit_rows
                                             if r["source"] != CREDIT_SOURCE_SUBMITTED]})
        if live:
            # E78: give the sweep a real closer. Without one it reported closes
            # it never made. Marketable-limit at the current mark plus a small
            # allowance, so a triggered stop actually leaves the book.
            # E116: a structure ALREADY has a resting 50% exit (E5), and that
            # order holds the position's entire closing quantity. Submitting a
            # second closing order was rejected by the broker every time -
            # "insufficient qty available for order (requested: 1, available:
            # 0)" - so the trailing exit (E102/E109) fired six times today and
            # closed nothing, while recording CLOSE FAILED. The fix is to
            # RE-PRICE the resting exit to a marketable limit via order replace:
            # one atomic operation, the exit is never absent, no qty conflict.
            # Only when no resting exit exists does it fall back to submitting.
            try:
                _working = list(feed.open_orders())
            except Exception:
                _working = []
            def _resting_exit(short_sym):
                for _o in _working:
                    _ls = _o.get("legs") or [_o]
                    if any(str(_l.get("position_intent", "")).endswith("_to_close")
                           and _l.get("symbol") == short_sym for _l in _ls):
                        return _o
                return None
            # E118: a structure with NO resting exit is healed here, every
            # cycle. The entry path places the 50% exit one second after the
            # opening order (E5); when the open has not filled yet the broker
            # infers buy_to_open on the close legs and refuses it - "position
            # intent mismatch" - which is what happened to the QCOM 175/180
            # call spread at 12:55 ET on 3 Sep: 12 contracts filled, exit
            # recorded FAILED, and nothing ever tried again. Every entry runs
            # that race; the 12:50 entries won it by 0.4 s. Whether the exit
            # was refused, expired, or cancelled by hand, the invariant is
            # the same: a live credit structure always has a *_to_close order
            # working. Records what it placed, or that it could not.
            for _m in live:
                if _resting_exit(_m.symbol) is not None:
                    continue
                for (_u, _r, _e), _v in legs.items():
                    if "short" not in _v or _v["short"][3] != _m.symbol:
                        continue
                    _lsym = _v.get("long", (0, 0, 0.0, None))[3]
                    _rec = {"action": "exit_healed", "symbol": _m.symbol,
                            "qty": _m.qty, "entry_credit": round(_m.entry_credit, 2),
                            "entry_credit_source": _m.entry_credit_source,   # E119
                            "reason": "E118: no resting *_to_close order found"}
                    if not _lsym:
                        _rec["result"] = "REFUSED — no long leg, will not rest a naked close"
                    else:
                        _ex = place_exit([execute.Leg(_m.symbol, "sell", 1),
                                          execute.Leg(_lsym, "buy", 1)],
                                         _m.qty, _m.entry_credit,
                                         ledger=ledger, dry_run=dry_run,
                                         entry_credit_source=_m.entry_credit_source)
                        _rec["limit_price"] = _ex.get("limit_price")
                        _rec["result"] = _ex.get("result")
                    ledger.record_raw(_rec)
                    break
            def _closer(sym, qty, _legs=legs):
                for (u, r, e), v in _legs.items():
                    if "short" not in v or v["short"][3] != sym:
                        continue
                    ssym = v["short"][3]
                    lsym = v.get("long", (0, 0, 0, None))[3]
                    if not lsym:
                        raise RuntimeError("no long leg - refusing a naked close")
                    limit = round(max(v["short"][2] - v.get("long", (0, 0, 0.0, None))[2]
                                      + 0.05, 0.05), 2)
                    _rest = _resting_exit(ssym)
                    if _rest and _rest.get("id"):
                        rec = {"action": "replace_exit", "symbol": ssym, "qty": qty,
                               "order_id": _rest["id"], "old_limit": _rest.get("limit_price"),
                               "new_limit": limit, "dry_run": dry_run,
                               "context": {"strategy": "E116 trail re-prices the resting exit"}}
                        if dry_run:
                            rec["result"] = "DRY_RUN — not replaced"
                        else:
                            execute.preflight()
                            _resp = execute._run(["order", "replace", "--order-id", _rest["id"],
                                                  "--limit-price", f"{limit:.2f}"])
                            rec["result"] = "REPLACED"
                            rec["new_order_id"] = _resp.get("id")
                        ledger.record_raw(rec)
                        return rec
                    return execute.submit(
                        [execute.Leg(ssym, "sell", 1), execute.Leg(lsym, "buy", 1)],
                        qty, limit, dry_run=dry_run, close=True,
                        context={"strategy": "E78 exit sweep (no resting exit found)"})
                raise RuntimeError(f"no paired legs found for {sym}")
            swept = manage(live, ledger=ledger, dry_run=dry_run, closer=_closer)
            _update_peaks(live)         # E102: raise the high-water marks
    except Exception as e:
        ledger.record_raw({"action": "exit_sweep_failed", "error": str(e)[:160]})
    # E84: surface unreadable positions on the RESULT, not only in the log. A
    # holding the sweep could not parse is absent from closed/held/unpriceable/
    # failed, so without this it disappears from the board entirely.
    swept.setdefault("dropped", [])
    swept["dropped"] = list(swept["dropped"]) + sweep_dropped
    if sweep_dropped:
        ledger.record_raw({"action": "sweep_incomplete",
                           "dropped": sweep_dropped,
                           "note": "these holdings were NOT evaluated for exit"})

    committed, traded, refused = book["committed"], [], []
    held = book["held"]
    # Earnings blocklist, built once in pre-market. Read here rather than
    # queried, so a SEC timeout can never sit inside the trading loop (E32).
    bl = blocklist.load()
    ledger.record_raw({"action": "earnings_blocklist",
                       "present": bl is not None,
                       "age_hours": round(blocklist.age_hours(bl), 2) if bl else None,
                       "blocked": len((bl or {}).get("blocked") or {}),
                       "clear": len((bl or {}).get("clear") or [])})
    exits_placed = []
    news_checked = {}      # symbol -> verdict, one fetch per name per cycle
    catalyst_result = None
    rotation_result = None

    # ── E71: sector rotation, ADVISORY ──────────────────────────────────────
    # Three layers - regime (SPY vs GLD/TLT/BIL), 11 GICS sectors ranked by
    # relative strength vs SPY, subsector amplification. It RANKS and LOGS on
    # every cycle; it does not place orders. The source framework is explicit
    # that rotation "takes weeks to unfold" and that daily rebalancing produces
    # whipsaws, so wiring it to auto-execute on a 5-minute loop would trade
    # against the signal's own design. The ranking steers which underlyings the
    # options engine should prefer; the orders stay with the gated engine.
    try:
        from deltax import rotation as _rot
        _need = list(dict.fromkeys(
            _rot.CORE_SECTORS + [_rot.BENCHMARK] + _rot.SAFE_HAVENS
            + [s for v in _rot.SUBSECTORS.values() for s in v]))
        _closes = {}
        for _s in _need:
            try:
                _b = feed.daily_bars(_s, str(today.fromordinal(today.toordinal() - 90)),
                                     str(today), 80)
                _closes[_s] = [x["c"] for x in _b if x.get("c")]
            except Exception:
                _closes[_s] = []
        _sel = _rot.select(_closes)
        rotation_result = {
            "regime": _sel["regime"], "reason": _sel["reason"],
            "ratio": _sel["ratio"],
            "picks": [{"symbol": p.symbol, "roc": round(p.roc, 5),
                       "rs": round(p.rs, 5), "via": p.subsector}
                      for p in _sel["picks"]],
            "ranked": [{"symbol": r.symbol, "rs": round(r.rs, 5)}
                       for r in (_sel.get("ranked") or [])[:11]],
        }
        ledger.record_raw({"action": "rotation", **rotation_result,
                           "advisory": True})
    except Exception as _re:
        ledger.record_raw({"action": "rotation_failed",
                           "error": f"{type(_re).__name__}: {str(_re)[:120]}"})

    # ── E58: catalyst rule ────────────────────────────────────────────────
    # A defined-risk LONG vertical on a live supply shock. This is the only
    # structure here that buys premium. It authorises a setup; it does not
    # force an entry, and every stage below can refuse.
    try:
        from deltax import catalyst as _cat
        _u = _cat.UNDERLYING
        _already = any(u == _u for u, _s in held)
        if _already:
            # E62: escalation ADD-ON. Already holding is no longer an automatic
            # skip - if the catalyst is ESCALATING with physical confirmation
            # and the size band now exceeds the risk already committed, add the
            # difference. Every ambiguity fails closed to a plain skip.
            _added = False
            try:
                from deltax import probability as _prob
                from deltax.feeds import (previous_close as _pc,
                                          latest_price as _lp,
                                          intraday_vwap as _vw)

                # E63: the DETERMINISTIC Thursday flatten. The sweep passes
                # dte=None so the time stop never fires there, and manage()
                # only records closes - it does not submit them. This is the
                # enforcement of "never hold through NFP Friday": at <= 1 DTE,
                # close the spread at a conservative live limit and stop.
                _dte = (_cat.EXPIRY - today).days
                if _dte <= 1:
                    try:
                        _vq = _cat.price_vertical(feed)
                        # closing collects the spread's bid side: long bid
                        # minus short ask, floored so a broken quote cannot
                        # produce a nonsense limit.
                        _lb = 0.0; _sa = 0.0
                        _chz = feed.option_chain(_u, option_type="call",
                            expiry_gte=str(_cat.EXPIRY), expiry_lte=str(_cat.EXPIRY),
                            strike_gte=_cat.LONG_STRIKE - 1,
                            strike_lte=_cat.SHORT_STRIKE + 1)
                        _lqz = (_chz.get(_vq.long_symbol) or {}).get("latestQuote") or {}
                        _sqz = (_chz.get(_vq.short_symbol) or {}).get("latestQuote") or {}
                        _lb = _lqz.get("bp") or 0.0
                        _sa = _sqz.get("ap") or 0.0
                        _close_limit = max(0.05, round(_lb - _sa - 0.03, 2))
                        _held_qty = 0
                        for _p_ in feed.positions():
                            if _p_.get("symbol") == _vq.long_symbol:
                                _held_qty = int(abs(float(_p_.get("qty") or 0)))
                        if _held_qty >= 1:
                            _rec = execute.submit(
                                _cat.legs_for(_vq), _held_qty, _close_limit,
                                dry_run=dry_run, close=True,
                                context={"strategy": "E63 Thursday flatten",
                                         "dte": _dte})
                            ledger.record_raw(_rec)
                            catalyst_result = {"flattened": _held_qty,
                                               "limit": _close_limit,
                                               "result": _rec.get("result")}
                            _added = True      # suppresses the skip record
                    except Exception as _fe:
                        ledger.record_raw({"action": "catalyst_flatten_failed",
                                           "error": f"{type(_fe).__name__}: {str(_fe)[:120]}"})
                    # at <= 1 DTE never consider adding, whatever happened above
                    raise StopIteration

                # E63: only orders that OPEN positions block an add-on. The
                # resting *_to_close exit must not - it rests for the whole
                # life of the position and would make escalation dead code.
                def _opens(_o):
                    _lgs = _o.get("legs") or [_o]
                    return any(str(_l.get("symbol", "")).startswith(_u)
                               and not str(_l.get("position_intent", "")
                                           ).endswith("_to_close")
                               for _l in _lgs)
                _pending_uso = any(_opens(_o) for _o in feed.open_orders())
                _sn = feed.snapshots([_u]).get(_u) or {}
                _on, _why = _cat.catalyst_active(_pc(_sn), _lp(_sn))
                if not _pending_uso and _on:
                    _v = _cat.price_vertical(feed)
                    if _v.ok and _v.debit:
                        _spotpx, _vwap, _prev = _lp(_sn), _vw(_sn), _pc(_sn)
                        _ev = {"uso_confirms": bool(_spotpx and _vwap and _prev
                                and _spotpx > _vwap and _spotpx > _prev),
                               "momentum_lost": bool(_spotpx and _vwap
                                and _spotpx < _vwap),
                               "options_reasonably_priced": True}
                        _lvl = _cat.LONG_STRIKE + (_v.exit_limit or 0)
                        _base = _prob.p_touch_base(_spotpx, _lvl, None, 1.9)
                        # IV fetch as in the entry path; None fails closed
                        try:
                            _chn = feed.option_chain(_u, option_type="call",
                                expiry_gte=str(_cat.EXPIRY), expiry_lte=str(_cat.EXPIRY),
                                strike_gte=round((_spotpx or 0)-1, 2),
                                strike_lte=round((_spotpx or 0)+1, 2))
                            for _nd in _chn.values():
                                if _nd.get("impliedVolatility"):
                                    _base = _prob.p_touch_base(
                                        _spotpx, _lvl, _nd["impliedVolatility"], 1.9)
                                    break
                        except Exception:
                            _base = None
                        _post = _prob.update(_base, _ev)
                        _phys = _prob.physical_count(_ev)
                        _band, _lab = _prob.size_band(_post.p, _phys)
                        # committed risk from the actual fills, or fail closed
                        _committed_uso = None
                        try:
                            _lq = _sq = None
                            for _p_ in feed.positions():
                                if _p_.get("symbol") == _v.long_symbol:
                                    _lq = (abs(float(_p_.get("qty") or 0)),
                                           float(_p_.get("avg_entry_price") or 0))
                                if _p_.get("symbol") == _v.short_symbol:
                                    _sq = (abs(float(_p_.get("qty") or 0)),
                                           float(_p_.get("avg_entry_price") or 0))
                            if _lq and _sq:
                                _committed_uso = min(_lq[0], _sq[0]) * (
                                    _lq[1] - _sq[1]) * 100
                        except Exception:
                            _committed_uso = None
                        _status = _prob.catalyst_status(_ev)
                        if (_committed_uso is not None and _status == "ESCALATING"
                                and _phys >= 1
                                and _band - _committed_uso >= _v.debit * 100):
                            _extra = int((_band - _committed_uso) // (_v.debit * 100))
                            _oicap = int(min(_v.long_oi, _v.short_oi)
                                         * _cat.MAX_OI_FRACTION)
                            _extra = max(0, min(_extra, _oicap))
                            if _extra >= 1:
                                _rec = execute.submit(
                                    _cat.legs_for(_v), _extra, _v.debit,
                                    dry_run=dry_run,
                                    context={"strategy": "E62 escalation add-on",
                                             "band": _band, "posterior": _post.p,
                                             "committed_before": _committed_uso})
                                ledger.record_raw(_rec)
                                catalyst_result = {"added": _extra,
                                                   "band": _band,
                                                   "result": _rec.get("result")}
                                _added = True
                        ledger.record_raw({"action": "catalyst_addon_check",
                                           "status": _status, "physical": _phys,
                                           "posterior": _post.p, "band": _band,
                                           "committed": _committed_uso,
                                           "added": _added})
            except StopIteration:
                pass                     # E63 flatten path: clean stop, not a failure
            except Exception as _ae:
                ledger.record_raw({"action": "catalyst_addon_failed",
                                   "error": f"{type(_ae).__name__}: {str(_ae)[:120]}"})
            if not _added:
                catalyst_result = {"skipped": f"{_u} already held"}
                ledger.record_raw({"action": "catalyst_skipped",
                                   "reason": catalyst_result["skipped"]})
        elif not demo_permits(perm.state) and not perm_override:
            # perm_override is analysis-only (force AND dry_run), so this can
            # never let a real order through a HALT.
            catalyst_result = {"skipped": f"permission {perm.state}"}
            ledger.record_raw({"action": "catalyst_skipped",
                               "reason": catalyst_result["skipped"],
                               "state": perm.state})
        else:
            _sn = feed.snapshots([_u]).get(_u) or {}
            from deltax.feeds import previous_close as _pc, latest_price as _lp
            _on, _why = _cat.catalyst_active(_pc(_sn), _lp(_sn))
            ledger.record_raw({"action": "catalyst_check", "underlying": _u,
                               "active": _on, "reason": _why})
            if not _on:
                catalyst_result = {"skipped": f"catalyst inactive - {_why}"}
            else:
                _v = _cat.price_vertical(feed)
                # E61: P(exit level touched before the Thursday time stop),
                # market-implied base moved by live evidence. Sizing follows
                # the posterior; the $10k ceiling stays a ceiling.
                try:
                    from deltax import probability as _prob
                    from deltax.feeds import intraday_vwap as _vw
                    _iv = None
                    try:
                        _chn = feed.option_chain(_u, option_type="call",
                                                 expiry_gte=str(_cat.EXPIRY),
                                                 expiry_lte=str(_cat.EXPIRY),
                                                 strike_gte=round((_lp(_sn) or 0)-1,2),
                                                 strike_lte=round((_lp(_sn) or 0)+1,2))
                        for _n in _chn.values():
                            if _n.get("impliedVolatility"):
                                _iv = _n["impliedVolatility"]; break
                    except Exception:
                        pass
                    _spotpx = _lp(_sn); _vwap = _vw(_sn); _prev = _pc(_sn)
                    _lvl = (_v.long_strike if hasattr(_v, "long_strike") else
                            _cat.LONG_STRIKE) + (_v.exit_limit or 0)
                    _base = _prob.p_touch_base(_spotpx, _lvl, _iv, 1.9)
                    _ev = {
                        "uso_confirms": bool(_spotpx and _vwap and _prev
                                             and _spotpx > _vwap and _spotpx > _prev),
                        "momentum_lost": bool(_spotpx and _vwap and _spotpx < _vwap),
                        "options_reasonably_priced": bool(_v.ok),
                    }
                    _post = _prob.update(_base, _ev)
                    _phys = _prob.physical_count(_ev)
                    _band, _lab = _prob.size_band(_post.p, _phys)
                    ledger.record_raw({"action": "catalyst_probability",
                                       "exit_level": round(_lvl, 2), "iv": _iv,
                                       "base": _base, "posterior": _post.p,
                                       "evidence": _post.applied,
                                       "physical_confirmations": _phys,
                                       "status": _prob.catalyst_status(_ev),
                                       "size_band": _band, "band_label": _lab})
                    # E62: the band sets the spend in BOTH directions - it can
                    # escalate to HARD_MAX_RISK on physical confirmation and
                    # shrink to zero on a fading posterior. The 5%-of-OI cap
                    # and the debit ceiling still bind above it.
                    if _v.ok and _v.debit:
                        _want = int(_band // (_v.debit * 100))
                        _oicap = int(min(_v.long_oi, _v.short_oi)
                                     * _cat.MAX_OI_FRACTION)
                        _n = max(0, min(_want, _oicap))
                        if _n != _v.contracts:
                            ledger.record_raw({"action": "catalyst_resize",
                                               "from": _v.contracts, "to": _n,
                                               "band": _band, "oi_cap": _oicap})
                            _v.contracts = _n
                        _w = _cat.SHORT_STRIKE - _cat.LONG_STRIKE
                        _v.max_loss = round(_v.debit * 100 * _v.contracts, 2)
                        _v.max_profit = round((_w - _v.debit) * 100 * _v.contracts, 2)
                        if _v.contracts < 1:
                            _v.ok = False
                            _v.reason = f"posterior {_post.p:.0%} sizes to zero - NO TRADE"
                        else:
                            # keep the human-readable record consistent with
                            # the RESIZED position (E47: stale labels lie)
                            _v.reason = (f"{_v.contracts}x {_cat.LONG_STRIKE:.0f}/"
                                         f"{_cat.SHORT_STRIKE:.0f} @ ${_v.debit:.2f}"
                                         f" - risk ${_v.max_loss:,.0f},"
                                         f" max ${_v.max_profit:,.0f}"
                                         f" [band ${_band:,.0f}, p={_post.p:.0%}]")
                except Exception as _pe:
                    ledger.record_raw({"action": "catalyst_probability_failed",
                                       "error": f"{type(_pe).__name__}: {str(_pe)[:120]}"})
                ledger.record_raw({"action": "catalyst_price", "ok": _v.ok,
                                   "reason": _v.reason, "debit": _v.debit,
                                   "contracts": _v.contracts,
                                   "max_loss": _v.max_loss,
                                   "detail": _v.detail})
                if not _v.ok:
                    catalyst_result = {"refused": _v.reason}
                else:
                    _legs = _cat.legs_for(_v)
                    _rec = execute.submit(_legs, _v.contracts, _v.debit,
                                          dry_run=dry_run,
                                          context={"strategy": "E58 catalyst",
                                                   "underlying": _u,
                                                   "breakeven": _v.breakeven})
                    ledger.record_raw(_rec)
                    catalyst_result = {"placed": _v.reason,
                                       "result": _rec.get("result")}
                    # Exit rests immediately, exactly as the income book does:
                    # a target that needs someone awake is not a target (E5).
                    if str(_rec.get("result", "")).startswith(("SUBMITTED", "DRY_RUN")):
                        # E63: the exit is a CLOSING order - close=True flips
                        # the entry legs, stamps *_to_close, and rests GTC so
                        # it survives into Thursday. A raw-leg exit carried
                        # *_to_open intents and died at the day's end.
                        _ex = execute.submit(
                            _legs, _v.contracts, _v.exit_limit,
                            dry_run=dry_run, close=True,
                            context={"strategy": "E58 catalyst exit",
                                     "target_multiple": _cat.TARGET_MULTIPLE})
                        ledger.record_raw(_ex)
                        catalyst_result["exit"] = _ex.get("result")
    except Exception as e:
        ledger.record_raw({"action": "catalyst_failed",
                           "error": f"{type(e).__name__}: {str(e)[:200]}"})
        catalyst_result = {"error": f"{type(e).__name__}"}

    # E50: richest variance premium first. run.py used to walk the list in the
    # order it was typed and stop at the cap, so capital went to whichever name
    # came first rather than whichever paid most. Advisory only - every gate
    # still runs on every candidate, and an unmeasurable name keeps its place.
    from deltax.feeds import latest_price as _px
    from deltax.gates import CONTEST_CLOSE as _RANK_EXPIRY
    try:
        _spots = {s: _px(feed.snapshots([s]).get(s) or {}) for s in INCOME_UNIVERSE}
        _ordered = rank_by_vol_premium(feed, INCOME_UNIVERSE,
                                       str(_RANK_EXPIRY), _spots)
    except Exception:
        _ordered = list(INCOME_UNIVERSE)

    # E96: entry freeze. Checked HERE as well as in execute.submit() so a frozen
    # cycle does not spend 20s and ~100 broker calls screening candidates it can
    # never act on. Exits ran above and are untouched by this.
    _frz = _G.gate_new_entries()
    if not _frz.passed:
        ledger.record_raw({"action": "entries_frozen", "reason": _frz.detail,
                           "exits_active": True,
                           "committed": round(committed, 2),
                           "note": "resting 50% exits, the time stop and the "
                                   "Friday 10:00 flatten all remain in force"})
        return {"regime": regime, "traded": [], "refused": [],
                "committed": committed, "permission": perm.state,
                # E96: carry the REAL advisory flag through. Hard-coding False
                # here made an analysis run (--force + dry_run, which sets
                # perm_override) report itself as a live decision the moment the
                # freeze short-circuited the cycle. An early return must preserve
                # the semantics of the path it is short-circuiting, not invent
                # simpler ones.
                "advisory_only": perm_override, "book": book, "swept": swept,
                "equities": _eq, "exits": exits_placed,
                "frozen": _frz.detail,
                "catalyst": catalyst_result, "rotation": rotation_result}

    _rv_cache = {}                      # E101: realised vol, once per symbol
    for symbol in _ordered:
        if len(traded) >= MAX_CONCURRENT:
            break
        # E75: one unreadable symbol must not end the scan. Before this, a
        # single timeout here killed the loop and every candidate after it was
        # silently never evaluated - the cycle looked like a clean "no action".
        try:
            spot = (feed.snapshots([symbol]).get(symbol) or {})
        except Exception as e:
            ledger.record_raw({"action": "snapshot_failed", "symbol": symbol,
                               "error": f"{type(e).__name__}: {str(e)[:120]}"})
            refused.append((symbol, "both", "snapshot_unreadable"))
            continue
        from deltax.feeds import latest_price
        px = latest_price(spot)
        if not px:
            continue
        # E25: an instrument with clean history may not exist today. Age the
        # last bar and let gate_listed decide; unknown fails closed.
        bar_age = None
        db = spot.get("dailyBar") or {}
        if db.get("t"):
            try:
                bar_t = datetime.fromisoformat(str(db["t"]).replace("Z", "+00:00"))
                bar_age = (now - bar_t).total_seconds() / 86400.0
            except (ValueError, TypeError) as _be:
                # E83: this handler swallowed the shadowing bug for a whole
                # session. A malformed timestamp is bad DATA and belongs here;
                # a TypeError from `now` not being a datetime is a BUG, and
                # silently degrading to None turned gate_listed into a
                # permanent fail-closed that reported healthy ETFs as "likely
                # delisted". A gate going dark must be loud.
                bar_age = None
                ledger.record_raw({"action": "bar_age_unreadable",
                                   "symbol": symbol,
                                   "raw_t": str(db.get("t"))[:40],
                                   "now_type": type(now).__name__,
                                   "error": f"{type(_be).__name__}: {str(_be)[:120]}",
                                   "consequence": "gate_listed fails closed"})
        # E11: no directional edge proven, so nominate BOTH sides.
        # Dealer gamma regime. ADVISORY: it cannot be backtested with this data
        # (open interest has no as-of parameter), and E10 forbids an unvalidated
        # signal gating a trade. Measured, logged, displayed - never blocking.
        try:
            gch = feed.option_chain(symbol, expiry_gte=gte, expiry_lte=lte,
                                    strike_gte=round(px * 0.90, 2),
                                    strike_lte=round(px * 1.10, 2))
            gcs = feed.option_contracts(symbol, expiry_gte=gte, expiry_lte=lte,
                                        strike_gte=round(px * 0.90, 2),
                                        strike_lte=round(px * 1.10, 2), limit=1000)
            gmap = gamma_mod.build(symbol, px,
                                   gch, {c["symbol"]: c.get("open_interest") for c in gcs})
            _, gwhy = gamma_mod.gate_gamma_regime(gmap, require_positive=False)
            ledger.record_raw({"action": "gamma_regime", "symbol": symbol,
                               "regime": gmap.regime, "net_gex": round(gmap.total, 0),
                               "pin": gmap.pin, "flip": gmap.flip,
                               "advisory": True, "note": gwhy})
        except Exception as e:
            ledger.record_raw({"action": "gamma_failed", "symbol": symbol,
                               "error": str(e)[:120]})

        # One earnings decision per symbol, before either side is considered.
        ok_earn, why_earn = blocklist.check(symbol, date.fromisoformat(
            (lte if isinstance(lte, str) else str(lte))), bl)
        if not ok_earn:
            refused.append((symbol, "both", "earnings"))
            continue

        for side, lo, hi in (("put", 0.80, 1.02), ("call", 0.98, 1.20)):
            # E106: `already_held` used to fire on (underlying, side) - one
            # spread per side per underlying, ever. On the last session it
            # blocked SPY, DIA, QQQ and IWM (the four most tradeable names, all
            # holding a 09-08 structure) from any 09-11 or 09-18 spread, and
            # left the book at 28% of the cap. A second spread at a DIFFERENT
            # expiry is a different structure; the $30k hard cap and the joint
            # CVaR signal already bound the concentration. The check now keys
            # on the structure and runs AFTER the expiry is chosen, below.
            # Same-expiry duplicates are still refused - see the held_exp check
            # after choose_expiry() below. The old (underlying, side) test is
            # gone, not conditioned: a check that fires "only when frozen"
            # would be a trap for the next reader.
            allowed, why = gate_permission(perm, side)
            # E57: DEMONSTRATION_MODE may proceed through DEFENSIVE - a
            # directional caution - because size is capped at one contract.
            # It may never proceed through HALT or NO_NEW_POSITIONS, which mean
            # broken data or a breached loss limit, not a view on direction.
            if not allowed and DEMONSTRATION_MODE and demo_permits(perm.state):
                allowed = True
                why = f"demo override of {perm.state} at capped size (E57)"
                ledger.record_raw({"action": "demo_permission_override",
                                   "symbol": symbol, "side": side,
                                   "state": perm.state, "reason": why})
            if not allowed and not perm_override:
                refused.append((symbol, side, f"permission:{perm.state}"))
                continue
            klo, khi = round(px*lo, 2), round(px*hi, 2)
            # E106: hand the picker the expiries this (symbol, side) already
            # holds - OCC YYMMDD -> YYYY-MM-DD - so it moves to the next date.
            _skip = {f"20{e[:2]}-{e[2:4]}-{e[4:6]}"
                     for (u, r, e) in book.get("held_exp", set())
                     if u == symbol and r == side}
            picked = choose_expiry(feed, symbol, side, gte, lte, klo, khi,
                                   skip_expiries=_skip)
            if not picked:
                # E88: a bare `continue` here could not distinguish "no expiry
                # is liquid enough" - a real, expected outcome - from "the
                # chain's open-interest field was unreadable", which makes every
                # strike look illiquid and drops the symbol with nobody told.
                # _as_int returns 0 for unreadable values, which fails closed
                # correctly, but 0 and "unknown" are not the same fact.
                from deltax.screener import LAST_UNREADABLE_OI as _bad_oi
                if _bad_oi:
                    ledger.record_raw({
                        "action": "expiry_skipped_unreadable_oi",
                        "symbol": symbol, "side": side,
                        "unreadable_values": _bad_oi[:8],
                        "unreadable_count": len(_bad_oi),
                        "consequence": "every strike counted as illiquid - the "
                                       "symbol was skipped for a DATA fault, not "
                                       "a liquidity one"})
                    refused.append((symbol, side, "unreadable_oi"))
                else:
                    refused.append((symbol, side, "no_liquid_expiry"))
                continue
            expiry_str, oi = picked
            # E106: refuse a duplicate of a structure already in the book or
            # working at the broker. Expiry is YYYY-MM-DD here and YYMMDD in
            # the OCC symbol; normalise before comparing.
            _exp6 = expiry_str.replace("-", "")[2:]
            if (symbol, side, _exp6) in book.get("held_exp", set()):
                refused.append((symbol, side, "already_held"))
                continue
            # One expiry per query - the endpoint pages by expiry then strike.
            chain = feed.option_chain(symbol, option_type=side,
                                      expiry_gte=expiry_str, expiry_lte=expiry_str,
                                      strike_gte=klo, strike_lte=khi)
            if not chain:
                continue
            # E95: search the chain instead of guessing one structure. The old
            # call took the single strike nearest target delta and the single
            # strike a width away; if that pair quoted badly the whole symbol
            # was lost for the cycle, even when the same expiry held a dozen
            # structures that pass every gate. Measured live: the point pick
            # nominated QQQ puts at OI 226 (below the 500 floor) and QQQ calls
            # at a 17% spread (above the 15% cap) while the search found OI 507
            # and a 10% spread at a better credit, on the same chain.
            from deltax.screener import search_vertical as _search
            cand = _search(chain, side=side, target_delta=target,
                           width=DEFAULT_WIDTH.get(symbol, 5.0),
                           oi_by_symbol=oi,
                           max_spread_pct=_G.MAX_SPREAD_PCT,
                           min_credit=_G.MIN_CREDIT)
            # E101: realised vol for the variance-premium gate. Computed once
            # per symbol per cycle and reused for both sides - it is a property
            # of the underlying, not of the structure. A gate with no data is
            # dead code (E74), so this is fetched before the gate can run, and
            # None reaches the gate as a refusal rather than a skip.
            if _rv_cache.get(symbol, "miss") == "miss":
                try:
                    _rv_cache[symbol] = realized_vol_20(feed, symbol, today)
                except Exception:
                    _rv_cache[symbol] = None
            if not cand:
                # A genuine "nothing here is tradeable" - record it rather than
                # skipping in silence, so an empty cycle can be told apart from
                # a broken one (E84/E88).
                ledger.record_raw({"action": "no_tradeable_structure",
                                   "symbol": symbol, "side": side,
                                   "expiry": expiry_str,
                                   "note": "no (short,long) pair in the delta band "
                                           "cleared the OI floor and spread cap"})
                refused.append((symbol, side, "no_tradeable_structure"))
                continue
            dec = evaluate(
                symbol=symbol, equity=equity,
                max_loss_per_contract=cand["max_loss_per_contract"],
                max_profit_per_contract=cand["max_profit_per_contract"],
                credit=cand["credit"], expiry=date.fromisoformat(cand["expiry"]),
                today=today, open_interest=cand["open_interest"],
                open_portfolio_max_loss=committed, structure="credit",
                width=cand["width"], short_delta=cand["short"]["delta"],
                worst_leg_spread_pct=cand["worst_leg_spread_pct"],
                roundtrip_cost=cand.get("roundtrip_cost"),
                implied_vol=cand.get("implied_vol"),
                realized_vol=_rv_cache.get(symbol),
                tradable=True, last_bar_age_days=bar_age, asset_class="equity")
            bias, bias_icon, bias_note = directional_bias(side, "credit")
            ledger.record(dec, context={
                "book": "income", "side": side, "regime": regime.note,
                # bias is the DIRECTION expressed. short_leg/long_leg are the
                # spread's legs. Both used to be called "short"/"long", which
                # made the ledger unreadable on exactly the question the team
                # asked: which way are we leaning?
                "bias": bias, "bias_note": bias_note,
                "short_leg": cand["short"]["strike"],
                "long_leg": cand["long"]["strike"],
                "delta": round(cand["short"]["delta"], 4),
                "credit": round(cand["credit"], 2)})
            if dec.decision != "TRADE":
                refused.append((symbol, side, dec.failed_gate))
                continue
            legs = [execute.Leg(cand["short"]["symbol"], "sell"),
                    execute.Leg(cand["long"]["symbol"], "buy")]
            # LAST gate before real money: read the tape for THIS name only.
            # Runs here, not earlier, so it screens the handful that survived 13
            # gates rather than the whole universe (E35).
            if symbol not in news_checked:
                news_checked[symbol] = news_gate.screen(symbol)
                ledger.record_raw({"action": "news_gate", "symbol": symbol,
                                   "allowed": news_checked[symbol]["allowed"],
                                   "read": news_checked[symbol]["read"],
                                   "recent": news_checked[symbol]["recent"],
                                   "reachable": news_checked[symbol]["reachable"],
                                   "reason": news_checked[symbol]["reason"]})
            nv = news_checked[symbol]
            if not nv["allowed"]:
                refused.append((symbol, side, "news"))
                continue

            # E57: hard ceiling applied AFTER every risk calculation, so it can
            # only ever reduce size, never raise it.
            qty = demo_cap(dec.contracts)
            if qty != dec.contracts:
                ledger.record_raw({"action": "demo_size_cap", "symbol": symbol,
                                   "side": side, "sized": dec.contracts,
                                   "capped_to": qty, "reason": "E57"})
            try:
                rec = execute.submit(legs, qty, cand["credit"],
                                     dry_run=dry_run,
                                     context={"symbol": symbol, "side": side})
            except Exception as e:
                # One rejected order must not kill the loop. Record it, skip
                # this candidate, keep the rest of the book intact.
                ledger.record_raw({"action": "submit_failed", "symbol": symbol,
                                   "side": side, "error": f"{type(e).__name__}: {str(e)[:160]}"})
                refused.append((symbol, side, "submit_failed"))
                continue
            ledger.record_raw(rec)
            # E5/E15: the exit is placed AT ENTRY, not watched for later. An
            # exit that needs the agent alive at the right minute is not an
            # exit — and the 50% close is where the measured edge lives.
            if str(rec.get("result", "")).startswith(("SUBMITTED", "DRY_RUN")):
                ex = place_exit(legs, qty, cand["credit"],
                                ledger=ledger, dry_run=dry_run,
                                # E119: this IS the submitted limit
                                entry_credit_source=CREDIT_SOURCE_SUBMITTED)
                exits_placed.append((symbol, side, ex.get("limit_price")))
            # Risk actually committed follows the CAPPED size, not the sized
            # quantity, or the book would reserve budget it never spent.
            committed += dec.max_loss * (qty / dec.contracts if dec.contracts else 1)
            traded.append((symbol, f"{side}/{bias}", qty, cand["credit"],
                           dec.max_loss, rec["result"]))
    return {"regime": regime, "traded": traded, "refused": refused,
            "committed": committed, "skipped": None,
            "permission": perm.state, "advisory_only": perm_override,
            "book": book, "exits": exits_placed, "swept": swept,
            "equities": _eq,          # E86: never silent
            "catalyst": catalyst_result, "rotation": rotation_result}


if __name__ == "__main__":
    live = "--live" in sys.argv
    if live and "--force" in sys.argv:
        sys.exit("REFUSED: --force is an analysis switch and cannot be "
                 "combined with --live. Trade permission is absolute for "
                 "real orders.")
    feed, led = AlpacaFeed(), Ledger("logs")
    out = run(feed, led, equity=100_000.0, today=date.today(),
              dry_run=not live, force_window="--force" in sys.argv)
    if out.get("skipped"):
        # Even a skipped cycle narrates: account state, why, and what it holds.
        try:
            acct = feed.account()
            eq, csh = float(acct.get("equity") or 0), float(acct.get("cash") or 0)
        except Exception as _ae:
            # E89: this degraded to 0.0, which the board rendered as
            # "$0.00 (-100.0% today)" and "net -100,000.00" - a transient API
            # failure shown to the team, and on the public board to the judges,
            # as total loss of the fund. None means "not read".
            eq = csh = None
            led.record_raw({"action": "account_read_failed",
                            "error": f"{type(_ae).__name__}: {str(_ae)[:120]}",
                            "consequence": "board shows account unavailable"})
        # E75: the clock is read AGAIN here, and it sat outside the try above.
        # That is how the 2 Sep traceback actually reached the terminal: the
        # clock failed inside run(), run() returned "skipped", and this line
        # then called the same failing endpoint a second time and crashed the
        # process on the way out. Reporting must never be able to raise.
        try:
            _mkt = bool(feed.clock().get("is_open"))
        except Exception:
            _mkt = False
        print(report.render(equity=eq, cash=csh,
                            market_open=_mkt,
                            regime=getattr(out.get("regime"), "note", "—"),
                            permission=out.get("permission", "—"),
                            events=[("refuse", "CYCLE", out["skipped"])]))
        sys.exit(0)

    try:
        acct = feed.account()
        eq, csh = float(acct.get("equity") or 0), float(acct.get("cash") or 0)
    except Exception as _ae:
        eq = csh = None                     # E89: see above - never render 0.0
        led.record_raw({"action": "account_read_failed",
                        "error": f"{type(_ae).__name__}: {str(_ae)[:120]}",
                        "consequence": "board shows account unavailable"})
    r = out["regime"]
    # E105: real scoreboard inputs. Unrealised is the broker's own mark on the
    # open legs; realised is what the account has actually banked relative to
    # the start, net of that. Rows pair each short leg with its long by
    # (underlying, right, expiry) - the same grouping the exit sweep uses.
    _t_unreal, _t_real, _t_rows = 0.0, 0.0, []
    try:
        from deltax.reconcile import parse_occ as _p_occ
        _plist = list(feed.positions())
        _t_unreal = sum(float(p.get("unrealized_pl") or 0) for p in _plist)
        _t_real = (eq - 100_000.0) - _t_unreal if eq is not None else 0.0
        _grp = {}
        for p in _plist:
            o = _p_occ(p.get("symbol", ""))
            if not o:
                continue
            q = float(p.get("qty") or 0)
            _grp.setdefault((o["underlying"], o["right"], o["expiry"]), {})[
                "short" if q < 0 else "long"] = (abs(q), abs(float(p.get("avg_entry_price") or 0)),
                                                abs(float(p.get("current_price") or 0)), o["strike"])
        for (u, rt, ex_), v in sorted(_grp.items()):
            sh, lg = v.get("short"), v.get("long")
            if not sh or not lg:
                continue
            credit = sh[1] - lg[1]
            cur = sh[2] - lg[2]
            _t_rows.append({"symbol": u, "side": rt, "short": sh[3], "long": lg[3],
                            "credit": credit, "current": cur,
                            "captured": ((credit - cur) / credit) if credit > 0 else None})
    except Exception:
        pass                                # terminal only; never take the run down
    events = [("open", sym, f"OPENED {side} · {ct} ct · credit ${cr*100*ct:,.0f} → {res}")
              for sym, side, ct, cr, ml, res in out["traded"]]
    events += [("exit", sym, f"EXIT RESTING at {lim:.2f} (50% of credit) · fills unattended")
               for sym, side, lim in out.get("exits", []) if lim]
    print(report.render(
        equity=eq, cash=csh, market_open=True,
        # E105: these were hard-coded 0.0 and [], so the terminal printed
        # "realized +0.00 / unrealized +0.00 / no open positions" over a
        # 12-leg book with -$1,212 realised on the SMH close. Same shape as
        # E78 and E89: a report that does not describe what happened. Both
        # figures now come from the broker; the rows are the paired book.
        unrealized=_t_unreal, realized=_t_real,
        positions=_t_rows, events=events, refused=out["refused"],
        regime=f"{r.weak_count}/3 weak", permission=out.get("permission", "—")))
