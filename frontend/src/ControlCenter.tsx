import {useEffect,useState} from 'react';

/**
 * Research Control Center (MVP 0.3.0).
 *
 * This page exists to keep four facts apart that are easy to blur together, and that
 * become actively misleading when they are:
 *
 *   ENGINEERING STATE          is the system running correctly?
 *   RESEARCH-ECONOMIC EVIDENCE is there a measured, economically usable edge?
 *   OPERATIONAL AUTHORIZATION  has a human authorized a live session?
 *   CURRENT ACTION             what is AutoFund doing right now?
 *
 * A healthy process with no edge is the normal state of this project, not a
 * contradiction, so the four are rendered as four separate labelled blocks. Nothing
 * here combines them, and no field on this page is derived from another dimension:
 * a green SYSTEM panel must never read as profitable evidence, and a predictive
 * research finding must never read as permission to trade.
 *
 * There are no BUY/SELL controls and no order ticket. The Control Center answers
 * questions; the control plane owns session lifecycle. Every value shown is either a
 * real measurement or the literal sentinel the backend published - `UNKNOWN`,
 * `NOT_RECORDED` or `NOT_APPLICABLE` - because inventing a plausible number for a
 * missing measurement is worse than showing that it is missing.
 */

/** Render a value, or the honest sentinel the backend sent. Never a fabricated default. */
function v(value:unknown):string{
  if(value===null||value===undefined||value==='')return 'UNKNOWN';
  if(typeof value==='boolean')return value?'YES':'NO';
  return String(value);
}

/** A measurement that has no source is `UNKNOWN`; one that cannot exist is `NOT_APPLICABLE`. */
function bps(value:unknown):string{
  return value===null||value===undefined||value==='' ? 'UNKNOWN' : `${value} bps`;
}

