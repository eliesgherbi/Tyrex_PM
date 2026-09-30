# BTC five-minute strategy research: settlement value, execution, and a $3 target

Research date: 28 September 2026. Status: research proposal, not a validated profitable strategy. This review used primary papers, current provider documentation, a recent BTC five-minute rules page, and selected local implementation files. No historical strategy backtest, authenticated feed connection, or live trading was performed. Numerical examples are hypothetical.

## Research decision

The first candidate to test is a **settlement-aware probability model with selective, cost-aware entries and a hard exposure cap**. Compare it against the market's own probability and the existing Z-Gap baseline. Add passive execution only after measuring fill-conditioned adverse selection. Keep complete-set arbitrage as a separate opportunity detector. Do not require entry into every market.

The economic hypothesis is that some executable prices may underreact to the information already embedded in the settlement average, remaining uncertainty, and external price discovery. This is a conjecture. A mathematically correct forecast model can still have no advantage over the market.

There are three separate objectives:

1. $3 realized on every market: not supported by the research; ordinary statistical trading has losing outcomes.
2. $3 expected net profit per selected, filled trade: a sizing and opportunity-selection target, conditional on a valid edge estimate.
3. $3 average per scheduled market: includes skips, failed orders, and losing trades. With one possible episode in each five-minute window, there are 288 windows per day, making this an $864/day average objective before any unallocated operating costs. That arithmetic is not a forecast.

With selection rate r, fill rate h conditional on selection, and average net profit g conditional on a fill, the simplified average per scheduled market is r*h*g. Therefore, a $3 selected-fill target does not imply a $3 scheduled-market average. Multiple fills in one market share its outcome; count the full market episode for performance and inference.

## Current mechanics change the model

