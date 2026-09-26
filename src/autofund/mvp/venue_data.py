"""The candidate venue set, with every external claim tied to the official source it came from.

**This module is data, not logic.** It records what four venues publish and how each fact was
established, so the arithmetic in `venue_economics` can be audited against its inputs. Nothing
here invents a number: a fact that could not be established from an official source is recorded
as unknown, and the spec's instruction not to request credentials in this milestone means some of
them stay unknown by design rather than by omission.

**The set is deliberately small.** The spec caps it at three serious venues beside the incumbent,
chosen on a credible API, documented fees, usable spot markets and relevance to a Mexican
retail account. The four below are the venues a Mexican retail trader would actually consider,
and no others were evaluated: adding venues would not change a conclusion that turns on a gap of
two orders of magnitude, and ranking on marketing claims is forbidden.

**Retrieval dates are recorded because fees change.** Each schedule carries the moment it was
read, so a stale figure can be identified as stale rather than trusted indefinitely, and the
model reports the retrieval date alongside every rate it uses.
"""

from datetime import UTC, datetime
from decimal import Decimal

from .venue_economics import (
    ACCOUNT_CONFIRMED,
    ACCOUNT_RATE_UNKNOWN,
    EFFORT_HIGH,
    EFFORT_LOW,
    PUBLISHED_BASELINE,
    VERIFIED,
    VenueFeeSchedule,
    VenueProfile,
    fee_rate_from_percent,
)

VENUE_DATA_VERSION = "autofund.venue-data.v1"

# The date the schedules below were read. Recorded once because they were all retrieved in the
# same research session, and carried into every certificate so a reader can tell how current the
# comparison is.
RETRIEVED_AT = "2026-09-25"

# Sources, kept as constants so the certificate can cite them without the prose drifting from
# what was actually consulted.
SOURCE_BITSO_ACCOUNT = "https://docs.bitso.com/bitso-api/docs/list-fees.md"
SOURCE_BITSO_FEES_ACCOUNT = "GET /v3/fees (account-authenticated, read-only)"
SOURCE_BITSO_BOOKS = "https://api.bitso.com/v3/available_books/"
SOURCE_BITSO_TICKER = "https://api.bitso.com/v3/ticker/"
SOURCE_BITSO_ORDERS = "https://docs.bitso.com/bitso-api/docs/place-an-order.md"
SOURCE_BINANCE_FEES = "https://www.binance.com/en/fee/schedule (Spot tab, Regular User row)"
SOURCE_BINANCE_EXCHANGE_INFO = "https://api.binance.com/api/v3/exchangeInfo"
SOURCE_KRAKEN_FEES = ("https://www.kraken.com/features/fee-schedule "
                      "(Kraken Pro -> Spot Crypto tab, Tier 1 row)")
SOURCE_KRAKEN_PAIRS = "https://api.kraken.com/0/public/AssetPairs"
SOURCE_COINBASE_FEES = ("https://help.coinbase.com/en/coinbase/trading-and-funding/"
                        "advanced-trade/advanced-trade-fees")
SOURCE_COINBASE_PRODUCTS = "https://api.exchange.coinbase.com/products"


def bitso_schedule() -> VenueFeeSchedule:
    """The incumbent's account-confirmed rates.

    Taken from the real account's own fee schedule, read GET-only, which is why this is the only
    entry tagged ACCOUNT_CONFIRMED: it is a measured fact about this account rather than a
    published rate that might not apply to it.
    """
    return VenueFeeSchedule(
        venue="Bitso", maker_rate=Decimal("0.0060"), taker_rate=Decimal("0.0078"),
        basis=ACCOUNT_CONFIRMED,
        source=f"{SOURCE_BITSO_ACCOUNT}; {SOURCE_BITSO_FEES_ACCOUNT}",
        retrieved_at=RETRIEVED_AT,
        notes=("account-confirmed via GET /v3/fees with read-only credentials; the published "
               "schedule is volume-tiered but this account's actual rates are what is recorded"))