export interface ResearchStatusView{
  engineering:string; research:string; production:string; current_action:string;
  built_at:string; statuses_are_independent:boolean; note:string;
}
export interface PortfolioView{
  cash_mxn:string; equity_mxn:string; deployed_mxn:string; remaining_deployment_mxn:string;
  authorized_capital_mxn:string; max_deployment_mxn:string; single_order_cap_mxn:string;
  realized_pnl_mxn:string; unrealized_pnl_mxn:string|null; fees_mxn:string;
  fills:number; orders:number;
  autofund_inventory:{asset?:string;quantity?:string;average_cost_mxn?:string;market_value_mxn?:string}[];
  exchange_wallet:{status:string;error:string|null;read_only:boolean;observed_at:string|null;
    balances:{currency:string;total:string;available:string;locked:string;approx_mxn:string|null}[]};
  unresolved_orders:string[]; has_unresolved_order:boolean; accounting_status:string;
  wallet_is_not_inventory:boolean; source:string;
}
export interface EngineeringBlock{
  app_state:string; execution:string; ledger:string; market_data:string; collectors:string;
  control_state:string; overall:string; detail:Record<string,unknown>;
  says_nothing_about_profitability:boolean;
}
export interface ResearchBlock{
  price_only:string; cross_market_alpha:string; microstructure:string; cross_venue:string;
  experiments_total:number; experiments_completed:number;
  validated_information_signals:number; economically_usable_signals:number;
  strategies_frozen:number; strategies_not_viable:number; strategies_active_production:number;
  active_campaigns:string[]; accumulating_campaigns:string[];
}
export interface ProductionBlock{
  certified_opportunities:number; production_eligible:number; session_authorized:boolean;
  unresolved_orders:number; current_action:string; reasons:string[];
  authorization_state:string; demo_mode:boolean; auto_execution:boolean;
  evidence_does_not_imply_authorization:boolean;
}
export interface ResearchOverview{
  schema_version:string; status:ResearchStatusView; portfolio:PortfolioView;
  engineering:EngineeringBlock; research:ResearchBlock; production:ProductionBlock;
  latest_experiment:Record<string,unknown>|null; latest_evidence:Record<string,unknown>|null;
}
export interface AlphaRecord{
  alpha_id:string; name:string; family:string; version:string; fingerprint:string|null;
  classification:string; prediction_status:string; economic_status:string;
  markets:string[]; prediction_horizon:string; development_status:string;
  validation_status:string; movement_bps:string|null; required_friction_bps:string|null;
  economic_headroom_bps:string|null; tradeable:boolean|null; evidence_provenance:string[];
  reason_codes:string[]; source_experiment:string; artifact_refs:string[];
  created_at:string|null; frozen_at:string|null; is_a_strategy:boolean;
  hypothesis:string; definition:string;
}
export interface StrategyRecord{
  profile_id:string; strategy_id:string; version:string; fingerprint:string|null; status:string;
  markets_evaluated:string[]; timeframes:string[]; alpha_dependency:string|null;
  entry_model:string; exit_model:string; risk_model:string; economic_policy_fingerprint:string;
  development_evidence:Record<string,unknown>; holdout_evidence:Record<string,unknown>;
  forward_shadow_evidence:Record<string,unknown>; production_certification:string;
  reason_codes:string[]; predecessor_profile:string|null; successor_profile:string|null;
  frozen:boolean; is_frozen_terminal:boolean;
  engineering_status:string; economic_status:string; risk_status:string; production_eligible:boolean;
  round_trips:number|null; net_pnl_mxn:string|null; max_drawdown_mxn:string|null;
  median_mae_mxn:string|null; median_mfe_mxn:string|null;
  economic_rejection_rate:string|null; risk_rejection_rate:string|null;
  mae_mfe_available:boolean; artifact_refs:string[];
}
export interface ExperimentRecord{
  experiment_id:string; milestone:string; title:string; hypothesis:string;
  baseline_commit:string|null; fingerprint:string|null; status:string;
  started_at:string|null; finished_at:string|null;
  development_interval:Record<string,unknown>|null; holdout_interval:Record<string,unknown>|null;
  forward_interval:Record<string,unknown>|null;
  markets:string[]; strategies:string[]; alpha_sources:string[];
  economic_policy_fingerprint:string|null; risk_policy_fingerprint:string|null;
  dataset_fingerprints:string[]; artifact_refs:string[];
  result_classification:string; reason_codes:string[]; test_summary:string|null;
  superseded_by:string|null; supersedes:string[];
}
export interface EvidenceRecord{
  evidence_id:string; provenance:string; experiment_id:string|null; dataset_fingerprint:string|null;
  start:string|null; end:string|null; markets:string[]; observation_count:number|null;
  quality:string; gaps:number|null; artifact_path:string; artifact_bytes:number|null;
  summary:Record<string,unknown>; alpha_sources:string[]; strategies:string[];
  is_real_observation:boolean;
}
export interface CampaignRecord{
  campaign_id:string; title:string; experiment_id:string; experiment_fingerprint:string|null;
  status:string; process_health:string; evidence_conclusion:string;
  planned_duration_hours:string; actual_coverage_hours:string; coverage_percent:string;
  markets:string[]; observations:number; gaps:number; latest_sample_at:string|null;
  storage_bytes:number; evidence_provenance:string; coverage_sufficient:boolean;
  artifact_paths:string[]; health_is_separate_from_conclusion:boolean;
}
export interface EligibilityRecord{
  market:string; profile_id:string; alpha_source:string|null; research_status:string;
  data_quality:string; strategy_compatibility:string; economic_status:string; risk_status:string;
  certification_status:string; authorization_status:string; position_status:string;
  blocking_reason:string; eligible:boolean;
}
export interface TimelineEvent{at:string;kind:string;subject:string;detail:string;source:string}
export interface ArtifactRecord{
  relative_path:string; artifact_type:string; status:string; archive_state:string;
  size_bytes:number|null; modified_at:string|null; fingerprint:string|null;
  experiment_id:string|null; summary:Record<string,unknown>|null; error:string|null;
}
export interface Paged<T>{total:number;offset:number;limit:number;returned:number;items:T[]}

/** Fetch a read-only research resource. There is no mutation path in this module. */
export function useResearch<T>(path:string,query=''){
  const[data,setData]=useState<T|null>(null),[error,setError]=useState(''),[loading,setLoading]=useState(true);
  useEffect(()=>{
    let active=true;
    setLoading(true);setError('');
    fetch(`/api/v1/research${path}${query}`)
      .then(async r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json() as Promise<T>})
      .then(x=>{if(active){setData(x);setLoading(false)}})
      .catch((e:Error)=>{if(active){setError(e.message);setLoading(false)}});
    return()=>{active=false};
  },[path,query]);
  return {data,error,loading};
}

/** Shared read-only frame: loading, failure and absence are all explicit states. */
function Frame({title,error,loading,children,note}:
  {title:string;error:string;loading:boolean;children:React.ReactNode;note?:string}){
  return <article className="control-center">
    <h2>{title}</h2>
    {note&&<p className="cc-note">{note}</p>}
    {loading&&<p role="status">Loading…</p>}
    {!loading&&error&&<p className="error" role="alert">
      Research registry unavailable: {error}. The registry is a read model over local artifacts;
      a failure here says nothing about trading state.</p>}
    {!loading&&!error&&children}
  </article>;
}

/** One of the four separated facts. Each dimension gets its own visual identity. */
function Truth({dimension,value,hint}:{dimension:string;value:string;hint:string}){
  return <div className={`cc-truth cc-${dimension.toLowerCase()}`}
    data-dimension={dimension.toUpperCase()} data-value={value}>
    <small>{dimension}</small><b>{v(value)}</b><span>{hint}</span>
  </div>;
}

