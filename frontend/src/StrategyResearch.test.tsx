import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {MvpApp} from './MvpApp';

class Events {addEventListener(){} close(){}}

const stopped:any={product_version:'AutoFund MVP 0.1',demo_mode:true,app_state:'STOPPED',auto_execution:false,
 session_id:null,cash_mxn:'50',equity_mxn:'50',deployed_mxn:'0',market_quality:'VALID',accounting_status:'PASS',
 risk_status:'NORMAL',connected:true,kill_triggered:false,position:null,last_signal:'NO_SIGNAL',orders:0,fills:0,
 realized_pnl_mxn:'0',fees_mxn:'0',telemetry:[],champion:{profile_id:'mean-reversion-safe',strategy_id:'mean_reversion',
 version:'0.1',fingerprint:'5a47c9833724e58d64ef37e8702aafaf2e503940e91ad17aa626a72ac53865a8',
 certification_status:'CERTIFIED'},market_regime:'NORMAL',challengers:[],
 production_preflight:{ready:true,label:'READY',reason:'',blockers:[]}};

/** The real economics: a 20 bps Champion target against ~156 bps of round-trip fee. */
const research:any={research_version:'autofund.production-market-selector.v1',
 promotion:'DISABLED',multi_market_production:'DISABLED',
 champion:{profile_id:'mean-reversion-safe',strategy_id:'mean_reversion',version:'0.1',
  fingerprint:'5a47c9833724e58d64ef37e8702aafaf2e503940e91ad17aa626a72ac53865a8',
  certification_status:'CERTIFIED',lifecycle:'ACTIVE_PRODUCTION',intended_gross_edge_bps:'20'},
 profiles:[
  {profile_id:'mean-reversion-safe-v1',strategy_id:'mean_reversion',version:'0.1',
   parameters:{},target_model:{version:'autofund.volatility-aware-target.v1',atr_multiple:'1.5',
   floor_bps:'60',cap_bps:'1200'},fingerprint:'a'.repeat(64),strategy_fingerprint:'b'.repeat(64),
   profile_contract_version:'autofund.strategy-profile.v2',markets:['btc_mxn'],evaluator:'MeanReversionSafeV1',
   description:'frozen',markets_certified:['btc_mxn'],lifecycle:'ACTIVE_PRODUCTION'},
  {profile_id:'trend-continuation-v1',strategy_id:'trend_continuation',version:'0.1',
   parameters:{},target_model:{version:'autofund.volatility-aware-target.v1',atr_multiple:'2.0',
   floor_bps:'250',cap_bps:'1500'},fingerprint:'c'.repeat(64),strategy_fingerprint:'d'.repeat(64),
   profile_contract_version:'autofund.strategy-profile.v2',markets:[],evaluator:'TrendContinuationV1',
   description:'research challenger',markets_certified:[],lifecycle:'RESEARCH'},
  {profile_id:'volatility-mean-reversion-v1',strategy_id:'volatility_mean_reversion',version:'0.1',
   parameters:{},target_model:{version:'autofund.volatility-aware-target.v1',atr_multiple:'1.5',
   floor_bps:'120',cap_bps:'900'},fingerprint:'e'.repeat(64),strategy_fingerprint:'f'.repeat(64),
   profile_contract_version:'autofund.strategy-profile.v2',markets:[],evaluator:'VolatilityAwareMeanReversionV1',
   description:'research challenger',markets_certified:[],lifecycle:'RESEARCH'}],
 economic_viability:{taker_fee_rate:'0.00780000',spread_bps:'10',account_fee_confirmed:true,
  minimum_viable_gross_edge_bps:'165.39160000',champion_intended_gross_edge_bps:'20',
  champion_economically_viable:false},
 markets:[{market:'btc_mxn',status:'ELIGIBLE',strategy_compatibility:'CERTIFIED_FOR_MARKET',
  market_class:'VOLATILE_CRYPTO',lifecycle:'RESEARCH_ONLY'},
  {market:'usd_mxn',status:'ELIGIBLE',strategy_compatibility:'RESEARCH_ONLY',
  market_class:'STABLE_OR_FIAT_LIKE',lifecycle:'RESEARCH_ONLY'}],
 shadow:[{market:'BTC/MXN',evaluations:156,signals:2,shadow_trades:1,gross_pnl_mxn:'0.89',
  fees_mxn:'0.09',net_pnl_mxn:'0.80',max_drawdown_mxn:'0',economic_reject_rate:'0.5',
  data_source:'CAPTURED_MARKET_DATA',evaluated:true,profiles:[{profile_id:'trend-continuation-v1'}]}],
 profile_by_id:['mean-reversion-safe-v1','trend-continuation-v1','volatility-mean-reversion-v1']};

