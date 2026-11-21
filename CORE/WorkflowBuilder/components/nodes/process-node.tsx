"use client";

import { memo, useState, useEffect, lazy, Suspense } from "react";
import { nodeStateBorderClass } from "@/lib/node-state-style";
import { nodeAriaLabel } from "@/lib/canvas-a11y";
import { Handle, Position, type NodeProps } from "reactflow";
import { Settings, ImageIcon, Loader2 } from "lucide-react";
import type { NodeData } from "@/lib/types";
import { getColorForType } from "@/lib/color-utils";
import { AssetPreview } from "./asset-preview";
import { isAiNode, AI_OUTPUT_LABEL_TEXT, AI_OUTPUT_TOOLTIP } from "@/lib/ai-output-label";
import { NodeChips, LifecycleBadge } from "../node-chips";
import { NodeStackOverlay } from "./node-stack-overlay";

const Model3DViewerModal = lazy(() =>
  import('../model3d-thumbnail').then(m => ({ default: m.Model3DViewerModal }))
);

const MODEL3D_EXTS = new Set(['obj', 'fbx', 'glb', 'gltf', 'ply', 'stl', 'dae', 'usd', 'usdz']);

function ElapsedTimer({ startedAt }: { startedAt: string }) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const start = new Date(startedAt).getTime();
    const tick = () => setElapsed(Math.floor((Date.now() - start) / 1000));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [startedAt]);
  const mins = Math.floor(elapsed / 60);
  const secs = elapsed % 60;
  return <span className="text-[9px] text-gray-400">{mins > 0 ? `${mins}m ` : ''}{secs}s</span>;
}

// Helper to get handle styling based on file_mode
const HANDLE_SIZE = '14px';

const getHandleStyle = (fileMode: string | undefined, baseColor: string) => {
  const base = {
    zIndex: 20,
    width: HANDLE_SIZE,
    height: HANDLE_SIZE,
  };

  if (fileMode === 'single') {
    return {
      ...base,
      backgroundColor: 'transparent',
      border: `3px solid ${baseColor}`,
      borderRadius: '50%',
    };
  } else if (fileMode === 'multiple') {
    return {
      ...base,
      backgroundColor: baseColor,
      borderRadius: '2px',
      boxShadow: `0 0 0 2px white, 0 0 0 4px ${baseColor}`,
    };
  } else if (fileMode === 'both') {
    return {
      ...base,
      backgroundColor: baseColor,
      borderRadius: '4px',
      border: `2px solid white`,
      boxShadow: `0 0 0 2px ${baseColor}`,
    };
  }
  return {
    ...base,
    backgroundColor: baseColor,
    borderRadius: '50%',
  };
};

type PortEntry = { name: string; type: string; label?: string; fileMode?: string };

const getSchemaEntries = (schema: { [key: string]: any } | undefined, legacyTypeKey: 'input_type' | 'output_type'): PortEntry[] => {
    if (!schema) return [];

    if (schema[legacyTypeKey]) {
        if (schema[legacyTypeKey] === 'NONE') return [];

        const types = schema[legacyTypeKey].split(',').map((t: string) => t.trim());
        const name = legacyTypeKey.replace('_type', '');

        return types.map((type: string, index: number) => ({
            name: types.length > 1 ? `${name}_${index}` : name,
            type,
            fileMode: schema.file_mode,
        }));
    }

    // Object-form named ports: {portName: {type, file_mode, label}}. The
    // optional `label` (or `human_name`) is what the canvas draws next to the
    // handle so a multi-input node's channels — e.g. positive vs
    // regularization vs avoid/negative images — are individually named and
    // unmistakable, not three anonymous squares.
    return Object.entries(schema)
        .filter(([, props]) => typeof props === 'object' && props !== null)
        .map(([name, props]) => ({
            name,
            type: (props && props.type) ? props.type : 'ANY',
            label: (props && (props.label || props.human_name)) || undefined,
            fileMode: (props && props.file_mode) || undefined,
        }));
};

