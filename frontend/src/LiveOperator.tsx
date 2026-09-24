import {useEffect,useMemo,useState} from 'react';

/** F4.6 runtime contract, extended additively for the MVP control plane. */
export interface MvpRuntime {
  status:string; heartbeat:string; quality:string; mode:string; elapsed_seconds:number;
  runtime_state:'RUNNING'|'INACTIVE'; market_stream:'ACTIVE'|'INACTIVE';
  last_known_quality?:string|null; last_known_market_at?:string|null;
  last_event_at:string|null; last_market_event_at:string|null; last_closed_candle_at:string|null;
  current_candle:null|{status:string;interval_start:string;interval_end:string;open:string;high:string;
    low:string;last:string;volume:string;trade_count:number};
  market_snapshot:null|{last_price_mxn:string|null;best_bid_mxn:string;best_ask_mxn:string;spread_mxn:string;
    spread_bps:string;orderbook_at:string;sequence?:number|null;request_latency_ms?:number|null};
  events:{event_id:number;event_type:string;timestamp:string;summary:string;outcome?:string|null;
    component?:string|null;level?:string|null;correlation_id?:string|null;checkpoint_id?:string|null}[];
}
export interface MvpCandle {status:string;interval_start:string;interval_end:string;open:string;high:string;
  low:string;last:string;close?:string;volume:string;trade_count:number}
export interface MvpStage {stage:string;status:string;at:string|null;event:string|null;detail:string|null}
export interface MvpObservability {
  runtime:MvpRuntime; observability:{status:string;degraded:boolean;scope:string;trading_effect:string;
    browser_tab_closure_effect:string;heartbeat_age_seconds:number|null;publication_age_seconds:number|null};
  market_state:string; pipeline:MvpStage[]; candles:MvpCandle[];
  strategy:{last_evaluation_at:string|null;evaluations:number;signals:number;no_signal:number;
    last_decision:null|{at:string|null;decision:string;signal:string|null;reason:string;correlation_id:string|null}};
  session:{elapsed_seconds:number|null;remaining_seconds:number|null;max_duration_seconds:number|null;
    max_orders_per_session:number|null;max_session_loss_mxn:string|null};
  metrics:Record<string,any>;
  /** 0.1.2 hardening surfaces; optional so older snapshots still render. */
  signals?:{admitted:number;suppressed_pending_order:number;strategy_buy_decisions:number;
    strategy_sell_decisions:number};
  blocked_recovery?:{blocked:boolean;origins:string[]};
  wallet?:{status:string;observed_at?:string|null;read_only:boolean};
  runtime_gaps?:{gap_seconds:string;last_event_at:string|null;next_event_at:string}[];
  monitored_seconds?:number|null;
}

const unknown='UNKNOWN';
const waiting='WAITING FOR DATA';
const NA='NOT_APPLICABLE';
/** Real-money mode must never invent data. */
const age=(at:string|null|undefined,now:number)=>at?`${Math.max(0,Math.floor((now-Date.parse(at))/1000))}s ago`:waiting;
export const fmt=(value:unknown)=>value===null||value===undefined||value===''?unknown:String(value);
/** Marks values that change every second, so visual regression can mask them. */
const Volatile=({children}:{children:React.ReactNode})=><span data-volatile="true">{children}</span>;
const stages:Record<string,string>={MARKET:'Market',CANDLE:'Candle',STRATEGY:'Strategy',SIGNAL:'Signal',CAPITAL:'Capital',
  RISK:'Risk',FINAL_MARKET_CHECK:'Final market check',ORDER:'Order',FILL:'Fill',RECONCILIATION:'Reconciliation',LEDGER:'Ledger'};

export function useTicker(intervalMs=1000){const[now,setNow]=useState(Date.now());useEffect(()=>{const t=setInterval(()=>setNow(Date.now()),intervalMs);return()=>clearInterval(t)},[intervalMs]);return now}

