"use client"

import { memo } from "react"
import { nodeStateBorderClass } from "@/lib/node-state-style";
import { nodeAriaLabel } from "@/lib/canvas-a11y"
import { Handle, Position, type NodeProps } from "reactflow"
import { GitCommitHorizontal } from "lucide-react"
import type { NodeData } from "@/lib/types"
import { NodeStackOverlay } from "./node-stack-overlay";

interface SplitNodeData extends NodeData {
  outputCount?: number;
}

// Split nodes always have cyan border (their original color)
const getStatusColor = (): string => {
  return "border-cyan-500"; // Always cyan for split nodes
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
    case "queued":
      return "bg-purple-500";
    default:
      return "bg-gray-300"; // Default grey for no status
  }
};

export const SplitNode = memo(({ data, isConnectable }: NodeProps<SplitNodeData>) => {
  const { outputCount = 2 } = data;
  const statusColor = nodeStateBorderClass(data, getStatusColor());
  const nodeAriaLabelValue = nodeAriaLabel("split", data.label);

  return (
    <div role="group" aria-label={nodeAriaLabelValue} className={`px-4 pt-6 pb-2 shadow-md rounded-md bg-white border-2 ${statusColor} w-max transition-colors duration-300 relative`}>
      <NodeStackOverlay nodeKind={(data as any)?.node_kind} requiresStack={(data as any)?.requires_stack} />
      {/* Status indicator box in top-right corner */}
      <div className={`absolute -top-2 -right-2 w-5 h-5 rounded ${getStatusBgColor(data.status)} border-2 border-white shadow-sm transition-colors duration-300 ${data.status === 'running' ? 'animate-pulse' : ''}`}></div>

      <div className="flex items-center gap-2">
        <div className="rounded-full w-8 h-8 flex-shrink-0 flex items-center justify-center bg-cyan-100 text-cyan-500">
          <GitCommitHorizontal className="h-4 w-4" />
        </div>
        <div className="text-sm font-bold whitespace-nowrap">{data.label || "Split"}</div>
      </div>

      <div className="text-xs text-gray-500 mt-1 max-w-xs">
        {data.description || `Split into ${outputCount} parallel paths.`}
      </div>

      <Handle
        type="target"
        position={Position.Top}
        title="Input"
        isConnectable={isConnectable}
        className="w-2.5 h-2.5 bg-cyan-500"
      />

      {Array.from({ length: outputCount }, (_, i) => (
        <Handle
          key={`path-${i}`}
          type="source"
          position={Position.Bottom}
          id={`path-${i}`}
          title={`Path ${i + 1}`}
          style={{ left: `${(100 / (outputCount + 1)) * (i + 1)}%` }}
          isConnectable={isConnectable}
          className="w-2.5 h-2.5 bg-cyan-500"
        />
      ))}
    </div>
  )
})

SplitNode.displayName = "SplitNode"