function Cards({rows}:{rows:[string,React.ReactNode][]}){
  return <div className="cards">{rows.map(([k,val])=>
    <article key={k}><small>{k}</small><b>{val}</b></article>)}</div>;
}

/** Portfolio: the AutoFund book and the exchange account are never summed. */
export function PortfolioPanel({portfolio}:{portfolio:PortfolioView}){
  return <div className="cc-panel"
    data-wallet-is-not-inventory={portfolio.wallet_is_not_inventory}
    data-autofund-inventory-count={portfolio.autofund_inventory.length}
    data-exchange-wallet-balances={portfolio.exchange_wallet.balances.length}>
    <h3>Portfolio</h3>
    <p className="cc-note">AutoFund inventory is derived only from confirmed AutoFund fills.
      The exchange account is shown separately and is never added to it: summing the two would
      overstate deployable capital while looking entirely reasonable.</p>
    <Cards rows={[
      ['Authorized capital',`${portfolio.authorized_capital_mxn} MXN`],
      ['Max deployment',`${portfolio.max_deployment_mxn} MXN`],
      ['Single order cap',`${portfolio.single_order_cap_mxn} MXN`],
      ['Remaining deployment',`${portfolio.remaining_deployment_mxn} MXN`],
      ['Cash',`${portfolio.cash_mxn} MXN`],['Deployed',`${portfolio.deployed_mxn} MXN`],
      ['Equity',`${portfolio.equity_mxn} MXN`],
      ['Realized P&L',`${portfolio.realized_pnl_mxn} MXN`],
    ]}/>
    <div className="table"><table><tbody>
      <tr><th>Accounting status</th><td>{v(portfolio.accounting_status)}</td></tr>
      <tr><th>Unresolved order</th>
        <td>{portfolio.has_unresolved_order?portfolio.unresolved_orders.join(', '):'NONE'}</td></tr>
      <tr><th>Source</th><td>{portfolio.source}</td></tr>
    </tbody></table></div>
    <h4>AutoFund inventory</h4>
    {portfolio.autofund_inventory.length
      ?<div className="table"><table><thead><tr><th>Asset</th><th>Quantity</th>
        <th>Average cost MXN</th><th>Market value MXN</th></tr></thead><tbody>
        {portfolio.autofund_inventory.map(row=><tr key={row.asset??'position'}>
          <td>{v(row.asset)}</td><td>{v(row.quantity)}</td><td>{v(row.average_cost_mxn)}</td>
          <td>{v(row.market_value_mxn)}</td></tr>)}</tbody></table></div>
      :<p>No AutoFund-owned inventory. Position NONE.</p>}
    <h4>Exchange account — separate, read only</h4>
    <p className="cc-note">Status {v(portfolio.exchange_wallet.status)} · read only{' '}
      {portfolio.exchange_wallet.read_only?'YES':'NO'}
      {portfolio.exchange_wallet.error?` · ${portfolio.exchange_wallet.error}`:''}</p>
    {portfolio.exchange_wallet.balances.length
      ?<div className="table"><table><thead><tr><th>Currency</th><th>Total</th><th>Available</th>
        <th>Locked</th><th>Approx MXN</th></tr></thead><tbody>
        {portfolio.exchange_wallet.balances.map(row=><tr key={row.currency}>
          <td>{row.currency}</td><td>{row.total}</td><td>{row.available}</td><td>{row.locked}</td>
          <td>{v(row.approx_mxn)}</td></tr>)}</tbody></table></div>
      :<p>No exchange balances published.</p>}
  </div>;
}

/** Engineering health. Explicitly says nothing about profitability. */
export function EngineeringPanel({engineering}:{engineering:EngineeringBlock}){
  return <div className="cc-panel"
    data-says-nothing-about-profitability={engineering.says_nothing_about_profitability}>
    <h3>System</h3>
    <p className="cc-note">A healthy process is not evidence of profit. This panel reports whether
      AutoFund is running correctly, and nothing about whether it can make money.</p>
    <div className="table"><table><tbody>
      <tr><th>Process</th><td>{v(engineering.overall)}</td></tr>
      <tr><th>Control state</th><td>{v(engineering.control_state)}</td></tr>
      <tr><th>Execution</th><td>{v(engineering.execution)}</td></tr>
      <tr><th>Ledger</th><td>{v(engineering.ledger)}</td></tr>
      <tr><th>Market data</th><td>{v(engineering.market_data)}</td></tr>
      <tr><th>Collectors</th><td>{v(engineering.collectors)}</td></tr>
      <tr><th>Blocked recovery</th><td>{v(engineering.detail?.blocked_recovery)}</td></tr>
      <tr><th>Runtime gaps</th><td>{v(engineering.detail?.runtime_gaps)}</td></tr>
    </tbody></table></div>
  </div>;
}

