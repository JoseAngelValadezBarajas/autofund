import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {MvpApp} from './MvpApp';

/**
 * Research Control Center (MVP 0.3.0).
 *
 * These tests exist to protect one property above all others: that a green engineering
 * state never reads as profitable evidence, and that positive research evidence never
 * reads as permission to trade. The four truths are asserted as four independent
 * values, because a single combined "status" is exactly the failure this milestone
 * was built to prevent.
 *
 * The fixtures are the real published values from the 0.2.6, 0.2.7 and 0.2.8
 * certifications, including the uncomfortable ones: 2.5136 bps of validated movement
 * against 173.00 bps of required friction, and a cross-venue capture that covered 0.18
 * of the 72 hours it predeclared.
 */

class Events{addEventListener(){} close(){}}

const stopped:any={product_version:'AutoFund MVP 0.3.0',demo_mode:true,app_state:'STOPPED',
 auto_execution:false,session_id:null,cash_mxn:'50',equity_mxn:'50',deployed_mxn:'0',
 market_quality:'VALID',accounting_status:'PASS',risk_status:'NORMAL',connected:true,
 kill_triggered:false,position:null,last_signal:'NO_SIGNAL',orders:0,fills:0,
 realized_pnl_mxn:'0',fees_mxn:'0',telemetry:[],champion:{profile_id:'mean-reversion-safe',
 certification_status:'CERTIFIED'},market_regime:'NORMAL',challengers:[],
 production_preflight:{ready:true,label:'READY',reason:'',blockers:[]}};

/** The real overview: a healthy system, one predictive signal, nothing tradeable. */
const overview:any={
 schema_version:'autofund.research-registry.v1',
 status:{engineering:'HEALTHY',research:'ACCUMULATING',production:'DISABLED',
  current_action:'NO_TRADE',built_at:'2026-09-26T09:38:01+00:00',
  statuses_are_independent:true,
  note:'engineering health, evidence quality, economic result and production authorization are four separate facts and are never combined'},
 portfolio:{cash_mxn:'50',equity_mxn:'50',deployed_mxn:'0',remaining_deployment_mxn:'25',
  authorized_capital_mxn:'50',max_deployment_mxn:'25',single_order_cap_mxn:'11',
  realized_pnl_mxn:'0',unrealized_pnl_mxn:null,fees_mxn:'0',fills:0,orders:0,
  autofund_inventory:[],unresolved_orders:[],has_unresolved_order:false,
  accounting_status:'PASS',wallet_is_not_inventory:true,
  source:'authoritative ledger and journal',
  exchange_wallet:{status:'UNAVAILABLE',error:null,read_only:true,observed_at:null,balances:[]}},
 engineering:{app_state:'BOOTING',execution:'HEALTHY',ledger:'HEALTHY',market_data:'VALID',
  collectors:'HEALTHY',control_state:'BOOTING',overall:'HEALTHY',
  detail:{blocked_recovery:false,runtime_gaps:0},says_nothing_about_profitability:true},
 research:{price_only:'FROZEN',cross_market_alpha:'PREDICTIVE_NOT_ECONOMIC',
  microstructure:'ACCUMULATING',cross_venue:'INSUFFICIENT_EVIDENCE',experiments_total:11,
  experiments_completed:11,validated_information_signals:1,economically_usable_signals:0,
  strategies_frozen:3,strategies_not_viable:3,strategies_active_production:0,
  active_campaigns:[],accumulating_campaigns:['CROSS_VENUE_CAPTURE','MICROSTRUCTURE_CAPTURE']},
 production:{certified_opportunities:0,production_eligible:0,session_authorized:false,
  unresolved_orders:0,current_action:'NO_TRADE',
  reasons:['NOT_VIABLE','INSUFFICIENT_EVIDENCE','PREDICTIVE_NOT_ECONOMIC'],
  authorization_state:'DISABLED',demo_mode:false,auto_execution:false,
  evidence_does_not_imply_authorization:true},
 latest_experiment:null,latest_evidence:null};

