"""InputContract catalog, closures, and the three reference mappings."""

from __future__ import annotations

from tyrex_pm.facts import ids as F
from tyrex_pm.facts.mappings import (
    ASK70_INPUT_CONTRACT,
    Q_EDGE_INPUT_CONTRACT,
    Z_GAP_INPUT_CONTRACT,
)
from tyrex_pm.runtime.composition import adapters_to_start, missing_live_adapters, starts_adapter
from tyrex_pm.runtime.scheduler import EvaluationScheduler
from tyrex_pm.strategies.registry import get_strategy_plugin, registered_strategy_kinds


def test_ask70_does_not_depend_on_binance_or_chainlink() -> None:
    sources = ASK70_INPUT_CONTRACT.source_facts()
    assert F.POLYMARKET_BOOKS in sources
    assert F.BINANCE_SPOT_TRADES not in sources
    assert F.CHAINLINK_TWAP not in sources
    assert F.BINANCE_SPOT_L2 not in sources
    assert not starts_adapter(ASK70_INPUT_CONTRACT, F.BINANCE_SPOT_TRADES)
    assert starts_adapter(ASK70_INPUT_CONTRACT, F.POLYMARKET_BOOKS)


def test_z_gap_starts_spot_trades_and_chainlink() -> None:
    sources = Z_GAP_INPUT_CONTRACT.source_facts()
    assert F.BINANCE_SPOT_TRADES in sources
    assert F.CHAINLINK_TWAP in sources
    assert F.POLYMARKET_BOOKS in sources
    assert F.BINANCE_SPOT_L2 not in sources
    assert F.BINANCE_PERP_L2 not in sources


def test_q_edge_guide_closure_includes_l2_and_perp_without_a_plugin() -> None:
    sources = adapters_to_start(Q_EDGE_INPUT_CONTRACT)
    assert F.BINANCE_SPOT_L2 in sources
    assert F.BINANCE_PERP_TRADES in sources
    assert F.BINANCE_PERP_L2 in sources
    assert F.BINANCE_PERP_FUNDING in sources
    missing = missing_live_adapters(Q_EDGE_INPUT_CONTRACT)
    assert F.BINANCE_SPOT_L2 in missing
    assert "q_edge" not in registered_strategy_kinds()


def test_ask70_scheduler_wakes_on_books_not_binance() -> None:
    scheduler = EvaluationScheduler(ASK70_INPUT_CONTRACT, default_coalesce_s=0.0)
    assert scheduler.should_evaluate(F.POLYMARKET_BOOKS, mono_s=1.0)
    assert not scheduler.should_evaluate(F.BINANCE_SPOT_TRADES, mono_s=1.0)
    scheduler.mark_evaluated(mono_s=1.0)
    assert scheduler.should_evaluate_timer(mono_s=2.0)


def test_z_gap_scheduler_requires_ptb_before_binance_wake() -> None:
    ready = {"ptb": False}

    def derived_ready(fact: str) -> bool:
        return fact != F.PTB_SEALED or ready["ptb"]

    scheduler = EvaluationScheduler(
        Z_GAP_INPUT_CONTRACT,
        derived_ready=derived_ready,
        default_coalesce_s=0.0,
    )
    assert not scheduler.should_evaluate(F.BINANCE_SPOT_TRADES, mono_s=1.0)
    ready["ptb"] = True
    assert scheduler.should_evaluate(F.BINANCE_SPOT_TRADES, mono_s=1.0)
    assert not scheduler.should_evaluate(F.POLYMARKET_BOOKS, mono_s=1.0)


def test_plugins_expose_reference_contracts() -> None:
    ask70 = get_strategy_plugin("ask70")
    z_gap = get_strategy_plugin("z_gap")
    assert ask70.input_contract is ASK70_INPUT_CONTRACT
    assert z_gap.input_contract is Z_GAP_INPUT_CONTRACT
    assert ask70.requires_protection is True
    assert z_gap.requires_protection is False
