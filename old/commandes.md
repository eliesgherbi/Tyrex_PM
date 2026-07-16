python -m tyrex_pm.runtime.app run \
    --strategy config/strategies/sell_test.yaml \
    --scenario live_guru \
    --run-name sell_test_live_$(date +%s) \
    --max-iterations 60


python -m tyrex_pm.runtime.app run   --strategy config/strategies/sell_test.yaml   --scenario live_guru   --run-name "sell_test_live_ordering_check"