/** Research evidence: is there a measured, economically usable edge? */
export function ResearchPanel({research}:{research:ResearchBlock}){
  return <>
    <h3>Research evidence</h3>
    <p className="cc-note">A predictive signal is not a tradeable one. {research.validated_information_signals}
      {' '}validated information signal{research.validated_information_signals===1?'':'s'} and{' '}
      {research.economically_usable_signals} economically usable.</p>
    <div className="table"><table><tbody>
      <tr><th>Price-only families</th><td>{v(research.price_only)}</td></tr>
      <tr><th>Cross-market alpha</th><td>{v(research.cross_market_alpha)}</td></tr>
      <tr><th>Microstructure</th><td>{v(research.microstructure)}</td></tr>
      <tr><th>Cross-venue</th><td>{v(research.cross_venue)}</td></tr>
      <tr><th>Validated information signals</th><td>{research.validated_information_signals}</td></tr>
      <tr><th>Economically usable signals</th><td>{research.economically_usable_signals}</td></tr>
      <tr><th>Strategies frozen</th><td>{research.strategies_frozen}</td></tr>
      <tr><th>Strategies not viable</th><td>{research.strategies_not_viable}</td></tr>
      <tr><th>Strategies in production</th><td>{research.strategies_active_production}</td></tr>
      <tr><th>Experiments</th>
        <td>{research.experiments_completed} / {research.experiments_total} completed</td></tr>
      <tr><th>Accumulating campaigns</th>
        <td>{research.accumulating_campaigns.length
          ?research.accumulating_campaigns.join(', '):'NONE'}</td></tr>
      <tr><th>Active campaigns</th>
        <td>{research.active_campaigns.length?research.active_campaigns.join(', '):'NONE'}</td></tr>
    </tbody></table></div>
  </>;
}

/** Operational authorization. Positive evidence does not appear here as permission. */
export function ProductionPanel({production}:{production:ProductionBlock}){
  return <div className="cc-panel"
    data-evidence-does-not-imply-authorization={production.evidence_does_not_imply_authorization}
    data-session-authorized={production.session_authorized}>
    <h3>Production readiness</h3>
    <p className="cc-note">Certified opportunities and a certified strategy are both required.
      Positive research evidence cannot authorize a session, and no number on this page is derived
      from the research block.</p>
    <div className="table"><table><tbody>
      <tr><th>Certified opportunities</th><td>{production.certified_opportunities}</td></tr>
      <tr><th>Production eligible</th><td>{production.production_eligible}</td></tr>
      <tr><th>Session authorized</th>
        <td>{production.session_authorized?'YES':'NO'}</td></tr>
      <tr><th>Authorization state</th><td>{v(production.authorization_state)}</td></tr>
      <tr><th>Auto execution</th><td>{production.auto_execution?'ON':'OFF'}</td></tr>
      <tr><th>Demo mode</th><td>{production.demo_mode?'YES':'NO'}</td></tr>
      <tr><th>Unresolved orders</th><td>{production.unresolved_orders}</td></tr>
      <tr><th>Reasons</th>
        <td>{production.reasons.length?production.reasons.join(', '):'NONE'}</td></tr>
    </tbody></table></div>
  </div>;
}

export function ControlCenterPage(){
  const{data,error,loading}=useResearch<ResearchOverview>('/overview');
  return <Frame title="Research control center" loading={loading} error={error}
    note={data?.status.note}>
    {data&&<>
      <div className="cc-truths">
        <Truth dimension="engineering" value={data.status.engineering}
          hint="is the system running correctly"/>
        <Truth dimension="research" value={data.status.research}
          hint="is there measured, usable evidence"/>
        <Truth dimension="production" value={data.status.production}
          hint="has a session been authorized"/>
        <Truth dimension="action" value={data.status.current_action}
          hint="what AutoFund is doing now"/>
      </div>
      <p className="cc-invariant" role="status"
        data-statuses-independent={data.status.statuses_are_independent}>
        These four statuses are independent facts and are never combined.
        {data.status.statuses_are_independent?' Separation enforced.':' '}
      </p>
      <dl className="cc-meta"><dt>Registry schema</dt><dd>{data.schema_version}</dd>
        <dt>Built at</dt><dd>{data.status.built_at}</dd></dl>
      <PortfolioPanel portfolio={data.portfolio}/>
      <EngineeringPanel engineering={data.engineering}/>
      <ResearchPanel research={data.research}/>
      <ProductionPanel production={data.production}/>
      {data.engineering.says_nothing_about_profitability&&
        <p className="cc-note">Engineering health is reported with no claim about
          profitability.</p>}
      {data.production.evidence_does_not_imply_authorization&&
        <p className="cc-note">Research evidence does not imply authorization.</p>}
    </>}
  </Frame>;
}

