import React from 'react';
import {act,cleanup,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,test,vi} from 'vitest';
import {App} from './main';

class Stream {
  static instance:Stream;
  onerror:()=>void=()=>{}; onopen:()=>void=()=>{};
  listeners:Record<string,()=>void>={};
  constructor(){Stream.instance=this}
  addEventListener(name:string,callback:()=>void){this.listeners[name]=callback}
  close(){}
}
afterEach(()=>{cleanup();vi.unstubAllGlobals()});
function setup(demo:boolean){
  const models:Record<string,any>={overview:{demo_mode:demo,current_shadow_equity_mxn:'50.64',max_deployment_mxn:'25',market_quality:'VALID'},quality:{status:'VALID',stale_snapshots:0,out_of_order_trades:0,gaps:0,benchmark_eligible:true},runtime:{session_id:null,status:'STOPPED'},equity:[{equity_mxn:'50.64'}],market:{market:'btc_mxn'},candles:[],activity:{items:[]},ledger:{items:[]},sessions:{items:[]},risk:{},health:{accounting:'PASS'}};
  const fetcher=vi.fn(async(url:string)=>({ok:true,json:async()=>models[url.split('/').at(-1)!]}));
  vi.stubGlobal('fetch',fetcher);vi.stubGlobal('EventSource',Stream);return fetcher;
}
test.each([true,false])('demo badge follows backend contract %s',async(demo)=>{setup(demo);render(<App/>);await screen.findByRole('heading',{name:'Overview'});expect(Boolean(screen.queryByText('DEMO DATA'))).toBe(demo);expect(screen.getByText('REAL TRADING DISABLED')).toBeTruthy();expect(screen.getByText('WRITE CAPABILITY BLOCKED')).toBeTruthy();expect(screen.queryAllByRole('button').filter(x=>/BUY|SELL|ORDER|WITHDRAW/.test(x.textContent??''))).toHaveLength(0)});
test('disconnect retains financial snapshot and reconnect reads REST again',async()=>{const fetcher=setup(false);render(<App/>);await screen.findByText('50.64 MXN');act(()=>Stream.instance.onerror());expect(screen.getByText('LIVE DATA DISCONNECTED')).toBeTruthy();expect(screen.getByText('50.64 MXN')).toBeTruthy();fetcher.mockClear();await act(async()=>Stream.instance.onopen());await waitFor(()=>expect(fetcher).toHaveBeenCalledWith('/api/v1/runtime'));expect(screen.getByText('50.64 MXN')).toBeTruthy();expect(Stream.instance.listeners.snapshot).toBeTypeOf('function')});
