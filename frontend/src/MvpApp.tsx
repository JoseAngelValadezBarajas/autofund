import {useEffect,useState} from 'react';
import {LiveActivityPage,LiveMarketPage,LiveOperatorView,LiveTelemetryPage,type MvpObservability,useTicker} from './LiveOperator';
import {LearningPage,MarketScannerPage,type LearningView,type ScannerEvidence} from './MarketScanner';
import {StrategyResearchPage,type StrategyResearchView} from './StrategyResearch';
import {ProductionMarketsPage,type ProductionMarketsView} from './ProductionMarkets';

type Snapshot={product_version:string;demo_mode:boolean;app_state:string;auto_execution:boolean;session_id:string|null;cash_mxn:string;equity_mxn:string;deployed_mxn:string;market_quality:string;accounting_status:string;risk_status:string;connected:boolean;kill_triggered:boolean;position:any;last_signal:string;orders:number;fills:number;realized_pnl_mxn:string;fees_mxn:string;telemetry:any[];champion:any;market_regime:string;challengers:any[];wallet?:{status:string;error?:string|null;read_only:boolean;balances:{currency:string;total:string;available:string;locked:string;approx_mxn:string|null}[]};execution?:any;production_preflight?:{ready:boolean;label:string;reason:string;blockers:string[];guidance?:string[]};runtime?:MvpObservability['runtime'];observability?:MvpObservability['observability'];market_state?:string;pipeline?:MvpObservability['pipeline'];strategy?:MvpObservability['strategy'];candles?:MvpObservability['candles'];metrics?:MvpObservability['metrics'];signals?:MvpObservability['signals'];economics?:EconomicExitView;blocked_recovery?:MvpObservability['blocked_recovery'];runtime_gaps?:MvpObservability['runtime_gaps'];monitored_seconds?:number|null;session?:MvpObservability['session']&{started_at?:string|null;ended_at?:string|null;actual_runtime_seconds?:number|null;stop_reason?:string|null;frozen?:boolean};learning?:LearningView;scanner?:ScannerEvidence;strategy_research?:StrategyResearchView;production_markets?:ProductionMarketsView};

/**
 * 0.1.3 fee-aware economic exit model for the open AutoFund position.
 *
 * Informational only: it explains why a profit-taking exit may be refused. It is
 * never a control surface, and the operator cannot trade from it.
 */
export interface EconomicExitView{position_open:boolean;classification:string;admissible?:boolean;outcome?:string;
  reason?:string;economically_rejected?:number;strategy_exit_price_mxn?:string;
  fee_only_break_even_price_mxn?:string;estimated_break_even_price_mxn?:string;
  current_best_bid_mxn?:string;expected_net_pnl_if_sold_now_mxn?:string;
  expected_net_pnl_at_strategy_exit_mxn?:string;cost_basis_mxn?:string;owned_quantity?:string;
  average_cost_mxn?:string;distance_to_break_even_bps?:string;
  policy?:{version:string;minimum_net_profit_mxn:string;minimum_net_edge_bps:string}}

