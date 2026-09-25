/**
 * Production Markets: why each market is, or is not, trading.
 *
 * The single most important property of this view is that it never shows an ambiguous
 * "not selected". Every market carries a deterministic reason code, and the visual
 * states are deliberately distinct because collapsing them would misreport the
 * situation:
 *
 *   RESEARCH                discovered, no evidence yet
 *   ACCUMULATING            real evidence being collected
 *   INSUFFICIENT EVIDENCE   enough data could not be gathered
 *   NOT VIABLE              economics demonstrated to fail
 *   CERTIFIED               may be selected
 *   CURRENT OPPORTUNITY     certified and admissible right now
 *   POSITION OPEN           AutoFund owns inventory here
 *
 * The page exposes no BUY/SELL control. Multi-market Production is displayed as
 * DISABLED so the current boundary is visible rather than implied.
 */

export interface ProductionMarketRow {
  book:string; market:string; market_class:string;
  research_status:string; strategy_compatibility:string;
  evidence_provenance:string;
  closed_candles:number; evaluations:number; signals:number;
  shadow_round_trips:number; shadow_net_pnl_mxn:string;
  shadow_fees_mxn:string; shadow_drawdown_mxn:string;
  economic_reject_rate:string;
  certification_state:string; certified:boolean;
  current_strategy_decision:string;
  expected_net_edge_bps:string|null;
  fill_model:string; evidence_quality:string;
  experiment_fingerprint:string; window_provenance:string;
  certification_reason:string; executable_fill_model:boolean;
  production_eligible:boolean;
  position_status:string;
  reason_code:string;
  spread_bps:string|null; depth_mxn:string|null; taker_fee:string|null;
}

export interface ProductionMarketsView {
  research_version:string;
  certification_version:string;
  universe_version:string;
  evidence_store_version:string;
  promotion:string;
  multi_market_production:string;
  production_market:string;
  account_fee_confirmed:boolean;
  taker_fee_rate:string;
  markets:ProductionMarketRow[];
  certified_markets:string[];
  selectable_markets:string[];
  selection_outcome:string;
  one_unresolved_order_globally:boolean;
  llm_influences_selection:boolean;
}

/** Human-readable outcome for a reason code. Never an ambiguous blank. */
const REASON_LABEL:Record<string,string>={
  NO_SIGNAL:'NO SIGNAL',
  NOT_CERTIFIED:'NOT CERTIFIED',
  INSUFFICIENT_EVIDENCE:'INSUFFICIENT EVIDENCE',
  NOT_VIABLE:'NOT VIABLE',
  NEGATIVE_NET_EDGE:'NEGATIVE NET EDGE',
  ECONOMIC_GUARD_REJECT:'ECONOMIC GUARD REJECT',
  SPREAD_REJECT:'SPREAD REJECT',
  DEPTH_REJECT:'DEPTH REJECT',
  CAPITAL_REJECT:'CAPITAL REJECT',
  RISK_REJECT:'RISK REJECT',
  POSITION_CONSTRAINT:'POSITION CONSTRAINT',
  DATA_INVALID:'DATA INVALID',
  MARKET_DATA_UNAVAILABLE:'DATA UNAVAILABLE',
  FEE_DATA_UNAVAILABLE:'FEE UNAVAILABLE',
  FEE_DATA_INVALID:'FEE UNAVAILABLE',
  FIAT_LIKE_MARKET_EXCLUDED:'EXCLUDED (FIAT-LIKE)',
  MARKET_NOT_IN_CERTIFIED_UNIVERSE:'NOT IN CERTIFIED UNIVERSE',
  CERTIFICATION_SUSPENDED:'CERTIFICATION SUSPENDED',
  UNRESOLVED_ORDER_IN_FLIGHT:'UNRESOLVED ORDER IN FLIGHT',
  POSITION_ALREADY_OPEN_IN_MARKET:'POSITION OPEN',
  CURRENT_OPPORTUNITY:'CURRENT OPPORTUNITY',
};

/** Visual state for a row, derived from certification and position facts. */
function visualState(row:ProductionMarketRow):string{
  if(row.position_status==='POSITION_OPEN')return 'POSITION OPEN';
  if(row.production_eligible)return 'CURRENT OPPORTUNITY';
  if(row.certified)return 'CERTIFIED';
  if(row.certification_state==='NOT_VIABLE')return 'NOT VIABLE';
  if(row.certification_state==='INSUFFICIENT_EVIDENCE')return 'INSUFFICIENT EVIDENCE';
  if(row.certification_state==='ACCUMULATING_EVIDENCE')return 'ACCUMULATING';
  return 'RESEARCH';
}

const text=(value:unknown,fallback='—')=>
  value===null||value===undefined||value===''?fallback:String(value);

/** A fingerprint is only useful if it is quoted exactly; a truncated one is a different
 *  value, so the full hash is shown and the cell is allowed to wrap. */
const fingerprint=(value:string)=>value?value:'—';