// multi-input-left-port-hit-area — each multi-input channel renders as a
// FULL-ROW target Handle so a drop anywhere on the labeled row connects
// (ReactFlow's getClosestHandle finds the handle element under the cursor via
// elementsFromPoint). But app/globals.css pins EVERY `.react-flow__handle` to
// 14px !important. That would collapse the row handle back to a 14px corner —
// which (a) defeats the drop-anywhere hit area, (b) mis-anchors the connection
// hint line (a collapsed box yields a degenerate left-edge anchor so the line
// doesn't track from the port), and (c) truncates the label to nothing (a
// `truncate` span inside a 14px flex box). This higher-specificity rule
// (`.react-flow__handle.wb-port-hit` — two classes, unlayered + !important)
// wins over the 14px rule and lets the row handle fill its row. It is scoped
// to our port hit-targets only, so every other handle keeps its 14px sizing.
const PORT_HIT_HANDLE_CSS =
  '.react-flow__handle.wb-port-hit{width:100%!important;height:100%!important;min-width:0!important;min-height:0!important;border-radius:0!important;background:transparent!important;border:none!important;}';

let _portHitCssInjected = false;
function useInjectPortHitCss() {
  useEffect(() => {
    if (_portHitCssInjected || typeof document === 'undefined') return;
    if (document.getElementById('wb-port-hit-style')) {
      _portHitCssInjected = true;
      return;
    }
    const style = document.createElement('style');
    style.id = 'wb-port-hit-style';
    style.textContent = PORT_HIT_HANDLE_CSS;
    document.head.appendChild(style);
    _portHitCssInjected = true;
  }, []);
}

// Process nodes always have purple border (their original color)
const getStatusColor = (): string => {
  return "border-purple-500"; // Always purple for process nodes
};

// Helper function to get background color for status indicator
const getStatusBgColor = (status?: string): string => {
  switch (status) {
    case "running":
      return "bg-green-400"; // Green pulse while running
    case "success":
      return "bg-green-500";
    case "failure":
      return "bg-red-500";
    case "blocked":
      return "bg-orange-500";
    case "queued":
      return "bg-purple-500";
    default:
      return "bg-gray-300"; // Default grey for no status
  }
};


