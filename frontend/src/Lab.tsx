import {useCallback,useEffect,useState} from 'react';

/**
 * AutoFund Lab: the paper-trading laboratory.
 *
 * Everything here is paper money. The page always says so, always shows that a strategy is
 * UNCERTIFIED unless the private research record says otherwise, and never offers a way to enter a
 * symbol or size: orders come only from a server-built plan that the operator reviews.
 */

export type LabStatus={
  product:string;mode:string;live_trading:string;checked_at:string;warnings:string[];certification:string;
  credentials:{configured:boolean;paper_key:boolean;error:string|null};
  portfolio:null|{
    account:{status:string;equity:string;cash:string;buying_power:string;day_change:string;trading_blocked:boolean};
    clock:{is_open:boolean;next_open:string;next_close:string};
    positions:{symbol:string;qty:string;market_value:string;avg_entry_price:string;current_price:string;unrealized_pl:string}[];
    orders:{id:string;symbol:string;side:string;status:string;notional:string|null;filled_qty:string;filled_avg_price:string|null;submitted_at:string}[]};
  capture:{running:boolean;interval_seconds:number;symbols:string[];started_at:string|null;stopped_at:string|null;
    stop_reason:string|null;polls:number;quotes_written:number;in_session_quotes:number;errors:number;
    last_error:string|null;last_poll_at:string|null;files:number;bytes:number;directory:string;note:string};
  session:{state:string;session_id:string|null;strategy_id:string|null;capital_fraction:string|null;
    auto_rebalance:boolean;started_at:string|null;halt_reason:string|null;last_rebalance:string|null;
    last_plan:Plan|null;last_execution:any;start_phrase:string;flatten_phrase:string;
    limits:{max_capital_fraction:string;max_order_fraction:string;min_order:string}};
  research:{id:string;decision:string;selected_lookback:number|null;holdout_net_sharpe:string;
    benchmark_net_sharpe:string;timing_control_p:string;trials_counted:number|null}[];
  strategies:{id:string;label:string;lookback:number;universe:string[];description:string}[];
};
export type Plan={plan_id:string;strategy_id:string;as_of:string;equity:string;capital:string;executable:boolean;
  blocked:string[];orders:{symbol:string;action:string;notional:string;current_value:string;target_value:string}[]};
type Signal={symbol:string;as_of:string;last_close:string;trailing_return_pct:string;action:string};

const money=(v:string|null|undefined)=>v==null?'—':Number(v).toLocaleString('en-US',{style:'currency',currency:'USD'});
const when=(v:string|null)=>v?new Date(v).toLocaleString():'—';

function Table({head,rows}:{head:string[];rows:(string|number|null)[][]}){
  return <div className="table"><table><thead><tr>{head.map(h=><th key={h}>{h}</th>)}</tr></thead>
    <tbody>{rows.map((r,i)=><tr key={i}>{r.map((c,j)=><td key={j}>{c??'—'}</td>)}</tr>)}</tbody></table></div>;
}

function EquityChart({points}:{points:{t:string;equity:string}[]}){
  if(points.length<2)return <p className="lab-note">The equity curve appears once a session has recorded at least two points.</p>;
  const ys=points.map(p=>Number(p.equity)),lo=Math.min(...ys),hi=Math.max(...ys),span=hi-lo||1;
  const line=points.map((p,i)=>`${20+i*(560/(points.length-1))},${150-(Number(p.equity)-lo)/span*120}`).join(' ');
  return <figure><figcaption>Paper equity ({money(String(lo))} – {money(String(hi))})</figcaption>
    <svg aria-label="Paper equity" className="chart" viewBox="0 0 600 170"><polyline fill="none" stroke="#67d5ff" strokeWidth="3" points={line}/></svg></figure>;
}

