import { expect, test } from 'vitest';

test('dashboard safety labels are present in the application source', async () => {
  const source = await import('./main.tsx?raw');
  expect(source.default).toContain('SHADOW');
  expect(source.default).toContain('TRADING: DISABLED');
  expect(source.default).not.toContain('BUY NOW');
});
