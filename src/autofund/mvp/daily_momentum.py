"""A3: pre-registered evaluation of low-turnover trend hypotheses on daily bars.

This module judges; it does not trade. It is built so the failure modes that make backtests lie are
structurally hard:

* **Registered first.** Every choice (universe, lookbacks, split dates, cost, thresholds, seed) comes
  from a registration file whose SHA-256 is stored in the result, and the caller must prove the file
  was committed unchanged before the holdout is touched.
* **Selection on development only.** The configuration is chosen using the development window. The
  holdout is then evaluated for that single configuration, once.
* **Every trial is counted** and the significance threshold is divided by the count.
* **Net of costs, always.** Cost is charged per side on every change of position.
* **A control with the same shape.** A circularly shifted copy of each position series keeps
  exposure and turnover and destroys timing, so beating it means the *timing* carried information.
* **No lookahead.** A position decided at the close of day ``t`` first earns the return of day
  ``t + 1``.

The expected outcome on most data is ``NO_TRADE``, and that is a complete result.
"""

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from autofund.decimal_utils import ONE, ZERO, financial
from autofund.exchanges.alpaca.models import AlpacaBarSeries

EVALUATION_VERSION = "autofund.daily-momentum.v1"
TRADING_DAYS = Decimal("252")
BPS = Decimal("10000")
ELIGIBLE_FOR_PAPER = "PASS_TO_PAPER"
NO_TRADE = "NO_TRADE"


class RegistrationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Registration:
    id: str
    symbols: tuple[str, ...]
    adjustment: str
    feed: str
    start: date
    dev_end: date
    holdout_start: date
    end: date
    lookbacks: tuple[int, ...]
    rebalance_every: int
    cost_bps_per_side: Decimal
    control_permutations: int
    seed: int
    alpha: Decimal
    min_holdout_days: int
    prior_trials: int
    sha256: str

    @property
    def trials(self) -> int:
        """Every configuration this research programme has tried, earlier hypotheses included."""
        return self.prior_trials + len(self.lookbacks)