export const ProcessNode = memo(({ data, isConnectable, id }: NodeProps<NodeData>) => {
    useInjectPortHitCss();
    // Dynamic I/O: side effects (e.g., NIM model) can override input/output types
    const dynamicInputMode = data.extraVariables?.INPUT_MODE || data.extraVariables?.input_mode;
    const dynamicOutputMode = data.extraVariables?.OUTPUT_MODE || data.extraVariables?.output_mode;

    const effectiveInputSchema = dynamicInputMode
        ? { ...data.input_schema, input_type: dynamicInputMode }
        : data.input_schema;
    const effectiveOutputSchema = dynamicOutputMode
        ? { ...data.output_schema, output_type: dynamicOutputMode }
        : data.output_schema;

    const inputs = getSchemaEntries(effectiveInputSchema, 'input_type');
    const outputs = getSchemaEntries(effectiveOutputSchema, 'output_type');
    const statusColor = nodeStateBorderClass(data, getStatusColor());

    // Get file_mode for display
    const inputFileMode = data.input_schema?.file_mode;
    const outputFileMode = data.output_schema?.file_mode;
    const outputPaired = data.output_schema?.paired;

    // 3D viewer state
    const [viewerOpen, setViewerOpen] = useState(false);
    const has3DOutput = data.previewAssets?.some((a: any) => a.category === 'model3d') || false;
    const first3DAsset = data.previewAssets?.find((a: any) => a.category === 'model3d');
    const first3DExt = first3DAsset?.file_extension || 'glb';

    // Calculate cost display using pre-calculated file count from WorkflowBuilder
    const getCostDisplay = () => {
        if (!data.cost_in_credits) return null;

        const fileCount = data.calculatedFileCount;

        if (fileCount !== null && fileCount !== undefined) {
            // Show actual cost
            const totalCost = fileCount * data.cost_in_credits;
            return `${fileCount} × ${data.cost_in_credits} = ${totalCost}`;
        }

        // Show formula when we can't calculate
        return `n × ${data.cost_in_credits}`;
    };

    // Determine what to show in the preview area
    const hasOutput = data.status === 'success' && data.previewAssets && data.previewAssets.length > 0;
    const hasInputPreview = data.inputPreviewUrl;
    const isRunning = data.status === 'running';
    // node-catalogue-audit ("COMPLETED lies") — the Failed block was LATCHED:
    // the canvas poller only rewrites node.data.status when the run it is
    // polling has a NodeRun for THIS node (hooks/useWorkflowApi.ts:703
    // `matchingRun` find + :709 write). With reuse_cache (the "Fresh run"
    // toggle off, useWorkflowApi.ts:1866 `reuse_cache: !freshRun`) a re-run
    // reuses an unchanged node's cached result and writes NO NodeRun row for
    // it, so `matchingRun` is null and the PREVIOUS run's 'failure' +
    // error_message stay on node.data forever — the card keeps rendering
    // "Failed" after a successful re-run (e2e wb-node-catalogue-audit.spec.ts:402
    // `toHaveCount(0)`). Stamp the run a status first arrived under and only
    // honour the failure indicator while that stamp matches the newest run the
    // app started (`__lastWorkflowRunId`, set at useWorkflowApi.ts:1914/:2324).
    // A status the poller DOES write for the new run re-stamps, so a genuine
    // failure in the current run still renders.
    const latestRunId = typeof window === 'undefined'
        ? null
        : ((window as any).__lastWorkflowRunId as number | null);
    const [statusStamp, setStatusStamp] = useState<{ run: number | null; status: string | null }>({ run: null, status: null });
    if (data.status !== statusStamp.status) setStatusStamp({ run: latestRunId, status: data.status });
    const isFailed = data.status === 'failure' && statusStamp.run === latestRunId;
    // Gate 5 — this node is holding the run open, waiting on a person. Shown
    // ON the node because that is where the user is looking when a run stops:
    // making them find a menu to discover WHY it stopped is the same problem
    // the gate exists to make visible.
    const isAwaitingApproval = data.status === 'pending_action';

    // node-version-pinning — this node is pinned to an older build than the
    // node's current one, so a newer version is available to update to.
    const isVersionBehind =
        data.pinnedNodeVersionId != null &&
        data.currentNodeVersionId != null &&
        data.pinnedNodeVersionId !== data.currentNodeVersionId;

  const nodeAriaLabelValue = nodeAriaLabel("process", data.label);

  return (
    <div role="group" aria-label={nodeAriaLabelValue} className={`shadow-md rounded-lg bg-white border-2 ${statusColor} w-[17rem] transition-colors duration-300 relative overflow-hidden`}>
      {/* Status indicator box in top-right corner */}
      <div className={`absolute top-1 right-1 w-4 h-4 rounded ${getStatusBgColor(data.status)} border-2 border-white shadow-sm transition-colors duration-300 z-10 ${isRunning ? 'animate-pulse' : ''}`}></div>

      {/* generalized-stack-wake D12 — managed-stack binding + live wake-state.
          Renders only when this node's kind is tied to a stack; unbound nodes
          are unchanged. */}
      <NodeStackOverlay nodeKind={data.node_kind} requiresStack={(data as any)?.requires_stack} />

      {isAwaitingApproval && (
        <div className="px-2 py-1.5 bg-amber-50 border-b border-amber-200">
          <p className="text-[10px] font-medium text-amber-700 mb-1">Waiting for your approval</p>
          <button
            type="button"
            className="w-full text-[10px] font-medium px-2 py-1 rounded bg-amber-500 text-white hover:bg-amber-600"
            onClick={(e) => {
              // The dialog owns approve/reject/notes and the 409 handling.
              // Duplicating that here would be a second decision path to keep
              // correct; this only opens it.
              e.stopPropagation();
              window.dispatchEvent(new CustomEvent('mod:approvals:open'));
            }}
          >
            Review &amp; approve
          </button>
        </div>
      )}

            {/* Inputs. A node with a SINGLE input keeps the classic top-edge
                handle (unchanged for the ~all single-input nodes). A node with
                MULTIPLE named inputs (object-form ports, e.g. the SDXL LoRA
                Train node's Image / Regularization / Avoid-Negative / Captions
                channels) renders each input as a LEFT-edge handle with a
                visible, colour-coded label so every channel is a distinct,
                named, connectable port — not an anonymous cluster of squares. */}
            {inputs.length > 1 ? (
              <div className="border-b border-blue-700 bg-blue-600">
                {inputs.map((input) => {
                  const baseColor = getColorForType(input.type);
                  const handleStyle = getHandleStyle(input.fileMode ?? inputFileMode, baseColor);
                  const labelText = input.label || input.name;
                  const tooltipText = `Input: ${labelText} (${input.type})${input.fileMode ? ` - Mode: ${input.fileMode}` : ''}`;
                  // The labeled ROW is a positioned container. The Handle is
                  // layered as a full-row TRANSPARENT hit target (position
                  // absolute, inset 0; the wb-port-hit CSS above forces its box
                  // to 100% against globals' 14px !important), so a drop anywhere
                  // on the row connects AND its left edge (Position.Left anchor)
                  // pins the connection hint line to the port. The coloured
                  // swatch + WHITE label are siblings with `pointer-events:none`
                  // so the pointer falls through them to the handle, and — living
                  // in the full-width row, not inside a 14px handle — the label
                  // no longer truncates to nothing.
                  const swatchStyle = {
                    ...handleStyle,
                    position: 'static' as const,
                    zIndex: 'auto' as const,
                    width: '12px',
                    height: '12px',
                    flexShrink: 0,
                  };
                  return (
                    <div
                      key={input.name}
                      className="relative flex items-center gap-1.5 py-1 pl-3 pr-2"
                      title={tooltipText}
                    >
                      <Handle
                        type="target"
                        position={Position.Left}
                        id={input.name}
                        title={tooltipText}
                        isConnectable={isConnectable}
                        className="wb-port-hit"
                        style={{
                          position: 'absolute',
                          top: 0,
                          left: 0,
                          right: 0,
                          bottom: 0,
                          transform: 'none',
                          borderRadius: 0,
                          background: 'transparent',
                          border: 'none',
                          zIndex: 20,
                        }}
                      />
                      <span className="rounded-sm pointer-events-none" style={swatchStyle} />
                      <span className="text-[10px] leading-tight text-white truncate pointer-events-none">{labelText}</span>
                    </div>
                  );
                })}
              </div>
            ) : (
              inputs.map((input, index: number) => {
                const baseColor = getColorForType(input.type);
                const handleStyle = getHandleStyle(input.fileMode ?? inputFileMode, baseColor);
                const labelText = input.label || input.name;
                const tooltipText = `Input: ${labelText} (${input.type})${(input.fileMode ?? inputFileMode) ? ` - Mode: ${input.fileMode ?? inputFileMode}` : ''}`;
                return (
                  <Handle
                    key={input.name}
                    type="target"
                    position={Position.Top}
                    id={input.name}
                    title={tooltipText}
                    style={{
                      left: `${(100 / (inputs.length + 1)) * (index + 1)}%`,
                      ...handleStyle,
                    }}
                    isConnectable={isConnectable}
                  />
                );
              })
            )}

            {/* Header bar */}
            <div className="px-3 pt-6 pb-2">
                <div className="flex items-center gap-2">
                    <div className="rounded-full w-6 h-6 flex-shrink-0 flex items-center justify-center bg-purple-100 text-purple-500">
                        <Settings className="h-3 w-3" />
                    </div>
                    <div className="text-xs font-bold truncate">{data.label || "Process"}</div>
                    {/* FX.72 p4 (Q55=C): real text badge, not colour-only (a11y). */}
                    {isAiNode(data) && (
                        <span
                            title={AI_OUTPUT_TOOLTIP}
                            className="flex-shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded bg-violet-100 text-violet-700"
                        >
                            {AI_OUTPUT_LABEL_TEXT}
                        </span>
                    )}
                    {/* FX.103 p4 — deprecation/retirement badge on the node
                        header (same badge component the palette uses). Reads
                        the canvas NodeData lifecycle hydrated at drop time and
                        refreshed by the node panel's live-def fetch. */}
                    <LifecycleBadge
                        className="mx-1"
                        lifecycleState={(data as any).lifecycleState}
                        retireAt={(data as any).retireAt}
                    />
                    {typeof data.currentNodeVersion === 'number' && (
                        <span
                            title={isVersionBehind ? `Update available (v${data.currentNodeVersion})` : `Version ${data.currentNodeVersion}`}
                            className={`ml-auto flex-shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded-full ${isVersionBehind ? 'bg-amber-100 text-amber-700' : 'bg-gray-100 text-gray-500'}`}
                        >
                            v{data.currentNodeVersion}{isVersionBehind ? ' ↑' : ''}
                        </span>
                    )}
                </div>
                {data.nickname && <div className="text-[11px] text-gray-500 truncate mt-0.5 pl-8">{data.nickname}</div>}
            </div>

            {/* Preview area — always visible */}
            <div className="w-full h-36 bg-gray-50 border-t border-b border-gray-100 relative flex items-center justify-center">
                {hasOutput ? (
                    /* Completed — show output preview */
                    <>
                    <AssetPreview assets={data.previewAssets ?? []} aiGenerated={isAiNode(data)} />
                    {has3DOutput && (
                        <button
                            onClick={(e) => { e.stopPropagation(); setViewerOpen(true); }}
                            className="absolute bottom-1 right-1 bg-indigo-600/90 text-white text-[8px] font-bold px-1.5 py-0.5 rounded z-10 hover:bg-indigo-700"
                        >
                            View 3D
                        </button>
                    )}
                    </>
                ) : isRunning && hasInputPreview ? (
                    /* Running with input preview — show input being processed */
                    <>
                        {data.inputPreviewCategory === 'video' ? (
                            <video
                                src={data.inputPreviewUrl}
                                className="w-full h-full object-cover opacity-50"
                                muted loop autoPlay playsInline
                            />
                        ) : (
                            <img
                                src={data.inputPreviewUrl}
                                alt="Processing"
                                className="w-full h-full object-cover opacity-50"
                            />
                        )}
                        <div className="absolute inset-0 flex flex-col items-center justify-center">
                            <div className="flex items-center gap-1.5 bg-black/60 text-white text-xs font-medium px-3 py-1.5 rounded-full">
                                <Loader2 className="w-3 h-3 animate-spin" />
                                Processing...
                            </div>
                            {data.started_at && <ElapsedTimer startedAt={data.started_at} />}
                        </div>
                    </>
                ) : isRunning ? (
                    /* Running without input preview */
                    <div className="flex flex-col items-center gap-1.5 text-gray-400">
                        <Loader2 className="w-5 h-5 animate-spin" />
                        <span className="text-[10px]">Processing...</span>
                        {data.started_at && <ElapsedTimer startedAt={data.started_at} />}
                    </div>
                ) : isFailed ? (
                    /* Failed */
                    <div className="flex flex-col items-center gap-1 text-red-400 px-2">
                        <span className="text-lg">!</span>
                        <span className="text-[10px] font-medium">Failed</span>
                        {data.error_message && (
                            <span className="text-[9px] text-red-300 text-center line-clamp-3 max-w-full">
                                {data.error_message}
                            </span>
                        )}
                    </div>
                ) : hasInputPreview ? (
                    /* Queued/idle but has parent preview — show what will be processed */
                    data.inputPreviewCategory === 'video' ? (
                        <video
                            src={data.inputPreviewUrl}
                            className="w-full h-full object-cover opacity-30"
                            muted playsInline
                        />
                    ) : (
                        <img
                            src={data.inputPreviewUrl}
                            alt="Input"
                            className="w-full h-full object-cover opacity-30"
                        />
                    )
                ) : (
                    /* Empty — no preview yet */
                    <div className="flex flex-col items-center gap-1 text-gray-300">
                        <ImageIcon className="w-6 h-6" />
                        <span className="text-[10px]">No preview</span>
                    </div>
                )}
            </div>

            {/* Footer info */}
            {data.description && (
                <div className="px-3 py-1.5 text-[10px] text-gray-400 truncate">
                    {data.description}
                </div>
            )}

            {/* LDF-3 §3 — credit + processing chips, bottom-right corner */}
            <div className="absolute bottom-1 right-1 z-10 pointer-events-none">
                <NodeChips
                    cost={data.cost_in_credits}
                    gpuVariants={data.gpu_variants}
                />
            </div>

            {outputs.map((output: { name: string; type: string }, index: number) => {
                const baseColor = getColorForType(output.type);
                const handleStyle = getHandleStyle(outputFileMode, baseColor);
                const tooltipText = `Output: ${output.name} (${output.type})${outputFileMode ? ` - Mode: ${outputFileMode}` : ''}${outputPaired !== undefined ? ` - Paired: ${outputPaired ? 'Yes' : 'No'}` : ''}`;
                return (
                  <Handle
                    key={output.name}
                    type="source"
                    position={Position.Bottom}
                    id={output.name}
                    title={tooltipText}
                    style={{
                      left: `${(100 / (outputs.length + 1)) * (index + 1)}%`,
                      ...handleStyle,
                    }}
                    isConnectable={isConnectable}
                  />
                );
            })}

            {/* 3D Viewer Modal */}
            {viewerOpen && first3DAsset && (
                <Suspense fallback={null}>
                    <Model3DViewerModal
                        url={first3DAsset.url}
                        ext={first3DExt}
                        onClose={() => setViewerOpen(false)}
                    />
                </Suspense>
            )}
    </div>
  );
});

ProcessNode.displayName = "ProcessNode";