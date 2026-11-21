"use client";

import { useState, useCallback, useRef } from 'react';
import { addEdge, useReactFlow, type Connection, type Edge, type OnConnectStartParams, type Node } from 'reactflow';
import { createNode } from '@/lib/workflow-utils';
import { nextFreePosition } from '@/lib/canvas-placement';
import { toast } from "@/components/ui/use-toast";
import type { NodeData, NodeDefinition } from '@/lib/types';

// Helper function to get schema entries, same as in the original file
type SchemaEntry = { name: string; type: string };

const getSchemaEntries = (
    schema: { [key: string]: any } | undefined,
    legacyTypeKey: 'input_type' | 'output_type',
): SchemaEntry[] => {
    if (!schema) return [];
    if (schema[legacyTypeKey]) {
        if (schema[legacyTypeKey] === 'NONE') return [];
        const types: string[] = schema[legacyTypeKey].split(',').map((t: string) => t.trim().toUpperCase());
        const name = legacyTypeKey.replace('_type', '');
        return types.map((type: string, index: number) => ({
            name: types.length > 1 ? `${name}_${index}` : name,
            type,
        }));
    }
    return Object.entries(schema).map(([name, props]) => ({
        name,
        type: typeof props === 'object' && props !== null && props.type ? props.type.toUpperCase() : 'ANY',
    }));
};

// Resolve effective I/O schema, accounting for dynamic overrides from extraVariables
// (e.g., NIM model selection sets INPUT_MODE/OUTPUT_MODE)
const getEffectiveSchema = (
    schema: { [key: string]: any } | undefined,
    typeKey: 'input_type' | 'output_type',
    extraVars?: { [key: string]: any }
) => {
    if (!extraVars) return schema;
    const modeKey = typeKey === 'input_type' ? 'INPUT_MODE' : 'OUTPUT_MODE';
    const modeKeyLower = modeKey.toLowerCase();
    const dynamicMode = extraVars[modeKey] || extraVars[modeKeyLower];
    if (dynamicMode) {
        return { ...schema, [typeKey]: dynamicMode };
    }
    return schema;
};

// Type compatibility for canvas connections. Mirrors the backend
// services/port_type_enforcement.are_types_compatible so the canvas and the
// wizard agree on what can connect.
//  - wildcards ('', ANY, NONE) match anything
//  - exact match (case-insensitive)
//  - generic file containers (resource/file/binary/blob) carry an arbitrary
//    uploaded file, so they're compatible with any concrete format on the
//    other side — "Resource Input" (resource) CAN feed "PDF to Image" (pdf).
const WILDCARD_TYPES = new Set(['', 'any', 'none']);
const GENERIC_FILE_TYPES = new Set(['resource', 'file', 'binary', 'blob']);
export const typesConnectable = (a?: string, b?: string): boolean => {
    const aa = (a || '').trim().toLowerCase();
    const bb = (b || '').trim().toLowerCase();
    if (WILDCARD_TYPES.has(aa) || WILDCARD_TYPES.has(bb)) return true;
    if (aa === bb) return true;
    if (GENERIC_FILE_TYPES.has(aa) || GENERIC_FILE_TYPES.has(bb)) return true;
    return false;
};

type NodeDefinitionUI = Partial<NodeDefinition> & { nodeType: string };

// Operator/flow-control nodes render CUSTOM source handles (true/false,
// case-N, path-N) that are NOT described by output_schema, and a single
// unnamed (id=null) target handle. Their output handles therefore can't be
// validated against a schema — they're accepted as-is — and their input side
// is the default null handle.
const OPERATOR_NODE_TYPES = new Set(['if-else', 'switch', 'split', 'conditional']);

type NormalizedConnection = {
    source: string;
    sourceHandle: string | null;
    target: string;
    targetHandle: string | null;
};

// The valid OUTPUT handle ids for a node. `null` means "handles are dynamic /
// custom" (operator nodes) — any reported handle is accepted for that side.
const outputHandleNames = (node: Node<NodeData>): string[] | null => {
    if (OPERATOR_NODE_TYPES.has(node.type as string)) return null;
    return getSchemaEntries(
        getEffectiveSchema(node.data.output_schema, 'output_type', node.data.extraVariables),
        'output_type',
    ).map((e) => e.name);
};

