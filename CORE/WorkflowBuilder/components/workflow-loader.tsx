"use client";

import { useEffect, useState } from "react";
import dynamic from "next/dynamic";

// Default Loading state for the dynamic import. Pre-fix this was a
// bare "Loading..." that stayed on screen forever if the next/dynamic
// chunk fetch never resolved (post-deploy hash mismatch, CDN blip,
// dropped packet without TCP RST). Tim 2026-06-04: "once in a blue
// moon" hang reported by playtesters that a refresh fixed; we now
// detect a stuck-state at 8s and surface a Retry button instead of
// leaving the user staring at "Loading..." indefinitely.
const STUCK_AFTER_MS = 8_000;

export function Loading({ onRetry }: { onRetry?: () => void } = {}) {
  const [stuck, setStuck] = useState(false);
  useEffect(() => {
    const t = window.setTimeout(() => setStuck(true), STUCK_AFTER_MS);
    return () => window.clearTimeout(t);
  }, []);
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'center',
        alignItems: 'center',
        height: '100vh',
        gap: '12px',
        fontFamily: 'system-ui, -apple-system, sans-serif',
      }}
    >
      <div>Loading...</div>
      {stuck && (
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            gap: '8px',
            color: '#6b7280',
            fontSize: '13px',
            maxWidth: '360px',
            textAlign: 'center',
          }}
        >
          <div>This is taking longer than expected.</div>
          <button
            type="button"
            onClick={onRetry ?? (() => window.location.reload())}
            style={{
              padding: '6px 14px',
              borderRadius: '6px',
              border: '1px solid #d1d5db',
              background: 'white',
              cursor: 'pointer',
              fontSize: '13px',
              color: '#1f2937',
            }}
          >
            Retry
          </button>
        </div>
      )}
    </div>
  );
}

const WorkflowBuilder = dynamic(() => import("@/components/workflow-builder"), {
  ssr: false,
  loading: () => <Loading />,
});

export default function WorkflowLoader() {
  // No auth check needed - the workflow builder will make API calls
  // If user is not authenticated, those API calls will fail with 401
  // and the UI can handle showing login redirect appropriately
  return <WorkflowBuilder />;
}