/** Real 0.2.6 / 0.2.7 numbers: a signal that predicts and cannot pay for itself. */
const signalAlpha:any={alpha_id:'VALIDATED_INFORMATION_SIGNAL_V1',name:'Validated information signal',
 family:'CROSS_MARKET',version:'1',
 fingerprint:'fae4faa85ef648acb5e831cb6bc3fe242d8a1723cc3c474262a886a4600ab3aa',
 classification:'PREDICTIVE_NOT_ECONOMIC',prediction_status:'PREDICTIVE',
 economic_status:'NOT_ECONOMIC',markets:['BTC/MXN','ETH/MXN'],prediction_horizon:'5 minutes',
 development_status:'COMPLETED',validation_status:'PASSED',
 movement_bps:'2.513590292581896613688157726',required_friction_bps:'173.00',
 economic_headroom_bps:'-170.4864097074181033863118423',tradeable:false,
 evidence_provenance:['REAL_HISTORICAL_DEVELOPMENT'],reason_codes:['FRICTION_EXCEEDS_MOVEMENT'],
 source_experiment:'mvp-0-2-6',artifact_refs:['mvp/alpha/mvp-0-2-6-certification.json'],
 created_at:'2026-09-25T00:00:00+00:00',frozen_at:'2026-09-25T00:00:00+00:00',
 is_a_strategy:false,hypothesis:'Order flow predicts short-horizon direction',
 definition:'Validated predictive signal, not economically usable at retail friction'};

const frozenAlpha:any={...signalAlpha,alpha_id:'PRICE_ONLY_RESEARCH_BASELINE',
 name:'Price-only research baseline',family:'PRICE_ONLY',classification:'FROZEN',
 prediction_status:'NOT_PREDICTIVE',economic_status:'NOT_ECONOMIC',movement_bps:null,
 required_friction_bps:null,economic_headroom_bps:null,tradeable:false,
 prediction_horizon:'NOT_RECORDED',validation_status:'NOT_APPLICABLE',
 created_at:null,frozen_at:null,reason_codes:['NO_CURRENT_EDGE']};

/** Engineering PASS with economics FAIL - the combination this project lives in. */
const strategyRow:any={profile_id:'mean-reversion-safe-v1',strategy_id:'mean_reversion_safe',
 version:'NOT_RECORDED',
 fingerprint:'1cadcfa967a919b6d041cf382d5acc993d7c70c7c1b008bba2db764c4f51bb6e',
 status:'NOT_VIABLE',markets_evaluated:['BTC/MXN','ETH/MXN'],timeframes:['15m','1h'],
 alpha_dependency:null,entry_model:'OHLC indicator entry',exit_model:'target or invalidation',
 risk_model:'volatility-scaled stop',economic_policy_fingerprint:'autofund.economic-edge.v1',
 development_evidence:{},holdout_evidence:{},forward_shadow_evidence:{},
 production_certification:'NOT_CERTIFIED',reason_codes:['NEGATIVE_NET_ECONOMICS'],
 predecessor_profile:null,successor_profile:null,frozen:true,is_frozen_terminal:true,
 engineering_status:'PASS',economic_status:'FAIL',risk_status:'UNKNOWN',
 production_eligible:false,round_trips:null,net_pnl_mxn:null,max_drawdown_mxn:null,
 median_mae_mxn:null,median_mfe_mxn:null,economic_rejection_rate:null,
 risk_rejection_rate:null,mae_mfe_available:false,artifact_refs:[]};

const experimentRow:any={experiment_id:'mvp-0-2-8',milestone:'MVP 0.2.8',
 title:'Cross-venue rare dislocation alpha',
 hypothesis:'Rare cross-venue dislocations are large enough to clear Bitso friction',
 baseline_commit:'0cef2fb',fingerprint:null,status:'COMPLETED',
 started_at:'2026-09-26T08:38:33+00:00',finished_at:'2026-09-26T08:38:33+00:00',
 development_interval:null,holdout_interval:{hours:720},forward_interval:null,
 markets:['BTC/MXN','ETH/MXN','SOL/MXN'],strategies:[],alpha_sources:['CROSS_VENUE_DISLOCATION'],
 economic_policy_fingerprint:null,risk_policy_fingerprint:null,dataset_fingerprints:[],
 artifact_refs:['mvp/cross-venue/mvp-0-2-8-certification.json'],
 result_classification:'INSUFFICIENT_CROSS_VENUE_EVIDENCE',
 reason_codes:['coverage 0.176h against a predeclared 72h'],test_summary:'1439 passed',
 superseded_by:null,supersedes:[]};