// The valid INPUT (port) handle ids for a node. `null` means the node has a
// single unnamed target (operator nodes) — its input handle is null.
const inputHandleNames = (node: Node<NodeData>): string[] | null => {
    if (OPERATOR_NODE_TYPES.has(node.type as string)) return null;
    return getSchemaEntries(
        getEffectiveSchema(node.data.input_schema, 'input_type', node.data.extraVariables),
        'input_type',
    ).map((e) => e.name);
};

// Resolve which OUTPUT handle of `node` a connection is using, tolerant of
// ReactFlow crossing the reported handle ids on a reversed drag: try each
// reported handle against the node's real outputs, and fall back to the sole
// output when a node has exactly one. Returns {ok:false} when no reported
// handle is a real output (and there's no unambiguous default) — i.e. this
// node cannot be the source of this connection.
const pickOutputHandle = (
    node: Node<NodeData>,
    reported: (string | null | undefined)[],
): { ok: boolean; handle: string | null } => {
    const names = outputHandleNames(node);
    if (names === null) {
        // Operator source: outputs are custom handles — require a named handle.
        const h = reported.find((r) => r != null);
        return h != null ? { ok: true, handle: h } : { ok: false, handle: null };
    }
    const h = reported.find((r) => r != null && names.includes(r));
    if (h != null) return { ok: true, handle: h };
    if (names.length === 1) return { ok: true, handle: names[0] };
    return { ok: false, handle: null };
};

// Resolve which INPUT port of `node` a connection is using — mirror of
// pickOutputHandle for the target side.
const pickInputHandle = (
    node: Node<NodeData>,
    reported: (string | null | undefined)[],
): { ok: boolean; handle: string | null } => {
    const names = inputHandleNames(node);
    if (names === null) {
        // Operator target: single unnamed (null) input handle.
        return { ok: true, handle: null };
    }
    const h = reported.find((r) => r != null && names.includes(r));
    if (h != null) return { ok: true, handle: h };
    if (names.length === 1) return { ok: true, handle: names[0] };
    return { ok: false, handle: null };
};

// Force a completed connection into the ONE correct orientation:
// sourceHandle = an OUTPUT of the source node, targetHandle = an INPUT port of
// the target node. ReactFlow can hand us a reversed drag (nodes and/or handle
// ids crossed — the root cause of the sourceHandle="input"/targetHandle="output"
// garbage edge). We re-derive the orientation from the node schemas rather than
// trusting the reported source/target assignment:
//   - Try FORWARD first (reported source is the output side) so a normal drag
//     keeps the user's direction.
//   - Else try REVERSED (reported target is the output side) and swap.
//   - Else the connection is invalid (e.g. output->output) — return null so the
//     caller rejects it instead of saving a malformed edge.
export const normalizeConnection = (
    connection: Edge | Connection,
    sourceNode: Node<NodeData>,
    targetNode: Node<NodeData>,
): NormalizedConnection | null => {
    if (sourceNode.id === targetNode.id) return null; // no self-loops
    const reported = [connection.sourceHandle, connection.targetHandle];

    const distinct = (a: string | null, b: string | null): boolean => a == null || b == null || a !== b;

    // Forward: reported source node is the OUTPUT side.
    const fOut = pickOutputHandle(sourceNode, reported);
    const fIn = pickInputHandle(targetNode, reported);
    if (fOut.ok && fIn.ok && distinct(fOut.handle, fIn.handle)) {
        return { source: sourceNode.id, sourceHandle: fOut.handle, target: targetNode.id, targetHandle: fIn.handle };
    }

    // Reversed: reported target node is the OUTPUT side — swap.
    const rOut = pickOutputHandle(targetNode, reported);
    const rIn = pickInputHandle(sourceNode, reported);
    if (rOut.ok && rIn.ok && distinct(rOut.handle, rIn.handle)) {
        return { source: targetNode.id, sourceHandle: rOut.handle, target: sourceNode.id, targetHandle: rIn.handle };
    }

    return null;
};

const handleEq = (a: string | null | undefined, b: string | null | undefined): boolean =>
    (a ?? null) === (b ?? null);

