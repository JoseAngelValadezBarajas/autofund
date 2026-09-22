import {LiveActivityPage} from './LiveOperator';

/** Research-only market scanner contract. Never implies a trading action. */
export interface ScannerCandidate {
  book:string; rank:number; status:string; reason:string; score:string; score_version:string;
  components:Record<string,string>; movement_bps:string|null; volatility_bps:string|null;
  high_low_range_bps:string|null; best_bid_mxn:string|null; best_ask_mxn:string|null;
  spread_bps:string|null; depth_mxn:string|null; volume_mxn:string|null; minimum_order_mxn:string|null;
  maker_fee:string|null; taker_fee:string|null; estimated_round_trip_friction_mxn:string|null;
  estimated_round_trip_friction_bps:string|null; data_quality:string; cap_executable:boolean;
  strategy_compatibility:string; lifecycle:string; shadow_evaluations:number; shadow_signals:number;
  shadow_net_pnl_mxn:string|null; evidence_count:number; data_fingerprint:string;
}
export interface ScannerEvidence {
  scanner_ran:boolean; degraded:boolean; error?:string|null; universe_size:number; scanned_at?:string|null;
  score_version?:string; eligible:ScannerCandidate[]; rejected:ScannerCandidate[];
  candidates:ScannerCandidate[]; shadow:any[]; live_market:string; production_market_rotation:string;
  market_promotion:string; read_only:boolean; execution_path_to_production:string;
  interval_seconds?:number; status?:string; fee_source?:string; last_fee_refresh_at?:string|null;
  books_with_account_fee?:number; books_with_market_data?:number;
}
export interface LearningView {
  champion:{profile_id:string;strategy_id:string;version:string;fingerprint:string;certification_status:string};
  sessions_observed:number; eligible_evaluations:number; signals:number;
  reason_distribution:Record<string,number>; regime_distribution:Record<string,number>;
  near_signal_count:number; distance_to_signal_min:string|null;
  observations:any[]; challengers:{fingerprint:string;status:string;evidence_count:number}[];
  challenger_count:number; promotion:string; auto_promotion:string; market_promotion:string;
  min_sessions_for_challenger?:number; min_signals_for_challenger?:number;
  scanner?:any;
}

const num=(value:unknown,digits=2)=>{const n=Number(value);return Number.isFinite(n)?n.toFixed(digits):'UNKNOWN'};
const NA='NOT_APPLICABLE';

/** Research labelling is mandatory: the scanner never recommends a trade. */
function Status({candidate}:{candidate:ScannerCandidate}){
  const eligible=candidate.status==='ELIGIBLE';
  return <span className={`status status-${eligible?'eligible':'rejected'}`} data-status={candidate.status}>
    {eligible?'RESEARCH CANDIDATE':candidate.status}</span>;
}

function ComponentBars({components}:{components:Record<string,string>}){
  const rows=Object.entries(components??{}).sort(([a],[b])=>a.localeCompare(b));
  if(!rows.length)return <span>{NA}</span>;
  return <ul className="components">{rows.map(([name,value])=><li key={name}>
    <small>{name.replace(/_score$/,'')}</small>
    <span className="bar"><i style={{width:`${Math.max(0,Math.min(100,Number(value)*100))}%`}}/></span>
    <b>{num(value,3)}</b></li>)}</ul>;
}

