/**
 * Strategy Research: profile x market evidence, with honest lifecycle labelling.
 *
 * Four lifecycle states are shown, and they are deliberately distinct because
 * collapsing them would be a lie of omission:
 *
 *   RESEARCH                evaluated, no certified market
 *   SHADOW                  evaluated against real or captured data
 *   PRODUCTION CERTIFIABLE  passed every evidence requirement
 *   ACTIVE PRODUCTION       the Champion, the only profile trading real money
 *
 * The panel is informational. It exposes no BUY/SELL control, and
 * `multi_market_production` is displayed as DISABLED so the current boundary is
 * visible rather than implied.
 */

export interface ResearchProfile {
  profile_id:string; strategy_id:string; version:string;
  parameters:Record<string,string>;
  target_model:{version:string;atr_multiple:string;floor_bps:string;cap_bps:string};
  fingerprint:string; strategy_fingerprint:string; profile_contract_version:string;
  markets:string[]; evaluator:string; description:string;
  markets_certified:string[]; lifecycle:string;
}

export interface RiskAdjustedProfile extends ResearchProfile {
  risk_adjusted_metrics:boolean; declares_invalidation:boolean;
}

export interface ExecutionResearchView {
  maker_fee_rate:string|null;
  taker_fee_rate:string;
  maker_fee_confirmed:boolean;
  maker_below_taker:boolean;
  modes:Record<string,{mode:string;entry_liquidity:string;exit_liquidity:string;
    entry_order_type:string;exit_order_type:string;timeout_bars:number;
    requires_post_only:boolean;research_only:boolean;production_authorised:boolean;
    description:string}>;
  fee_floors:Record<string,string>;
  fill_evidence:string;
  maker_fills_determined:boolean;
  research_only:boolean;
  production_authorised:boolean;
  cancel_implemented:boolean;
  production_mutations_added:number;
}

export interface ProductionGapView {
  missing:string[]; partial:string[]; present:string[];
  production_mutations_required:string[];
}

export interface HorizonProfileView {
  profile_id:string; strategy_id:string; version:string; fingerprint:string;
  strategy_fingerprint:string;
  timeframe:{version:string;name:string;seconds:number;base_bars:number;
    label_convention:string};
  concept:string; horizon_profile_version:string; description:string;
}

export interface HorizonResearchView {
  hypothesis:string;
  predeclared_timeframes:string[];
  horizons_tested_by_rule:boolean;
  profiles:HorizonProfileView[];
  profile_ids:string[];
  max_configurations_per_strategy_and_horizon:number;
  configuration_budget:Record<string,number>;
  selection_rule:string[];
  all_configurations_retained:boolean;
  certifying_execution_mode:string;
  maker_bound_may_certify:boolean;
  base_bar_seconds:number;
  aggregation_uses_existing_base_bars:boolean;
  incomplete_buckets_dropped_not_filled:boolean;
  same_bar_fills_allowed:boolean;
  outcomes:string[];
  verdict:string|null;
  verdict_pending_frozen_experiment:boolean;
  frozen_profile_fingerprints_changed:boolean;
  drawdown_policy_changed:boolean;
  capital_limits_changed:boolean;
}

export interface MicrostructureCaptureView {
  enabled:boolean;
  provenance:string;
  endpoints:string[];
  order_capability:string;
  production_mutation:string;
  credentials_scope:string;
  separate_from_production_ledger:boolean;
  captured_books:string[];
  order_book_events:number;
  trade_tape_events:number;
  capture_seconds:number;
  health:string;
  gaps:number;
  duplicates:number;
  sequence_regressions:number;
  anomalies_repaired:number;
  bounded_storage:boolean;
  retention_hours:number;
  queue_position_observable:boolean;
  passive_fill_exactness:string;
  adverse_selection_analysis_possible:boolean;
  exact_passive_fill_claim_made:boolean;
}

export interface AsymmetryProfileView {
  profile_id:string; strategy_id:string; version:string; fingerprint:string;
  strategy_fingerprint:string; concept:string; asymmetric_challenger_version:string;
  description:string;
  parameters:[string,string][]|Record<string,string>;
  timeframe?:{name:string;seconds:number;base_bars:number;label_convention:string};
}