const evidenceRow:any={evidence_id:'mvp-0-2-8:capture',provenance:'REAL_CAPTURED_CROSS_VENUE',
 experiment_id:'mvp-0-2-8',dataset_fingerprint:'e2c29293ec15a7d7',start:'2026-09-26T08:00:00+00:00',
 end:'2026-09-26T08:38:00+00:00',markets:['BTC/MXN'],observation_count:306,quality:'SUFFICIENT',
 gaps:0,artifact_path:'mvp/cross-venue/capture/btc_mxn.00000.jsonl',artifact_bytes:265831,
 summary:{},alpha_sources:['CROSS_VENUE_DISLOCATION'],strategies:[],is_real_observation:true};

/** A healthy collector whose evidence is insufficient: the two facts must not merge. */
const campaignRow:any={campaign_id:'CROSS_VENUE_CAPTURE',title:'Cross-venue dislocation capture',
 experiment_id:'mvp-0-2-8',experiment_fingerprint:null,status:'STOPPED',process_health:'HEALTHY',
 evidence_conclusion:'INSUFFICIENT_CROSS_VENUE_EVIDENCE',planned_duration_hours:'72',
 actual_coverage_hours:'0.1762589675',coverage_percent:'0.2448041215',markets:['BTC/MXN'],
 observations:216,gaps:0,latest_sample_at:null,storage_bytes:265831,
 evidence_provenance:'REAL_CAPTURED_CROSS_VENUE',coverage_sufficient:false,
 artifact_paths:['mvp/cross-venue/capture/btc_mxn.00000.jsonl'],
 health_is_separate_from_conclusion:true};

const eligibilityRow:any={market:'BTC/MXN',profile_id:'mean-reversion-safe-v1',
 alpha_source:'VALIDATED_INFORMATION_SIGNAL_V1',research_status:'NOT_VIABLE',data_quality:'OK',
 strategy_compatibility:'INCOMPATIBLE',economic_status:'FAIL',risk_status:'UNKNOWN',
 certification_status:'NOT_CERTIFIED',authorization_status:'PRODUCTION_DISABLED',
 position_status:'NONE',blocking_reason:'NOT_VIABLE',eligible:false};

const paged=(items:any[],extra:any={})=>({total:items.length,offset:0,limit:100,
 returned:items.length,items,...extra});

/** Route the fetch mock by path so each page receives its own contract. */
const routes:{[path:string]:any}={
 '/api/v1/research/overview':overview,
 '/api/v1/research/alpha':paged([signalAlpha,frozenAlpha]),
 '/api/v1/research/strategies':paged([strategyRow]),
 '/api/v1/research/experiments':paged([experimentRow]),
 '/api/v1/research/evidence':paged([evidenceRow]),
 '/api/v1/research/campaigns':paged([campaignRow],{health_is_separate_from_conclusion:true}),
 '/api/v1/research/eligibility':paged([eligibilityRow]),
 '/api/v1/research/artifacts':paged([{relative_path:'mvp/x/cert.json',artifact_type:'CERTIFICATION',
   status:'INDEXED',archive_state:'CURRENT',size_bytes:1024,modified_at:null,
   fingerprint:'abc123def4567890abc123def4567890',experiment_id:'mvp-0-2-8',summary:{},error:null}]),
 '/api/v1/research/timeline':paged([{at:'2026-09-26T08:38:33+00:00',kind:'EXPERIMENT_COMPLETED',
   subject:'mvp-0-2-8',detail:'INSUFFICIENT_CROSS_VENUE_EVIDENCE',source:'research registry'}],
   {read_only:true,financial_events_come_from_their_own_source:true,
    financial_events_included_here:false}),
};

beforeEach(()=>{
  vi.stubGlobal('EventSource',Events);
  vi.stubGlobal('fetch',vi.fn((url:string)=>{
    // Pages append pagination query strings, so key the mock on the path alone.
    const path=url.split('?')[0];
    return Promise.resolve({
      ok:true,
      json:async()=>path.includes('control/session')?{control_token:'token'}
        :(routes[path]??{...paged([]),api_version:'autofund.research-api.v1'}),
    });
  }));
});
afterEach(()=>{cleanup();vi.unstubAllGlobals()});

const open=(name:string)=>fireEvent.click(screen.getByRole('button',{name}));
/** Research pages fetch asynchronously; wait for the payload to land. */
const settle=()=>new Promise(resolve=>setTimeout(resolve,0));
const attr=(selector:string,name:string)=>
  Array.from(document.querySelectorAll(selector)).map(n=>n.getAttribute(name));

test('the research navigation is available and read-only',()=>{
 render(<MvpApp initial={stopped}/>);
 for(const page of ['Control Center','Alpha Registry','Strategy Registry','Experiments',
   'Evidence','Campaigns','Timeline','Eligibility','Artifacts'])
   expect(screen.getByRole('button',{name:page})).toBeTruthy();
});