/** Alpha Registry: information sources, with prediction and economics reported separately. */
export function AlphaRegistryPage(){
  const{data,error,loading}=useResearch<Paged<AlphaRecord>>('/alpha');
  return <Frame title="Alpha registry" loading={loading} error={error}
    note="An alpha source is information. It is not a strategy, and a source that predicts is not
      automatically a source you can trade.">
    {data&&<>
      <p className="cc-note">{data.total} registered source{data.total===1?'':'s'}.</p>
      <div className="table"><table><thead><tr><th>Alpha</th><th>Family</th><th>Classification</th>
        <th>Prediction</th><th>Economic</th><th>Tradeable</th>
        <th>Movement</th><th>Required friction</th><th>Headroom</th></tr></thead><tbody>
        {data.items.map(a=><tr key={a.alpha_id} data-alpha={a.alpha_id}
          data-classification={a.classification} data-tradeable={a.tradeable}
          data-prediction={a.prediction_status} data-economic={a.economic_status}>
          <td>{a.alpha_id}</td><td>{a.family}</td><td>{a.classification}</td>
          <td>{a.prediction_status}</td><td>{a.economic_status}</td>
          <td className={a.tradeable?'positive':'negative'}>{v(a.tradeable)}</td>
          <td>{bps(a.movement_bps)}</td><td>{bps(a.required_friction_bps)}</td>
          <td className={(a.economic_headroom_bps??'0').startsWith('-')?'negative':'positive'}>
            {bps(a.economic_headroom_bps)}</td></tr>)}</tbody></table></div>
      <h3>Detail</h3>
      {data.items.map(a=><article key={a.alpha_id}>
        <h4>{a.alpha_id}</h4>
        <p>{a.hypothesis}</p>
        <dl><dt>Name</dt><dd>{a.name}</dd><dt>Version</dt><dd>{v(a.version)}</dd>
          <dt>Fingerprint</dt><dd className="cc-fp">{v(a.fingerprint)}</dd>
          <dt>Markets</dt><dd>{a.markets.length?a.markets.join(', '):'NOT_APPLICABLE'}</dd>
          <dt>Horizon</dt><dd>{v(a.prediction_horizon)}</dd>
          <dt>Development</dt><dd>{v(a.development_status)}</dd>
          <dt>Validation</dt><dd>{v(a.validation_status)}</dd>
          <dt>Provenance</dt>
          <dd>{a.evidence_provenance.length?a.evidence_provenance.join(', '):'NOT_RECORDED'}</dd>
          <dt>Source experiment</dt><dd>{v(a.source_experiment)}</dd>
          <dt>Reasons</dt><dd>{a.reason_codes.length?a.reason_codes.join(', '):'NONE'}</dd>
          <dt>Created</dt><dd>{v(a.created_at)}</dd><dt>Frozen</dt><dd>{v(a.frozen_at)}</dd>
          <dt>Is a strategy</dt><dd>{a.is_a_strategy?'YES':'NO'}</dd>
          <dt>Definition</dt><dd>{a.definition}</dd></dl>
      </article>)}
    </>}
  </Frame>;
}