function MarketRow({row}:{row:ProductionMarketRow}){
  const state=visualState(row);
  return <tr data-market={row.market} data-state={state} data-reason={row.reason_code}>
    <td>{row.market}</td>
    <td>{row.market_class}</td>
    <td>{row.evidence_provenance}</td>
    <td><span data-window-provenance={row.window_provenance}>{row.window_provenance}</span></td>
    <td>{row.closed_candles}</td>
    <td>{row.evaluations}</td>
    <td>{row.signals}</td>
    <td>{row.shadow_round_trips}</td>
    <td>{row.shadow_net_pnl_mxn}</td>
    <td>{row.shadow_fees_mxn}</td>
    <td>{row.shadow_drawdown_mxn}</td>
    <td>{row.economic_reject_rate}</td>
    <td><span data-fill-model={row.fill_model}>{row.fill_model}</span></td>
    <td><span data-evidence-quality={row.evidence_quality}>{row.evidence_quality}</span></td>
    <td className="fingerprint" title={row.experiment_fingerprint}>
      {fingerprint(row.experiment_fingerprint)}</td>
    <td><span className="status" data-certification={row.certification_state}>
      {row.certification_state}</span></td>
    <td>{row.current_strategy_decision}</td>
    <td>{text(row.expected_net_edge_bps)}</td>
    <td><span className="status" data-state={state}>{state}</span></td>
    <td><span className="reason" data-reason={row.reason_code}>
      {REASON_LABEL[row.reason_code]??row.reason_code}</span></td>
    <td><span className="reason" data-certification-reason={row.certification_reason}>
      {row.certification_reason}</span></td>
  </tr>;
}

export function ProductionMarketsPage({markets}:
  {markets:ProductionMarketsView|undefined|null}){
  if(!markets)return <article className="production-markets">
    <h2>Production markets</h2>
    <div className="real-money" role="status">
      <strong>REAL MONEY — SELECTION DISABLED</strong>
      <p>Production market btc_mxn · Promotion DISABLED · Multi-market
        Production DISABLED</p>
    </div>
    <p className="warn">No production market evidence published yet.</p>
    <p>Certification is a property of a <b>market and profile pair</b>, not of a profile
      in general. Only certifying evidence counts: fixture evidence can never certify.
      The selector chooses at most one opportunity globally, returns NO_TRADE rather
      than the least-bad market, and is fully deterministic. No language model
      influences market selection, sizing or certification.</p>
  </article>;
  const rows=markets.markets;
  return <article className="production-markets">
    <h2>Production markets</h2>
    <div className="real-money" role="status">
      <strong>REAL MONEY — SELECTION DISABLED</strong>
      <p>Production market {markets.production_market} · Promotion {markets.promotion} ·
        Multi-market Production {markets.multi_market_production}</p>
    </div>
    <p>Certification is a property of a <b>market and profile pair</b>, not of a profile
      in general. Only certifying evidence counts: fixture evidence can never certify.
      The selector chooses at most one opportunity globally, returns NO_TRADE rather
      than the least-bad market, and is fully deterministic. No language model
      influences market selection, sizing or certification.</p>

    <h3>Selection</h3>
    <div className="table"><table><tbody>
      <tr><th>Selection outcome</th><td>{markets.selection_outcome}</td></tr>
      <tr><th>Certified markets</th>
        <td>{markets.certified_markets.length?markets.certified_markets.join(', '):'NONE'}</td></tr>
      <tr><th>Currently selectable</th>
        <td>{markets.selectable_markets.length?markets.selectable_markets.join(', '):'NONE'}</td></tr>
      <tr><th>Confirmed taker fee</th>
        <td>{markets.account_fee_confirmed?markets.taker_fee_rate:'UNAVAILABLE'}</td></tr>
      <tr><th>One unresolved order globally</th>
        <td>{markets.one_unresolved_order_globally?'YES':'NO'}</td></tr>
      <tr><th>LLM influences selection</th>
        <td className={markets.llm_influences_selection?'negative':'positive'}>
          {markets.llm_influences_selection?'YES':'NO'}</td></tr>
    </tbody></table></div>

    <h3>Markets</h3>
    <p>Evidence is quoted with the execution model and the predeclared window
      provenance that produced it. A round-trip count alone is not comparable across
      fill models, and development evidence is real data that is still <b>not</b>
      independent confirmation, so the two are never shown as if interchangeable.</p>
    {rows.length?<div className="table"><table>
      <thead><tr><th>Market</th><th>Class</th><th>Provenance</th><th>Window</th>
        <th>Candles</th>
        <th>Evaluations</th><th>Signals</th><th>Round trips</th><th>Net P&amp;L</th>
        <th>Fees</th><th>Drawdown</th><th>Econ. reject rate</th><th>Fill model</th>
        <th>Fill evidence</th><th>Experiment</th><th>Certification</th>
        <th>Decision</th><th>Net edge (bps)</th><th>State</th><th>Why not trading</th>
        <th>Certification reason</th></tr></thead>
      <tbody>{rows.map(row=><MarketRow key={row.book} row={row}/>)}</tbody>
    </table></div>
      :<p>No market has been evaluated yet.</p>}

    <p className="policy">Research {markets.research_version} · Certification
      {' '}{markets.certification_version} · Universe {markets.universe_version} ·
      Evidence store {markets.evidence_store_version}</p>
  </article>;
}