export interface AsymmetryResearchView {
  hypothesis:string;
  challengers:AsymmetryProfileView[];
  challenger_ids:string[];
  max_configurations_per_challenger:number;
  configuration_budget:Record<string,number>;
  reward_risk_thresholds:string[];
  threshold_basis:string;
  selection_rule:string[];
  comparison_semantics:string[];
  all_configurations_retained:boolean;
  thresholds_chosen_before_results:boolean;
  certifying_execution_mode:string;
  maker_bound_computed:boolean;
  maker_bound_reason:string;
  gate_is_research_only:boolean;
  gate_can_override_economic_guard:boolean;
  gate_can_override_risk_engine:boolean;
  gate_can_override_risk_policy:boolean;
  no_future_structure:boolean;
  verdict:string|null;
  verdict_pending_frozen_experiment:boolean;
  drawdown_policy_changed:boolean;
  risk_engine_changed:boolean;
  capital_limits_changed:boolean;
}

export interface StrategyResearchView {
  research_version:string;
  promotion:string;
  multi_market_production:string;
  champion:{profile_id:string;strategy_id:string;version:string;fingerprint:string;
    certification_status:string;lifecycle:string;intended_gross_edge_bps:string};
  profiles:ResearchProfile[];
  economic_viability:{taker_fee_rate:string;spread_bps:string;account_fee_confirmed:boolean;
    minimum_viable_gross_edge_bps:string|null;champion_intended_gross_edge_bps:string;
    champion_economically_viable:boolean|null};
  markets:{market:string;status:string;strategy_compatibility:string;market_class:string;
    lifecycle:string}[];
  shadow:Record<string,unknown>[];
  profile_by_id:string[];
  frozen_profiles?:string[];
  risk_adjusted_challengers?:RiskAdjustedProfile[];
  risk_adjusted_research?:{
    selection_rule:string[]; configuration_sets:Record<string,number>;
    mae_mfe_implemented:boolean; markets_pooled:boolean;
    holdout_frozen_before_selection:boolean; drawdown_policy_changed:boolean;
    capital_limits_changed:boolean};
  execution_research?:ExecutionResearchView;
  production_gap?:ProductionGapView;
  horizon_research?:HorizonResearchView;
  microstructure_capture?:MicrostructureCaptureView;
  asymmetry_research?:AsymmetryResearchView;
}

/** Shadow rows carry whichever fields the pipeline produced; read them defensively. */
type ShadowRow = Record<string, unknown>;

const text=(value:unknown,fallback='—')=>
  value===null||value===undefined||value===''?fallback:String(value);

const LIFECYCLE_LABEL:Record<string,string>={
  RESEARCH:'RESEARCH', SHADOW:'SHADOW', PRODUCTION_CERTIFIABLE:'PRODUCTION CERTIFIABLE',
  ACTIVE_PRODUCTION:'ACTIVE PRODUCTION', RESEARCH_ONLY:'RESEARCH ONLY',
};

/**
 * Economic viability of the active Champion. This is the 0.1.3 finding made
 * visible: a 20 bps target cannot pay a ~156 bps round trip.
 */
function ChampionViability({research}:{research:StrategyResearchView}){
  const viability=research.economic_viability;
  const viable=viability.champion_economically_viable;
  return <>
    <h3>Champion economic viability</h3>
    <p className="warn">
      {viable===null
        ? 'Account fee not confirmed yet: viability cannot be established.'
        : viable
          ? 'The Champion\u2019s intended move clears estimated friction.'
          : 'The Champion\u2019s intended move is below estimated friction, so its BUY signals are refused by the economic guard.'}
    </p>
    <div className="table"><table><tbody>
      <tr><th>Confirmed taker fee</th><td>{viability.account_fee_confirmed?viability.taker_fee_rate:'UNAVAILABLE'}</td></tr>
      <tr><th>Observed spread (bps)</th><td>{text(viability.spread_bps)}</td></tr>
      <tr><th>Champion intended gross edge (bps)</th><td>{viability.champion_intended_gross_edge_bps}</td></tr>
      <tr><th>Minimum viable gross edge (bps)</th><td>{text(viability.minimum_viable_gross_edge_bps)}</td></tr>
      <tr><th>Champion economically viable</th>
        <td className={viable?'positive':'negative'}>{viable===null?'UNKNOWN':viable?'YES':'NO'}</td></tr>
    </tbody></table></div>
  </>;
}