/** Strategy Registry: the four verdicts on one row, deliberately not merged. */
export function StrategyRegistryPage(){
  const{data,error,loading}=useResearch<Paged<StrategyRecord>>('/strategies');
  return <Frame title="Strategy registry" loading={loading} error={error}
    note="Engineering, economic, risk and production verdicts are four separate columns. A strategy
      can pass engineering and fail economics, and here several do.">
    {data&&<>
      <p className="cc-note">{data.total} registered strateg{data.total===1?'y':'ies'}.</p>
      <div className="table"><table><thead><tr><th>Profile</th><th>Strategy</th><th>Status</th>
        <th>Engineering</th><th>Economic</th><th>Risk</th><th>Production eligible</th>
        <th>Frozen</th></tr></thead><tbody>
        {data.items.map(s=><tr key={s.profile_id} data-profile={s.profile_id}
          data-engineering={s.engineering_status} data-economic={s.economic_status}
          data-risk={s.risk_status} data-production-eligible={s.production_eligible}
          data-frozen-terminal={s.is_frozen_terminal}>
          <td>{s.profile_id}</td><td>{s.strategy_id}</td><td>{s.status}</td>
          <td className={s.engineering_status==='PASS'?'positive':'negative'}>
            {s.engineering_status}</td>
          <td className={s.economic_status==='PASS'?'positive':'negative'}>{s.economic_status}</td>
          <td className={s.risk_status==='PASS'?'positive':'negative'}>{s.risk_status}</td>
          <td className={s.production_eligible?'positive':'negative'}>
            {s.production_eligible?'YES':'NO'}</td>
          <td>{s.is_frozen_terminal?'FROZEN TERMINAL':(s.frozen?'FROZEN':'NO')}</td>
        </tr>)}</tbody></table></div>
      <h3>Detail</h3>
      {data.items.map(s=><article key={s.profile_id}>
        <h4>{s.profile_id}</h4>
        <dl><dt>Fingerprint</dt><dd className="cc-fp">{v(s.fingerprint)}</dd>
          <dt>Markets evaluated</dt>
          <dd>{s.markets_evaluated.length?s.markets_evaluated.join(', '):'NOT_APPLICABLE'}</dd>
          <dt>Timeframes</dt><dd>{s.timeframes.length?s.timeframes.join(', '):'NOT_APPLICABLE'}</dd>
          <dt>Alpha dependency</dt><dd>{v(s.alpha_dependency)}</dd>
          <dt>Entry</dt><dd>{v(s.entry_model)}</dd><dt>Exit</dt><dd>{v(s.exit_model)}</dd>
          <dt>Risk</dt><dd>{v(s.risk_model)}</dd>
          <dt>Economic policy</dt><dd>{v(s.economic_policy_fingerprint)}</dd>
          <dt>Production certification</dt><dd>{v(s.production_certification)}</dd>
          <dt>Development evidence</dt>
          <dd>{Object.keys(s.development_evidence).length
            ?JSON.stringify(s.development_evidence):'NOT_RECORDED'}</dd>
          <dt>Holdout evidence</dt>
          <dd>{Object.keys(s.holdout_evidence).length
            ?JSON.stringify(s.holdout_evidence):'NOT_RECORDED'}</dd>
          <dt>Forward shadow evidence</dt>
          <dd>{Object.keys(s.forward_shadow_evidence).length
            ?JSON.stringify(s.forward_shadow_evidence):'NOT_RECORDED'}</dd>
          <dt>Round trips</dt><dd>{v(s.round_trips)}</dd>
          <dt>Net P&amp;L</dt><dd>{s.net_pnl_mxn===null?'UNKNOWN':`${s.net_pnl_mxn} MXN`}</dd>
          <dt>Max drawdown</dt>
          <dd>{s.max_drawdown_mxn===null?'UNKNOWN':`${s.max_drawdown_mxn} MXN`}</dd>
          <dt>Median MAE / MFE</dt>
          <dd>{s.mae_mfe_available
            ?`${v(s.median_mae_mxn)} / ${v(s.median_mfe_mxn)} MXN`
            :'NOT_RECORDED — no capture exists that could produce these'}</dd>
          <dt>Economic rejection rate</dt><dd>{v(s.economic_rejection_rate)}</dd>
          <dt>Risk rejection rate</dt><dd>{v(s.risk_rejection_rate)}</dd>
          <dt>Reasons</dt><dd>{s.reason_codes.length?s.reason_codes.join(', '):'NONE'}</dd>
          <dt>Predecessor / successor</dt>
          <dd>{v(s.predecessor_profile)} → {v(s.successor_profile)}</dd>
        </dl>
      </article>)}
    </>}
  </Frame>;
}

export function ExperimentRegistryPage(){
  const{data,error,loading}=useResearch<Paged<ExperimentRecord>>('/experiments');
  return <Frame title="Experiment registry" loading={loading} error={error}
    note="Every milestone that produced evidence is registered with its hypothesis and its
      outcome. A negative result is a result.">
    {data&&<>
      <p className="cc-note">{data.total} registered experiment{data.total===1?'':'s'}.</p>
      <div className="table"><table><thead><tr><th>Milestone</th><th>Title</th><th>Status</th>
        <th>Result</th><th>Alpha sources</th><th>Finished</th></tr></thead><tbody>
        {data.items.map(e=><tr key={e.experiment_id}>
          <td>{e.milestone}</td><td>{e.title}</td><td>{e.status}</td>
          <td>{v(e.result_classification)}</td>
          <td>{e.alpha_sources.length?e.alpha_sources.join(', '):'NONE'}</td>
          <td>{v(e.finished_at)}</td></tr>)}</tbody></table></div>
      <h3>Detail</h3>
      {data.items.map(e=><article key={e.experiment_id}>
        <h4>{e.milestone} — {e.title}</h4>
        <p>{e.hypothesis}</p>
        <dl><dt>Baseline commit</dt><dd>{v(e.baseline_commit)}</dd>
          <dt>Fingerprint</dt><dd className="cc-fp">{v(e.fingerprint)}</dd>
          <dt>Markets</dt><dd>{e.markets.length?e.markets.join(', '):'NOT_APPLICABLE'}</dd>
          <dt>Strategies</dt><dd>{e.strategies.length?e.strategies.join(', '):'NONE'}</dd>
          <dt>Development window</dt>
          <dd>{e.development_interval?JSON.stringify(e.development_interval):'NOT_RECORDED'}</dd>
          <dt>Holdout window</dt>
          <dd>{e.holdout_interval?JSON.stringify(e.holdout_interval):'NOT_RECORDED'}</dd>
          <dt>Forward window</dt>
          <dd>{e.forward_interval?JSON.stringify(e.forward_interval):'NOT_RECORDED'}</dd>
          <dt>Test summary</dt><dd>{v(e.test_summary)}</dd>
          <dt>Supersedes</dt><dd>{e.supersedes.length?e.supersedes.join(', '):'NONE'}</dd>
          <dt>Superseded by</dt><dd>{v(e.superseded_by)}</dd>
          <dt>Artifacts</dt><dd>{e.artifact_refs.length?e.artifact_refs.join(', '):'NONE'}</dd>
        </dl>
        <p className="cc-reasons">Reasons: {e.reason_codes.length
          ?e.reason_codes.join(' · '):'NONE'}</p>
      </article>)}
    </>}
  </Frame>;
}