/** Compact economic exit panel: no manual BUY/SELL controls, ever. */
function EconomicExitPanel({economics}:{economics:EconomicExitView}){
  if(!economics.position_open)return null;
  const negative=(economics.expected_net_pnl_at_strategy_exit_mxn??'0').startsWith('-');
  return <>
    <h2>ECONOMIC EXIT</h2>
    <p className="economic-status" data-classification={economics.classification}>
      {economics.classification} · strategy exit is {economics.classification==='NET-PROFITABLE'
        ?'expected to net a profit after fees':'expected to net a loss after fees'}
    </p>
    <div className="table"><table><tbody>
      <tr><th>Strategy exit price</th><td>{economics.strategy_exit_price_mxn??'UNKNOWN'} MXN</td></tr>
      <tr><th>Fee-only break-even</th><td>{economics.fee_only_break_even_price_mxn??'UNKNOWN'} MXN</td></tr>
      <tr><th>Estimated break-even</th><td>{economics.estimated_break_even_price_mxn??'UNKNOWN'} MXN</td></tr>
      <tr><th>Current best bid</th><td>{economics.current_best_bid_mxn??'UNKNOWN'} MXN</td></tr>
      <tr><th>Distance to break-even</th><td>{economics.distance_to_break_even_bps??'UNKNOWN'} bps</td></tr>
      <tr><th>Expected P&amp;L if sold now</th><td>{economics.expected_net_pnl_if_sold_now_mxn??'UNKNOWN'} MXN</td></tr>
      <tr><th>Expected P&amp;L at strategy exit</th>
        <td className={negative?'negative':'positive'}>{economics.expected_net_pnl_at_strategy_exit_mxn??'UNKNOWN'} MXN</td></tr>
      <tr><th>Economic rejections</th><td>{economics.economically_rejected??0}</td></tr>
    </tbody></table></div>
    <p>A profit-taking SELL is admitted only when the strategy exit price clears
      break-even after the confirmed account fee. Safety exits are never blocked.</p>
    {economics.policy&&<p className="policy">Economic policy {economics.policy.version} ·
      minimum net profit {economics.policy.minimum_net_profit_mxn} MXN ·
      minimum net edge {economics.policy.minimum_net_edge_bps} bps</p>}
  </>
}

const pages=['Overview','Market','Activity','Wallet','Positions','Ledger','Sessions','Learning','Profiles','Markets','Telemetry','System','Scanner'];

/** True when the backend published a real live observability model. */
function live(data:Snapshot):MvpObservability|null{
  if(!data.runtime||!data.observability||!data.pipeline||!data.strategy||!data.metrics||!data.session)return null;
  return {runtime:data.runtime,observability:data.observability,pipeline:data.pipeline,strategy:data.strategy,
    candles:data.candles??[],metrics:data.metrics,session:data.session,market_state:data.market_state??'NOT_RUNNING'};
}