function ProfileTable({research}:{research:StrategyResearchView}){
  return <>
    <h3>Strategy profiles</h3>
    <div className="table"><table>
      <thead><tr><th>Profile</th><th>Strategy</th><th>Ver.</th><th>Lifecycle</th>
        <th>Markets certified</th><th>ATR multiple</th><th>Target floor</th><th>Target cap</th>
        <th>Fingerprint</th></tr></thead>
      <tbody>{research.profiles.map(profile=><tr key={profile.profile_id}
        data-profile={profile.profile_id} data-lifecycle={profile.lifecycle}>
        <td>{profile.profile_id}</td><td>{profile.strategy_id}</td><td>{profile.version}</td>
        <td><span className="status" data-lifecycle={profile.lifecycle}>
          {profile.lifecycle==='ACTIVE_PRODUCTION'?LIFECYCLE_LABEL.ACTIVE_PRODUCTION:'RESEARCH'}</span></td>
        <td>{profile.markets_certified.length?profile.markets_certified.join(', '):'NONE'}</td>
        <td>{profile.target_model.atr_multiple}</td>
        <td>{profile.target_model.floor_bps} bps</td>
        <td>{profile.target_model.cap_bps} bps</td>
        <td><code title={profile.fingerprint}>{profile.fingerprint.slice(0,16)}…</code></td>
      </tr>)}</tbody></table></div>
  </>;
}

function ShadowTable({rows}:{rows:ShadowRow[]}){
  if(!rows.length)return <p>No shadow evaluation recorded yet. Shadow research evaluates
    discovered markets that have captured candles and an observed order book; a market
    without them is excluded rather than given synthetic data.</p>;
  return <>
    <h3>Shadow research per market</h3>
    <div className="table"><table>
      <thead><tr><th>Market</th><th>Profiles</th><th>Evaluations</th><th>Signals</th>
        <th>Shadow trades</th><th>Gross P&amp;L</th><th>Fees / friction</th><th>Net P&amp;L</th>
        <th>Drawdown</th><th>Economic reject rate</th><th>Data source</th><th>Evidence</th></tr></thead>
      <tbody>{rows.map((row,index)=><tr key={String(row.market??index)}>
        <td>{text(row.market)}</td>
        <td>{(row.profiles as unknown[]|undefined)?.length??0}</td>
        <td>{text(row.evaluations, '0')}</td>
        <td>{text(row.signals, '0')}</td>
        <td>{text(row.shadow_trades, '0')}</td>
        <td>{text(row.gross_pnl_mxn, '0')}</td>
        <td>{text(row.fees_mxn, '0')}</td>
        <td>{text(row.net_pnl_mxn, '0')}</td>
        <td>{text(row.max_drawdown_mxn, '0')}</td>
        <td>{text(row.economic_reject_rate, '0')}</td>
        <td>{text(row.data_source)}</td>
        <td><span className="status" data-evidence={String(row.evaluated)}>
          {row.evaluated?'EVALUATED':'AWAITING DATA'}</span></td>
      </tr>)}</tbody></table></div>
  </>;
}

function MarketTable({research}:{research:StrategyResearchView}){
  if(!research.markets.length)return null;
  return <>
    <h3>Market x profile compatibility</h3>
    <p>Compatibility is declared per profile. A BTC profile is not assumed to be an ETH or
      XRP profile, and stablecoin/fiat-like books are excluded from volatile-crypto research.</p>
    <div className="table"><table>
      <thead><tr><th>Market</th><th>Class</th><th>Discovery status</th>
        <th>Strategy compatibility</th><th>Lifecycle</th></tr></thead>
      <tbody>{research.markets.map(market=><tr key={market.market}>
        <td>{market.market}</td><td>{market.market_class}</td><td>{market.status}</td>
        <td>{market.strategy_compatibility}</td>
        <td>{LIFECYCLE_LABEL[market.lifecycle]??market.lifecycle}</td>
      </tr>)}</tbody></table></div>
  </>;
}