export function EvidenceExplorerPage(){
  const{data,error,loading}=useResearch<Paged<EvidenceRecord>>('/evidence');
  return <Frame title="Evidence explorer" loading={loading} error={error}
    note="Provenance is the point of this page. A fixture is not a measurement, and only real
      observation can support a conclusion.">
    {data&&<>
      <p className="cc-note">{data.total} evidence record{data.total===1?'':'s'}.</p>
      <div className="table"><table><thead><tr><th>Evidence</th><th>Provenance</th><th>Experiment</th>
        <th>Real observation</th><th>Quality</th><th>Observations</th><th>Markets</th>
        <th>Bytes</th></tr></thead><tbody>
        {data.items.map(r=><tr key={r.evidence_id} data-evidence={r.evidence_id}
          data-provenance={r.provenance} data-real-observation={r.is_real_observation}>
          <td>{r.evidence_id}</td><td>{r.provenance}</td><td>{v(r.experiment_id)}</td>
          <td className={r.is_real_observation?'positive':'negative'}>
            {r.is_real_observation?'YES':'NO'}</td>
          <td>{v(r.quality)}</td><td>{v(r.observation_count)}</td>
          <td>{r.markets.length?r.markets.join(', '):'NOT_APPLICABLE'}</td>
          <td>{r.artifact_bytes===null?'UNKNOWN':r.artifact_bytes.toLocaleString()}</td>
        </tr>)}</tbody></table></div>
      <h3>Lineage</h3>
      {data.items.map(r=><article key={`lineage-${r.evidence_id}`}>
        <h4>{r.evidence_id}</h4>
        <dl><dt>Artifact</dt><dd className="cc-fp">{r.artifact_path}</dd>
          <dt>Dataset fingerprint</dt><dd className="cc-fp">{v(r.dataset_fingerprint)}</dd>
          <dt>Window start / end</dt><dd>{v(r.start)} → {v(r.end)}</dd>
          <dt>Gaps</dt><dd>{v(r.gaps)}</dd>
          <dt>Alpha sources</dt><dd>{r.alpha_sources.length?r.alpha_sources.join(', '):'NONE'}</dd>
          <dt>Strategies</dt><dd>{r.strategies.length?r.strategies.join(', '):'NONE'}</dd>
          <dt>Summary</dt><dd>{Object.keys(r.summary??{}).length
            ?JSON.stringify(r.summary):'NOT_RECORDED'}</dd></dl>
      </article>)}
    </>}
  </Frame>;
}

/** Campaigns: process health and evidence conclusion are two different questions. */
export function CampaignsPage(){
  const{data,error,loading}=useResearch<Paged<CampaignRecord>>('/campaigns');
  return <Frame title="Campaigns" loading={loading} error={error}
    note="A collector can be perfectly healthy and still have produced no usable evidence. Health
      and conclusion are reported separately and never averaged.">
    {data&&<>
      <p className="cc-note">{data.total} campaign{data.total===1?'':'s'}.</p>
      {data.items.map(c=><article key={c.campaign_id} data-campaign={c.campaign_id}
        data-process-health={c.process_health} data-evidence-conclusion={c.evidence_conclusion}
        data-coverage-sufficient={c.coverage_sufficient}>
        <h4>{c.campaign_id} — {c.title}</h4>
        <div className="cc-truths">
          <div className="cc-truth cc-engineering"><small>PROCESS HEALTH</small>
            <b>{v(c.process_health)}</b><span>is the collector running</span></div>
          <div className="cc-truth cc-research"><small>EVIDENCE CONCLUSION</small>
            <b>{v(c.evidence_conclusion)}</b><span>what the data supports</span></div>
        </div>
        <dl><dt>Status</dt><dd>{v(c.status)}</dd>
          <dt>Experiment</dt><dd>{v(c.experiment_id)}</dd>
          <dt>Provenance</dt><dd>{v(c.evidence_provenance)}</dd>
          <dt>Planned duration</dt><dd>{v(c.planned_duration_hours)} hours</dd>
          <dt>Actual coverage</dt><dd>{v(c.actual_coverage_hours)} hours</dd>
          <dt>Coverage</dt><dd>{v(c.coverage_percent)}%</dd>
          <dt>Coverage sufficient</dt>
          <dd className={c.coverage_sufficient?'positive':'negative'}>
            {c.coverage_sufficient?'YES':'NO — a rare event could not have been observed'}</dd>
          <dt>Observations</dt><dd>{c.observations}</dd><dt>Gaps</dt><dd>{c.gaps}</dd>
          <dt>Latest sample</dt><dd>{v(c.latest_sample_at)}</dd>
          <dt>Storage</dt><dd>{c.storage_bytes.toLocaleString()} bytes</dd>
          <dt>Artifacts</dt><dd className="cc-fp">{c.artifact_paths.join(', ')}</dd></dl>
      </article>)}
    </>}
  </Frame>;
}