test('the control center shows the four truths as four separate values',async()=>{
 render(<MvpApp initial={stopped}/>);
 open('Control Center');
 await settle();
 for(const [dimension,value] of [['ENGINEERING','HEALTHY'],['RESEARCH','ACCUMULATING'],
   ['PRODUCTION','DISABLED'],['ACTION','NO_TRADE']]){
   const node=document.querySelector(`[data-dimension="${dimension}"]`);
   expect(node).toBeTruthy();
   expect(node!.getAttribute('data-value')).toBe(value);
 }
 expect(document.querySelector('[data-statuses-independent="true"]')).toBeTruthy();
});

test('a healthy system is not presented as profitable evidence',async()=>{
 render(<MvpApp initial={stopped}/>);
 open('Control Center');
 await settle();
 // Engineering is HEALTHY and there is nothing to trade. Both render, unmerged.
 expect(document.querySelector('[data-dimension="ENGINEERING"]')!.getAttribute('data-value'))
   .toBe('HEALTHY');
 expect(document.querySelector('[data-dimension="ACTION"]')!.getAttribute('data-value'))
   .toBe('NO_TRADE');
 expect(document.querySelector('[data-says-nothing-about-profitability="true"]')).toBeTruthy();
 // The panel must not claim profitability anywhere.
 expect(screen.queryByText(/PROFITABLE/i)).toBeNull();
 expect(screen.queryByText(/PROFIT EXPECTED/i)).toBeNull();
});

test('positive research evidence is not presented as authorization',async()=>{
 render(<MvpApp initial={stopped}/>);
 open('Control Center');
 await settle();
 expect(document.querySelector('[data-session-authorized="false"]')).toBeTruthy();
 expect(document.querySelector(
   '[data-evidence-does-not-imply-authorization="true"]')).toBeTruthy();
 expect(screen.getByText(/Research evidence does not imply authorization/)).toBeTruthy();
});

test('wallet and AutoFund inventory are reported separately, never summed',async()=>{
 render(<MvpApp initial={stopped}/>);
 open('Control Center');
 await settle();
 const holder=document.querySelector('[data-wallet-is-not-inventory="true"]');
 expect(holder).toBeTruthy();
 expect(holder!.getAttribute('data-autofund-inventory-count')).toBe('0');
 expect(holder!.getAttribute('data-exchange-wallet-balances')).toBe('0');
  // Remaining deployment is capped at the frozen limit, not inflated by wallet balances.
  const remaining=Array.from(document.querySelectorAll('.cards article'))
    .find(card=>card.textContent?.includes('Remaining deployment'));
  expect(remaining?.textContent).toContain('25 MXN');
});

test('the alpha registry separates prediction from economics',async()=>{
 render(<MvpApp initial={stopped}/>);
 open('Alpha Registry');
 await settle();
 const row=document.querySelector('[data-alpha="VALIDATED_INFORMATION_SIGNAL_V1"]');
 expect(row).toBeTruthy();
 expect(row!.getAttribute('data-prediction')).toBe('PREDICTIVE');
 expect(row!.getAttribute('data-economic')).toBe('NOT_ECONOMIC');
 expect(row!.getAttribute('data-classification')).toBe('PREDICTIVE_NOT_ECONOMIC');
 // A predictive signal must not be labelled as tradeable.
 expect(row!.getAttribute('data-tradeable')).toBe('false');
});

test('the alpha registry shows the real movement against the real required friction',async()=>{
 render(<MvpApp initial={stopped}/>);
 open('Alpha Registry');
 await settle();
 expect(screen.getByText(/2\.513590292581896613688157726 bps/)).toBeTruthy();
 expect(screen.getByText('173.00 bps')).toBeTruthy();
 expect(screen.getByText(/-170\.4864097074181033863118423 bps/)).toBeTruthy();
});

test('a strategy can be engineering PASS and economic FAIL at the same time',async()=>{
 render(<MvpApp initial={stopped}/>);
 open('Strategy Registry');
 await settle();
 const row=document.querySelector('[data-profile="mean-reversion-safe-v1"]');
 expect(row).toBeTruthy();
 expect(row!.getAttribute('data-engineering')).toBe('PASS');
 expect(row!.getAttribute('data-economic')).toBe('FAIL');
 expect(row!.getAttribute('data-production-eligible')).toBe('false');
 expect(row!.getAttribute('data-frozen-terminal')).toBe('true');
});

