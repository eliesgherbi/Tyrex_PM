"""Reference InputContract mappings: ask70, z_gap, and q-edge (guide, not built)."""

from __future__ import annotations

from tyrex_pm.facts import ids as F
from tyrex_pm.facts.contract import InputContract, OnFact, OnTimer
from tyrex_pm.indicators.spec import IndicatorSpec

ASK70_INPUT_CONTRACT = InputContract(
    required=(F.POLYMARKET_BOOKS, F.CLOCK_SYNC, F.ACCOUNT_SNAPSHOT),
    optional=(F.POLYMARKET_MARKET_META,),
    indicators=(),
    evaluate_on=(
        OnFact(F.POLYMARKET_BOOKS, coalesce_s=1.0),
        OnTimer(interval_s=1.0),
    ),
)

Z_GAP_EWMA_VOL = IndicatorSpec(
    name="ewma_vol",
    source=F.BINANCE_SPOT_TRADES,
)

Z_GAP_INPUT_CONTRACT = InputContract(
    required=(
        F.POLYMARKET_BOOKS,
        F.BINANCE_SPOT_TRADES,
        F.CHAINLINK_TWAP,
        F.CLOCK_SYNC,
        F.ACCOUNT_SNAPSHOT,
        F.PTB_SEALED,
        F.REFERENCE_ALIGNED,
        F.POLYMARKET_MARKET_META,
    ),
    optional=(),
    indicators=(Z_GAP_EWMA_VOL,),
    evaluate_on=(
        OnFact(F.BINANCE_SPOT_TRADES, coalesce_s=1.0, after_ready=F.PTB_SEALED),
    ),
)

# Guide strategy only — not registered as a live plugin.
Q_EDGE_INPUT_CONTRACT = InputContract(
    required=(
        F.POLYMARKET_BOOKS,
        F.POLYMARKET_MARKET_META,
        F.CHAINLINK_TWAP,
        F.BINANCE_SPOT_TRADES,
        F.BINANCE_SPOT_L2,
        F.BINANCE_SPOT_MID,
        F.BINANCE_PERP_TRADES,
        F.BINANCE_PERP_L2,
        F.BINANCE_PERP_MID,
        F.BINANCE_PERP_FUNDING,
        F.CLOCK_SYNC,
        F.ACCOUNT_SNAPSHOT,
        F.PTB_SEALED,
        F.TAU,
    ),
    optional=(F.REFERENCE_ALIGNED,),
    indicators=(
        IndicatorSpec(name="distance_to_k", source=F.CHAINLINK_TWAP, extra_sources=(F.PTB_SEALED,)),
        IndicatorSpec(name="spot_cl_gap", source=F.BINANCE_SPOT_MID, extra_sources=(F.CHAINLINK_TWAP,)),
        IndicatorSpec(
            name="imbalance",
            source=F.BINANCE_SPOT_L2,
            levels=(1, 3, 5, 10),
        ),
        IndicatorSpec(name="microprice_gap", source=F.BINANCE_SPOT_L2),
        IndicatorSpec(
            name="ofi",
            source=F.BINANCE_SPOT_L2,
            horizons_ms=(250, 1000, 3000, 5000, 15000),
        ),
        IndicatorSpec(
            name="ofi",
            source=F.BINANCE_PERP_L2,
            horizons_ms=(1000, 5000),
        ),
        IndicatorSpec(
            name="trade_imbalance",
            source=F.BINANCE_SPOT_TRADES,
            horizons_ms=(1000, 5000, 15000),
        ),
        IndicatorSpec(
            name="momentum",
            source=F.BINANCE_SPOT_MID,
            horizons_ms=(1000, 5000, 15000, 30000, 60000),
        ),
        IndicatorSpec(
            name="realized_vol",
            source=F.BINANCE_SPOT_TRADES,
            horizons_ms=(10000, 30000, 60000, 300000),
        ),
        IndicatorSpec(
            name="basis",
            source=F.BINANCE_SPOT_MID,
            extra_sources=(F.BINANCE_PERP_MID,),
        ),
        IndicatorSpec(
            name="trade_imbalance",
            source=F.BINANCE_PERP_TRADES,
            horizons_ms=(1000, 5000),
        ),
        IndicatorSpec(name="microprice_gap", source=F.BINANCE_PERP_L2),
    ),
    evaluate_on=(
        OnFact(F.CHAINLINK_TWAP, coalesce_s=0.2, after_ready=F.PTB_SEALED),
        OnFact(F.BINANCE_SPOT_L2, coalesce_s=0.2, after_ready=F.PTB_SEALED),
        OnFact(F.BINANCE_SPOT_TRADES, coalesce_s=0.2, after_ready=F.PTB_SEALED),
        OnFact(F.POLYMARKET_BOOKS, coalesce_s=0.2, after_ready=F.PTB_SEALED),
    ),
)

REFERENCE_CONTRACTS: dict[str, InputContract] = {
    "ask70": ASK70_INPUT_CONTRACT,
    "z_gap": Z_GAP_INPUT_CONTRACT,
    "q_edge": Q_EDGE_INPUT_CONTRACT,
}
