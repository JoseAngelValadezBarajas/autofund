import {useEffect, useState} from 'react';

export interface Runtime {
  demo_mode: boolean; session_id: string|null; status: string; mode: string;
  started_at: string|null; elapsed_seconds: number; heartbeat: string;
  last_event_at: string|null; last_market_event_at: string|null;
  last_closed_candle_at: string|null; last_state_update_at: string|null;
  market: string; interval: string; strategy_id: string|null;
  quality: string; risk_status: string; accounting_status: string;
  current_candle: null|{open:string; high:string; low:string; last:string; volume:string;
    trade_count:number; interval_start:string; interval_end:string; status:string};
  market_snapshot: null|{last_price_mxn:string|null; best_bid_mxn:string; best_ask_mxn:string;
    spread_mxn:string; spread_bps:string; orderbook_at:string};
  events: {event_id:number; event_type:string; timestamp:string; summary:string}[];
}

export function RuntimePanel({runtime, disconnected=false, market=false}:{runtime:Runtime; disconnected?:boolean; market?:boolean}) {
  const [now,setNow]=useState(Date.now());
  useEffect(()=>{const timer=setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(timer)},[]);
  const active=runtime.status==='RUNNING'&&!disconnected&&runtime.heartbeat==='LIVE';
  const age=(at:string|null)=>at?(runtime.demo_mode?'DEMO timestamp':`${Math.max(0,Math.floor((now-Date.parse(at))/1000))}s ago`):'Not observed';
  const c=runtime.current_candle;
  return <article className="runtime" aria-label="Session runtime">
    <div className="runtime-title"><strong>{disconnected?'DISCONNECTED':runtime.status}</strong><b>{disconnected?'DISCONNECTED':runtime.heartbeat}</b><span>{runtime.mode} · {runtime.market.toUpperCase()} · {runtime.interval}</span></div>
    <dl className="runtime-grid">
      <div><dt>Session ID</dt><dd>{runtime.session_id??'No active session'}</dd></div>
      <div><dt>Strategy</dt><dd>{runtime.strategy_id??'Not initialized'}</dd></div>
      <div><dt>Started UTC</dt><dd>{runtime.started_at??'—'}</dd></div>
      <div><dt>Elapsed runtime</dt><dd>{runtime.elapsed_seconds}s</dd></div>
      <div><dt>Last market event</dt><dd>{age(runtime.last_market_event_at)}</dd></div>
      <div><dt>Last runtime event</dt><dd>{age(runtime.last_event_at)}</dd></div>
      <div><dt>Last closed candle</dt><dd>{runtime.last_closed_candle_at??'Not observed'}</dd></div>
      <div><dt>SSE status</dt><dd>{disconnected?'DISCONNECTED':'CONNECTED'}</dd></div>
      <div><dt>Market quality</dt><dd>{runtime.quality}</dd></div>
      <div><dt>Risk</dt><dd>{runtime.risk_status}</dd></div>
      <div><dt>Accounting</dt><dd>{runtime.accounting_status}</dd></div>
    </dl>
    <p className="process-flow">Market → Closed candle → Strategy → Signal → Capital / Risk → Shadow fill → Ledger</p>
    <p className="process-flow">Last activity: {runtime.events.at(-1)?.event_type??'Not observed'}</p>
    {market&&(c?<div aria-label="Open candle"><h2>OPEN CANDLE</h2><p>Informational only · Never a closed strategy input</p><dl className="runtime-grid">{[['OPEN',c.open],['HIGH',c.high],['LOW',c.low],['LAST',c.last],['VOLUME',c.volume],['Trades',c.trade_count],['Interval start',c.interval_start],['Interval end',c.interval_end]].map(([k,v])=><div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>{active&&!runtime.demo_mode&&<p>Closes in {Math.max(0,Math.ceil((Date.parse(c.interval_end)-now)/1000))}s</p>}</div>:<p>No accepted trades in current candle</p>)}
    {market&&runtime.market_snapshot&&<p>Last {runtime.market_snapshot.last_price_mxn??'—'} · Bid {runtime.market_snapshot.best_bid_mxn} · Ask {runtime.market_snapshot.best_ask_mxn} · Spread {runtime.market_snapshot.spread_mxn} MXN / {runtime.market_snapshot.spread_bps} bps · Order book {age(runtime.market_snapshot.orderbook_at)}</p>}
  </article>
}
