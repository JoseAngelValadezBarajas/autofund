import {RuntimePanel, type Runtime} from './RuntimePanel';

export interface LiveAccounting {
  allocated_capital:string; max_deployment_mxn:string; single_order_cap:string;
  cash_mxn:string; inventory_btc:string; cost_basis_mxn:string; realized_pnl_mxn:string;
  orders:{origin_id:string; state:string; oid:string|null}[];
  ledger:{entry_id:number; type:string; cash_delta_mxn:string; asset_delta:string; fee_mxn:string}[];
}

export function MicroLivePanel({runtime,page,setPage,state,connected}:{runtime:Runtime & {micro_live:LiveAccounting};page:string;setPage:(page:string)=>void;state:string;connected:boolean}) {
  const live=runtime.micro_live;
  return <main className="micro-live">
    <header>{runtime.demo_mode&&<strong className="demo">DEMO DATA</strong>}<b>AutoFund</b><span>MICRO-LIVE</span><span>BITSO PRODUCTION</span><strong>REAL MONEY</strong><strong>AUTO EXECUTION DISABLED</strong><strong>WRITE CAPABILITY BLOCKED</strong></header>
    <aside>{['Overview','Market','Activity','Ledger','Sessions','System'].map(x=><button key={x} onClick={()=>setPage(x)} aria-current={page===x}>{x}</button>)}</aside>
    <section><small>{state}</small><h1>{page}</h1>
      <div className="real-money" role="status"><strong>REAL MONEY</strong><p>MICRO-LIVE · BITSO PRODUCTION · AUTO EXECUTION DISABLED</p><p>READ ONLY monitoring. Human confirmation happens exclusively in the terminal.</p></div>
      <RuntimePanel runtime={runtime} disconnected={!connected} market={page==='Market'}/>
      {page==='Overview'&&<><div className="cards">{[['Allocated capital',live.allocated_capital+' MXN'],['Max deployment',live.max_deployment_mxn+' MXN'],['Single order cap',live.single_order_cap+' MXN'],['AutoFund BTC inventory',live.inventory_btc+' BTC'],['AutoFund cash',live.cash_mxn+' MXN'],['Cost basis',live.cost_basis_mxn+' MXN'],['Realized P&L',live.realized_pnl_mxn+' MXN']].map(([label,value])=><article key={label}><small>{label}</small><b>{value}</b></article>)}</div><h2>Operator certification order</h2>{live.orders.map(order=><article key={order.origin_id}><strong>{order.state.replaceAll('_',' ')}</strong><p>{order.origin_id}</p><p>Exchange order: {order.oid??'Not submitted'}</p></article>)}<p>Inventory and accounting derive exclusively from confirmed AutoFund fills. Personal exchange balances are private.</p></>}
      {page==='Market'&&<p>BTC/MXN · Market quality: {runtime.quality}. No closed strategy candle is fabricated for operator certification.</p>}
      {page==='Activity'&&<div className="table"><table><thead><tr><th>UTC</th><th>Event</th><th>Summary</th></tr></thead><tbody>{runtime.events.map(event=><tr key={event.event_id}><td>{event.timestamp}</td><td>{event.event_type}</td><td>{event.summary}</td></tr>)}</tbody></table></div>}
      {page==='Ledger'&&<div className="table"><table><thead><tr><th>Entry</th><th>Type</th><th>Cash delta MXN</th><th>BTC delta</th><th>Fee MXN</th></tr></thead><tbody>{live.ledger.map(entry=><tr key={entry.entry_id}><td>{entry.entry_id}</td><td>{entry.type}</td><td>{entry.cash_delta_mxn}</td><td>{entry.asset_delta}</td><td>{entry.fee_mxn}</td></tr>)}</tbody></table></div>}
      {page==='Sessions'&&<dl><dt>Session</dt><dd>{runtime.session_id}</dd><dt>Intent source</dt><dd>OPERATOR_CERTIFICATION</dd><dt>Started UTC</dt><dd>{runtime.started_at}</dd></dl>}
      {page==='System'&&<dl><dt>Dashboard</dt><dd>READ ONLY</dd><dt>Write capability</dt><dd>BLOCKED</dd><dt>Auto execution</dt><dd>DISABLED</dd><dt>Operator confirmation</dt><dd>TERMINAL ONLY</dd><dt>HTTP mutation endpoints</dt><dd>NONE</dd><dt>Accounting</dt><dd>{runtime.accounting_status}</dd></dl>}
    </section>
  </main>;
}