function RiskAdjustedPanel({research}:{research:StrategyResearchView}){
  const challengers=research.risk_adjusted_challengers??[];
  const meta=research.risk_adjusted_research;
  if(!challengers.length||!meta)return null;
  return <>
    <h3>Risk-adjusted challengers</h3>
    <p>Challengers added after the corrected drawdown measurement. Each declares where its
      thesis is invalidated <b>when the position opens</b>, so risk is decided before it is
      taken rather than discovered from a drawdown. A profile with no declared boundary is
      shown as such: it makes no risk claim at all, and must not be read as risk-bounded.</p>
    <div className="table"><table>
      <thead><tr><th>Profile</th><th>Version</th><th>Generation</th>
        <th>Declares boundary</th><th>Declares time stop</th><th>Target floor (bps)</th>
        <th>Markets certified</th><th>Fingerprint</th></tr></thead>
      <tbody>{challengers.map(profile=><tr key={profile.profile_id}
        data-challenger={profile.profile_id}
        data-declares-invalidation={profile.declares_invalidation}>
        <td>{profile.profile_id}</td>
        <td>{profile.version}</td>
        <td>RISK_ADJUSTED</td>
        <td><span data-boundary={profile.declares_invalidation}>
          {profile.declares_invalidation?'YES':'NO'}</span></td>
        <td>{profile.parameters?.max_holding_bars??'—'}</td>
        <td>{profile.target_model.floor_bps}</td>
        <td>{profile.markets_certified?.length?profile.markets_certified.join(', '):'NONE'}</td>
        <td className="fingerprint" title={profile.fingerprint}>{profile.fingerprint}</td>
      </tr>)}</tbody></table></div>
    <h3>Research discipline</h3>
    <div className="table"><table><tbody>
      <tr><th>MAE / MFE implemented</th>
        <td data-mae-mfe={meta.mae_mfe_implemented}>
          {meta.mae_mfe_implemented?'YES':'NO'}</td></tr>
      <tr><th>Markets pooled</th>
        <td data-pooled={meta.markets_pooled}>{meta.markets_pooled?'YES':'NO'}</td></tr>
      <tr><th>Holdout frozen before selection</th>
        <td>{meta.holdout_frozen_before_selection?'YES':'NO'}</td></tr>
      <tr><th>Configurations per challenger</th>
        <td>{Object.entries(meta.configuration_sets??{})
          .map(([id,count])=>`${id}: ${count}`).join(' · ')}</td></tr>
      <tr><th>Selection rule</th><td>{(meta.selection_rule??[]).join(' → ')}</td></tr>
      <tr><th>Drawdown policy changed</th>
        <td data-policy-changed={meta.drawdown_policy_changed}>
          {meta.drawdown_policy_changed?'YES':'NO'}</td></tr>
      <tr><th>Capital limits changed</th>
        <td data-capital-changed={meta.capital_limits_changed}>
          {meta.capital_limits_changed?'YES':'NO'}</td></tr>
    </tbody></table></div>
    <p>The selection rule is shown because it is the thing that makes the search honest: risk
      gates rank ahead of P&amp;L, so a configuration cannot win by earning more while risking
      more. Every tested configuration is retained, not only the winner.</p>
  </>;
}

