# Review of the proposed BTC five-minute residual-band strategy

Date: 29 September 2026. Scope: source verification, mathematical review, and comparison with the local implementation. This is a research assessment, not a backtest or evidence of profitability. Numerical examples are calculations under stated assumptions. No trading configuration was changed.

Companion: [Settlement-aware strategy analysis](btc5m_settlement_aware_strategy_analysis_2026-09-29.md). Earlier literature review: [28 September research](btc5m_strategy_research_2026-09-28.md).

## Assessment

The strongest part of your research is its economic discipline: transaction costs matter, position sizing cannot manufacture an edge, buying both sides is useful only at the right combined cost, and trading every window is an unsuitable requirement for a selective strategy.

I would retain the fair-value/execution/inventory decomposition. I would not yet implement the proposed OU bands, declare maker trading profitable, or raise the budget to $100. Several assumptions are presented as established results, and the settlement specification may invalidate the endpoint model at the center of the proposal.

The revised research question should be: **does information available to us predict the official payoff or an executable price change better than the market, by enough to pay all costs, at sizes and delays we can actually execute?**

## 1. The opening claim needs an expected-value qualification

A fairly priced contract can produce a $3 realized winning trade. What it cannot provide to an uninformed taker is positive expected net profit at a fair executable price with positive fees.

Let p be the physical probability of winning, a the purchase price, and f(a) the fee per share. Holding N shares to settlement gives

\[
\Pi=N[Y-a-f(a)],\qquad E[\Pi]=N[p-a-f(a)],\quad Y\in\{0,1\}.
\]

If p=a, the expectation is negative although the winning realization is positive. Distinguish realized profit, expected profit conditional on selection and fill, and average profit across all scheduled markets.

Neither maker status nor a submitted pair of orders guarantees $3 in every window. A completed equal-share pair below $1 has a contractual payoff advantage; an unfilled or partially filled pair does not.

## 2. The empirical sources support caution, with narrower scopes

