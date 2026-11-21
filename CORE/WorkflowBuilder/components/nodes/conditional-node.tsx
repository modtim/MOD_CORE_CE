"use client"

import { memo } from "react"
import { nodeStateBorderClass } from "@/lib/node-state-style";
import { nodeAriaLabel } from "@/lib/canvas-a11y"
import { Handle, Position, type NodeProps } from "reactflow"
import { GitBranch } from "lucide-react"
import type { NodeData } from "@/lib/types"
import { NodeStackOverlay } from "./node-stack-overlay";

// Conditional nodes always have amber border (their original color)
const getStatusColor = (): string => {
  return "border-amber-500"; // Always amber for conditional nodes
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

export const ConditionalNode = memo(({ data, isConnectable }: NodeProps<NodeData>) => {
  const statusColor = nodeStateBorderClass(data, getStatusColor());
  const nodeAriaLabelValue = nodeAriaLabel("conditional", data.label);

  return (
    <div role="group" aria-label={nodeAriaLabelValue} className={`px-4 pt-6 pb-2 shadow-md rounded-md bg-white border-2 ${statusColor} w-max transition-colors duration-300 relative`}>
      <NodeStackOverlay nodeKind={(data as any)?.node_kind} requiresStack={(data as any)?.requires_stack} />
      {/* Status indicator box in top-right corner */}
      <div className={`absolute -top-2 -right-2 w-5 h-5 rounded ${getStatusBgColor(data.status)} border-2 border-white shadow-sm transition-colors duration-300 ${data.status === 'running' ? 'animate-pulse' : ''}`}></div>

          <div className="flex items-center gap-2">
              <div className="rounded-full w-8 h-8 flex items-center justify-center bg-amber-100 text-amber-500">
              <GitBranch className="h-4 w-4" />
              </div>
              <div className="text-sm font-bold whitespace-nowrap">{data.label || "Conditional"}</div>
          </div>

          {data.description && (
              <div className="text-xs text-gray-500 mt-1 max-w-xs">{data.description}</div>
          )}

        {/* This seems to be a custom property you had, so I'm keeping it. */}
        {/* @ts-ignore */}
        {data.condition && <div className="mt-2 text-xs bg-gray-100 p-1 rounded">Condition: {data.condition}</div>}

        <div className="flex justify-between mt-2 text-xs">
             {/* @ts-ignore */}
            <div className="text-green-600">{data.trueLabel || "Yes"}</div>
             {/* @ts-ignore */}
            <div className="text-red-600">{data.falseLabel || "No"}</div>
        </div>

        <Handle
          type="target"
          position={Position.Top}
          title="Input"
          isConnectable={isConnectable}
          className="w-2.5 h-2.5 bg-amber-500"
        />
        <Handle
          type="source"
          position={Position.Bottom}
          id="true"
          title="Output: True"
          style={{ left: "25%" }}
          isConnectable={isConnectable}
          className="w-2.5 h-2.5 bg-green-500"
        />
        <Handle
          type="source"
          position={Position.Bottom}
          id="false"
          title="Output: False"
          style={{ left: "75%" }}
          isConnectable={isConnectable}
          className="w-2.5 h-2.5 bg-red-500"
        />
    </div>
  )
})

ConditionalNode.displayName = "ConditionalNode"