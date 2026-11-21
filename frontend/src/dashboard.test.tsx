import { expect, test } from 'vitest';

test('dashboard safety labels are present in the dashboard source', async () => {
  const source = await import('./LegacyDashboard.tsx?raw');
  expect(source.default).toContain('SHADOW');
  expect(source.default).toContain('REAL TRADING DISABLED');
  expect(source.default).not.toContain('BUY NOW');
});
