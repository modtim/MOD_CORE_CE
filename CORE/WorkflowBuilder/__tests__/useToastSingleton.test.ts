/**
 * Regression test for the "Workflow Saved" toast never rendering.
 *
 * Root cause: hooks/use-toast.ts and components/ui/use-toast.ts were
 * two independent files with byte-identical source, so each was
 * compiled as its own ES module with its own module-scoped
 * `memoryState`/`listeners` singleton. components/ui/toaster.tsx (the
 * app's single mounted <Toaster/>) subscribed via useToast() from
 * "@/hooks/use-toast", while every toast() caller in the app
 * (useWorkflowApi.ts, node-config-panel.tsx, workflow-builder.tsx,
 * etc.) imported toast() from "@/components/ui/use-toast" — a
 * disconnected store the Toaster never listened to. Toasts dispatched
 * from that side silently vanished.
 *
 * This test asserts both modules now resolve to the SAME singleton:
 * a toast() dispatched via the "@/components/ui/use-toast" path (the
 * path useWorkflowApi.ts's save-success handler uses) must be visible
 * to a useToast() subscriber obtained via "@/hooks/use-toast" (the
 * path the mounted <Toaster/> uses).
 */

import { renderHook, act } from '@testing-library/react';
import { toast as toastFromWorkflowApiPath } from '@/components/ui/use-toast';
import { useToast as useToastFromToasterPath } from '@/hooks/use-toast';

describe('toast store singleton', () => {
  it('surfaces a toast() call made via components/ui/use-toast to a subscriber using hooks/use-toast', () => {
    const { result } = renderHook(() => useToastFromToasterPath());

    act(() => {
      toastFromWorkflowApiPath({
        title: 'Workflow Saved',
        description: 'Successfully saved new workflow "My Workflow".',
      });
    });

    expect(result.current.toasts.some((t) => t.title === 'Workflow Saved')).toBe(true);
  });

  it('exports the identical toast function reference from both module paths', () => {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const hooksModule = require('@/hooks/use-toast');
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const uiModule = require('@/components/ui/use-toast');
    expect(hooksModule.toast).toBe(uiModule.toast);
    expect(hooksModule.useToast).toBe(uiModule.useToast);
  });
});
