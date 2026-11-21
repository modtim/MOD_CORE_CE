"use client"

import { memo } from "react"
import { nodeStateBorderClass } from "@/lib/node-state-style";
import { nodeAriaLabel } from "@/lib/canvas-a11y"
import { Handle, Position, type NodeProps } from "reactflow"
import { Shuffle } from "lucide-react"
import type { NodeData } from "@/lib/types"
import { NodeStackOverlay } from "./node-stack-overlay";

interface SwitchNodeData extends NodeData {
  outputCount?: number;
}

// Switch nodes always have orange border (their original color)
const getStatusColor = (): string => {
  return "border-orange-500"; // Always orange for switch nodes
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

export const SwitchNode = memo(({ data, isConnectable }: NodeProps<SwitchNodeData>) => {
  const { outputCount = 3 } = data; // Default to 3 cases
  const statusColor = nodeStateBorderClass(data, getStatusColor());
  const nodeAriaLabelValue = nodeAriaLabel("switch", data.label);

  return (
    <div role="group" aria-label={nodeAriaLabelValue} className={`px-4 pt-6 pb-2 shadow-md rounded-md bg-white border-2 ${statusColor} w-max transition-colors duration-300 relative`}>
      <NodeStackOverlay nodeKind={(data as any)?.node_kind} requiresStack={(data as any)?.requires_stack} />
      {/* Status indicator box in top-right corner */}
      <div className={`absolute -top-2 -right-2 w-5 h-5 rounded ${getStatusBgColor(data.status)} border-2 border-white shadow-sm transition-colors duration-300 ${data.status === 'running' ? 'animate-pulse' : ''}`}></div>

      <div className="flex items-center gap-2">
        <div className="rounded-full w-8 h-8 flex-shrink-0 flex items-center justify-center bg-orange-100 text-orange-500">
          <Shuffle className="h-4 w-4" />
        </div>
        <div className="text-sm font-bold whitespace-nowrap">{data.label || "Switch"}</div>
      </div>

      <div className="text-xs text-gray-500 mt-1 max-w-xs">
        {data.description || `Branch based on ${outputCount} cases.`}
      </div>

      <Handle
        type="target"
        position={Position.Top}
        title="Input"
        isConnectable={isConnectable}
        className="w-2.5 h-2.5 bg-orange-500"
      />

      {Array.from({ length: outputCount }, (_, i) => (
        <Handle
          key={`case-${i}`}
          type="source"
          position={Position.Bottom}
          id={`case-${i}`}
          title={`Case ${i + 1}`}
          style={{ left: `${(100 / (outputCount + 1)) * (i + 1)}%` }}
          isConnectable={isConnectable}
          className="w-2.5 h-2.5 bg-orange-500"
        />
      ))}
    </div>
  )
})

SwitchNode.displayName = "SwitchNode"
