"use client"

import type React from "react"

import { useCallback } from "react"
import { BaseEdge, EdgeLabelRenderer, type EdgeProps, getBezierPath, useReactFlow } from "reactflow"
import { GitBranch } from "lucide-react"

export default function CustomEdge({
  id,
  source,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  data,
  style = {},
  markerEnd,
}: EdgeProps) {
  const [edgePath, labelX, labelY] = getBezierPath({
    sourceX,
    sourceY,
    sourcePosition,
    targetX,
    targetY,
    targetPosition,
  })

  const { setEdges } = useReactFlow()

  const onEdgeClick = useCallback(
    (evt: React.MouseEvent<SVGGElement, MouseEvent>, id: string) => {
      evt.stopPropagation()
      setEdges((edges) => edges.filter((edge) => edge.id !== id))
    },
    [setEdges],
  )

  // Fan-out badge: the hook stamps data.fanCount on edges whose SOURCE node
  // ran as a batch fan-out (>1 task) in the selected run. Clicking opens the
  // per-file input/output modal. We use a DOM CustomEvent (the codebase's
  // cross-component convention) so the edge stays a pure presentational
  // component and doesn't need callbacks threaded through edge.data.
  const fanCount: number | undefined = data?.fanCount
  const fanSourceId: string | undefined = data?.fanSourceId || source
  const onFanClick = useCallback(
    (evt: React.MouseEvent) => {
      evt.stopPropagation()
      window.dispatchEvent(
        new CustomEvent("mod:open-fanout", {
          detail: { nodeId: fanSourceId, count: fanCount },
        }),
      )
    },
    [fanSourceId, fanCount],
  )

  return (
    <>
      <BaseEdge path={edgePath} markerEnd={markerEnd} style={style} />
      <EdgeLabelRenderer>
        {typeof fanCount === "number" && fanCount > 1 && (
          <button
            type="button"
            onClick={onFanClick}
            title={`Fan-out: ${fanCount} parallel items — click to view each input → output`}
            style={{
              position: "absolute",
              transform: `translate(-50%, -50%) translate(${labelX}px,${labelY + (data?.label ? 22 : 0)}px)`,
              background: "hsl(38 92% 50%)",
              color: "#1a1200",
              padding: "3px 9px",
              borderRadius: 999,
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: "0.04em",
              pointerEvents: "all",
              border: "1px solid hsl(38 92% 38%)",
              boxShadow: "0 1px 3px rgba(0,0,0,0.25)",
              cursor: "pointer",
              display: "flex",
              alignItems: "center",
              gap: 4,
            }}
            className="nodrag nopan"
          >
            <GitBranch size={12} strokeWidth={2.5} />
            FAN ×{fanCount}
          </button>
        )}
        {data?.label && (
          <div
            style={{
              position: "absolute",
              transform: `translate(-50%, -50%) translate(${labelX}px,${labelY}px)`,
              // Theme-aware via CSS variables — globals.css defines
              // --card / --foreground / --border for both `:root`
              // (light) and `.dark`, so the label flips automatically
              // when next-themes toggles the html class. Avoids a
              // useTheme() hook and keeps the edge component pure.
              background: "hsl(var(--card))",
              color: "hsl(var(--foreground))",
              padding: "4px 8px",
              borderRadius: 4,
              fontSize: 12,
              fontWeight: 500,
              pointerEvents: "all",
              border: "1px solid hsl(var(--border))",
            }}
            className="nodrag nopan"
          >
            {data.label}
          </div>
        )}
      </EdgeLabelRenderer>
    </>
  )
}