function ExecutionResearchPanel({research}:{research:StrategyResearchView}){
  const exec=research.execution_research;
  const gap=research.production_gap;
  if(!exec)return null;
  const floors=Object.entries(exec.fee_floors??{});
  const modes=Object.values(exec.modes??{});
  return <>
    <h3>Passive execution economics</h3>
    <p>Research only. These are hypotheses about how an order would be <b>submitted</b>,
      not an authorisation: no passive order has been placed, no cancel capability exists,
      and the Production mutation boundary is unchanged. Fee floors are structural and come
      from the account-confirmed schedule; the <b>fill</b> question is separate and is
      deliberately not answered, because candle history cannot show that a resting order
      was ahead of the trades that printed at its price.</p>
    <div className="table"><table><tbody>
      <tr><th>Maker fee (account confirmed)</th>
        <td data-maker-confirmed={exec.maker_fee_confirmed}>
          {exec.maker_fee_confirmed?exec.maker_fee_rate:'UNAVAILABLE'}</td></tr>
      <tr><th>Taker fee</th><td>{exec.taker_fee_rate}</td></tr>
      <tr><th>Maker below taker</th>
        <td data-maker-cheaper={exec.maker_below_taker}>
          {exec.maker_below_taker?'YES':'NO'}</td></tr>
      <tr><th>Fill evidence</th>
        <td data-fill-evidence={exec.fill_evidence}>{exec.fill_evidence}</td></tr>
      <tr><th>Maker fills determined</th>
        <td data-maker-fills={exec.maker_fills_determined}>
          {exec.maker_fills_determined?'YES':'NO'}</td></tr>
      <tr><th>Passive Production authorised</th>
        <td data-production-authorised={exec.production_authorised}>
          {exec.production_authorised?'YES':'NO'}</td></tr>
      <tr><th>Cancel capability implemented</th>
        <td data-cancel-implemented={exec.cancel_implemented}>
          {exec.cancel_implemented?'YES':'NO'}</td></tr>
    </tbody></table></div>

    <h4>Fee-only round-trip floor by execution mode</h4>
    <p>The gross price movement required for fees alone to break even, using the real fee
      currencies: the buy fee is charged in the base asset and the sell fee in the quote
      asset, so the round trip is not the sum of the two rates. Spread is excluded because
      only one leg pays it, and counting it here would charge the same cost twice.</p>
    <div className="table"><table>
      <thead><tr><th>Mode</th><th>Entry</th><th>Exit</th><th>Timeout (bars)</th>
        <th>Post-only</th><th>Fee-only floor (bps)</th></tr></thead>
      <tbody>{modes.map(mode=><tr key={mode.mode} data-mode={mode.mode}
        data-requires-post-only={mode.requires_post_only}>
        <td>{mode.mode}</td>
        <td>{mode.entry_order_type}</td>
        <td>{mode.exit_order_type}</td>
        <td>{mode.timeout_bars}</td>
        <td>{mode.requires_post_only?'YES':'NO'}</td>
        <td>{exec.fee_floors?.[mode.mode]??'—'}</td>
      </tr>)}</tbody></table></div>
    <p>{floors.length} modes priced. A lower floor means a smaller move is needed to clear
      costs; it says nothing about whether such a move will be found, or whether a resting
      order would have filled.</p>

    {gap&&<>
      <h4>Production architecture gap</h4>
      <p>What would have to change before passive Production execution could be safe. No new
        Production mutation capability was added by this milestone.</p>
      <div className="table"><table><tbody>
        <tr><th>Present</th><td data-gap-present={gap.present.length}>
          {gap.present.join(', ')||'NONE'}</td></tr>
        <tr><th>Partial</th><td data-gap-partial={gap.partial.length}>
          {gap.partial.join(', ')||'NONE'}</td></tr>
        <tr><th>Missing</th><td data-gap-missing={gap.missing.length}>
          {gap.missing.join(', ')||'NONE'}</td></tr>
        <tr><th>Production mutations required</th>
          <td data-gap-mutations={gap.production_mutations_required.length}>
            {gap.production_mutations_required.join(', ')||'NONE'}</td></tr>
      </tbody></table></div>
    </>}
  </>;
}

/**
 * Time-horizon research (MVP 0.2.4). This panel reports a *predeclared* experiment, not a
 * result: the verdict is produced by a frozen manifest evaluated on real market data, and
 * until that has run there is nothing to claim. Publishing the predeclared horizons, budget
 * and selection rule up front is what makes the eventual verdict checkable.
 */