**Vultax: confirmed figures, overstated sample specificity.** The 2,497 observations combine Bitcoin and Ether and five- and fifteen-minute markets. They are not 2,497 BTC five-minute windows. The article reports Up at 50.2%, midpoint-time favourites winning 74.6% at an average 75.0-cent price, and near-close favourites winning 79.0% at 79.0 cents. This is useful aggregate calibration evidence. It does not test first-touch ask70 entries, their stopping rule, or a conditional subgroup selected using additional information. [Original research](https://vultax.com/research/polymarket-bitcoin-up-or-down-5-minute-markets)

**Fierdimo: useful negative evidence, not a proof of market-wide impossibility.** The repository reports 407 oracle-observed windows and a 205-window held-out half; its book baseline beats the tested oracle models. It explicitly acknowledges approximating the Down book as the complement of Up and discarding unrealistic maker simulations. Its measured lead from Bybit to Chainlink did not add predictive value in that experiment. This argues against equating an observable feed lead with monetizable alpha. It does not establish that making markets is profitable. I inspected the public report, not a reproduction of its dataset and code. [Research repository](https://github.com/Fierdimo/polymarket-analysis)

**Semenas: bibliographic correction and verification limit.** The indexed SSRN record identifies a July 17, 2026 paper studying June 22–23, with 182 contracts and 67.6% wins versus a 63% average entry price. June describes the sample, not the paper date. The full document could not be retrieved in this session, so I have not independently verified its significance tests or cost treatment. A 4.6-point realized difference is not a defensible estimate of future net edge. [SSRN record](https://ssrn.com/abstract=7134801)

These studies are separate sources; independence of their datasets and errors has not been established here. None was replicated in this review.

## 3. Settlement comes before the probability formula

The September 22 BTC five-minute rules inspected for this review name the Chainlink BTC/USD 60-second TWAP stream. The page's generic FAQ uses endpoint language, illustrating why the actual Rules section and settlement reports must take precedence. The exact averaging interval, opening reference, closing report boundary, precision, and equality convention still need contract-specific verification. [Inspected rules](https://polymarket.com/event/btc-updown-5m-1790074800?outcomeIndex=0)

Your final checklist correctly raises the endpoint-versus-average issue. It needs to become the first gate, before fitting volatility, residual bands, or gamma thresholds.

For an endpoint geometric diffusion under the physical measure,

\[
p=P(S_T\ge K\mid\mathcal F_t)
=\Phi\left(\frac{\log(S_t/K)+(\mu-\sigma^2/2)\tau}{\sigma\sqrt\tau}\right).
\]

Replacing physical drift by the appropriate pricing drift gives an option-pricing benchmark, not automatically a forecast of actual outcomes. A short-horizon zero-log-drift approximation is reasonable to test. It is not exactly the Bachelier formula: an arithmetic diffusion uses dollar volatility and S-K rather than log(S/K).

The omitted diffusion correction has magnitude 0.5*sigma*sqrt(tau). At 80% annual volatility and 300 seconds, using 365-day years, this shifts z by about 0.00123. Usually small, but the assertion that it is always below 0.001 is too strong.

If the settlement variable is an average, its already-observed history belongs in the state. Adding a Binance–Chainlink basis buffer to an endpoint formula does not repair that missing state. The companion analysis derives a transparent averaging model, conditional on the reference really matching that averaging functional.

## 4. Basis correction is sensible, but its distribution must be identified

Your formula

\[
\Phi\left(\frac{\log(S/K)-b}{\sqrt{\sigma^2\tau+v}}\right)
\]

can be a useful approximation if b is the conditional mean log difference between the exchange and reference price, v is the relevant prediction-error variance, and the errors are jointly Gaussian with the required independence assumptions.

For future exchange log return R and basis B, the uncertainty in R-B is

\[
\operatorname{Var}(R-B)=\operatorname{Var}(R)+\operatorname{Var}(B)
-2\operatorname{Cov}(R,B).
\]

The covariance term and conditional mean dynamics cannot generally be omitted. A delayed report, a smoothed reference, an exchange premium, and parameter-estimation uncertainty are different effects. Record source timestamps and receive timestamps and model those effects explicitly.

The ATM volatility observation is correct for the symmetric zero-drift endpoint approximation: p=0.5 when S=K, and dp/dsigma=-z*phi(z)/sigma. It is not a universal claim about the settlement contract. Under averaging, accumulated history can imply a probability far from 0.5 even when current spot equals K. The proposed |z|>0.8 gate is a tunable hypothesis, not a consequence of volatility blindness.

## 5. The classical papers provide frameworks, not fitted PM models

**HAR-RV.** Corsi's daily/weekly/monthly construction is a valuable benchmark. The original evidence does not establish that it is the right replacement for EWMA on a remaining five-minute horizon. Compare a suitably adapted model against EWMA and simpler intraday variance estimates out of sample, using the uncertainty of the actual settlement functional as the target. [Corsi, original paper](https://statmath.wu.ac.at/~hauser/LVs/FinEtricsQF/References/Corsi2009JFinEtrics_LMmodelRealizedVola.pdf)

**0DTE expansions.** The cited SSRN 4503344 is Bandi, Fusari and Renò's *0DTE Option Pricing*. Its abstract reports pricing and hedging improvements using skewness and kurtosis adjustments. It does not support dismissing those adjustments as economically irrelevant. Their importance for BTC five-minute binaries remains a separate empirical question; risk-neutral option fits do not directly deliver physical winning probabilities. Start simple, then test jump/tail sensitivity. [Paper record, revised March 2026](https://ssrn.com/abstract=4503344)

**Hawkes processes.** Clustered arrivals motivate self-exciting models, but do not establish that this particular book follows a Hawkes process. Compare that hypothesis against simpler event-rate and recent-jump features. A cancellation after a price jump can arrive after the adverse fill; model the complete cancellation race. [Bacry, Mastromatteo and Muzy](https://arxiv.org/abs/1502.04592)

## 6. OU mean reversion does not make the residual a tradable asset

Write the residual as r=m-p*, with m a book price estimate. Then

\[
\Delta m=\Delta r+\Delta p^*.
\]

Suppose m=0.55 and p*=0.60. You buy because the residual is -0.05. Later m=0.50 and p*=0.50: the residual has reverted perfectly to zero, but the asset you bought fell five cents before transaction costs. Convergence can occur through your model changing its mind.

Bertram provides analytic first-passage results for an assumed OU traded process. That is a useful optimization reference, not proof that a bounded, expiring model-price residual follows a stationary OU process or can be traded without hedging its moving fair value. Finite expiry, price-dependent fees, discrete ticks, and informative fills also change the control problem. [Bertram](https://ssrn.com/abstract=1505073)

Test residual dynamics by time remaining and regime, but evaluate executable P&L separately. Measure whether apparent convergence comes from movement of the book or the model. Include all estimated hedging costs if using an external hedge. Cummins' empirical examination of the Bertram model is especially relevant: Gaussian OU misspecification overstated returns and understated cycle times in that equity sample. [Cummins](https://strathprints.strath.ac.uk/82360/)

Also distinguish a simple depth-weighted midpoint from a calibrated conditional future-price estimator. Neither is the price at which the whole intended order can execute. [Stoikov's micro-price presentation](https://www.ma.imperial.ac.uk/~ajacquie/Gatheral60/Slides/Gatheral60%20-%20Stoikov.pdf)

## 7. The local sensitivity calculation is useful; the control rule is a heuristic

For the stated endpoint approximation, delta=phi(z)/(S*sigma*sqrt(tau)). With diffusion dS approximately S*sigma*dW, the local probability standard deviation over dt is approximately

\[
\phi(z)\sqrt{dt/\tau}.
\]

At z=0, this is 12.6 cents for dt=1 and tau=10 seconds, or 3.0 cents at tau=180 seconds. Those are local diffusion approximations, not exact one-second distributions, and need rederivation for an averaged payoff. The displayed calculation concerns delta-driven probability variance; calling all of it digital gamma obscures the distinction.

Avellaneda–Stoikov motivates inventory-sensitive quotes. Substituting a binary probability into its familiar reservation-price expression is a starting heuristic. Current instantaneous variance times remaining time need not equal remaining payoff variance. Under a correctly specified conditional-probability martingale, the remaining Bernoulli variance is p(1-p). A finite-horizon binary inventory controller should reflect this terminal payoff and adverse selection. [Avellaneda–Stoikov](https://math.nyu.edu/inmemoriam/avellaneda/HighFrequencyTrading.pdf)

The proposed 45-second exclusion, |z|<1, and 90% override have no verified optimality here. Treat them as preregistered candidate rules and compare them against alternatives without mining the final test sample.

## 8. Maker economics must be conditioned on getting filled

The official category schedule supports f(a)=0.07*a*(1-a) per share, or N*f(a) total, and zero platform maker trading fees. The 20% figure describes the crypto maker rebate allocation, not a guaranteed personal return. Use actual market parameters and confirmed eligibility; set rebates to zero in the base case. [Fee documentation](https://docs.polymarket.com/trading/fees)

A quote one cent below your unconditional fair value can still lose money: the circumstances causing someone to sell to you may reduce the true probability by more than one cent. The necessary quantity is

\[
E[Y\mid\text{our order fills, pre-trade information}]-\text{all-in cost}.
\]

Likewise, half-spread plus fee is a midpoint-based approximation. When computing value minus executable ask, the spread is already included. A micro-price residual requires an additional adjustment between micro-price and the executable price, not automatically half the spread.

For a conservative Up probability interval [p-,p+], use separate side tests:

\[
e_U=p^- - c_U,\qquad e_D=(1-p^+) - c_D.
\]

Costs c must reflect depth, latency, and the chosen exit policy. Low fees near 90 cents do not prove that only late favourites can be viable taker trades.

For pairs, N matched shares acquired at combined cost 0.97 lock 0.03*N before additional costs. But two submitted bids are contingent executions. Include orphan losses and no-fills in results per attempted episode. A completion cap can prevent further spending; it cannot guarantee that the orphan can be flattened cheaply.

## 9. Correct the $3 sizing arithmetic

The identity N=3/e is correct when e is genuinely positive expected net profit per share and does not shrink at the requested size. It is not an optimal sizing rule on its own.

At a=0.70, f=0.0147 and all-in unit cost c=0.7147:

- A $5 pre-fee notional buys about 7.143 shares. A $3 expected-profit target would require 0.42 dollars of net edge per share, not 0.60. That is impossible here because even the maximum pre-fee unit gain is 0.30.
- With the actual $5 total-debit cap, N<=5/0.7147=6.996. Even a certain win produces only about $1.996 profit before other costs.
- At 100 shares and a genuine 3-cent net edge, expected profit is $3; the cost is $71.47, not $70 including fees. The required winning probability is 0.7447. Outcomes are +$28.53 or -$71.47, with a one-trade standard deviation of about $43.60 under that probability.
- Buying 50–100 shares one cent under fair value provides $0.50–$1 of expected value. A $3 capture needs 100 shares at three cents, or 50 shares at six cents, with the entire execution cycle accounted for.

Full Kelly at probability 0.96 and a fee-free 0.90 entry allocates 60% of bankroll to the wager, not literally the whole account. It is still very sensitive to estimation error. A fractional-Kelly limit and an explicit dollar-loss cap should constrain target sizing; the target must never override them.

## 10. The local baseline is more specific than the write-up assumes

The inspected `binary_fair_value.py` uses the stated endpoint-style log-ratio formula and has no accumulated-average input. Reusing the indicator without resolving settlement semantics is insufficient.

The existing Z-Gap valuation structures already separate executable asks, estimated buy fees, slippage, settlement edge, and repricing edge. This review is not a full implementation audit, but the proposed cost-awareness is partly present already; it should not be described as wholly missing.

The inspected ask70 profile has entry threshold 0.70, price padding up to 0.08, take-profit 0.85, stop 0.55, a one-second evaluation interval, $5 total-debit cap, and mandatory exit 90 seconds before expiry. Consequently, neither a literal 70-cent fill nor holding to settlement describes every baseline trade. Compare against the complete actual policy in an offline replay.

Files inspected: `src/tyrex_pm/indicators/binary_fair_value.py`, `src/tyrex_pm/strategies/z_gap/valuations.py`, and `config/runs/ask70_protection_tiny_live.yaml`. No live process was started or changed.

## Recommended revision

Retain the economic architecture; reorder the proof obligations:

1. Attest each contract's payoff and reproduce its reference and resolution from causally available data.
2. Test whether the probability model adds information beyond the market price on held-out windows.
3. Test taker and maker execution separately, including partial fills, queue uncertainty, cancellation latency, and adverse selection.
4. Impose bankroll, maximum-loss, and liquidity constraints before asking whether the feasible size can target $3 EV.
5. Add residual-band optimization only if residual dynamics and executable convergence both survive those tests.

Your proposal supplies useful hypotheses. The decisive missing evidence is a positive, reproducible, execution-aware result out of sample. The same standard applies to my settlement-aware proposal.