The September 22 BTC five-minute market inspected for this review refers to a Chainlink BTC/USD **60-second TWAP** stream. It should not be modeled automatically as the last Binance tick at expiry. The exact opening reference, closing report boundary, weighting, precision, and equality rule must be attested per contract. The current market at research time was not successfully retrieved; the inspected rules are a recent example, not a live contract attestation. [Recent market rules](https://polymarket.com/event/btc-updown-5m-1790074800?outcomeIndex=0)

Current PolyBolt documentation identifies a 60-second Chainlink TWAP channel. The migration guide distinguishes this from its Pyth spot-price channel. A similarly named BTC feed is not necessarily the settlement source. A delivered TWAP value alone does not reveal every underlying sample needed to reconstruct a future overlapping average. [PolyBolt](https://docs.polymarket.com/api-reference/live-data/overview), [migration semantics](https://docs.polymarket.com/migrate/rtds-to-polybolt)

The documented crypto taker fee is q*r*a*(1-a), with r=0.07 in the current category table; makers pay no platform trading fee. Read actual market parameters, include any applicable builder costs, and reconcile filled fees. A 50-cent taker purchase costs 1.75 cents per share in platform fees alone. Exiting by sale adds another fee when applicable. Do not subtract a spread again when an executable ask/bid already includes it. [Fees](https://docs.polymarket.com/trading/fees)

Selected crypto/finance markets have a documented 250 ms taker hold, identified by `itode` in market metadata. Orders cannot be canceled during that hold. The total information-to-fill delay also includes feed delivery, computation, network travel, and matching. Model conditions at execution, not an instantaneous fill at the signal quote. [Order lifecycle](https://docs.polymarket.com/concepts/order-lifecycle)

## What the literature contributes

### Foundational work

- **Bachelier (1900), Theorie de la speculation.** Diffusion offers a tractable model of price uncertainty. Use it as a transparent null model, not proof that BTC follows a Gaussian process. [Original paper](https://archive.numdam.org/articles/10.24033/asens.476/)
- **Geman and Yor (1993), Bessel Processes, Asian Options, and Perpetuities.** The average of a price path is a distinct payoff object. This motivates retaining accumulated-average state. Their geometric-Brownian results are not a ready-made calibrated PM trading signal. [Paper](https://onlinelibrary.wiley.com/doi/pdf/10.1111/j.1467-9965.1993.tb00092.x)
- **Cont, Kukanov and Stoikov (2014), The Price Impact of Order Book Events.** Order-flow imbalance and depth explain short-horizon price changes in their equity sample. Contemporaneous explanatory power is not automatically an advance forecast. [Paper](https://arxiv.org/abs/1011.6402)
- **Gould and Bonart (2015 preprint; 2016 publication), Queue Imbalance as a One-Tick-Ahead Price Predictor.** Useful justification for testing queue imbalance as a short-horizon feature. Their Nasdaq evidence does not establish five-minute binary-outcome alpha. [Paper](https://arxiv.org/abs/1512.03492)
- **Avellaneda and Stoikov (2008), High-frequency Trading in a Limit Order Book.** Quotes should respond to inventory and execution probabilities. Use the control framework, but rederive terminal value for a bounded binary payoff and account for informative fills. [Author-hosted paper](https://math.nyu.edu/inmemoriam/avellaneda/HighFrequencyTrading.pdf)
- **Kelly (1956), A New Interpretation of Information Rate.** Position sizing converts informational advantage into long-run growth. It does not create an advantage. [Original publication](https://onlinelibrary.wiley.com/doi/abs/10.1002/j.1538-7305.1956.tb03809.x)
- **Busseti, Ryu and Boyd (2016), Risk-Constrained Kelly Gambling; Sun and Boyd (2018), Distributional Robust Kelly Gambling.** Optimize growth subject to risk limits and uncertainty about outcome probabilities. Their guarantees depend on the assumed return process and uncertainty set. [Risk-constrained Kelly](https://web.stanford.edu/~boyd/papers/kelly.html), [robust Kelly](https://stanford.edu/~boyd/papers/robust_kelly.html)
- **Gneiting and Raftery (2007), Strictly Proper Scoring Rules.** Judge probabilities with log loss/Brier score and calibration, not winner-picking accuracy alone. [Paper](https://sites.stat.washington.edu/people/raftery/Research/PDF/Gneiting2007jasa.pdf)
- **Bailey, Borwein, Lopez de Prado and Zhu, The Probability of Backtest Overfitting (2015 manuscript).** Repeated strategy searches manufacture apparent success. Keep a trial register, chronological tests, and an untouched final evaluation. [Author-hosted manuscript](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)

### Recent and market-specific evidence

- **Albers et al. (2021), Fragmentation, Price Formation, and Cross-Impact in Bitcoin Markets.** External venue information can matter over subsecond horizons; fee regimes and execution determine whether predictability becomes profit. Test present-day leading venues and measured latency rather than importing their coefficients. [Paper](https://arxiv.org/abs/2108.09750)
- **Young (2026), OpenMarket.** A BTC 15-minute Binance/PM study reports that its walk-forward model underperforms the PM midpoint baseline and simulated trading loses after its stated costs. Its archive predates the current TWAP mechanics. Useful negative evidence and data methodology, not a five-minute profitability verdict. Its symmetric nearest-neighbor alignment table is unsuitable as a causal trading join without rebuilding by information availability. [Paper](https://arxiv.org/html/2607.26245v1)
- **Dubach (2026), The Anatomy of a Decentralized Prediction Market, v2.** Feed-inferred trade direction can disagree materially with on-chain labels. Do not build PM signed-flow alpha from an unverified trade-direction heuristic. On-chain labels available later can audit training labels; they cannot be injected into an earlier trading decision. Use v2: it narrows earlier claims about time-to-close depth effects. [Revised paper](https://arxiv.org/abs/2604.24366v2)
- **Dai, Jia and Yu (2026), Settlement Manipulation in Prediction Markets.** The working paper reports settlement-time flow/reversal patterns in an early BTC five-minute sample ending April 8, 2026. It motivates stress-testing boundary regimes; it does not establish a profitable present-day reversal strategy, especially after settlement-reference changes. [Paper](https://arxiv.org/html/2606.31675v1)
- **Saguillo et al. (2025), Unravelling the Probabilistic Forest.** Historical evidence that logical price inconsistencies have been exploited on PM. Their results precede BTC five-minute trading and do not establish current opportunity frequency or executable capacity. [Paper](https://arxiv.org/abs/2508.03474)
- **Portnaya (2026), Do Prediction Markets Match Option Prices?** Longer-horizon BTC threshold contracts exhibit differences from option-implied valuations in a small historical sample. Use as a relative-value research direction; neither the horizon nor the settlement payoff matches the current five-minute TWAP contract. Risk-neutral probabilities also differ conceptually from realized outcome probabilities. [Paper](https://arxiv.org/abs/2606.19517)
- **Bacry, Mastromatteo and Muzy (2015), Hawkes Processes in Finance.** Self-exciting event models offer a principled representation of clustered order arrivals. Reserve this complexity for a demonstrated failure of simpler flow and volatility features. [Paper](https://arxiv.org/abs/1502.04592)

The recent PM papers are preprints/working papers unless separately verified otherwise. Their results have not been independently reproduced in this session. Literature selection intentionally includes negative evidence and limitations of transferring findings across assets, horizons, and rule regimes.

## Candidate ranking and falsification

**First: settlement-aware value trading.** Estimate the physical probability of the official outcome; buy a side only if its conservative value exceeds the executable all-in purchase cost. Start with a single entry and hold to settlement. Reject if out-of-sample selected-trade performance fails after costs or if the model offers no economic improvement over a market-only baseline.

**Second: informed passive quoting.** Place limited maker orders around the same fair-value estimate; monitor inventory and rapidly changing information. A zero maker fee is not a profit mechanism. Reject if filled orders have negative subsequent markouts or adverse selection exceeds spread capture. Evaluate all attempts, unfilled quotes, forced hedges, and cancellations.

**Separate overlay: complete-set arbitrage.** Buy equal net shares only when their executable total acquisition cost is below $1. Merge matched inventory when appropriate. Paired execution and settlement status remain necessary controls. Do not budget a recurring $3 unless measured size and frequency support it. [Token mechanics](https://docs.polymarket.com/concepts/positions-tokens), [independent batch order results](https://docs.polymarket.com/trading/place-orders)

**Lower priority: cross-venue lag scalping.** Require measured edge persistence beyond the actual taker delay and latency distribution. Reject if profitability vanishes with delayed arrival or canceled quotes.

**Exploratory: boundary-regime effects, options benchmarks, Hawkes models.** Test only after the basic forecast and execution pipeline works. No manipulation, no extrapolation of an old settlement effect to a changed contract, and no assumption that a forecasting improvement pays the spread.

## Proposed model

### 1. Define the payoff before the price model

Let K be the confirmed opening threshold, T the close boundary, R_T the official closing reference, and Y=1{R_T >= K}. The forecast is p_t=P(Y=1 | information actually received by t). Down has probability 1-p_t, assuming the verified contract has exactly these complementary outcomes.

Freeze K from an authoritative or explicitly validated observation. If K remains uncertain, model its joint uncertainty with R_T rather than substituting an unverified first tick. Preserve decimal precision and tie treatment; a continuous model's zero tie probability does not justify dropping ties from actual labels.

### 2. Transparent endpoint null model

For an illustrative local arithmetic diffusion dS=mu*dt+sigma*dW, with sigma measured in dollars per square-root second:

    p_endpoint = Phi((S_t - K + mu*h)/(sigma*sqrt(h))), h=T-t.

This is a physical forecast under specified mu and sigma, not automatically a risk-neutral no-arbitrage price. Begin with mu=0 and estimate volatility causally. Distinguish this dollar-volatility formulation from the current code's log-return-volatility units.

### 3. Model a terminal average with its memory

Assume for this analytical baseline that the verified reference is the uniformly weighted arithmetic average over [T-L,T]. L=60 seconds is suggested by the reviewed feed, but boundaries and weights must be verified. Do not assume a five-minute full-window average merely from the market title.

Within the averaging interval, let h=T-t and I_t=integral from T-L to t of S_u du. Then:

    R_T = (I_t + integral from t to T of S_u du)/L
    E[R_T | F_t] = (I_t + h*S_t + mu*h^2/2)/L
    Var[R_T | F_t] = sigma^2*h^3/(3*L^2)
    p_average = Phi((E[R_T | F_t]-K)/sqrt(Var[R_T | F_t])).

Derivation: write the future random integral as sigma*integral_0^h (h-v)dW_v, then apply Ito isometry. The variance is sigma^2*integral_0^h(h-v)^2 dv / L^2. This was checked against numerical integration at four remaining horizons; that verifies the algebra, not the BTC model.

Before the averaging interval begins, h>=L:

    E[R_T | F_t] = S_t + mu*(h-L/2)
    Var[R_T | F_t] = sigma^2*(h-2L/3).

Both variance branches agree at h=L. Their uncertainty differs materially from endpoint uncertainty. With 30 seconds left in a 60-second average, the ideal average standard deviation is about 0.289 times endpoint standard deviation, conditional on perfectly observed average history. This is an uncertainty result, not a trading edge.

The diffusion/physics connection can also be expressed through a backward equation in price s and accumulated integral i:

    u_t + mu*u_s + (sigma^2/2)*u_ss + s*u_i = 0,
    u(T,s,i) = 1{i/L >= K}.

Diffusion spreads uncertainty; the integral records the path already observed. This is a concrete use of physics-inspired mathematics. It does not establish profitable mispricing.

For production research, use a state-space model or conditional path simulation to include unobserved reference samples, feed latency, cross-venue basis, jumps, changing volatility, and uncertainty in averaging boundaries. A naive independent noise term can understate correlated oracle/basis error. Calibrate the complete predictive distribution to official resolutions.

### 4. Ask whether the model adds to the market

Train a small regularized model, for example:

    logit(p_hat) = logit(m_PM)
                  + beta0
                  + beta1*(logit(p_average)-logit(m_PM))
                  + beta2*normalized_Binance_OFI
                  + beta3*reference_basis
                  + selected interactions with time and volatility.

Here m_PM is a contemporaneous, non-executable market-probability baseline; clip probabilities only for numerical logit stability. Start with beta=0 as the market-only null. Compare endpoint-only, average-only, market-only, market-plus-average, then market-plus-average-plus-flow models. Feature lookbacks and transformations use only prior observations.

Do not infer that beta1 is nonzero or profitable. Use chronological training/calibration/evaluation. Estimate a probability uncertainty interval [p_minus,p_plus] from held-out calibration, block resampling, and regime sensitivity. Calling p_hat minus an arbitrary buffer a 95% lower confidence bound would be unjustified.

### 5. Execution-conditioned edge

For a chosen side and size q, let C(q) include the filled book levels, fee schedule, residual slippage allowance, and any fixed costs. For a hold-to-settlement purchase, the point-model expected profit is q*p-C(q). Use p_minus for Up and 1-p_plus for Down when applying a conservative bound.

For maker orders or selective fills, use E[Y | order fills, state, action], not simply the unconditional pre-order p. The attempt-level expected payoff includes no-fill outcomes and all costs. More generally evaluate E[Q*Y - C | observed state, action], with random fill quantity Q and cost C, under the information-to-fill latency distribution.

The signal cannot use a quote the trader would not see until later. A backtest must move forward to order arrival, apply any venue hold, then determine matching, depth, fills, and failures.

## Sizing and the $3 objective

For flat all-in unit cost c in (0,1), conservative win probability p_minus, and q shares:

    edge_per_share e = p_minus-c
    conservative_expected_profit = q*e
    win_profit = q*(1-c)
    loss_profit = -q*c.

A $3 model-expected target requires q_target=3/e, rounded up to allowed lot size, provided e>0. It is permissible only if that size fits both depth and the risk budget. If the maximum acceptable full-position loss is Lmax, then q<=Lmax/c. Ignoring lot rounding, feasibility requires:

    e/c >= 3/Lmax.

This is a useful engineering constraint: small edge requires more loss capacity. Fixed costs and increasing depth costs must be handled in the full C(q), not hidden in a constant c.

Illustration: ask $0.50, platform fee $0.0175, and an assumed $0.0025 extra allowance give c=$0.52. If a defensible conservative probability is 0.56, e=$0.04. Buying 75 shares gives $3 expected profit at that probability, a $36 winning outcome and a $39 losing outcome. If the true probability equals 0.56, losses occur 44% of the time. Nothing in this example says such an opportunity exists.

For bankroll B and a single binary position, maximize:

    min over p in [p_minus,p_plus]
      p*log(1+q*(1-c)/B) + (1-p)*log(1-q*c/B)

subject to q>=0, q*c<=Lmax, depth/lot constraints, and strictly positive remaining wealth. For a long position the inner worst case is p_minus. Without other constraints and with constant c, the full Kelly solution is:

    q_K = max(0, B*(p_minus-c)/(c*(1-c))).

A fractional Kelly factor and independent hard risk limits can reduce this. They are risk controls, not proof of model validity. Do not increase q above the chosen risk size merely to force $3. If q_safe*e<3, skip under a strict $3 selected-trade objective, or explicitly choose a lower target.

For dynamic maker/taker inventory, formulate a finite-horizon control problem with state (time, reference distribution, PM books, inventory, cash, pending orders). A distributionally robust expected-log objective or expected profit minus CVaR of losses is suitable. CVaR_alpha(L)=min_eta[eta+E[(L-eta)+]/(1-alpha)] measures average tail loss. Fixed-scenario position sizing is tractable; optimizing a learned execution policy is not automatically convex.

## Entry and exit policy to test first

1. Reject a market with uncertain rules/K, invalid books, missing reference coverage, or incompatible fee/latency metadata.
2. Estimate p and its uncertainty for the official outcome, conditional on the expected execution event.
3. Price both sides through executable depth. Buy the eligible side with the best conservative risk-adjusted value, otherwise do nothing.
4. Allow at most one initial position per market in the first experiment. Size for the possibility of total stake loss; do not rely on an SL to cap it.
5. Use hold-to-settlement as the first economic benchmark. Then test a separately specified sell-versus-hold policy and charge its costs.
6. An early exit should compare net executable sale value with future holding value and risk. A fixed $3 take-profit is not evidence of optimal stopping. A Bellman policy can eventually model the option to wait and sell later; a current bid-versus-p comparison alone is only a simplified benchmark.
7. Predefine behavior for feed failure and order reconciliation. Emergency liquidation is a rule to attempt execution, not a guaranteed fill or price.

## Validation that can reject the proposal

Collect original PM books for both tokens, external trades/depth, settlement-reference reports, fee/tick/minimum-size/delay metadata, official K evidence, outcomes, and local arrival timestamps. Keep source and receive times; separate feed versions and settlement regimes. Do not relabel old spot-settled markets as historical current-TWAP executions.

Run a small preregistered set of hypotheses. Split chronologically by whole market and day. Purge overlapping information/label intervals; fit calibration only on prior data. Randomly splitting neighboring ticks creates leakage. A million ticks from a few markets is not a million independent outcome observations.

Required comparisons: no trade; market-only probability; existing endpoint Z-Gap; average model; market-plus-average; final flow extension. Keep the order policy, risk limits, and fee assumptions identical when comparing forecast models.

Replay delayed order arrival and venue holds, book depth, partial/no fills, cancellation races, and confirmed versus failed settlement. Maker fills need conservative queue assumptions: touching a quote is not a fill. An L2 archive does not reveal exact order-level queue position. Audit signed PM flow against authoritative fills without leaking delayed labels into live features.

Report mean net profit per selected attempt, per filled episode, and per scheduled market; selection/fill rates; full outcome distribution; drawdown; capital occupancy; calibration; after-fill markouts; latency percentiles; and performance by probability, volatility, time-to-close, and rule regime. Include operating costs in the final business calculation.

Use day-block resampling and an untouched final test for uncertainty. Report a one-sided lower confidence bound for mean net profit. A bound above zero supports an edge; a bound above $3 would support the stronger average target at the evaluated scale, subject to model/regime stability. Correct for the model/threshold search and avoid repeatedly peeking at the test until it passes.

Stress larger delays, less favorable depth, extra ticks of slippage, reference mismatch, and calibration shifts. A strategy supported only by immediate fills or a single tuned period fails. A high win rate without positive net expectancy fails. Scaling that erases the edge fails.

Shadow operation validates data and decision timing; it cannot fully validate real maker fills or market impact. Any later small live execution experiment needs an explicit risk allocation and authorization. This research turn does not run one.

## Fit with the existing project

The inspected code already separates model probability, entry costs, and liquidation value in `src/tyrex_pm/strategies/z_gap/valuations.py`, and has calibration-row infrastructure in `calibration.py`. Reuse these boundaries.

`src/tyrex_pm/indicators/binary_fair_value.py` currently computes Phi(log(S/K)/(sigma*sqrt(tau_eff))). Its inputs do not carry the accumulated averaging integral or a distribution over it. Treat that as an endpoint-style baseline and audit its applicability to the actual reference before extending it. This is a modeling concern, not a claim that a live result has been diagnosed.

`src/tyrex_pm/lab/evaluate_replay.py` describes deterministic fixture replay. It is not evidence of a historical, queue-aware execution backtest. The existing warehouse document explicitly labels the collector as planned, not implemented. Recheck these boundaries when implementation begins.

Implementation order:

1. Attest the current contract and feed boundaries; assemble an outcome-labeled, causally joined dataset.
2. Implement an offline average-aware probability benchmark and its mathematical checks.
3. Compare forecast calibration and execution economics with market-only and endpoint baselines.
4. Add robust sizing and target-feasibility checks after an edge is measured.
5. Evaluate a separate passive quoting extension only if its execution data supports it.

The research conclusion is a candidate and a falsification plan. There is currently no measured evidence in this work establishing $3 profit per trade or profitability in every BTC five-minute market.