def binance_schedule() -> VenueFeeSchedule:
    """Binance's published Regular User spot rate.

    This is the lowest published baseline found across the candidate set, and it is a *taker* rate
    requiring no volume tier and no passive fill. That matters for interpretation: it means the
    cheapest account-accessible cost identified is also the one that does not depend on a maker
    order being filled, so it cannot be dismissed as an unavailable best case.
    """
    return VenueFeeSchedule(
        venue="Binance", maker_rate=fee_rate_from_percent(Decimal("0.100")),
        taker_rate=fee_rate_from_percent(Decimal("0.100")), basis=PUBLISHED_BASELINE,
        source=SOURCE_BINANCE_FEES, retrieved_at=RETRIEVED_AT,
        notes=("Regular User row on the Spot tab: 0.100% / 0.100%. The BNB-discount column "
               "quotes 0.075%, but it requires holding BNB, so the undiscounted public rate is "
               "used and the discount is not claimed"))


def kraken_schedule() -> VenueFeeSchedule:
    """Kraken's published Tier 1 spot rate.

    Tier 1 is the entry tier, so it applies to a new account without any volume history. It is
    also the most expensive of the four, which makes it the clearest example of a venue where the
    published fee alone settles the question.
    """
    return VenueFeeSchedule(
        venue="Kraken", maker_rate=fee_rate_from_percent(Decimal("0.40")),
        taker_rate=fee_rate_from_percent(Decimal("0.80")), basis=PUBLISHED_BASELINE,
        source=SOURCE_KRAKEN_FEES, retrieved_at=RETRIEVED_AT,
        notes=("Tier 1 row ($0+ 30-day volume): maker 0.40%, taker 0.80%. Higher tiers require "
               "volume or assets on platform, neither of which this account has"))


def coinbase_schedule() -> VenueFeeSchedule:
    """Coinbase Advanced, whose rates cannot be seen without authenticating.

    Marked ACCOUNT_RATE_UNKNOWN because Coinbase's own help page says to sign in to view the fee
    structure. The spec forbids requesting credentials in this milestone, so the gap is carried
    rather than filled, and it propagates into an INSUFFICIENT_COST_EVIDENCE verdict instead of a
    guessed favourable one. This is the case that proves the model can say "I do not know".
    """
    return VenueFeeSchedule(
        venue="Coinbase Advanced", maker_rate=None, taker_rate=None,
        basis=ACCOUNT_RATE_UNKNOWN, source=SOURCE_COINBASE_FEES, retrieved_at=RETRIEVED_AT,
        notes=("official help page directs the reader to sign in to see the complete fee "
               "structure, so no published baseline is available; credentials were not requested"))


def venue_schedules() -> tuple[VenueFeeSchedule, ...]:
    return (bitso_schedule(), binance_schedule(), kraken_schedule(), coinbase_schedule())


def schedule_for(venue: str) -> VenueFeeSchedule:
    for schedule in venue_schedules():
        if schedule.venue.lower() == venue.lower():
            return schedule
    raise KeyError(f"no recorded schedule for venue {venue!r}")


# ---------------------------------------------------------------------------------------------
# Feasibility contracts.
#
# Verified from public endpoints on the retrieval date, or from official documentation. A field
# set to None means it could not be established, never that it is absent.
# ---------------------------------------------------------------------------------------------

def bitso_profile() -> VenueProfile:
    """The incumbent. The only venue here with MXN-quoted books and a verified minimum."""
    return VenueProfile(
        venue="Bitso", accessibility=VERIFIED,
        spot_api=True, public_market_data_api=True, authenticated_trading_api=True,
        supports_market_orders=True, supports_limit=True, supports_post_only=True,
        supports_client_order_id=True, order_status_api=True, fills_api=True,
        open_orders_api=True, cancel_api=True, websocket_support=True,
        order_book_api=True, trade_tape_api=True,
        minimum_order_quote=Decimal("10"), minimum_order_currency="MXN",
        mxn_quote_pairs=True,
        mxn_pair_symbols=("btc_mxn", "eth_mxn", "sol_mxn", "xrp_mxn"),
        fee_tier_requirements="volume-tiered; this account's rates are confirmed",
        fee_currency_semantics="BUY_FEE_IN_BASE;SELL_FEE_IN_QUOTE",
        rate_limits="documented throttling; the observer already paces at 1.05s per request",
        liquidity_evidence=("12 MXN-quoted books; observed spreads 2.62-7.88 bps across the four "
                            "tracked books at the time of reading"),
        recovery_feasibility=("origin_id gives client-order identity, and open/order-status/fills "
                              "endpoints exist, so restart reconciliation is implementable"),
        migration_effort=EFFORT_LOW,
        notes=("already integrated; the comparison baseline. post-only is available via "
               "time_in_force=postonly, which is what makes a maker scenario representable"))