test('unrecorded MAE and MFE are labelled, never invented',async()=>{
 render(<MvpApp initial={stopped}/>);
 open('Strategy Registry');
 await settle();
 // These are the values a plausible-looking implementation would have made up.
 expect(screen.getByText(/NOT_RECORDED — no capture exists that could produce these/)).toBeTruthy();
});

test('the experiment registry reports a negative result as a result',async()=>{
 render(<MvpApp initial={stopped}/>);
 open('Experiments');
 await settle();
 expect(screen.getByText('INSUFFICIENT_CROSS_VENUE_EVIDENCE')).toBeTruthy();
 expect(screen.getByText('MVP 0.2.8')).toBeTruthy();
});

test('a missing window is reported as not recorded rather than blank',async()=>{
 render(<MvpApp initial={stopped}/>);
 open('Experiments');
 await settle();
 // The forward window genuinely does not exist; the holdout window does.
 expect(screen.getAllByText('NOT_RECORDED').length).toBeGreaterThan(0);
});

test('the evidence explorer distinguishes real observation from a fixture',async()=>{
 render(<MvpApp initial={stopped}/>);
 open('Evidence');
 await settle();
 const row=document.querySelector('[data-evidence="mvp-0-2-8:capture"]');
 expect(row).toBeTruthy();
 expect(row!.getAttribute('data-provenance')).toBe('REAL_CAPTURED_CROSS_VENUE');
 expect(row!.getAttribute('data-real-observation')).toBe('true');
});

test('a healthy collector with insufficient coverage is not called a market conclusion',async()=>{
 render(<MvpApp initial={stopped}/>);
 open('Campaigns');
 await settle();
 const node=document.querySelector('[data-campaign="CROSS_VENUE_CAPTURE"]');
 expect(node).toBeTruthy();
 // Health and conclusion are different questions, and both answers are shown.
 expect(node!.getAttribute('data-process-health')).toBe('HEALTHY');
 expect(node!.getAttribute('data-evidence-conclusion'))
   .toBe('INSUFFICIENT_CROSS_VENUE_EVIDENCE');
 expect(node!.getAttribute('data-coverage-sufficient')).toBe('false');
 expect(screen.getByText(/a rare event could not have been observed/)).toBeTruthy();
});

test('the timeline does not duplicate financial events',async()=>{
 render(<MvpApp initial={stopped}/>);
 open('Timeline');
 await settle();
 expect(document.querySelector('[data-financial-events-included="false"]')).toBeTruthy();
 // Research events are shown; no FILL or ORDER event is copied in from the journal.
 expect(screen.getByText('EXPERIMENT_COMPLETED')).toBeTruthy();
 expect(screen.queryByText('FILL')).toBeNull();
});

test('every ineligible market carries a blocking reason',async()=>{
 render(<MvpApp initial={stopped}/>);
 open('Eligibility');
 await settle();
 const reasons=attr('[data-market]','data-blocking-reason');
 expect(reasons.length).toBeGreaterThan(0);
 for(const reason of reasons) expect(reason).toBeTruthy();
 for(const eligible of attr('[data-market]','data-eligible'))
   expect(eligible).toBe('false');
});

test('the artifact index shows fingerprints and excludes derived output',async()=>{
 render(<MvpApp initial={stopped}/>);
 open('Artifacts');
 await settle();
 expect(screen.getByText('mvp/x/cert.json')).toBeTruthy();
 // The registry snapshot is derived from the index and must not appear inside it.
 expect(screen.queryByText(/research-registry\.json/)).toBeNull();
});

test('the control center exposes no order ticket or manual trade control',async()=>{
 render(<MvpApp initial={stopped}/>);
 open('Control Center');
 await settle();
 for(const banned of ['BUY','SELL','Order ticket','Submit order','Quantity'])
   expect(screen.queryByText(banned)).toBeNull();
 expect(screen.queryByRole('textbox')).toBeNull();
});

test('an unavailable registry degrades honestly instead of blanking the page',async()=>{
 vi.stubGlobal('fetch',vi.fn((url:string)=>Promise.resolve({
   ok:false,status:503,json:async()=>({}),
 })));
 render(<MvpApp initial={stopped}/>);
 open('Control Center');
 await settle();
 const alert=screen.getByRole('alert');
 // A read-model failure says nothing about trading, and the message says so.
 expect(alert.textContent).toContain('says nothing about trading state');
});
