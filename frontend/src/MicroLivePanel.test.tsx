import {cleanup,render,screen} from '@testing-library/react';
import {afterEach,expect,test} from 'vitest';
import {MicroLivePanel} from './MicroLivePanel';
import fixture from '../../src/autofund/dashboard/fixtures/micro_live.json';

afterEach(cleanup);
test.each([true,false])('live warning and demo contract %s',demo=>{
  const runtime={...fixture[0],demo_mode:demo} as any;
  render(<MicroLivePanel runtime={runtime} page="Overview" setPage={()=>{}} state="SSE CONNECTED" connected={true}/>);
  expect(screen.getAllByText('REAL MONEY')).toHaveLength(2);
  expect(screen.getByText('AUTO EXECUTION DISABLED')).toBeTruthy();
  expect(screen.getByText('11 MXN')).toBeTruthy();
  expect(Boolean(screen.queryByText('DEMO DATA'))).toBe(demo);
  expect(screen.getByText('AWAITING OPERATOR')).toBeTruthy();
  expect(screen.queryAllByRole('button').filter(x=>/BUY|SELL|CONFIRM|ORDER|WITHDRAW|TRANSFER/.test(x.textContent??''))).toHaveLength(0);
});
test('disconnection retains own reconciled accounting',()=>{
  render(<MicroLivePanel runtime={fixture[4] as any} page="Overview" setPage={()=>{}} state="LIVE DATA DISCONNECTED" connected={false}/>);
  expect(screen.getByText('44.95 MXN')).toBeTruthy();
  expect(screen.getByText('0.000005 BTC')).toBeTruthy();
  expect(screen.getByText('RECONCILED')).toBeTruthy();
  expect(screen.getByText('LIVE DATA DISCONNECTED')).toBeTruthy();
});