/** Live candlestick chart reusing the F4.5 OHLC SVG approach, plus the open candle. */
export function CandleChart({candles,open,now}:{candles:MvpCandle[];open:MvpCandle|null;now:number}){
  const points=useMemo(()=>[...candles,...(open?[open]:[])], [candles,open]);
  if(!points.length)return <p>{waiting}</p>;
  const ys=points.flatMap(x=>[Number(x.low),Number(x.high)]);
  const lo=Math.min(...ys),hi=Math.max(...ys),span=hi-lo||1;
  const y=(v:unknown)=>150-(Number(v)-lo)/span*110;
  const step=550/Math.max(points.length,1);
  return <figure><figcaption>BTC/MXN closed 1m candles plus current open candle</figcaption>
    <svg aria-label="BTC/MXN candlestick chart" className="chart" viewBox="0 0 600 180">
      {points.map((x,i)=>{const cx=25+i*step;const close=Number(x.close??x.last);const rising=close>=Number(x.open);
        const color=x.status==='OPEN'?'#67d5ff':(rising?'#8ee3c1':'#ffbf69');
        return <g key={x.interval_start+(x.status==='OPEN'?'-open':'-closed')} data-candle={x.status}>
          <line x1={cx} x2={cx} y1={y(x.high)} y2={y(x.low)} stroke={color} strokeWidth={x.status==='OPEN'?1:2}
            strokeDasharray={x.status==='OPEN'?'3 3':undefined}/>
          <rect x={cx-6} y={Math.min(y(x.open),y(close))} width={x.status==='OPEN'?4:12}
            height={Math.max(2,Math.abs(y(x.open)-y(close)))} fill={x.status==='OPEN'?'none':color}
            stroke={color}/></g>})}
    </svg>
    <p className="process-flow">Open candle is OBSERVATIONAL ONLY and never a strategy input.{
      open?` Closes in ${Math.max(0,Math.ceil((Date.parse(open.interval_end)-now)/1000))}s`:''}</p>
  </figure>;
}

export function Pipeline({pipeline}:{pipeline:MvpStage[]}){
  return <ol className="pipeline" aria-label="Autonomous pipeline">{pipeline.map(s=>
    <li key={s.stage} data-status={s.status} className={`stage stage-${s.status.toLowerCase()}`}>
      <small>{stages[s.stage]??s.stage}</small><b>{s.status}</b>
      <span>{s.event??'—'}</span>{s.detail&&<em>{s.detail}</em>}</li>)}</ol>;
}

export function ActivityTail({events,now,limit=30}:{events:MvpRuntime['events'];now:number;limit?:number}){
  if(!events.length)return <p>{waiting} — no checkpoint recorded yet</p>;
  return <div className="table"><table><thead><tr><th>Time</th><th>Level</th><th>Component</th><th>Event</th><th>Message</th><th>Correlation</th></tr></thead>
    <tbody>{[...events].reverse().slice(0,limit).map(e=><tr key={e.event_id}>
      <td>{e.timestamp?.replace('T',' ').replace('Z',' UTC')??unknown}</td>
      <td>{fmt(e.level)}</td><td>{fmt(e.component)}</td>
      <td>{e.event_type}</td><td>{e.summary||'—'}</td><td>{e.correlation_id??'—'}</td></tr>)}</tbody></table></div>;
}

