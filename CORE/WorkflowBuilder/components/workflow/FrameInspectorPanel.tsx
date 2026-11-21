"use client";

/**
 * sz-2.10a — Frame Inspector panel: a read-only view of one paused node's
 * debug frame (GET /road/workflow-runs/{runId}/debug/frame/{nodeId},
 * see lib/debug-frame.ts). Loads once on mount plus an explicit Refresh
 * button — no polling: the frame is a snapshot of a paused node. 404
 * renders a "Frame not available" state; other failures render a short
 * safe error (the token and response body never reach the DOM).
 *
 * Not yet wired into the canvas/RunRail — that's 2.10b.
 */

import React from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { AlertTriangle, RefreshCw } from "lucide-react";
import {
  fetchDebugFrame,
  isMaskedValue,
  formatFrameValue,
  truncateFrameValue,
  hotSwapDebugNode,
  stripMaskedKeys,
} from "@/lib/debug-frame";
import type { DebugFrame } from "@/lib/debug-frame";

interface FrameInspectorPanelProps {
  apiUrl: string;
  accessToken: string;
  runId: number;
  nodeId: string;
  onClose: () => void;
}

const MASKED_LABEL = "masked";
const TRUNCATE_AT = 500;

type FrameState =
  { kind: "loading" } | { kind: "frame"; frame: DebugFrame } |
  { kind: "missing" } | { kind: "error"; message: string };

function ValueCell({
  value,
  expanded,
  testid,
}: { value: unknown; expanded: boolean; testid: string }): React.JSX.Element {
  if (isMaskedValue(value)) {
    return (
      <span
        data-testid={testid}
        className="inline-flex items-center rounded bg-gray-100 text-gray-500 px-1.5 text-xs font-medium"
      >
        {MASKED_LABEL}
      </span>
    );
  }
  const formatted = formatFrameValue(value);
  const isJson = formatted.startsWith("{") || formatted.startsWith("[");
  const tooLong = formatted.length > TRUNCATE_AT;
  if (tooLong && !expanded) {
    return (
      <span data-testid={testid} className="text-xs break-all">
        {truncateFrameValue(formatted)}
      </span>
    );
  }
  if (isJson) {
    return (
      <pre data-testid={testid} className="text-xs bg-gray-50 rounded px-1.5">
        {formatted}
      </pre>
    );
  }
  return (
    <span data-testid={testid} className="text-xs break-all">
      {formatted}
    </span>
  );
}

function KeyRow({
  keyName,
  value,
  expanded,
}: { keyName: string; value: unknown; expanded: boolean }): React.JSX.Element {
  return (
    <div
      data-testid="frame-key-row"
      className="flex flex-wrap items-center gap-2 text-xs"
    >
      <span className="font-mono text-gray-700">{keyName}</span>
      <ValueCell
        value={value}
        expanded={expanded}
        testid={`frame-value-${keyName}`}
      />
    </div>
  );
}

function Section({
  title,
  entries,
  expanded,
}: { title: string; entries: Record<string, unknown>; expanded: boolean }): React.JSX.Element {
  return (
    <div data-testid={`frame-section-${title}`} className="mt-3">
      <div className="text-xs font-semibold uppercase text-gray-600 mb-1.5">
        {title}
      </div>
      <div className="space-y-1.5">
        {Object.entries(entries).map(([k, v]) => (
          <KeyRow key={k} keyName={k} value={v} expanded={expanded} />
        ))}
      </div>
    </div>
  );
}

/** True when any value in the frame exceeds the truncation budget. */
function hasLongFrameValue(frame: DebugFrame): boolean {
  const values: unknown[] = [];
  Object.values(frame.config ?? {}).forEach((v) => values.push(v));
  Object.values(frame.inputs ?? {}).forEach((v) => values.push(v));
  if (frame.worker_context) {
    Object.values(frame.worker_context).forEach((v) => values.push(v));
  }
  (frame.upstream_outputs ?? []).forEach((parent) =>
    Object.values(parent).forEach((v) => values.push(v))
  );
  return values.some((v) => formatFrameValue(v).length > TRUNCATE_AT);
}

/**
 * Build a hot-swap payload from the panel's raw inputs. Returns `null` when
 * the config text is not valid JSON — the caller shows the parse error inline
 * and sends nothing. Masked ("***") values are stripped so the mask the API
 * rendered can never be written back as a real value.
 */
