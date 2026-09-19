import {act,cleanup,render,screen,waitFor} from '@testing-library/react';
import {afterEach,expect,test,vi} from 'vitest';
import {App} from './main';
import fixture from '../../src/autofund/dashboard/fixtures/micro_live.json';

class Stream {
  static instance:Stream;
  onerror=()=>{};onopen=()=>{};
  callback:((event:MessageEvent)=>void)|null=null;
  constructor(){Stream.instance=this}
  addEventListener(_:string,callback:(event:MessageEvent)=>void){this.callback=callback}
  close(){}
}
afterEach(()=>{cleanup();vi.unstubAllGlobals()});
test('micro-live reads own runtime only and retains last SSE snapshot on disconnect',async()=>{
  const fetcher=vi.fn(async(_url:string)=>({ok:true,json:async()=>fixture[0]}));
  vi.stubGlobal('fetch',fetcher);vi.stubGlobal('EventSource',Stream);
  render(<App/>);
  await screen.findByText('AWAITING OPERATOR');
  expect(fetcher.mock.calls.every(call=>call[0]==='/api/v1/runtime')).toBe(true);
  act(()=>Stream.instance.callback!(new MessageEvent('snapshot',{data:JSON.stringify({runtime:fixture[4]})})));
  await screen.findByText('RECONCILED');
  expect(screen.getByText('0.000005 BTC')).toBeTruthy();
  act(()=>Stream.instance.onerror());
  expect(screen.getByText('LIVE DATA DISCONNECTED')).toBeTruthy();
  expect(screen.getByText('0.000005 BTC')).toBeTruthy();
  expect(screen.getByText('44.95 MXN')).toBeTruthy();
  act(()=>Stream.instance.callback!(new MessageEvent('snapshot',{data:'malformed'})));
  await waitFor(()=>expect(fetcher).toHaveBeenCalled());
});