/** Overview live operator view; rendered only while a session is active. */
export function LiveOperatorView({model,now,sessionId,equity,orders,fills,champion,regime}:{
  model:MvpObservability;now:number;sessionId:string|null;equity:string;orders:number;fills:number;
  champion?:string;regime?:string}){
  const {runtime,observability,session,strategy,metrics,market_state}=model;
  const m=runtime.market_snapshot,c=runtime.current_candle,decision=strategy.last_decision;
  const state=observability.degraded?'OBSERVABILITY DEGRADED':market_state.replaceAll('_',' ');
  return <>
    <div className="real-money" role="status"><strong>{state}</strong>
      <p>AUTO EXECUTION ON · BTC/MXN · Kill switch READY</p>
      {observability.degraded&&<p className="error">Observability degraded: operator view is stale while the backend continues. Trading effect: {observability.trading_effect}.</p>}
    </div>
    <h2>Session</h2>
    <Cards rows={[['State',String(runtime.status)],['Session id',sessionId??unknown],
      ['Elapsed',session.elapsed_seconds===null?unknown:`${session.elapsed_seconds}s`],
      ['Remaining',session.remaining_seconds===null?unknown:`${session.remaining_seconds}s`],
      ['Orders used / max',`${orders} / ${session.max_orders_per_session??unknown}`],
      ['Session loss limit',session.max_session_loss_mxn?`${session.max_session_loss_mxn} MXN`:unknown],
      ['Equity',equity+' MXN'],['Fills',String(fills)]]}/>
    <h2>Market</h2>
    <Cards rows={[['BTC/MXN reference',fmt(m?.last_price_mxn)],['Best bid',fmt(m?.best_bid_mxn)],['Best ask',fmt(m?.best_ask_mxn)],['Spread',`${fmt(m?.spread_mxn)} MXN / ${fmt(m?.spread_bps)} bps`],['Market quality',String(runtime.quality)],['Last market event',age(runtime.last_market_event_at,now)],['Last closed candle',age(runtime.last_closed_candle_at,now)],['Sequence',fmt(m?.sequence)]]}/>
    <h2>Live candle chart</h2>
    <CandleChart candles={model.candles} open={c} now={now}/>
    <h2>Strategy</h2>
    <dl><dt>Champion</dt><dd>{champion??unknown}</dd><dt>Market regime</dt><dd>{regime??unknown}</dd>
      <dt>Last evaluation</dt><dd>{strategy.last_evaluation_at??waiting}</dd><dt>Evaluations</dt><dd>{strategy.evaluations}</dd>
      <dt>Last decision</dt><dd>{decision?`${decision.decision}${decision.signal?` (${decision.signal})`:''}`:waiting}</dd>
      <dt>Reason</dt><dd>{decision?.reason||waiting}</dd><dt>Signals / no signal</dt><dd>{strategy.signals} / {strategy.no_signal}</dd></dl>
    <h2>Pipeline</h2>
    <Pipeline pipeline={model.pipeline}/>
    <h2>Activity</h2>
    <ActivityTail events={runtime.events} now={now}/>
    <h2>Operational metrics</h2>
    <Cards rows={[['Orders',String(metrics.order_intents??0)],['Fills',String(metrics.fills??0)],['Signals',String(metrics.signals??0)],['Rejections',String((metrics.capital_reject??0)+(metrics.risk_reject??0)+(metrics.final_market_reject??0))],['Halts',String(metrics.halts??0)],['Market events',String(metrics.market_events??0)],['Closed candles',String(metrics.closed_candles??0)],['Observability',observability.status]]}/>
    {(model.signals||model.blocked_recovery||model.runtime_gaps?.length)&&<>
      <h2>Signal admission</h2>
      {model.signals
        ?<Cards rows={[['Buy decisions',String(model.signals.strategy_buy_decisions)],
            ['Sell decisions',String(model.signals.strategy_sell_decisions)],
            ['Signals admitted',String(model.signals.admitted)],
            ['Suppressed pending order',String(model.signals.suppressed_pending_order)]]}/>
        :<p>{NA}</p>}
      <h2>Order recovery</h2>
      {model.blocked_recovery?.blocked
        ?<p className="error">BLOCKED RECOVERY — an acknowledged order has no established financial outcome.
          New financial writes are blocked and automatic execution is off. Affected origin(s):{' '}
          {model.blocked_recovery.origins.join(', ')||unknown}. Recovery is GET-only and never re-submits.</p>
        :<p>No order is awaiting an unresolved outcome.</p>}
      <h2>Runtime gaps</h2>
      {model.runtime_gaps?.length
        ?<><p className="process-flow">Wall-clock runtime includes periods with no executing process. These are
          not monitored market time.</p>
          <div className="table"><table><thead><tr><th>Gap seconds</th><th>Last event</th><th>Next event</th></tr></thead>
            <tbody>{model.runtime_gaps.map((gap,index)=><tr key={index}>
              <td>{gap.gap_seconds}</td><td>{gap.last_event_at??unknown}</td><td>{gap.next_event_at}</td></tr>)}
            </tbody></table></div>
          {model.monitored_seconds!=null&&<p>Monitored seconds: {model.monitored_seconds}</p>}</>
        :<p>No runtime gap detected.</p>}
    </>}
  </>;
}