// True when adding `edge` would duplicate an existing edge. Rejects:
//   1. the exact same wire (same source/target + same handles),
//   2. a second wire into an already-occupied (target node, target port) — an
//      input port takes one upstream, and this is what catches the normalized
//      garbage edge colliding with the correct one,
//   3. the same node pair already wired with the handles swapped.
export const isDuplicateEdge = (edge: NormalizedConnection, existing: Edge[]): boolean =>
    existing.some((e) => {
        const exact =
            e.source === edge.source &&
            e.target === edge.target &&
            handleEq(e.sourceHandle, edge.sourceHandle) &&
            handleEq(e.targetHandle, edge.targetHandle);
        const portOccupied =
            edge.targetHandle != null &&
            e.target === edge.target &&
            handleEq(e.targetHandle, edge.targetHandle);
        const swappedPair =
            e.source === edge.target &&
            e.target === edge.source &&
            handleEq(e.sourceHandle, edge.targetHandle) &&
            handleEq(e.targetHandle, edge.sourceHandle);
        return exact || portOccupied || swappedPair;
    });

export const useWorkflowEvents = (
    nodes: Node<NodeData>[],
    edges: Edge[],
    setNodes: (nodes: Node[] | ((prevNodes: Node[]) => Node[])) => void,
    setEdges: (edges: Edge[] | ((prevEdges: Edge[]) => Edge[])) => void,
    allNodeDefs: NodeDefinitionUI[],
    // Records an undo/redo history entry for the CURRENT (pre-mutation) canvas
    // state. Called immediately before a real mutation (add node, connect) so
    // the change becomes undoable. No-op default keeps the hook usable without
    // a history stack.
    commit: () => void = () => {}
) => {
    const reactFlowWrapper = useRef<HTMLDivElement>(null);
    const { screenToFlowPosition, project } = useReactFlow();
    const connectingNode = useRef<OnConnectStartParams | null>(null);
    const [menu, setMenu] = useState<any>(null);

    const onConnectStart = useCallback((_: any, params: OnConnectStartParams) => {
        connectingNode.current = params;
    }, []);

    const onConnectEnd = useCallback((event: MouseEvent | TouchEvent) => {
        if (!connectingNode.current) return;

        const targetIsPane = (event.target as Element).classList.contains("react-flow__pane");

        if (targetIsPane && reactFlowWrapper.current) {
            const { nodeId, handleId, handleType } = connectingNode.current;
            // ReactFlow's OnConnectEnd hands us MouseEvent | TouchEvent.
            // Pull clientX/Y from the right place per event type.
            const point = "touches" in event
                ? { x: event.changedTouches[0].clientX, y: event.changedTouches[0].clientY }
                : { x: event.clientX, y: event.clientY };
            const position = screenToFlowPosition({
                x: point.x,
                y: point.y,
            });

            setTimeout(() => {
                const offset = 2; // Add a small offset to the side
                setMenu({
                    top: point.y,
                    left: point.x + offset, // Position menu to the right of the cursor
                    position,
                    sourceNodeId: nodeId,
                    sourceHandleId: handleId,
                    sourceHandleType: handleType
                });
            }, 0);
        }
    }, [screenToFlowPosition]);

    // Structural validity gate ReactFlow calls DURING a drag. Rejects any drop
    // that can't be resolved to a real output->input orientation (e.g.
    // output->output, or a handle that belongs to neither node's ports) and any
    // drop that would duplicate an existing wire. A reversed-but-resolvable drag
    // is allowed here and normalized in onConnect.
    const isValidConnection = useCallback((connection: Connection): boolean => {
        const sourceNode = nodes.find((node) => node.id === connection.source);
        const targetNode = nodes.find((node) => node.id === connection.target);
        if (!sourceNode || !targetNode) return false;

        const normalized = normalizeConnection(connection, sourceNode, targetNode);
        if (!normalized) return false;
        if (isDuplicateEdge(normalized, edges)) return false;
        return true;
    }, [nodes, edges]);

    const onConnect = useCallback((connection: Edge | Connection) => {
        const sourceNode = nodes.find((node) => node.id === connection.source);
        const targetNode = nodes.find((node) => node.id === connection.target);

        if (!sourceNode || !targetNode) return;

        // Force the connection into the correct source-output -> target-input
        // orientation, re-deriving it from the node schemas. A reversed drag is
        // normalized here; a genuinely invalid one (no valid orientation) is
        // rejected instead of being saved as a malformed edge.
        const normalized = normalizeConnection(connection, sourceNode, targetNode);
        if (!normalized) {
            toast({
                title: "Connection Error",
                description: "Invalid connection — connect a node's output to another node's input.",
                variant: "destructive",
            });
            return;
        }

        // Re-resolve the nodes for the (possibly swapped) normalized orientation.
        const outNode = nodes.find((node) => node.id === normalized.source);
        const inNode = nodes.find((node) => node.id === normalized.target);
        if (!outNode || !inNode) return;

        // Type compatibility. Skipped when the source is an operator node (its
        // custom output handles have no schema type) or when either resolved
        // handle isn't in the schema (nothing to compare).
        if (!OPERATOR_NODE_TYPES.has(outNode.type as string)) {
            const outs = getSchemaEntries(getEffectiveSchema(outNode.data.output_schema, 'output_type', outNode.data.extraVariables), 'output_type');
            const ins = getSchemaEntries(getEffectiveSchema(inNode.data.input_schema, 'input_type', inNode.data.extraVariables), 'input_type');
            const outHandle = outs.find((o) => o.name === normalized.sourceHandle);
            const inHandle = ins.find((i) => i.name === normalized.targetHandle);
            if (outHandle && inHandle && !typesConnectable(outHandle.type, inHandle.type)) {
                toast({
                    title: "Connection Error",
                    description: `Cannot connect: Output "${outHandle.type}" does not match Input "${inHandle.type}".`,
                    variant: "destructive",
                });
                return;
            }
        }

        setEdges((eds) => {
            // Dedup against the latest edges inside the updater so a reversed
            // drag normalizing onto an existing wire (the duplicate-edge bug) is
            // dropped rather than added a second time.
            if (isDuplicateEdge(normalized, eds)) return eds;
            commit();
            return addEdge({ ...normalized, type: 'custom' }, eds);
        });
    }, [nodes, setEdges, commit]);

    const onDragOver = useCallback((event: React.DragEvent<HTMLDivElement>) => {
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
    }, []);

    const onDrop = useCallback((event: React.DragEvent<HTMLDivElement>) => {
        event.preventDefault();

        if (reactFlowWrapper.current) {
            const reactFlowBounds = reactFlowWrapper.current.getBoundingClientRect();
            const data = event.dataTransfer.getData("application/reactflow");

            if (!data) return;

            const nodeDef = JSON.parse(data);
            const position = screenToFlowPosition({
                x: event.clientX - reactFlowBounds.left,
                y: event.clientY - reactFlowBounds.top,
            });

            const newNode = createNode({ nodeDef, position });
            commit();
            setNodes((nds) => nds.concat(newNode));
        }
    }, [screenToFlowPosition, setNodes, commit]);

    // FX.68.p3 [U] — the keyboard alternative to the palette drag. Enter on a
    // focused palette row asks the parent (workflow-builder) to drop the node
    // here, at the canvas centre, reusing the SAME createNode the drag's onDrop
    // uses below — so a keyboard add is byte-identical to a mouse add, just
    // positioned. The centre is the flow-space centre of the current viewport
    // (screenToFlowPosition of the wrapper's centre), so the node lands where
    // the user is already looking; fitView keeps it in view.
    // BUG-1027j — the centre alone is not enough: every Enter-add used to land
    // on the SAME point, so the 2nd node covered the 1st and a keyboard user
    // could never reach the earlier ones (it also made the CE golden-path
    // Playwright spec time out clicking a buried node). The node now takes the
    // first free slot on that row — see lib/canvas-placement.ts.
    const addNodeAtCenter = useCallback((nodeDef: any) => {
        const wrapper = reactFlowWrapper.current;
        if (!wrapper) return;
        const bounds = wrapper.getBoundingClientRect();
        const centre = screenToFlowPosition({
            x: bounds.left + bounds.width / 2,
            y: bounds.top + bounds.height / 2,
        });
        // Palette payloads are the UI-typed Partial<NodeDefinition> with the
        // react-flow type under `nodeType`; `type` is only a fallback. The
        // drag path (NodeLibrary onDragStart) stamps `type: nodeDef.nodeType`,
        // so coerce identically here — otherwise React Flow misses nodeTypes.
        const nodeDefWithType = {
            ...nodeDef,
            type: (nodeDef as any).nodeType ?? (nodeDef as any).type,
        } as any;
        commit();
        // createNode has to run inside the updater so nextFreePosition sees the
        // CURRENT nds (React Flow may have applied other node changes between
        // here and the update); a stale closure would let two adds pick the
        // same free slot.
        setNodes((nds) => nds.concat(
            createNode({ nodeDef: nodeDefWithType, position: nextFreePosition(centre, nds) }),
        ));
    }, [screenToFlowPosition, setNodes, commit]);

    const onPaneClick = useCallback(() => {
        setMenu(null);
    }, []);

    const addRecommendedNode = (nodeDef: NodeDefinitionUI) => {
        if (!menu) return;

        const newNode = createNode({
            // Recommended-node payloads are typed as Partial<NodeDefinition>
            // for UI brevity but the live API always returns the full
            // NodeDefinition shape — coerce so createNode's stricter
            // signature is satisfied. The unknown hop is required because
            // Partial<X> doesn't structurally subtype X.
            nodeDef: { ...nodeDef, type: nodeDef.nodeType! } as unknown as NodeDefinition & { type: string },
            position: menu.position
        });
        // One undo entry covers both the new node and its edge below.
        commit();
        setNodes((nds) => nds.concat(newNode));

        // Auto-connect target port. The drag started from a specific handle on
        // the EXISTING node (menu.sourceHandleId); resolve that handle's type so
        // the new node's edge lands on the first TYPE-COMPATIBLE port — with
        // multi-input nodes (left-edge ports) that is often NOT port [0], so
        // blindly taking [0] used to make a mismatched or dropped edge. Falls
        // back to the first port when nothing is compatible (still connects).
        const sourceHandle = menu.sourceHandleId;
        const draggedNode = nodes.find((n) => n.id === menu.sourceNodeId);
        const draggedType = (() => {
            if (!draggedNode) return undefined;
            if (menu.sourceHandleType === 'source') {
                const outs = getSchemaEntries(getEffectiveSchema(draggedNode.data.output_schema, 'output_type', draggedNode.data.extraVariables), 'output_type');
                return (outs.find((o) => o.name === sourceHandle) || outs[0])?.type;
            }
            const ins = getSchemaEntries(getEffectiveSchema(draggedNode.data.input_schema, 'input_type', draggedNode.data.extraVariables), 'input_type');
            return (ins.find((i) => i.name === sourceHandle) || ins[0])?.type;
        })();
        const candidatePorts = menu.sourceHandleType === 'source'
            ? getSchemaEntries(nodeDef.input_schema, 'input_type')
            : getSchemaEntries(nodeDef.output_schema, 'output_type');
        const targetHandle = (
            candidatePorts.find((p) => typesConnectable(draggedType, p.type)) || candidatePorts[0]
        )?.name;

        const newEdge = {
            id: `e${menu.sourceNodeId}-${newNode.id}`,
            source: menu.sourceHandleType === 'source' ? menu.sourceNodeId : newNode.id,
            target: menu.sourceHandleType === 'source' ? newNode.id : menu.sourceNodeId,
            sourceHandle,
            targetHandle,
            type: 'custom',
        };
        setEdges((eds) => addEdge(newEdge, eds));
        setMenu(null);
    };

    const getRecommendedNodes = () => {
        if (!menu) return [];

        const { sourceNodeId, sourceHandleId, sourceHandleType } = menu;
        const sourceNode = nodes.find(n => n.id === sourceNodeId);
        if (!sourceNode) return [];

        const effectiveOutputSchema = getEffectiveSchema(sourceNode.data.output_schema, 'output_type', sourceNode.data.extraVariables);
        const effectiveInputSchema = getEffectiveSchema(sourceNode.data.input_schema, 'input_type', sourceNode.data.extraVariables);
        const sourceOutputs = getSchemaEntries(effectiveOutputSchema, 'output_type');
        const sourceInputs = getSchemaEntries(effectiveInputSchema, 'input_type');

        if (sourceHandleType === 'source') {
            const sourceHandle = sourceOutputs.find(o => o.name === sourceHandleId) || sourceOutputs[0];
            if (!sourceHandle) return [];

            return allNodeDefs.filter(def => {
                const targetInputs = getSchemaEntries(def.input_schema, 'input_type');
                return targetInputs.some(input => typesConnectable(sourceHandle.type, input.type));
            });
        } else { // 'target'
            const sourceHandle = sourceInputs.find(i => i.name === sourceHandleId) || sourceInputs[0];
            if (!sourceHandle) return [];

            return allNodeDefs.filter(def => {
                const targetOutputs = getSchemaEntries(def.output_schema, 'output_type');
                return targetOutputs.some(output => typesConnectable(sourceHandle.type, output.type));
            });
        }
    };

    return {
        reactFlowWrapper,
        onConnect,
        isValidConnection,
        onConnectStart,
        onConnectEnd,
        onDrop,
        onDragOver,
        onPaneClick,
        addNodeAtCenter,
        menu,
        addRecommendedNode,
        getRecommendedNodes,
    };
};