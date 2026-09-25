import {cleanup,fireEvent,render,screen} from '@testing-library/react';
import {afterEach,beforeEach,expect,test,vi} from 'vitest';
import {MvpApp} from './MvpApp';

class Events {addEventListener(){} close(){}}
const stopped:any={product_version:'AutoFund MVP 0.1',demo_mode:true,app_state:'STOPPED',auto_execution:false,
 session_id:null,cash_mxn:'50',equity_mxn:'50',deployed_mxn:'0',market_quality:'VALID',accounting_status:'PASS',
 risk_status:'NORMAL',connected:true,kill_triggered:false,position:null,last_signal:'NO_SIGNAL',orders:0,fills:0,
 realized_pnl_mxn:'0',fees_mxn:'0',telemetry:[],champion:{profile_id:'safe',certification_status:'CERTIFIED'},
 market_regime:'NORMAL',challengers:[],production_preflight:{ready:true,label:'READY',reason:'',blockers:[]}};

// The real reference position at Production's confirmed 78 bps fee: the strategy
// exit cannot repay break-even, which is exactly what the panel must show.
const openPosition={asset:'BTC',quantity:'0.00000727',average_cost_mxn:'1501356.817056396148555708391',
 mark_mxn:'1467000',market_value_mxn:'10.664',realized_pnl_mxn:'0',unrealized_pnl_mxn:'-0.25',
 fees_mxn:'0',strategy_version:'0.1',status:'OPEN'};
const belowBreakEven={position_open:true,classification:'BELOW BREAK-EVEN',admissible:false,
 outcome:'ECONOMIC_EXIT_REJECT',reason:'EXPECTED_NET_EXIT_NEGATIVE',economically_rejected:1,
 strategy_exit_price_mxn:'1504359.530690508940852819807782',
 fee_only_break_even_price_mxn:'1513159.4608510342154361100490289179950241872853096',
 estimated_break_even_price_mxn:'1513159.4608510342154361100490289179950241872853096',
 current_best_bid_mxn:'1467000',expected_net_pnl_if_sold_now_mxn:'-0.257',
 expected_net_pnl_at_strategy_exit_mxn:'-0.063476483427336',cost_basis_mxn:'10.91486406',
 owned_quantity:'0.00000727',average_cost_mxn:'1501356.817056396148555708391',
 distance_to_break_even_bps:'31.47',
 policy:{version:'autofund.economic-edge.v1',minimum_net_profit_mxn:'0',minimum_net_edge_bps:'0'}};

beforeEach(()=>{vi.stubGlobal('EventSource',Events);vi.stubGlobal('fetch',vi.fn().mockResolvedValue({json:async()=>({control_token:'token'})}))});
afterEach(()=>{cleanup();vi.unstubAllGlobals()});

test('positions page reports the full economic exit model for an open position',()=>{
 render(<MvpApp initial={{...stopped,position:openPosition,economics:belowBreakEven}}/>);
 fireEvent.click(screen.getByRole('button',{name:'Positions'}));
 expect(screen.getByText('ECONOMIC EXIT')).toBeTruthy();
 expect(screen.getByText(/BELOW BREAK-EVEN/)).toBeTruthy();
 expect(screen.getByText('Strategy exit price')).toBeTruthy();
 expect(screen.getByText('1504359.530690508940852819807782 MXN')).toBeTruthy();
 expect(screen.getByText('Fee-only break-even')).toBeTruthy();
 expect(screen.getByText('Estimated break-even')).toBeTruthy();
 expect(screen.getByText('Current best bid')).toBeTruthy();
 expect(screen.getByText('Expected P&L if sold now')).toBeTruthy();
 expect(screen.getByText('Expected P&L at strategy exit')).toBeTruthy();
 expect(screen.getByText('-0.063476483427336 MXN')).toBeTruthy();
});

test('the economic panel exposes no manual trading control',()=>{
 render(<MvpApp initial={{...stopped,position:openPosition,economics:belowBreakEven}}/>);
 fireEvent.click(screen.getByRole('button',{name:'Positions'}));
 for(const name of ['BUY','SELL','PLACE ORDER','SELL NOW','EXIT NOW','FORCE SELL','DISMISS'])
   expect(screen.queryByRole('button',{name})).toBeNull();
});

test('the status label distinguishes a net-profitable exit from a loss-making one',()=>{
 const profitable={...belowBreakEven,classification:'NET-PROFITABLE',admissible:true,outcome:'ECONOMICALLY_ADMISSIBLE',
  expected_net_pnl_at_strategy_exit_mxn:'0.1091486406'};
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
