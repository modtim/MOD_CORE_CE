/**
 * RunRail: deleting the currently open workflow must stop the runs poll
 * WITHOUT letting the in-flight /runs request land (2400LR-b5dR).
 *
 * useWorkflowApi.handleDeleteWorkflow nulls currentWorkflow, so RunRail's
 * workflowId goes null. The fetch already in flight must be aborted
 * (AbortController.abort()), so no 404 response is delivered to the poller,
 * and no further tick is scheduled.
 */
import React from 'react';
import { render, fireEvent } from '@testing-library/react';
import RunRail from '@/components/RunRail';

let fetchCalls = 0;
let delivered404 = 0;

beforeEach(() => {
  fetchCalls = 0;
  delivered404 = 0;
  jest.useFakeTimers();
  global.fetch = jest.fn((...args: any[]) => {
    fetchCalls++;
    const sig = (args[1] && (args[1] as any).signal) as AbortSignal | undefined;
    let aborted = false;
    return new Promise((resolve, reject) => {
      if (sig) {
        sig.addEventListener('abort', () => {
          aborted = true;
          reject(new DOMException('aborted', 'AbortError'));
        });
      }
      setTimeout(() => {
        if (!aborted) delivered404++;
        resolve({ ok: false, status: 404, json: () => Promise.resolve([]) });
      }, 100);
    });
  }) as any;
});

afterEach(() => {
  global.fetch = undefined as any;
  jest.useRealTimers();
});

function App() {
  // Real state: deleting re-renders RunRail with workflowId=null, like the product
  // (workflow-builder passes api.currentWorkflow?.id ?? null).
  const [currentId, setCurrentId] = React.useState<number | null>(7);
  return (
    <div>
      <button id="delwf" onClick={() => setCurrentId(null)}>Delete current workflow</button>
      <RunRail
        workflowId={currentId}
        accessToken="tok"
        apiUrl="https://api.test"
        selectedRunId={null}
        onSelectRun={() => {}}
        onPausedNodesChange={() => {}}
        onExplainRun={() => {}}
      />
    </div>
  );
}

const drain = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };

describe('RunRail deleted-workflow polling', () => {
  it('aborts the in-flight runs fetch and never issues another', async () => {
    render(<App />);
    await drain();
    expect(fetchCalls).toBe(1);
    fireEvent.click(document.querySelector('#delwf')!);
    await drain();
    jest.advanceTimersByTime(10000);
    await drain();
    expect(fetchCalls).toBe(1);
    expect(delivered404).toBe(0);
  });
});