function HorizonPanel({research}:{research:StrategyResearchView}){
  const horizon=research.horizon_research;
  if(!horizon)return null;
  return <>
    <h3>Time-horizon research</h3>
    <p>The same two concepts evaluated on a coarser horizon. A larger bar carries a larger
      move, while friction is unchanged, so the share of the opportunity lost to costs should
      fall. Whether that is worth trading depends on how often the larger target is reached
      and how long capital is tied up, so capital-hours are reported beside every result.</p>
    <div className="table"><table><tbody>
      <tr><th>Hypothesis</th><td>{horizon.hypothesis}</td></tr>
      <tr><th>Predeclared timeframes</th>
        <td data-horizons={horizon.predeclared_timeframes.join(',')}>
          {horizon.predeclared_timeframes.join(', ')}</td></tr>
      <tr><th>Horizons tested by rule</th>
        <td data-horizons-fixed={horizon.horizons_tested_by_rule}>
          {horizon.horizons_tested_by_rule?'YES':'NO'}</td></tr>
      <tr><th>Max configurations per strategy and horizon</th>
        <td data-config-budget={horizon.max_configurations_per_strategy_and_horizon}>
          {horizon.max_configurations_per_strategy_and_horizon}</td></tr>
      <tr><th>All configurations retained</th>
        <td data-all-configs-retained={horizon.all_configurations_retained}>
          {horizon.all_configurations_retained?'YES':'NO'}</td></tr>
      <tr><th>Certifying execution mode</th>
        <td data-certifying-mode={horizon.certifying_execution_mode}>
          {horizon.certifying_execution_mode}</td></tr>
      <tr><th>Maker bound may certify</th>
        <td data-maker-may-certify={horizon.maker_bound_may_certify}>
          {horizon.maker_bound_may_certify?'YES':'NO'}</td></tr>
      <tr><th>Incomplete buckets dropped, not filled</th>
        <td data-incomplete-dropped={horizon.incomplete_buckets_dropped_not_filled}>
          {horizon.incomplete_buckets_dropped_not_filled?'YES':'NO'}</td></tr>
      <tr><th>Same-bar fills allowed</th>
        <td data-same-bar-fills={horizon.same_bar_fills_allowed}>
          {horizon.same_bar_fills_allowed?'YES':'NO'}</td></tr>
      <tr><th>Drawdown policy changed</th>
        <td data-horizon-drawdown-changed={horizon.drawdown_policy_changed}>
          {horizon.drawdown_policy_changed?'YES':'NO'}</td></tr>
      <tr><th>Verdict</th>
        <td data-verdict-pending={horizon.verdict_pending_frozen_experiment}>
          {horizon.verdict??'PENDING FROZEN EXPERIMENT'}</td></tr>
    </tbody></table></div>

    <h4>Horizon profiles</h4>
    <div className="table"><table>
      <thead><tr><th>Profile</th><th>Concept</th><th>Timeframe</th><th>Bars per bucket</th>
        <th>Fingerprint</th></tr></thead>
      <tbody>{horizon.profiles.map(profile=><tr key={profile.profile_id}
        data-horizon-profile={profile.profile_id}
        data-timeframe={profile.timeframe?.name}
        data-concept={profile.concept}>
        <td>{profile.profile_id}</td>
        <td>{profile.concept}</td>
        <td>{text(profile.timeframe?.name)}</td>
        <td>{text(profile.timeframe?.base_bars)}</td>
        <td><code>{text(profile.fingerprint).slice(0,16)}…</code></td></tr>)}
      </tbody></table></div>
  </>;
}

/**
 * Forward microstructure capture (MVP 0.2.4). Candle history records a price range per
 * interval and never a sequence, so it cannot show whether a resting order was ahead of the
 * trades that printed at its price. That evidence only exists forward, and it comes with
 * limits that are stated rather than glossed over.
 */
function MicrostructurePanel({research}:{research:StrategyResearchView}){
  const capture=research.microstructure_capture;
  if(!capture)return null;
  return <>
    <h3>Forward microstructure capture</h3>
    <p>Order-book and trade-tape evidence collected read-only, kept separate from the
      Production ledger. Queue position cannot be recovered from public data, so a passive
      fill can be bounded but never claimed exactly. Anomalies are recorded, never repaired:
      a forward-filled gap would look complete and would flatter a maker fill.</p>
    <div className="table"><table><tbody>
      <tr><th>Capturing</th><td data-capture-enabled={capture.enabled}>
        {capture.enabled?'YES':'NO'}</td></tr>
      <tr><th>Provenance</th>
        <td data-capture-provenance={capture.provenance}>{capture.provenance}</td></tr>
      <tr><th>Endpoints</th><td>{capture.endpoints.join(', ')}</td></tr>
      <tr><th>Order capability</th>
        <td data-order-capability={capture.order_capability}>
          {capture.order_capability}</td></tr>
      <tr><th>Production mutation</th>
        <td data-capture-mutation={capture.production_mutation}>
          {capture.production_mutation}</td></tr>
      <tr><th>Separate from Production ledger</th>
        <td data-capture-separate={capture.separate_from_production_ledger}>
          {capture.separate_from_production_ledger?'YES':'NO'}</td></tr>
      <tr><th>Books captured</th><td>{capture.captured_books.join(', ')||'NONE'}</td></tr>
      <tr><th>Order-book events</th>
        <td data-book-events={capture.order_book_events}>{capture.order_book_events}</td></tr>
      <tr><th>Trade-tape events</th>
        <td data-trade-events={capture.trade_tape_events}>{capture.trade_tape_events}</td></tr>
      <tr><th>Capture duration (s)</th><td>{capture.capture_seconds}</td></tr>
      <tr><th>Health</th><td data-capture-health={capture.health}>{capture.health}</td></tr>
      <tr><th>Gaps</th><td data-capture-gaps={capture.gaps}>{capture.gaps}</td></tr>
      <tr><th>Duplicates</th><td>{capture.duplicates}</td></tr>
      <tr><th>Sequence regressions</th><td>{capture.sequence_regressions}</td></tr>
      <tr><th>Anomalies repaired</th>
        <td data-anomalies-repaired={capture.anomalies_repaired}>
          {capture.anomalies_repaired}</td></tr>
      <tr><th>Bounded storage</th>
        <td data-bounded-storage={capture.bounded_storage}>
          {capture.bounded_storage?`YES (${capture.retention_hours}h retention)`:'NO'}</td></tr>
      <tr><th>Queue position observable</th>
        <td data-queue-observable={capture.queue_position_observable}>
          {capture.queue_position_observable?'YES':'NO'}</td></tr>
      <tr><th>Passive fill exactness</th>
        <td data-passive-exactness={capture.passive_fill_exactness}>
          {capture.passive_fill_exactness}</td></tr>
      <tr><th>Adverse-selection analysis possible</th>
        <td data-adverse-selection={capture.adverse_selection_analysis_possible}>
          {capture.adverse_selection_analysis_possible?'YES':'NO'}</td></tr>
      <tr><th>Exact passive fill claimed</th>
        <td data-exact-fill-claimed={capture.exact_passive_fill_claim_made}>
          {capture.exact_passive_fill_claim_made?'YES':'NO'}</td></tr>
    </tbody></table></div>
  </>;
}