export function LabApp(){
  const[status,setStatus]=useState<LabStatus|null>(null),[error,setError]=useState(''),[token,setToken]=useState('');
  const[phrases,setPhrases]=useState({start:'',flatten:''}),[strategy,setStrategy]=useState('momentum-126');
  const[fraction,setFraction]=useState('10'),[auto,setAuto]=useState(false),[interval,setIntervalSeconds]=useState(30);
  const[plan,setPlan]=useState<Plan|null>(null),[signals,setSignals]=useState<Signal[]>([]),[queue,setQueue]=useState(false);
  const[modal,setModal]=useState<''|'start'|'flatten'|'kill'>(''),[typed,setTyped]=useState(''),[busy,setBusy]=useState(false);
  const[journal,setJournal]=useState<any[]>([]),[equity,setEquity]=useState<{t:string;equity:string}[]>([]),[notice,setNotice]=useState('');

  const get=useCallback((path:string)=>fetch('/api/v1/lab/'+path).then(async r=>{
    const body=await r.json().catch(()=>({}));if(!r.ok)throw new Error(typeof body.detail==='string'?body.detail:'Request failed');return body}),[]);
  const post=useCallback(async(path:string,body:object)=>{
    const r=await fetch('/api/v1/lab/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-AutoFund-Control-Token':token},body:JSON.stringify(body)});
    const data=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error(typeof data.detail==='string'?data.detail:'Request rejected');return data},[token]);

  const refresh=useCallback(()=>get('status').then(s=>{setStatus(s);setError('')}).catch(e=>setError(String(e.message))),[get]);
  useEffect(()=>{
    fetch('/api/v1/control/session').then(r=>r.json()).then(x=>{setToken(x.control_token);setPhrases({start:x.start_phrase,flatten:x.flatten_phrase})}).catch(()=>setError('Cannot reach the AutoFund backend'));
    refresh();const id=setInterval(refresh,5000);return()=>clearInterval(id)},[refresh]);
  useEffect(()=>{
    const load=()=>{get('journal?limit=40').then(j=>setJournal(j.items)).catch(()=>{});get('equity').then(e=>setEquity(e.items.map((x:any)=>({t:x.t,equity:x.equity})))).catch(()=>{})};
    load();const id=setInterval(load,10000);return()=>clearInterval(id)},[get]);
  useEffect(()=>{get('signals/'+strategy).then(s=>setSignals(s.rows)).catch(()=>setSignals([]))},[get,strategy,status?.credentials.configured]);

  const act=async(label:string,fn:()=>Promise<any>)=>{
    setBusy(true);setError('');setNotice('');
    try{await fn();setNotice(label+' — done');await refresh()}catch(e:any){setError(label+': '+e.message)}finally{setBusy(false)}};
  const fractionDecimal=()=>String(Number(fraction)/100);

  if(!status)return <main className="lab"><section className="lab-main"><h1>AutoFund Lab</h1><p>{error||'Loading…'}</p></section></main>;
  const p=status.portfolio,s=status.session,c=status.capture,active=s.state==='ACTIVE';
  const marketOpen=!!p?.clock.is_open;

  return <main className="lab">
    <header className="lab-header">
      <b>AutoFund Lab</b><strong className="lab-badge paper">PAPER MONEY ONLY</strong>
      <strong className={'lab-badge '+(status.certification==='UNCERTIFIED'?'warn-badge':'ok-badge')}>{status.certification}</strong>
      <span>LIVE TRADING: {status.live_trading.replace('_',' ')}</span>
      <span className={'lab-badge '+(marketOpen?'ok-badge':'idle-badge')}>MARKET {p?(marketOpen?'OPEN':'CLOSED'):'UNKNOWN'}</span>
    </header>
    <section className="lab-main">
      {error&&<p className="error" role="alert">{error}</p>}
      {notice&&<p className="lab-notice" role="status">{notice}</p>}
      {status.warnings.map(w=><p key={w} className="warn" role="status">{w}</p>)}

      <h2>Paper account</h2>
      {p?<><div className="cards">
        <article><small>Equity</small><b>{money(p.account.equity)}</b></article>
        <article><small>Cash</small><b>{money(p.account.cash)}</b></article>
        <article><small>Day change</small><b className={Number(p.account.day_change)<0?'negative':'positive'}>{money(p.account.day_change)}</b></article>
        <article><small>Buying power</small><b>{money(p.account.buying_power)}</b></article></div>
        <h3>Positions</h3>{p.positions.length?<Table head={['Symbol','Qty','Value','Avg entry','Price','Unrealized P&L']}
          rows={p.positions.map(x=>[x.symbol,x.qty,money(x.market_value),money(x.avg_entry_price),money(x.current_price),money(x.unrealized_pl)])}/>:<p className="lab-note">No open positions.</p>}
        <h3>Recent orders</h3>{p.orders.length?<Table head={['Symbol','Side','Status','Notional','Filled','Avg price']}
          rows={p.orders.map(o=>[o.symbol,o.side,o.status,o.notional?money(o.notional):'—',o.filled_qty,o.filled_avg_price?money(o.filled_avg_price):'—'])}/>:<p className="lab-note">No orders yet.</p>}</>
        :<p className="lab-note">Connect paper keys to see the account.</p>}

      <h2>Data capture</h2>
      <article className="lab-card">
        <p><strong className={'lab-badge '+(c.running?'ok-badge':'idle-badge')}>{c.running?'RUNNING':'STOPPED'}</strong> {c.note}</p>
        <dl className="lab-grid"><dt>Started</dt><dd>{when(c.started_at)}</dd><dt>Last sample</dt><dd>{when(c.last_poll_at)}</dd>
          <dt>Quotes written</dt><dd>{c.quotes_written} ({c.in_session_quotes} in session)</dd><dt>Files</dt><dd>{c.files} · {(c.bytes/1024).toFixed(1)} KB in {c.directory}</dd>
          <dt>Errors</dt><dd>{c.errors}{c.last_error?` · ${c.last_error}`:''}</dd></dl>
        <div className="controls">
          <label>Every <input aria-label="Capture interval seconds" type="number" min={5} max={3600} value={interval} disabled={c.running} onChange={e=>setIntervalSeconds(Number(e.target.value))}/> s</label>
          {c.running?<button className="stop" disabled={busy} onClick={()=>act('Stop capture',()=>post('capture/stop',{reason:'operator'}))}>Stop capture</button>
            :<button className="start" disabled={busy||!status.credentials.configured} onClick={()=>act('Start capture',()=>post('capture/start',{interval_seconds:interval}))}>Start capture</button>}
        </div>
        <p className="lab-note">Capture records real top-of-book quotes for {c.symbols.join(', ')} so spreads can be measured. It is off until you start it and ends when you close the app.</p>
      </article>

      <h2>Signals</h2>
      <label>Strategy <select aria-label="Strategy" value={strategy} onChange={e=>setStrategy(e.target.value)}>
        {status.strategies.map(x=><option key={x.id} value={x.id}>{x.label}</option>)}</select></label>
      <p className="lab-note">{status.strategies.find(x=>x.id===strategy)?.description} Computed from closed daily bars only.</p>
      {signals.length?<Table head={['Symbol','As of','Last close','Trailing return %','Signal']} rows={signals.map(x=>[x.symbol,x.as_of,money(x.last_close),x.trailing_return_pct,x.action])}/>:<p className="lab-note">Signals appear once the keys are configured.</p>}

      <h2>Paper session</h2>
      <article className="lab-card">
        <p><strong className={'lab-badge '+(active?'ok-badge':s.state==='HALTED'?'warn-badge':'idle-badge')}>{s.state}</strong>
          {s.session_id&&<> {s.session_id}</>}{s.halt_reason&&<> — {s.halt_reason}</>}</p>
        {status.certification==='UNCERTIFIED'&&<p className="warn" role="note">UNCERTIFIED EXPERIMENT. No registered hypothesis has passed AutoFund's evidence gates, so this strategy has not
          been shown to beat simply holding. You are running it to see what happens with paper money, not because the evidence supports it.</p>}
        <div className="controls">
          <label>Deploy <input aria-label="Capital percent" type="number" min={1} max={Number(s.limits.max_capital_fraction)*100} value={fraction} disabled={active} onChange={e=>setFraction(e.target.value)}/> % of equity (max {Number(s.limits.max_capital_fraction)*100}%)</label>
          <label><input aria-label="Auto evaluate" type="checkbox" checked={auto} disabled={active} onChange={e=>setAuto(e.target.checked)}/> Auto-evaluate while the app is open</label>
        </div>
        <div className="controls">
          <button disabled={busy||!status.credentials.configured} onClick={()=>act('Build plan',async()=>{setPlan(await post('session/plan',{strategy_id:strategy,capital_fraction:fractionDecimal()}))})}>Preview plan</button>
          {!active&&<button className="start" disabled={busy||!status.credentials.configured} onClick={()=>{setTyped('');setModal('start')}}>Start session…</button>}
          {active&&<button className="stop" disabled={busy} onClick={()=>act('Stop session',()=>post('session/stop',{reason:'operator'}))}>Stop session</button>}
          <button className="kill" disabled={busy||!status.credentials.configured} onClick={()=>setModal('kill')}>KILL</button>
          <button disabled={busy||!status.credentials.configured} onClick={()=>{setTyped('');setModal('flatten')}}>Flatten lab positions…</button>
        </div>
        {plan&&<div className="lab-plan"><h3>Plan {plan.plan_id} · as of {plan.as_of}</h3>
          <p>Equity {money(plan.equity)} · capital to deploy {money(plan.capital)}</p>
          {plan.blocked.map(b=><p key={b} className="warn">BLOCKED: {b}</p>)}
          {plan.orders.length?<Table head={['Symbol','Action','Notional','Now','Target']} rows={plan.orders.map(o=>[o.symbol,o.action,money(o.notional),money(o.current_value),money(o.target_value)])}/>:<p className="lab-note">The portfolio already matches the signals: nothing to trade.</p>}
          {active&&plan.executable&&plan.orders.length>0&&<div className="controls">
            {!marketOpen&&<label><input type="checkbox" checked={queue} onChange={e=>setQueue(e.target.checked)}/> Market is closed: queue these orders for the next open</label>}
            <button className="start" disabled={busy||(!marketOpen&&!queue)} onClick={()=>act('Execute plan',async()=>{await post('session/execute',{plan_id:plan.plan_id,queue_if_closed:queue});setPlan(null)})}>Execute plan (paper)</button></div>}
          {!active&&plan.orders.length>0&&<p className="lab-note">Start a session to execute this plan.</p>}</div>}
        {s.last_execution&&<><h3>Last execution</h3><Table head={['Symbol','Action','Notional','Status','Filled','Avg price']}
          rows={s.last_execution.orders.map((o:any)=>[o.symbol,o.action,money(o.notional),o.status,o.filled_qty??'—',o.filled_avg_price?money(o.filled_avg_price):'—'])}/></>}
        <p className="lab-note">Last rebalance: {s.last_rebalance??'never'}. Caps in code: ≤{Number(s.limits.max_capital_fraction)*100}% of equity deployed, ≤{Number(s.limits.max_order_fraction)*100}% per order, ≥{money(s.limits.min_order)} per order.</p>
      </article>

      <h2>Equity</h2><EquityChart points={equity}/>

      <h2>Research evidence (private)</h2>
      {status.research.length?<Table head={['Hypothesis','Decision','Lookback','Holdout Sharpe','Buy & hold Sharpe','Timing p','Trials']}
        rows={status.research.map(r=>[r.id,r.decision,r.selected_lookback,r.holdout_net_sharpe,r.benchmark_net_sharpe,r.timing_control_p,r.trials_counted])}/>:<p className="lab-note">No evaluations recorded in research/results yet.</p>}

      <h2>Journal</h2>
      {journal.length?<Table head={['Time','Event','Detail']} rows={[...journal].reverse().map(j=>[new Date(j.t).toLocaleTimeString(),j.event,JSON.stringify(Object.fromEntries(Object.entries(j).filter(([k])=>k!=='t'&&k!=='event'))).slice(0,140)])}/>:<p className="lab-note">The journal is empty.</p>}
    </section>

    {modal&&<div className="modal" role="dialog" aria-modal="true"><div>
      {modal==='start'&&<><h2>Start a paper session</h2>
        <p>Paper money only. This strategy is <strong>{status.certification}</strong>. It will use up to {fraction}% of paper equity ({strategy}).</p>
        <label>Type <code>{phrases.start}</code> to authorize<input aria-label="Authorization phrase" value={typed} onChange={e=>setTyped(e.target.value)}/></label>
        <button className="start" disabled={typed!==phrases.start||busy} onClick={()=>{setModal('');act('Start session',()=>post('session/start',{strategy_id:strategy,capital_fraction:fractionDecimal(),auto_rebalance:auto,confirmation:typed}))}}>Authorize</button></>}
      {modal==='flatten'&&<><h2>Flatten lab positions</h2><p>Closes every paper position in the lab's ETFs (SPY, QQQ, TLT, GLD). Other holdings are untouched.</p>
        <label>Type <code>{phrases.flatten}</code><input aria-label="Flatten phrase" value={typed} onChange={e=>setTyped(e.target.value)}/></label>
        <button className="kill" disabled={typed!==phrases.flatten||busy} onClick={()=>{setModal('');act('Flatten',()=>post('session/flatten',{confirmation:typed}))}}>Flatten</button></>}
      {modal==='kill'&&<><h2>Kill session</h2><p>Cancels all open paper orders and halts the session. Positions are kept.</p>
        <button className="kill" disabled={busy} onClick={()=>{setModal('');act('Kill',()=>post('session/kill',{reason:'operator kill'}))}}>Kill now</button></>}
      <button onClick={()=>setModal('')}>Cancel</button></div></div>}
  </main>;
}