function Cards({rows}:{rows:string[][]}){return <div className="cards">{rows.map(([a,b])=><article key={a}><small>{a}</small><b>{b}</b></article>)}</div>}

/** Market page: real live market view over the existing SSE snapshot. */
export function LiveMarketPage({model,now}:{model:MvpObservability;now:number}){
  const {runtime,market_state}=model,m=runtime.market_snapshot,c=runtime.current_candle;
  return <>
    <div className="real-money" role="status"><strong>{market_state.replaceAll('_',' ')}</strong>
      <p>BTC/MXN · Bitso Production public data · No browser-to-exchange access</p></div>
    <Cards rows={[['Reference',fmt(m?.last_price_mxn)],['Best bid',fmt(m?.best_bid_mxn)],['Best ask',fmt(m?.best_ask_mxn)],
      ['Spread',`${fmt(m?.spread_mxn)} MXN`],['Spread bps',fmt(m?.spread_bps)],['Market quality',String(runtime.quality)],
      ['Last update age',age(runtime.last_market_event_at,now)],['Exchange sequence',fmt(m?.sequence)],
      ['Market request RTT',m?.request_latency_ms==null?unknown:`${m.request_latency_ms} ms`],
      ['Last closed candle',age(runtime.last_closed_candle_at,now)]]}/>
    <h2>Candles</h2>
    <CandleChart candles={model.candles} open={c} now={now}/>
    <h2>Current open candle</h2>
    {c?<div className="table"><table><tbody>{([['Open',c.open],['High',c.high],['Low',c.low],['Last',c.last],
      ['Observations',String(c.trade_count)],['Interval start',c.interval_start],['Interval end',c.interval_end]] as string[][])
      .map(([k,v])=><tr key={k}><th>{k}</th><td>{v}</td></tr>)}</tbody></table></div>:<p>{waiting}</p>}
    <h2>Recent closed candles</h2>
    {model.candles.length?<div className="table"><table><thead><tr><th>Interval start</th><th>Open</th><th>High</th><th>Low</th><th>Close</th></tr></thead>
      <tbody>{[...model.candles].reverse().map(x=><tr key={x.interval_start}><td>{x.interval_start}</td><td>{x.open}</td>
        <td>{x.high}</td><td>{x.low}</td><td>{x.close??x.last}</td></tr>)}</tbody></table></div>:<p>{waiting}</p>}
  </>;
}

/** Activity page: structured, filterable live telemetry. */
export function LiveActivityPage({events,now}:{events:MvpRuntime['events'];now:number}){
  const[component,setComponent]=useState('ALL'),[level,setLevel]=useState('ALL'),[event,setEvent]=useState('ALL'),[newest,setNewest]=useState(true);
  const components=['ALL',...[...new Set(events.map(e=>e.component??'unknown'))]];
  const levels=['ALL',...[...new Set(events.map(e=>e.level??'INFO'))]];
  const eventsList=['ALL',...[...new Set(events.map(e=>e.event_type))]];
  const rows=[...events].filter(e=>(component==='ALL'||e.component===component)
    &&(level==='ALL'||(e.level??'INFO')===level)&&(event==='ALL'||e.event_type===event));
  const ordered=newest?rows.reverse():rows;
  return <>
    <div className="filters">
      <label>Component<select aria-label="Filter by component" value={component} onChange={e=>setComponent(e.target.value)}>{components.map(x=><option key={x}>{x}</option>)}</select></label>
      <label>Level<select aria-label="Filter by level" value={level} onChange={e=>setLevel(e.target.value)}>{levels.map(x=><option key={x}>{x}</option>)}</select></label>
      <label>Event<select aria-label="Filter by event" value={event} onChange={e=>setEvent(e.target.value)}>{eventsList.map(x=><option key={x}>{x}</option>)}</select></label>
      <label className="toggle"><input type="checkbox" aria-label="Newest first" checked={newest} onChange={e=>setNewest(e.target.checked)}/>Newest first</label>
    </div>
    <p>{rows.length} checkpoints · observation time {age(new Date(now).toISOString(),now)}</p>
    {ordered.length?<div className="table"><table><thead><tr><th>Time</th><th>Level</th><th>Component</th><th>Event</th><th>Message</th><th>Correlation</th></tr></thead>
      <tbody>{ordered.map(e=><tr key={e.event_id} data-level={e.level??'INFO'}>
        <td>{e.timestamp?.replace('T',' ').replace('Z',' UTC')??unknown}</td><td>{fmt(e.level)}</td>
        <td>{fmt(e.component)}</td><td>{e.event_type}</td><td>{e.summary||'—'}</td>
        <td>{e.correlation_id??'—'}</td></tr>)}</tbody></table></div>:<p>{waiting}</p>}
  </>;
}