beforeEach(()=>{vi.stubGlobal('EventSource',Events);vi.stubGlobal('fetch',vi.fn().mockResolvedValue({json:async()=>({control_token:'token'})}))});
afterEach(()=>{cleanup();vi.unstubAllGlobals()});

const openProfiles=()=>fireEvent.click(screen.getByRole('button',{name:'Profiles'}));

test('strategy research shows the champion as the only active production profile',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText('Strategy research')).toBeTruthy();
 // Only the Champion is ACTIVE PRODUCTION; the challengers are RESEARCH.
 expect(screen.getAllByText('ACTIVE PRODUCTION').length).toBeGreaterThan(0);
 expect(screen.getAllByText('RESEARCH').length).toBeGreaterThan(0);
 expect(screen.getAllByText('mean-reversion-safe-v1').length).toBeGreaterThan(0);
});

test('the champion is reported not economically viable with the reason shown',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText('Champion economic viability')).toBeTruthy();
 expect(screen.getByText(/refused by the economic guard/)).toBeTruthy();
 expect(screen.getByText('20')).toBeTruthy();
 expect(screen.getByText('165.39160000')).toBeTruthy();
});

test('profiles are listed with their lifecycle, targets and fingerprints',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 for(const id of ['trend-continuation-v1','volatility-mean-reversion-v1'])
   expect(screen.getByText(id)).toBeTruthy();
 expect(screen.getByText('250 bps')).toBeTruthy();
 expect(screen.getByText('1500 bps')).toBeTruthy();
});

test('shadow research is reported per market with friction separated from net',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText('Shadow research per market')).toBeTruthy();
 expect(screen.getByText('BTC/MXN')).toBeTruthy();
 expect(screen.getByText('156')).toBeTruthy();
 expect(screen.getByText('0.80')).toBeTruthy();
 expect(screen.getByText('CAPTURED_MARKET_DATA')).toBeTruthy();
});

test('fiat-like markets are visibly classified and excluded from volatile research',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText('usd_mxn')).toBeTruthy();
 expect(screen.getByText('STABLE_OR_FIAT_LIKE')).toBeTruthy();
});

test('promotion and multi-market production are shown as disabled',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 expect(screen.getByText(/Promotion DISABLED/)).toBeTruthy();
 expect(screen.getByText(/Multi-market Production DISABLED/)).toBeTruthy();
});

test('the research page exposes no manual trading control',()=>{
 render(<MvpApp initial={{...stopped,strategy_research:research}}/>);
 openProfiles();
 for(const name of ['BUY','SELL','PLACE ORDER','PROMOTE','ACTIVATE','ENABLE','FORCE TRADE'])
   expect(screen.queryByRole('button',{name})).toBeNull();
});

test('missing research evidence is stated rather than fabricated',()=>{
 render(<MvpApp initial={{...stopped}}/>);
 openProfiles();
 expect(screen.getByText('Strategy research')).toBeTruthy();
 expect(screen.getByText(/No strategy research evidence published yet/)).toBeTruthy();
});
