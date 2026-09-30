# BTC five-minute strategy: settlement-aware value with constrained execution

Date: 29 September 2026. Status: proposed research design, not demonstrated alpha. This is the matching analysis of my candidate strategy, incorporating the review of your residual-band proposal. Source findings have not been independently replicated. All numerical trades below are hypothetical; no live orders, risk-limit changes, or strategy deployment were performed.

Related: [Review of your proposal](btc5m_peer_research_review_2026-09-29.md), [extended literature review](btc5m_strategy_research_2026-09-28.md).

## The proposition

The first candidate I would test is a settlement-aware probability model whose output is compared with executable prices and whose positions are constrained by worst-case dollar loss. The model includes the market's own probability as a strong baseline. Passive execution is a competing route that must earn its place through measured fill economics.

The hypothesis is specific: information about the official reference, its accumulated averaging history, and remaining uncertainty may occasionally imply a probability different enough from the executable book to pay costs. Correctly identifying that probability is necessary but does not establish an edge. Other traders may already price it better and faster.

The target is **$3 expected net profit on some selected market episodes at feasible size**. A requirement to realize at least $3 in every five-minute market is incompatible with this statistical proposal. If that requirement remains absolute, restrict the search to fully secured payoff inconsistencies and accept that no trade may qualify.

## What we are actually trading

Let Y be the official Up outcome, taking values zero or one. Up pays Y and Down pays 1-Y. Given N_U and N_D shares and total acquisition cost C, settlement profit is

\[
\Pi=N_UY+N_D(1-Y)-C.
\]

The two-state guaranteed-profit test is

\[
N_U-C\ge3,\qquad N_D-C\ge3.
\]

This can be posed as a linear optimization over executable order-book depth when prices and costs at each level are known. At 0.70 Up and 0.30 Down before fees, unequal sizing cannot satisfy both constraints: extra directional shares improve one outcome at the expense of the other. Equal shares break even before fees.

For an equal-share pair, all-in unit pair cost c_U+c_D<1 produces a payoff surplus N[1-c_U-c_D]. At 0.97, 100 completed pairs produce $3 before any omitted costs and require $97 of acquisition capital. Both legs must really be acquired in the same mutually exclusive, exhaustive contract. Sequential fills introduce a separate inventory risk.