export function MarketScannerPage({scanner}:{scanner:ScannerEvidence}){
  if(!scanner?.scanner_ran)return <article>
    <h2>Market opportunity scanner</h2>
    <p className="warn">Scanner has not completed a scan yet{scanner?.error?`: ${scanner.error}`:''}.</p>
    <p>Read-only research discovery across the exchange&apos;s MXN books. It runs beside Production and never
      creates orders.</p></article>;
  return <>
    <div className="real-money" role="status">
      <strong>READ-ONLY RESEARCH</strong>
      <p>Live Production market: {scanner.live_market.toUpperCase()} · Automatic market rotation:{' '}
        {scanner.production_market_rotation} · Promotion to real money: {scanner.market_promotion}</p>
      <p>Scanner ranks research candidates, not financial actions. No scanner result can create a Production
        order. Execution path to Production: {scanner.execution_path_to_production}.</p>
      {scanner.degraded&&<p className="error">MARKET_SCANNER_DEGRADED{scanner.error?`: ${scanner.error}`:''} —
        Production is unaffected.</p>}
      <p>Fee source: <strong>{scanner.fee_source??'UNAVAILABLE'}</strong> · Scanner status:{' '}
        <strong>{scanner.status??(scanner.degraded?'DEGRADED':'HEALTHY')}</strong> · Last successful fee refresh:{' '}
        <span data-volatile="true">{scanner.last_fee_refresh_at??'NEVER'}</span></p>
    </div>
    <div className="cards">
      {[['MXN books discovered',String(scanner.universe_size)],
        ['Eligible',String(scanner.eligible.length)],
        ['Rejected',String(scanner.rejected.length)],
        ['Books with account fee',String(scanner.books_with_account_fee??0)],
        ['Books with market data',String(scanner.books_with_market_data??0)],
        ['Score version',scanner.score_version??'UNKNOWN']].map(([label,value])=>
        <article key={label}><small>{label}</small><b>{value}</b></article>)}
    </div>
    <h2>Ranked candidates</h2>
    <div className="table"><table><thead><tr>
      <th>Rank</th><th>Book</th><th>Status</th><th>Score</th><th>Movement bps</th><th>Volatility bps</th>
      <th>Bid</th><th>Ask</th><th>Spread bps</th><th>Depth MXN</th><th>Min order MXN</th>
      <th>Maker fee</th><th>Taker fee</th><th>Round-trip friction</th><th>Data</th><th>Cap executable</th>
      <th>Strategy</th><th>Reason</th></tr></thead>
      <tbody>{scanner.candidates.map(c=><tr key={c.book} data-book={c.book}>
        <td>{c.rank>0?c.rank:'—'}</td><td>{c.book}</td><td><Status candidate={c}/></td>
        <td>{num(c.score)}</td><td>{c.movement_bps?num(c.movement_bps):'UNKNOWN'}</td>
        <td>{c.volatility_bps?num(c.volatility_bps):'UNKNOWN'}</td>
        <td>{c.best_bid_mxn??'UNKNOWN'}</td><td>{c.best_ask_mxn??'UNKNOWN'}</td>
        <td>{c.spread_bps?num(c.spread_bps,3):'UNKNOWN'}</td><td>{c.depth_mxn?num(c.depth_mxn):'UNKNOWN'}</td>
        <td>{c.minimum_order_mxn??'UNKNOWN'}</td><td>{c.maker_fee??'UNKNOWN'}</td><td>{c.taker_fee??'UNKNOWN'}</td>
        <td>{c.estimated_round_trip_friction_mxn?`${num(c.estimated_round_trip_friction_mxn,4)} MXN`:'UNKNOWN'}</td>
        <td>{c.data_quality}</td><td>{c.cap_executable?'YES':'NO'}</td>
        <td>{c.strategy_compatibility==='CERTIFIED_FOR_MARKET'?'CERTIFIED':'RESEARCH ONLY'}</td>
        <td>{c.reason}</td></tr>)}</tbody></table></div>
    <h2>Score components</h2>
    {scanner.candidates.filter(c=>c.status==='ELIGIBLE').map(c=><article key={c.book}>
      <h3>{c.book} · score {num(c.score)}</h3><ComponentBars components={c.components}/>
      <p className="process-flow">Data fingerprint {c.data_fingerprint.slice(0,16)}…</p></article>)}
    {scanner.eligible.length===0&&<p>No market passed the current eligibility filters.</p>}
    <h2>Shadow evaluation</h2>
    {scanner.shadow.length?<div className="table"><table><thead><tr>
      <th>Market</th><th>Lifecycle</th><th>Candles</th><th>Evaluations</th><th>Signals</th>
      <th>Signal rate</th><th>Net P&amp;L MXN</th><th>Strategy</th><th>Evidence</th></tr></thead>
      <tbody>{scanner.shadow.map((row:any)=><tr key={row.market}><td>{row.market}</td>
        <td>RESEARCH ONLY</td><td>{row.candles??0}</td><td>{row.evaluations??0}</td><td>{row.signals??0}</td>
        <td>{row.signal_rate??'0'}</td><td>{row.net_pnl_mxn??'0'}</td>
        <td>{row.strategy_compatibility==='CERTIFIED_FOR_MARKET'?'CERTIFIED':'RESEARCH ONLY'}</td>
        <td>{row.evidence_count??0}</td></tr>)}</tbody></table></div>
      :<p>No shadow evaluation recorded yet. {NA} until evidence exists.</p>}
    <h2>Isolation</h2>
    <dl><dt>Live Production market</dt><dd>{scanner.live_market.toUpperCase()}</dd>
      <dt>Scanner capability</dt><dd>GET / READ-ONLY</dd>
      <dt>Scanner → Production execution</dt><dd>NOT PRESENT</dd>
      <dt>Automatic market rotation</dt><dd>{scanner.production_market_rotation}</dd>
      <dt>Promotion to real money market</dt><dd>FUTURE MANUAL MILESTONE</dd>
      <dt>Refresh cadence</dt><dd>{scanner.interval_seconds??'UNKNOWN'}s</dd></dl>
  </>;
}