/** Telemetry page: operational metrics without external tooling. */
export function LiveTelemetryPage({model,now}:{model:MvpObservability;now:number}){
  const {runtime,observability,metrics}=model,durations=metrics.durations_ms??{};
  const duration=(name:string)=>durations[name]?`${durations[name].last_ms} ms (max ${durations[name].max_ms} ms, n=${durations[name].count})`:NA;
  const inactive=runtime.runtime_state==='INACTIVE';
  return <>
    {inactive
      ?<><p className="process-flow">The trading runtime is intentionally stopped. Ageing heartbeat and event
        timestamps are historical facts, not incidents.</p>
        <Cards rows={[['Application','ONLINE'],['Control SSE','CONNECTED'],['Trading runtime','INACTIVE'],
          ['Market stream','INACTIVE'],['Session',String(runtime.status)],
          ['Last market quality',String(runtime.last_known_quality??runtime.quality)],
          ['Last market event',runtime.last_market_event_at??unknown],
          ['Observability',observability.status]]}/></>
      :<Cards rows={[['Observability',observability.status],
          ['Heartbeat age',observability.heartbeat_age_seconds==null?unknown:`${Math.round(observability.heartbeat_age_seconds)}s`],
          ['Publication age',observability.publication_age_seconds==null?unknown:`${Math.round(observability.publication_age_seconds)}s`],
          ['Runtime',runtime.runtime_state],['Market stream',runtime.market_stream],
          ['SSE status',String(runtime.heartbeat)],['Market event age',age(runtime.last_market_event_at,now)],
          ['Market quality',String(runtime.quality)],['Market events',String(metrics.market_events??0)],
          ['Market unavailable',String(metrics.market_unavailable??0)]]}/>}
    <h2>Latency</h2>
    <dl><dt>Market request RTT</dt><dd>{duration('market_request_rtt')}</dd>
      <dt>Strategy evaluation</dt><dd>{duration('strategy_evaluation')}</dd>
      <dt>Final market preflight</dt><dd>{duration('final_market_preflight')}</dd>
      <dt>Reconciliation</dt><dd>{duration('reconciliation')}</dd></dl>
    <h2>Counts</h2>
    <Cards rows={[['Orders',String(metrics.order_intents??0)],['Fills',String(metrics.fills??0)],['Signals',String(metrics.signals??0)],
      ['Capital rejections',String(metrics.capital_reject??0)],['Risk rejections',String(metrics.risk_reject??0)],
      ['Final market rejections',String(metrics.final_market_reject??0)],['Halts',String(metrics.halts??0)],
      ['Reconciliations',String(metrics.reconciliations??0)],['Ledger updates',String(metrics.ledger_updates??0)],
      ['Closed candles',String(metrics.closed_candles??0)],['No-signal decisions',String(metrics.no_signal??0)]]}/>
    <h2>Pipeline</h2>
    <Pipeline pipeline={model.pipeline}/>
  </>;
}