export function MvpApp({initial}:{initial:Snapshot}){
  const[data,setData]=useState(initial),[token,setToken]=useState(''),[page,setPage]=useState('Overview'),
    [modal,setModal]=useState(''),[phrase,setPhrase]=useState(''),[loss,setLoss]=useState('10'),
    [duration,setDuration]=useState(3600),[orderLimit,setOrderLimit]=useState(10),[error,setError]=useState('');
  const now=useTicker();
  useEffect(()=>{
    fetch('/api/v1/control/session').then(r=>r.json()).then(x=>setToken(x.control_token));
    const es=new EventSource('/api/v1/mvp/stream');
    es.addEventListener('snapshot',e=>setData(JSON.parse((e as MessageEvent).data)));
    return()=>es.close();
  },[]);
  const post=async(action:string,body:any)=>{
    setError('');
    const r=await fetch('/api/v1/control/'+action,{method:'POST',
      headers:{'Content-Type':'application/json','X-AutoFund-Control-Token':token},body:JSON.stringify(body)});
    if(!r.ok){const d=(await r.json()).detail;setError(typeof d==='string'?d:(d?.message??d?.code??'Control rejected'));return}
    setData(await r.json());setModal('');setPhrase('');
  };
  const events=data.telemetry??[];
  const model=live(data);
  const running=data.app_state==='RUNNING';
  return <main className={`mvp state-${data.app_state.toLowerCase()}`}>
    <header>
      <b>AUTOFUND</b><strong>{data.product_version}</strong>
      {data.demo_mode&&<strong className="demo">DEMO DATA</strong>}
      <strong>{data.app_state}</strong><strong>REAL MONEY</strong>
      <span>AUTO EXECUTION {data.auto_execution?'ON':'OFF'}</span><span>50 MXN AUTHORIZED</span>
      <span>Equity {data.equity_mxn} MXN</span><span>Risk {data.risk_status}</span>
      <span>Bitso {data.connected?'CONNECTED':'DISCONNECTED'}</span>
      {model&&<span>Observability {model.observability.status}</span>}
    </header>
    <aside>{pages.map(x=><button key={x} onClick={()=>setPage(x)} aria-current={page===x}>{x}</button>)}</aside>
    <section>
      <h1>{page}</h1>
      {error&&<p className="error">{error}</p>}
      <div className="controls">
        {data.app_state==='STOPPED'&&<button className="start" onClick={()=>setModal('start')}>START AUTOFUND</button>}
        {running&&<><button className="stop" onClick={()=>post('stop',{reason:'operator'})}>STOP SESSION</button>
          <button className="kill" onClick={()=>setModal('kill')}>EMERGENCY KILL</button></>}
      </div>

      {page==='Overview'&&(running&&model
        ?<LiveOperatorView model={model} now={now} sessionId={data.session_id} equity={data.equity_mxn}
            orders={data.orders} fills={data.fills} champion={data.champion?.profile_id} regime={data.market_regime}/>
        :<>
          <div className="real-money">
            <strong>{data.app_state==='RUNNING'?'RUNNING REAL MONEY':data.app_state==='HALTED'?'AUTOFUND HALTED - NEW WRITES BLOCKED':'REAL MONEY SESSION SAFE'}</strong>
            <p>AUTO EXECUTION {data.auto_execution?'ON':'OFF'} · BTC/MXN · Kill switch {data.kill_triggered?'TRIGGERED':'READY'}</p>
          </div>
          <Cards rows={[['Authorized capital','50 MXN'],['Current equity',data.equity_mxn+' MXN'],
            ['Deployed',data.deployed_mxn+' MXN'],['Cash',data.cash_mxn+' MXN'],['Position',data.position?'OPEN':'NONE'],
            ['Last strategy decision',data.last_signal],['Market quality',data.market_quality],
            ['Accounting',data.accounting_status],['Risk',data.risk_status],
            ['Production preflight',data.production_preflight?.label??'BLOCKED']]}/>
          {!data.production_preflight?.ready&&<p className="error">Production preflight {data.production_preflight?.label??'BLOCKED'}: {data.production_preflight?.reason||'PRODUCTION_PREFLIGHT_BLOCKED'}{data.production_preflight?.guidance?.length?' — '+data.production_preflight.guidance.join(' '):''}</p>}
          <h2>Current trading</h2>
          <p>Signal: {data.last_signal} · Final market validation: {events.some(e=>e.event==='FINAL_MARKET_CHECK_PASS')?'PASS':'Not performed'} · Orders: {data.orders} · Fills: {data.fills}</p>
        </>)}

      {page==='Market'&&(model&&(running||data.app_state==='HALTED')
        ?<LiveMarketPage model={model} now={now}/>
        :<article><h2>BTC/MXN</h2><p>Quality {data.market_quality} · Regime {data.market_regime}</p>
          <p>Closed candles drive strategy decisions. Final market validation is mandatory before every write.</p>
          <p>{data.app_state==='STOPPED'?'No session is running; live market data is not published.':'Live market data unavailable.'}</p></article>)}

      {page==='Activity'&&<LiveActivityPage events={model?.runtime.events??[]} now={now}/>}

      {page==='Wallet'&&<><div className="real-money"><strong>BITSO WALLET — READ ONLY</strong>
        <p>Status {data.wallet?.status??'UNAVAILABLE'}{data.wallet?.error?` · ${data.wallet.error}`:''}</p></div>
        <p>Bitso Wallet may contain funds that do not belong to AutoFund&apos;s trading envelope.</p>
        <div className="table"><table><thead><tr><th>Currency</th><th>Total</th><th>Available</th>
          <th>Locked</th><th>Approx MXN</th></tr></thead><tbody>{(data.wallet?.balances??[]).map(row=><tr key={row.currency}>
          <td>{row.currency}</td><td>{row.total}</td><td>{row.available}</td><td>{row.locked}</td>
          <td>{row.approx_mxn??'UNKNOWN'}</td></tr>)}</tbody></table></div>
        {!data.wallet?.balances?.length&&<p>No non-zero wallet balances available.</p>}
        <h2>AUTOFUND OWNED</h2><dl><dt>Allocated cash</dt><dd>50 MXN</dd>
          <dt>Current AutoFund cash</dt><dd>{data.cash_mxn} MXN</dd>
          <dt>Owned BTC</dt><dd>{data.position?.quantity??'0'}</dd>
          <dt>Average cost</dt><dd>{data.position?.average_cost_mxn??'0'} MXN</dd>
          <dt>Current mark</dt><dd>{data.position?.mark_mxn??'UNKNOWN'} MXN</dd>
          <dt>Market value</dt><dd>{data.position?.market_value_mxn??'0'} MXN</dd>
          <dt>Realized P&amp;L</dt><dd>{data.realized_pnl_mxn} MXN</dd>
          <dt>Unrealized P&amp;L</dt><dd>{data.position?.unrealized_pnl_mxn??'0'} MXN</dd>
          <dt>Fees</dt><dd>{Object.keys(data.execution?.fees_by_currency??{}).length
            ? JSON.stringify(data.execution?.fees_by_currency) : '0'}</dd>
          <dt>Equity</dt><dd>{data.equity_mxn} MXN</dd></dl></>}

      {page==='Positions'&&<>{data.position
        ?<div className="table"><table><tbody>
            <tr><th>Asset</th><td>{data.position.asset}</td></tr>
            <tr><th>AutoFund-owned quantity</th><td>{data.position.quantity}</td></tr>
            <tr><th>Average cost MXN</th><td>{data.position.average_cost_mxn}</td></tr>
            <tr><th>Mark MXN</th><td>{data.position.mark_mxn}</td></tr>
            <tr><th>Market value MXN</th><td>{data.position.market_value_mxn}</td></tr>
            <tr><th>Realized P&amp;L MXN</th><td>{data.position.realized_pnl_mxn}</td></tr>
            <tr><th>Unrealized P&amp;L MXN</th><td>{data.position.unrealized_pnl_mxn}</td></tr>
            <tr><th>Fees</th><td>{data.position.fees_mxn}</td></tr>
            <tr><th>Strategy version</th><td>{data.position.strategy_version}</td></tr>
          </tbody></table></div>
        :<p>No AutoFund-owned position — position NONE.</p>}
        {data.economics&&<EconomicExitPanel economics={data.economics}/>}
        <p>Only inventory derived from confirmed AutoFund fills is shown. Unrelated Bitso balances are never displayed.</p></>}

      {page==='Ledger'&&<p>Ledger updates: {events.filter(e=>e.event==='LEDGER_UPDATED').length} · Realized P&amp;L {data.realized_pnl_mxn} MXN · Fees {data.fees_mxn} MXN</p>}

      {page==='Sessions'&&<><dl>
        <dt>Current session</dt><dd>{data.session_id??'None'}</dd><dt>State</dt><dd>{data.app_state}</dd>
        <dt>Market</dt><dd>{(data.learning?.scanner?.live_market??'btc_mxn').toUpperCase()}</dd>
        <dt>Strategy / profile</dt><dd>{data.learning?.champion?.profile_id??data.champion.profile_id}</dd>
        <dt>Orders / fills</dt><dd>{data.orders} / {data.fills}</dd>
        {model&&<><dt>Started</dt><dd>{data.session?.started_at??'—'}</dd>
          <dt>Ended</dt><dd>{data.session?.ended_at??(data.session?.frozen?'—':'IN PROGRESS')}</dd>
          <dt>Actual duration</dt><dd>{data.session?.actual_runtime_seconds??data.session?.elapsed_seconds??'UNKNOWN'}s</dd>
          <dt>Configured max duration</dt><dd>{model.session.max_duration_seconds??'UNKNOWN'}s</dd>
          <dt>Stop reason</dt><dd>{data.session?.stop_reason??(data.session?.frozen?'UNKNOWN':'NOT_STOPPED')}</dd>
          <dt>Max orders</dt><dd>{model.session.max_orders_per_session??'UNKNOWN'}</dd>
          <dt>Loss limit</dt><dd>{model.session.max_session_loss_mxn??'UNKNOWN'} MXN</dd></>}
      </dl>{data.session_id&&<a className="export" href={`/api/v1/sessions/${data.session_id}/diagnostics`}>Export session diagnostics</a>}</>}

      {page==='Learning'&&(data.learning
        ?<LearningPage learning={data.learning} activity={events}/>
        :<><h2>Current Champion</h2><dl>
          <dt>Profile</dt><dd>{data.champion.profile_id}</dd><dt>Certification</dt><dd>{data.champion.certification_status}</dd>
          <dt>Market regime</dt><dd>{data.market_regime}</dd><dt>Promotion</dt><dd>MANUAL</dd>
          <dt>Auto-promotion</dt><dd>DISABLED</dd></dl>
          <p>{data.challengers.length} active challengers · DO_NOT_TRADE is valid.</p></>)}

      {page==='Scanner'&&(data.scanner
        ?<MarketScannerPage scanner={data.scanner}/>
        :<article><h2>Market opportunity scanner</h2><p>Scanner evidence unavailable.</p></article>)}

      {page==='Profiles'&&<StrategyResearchPage research={data.strategy_research}/>}

      {page==='Markets'&&<ProductionMarketsPage markets={data.production_markets}/>}

      {page==='Telemetry'&&(model
        ?<LiveTelemetryPage model={model} now={now}/>
        :<Cards rows={[['Checkpoints',String(events.length)],
          ['Warnings',String(events.filter(e=>e.level==='WARNING').length)],
          ['Errors',String(events.filter(e=>['ERROR','CRITICAL'].includes(e.level)).length)],
          ['Halts',String(events.filter(e=>e.event.includes('HALT')||e.event.includes('KILL')).length)]]}/>)}

      {page==='System'&&<dl><dt>Product</dt><dd>{data.product_version}</dd>
        <dt>Control API</dt><dd>START / STOP / KILL ONLY</dd><dt>Exchange mutation</dt><dd>POST /api/v3/orders ONLY</dd>
        <dt>Withdrawals / transfers</dt><dd>NOT PRESENT</dd><dt>Margin / futures / leverage</dt><dd>DISABLED</dd>
        <dt>Auto-promotion</dt><dd>DISABLED</dd>
        {model&&<><dt>Observability</dt><dd>{model.observability.status}</dd>
          <dt>Observability scope</dt><dd>{model.observability.scope}</dd>
          <dt>Trading effect of observability guard</dt><dd>{model.observability.trading_effect}</dd>
          <dt>Browser tab closure effect</dt><dd>{model.observability.browser_tab_closure_effect}</dd></>}</dl>}
    </section>

    {modal==='start'&&<div className="modal" role="dialog"><div>
      <h2>REAL MONEY SESSION</h2>
      <p>Authorized capital: 50 MXN<br/>Maximum deployment: 25 MXN<br/>Maximum single order: 11 MXN<br/>
        Automatic execution: WILL BE ENABLED<br/>Withdrawals: NOT AVAILABLE IN AUTOFUND<br/>Leverage: DISABLED</p>
      <label>Maximum session loss (MXN)<input aria-label="Maximum session loss" value={loss} onChange={e=>setLoss(e.target.value)}/></label>
      <label>Maximum duration (seconds)<input aria-label="Maximum duration" type="number" value={duration} onChange={e=>setDuration(Number(e.target.value))}/></label>
      <label>Maximum orders<input aria-label="Maximum orders" type="number" value={orderLimit} onChange={e=>setOrderLimit(Number(e.target.value))}/></label>
      <label>Type START AUTOFUND REAL 50<input aria-label="Strong confirmation" value={phrase} onChange={e=>setPhrase(e.target.value)}/></label>
      <button disabled={phrase!=='START AUTOFUND REAL 50'} onClick={()=>post('start',{confirmation:phrase,max_session_loss_mxn:loss,max_session_duration_seconds:duration,max_orders_per_session:orderLimit})}>AUTHORIZE REAL SESSION</button>
      <button onClick={()=>setModal('')}>Cancel</button></div></div>}
    {modal==='kill'&&<div className="modal" role="dialog"><div>
      <h2>EMERGENCY KILL AUTOFUND?</h2><p>Blocks all new writes immediately. It does not liquidate.</p>
      <button className="kill" onClick={()=>post('kill',{reason:'operator emergency kill'})}>KILL NOW</button>
      <button onClick={()=>setModal('')}>Cancel</button></div></div>}
  </main>;
}

function Cards({rows}:{rows:string[][]}){return <div className="cards">{rows.map(([a,b])=><article key={a}><small>{a}</small><b>{b}</b></article>)}</div>}
