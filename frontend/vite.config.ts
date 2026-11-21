import react from '@vitejs/plugin-react';
import {defineConfig} from 'vite';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    exclude: ['e2e/**', 'node_modules/**'],
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/**/*.test.{ts,tsx}', 'src/vite-env.d.ts'],
      reporter: ['text-summary', 'lcov'],
      // Measured at about 85% statements when this gate was added; the floor sits below it.
      thresholds: {statements: 75, lines: 78, functions: 65, branches: 55},
    },
  },
});