/**
 * Asymmetric opportunity research (MVP 0.2.5). Reports a predeclared experiment and the
 * arithmetic that constrains it, not a result: the verdict comes from a frozen manifest
 * evaluated on a holdout, and until that has run there is nothing to claim. The gate's
 * subordination is stated explicitly because a research gate that looked authoritative would
 * invite someone to rely on it.
 */
function AsymmetryPanel({research}:{research:StrategyResearchView}){
  const asym=research.asymmetry_research;
  if(!asym)return null;
  const params=(p:AsymmetryProfileView['parameters'])=>
    Array.isArray(p)?p:Object.entries(p as Record<string,string>);
  return <>
    <h3>Asymmetric opportunity research</h3>
    <p>Entries placed near a genuine invalidation boundary, so the risk is the distance to a
      real price structure rather than a volatility multiple. The unchanged risk gate nets
      friction from both the reward and the risk path, so the gross move it demands starts at
      friction itself — which is why a narrow, honestly-placed stop is the only lever that
      changes the geometry.</p>
    <div className="table"><table><tbody>
      <tr><th>Hypothesis</th><td>{asym.hypothesis}</td></tr>
      <tr><th>Challengers</th>
        <td data-challenger-count={asym.challengers.length}>{asym.challengers.length}</td></tr>
      <tr><th>Max configurations per challenger</th>
        <td data-asym-config-budget={asym.max_configurations_per_challenger}>
          {asym.max_configurations_per_challenger}</td></tr>
      <tr><th>All configurations retained</th>
        <td data-asym-all-retained={asym.all_configurations_retained}>
          {asym.all_configurations_retained?'YES':'NO'}</td></tr>
      <tr><th>Thresholds chosen before results</th>
        <td data-asym-thresholds-frozen={asym.thresholds_chosen_before_results}>
          {asym.thresholds_chosen_before_results?'YES':'NO'}</td></tr>
      <tr><th>Reward/risk band tested</th>
        <td data-asym-thresholds={asym.reward_risk_thresholds.join(',')}>
          {asym.reward_risk_thresholds.join(', ')}</td></tr>
      <tr><th>Certifying execution mode</th>
        <td data-asym-certifying-mode={asym.certifying_execution_mode}>
          {asym.certifying_execution_mode}</td></tr>
      <tr><th>Maker bound computed</th>
        <td data-asym-maker-bound={asym.maker_bound_computed}>
          {asym.maker_bound_computed?'YES':'NO'}</td></tr>
      <tr><th>Gate is research only</th>
        <td data-asym-gate-research-only={asym.gate_is_research_only}>
          {asym.gate_is_research_only?'YES':'NO'}</td></tr>
      <tr><th>Can override EconomicEdgeGuard</th>
        <td data-asym-overrides-economic={asym.gate_can_override_economic_guard}>
          {asym.gate_can_override_economic_guard?'YES':'NO'}</td></tr>
      <tr><th>Can override RiskEngine</th>
        <td data-asym-overrides-risk={asym.gate_can_override_risk_engine}>
          {asym.gate_can_override_risk_engine?'YES':'NO'}</td></tr>
      <tr><th>Can widen risk policy</th>
        <td data-asym-overrides-policy={asym.gate_can_override_risk_policy}>
          {asym.gate_can_override_risk_policy?'YES':'NO'}</td></tr>
      <tr><th>Structure uses only past data</th>
        <td data-asym-no-future-structure={asym.no_future_structure}>
          {asym.no_future_structure?'YES':'NO'}</td></tr>
      <tr><th>RiskEngine changed</th>
        <td data-asym-risk-changed={asym.risk_engine_changed}>
          {asym.risk_engine_changed?'YES':'NO'}</td></tr>
      <tr><th>Drawdown policy changed</th>
        <td data-asym-drawdown-changed={asym.drawdown_policy_changed}>
          {asym.drawdown_policy_changed?'YES':'NO'}</td></tr>
      <tr><th>Verdict</th>
        <td data-asym-verdict-pending={asym.verdict_pending_frozen_experiment}>
          {asym.verdict??'PENDING FROZEN EXPERIMENT'}</td></tr>
    </tbody></table></div>

    <p className="policy">Why the band is {asym.reward_risk_thresholds.join(' to ')}: {asym.threshold_basis}</p>

    <h4>Asymmetric challengers</h4>
    <div className="table"><table>
      <thead><tr><th>Profile</th><th>Concept</th><th>Timeframe</th>
        <th>Max risk (bps)</th><th>Holding limit</th></tr></thead>
      <tbody>{asym.challengers.map(profile=><tr key={profile.profile_id}
        data-asym-profile={profile.profile_id} data-concept={profile.concept}>
        <td>{profile.profile_id}</td>
        <td>{profile.concept}</td>
        <td>{text(profile.timeframe?.name)}</td>
        <td>{text(Object.fromEntries(params(profile.parameters)).max_risk_bps)}</td>
        <td>{text(Object.fromEntries(params(profile.parameters)).max_holding_bars)}</td>
      </tr>)}
      </tbody></table></div>
  </>;
}