def binance_profile() -> VenueProfile:
    """Lowest published fee, but its MXN minimums are an order of magnitude out of reach.

    The minimum is the finding, not the fee. Binance publishes the cheapest account-accessible
    rate of the four and simultaneously requires 150 MXN on every MXN pair, which is roughly
    fourteen times the project's single-order cap. That combination is exactly why the spec
    forbids assuming a lower fee is a better product.
    """
    return VenueProfile(
        venue="Binance", accessibility=VERIFIED,
        spot_api=True, public_market_data_api=True, authenticated_trading_api=True,
        supports_market_orders=True, supports_limit=True, supports_post_only=True,
        supports_client_order_id=True, order_status_api=True, fills_api=True,
        open_orders_api=True, cancel_api=True, websocket_support=True,
        order_book_api=True, trade_tape_api=True,
        minimum_order_quote=Decimal("150"), minimum_order_currency="MXN",
        mxn_quote_pairs=True,
        mxn_pair_symbols=("BTCMXN", "ETHMXN", "SOLMXN", "USDTMXN", "USDCMXN", "XRPMXN"),
        fee_tier_requirements="Regular User tier needs no volume and no BNB balance",
        fee_currency_semantics="fee charged in the received asset by default",
        rate_limits="documented per-endpoint weights; compatible with the observer's pacing",
        liquidity_evidence=("deepest of the candidate set on BTC; four of six MXN pairs were "
                            "TRADING with XRPMXN in BREAK at the time of reading"),
        recovery_feasibility=("newOrderRespType and clientOrderId support exist, and order/fill "
                              "endpoints are documented, so recovery is implementable"),
        migration_effort=EFFORT_HIGH,
        notes=("minNotional is 150 MXN on every MXN pair with applyMinToMarket=True, so the "
               "11 MXN single-order envelope cannot place a market order at all"))


def kraken_profile() -> VenueProfile:
    """Credible API and deep USD books, but no MXN pair exists to trade.

    Reaching this venue from an MXN account requires a conversion leg that would itself pay a
    spread and a fee, so it is not a like-for-like comparison with the incumbent. The spread and
    conversion cost are not estimated here because the venue cannot be priced against the alpha
    without inventing them.
    """
    return VenueProfile(
        venue="Kraken", accessibility=VERIFIED,
        spot_api=True, public_market_data_api=True, authenticated_trading_api=True,
        supports_market_orders=True, supports_limit=True, supports_post_only=True,
        supports_client_order_id=True, order_status_api=True, fills_api=True,
        open_orders_api=True, cancel_api=True, websocket_support=True,
        order_book_api=True, trade_tape_api=True,
        minimum_order_quote=Decimal("0.5"), minimum_order_currency="USD",
        mxn_quote_pairs=False, mxn_pair_symbols=(),
        fee_tier_requirements="Tier 1 is the entry tier; higher tiers need 30-day volume or AoP",
        fee_currency_semantics=("charged on quote-currency volume by default; a fee-currency "
                                "option exists on some pairs"),
        rate_limits="documented call counters per tier; compatible with the observer's pacing",
        liquidity_evidence=("1451 listed pairs with deep USD books, but none quoted in MXN"),
        recovery_feasibility="client order ids and order/fill endpoints are documented",
        migration_effort=EFFORT_HIGH,
        notes=("zero MXN-quoted pairs were returned by the public AssetPairs endpoint, so an "
               "MXN-funded account cannot trade it without an unconverted FX leg"))