export function LearningPage({learning,activity}:{learning:LearningView;activity:any}){
  const reasons=Object.entries(learning.reason_distribution??{});
  const regimes=Object.entries(learning.regime_distribution??{});
  const scanner=learning.scanner??{};
  return <>
    <h2>Current Champion</h2>
    <dl><dt>Profile</dt><dd>{learning.champion.profile_id}</dd>
      <dt>Strategy</dt><dd>{learning.champion.strategy_id} v{learning.champion.version}</dd>
      <dt>Certification</dt><dd>{learning.champion.certification_status}</dd>
      <dt>Fingerprint</dt><dd className="fingerprint">{learning.champion.fingerprint.slice(0,32)}…</dd>
      <dt>Promotion</dt><dd>{learning.promotion}</dd>
      <dt>Auto-promotion</dt><dd>{learning.auto_promotion}</dd>
      <dt>Champion change during session</dt><dd>DISABLED</dd></dl>
    <h2>Session evidence</h2>
    <div className="cards">
      {[['Sessions observed',String(learning.sessions_observed)],
        ['Eligible evaluations',String(learning.eligible_evaluations)],
        ['Signals',String(learning.signals)],
        ['Near-signal evaluations',String(learning.near_signal_count)],
        ['Closest distance to signal',learning.distance_to_signal_min??NA],
        ['Challenger evidence',String(learning.challenger_count)]].map(([label,value])=>
        <article key={label}><small>{label}</small><b>{value}</b></article>)}
    </div>
    <h2>NO_SIGNAL reason distribution</h2>
    {reasons.length?<dl>{reasons.map(([name,value])=><div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl>
      :<p>{NA} — no eligible evaluations recorded yet.</p>}
    <h2>Market regime distribution</h2>
    {regimes.length?<dl>{regimes.map(([name,value])=><div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl>
      :<p>{NA} — no market regime recorded yet.</p>}
    <h2>Learning observations</h2>
    {learning.observations.length?<div className="table"><table><thead><tr>
      <th>Classification</th><th>Sessions</th><th>Eligible</th><th>Signals</th><th>Near</th>
      <th>Stop reason</th><th>Observations</th></tr></thead>
      <tbody>{learning.observations.map((row:any,index:number)=><tr key={index}>
        <td>{row.classification}</td><td>{row.sessions_observed}</td><td>{row.eligible_evaluations}</td>
        <td>{row.signals}</td><td>{row.near_signal_count??0}</td><td>{row.stop_reason??'—'}</td>
        <td>{(row.observations??[]).join(', ')||'—'}</td></tr>)}</tbody></table></div>
      :<p>{NA} — no completed session has been observed yet.</p>}
    <h2>Active challengers</h2>
    {learning.challengers.length?<div className="table"><table><thead><tr>
      <th>Fingerprint</th><th>Status</th><th>Evidence count</th></tr></thead>
      <tbody>{learning.challengers.map(c=><tr key={c.fingerprint}><td className="fingerprint">
        {c.fingerprint.slice(0,24)}…</td><td>{c.status}</td><td>{c.evidence_count}</td></tr>)}</tbody></table></div>
      :<p>No challenger exists. Evidence is insufficient to propose one
        {learning.min_sessions_for_challenger?` (requires ${learning.min_sessions_for_challenger} sessions)`:''}. </p>}
    <h2>Market opportunity research</h2>
    <p>Current live market: {String(scanner.live_market??'btc_mxn').toUpperCase()} · Scanner universe:{' '}
      {scanner.universe_size??0} MXN books · Eligible: {scanner.eligible_count??0} · Shadow-evaluated:{' '}
      {scanner.shadow_evaluations??0}</p>
    {scanner.candidates?.length?<div className="table"><table><thead><tr>
      <th>Market</th><th>Status</th><th>Score</th><th>Strategy</th><th>Reason</th></tr></thead>
      <tbody>{scanner.candidates.map((c:any)=><tr key={c.market}><td>{c.market}</td>
        <td>{c.status}</td><td>{num(c.score)}</td>
        <td>{c.strategy_compatibility==='CERTIFIED_FOR_MARKET'?'CERTIFIED':'RESEARCH ONLY'}</td>
        <td>{c.reason}</td></tr>)}</tbody></table></div>
      :<p>{NA} — scanner evidence unavailable.</p>}
    <p>Promotion of another market to REAL MONEY eligibility: {learning.market_promotion}.
      This is a future manual milestone and is not enabled.</p>
    <h2>Learning checkpoints</h2>
    <LiveActivityPage events={activity??[]} now={Date.now()}/>
  </>;
}
