import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {MvpApp} from './MvpApp';

class Events {addEventListener(){} close(){}}
const stopped:any={product_version:'AutoFund MVP 0.3.0',demo_mode:true,app_state:'STOPPED',auto_execution:false,
 session_id:null,cash_mxn:'50',equity_mxn:'50',deployed_mxn:'0',market_quality:'VALID',accounting_status:'PASS',
 risk_status:'NORMAL',connected:true,kill_triggered:false,position:null,last_signal:'NO_SIGNAL',orders:0,fills:0,
 realized_pnl_mxn:'0',fees_mxn:'0',telemetry:[],champion:{profile_id:'safe',certification_status:'CERTIFIED'},
 market_regime:'NORMAL',challengers:[],production_preflight:{ready:true,label:'READY',reason:'',blockers:[]}};

// A synthetic position at the account's confirmed 78 bps fee: the strategy exit still
// cannot repay break-even, which is exactly what the panel must show. The figures are
// synthetic rather than taken from a real account, because a public repository should not
// ship one operator's position size and cost basis.
const openPosition={asset:'BTC',quantity:'0.000006',average_cost_mxn:'1500000.00',
 mark_mxn:'1470000',market_value_mxn:'8.820000',realized_pnl_mxn:'0',unrealized_pnl_mxn:'-0.180000',
 fees_mxn:'0',strategy_version:'0.1',status:'OPEN'};
const belowBreakEven={position_open:true,classification:'BELOW BREAK-EVEN',admissible:false,
 outcome:'ECONOMIC_EXIT_REJECT',reason:'EXPECTED_NET_EXIT_NEGATIVE',economically_rejected:1,
 strategy_exit_price_mxn:'1503000.00000',
 fee_only_break_even_price_mxn:'1511791.9774239064704696633743196936101592420882887',
 estimated_break_even_price_mxn:'1511791.9774239064704696633743196936101592420882887',
 current_best_bid_mxn:'1470000',expected_net_pnl_if_sold_now_mxn:'-0.248796000',
 expected_net_pnl_at_strategy_exit_mxn:'-0.052340400',cost_basis_mxn:'9.00000000',
 owned_quantity:'0.000006',average_cost_mxn:'1500000.00',
 distance_to_break_even_bps:'284.30',
 policy:{version:'autofund.economic-edge.v1',minimum_net_profit_mxn:'0',minimum_net_edge_bps:'0'}};

beforeEach(()=>{vi.stubGlobal('EventSource',Events);vi.stubGlobal('fetch',vi.fn().mockResolvedValue({json:async()=>({control_token:'token'})}))});
afterEach(()=>{cleanup();vi.unstubAllGlobals()});

test('positions page reports the full economic exit model for an open position',()=>{
 render(<MvpApp initial={{...stopped,position:openPosition,economics:belowBreakEven}}/>);
 fireEvent.click(screen.getByRole('button',{name:'Positions'}));
 expect(screen.getByText('ECONOMIC EXIT')).toBeTruthy();
 expect(screen.getByText(/BELOW BREAK-EVEN/)).toBeTruthy();
 expect(screen.getByText('Strategy exit price')).toBeTruthy();
 expect(screen.getByText('1503000.00000 MXN')).toBeTruthy();
 expect(screen.getByText('Fee-only break-even')).toBeTruthy();
 expect(screen.getByText('Estimated break-even')).toBeTruthy();
 expect(screen.getByText('Current best bid')).toBeTruthy();
 expect(screen.getByText('Expected P&L if sold now')).toBeTruthy();
 expect(screen.getByText('Expected P&L at strategy exit')).toBeTruthy();
 expect(screen.getByText('-0.052340400 MXN')).toBeTruthy();
});

test('the economic panel exposes no manual trading control',()=>{
 render(<MvpApp initial={{...stopped,position:openPosition,economics:belowBreakEven}}/>);
 fireEvent.click(screen.getByRole('button',{name:'Positions'}));
 for(const name of ['BUY','SELL','PLACE ORDER','SELL NOW','EXIT NOW','FORCE SELL','DISMISS'])
   expect(screen.queryByRole('button',{name})).toBeNull();
});

test('the status label distinguishes a net-profitable exit from a loss-making one',()=>{
 const profitable={...belowBreakEven,classification:'NET-PROFITABLE',admissible:true,outcome:'ECONOMICALLY_ADMISSIBLE',
  expected_net_pnl_at_strategy_exit_mxn:'0.048864000'};
 render(<MvpApp initial={{...stopped,position:openPosition,economics:profitable}}/>);
 fireEvent.click(screen.getByRole('button',{name:'Positions'}));
 const status=document.querySelector('.economic-status');
 expect(status?.getAttribute('data-classification')).toBe('NET-PROFITABLE');
 expect(screen.getByText(/net a profit after fees/)).toBeTruthy();
});

test('no economic panel is shown without an open position',()=>{
 render(<MvpApp initial={{...stopped,position:null,economics:{position_open:false,classification:'NO_POSITION'}}}/>);
 fireEvent.click(screen.getByRole('button',{name:'Positions'}));
 expect(screen.queryByText('ECONOMIC EXIT')).toBeNull();
});

test('the reported policy is visible so admission rules are auditable',()=>{
 render(<MvpApp initial={{...stopped,position:openPosition,economics:belowBreakEven}}/>);
 fireEvent.click(screen.getByRole('button',{name:'Positions'}));
 expect(screen.getByText(/autofund\.economic-edge\.v1/)).toBeTruthy();
});