def coinbase_profile() -> VenueProfile:
    """No MXN products, and fees that are not knowable without authenticating."""
    return VenueProfile(
        venue="Coinbase Advanced", accessibility=VERIFIED,
        spot_api=True, public_market_data_api=True, authenticated_trading_api=True,
        supports_market_orders=True, supports_limit=True, supports_post_only=True,
        supports_client_order_id=True, order_status_api=True, fills_api=True,
        open_orders_api=True, cancel_api=True, websocket_support=True,
        order_book_api=True, trade_tape_api=True,
        minimum_order_quote=None, minimum_order_currency=None,
        mxn_quote_pairs=False, mxn_pair_symbols=(),
        fee_tier_requirements="not publishable: the official page directs the reader to sign in",
        fee_currency_semantics="not established",
        rate_limits="not established from the sources consulted",
        liquidity_evidence=("514 online products, of which zero are quoted in MXN"),
        recovery_feasibility="documented endpoints exist, but the venue is unreachable in MXN",
        migration_effort=EFFORT_HIGH,
        notes=("no MXN product exists, and Advanced Trade fees require sign-in to view, so the "
               "venue is both unreachable and unpriceable without credentials this milestone "
               "is forbidden to request"))


def venue_profiles() -> tuple[VenueProfile, ...]:
    return (bitso_profile(), binance_profile(), kraken_profile(), coinbase_profile())


def profile_for(venue: str) -> VenueProfile:
    for profile in venue_profiles():
        if profile.venue.lower() == venue.lower():
            return profile
    raise KeyError(f"no recorded profile for venue {venue!r}")


# ---------------------------------------------------------------------------------------------
# Measured inputs carried forward from earlier milestones and from public ticks.
# ---------------------------------------------------------------------------------------------

# The account-confirmed fee-derived floors, reproduced from MVP 0.2.4's certificate rather than
# recomputed, so this milestone cannot silently change a historical conclusion (spec section 7).
ESTABLISHED_FEE_FLOORS_BPS: dict[str, str] = {
    "TAKER_TAKER": "157.8443689034903612824254100",
    "MAKER_TAKER": "139.4498821187556704873465700",
    "MAKER_MAKER": "121.0887052698484670599047000",
}

# The friction model the earlier milestones used, and the one this milestone compares against:
# two taker fee legs, the spread crossed once, and modelled slippage.
HISTORICAL_SPREAD_ASSUMPTION_BPS = Decimal("12")
HISTORICAL_SLIPPAGE_ASSUMPTION_BPS = Decimal("5")

# Spreads observed on the incumbent's public ticker on the retrieval date. Tighter than the 12 bps
# the friction model assumed, and recorded separately rather than substituted for it so the
# comparison against historical conclusions stays like-for-like.
OBSERVED_SPREADS_BPS: dict[str, str] = {
    "btc_mxn": "7.88",
    "eth_mxn": "2.95",
    "sol_mxn": "4.92",
    "xrp_mxn": "2.62",
}
OBSERVED_SPREAD_SOURCE = SOURCE_BITSO_TICKER

# Order-book depth at the intended size was not measured, so slippage for an alternative venue
# is unknown rather than modelled. Assuming the incumbent's 5 bps for a venue whose book has never
# been read would be inventing evidence for the venue being compared.
ALTERNATIVE_VENUE_SLIPPAGE_BPS: Decimal | None = None


def retrieval_moment() -> datetime:
    return datetime(2026, 9, 25, tzinfo=UTC)


def public() -> dict[str, object]:
    """Everything this module asserts, for inclusion in the certificate."""
    return {
        "version": VENUE_DATA_VERSION,
        "retrieved_at": RETRIEVED_AT,
        "schedules": [schedule.public() for schedule in venue_schedules()],
        "profiles": [profile.public() for profile in venue_profiles()],
        "established_fee_floors_bps": dict(ESTABLISHED_FEE_FLOORS_BPS),
        "historical_friction_model": {
            "spread_bps": str(HISTORICAL_SPREAD_ASSUMPTION_BPS),
            "slippage_bps": str(HISTORICAL_SLIPPAGE_ASSUMPTION_BPS),
            "source": "MVP 0.2.3-0.2.6 friction model, preserved for comparability",
        },
        "observed_spreads_bps": dict(OBSERVED_SPREADS_BPS),
        "observed_spreads_source": OBSERVED_SPREAD_SOURCE,
        "alternative_venue_slippage_bps": None,
        "alternative_venue_slippage_reason": ("no order book for an alternative venue was read, "
                                              "so slippage there is unknown rather than modelled"),
        "credentials_requested": False,
        "evaluated_venue_count": len(venue_profiles()),
        "selection_criteria": ("credible API, sufficient liquidity, documented fee schedule, "
                               "usable spot markets, relevance to a Mexican retail account"),
    }