export function StrategyResearchPage({research}:
  {research:StrategyResearchView|undefined|null}){
  if(!research)return <article>
    <h2>Strategy research</h2>
    <p className="warn">No strategy research evidence published yet.</p>
    <p>Profiles are versioned and fingerprinted. The economic guard remains the sole authority
      on whether a proposed trade is financially admissible; no economically viable
      opportunity means no trade.</p></article>;
  return <article className="strategy-research">
    <h2>Strategy research</h2>
    <div className="real-money" role="status">
      <strong>RESEARCH ONLY</strong>
      <p>Promotion {research.promotion} · Multi-market Production
        {' '}{research.multi_market_production} · Production remains BTC/MXN</p>
    </div>
    <p>Each profile proposes an opportunity and states the gross price move it intends to
      capture. <b>EconomicEdgeGuard</b> independently decides whether that proposal is
      admissible after real fees, spread and depth-walked slippage. A profile whose intended
      move cannot clear friction reports NOT_VIABLE. No economically viable opportunity means
      no trade, and nothing here is promoted automatically.</p>

    <h3>Champion</h3>
    <div className="table"><table><tbody>
      <tr><th>Profile</th><td>{research.champion.profile_id}</td></tr>
      <tr><th>Version</th><td>{research.champion.version}</td></tr>
      <tr><th>Certification</th><td>{research.champion.certification_status}</td></tr>
      <tr><th>Lifecycle</th><td>{LIFECYCLE_LABEL.ACTIVE_PRODUCTION}</td></tr>
      <tr><th>Fingerprint</th><td><code>{research.champion.fingerprint}</code></td></tr>
    </tbody></table></div>

    <ChampionViability research={research}/>
    <ProfileTable research={research}/>
    <RiskAdjustedPanel research={research}/>
    <ExecutionResearchPanel research={research}/>
    <HorizonPanel research={research}/>
    <MicrostructurePanel research={research}/>
    <AsymmetryPanel research={research}/>
    <ShadowTable rows={research.shadow}/>
    <MarketTable research={research}/>

    <p className="policy">Research contract {research.research_version} · Portfolio-level
      selection contract prepared for MVP 0.2 (not activated).</p>
  </article>;
}
