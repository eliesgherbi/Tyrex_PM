"""
Guru-follow live execution via **Nautilus framework** ``submit_order`` → ExecEngine →
``PolymarketExecutionClient`` (**Step 4** + **Step 5** dynamic instruments).

**Package-source-confirmed:** same pattern as ``scripts/spike_nautilus_polymarket_exec.py`` /
``order_factory.limit`` + ``submit_order(..., client_id=POLYMARKET_CLIENT_ID)``.

``OrderIntent`` is translated here — **not** in
:class:`~tyrex_pm.strategy.copy_strategy.CopyStrategy` (thin strategy invariant).

**Repo-confirmed optional map:** ``RuntimeSettings.polymarket_token_to_instrument`` from YAML
``polymarket_instrument_ids`` (secondary overlay when non-empty).

**Step 5:** ``GuruInstrumentDynamicController`` resolves ``token_id`` → ``BinaryOption`` and
activates into ``Cache`` (**primary** path when present). Static map is **secondary** after a
failed or partial dynamic attempt.
"""

from __future__ import annotations

import hashlib
import os
import re
from decimal import Decimal
from typing import TYPE_CHECKING

from nautilus_trader.adapters.polymarket import POLYMARKET_CLIENT_ID
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId

from tyrex_pm.config.loaders import RuntimeSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent

if TYPE_CHECKING:
    from nautilus_trader.trading.strategy import Strategy

    from tyrex_pm.runtime.guru_instrument_dynamic import GuruInstrumentDynamicController

_TAG_SAFE = re.compile(r"[^a-zA-Z0-9_.:-]+")


def _client_order_id_from_guru_correlation(correlation_id: str) -> ClientOrderId:
    """
    Deterministic, short ``ClientOrderId`` from guru ``source_trade_id`` (often a tx hash).

    **Package-source-confirmed:** ``ClientOrderId`` accepts alphanumeric strings;
    keep bounded length.
    """
    digest = hashlib.sha256(correlation_id.encode("utf-8", errors="replace")).hexdigest()[:26]
    return ClientOrderId(f"TX{digest}")


def _guru_tag(correlation_id: str) -> str:
    """Nautilus order tag for grep / ops (ASCII-safe, length-bounded)."""
    s = _TAG_SAFE.sub("_", correlation_id.strip())[:120]
    return f"guru_cid={s}"


class NautilusGuruExecutionPort:
    """
    Live guru execution through the **framework** path (visibility in ``Cache``).

    Uses dynamic resolution when a controller is wired; optional YAML token map as overlay.
    """

    __slots__ = ("_strategy", "_runtime", "_token_to_instrument", "_dynamic")

    def __init__(
        self,
        strategy: Strategy,
        runtime: RuntimeSettings,
        *,
        dynamic: GuruInstrumentDynamicController | None = None,
    ) -> None:
        self._strategy = strategy
        self._runtime = runtime
        self._token_to_instrument = dict(runtime.polymarket_token_to_instrument)
        self._dynamic = dynamic

    def submit_intent(self, intent: OrderIntent, *, mode: str) -> None:
        if mode != "live":
            return
        if intent.price_ref is None:
            self._strategy.log.warning(
                f"event={ReasonCode.LIVE_ORDER_ERROR} component=nautilus_guru_exec "
                f"correlation_id={intent.correlation_id} detail=missing_price",
            )
            return

        qty = float(intent.quantity)
        price = float(intent.price_ref)
        side_u = intent.side.upper()
        if side_u == "BUY":
            min_n = float(os.environ.get("TYREX_MIN_BUY_NOTIONAL_USD", "1"))
            if min_n > 0 and price * qty + 1e-9 < min_n:
                self._strategy.log.warning(
                    f"event={ReasonCode.LIVE_ORDER_ERROR} correlation_id={intent.correlation_id} "
                    f"est={price * qty} min={min_n} component=nautilus_guru_exec",
                )
                return

        tid = str(intent.token_id)
        instr_s = self._token_to_instrument.get(tid)
        instrument_id: InstrumentId | None = None
        inst = None
        dyn_fail: str | None = None

        if self._dynamic is not None:
            inst, dtag = self._dynamic.resolve_and_activate(tid)
            if inst is not None:
                instrument_id = inst.id
            else:
                dyn_fail = dtag

        if inst is None and instr_s is not None:
            instrument_id = InstrumentId.from_str(instr_s)
            inst = self._strategy.cache.instrument(instrument_id)

        if inst is None:
            if instr_s is not None:
                self._strategy.log.error(
                    f"event={ReasonCode.GURU_INSTRUMENT_NOT_IN_CACHE} "
                    f"component=nautilus_guru_exec correlation_id={intent.correlation_id} "
                    f"detail=instrument_not_in_cache instrument_id={instrument_id}",
                )
                return
            if self._dynamic is not None and dyn_fail is not None:
                rc = (
                    ReasonCode.GURU_DYNAMIC_ACTIVATION_CAP
                    if dyn_fail == "activation_cap"
                    else ReasonCode.GURU_DYNAMIC_RESOLVE_FAILED
                )
                self._strategy.log.error(
                    f"event={rc} component=nautilus_guru_exec correlation_id={intent.correlation_id} "
                    f"token_id={tid} detail=dynamic_path failure={dyn_fail}",
                )
                return
            self._strategy.log.error(
                f"event={ReasonCode.GURU_INSTRUMENT_UNMAPPED} component=nautilus_guru_exec "
                f"correlation_id={intent.correlation_id} detail=no_instrument_for_token "
                f"token_id={tid}",
            )
            return

        assert instrument_id is not None and inst is not None

        side = OrderSide.BUY if side_u == "BUY" else OrderSide.SELL
        coid = _client_order_id_from_guru_correlation(intent.correlation_id)
        # Tags: Phase B B3 **tier 1** guru detection in ``state_readers.is_guru_resting_order``;
        # ``ClientOrderId`` ``TX``+hex is **tier 3** fallback if tags are not on cache snapshots.
        order = self._strategy.order_factory.limit(
            instrument_id=instrument_id,
            order_side=side,
            quantity=inst.make_qty(Decimal(str(qty))),
            price=inst.make_price(Decimal(str(price))),
            time_in_force=TimeInForce.GTC,
            client_order_id=coid,
            tags=[_guru_tag(intent.correlation_id)],
        )
        self._strategy.submit_order(order, client_id=POLYMARKET_CLIENT_ID)
        self._strategy.log.info(
            f"event={ReasonCode.LIVE_ORDER_SUBMIT} component=nautilus_guru_exec "
            f"correlation_id={intent.correlation_id} client_order_id={order.client_order_id} "
            f"instrument_id={instrument_id} side={side_u} qty={qty} price={price}",
        )