/** Activity timeline. Financial events live in their own journal and are not duplicated here. */
export function TimelinePage(){
  const{data,error,loading}=useResearch<Paged<TimelineEvent>>('/timeline','?limit=200');
  return <Frame title="Research activity" loading={loading} error={error}
    note="Research and production events only. Financial events keep their
      authoritative source in the production journal and are never copied here, because two
      records of the same fill would eventually disagree.">
    {data&&<div className="table" data-financial-events-included="false">
      <table><thead><tr><th>When</th><th>Kind</th><th>Subject</th>
      <th>Detail</th><th>Source</th></tr></thead><tbody>
      {data.items.map((e,i)=><tr key={`${e.at}-${e.kind}-${i}`}>
        <td>{e.at}</td><td>{e.kind}</td><td>{e.subject}</td><td>{e.detail}</td>
        <td>{e.source}</td></tr>)}</tbody></table></div>}
  </Frame>;
}

/** Production eligibility matrix: why each market and profile pair is or is not tradable. */
export function EligibilityPage(){
  const{data,error,loading}=useResearch<Paged<EligibilityRecord>>('/eligibility','?limit=500');
  return <Frame title="Production eligibility" loading={loading} error={error}
    note="Every pair carries a deterministic blocking reason. A pair that cannot be traded always
      says why, so 'not selected' is never ambiguous.">
    {data&&<div className="table"><table><thead><tr><th>Market</th><th>Profile</th>
      <th>Alpha source</th><th>Research</th><th>Data quality</th><th>Compatibility</th>
      <th>Economic</th><th>Risk</th><th>Certification</th><th>Authorization</th>
      <th>Position</th><th>Eligible</th><th>Blocking reason</th></tr></thead><tbody>
      {data.items.map((r,i)=><tr key={`${r.market}-${r.profile_id}-${i}`} data-market={r.market}
        data-profile={r.profile_id} data-eligible={r.eligible}
        data-blocking-reason={r.blocking_reason}>
        <td>{r.market}</td><td>{r.profile_id}</td><td>{v(r.alpha_source)}</td>
        <td>{v(r.research_status)}</td><td>{v(r.data_quality)}</td>
        <td>{v(r.strategy_compatibility)}</td><td>{v(r.economic_status)}</td>
        <td>{v(r.risk_status)}</td><td>{v(r.certification_status)}</td>
        <td>{v(r.authorization_status)}</td><td>{v(r.position_status)}</td>
        <td className={r.eligible?'positive':'negative'}>{r.eligible?'YES':'NO'}</td>
        <td>{v(r.blocking_reason)}</td></tr>)}</tbody></table></div>}
  </Frame>;
}

/** Artifact index: what evidence physically exists on disk. */
export function ArtifactIndexPage(){
  const{data,error,loading}=useResearch<Paged<ArtifactRecord>>('/artifacts','?limit=200');
  return <Frame title="Artifact index" loading={loading} error={error}
    note="Every certification, capture and telemetry stream the registry can see, with a
      fingerprint so a change is detectable. Derived output is excluded: a read model that indexed
      its own snapshot would change on every rebuild.">
    {data&&<>
      <p className="cc-note">{data.total} indexed artifact{data.total===1?'':'s'}
        {data.total>data.returned?` · showing first ${data.returned}`:''}.</p>
      <div className="table"><table><thead><tr><th>Path</th><th>Type</th><th>Status</th>
        <th>Archive</th><th>Bytes</th><th>Fingerprint</th><th>Error</th></tr></thead><tbody>
        {data.items.map(r=><tr key={r.relative_path}>
          <td>{r.relative_path}</td><td>{r.artifact_type}</td>
          <td className={r.status==='INDEXED'?'positive':'negative'}>{r.status}</td>
          <td>{r.archive_state}</td>
          <td>{r.size_bytes===null?'UNKNOWN':r.size_bytes.toLocaleString()}</td>
          <td className="cc-fp">{(r.fingerprint??'UNKNOWN').slice(0,16)}</td>
          <td>{v(r.error)}</td></tr>)}</tbody></table></div>
    </>}
  </Frame>;
}
