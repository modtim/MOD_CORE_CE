import '@testing-library/jest-dom';
import { configure } from '@testing-library/react';

// Raise asyncUtilTimeout from the 1000 ms default to 20 s.
// Under VM load waitFor / findBy* lose the race with a 1 s cap;
// 20 s is still bounded by the 30 s jest testTimeout so hangs
// continue to fail fast.
configure({ asyncUtilTimeout: 20000 });

// jsdom does not implement Element.prototype.scrollIntoView; define a
// no-op so tests can jest.spyOn it (wizard-runs-section reveal/scroll).
if (typeof Element !== 'undefined' && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// Mock localStorage
const localStorageMock = (() => {
  let store: Record<string, string> = {};
  return {
    getItem: (key: string) => store[key] || null,
    setItem: (key: string, value: string) => { store[key] = value; },
    removeItem: (key: string) => { delete store[key]; },
    clear: () => { store = {}; },
  };
})();
Object.defineProperty(window, 'localStorage', { value: localStorageMock });