function buildHotSwapBody(
  configText: string,
  defIdText: string
): { ok: true; body: Record<string, unknown> } | { ok: false; invalid: string } {
  const body: Record<string, unknown> = {};
  const trimmed = configText.trim();
  if (trimmed !== "") {
    let parsed: unknown;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      return { ok: false, invalid: "Config must be valid JSON" };
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { ok: false, invalid: "Config must be a JSON object" };
    }
    body.config = stripMaskedKeys(parsed);
  }
  const defTrimmed = defIdText.trim();
  if (defTrimmed !== "") {
    const defId = Number(defTrimmed);
    if (!Number.isInteger(defId) || defId <= 0) {
      return { ok: false, invalid: "Node definition id must be a positive integer" };
    }
    body.node_definition_id = defId;
  }
  return { ok: true, body };
}

export default function FrameInspectorPanel(props: FrameInspectorPanelProps): React.JSX.Element {
  const { apiUrl, accessToken, runId, nodeId, onClose } = props;
  const [state, setState] = React.useState<FrameState>({ kind: "loading" });
  const [expanded, setExpanded] = React.useState(false);
  const [configText, setConfigText] = React.useState("");
  const [defIdText, setDefIdText] = React.useState("");
  const [swapResult, setSwapResult] = React.useState<
    { kind: "idle" } | { kind: "error"; message: string } | { kind: "success" }
  >({ kind: "idle" });

  // One-shot load: null (404) → missing state; thrown Error carries a short
  // safe message (never the token or the response body).
  const load = () => {
    if (!apiUrl || !accessToken) {
      setState({ kind: "error", message: "Sign in to inspect frames" });
      return;
    }
    fetchDebugFrame(apiUrl, accessToken, runId, nodeId)
      .then((frame) => {
        if (frame === null) {
          setState({ kind: "missing" });
          return;
        }
        setState({ kind: "frame", frame });
        // Prefill the hot-swap editor with the frame's current config, masked
        // values stripped — secrets are never shown or editable here.
        setConfigText(JSON.stringify(stripMaskedKeys(frame.config ?? {}) ?? {}, null, 2));
        setSwapResult({ kind: "idle" });
      })
      .catch((err) =>
        setState({
          kind: "error",
          message: err instanceof Error ? err.message : "frame unavailable",
        })
      );
  };

  // Load once on mount. Refresh re-fires the same single-shot load — no
  // polling: the frame is a snapshot of a paused node.
  React.useEffect(() => {
    load();
  }, []);

  const applyHotSwap = () => {
    if (!apiUrl || !accessToken) {
      setSwapResult({ kind: "error", message: "Sign in to hot-swap" });
      return;
    }
    const built = buildHotSwapBody(configText, defIdText);
    if (!built.ok) {
      setSwapResult({ kind: "error", message: built.invalid });
      return;
    }
    hotSwapDebugNode(apiUrl, accessToken, runId, nodeId, built.body)
      .then((result) =>
        setSwapResult(
          result.ok
            ? { kind: "success" }
            : { kind: "error", message: result.detail }
        )
      );
  };

  const frame = state.kind === "frame" ? state.frame : null;
  const showMore = frame !== null && hasLongFrameValue(frame);

  return (
    <div
      data-testid="frame-inspector-panel"
      className="p-4 border border-gray-200 rounded-lg bg-white text-sm"
    >
      <div className="flex flex-wrap items-center gap-2 mb-2">
        <AlertTriangle className="w-4 h-4 mr-1.5 text-amber-600" />
        <span className="font-semibold">Frame Inspector</span>
        <Button
          variant="outline"
          size="sm"
          onClick={load}
          className="text-xs"
          title="Re-fetch this frame"
        >
          <RefreshCw className="w-3.5 h-3.5 mr-1" />
          Refresh
        </Button>
        <Button
          variant="ghost"
          size="sm"
          onClick={onClose}
          className="text-xs"
          title="Close inspector"
        >
          Close
        </Button>
      </div>

      {state.kind === "loading" && (
        <div data-testid="frame-loading" className="text-gray-500 italic text-xs">
          Loading frame…
        </div>
      )}
      {state.kind === "missing" && (
        <div data-testid="frame-missing" className="text-gray-600 text-xs">
          Frame not available — the run or node is not visible to you.
        </div>
      )}
      {state.kind === "error" && (
        <div data-testid="frame-error" className="text-red-700 text-xs">
          {state.message}
        </div>
      )}

      {frame && (
        <>
          <div data-testid="frame-section-node" className="flex flex-wrap items-center gap-2 text-xs">
            <span className="font-mono font-semibold">{frame.node_id}</span>
            {frame.node_kind && (
              <span data-testid="frame-node-kind" className="text-gray-600">
                {frame.node_kind}
              </span>
            )}
            <span
              data-testid="frame-node-status"
              className={frame.paused ? "text-amber-700 font-semibold" : "text-gray-600"}
            >
              {frame.paused ? "paused" : "running"}
            </span>
            {frame.paused && (
              <span
                data-testid="frame-paused-badge"
                className="inline-flex items-center rounded bg-amber-100 text-amber-800 px-1.5 text-xs font-medium"
              >
                paused
              </span>
            )}
          </div>

          <Section title="Config" entries={frame.config ?? {}} expanded={expanded} />
          <Section title="Inputs" entries={frame.inputs ?? {}} expanded={expanded} />

          {(frame.upstream_outputs ?? []).length > 0 && (
            <div data-testid="frame-section-upstream" className="mt-3">
              <div className="text-xs font-semibold uppercase text-gray-600 mb-1.5">
                Upstream outputs
              </div>
              <div className="space-y-1.5">
                {(frame.upstream_outputs ?? []).map((parent, i) => (
                  <div
                    key={i}
                    data-testid="frame-upstream-node"
                    className="p-2 border border-gray-100 rounded bg-gray-50"
                  >
                    <div className="text-xs font-mono text-gray-700">
                      {String(parent.node_id ?? parent.name ?? `#${i + 1}`)}
                    </div>
                    {Object.entries(parent)
                      .filter(([k]) => k !== "node_id" && k !== "name")
                      .map(([k, v]) => (
                        <KeyRow key={k} keyName={k} value={v} expanded={expanded} />
                      ))}
                  </div>
                ))}
              </div>
            </div>
          )}

          {frame.worker_context && (
            <Section
              title="Worker context"
              entries={frame.worker_context}
              expanded={expanded}
            />
          )}

          {frame.partial_output !== null && frame.partial_output !== undefined && (
            <div data-testid="frame-section-partial" className="mt-3">
              <div className="text-xs font-semibold uppercase text-gray-600 mb-1.5">
                Partial output
              </div>
              <ValueCell
                value={frame.partial_output}
                expanded={expanded}
                testid="frame-value-partial_output"
              />
            </div>
          )}

          <div data-testid="frame-section-hotswap" className="mt-3">
            <div className="text-xs font-semibold uppercase text-gray-600 mb-1.5">
              Hot-swap
            </div>
            <Textarea
              value={configText}
              onChange={(e) => setConfigText(e.target.value)}
              className="w-full text-xs font-mono rounded bg-gray-50 p-2"
              title="Config override (JSON)"
            />
            <div data-testid="frame-hotswap-masked-hint" className="text-gray-400 text-[11px] mt-0.5">
              Masked values are hidden and kept unchanged.
            </div>
            <input
              type="text"
              value={defIdText}
              onChange={(e) => setDefIdText(e.target.value)}
              className="text-xs font-mono rounded bg-gray-50 py-0.5"
              title="Node definition id (optional)"
            />
            <Button
              variant="outline"
              size="sm"
              onClick={applyHotSwap}
              className="text-xs"
              title="Store this override on the paused node"
            >
              Apply hot-swap
            </Button>
            {swapResult.kind === "idle" && (
              <div data-testid="frame-hotswap-idle" className="text-gray-500 text-xs mt-1.5">
                Edit the config JSON above, then apply it to this paused node.
              </div>
            )}
            {swapResult.kind === "success" && (
              <div data-testid="frame-hotswap-success" className="text-emerald-700 text-xs mt-1.5">
                Override stored — Resume to run with it
              </div>
            )}
            {swapResult.kind === "error" && (
              <div data-testid="frame-hotswap-error" className="text-red-700 text-xs mt-1.5">
                {swapResult.message}
              </div>
            )}
          </div>

          {showMore && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setExpanded((v) => !v)}
              className="text-xs text-gray-500 hover:text-brand-600"
              title="Toggle full values"
            >
              show more
            </Button>
          )}
        </>
      )}
    </div>
  );
}