For directional forecasting, first identify the official payoff functional. A recent BTC five-minute Rules section names a Chainlink 60-second TWAP stream. That does not by itself establish all sampling and boundary conventions. Archive market rules, the opening threshold, stream identity, and the actual settlement report. [Inspected market](https://polymarket.com/event/btc-updown-5m-1790074800?outcomeIndex=0)

## The model: price the reference that resolves the contract

For an endpoint contract, a diffusion probability can be an adequate null model. For an averaged reference, retain the known part of the average. This is the useful connection to Asian-option mathematics: the state must include accumulated path information. [Geman and Yor, 1993](https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1467-9965.1993.tb00092.x)

For illustration only, assume the verified closing reference is

\[
A_T=\frac1L\int_{T-L}^{T}S_u\,du,
\quad Y=\mathbf1\{A_T\ge K\},
\quad dS_u=\mu\,du+\sigma_d\,dW_u.
\]

Here sigma_d is BTC price volatility in dollars per square-root second, not annual percentage volatility. K is already observed without error, the reference history is observed exactly, and mu and sigma_d are constant over the remaining horizon. These assumptions are a transparent starting point, not a description established for the live feed.

During the averaging interval, define h=T-t and I_t=integral from T-L to t of S_u du. Then

\[
m_A=E[A_T\mid\mathcal F_t]
=\frac{I_t+hS_t+\mu h^2/2}{L},
\]

\[
v_A=\operatorname{Var}(A_T\mid\mathcal F_t)
=\frac{\sigma_d^2h^3}{3L^2},
\qquad
\widehat p_U=\Phi\left(\frac{m_A-K}{\sqrt{v_A}}\right).
\]

Before the averaging interval, h>=L, the corresponding expressions are

\[
m_A=S_t+\mu(h-L/2),\qquad
v_A=\sigma_d^2(h-2L/3).
\]

These follow by integrating Brownian increments. They agree at h=L. At expiry the model becomes the observed outcome rather than a division by zero. Exact equality, rounding, and missing reports require the contract's actual conventions.

The averaging insight changes the proposed volatility research. During the averaging interval, with deterministic time-varying variance, remaining variance is

\[
v_A=\frac1{L^2}\int_t^T (T-u)^2\sigma_d^2(u)\,du.
\]

The target is thus a weighted variance functional, not always the unweighted next-five-minute integrated variance. Earlier shocks affect more of the remaining average. Discrete samples require the corresponding covariance-weight calculation.

A state estimator must also account for exchange/reference basis, delayed reports, missing underlying history, and uncertainty in K. In general model the conditional distribution of A_T-K directly. Summing independent Gaussian variances is a special case, not a universal fix. If only the smoothed stream is observed, the hidden path may not be reconstructible exactly.

Bachelier-style arithmetic diffusion makes these derivations transparent. It can fail around jumps and changing volatility. Compare it with simulated reference paths under richer dynamics only after validating the simple model and the actual reference semantics.

## What physics and mathematical optimization contribute

The connection to diffusion physics is concrete. During averaging, the backward equation for outcome probability u(t,s,I), under the illustrative model, is

\[
u_t+\mu u_s+\tfrac12\sigma_d^2u_{ss}+s u_I=0,
\qquad u(T,s,I)=\mathbf1\{I/L\ge K\}.
\]

This describes the evolution of uncertainty about a specified terminal boundary. It supplies a coherent forecast and sensitivity calculation. It does not supply a reason why the market price should be wrong.

Optimization then answers distinct questions:

- **Payoff arbitrage:** linear programming across order-book levels subject to a positive payoff in each outcome, conditional on secured execution.
- **Directional sizing:** robust expected utility or constrained expected profit under a probability uncertainty set.
- **Maker quotes and exits:** finite-horizon stochastic control with inventory, cash, pending orders, fill probabilities, and adverse selection.
- **Model selection:** chronological statistical evaluation against the market baseline, followed by executable economic evaluation.

Avellaneda–Stoikov supplies the inventory-control template; robust Kelly supplies a way to make allocation sensitive to uncertain probabilities. Their assumptions must be adapted to binary payoff, size-dependent cost, and actual order handling. [Avellaneda–Stoikov](https://math.nyu.edu/inmemoriam/avellaneda/HighFrequencyTrading.pdf), [Sun and Boyd](https://web.stanford.edu/~boyd/papers/robust_kelly.html)

OU and Hawkes models are optional components. Neither is required for this first falsifiable strategy.

## The strategy: conservative value, selective execution

### State

Maintain time remaining; exact contract rules and reference state; basis and variance forecasts; a probability interval [p-,p+]; actual Up and Down books with depth and age; cash; gross inventories and matched pairs; outstanding orders; and observed execution delays.

Net directional inventory alone is insufficient for execution accounting. Terminal wealth can be written as cash+N_D+(N_U-N_D)Y, but matched positions, order reservations, and unsettled obligations still affect available capital.

### Probability baseline

Evaluate three preregistered forecasts: the book alone, the physical-reference model alone, and a simple calibrated combination. A candidate combination is

\[
\operatorname{logit}(p)=\alpha+
\beta\operatorname{logit}(p_{book})+
\eta\operatorname{logit}(p_{reference}),
\]

with clipped inputs and parameters fitted only on earlier data. If the reference model contributes no stable incremental information, remove it. A classifier is acceptable when its purpose is incremental calibration beyond the book; sophisticated architecture is not evidence of advantage.

Obtain conservative probabilities from held-out calibration uncertainty and explicit stress scenarios. A discretionary haircut is a risk buffer, not automatically a statistical confidence bound. Check calibration specifically on the selected trades, including maker fills when relevant.

### Entry economics

For an order of N shares, let C_j(N) include the full executable cost on side j: depth-weighted price, applicable fees, modeled latency slippage, and other attributable charges. Do not subtract the spread again from an ask-based cost. Under a hold-to-settlement policy,

\[
G_U(N)=Np^- -C_U(N),\qquad
G_D(N)=N(1-p^+) -C_D(N).
\]

Trade only if a permitted size has positive conservative expected profit after the model-error margin. If $3 is a strict target for selected episodes, also require G_j(N)>=3. If neither side qualifies, skip.

The documented crypto fee coefficient is 0.07 in the current category schedule, with zero platform trading fee for makers. Use actual market fee parameters, not a permanently hard-coded category assumption. Put unconfirmed rebates at zero. [Fees](https://docs.polymarket.com/trading/fees)

The documented taker hold for selected markets adds execution uncertainty; metadata and measured end-to-end latency belong in the simulator. [Order lifecycle](https://docs.polymarket.com/concepts/order-lifecycle)

### Taker benchmark and maker extension

Begin the economic experiment with one taker entry and settlement payoff. This isolates probability quality from an additional exit model. It is a research benchmark, not a recommendation to force taker execution or a claim that it will clear the fee hurdle.

In parallel at the replay stage, evaluate passive entry. Maker pricing must use expected value conditional on our order filling, not just the pre-order probability. Let F denote a fill. The relevant unit edge is E[Y_j | F, information]-cost, with a full episode model when exits are required.

A maker route qualifies only if its expected profit across fills, non-fills, partial fills, forced liquidations, and cancellations is positive under defensible queue assumptions. Zero trading fees do not remove adverse selection. Do not assume that a quote touched by the historical price was filled.

After a jump or stale reference, cancel or abstain according to the measured information and execution state. A fixed 45-second boundary is a testable rule; a sensitivity- and latency-based exclusion is a competing rule. Neither is established optimal in advance.

## How to size a $3 target without letting the target control risk

For constant unit cost c and conservative probability pL of the purchased outcome,

\[
e=p_L-c,\qquad N_{target}=3/e\quad(e>0).
\]

If maximum permissible settlement loss is Lmax, then N*c<=Lmax. Consequently, a necessary target-feasibility condition is

\[
\frac{p_L-c}{c}\ge\frac3{L_{max}}.
\]

This ratio is the expected net return on the capital at risk. The dollar target can be infeasible even when the model has a positive edge.

With bankroll B and nonnegative long-only size N, the simple robust Kelly problem is

\[
\max_{0\le N<B/c}\min_{p\in[p_L,p_H]}
\left[p\log(B+N(1-c))+(1-p)\log(B-Nc)\right].
\]

For positive N the worst probability is pL. With constant cost and no other constraints,

\[
N_K=\max\left(0,\frac{B(p_L-c)}{c(1-c)}\right).
\]

Use a chosen fraction of that quantity, plus the explicit loss, liquidity, and aggregate-exposure caps. Fractional Kelly reduces sensitivity; it does not validate pL. With depth-dependent costs, solve the constrained problem numerically rather than applying this closed form.

A fixed-target policy uses the smallest permitted size satisfying the $3 condition, provided it is below every risk cap. If risk-optimal or permitted size is smaller, accept the smaller target or skip. Do not enlarge the account risk to force the identity to hold. Bankroll and acceptable losses have not been specified, so this research does not recommend a new live cap.

Example: c=0.7147 and pL=0.7447 imply e=0.03. One hundred shares have $3 conservative expected profit, but settle at +$28.53 or -$71.47. This is entirely different from a trade with a reliable $3 realized gain.

At the current $5 total-debit cap and the same 70-cent ask, a certain settlement win earns at most about $1.996 after the stated entry fee. The $3 target is infeasible for this entry even with perfect prediction.

## Stops and take-profits are another optimization problem

An entry probability does not determine the probability of hitting a take-profit before a stop. If exiting before settlement, evaluate the distribution of the executable exit price and all selling costs under the full stopping policy.

For a risk-neutral one-step comparison, holding a share has conditional settlement value p_t, while selling has executable value bid minus selling costs. Risk constraints can justify selling even when holding has higher expectation. In a finite-horizon controller, compare sell, hold, hedge, and wait actions including future opportunities and inventory risk.

A stop is triggered by a state; it does not guarantee execution at the trigger price. To impose a hard dollar exposure limit without assuming stop fills, size against losing the entire acquisition cost of an unhedged binary position. A completed complementary hedge changes that payoff bound.

The local ask70 profile has a mandatory pre-expiry exit. A settlement-hold research candidate therefore requires separate lifecycle assumptions in simulation; it cannot be compared as though both policies had identical exits.

## Keep complete-set acquisition separate

Track matched-pair profit and unmatched directional inventory separately. A useful episode decomposition is

\[
E[\Pi]=P(complete)E[\Pi\mid complete]
+P(orphan)E[\Pi\mid orphan]
+P(no\ fill)E[\Pi\mid no\ fill].
\]

Completion probability alone is not enough: orphan losses can dominate small pair gains. Report the distribution of maximum unmatched exposure and realistic completion or liquidation costs. Posted bid sums below one are opportunity indicators, not evidence of realized arbitrage.

## What the literature changes about this proposal

The most valuable recent results are reasons to make the experiment harder to pass:

- **OpenMarket (2026):** its BTC fifteen-minute study reports an external predictive model weaker than the market baseline and losses under its simulated costs. Its horizon and sample are different from the present five-minute reference regime. This supports using the book as the benchmark, not assuming oracle modeling wins. [Paper](https://arxiv.org/abs/2607.26245)
- **Dubach (2026), revised v2:** trade-direction inference requires care; a convenient feed label can misrepresent executed flow. Establish label quality before adding flow features. [Paper](https://arxiv.org/abs/2604.24366v2)
- **Dai, Jia and Yu (2026):** findings around settlement-time trading in an earlier five-minute sample motivate stress scenarios, not an automatically transferable current strategy. [Paper](https://arxiv.org/abs/2606.31675)
- **Corsi (2009):** multiscale volatility is worth benchmarking, but a daily HAR result is not a validation of a subminute weighted-reference forecast. [Paper](https://statmath.wu.ac.at/~hauser/LVs/FinEtricsQF/References/Corsi2009JFinEtrics_LMmodelRealizedVola.pdf)
- **Bandi, Fusari and Renò, revised 2026:** short expiry does not license ignoring higher distributional moments. Evaluate tail sensitivity before treating high model probabilities as safe. [0DTE Option Pricing](https://ssrn.com/abstract=4503344)

Proper probability scores and controls against repeated backtest search remain essential. Better average Brier score does not by itself prove positive selected-trade P&L, and one profitable simulation with optimistic fills does not establish forecasting skill. [Gneiting–Raftery](https://sites.stat.washington.edu/people/raftery/Research/PDF/Gneiting2007jasa.pdf), [Bailey et al.](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)

## The work that decides whether this is real

1. **Reference audit.** Collect rules and actual reports across several windows; reproduce the threshold and settlement label. Resolve endpoint versus rolling average versus another window functional before valuation comparisons.
2. **Causal recorder.** Record both outcome books, trades, exchange/reference updates, sequence numbers, source and local receive timestamps, clock uncertainty, and order acknowledgements. Preserve events; one-second snapshots alone cannot establish subsecond fill economics. Do not use a future nearest timestamp in a historical decision.
3. **Probability experiment.** Fit only on earlier markets. Compare book-only, endpoint, settlement-aware, and combined forecasts on later full windows. Report calibration and loss by time remaining, distance to threshold, volatility, and selected-trade status.
4. **Execution replay.** Sweep realistic depths at simulated arrival, include fee rules and delays, and bound maker results using conservative and optimistic queue scenarios. Evaluate full market episodes including rejected, canceled, and partial orders. If the result works only under optimistic fills, it fails the deployment gate.
5. **Economic test.** Freeze parameters before the final evaluation. Compare the actual ask70 policy, existing Z-Gap policy, the simple settlement-hold benchmark, passive execution, and no trade. Use matched windows and separate rule regimes. Bootstrap by market/day or another defensible dependence block, not individual one-second rows.
6. **Capacity test.** Recompute opportunity count and P&L as size increases. Count how many selected episodes can support $3 expected net profit within the loss cap; include all scheduled windows when reporting daily averages. Stress worse latency, lower fill rates, basis errors, jumps, and missing reference data.

There is no universal few-hundred-markets threshold for significance. As an illustration, the 100-share example has a standard deviation near $43.60 against a $3 mean. A crude independent-sample normal calculation needs about (1.96*43.60/3)^2 = 812 observations merely to make the nominal 95% half-width about $3. Achieving useful statistical power generally needs more; dependence and selection complicate this further. This is not a prescribed sample size.

The deployment decision should require economically meaningful out-of-sample evidence, honest uncertainty, plausible execution, and acceptable drawdown at the permitted size. If the reference model does not improve the market baseline or the improvement cannot pay execution costs, reject the strategy rather than add complexity to rescue it.

## What remains unproved in my own proposal

We have not established that the settlement functional can be reconstructed accurately from accessible data; that our estimate beats the book; that any advantage survives delays and fees; that makers can capture it after fill selection; or that there is enough depth and opportunity frequency to support $3-sized episodes.

The mathematical model organizes those questions and makes them falsifiable. The first implementation priority is therefore the reference audit and causal replay dataset. The justified next result is a measured go/no-go conclusion, not a promise of profitability.
