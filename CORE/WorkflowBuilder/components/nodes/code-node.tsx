"use client"

import { memo } from "react"
import { nodeStateBorderClass } from "@/lib/node-state-style";
import { Handle, Position, type NodeProps } from "reactflow"
import { Code } from "lucide-react"
import type { NodeData } from "@/lib/types"
import { nodeAriaLabel } from "@/lib/canvas-a11y"
import { NodeStackOverlay } from "./node-stack-overlay";

// Code nodes always have gray border (their original color)
const getStatusColor = (): string => {
  return "border-gray-500"; // Always gray for code nodes
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

export const CodeNode = memo(({ data, isConnectable }: NodeProps<NodeData>) => {
  const statusColor = nodeStateBorderClass(data, getStatusColor());
  // R-038 — a11y: the ReactFlow wrapper (nodesFocusable) is the focusable
  // element (tabIndex=0, role=button, native Enter/Delete handling); the card
  // itself is a labelled group so screen readers announce "<type>: <label>".
  const nodeAriaLabelValue = nodeAriaLabel("code", data.label);

  return (
    <div role="group" aria-label={nodeAriaLabelValue} className={`px-4 pt-6 pb-2 shadow-md rounded-md bg-white border-2 ${statusColor} w-max transition-colors duration-300 relative`}>
      <NodeStackOverlay nodeKind={(data as any)?.node_kind} requiresStack={(data as any)?.requires_stack} />
      {/* Status indicator box in top-right corner */}
      <div className={`absolute -top-2 -right-2 w-5 h-5 rounded ${getStatusBgColor(data.status)} border-2 border-white shadow-sm transition-colors duration-300 ${data.status === 'running' ? 'animate-pulse' : ''}`}></div>

      <div className="flex items-center gap-2">
        <div className="rounded-full w-8 h-8 flex items-center justify-center bg-gray-100 text-gray-500">
          <Code className="h-4 w-4" />
        </div>
        <div className="text-sm font-bold whitespace-nowrap">{data.label || "Code"}</div>
      </div>

      {data.description && (
        <div className="text-xs text-gray-500 mt-1 max-w-xs">
          {data.description}
        </div>
      )}

      {/* This seems to be a custom property you had, so I'm keeping it. */}
      {/* @ts-ignore */}
      {data.codeLanguage && <div className="mt-2 text-xs bg-gray-100 p-1 rounded">Language: {data.codeLanguage}</div>}

      <Handle
        type="target"
        position={Position.Top}
        title="Input"
        isConnectable={isConnectable}
        className="w-2.5 h-2.5 bg-gray-500"
      />
      <Handle
        type="source"
        position={Position.Bottom}
        title="Output"
        isConnectable={isConnectable}
        className="w-2.5 h-2.5 bg-gray-500"
      />
    </div>
  )
})

CodeNode.displayName = "CodeNode"