def load_registration(path: str | Path) -> Registration:
    raw = Path(path).read_bytes()
    try:
        d = json.loads(raw)
        reg = Registration(
            id=d["id"], symbols=tuple(d["symbols"]), adjustment=d["adjustment"], feed=d["feed"],
            start=date.fromisoformat(d["start"]), dev_end=date.fromisoformat(d["dev_end"]),
            holdout_start=date.fromisoformat(d["holdout_start"]), end=date.fromisoformat(d["end"]),
            lookbacks=tuple(int(x) for x in d["lookbacks"]),
            rebalance_every=int(d["rebalance_every"]),
            cost_bps_per_side=Decimal(str(d["cost_bps_per_side"])),
            control_permutations=int(d["control_permutations"]), seed=int(d["seed"]),
            alpha=Decimal(str(d["alpha"])), min_holdout_days=int(d["min_holdout_days"]),
            prior_trials=int(d.get("prior_trials", 0)),
            sha256=hashlib.sha256(raw).hexdigest(),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise RegistrationError(f"malformed registration: {type(exc).__name__}") from None
    if not reg.start < reg.dev_end < reg.holdout_start < reg.end:
        raise RegistrationError("dates must satisfy start < dev_end < holdout_start < end")
    if not reg.lookbacks or min(reg.lookbacks) < 2 or reg.rebalance_every < 1:
        raise RegistrationError("invalid lookbacks or rebalance interval")
    if reg.prior_trials < 0:
        raise RegistrationError("prior_trials cannot be negative")
    if reg.control_permutations < 99 or reg.alpha <= ZERO or reg.alpha >= ONE:
        raise RegistrationError("invalid control or alpha")
    return reg


# ---- data ----

@dataclass(frozen=True, slots=True)
class Panel:
    """Aligned adjusted closes: ``closes[symbol][i]`` is the close on ``dates[i]``."""

    dates: tuple[date, ...]
    closes: dict[str, tuple[Decimal, ...]]


def build_panel(series: dict[str, AlpacaBarSeries], reg: Registration) -> Panel:
    for symbol in reg.symbols:
        s = series.get(symbol)
        if s is None:
            raise RegistrationError(f"missing series for {symbol}")
        if (s.adjustment, s.feed) != (reg.adjustment, reg.feed):
            raise RegistrationError("series adjustment/feed differ from the registration")
        if s.gaps:
            raise RegistrationError(f"{symbol} has confirmed gaps; refusing to interpolate")
    by_symbol = {
        sym: {c.timestamp.date(): c.close for c in series[sym].candles
              if reg.start <= c.timestamp.date() <= reg.end}
        for sym in reg.symbols
    }
    common = sorted(set.intersection(*(set(m) for m in by_symbol.values())))
    if not common:
        raise RegistrationError("no common trading days in the registered window")
    return Panel(tuple(common), {s: tuple(by_symbol[s][d] for d in common) for s in reg.symbols})


# ---- strategy ----

def positions(closes: tuple[Decimal, ...], lookback: int, every: int) -> tuple[int, ...]:
    """Long (1) or flat (0), decided at the close of each rebalance day from trailing return only.

    Before ``lookback`` bars exist the position is flat. Between rebalances it is held.
    """
    out: list[int] = []
    held = 0
    for i, close in enumerate(closes):
        if i >= lookback and i % every == 0:
            held = 1 if close > closes[i - lookback] else 0
        out.append(held)
    return tuple(out)


@financial
def net_returns(
    closes: dict[str, tuple[Decimal, ...]],
    book: dict[str, tuple[int, ...]],
    cost_bps_per_side: Decimal,
    lo: int,
    hi: int,
) -> tuple[Decimal, ...]:
    """Daily net portfolio returns for days ``lo..hi-1`` (equal weight, idle cash earns zero)."""
    weight = ONE / len(closes)
    cost = cost_bps_per_side / BPS
    out: list[Decimal] = []
    for t in range(lo, hi):
        total = ZERO
        for sym, series in closes.items():
            held = book[sym][t - 1]  # decided at the close of t-1: no lookahead
            previous = book[sym][t - 2] if t >= 2 else 0
            gross = (series[t] / series[t - 1] - ONE) * held
            total += weight * (gross - cost * abs(held - previous))
        out.append(total)
    return tuple(out)


# ---- metrics ----

@financial
def sharpe(returns: tuple[Decimal, ...]) -> Decimal:
    n = len(returns)
    if n < 2:
        return ZERO
    mean = sum(returns, ZERO) / n
    var = sum(((r - mean) ** 2 for r in returns), ZERO) / (n - 1)
    if var == ZERO:
        return ZERO
    return mean / var.sqrt() * TRADING_DAYS.sqrt()


@financial
def total_return(returns: tuple[Decimal, ...]) -> Decimal:
    growth = ONE
    for r in returns:
        growth *= ONE + r
    return growth - ONE


@financial
def max_drawdown(returns: tuple[Decimal, ...]) -> Decimal:
    equity, peak, worst = ONE, ONE, ZERO
    for r in returns:
        equity *= ONE + r
        peak = max(peak, equity)
        worst = min(worst, equity / peak - ONE)
    return worst


def turnover(book: dict[str, tuple[int, ...]], lo: int, hi: int) -> int:
    return sum(abs(b[t] - b[t - 1]) for b in book.values() for t in range(max(lo, 1), hi))


def _stats(returns: tuple[Decimal, ...]) -> dict[str, str]:
    return {"days": str(len(returns)), "net_sharpe": f"{sharpe(returns):.4f}",
            "total_return": f"{total_return(returns):.4f}",
            "max_drawdown": f"{max_drawdown(returns):.4f}"}


# ---- control ----

def control_p_value(
    closes: dict[str, tuple[Decimal, ...]],
    book: dict[str, tuple[int, ...]],
    reg: Registration,
    lo: int,
    hi: int,
    observed: Decimal,
) -> Decimal:
    """Share of timing-destroyed controls whose net Sharpe is at least the observed one.

    Each asset's *holdout-window* positions are circularly shifted by one common random offset. The
    exposure and the number of position changes are preserved; only the alignment with returns is
    destroyed. The ``+1`` terms make the estimate a valid, never-zero permutation p-value.
    """
    # Statistical permutation sampling, not security: it must be seeded and reproducible.
    rng = random.Random(reg.seed)  # nosec B311
    n = hi - lo
    if n < 4 * reg.rebalance_every:
        raise RegistrationError("window too short for a timing control")
    at_least = 0
    for _ in range(reg.control_permutations):
        offset = rng.randrange(reg.rebalance_every, n - reg.rebalance_every)
        shifted = {
            sym: held[:lo] + held[lo:hi][offset:] + held[lo:hi][:offset] + held[hi:]
            for sym, held in book.items()
        }
        value = sharpe(net_returns(closes, shifted, reg.cost_bps_per_side, lo, hi))
        if value >= observed:
            at_least += 1
    return Decimal(at_least + 1) / Decimal(reg.control_permutations + 1)


# ---- evaluation ----

def evaluate(panel: Panel, reg: Registration, *, registration_committed: bool) -> dict[str, Any]:
    """Run the registered evaluation. Refuses unless the registration was committed first."""
    if not registration_committed:
        raise RegistrationError(
            "the registration must be committed unchanged before any result is computed"
        )
    dates = panel.dates
    dev_hi = sum(1 for d in dates if d <= reg.dev_end)
    hold_lo = dev_hi
    hold_hi = len(dates)
    warmup = max(reg.lookbacks)
    if dev_hi - (warmup + 2) < 60:
        raise RegistrationError("development window too short after warm-up")
    dev_lo = warmup + 2
    if hold_hi - hold_lo < reg.min_holdout_days:
        raise RegistrationError("holdout shorter than the registered minimum")

    books = {
        lb: {s: positions(c, lb, reg.rebalance_every) for s, c in panel.closes.items()}
        for lb in reg.lookbacks
    }
    trials: list[dict[str, Any]] = []
    dev_sharpe: dict[int, Decimal] = {}
    for lb in reg.lookbacks:
        r = net_returns(panel.closes, books[lb], reg.cost_bps_per_side, dev_lo, dev_hi)
        dev_sharpe[lb] = sharpe(r)
        trials.append({"lookback": lb, "development": _stats(r),
                       "turnover": turnover(books[lb], dev_lo, dev_hi)})
    # Shortest lookback wins ties so selection never depends on dict or float ordering.
    chosen = max(reg.lookbacks, key=lambda lb: (dev_sharpe[lb], -lb))
    book = books[chosen]

    # ---- the single holdout evaluation ----
    hold = net_returns(panel.closes, book, reg.cost_bps_per_side, hold_lo, hold_hi)
    flat_book = {s: (1,) * len(dates) for s in panel.closes}
    bench = net_returns(panel.closes, flat_book, ZERO, hold_lo, hold_hi)
    hold_sharpe, bench_sharpe = sharpe(hold), sharpe(bench)
    p_value = control_p_value(panel.closes, book, reg, hold_lo, hold_hi, hold_sharpe)
    threshold = reg.alpha / reg.trials

    gates = {
        "development_net_sharpe_positive": dev_sharpe[chosen] > ZERO,
        "holdout_beats_benchmark_sharpe": hold_sharpe > bench_sharpe,
        "holdout_drawdown_not_worse_than_benchmark": max_drawdown(hold) >= max_drawdown(bench),
        "timing_control_p_below_adjusted_alpha": p_value <= threshold,
        "holdout_net_total_return_positive": total_return(hold) > ZERO,
    }
    decision = ELIGIBLE_FOR_PAPER if all(gates.values()) else NO_TRADE
    return {
        "version": EVALUATION_VERSION,
        "registration_id": reg.id,
        "registration_sha256": reg.sha256,
        "trials_counted": reg.trials,
        "prior_trials": reg.prior_trials,
        "cost_bps_per_side": str(reg.cost_bps_per_side),
        "development_trials": trials,
        "selected_lookback": chosen,
        "holdout_evaluations": 1,
        "holdout": _stats(hold),
        "holdout_turnover_position_changes": turnover(book, hold_lo, hold_hi),
        "benchmark_equal_weight_buy_and_hold": _stats(bench),
        "timing_control": {"permutations": reg.control_permutations, "p_value": f"{p_value:.4f}",
                           "adjusted_alpha": f"{threshold:.4f}"},
        "gates": gates,
        "decision": decision,
        "says_nothing_about": "after-tax return, capacity, or any period outside the window",
    }
