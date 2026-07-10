"""P4 allocation owner ids and intent source strings (no strategy imports)."""

from __future__ import annotations

OWNER_SELL_TEST = "sell_test"
OWNER_GURU_FOLLOW = "guru_follow"
OWNER_TP_SL_TEST = "tp_sl_test"
OWNER_SIMPLE_SIGNAL_TEST = "simple_signal_test"
OWNER_VALIDATION_HARNESS = "validation_harness"
OWNER_PAIRED_BINARY = "paired_binary"
OWNER_Z_GAP = "z_gap"
#: Owner id for production protection (TP/SL) exits when the protected owner is
#: not otherwise known (P4). Protection normally reuses the protected position's
#: owner id; this is the fallback bucket.
OWNER_PROTECTION = "protection"

SELL_TEST_INTENT_SOURCE = "sell_test_strategy"
SCHEDULED_EXIT_DEMO_SOURCE = "scheduled_exit_demo"
TP_SL_TEST_INTENT_SOURCE = "tp_sl_test_strategy"
SIMPLE_SIGNAL_TEST_INTENT_SOURCE = "simple_signal_test_strategy"
VALIDATION_HARNESS_INTENT_SOURCE = "validation_harness_strategy"
PAIRED_BINARY_INTENT_SOURCE = "paired_binary_strategy"
PROTECTION_INTENT_SOURCE = "protection_engine"

ALLOCATION_TEST_INTENT_SOURCE = "allocation_test_strategy"
DEFAULT_ALLOCATION_TEST_OWNER_A = "allocation_test_A"
DEFAULT_ALLOCATION_TEST_OWNER_B = "allocation_test_B"
