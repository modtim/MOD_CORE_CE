/**
 * Regression coverage for the WorkflowBuilder orchestrator-runaway bug
 * (workflow_version 81): a saved edge `input-2 -> process-1` with
 * sourceHandle="input" / targetHandle="output" — a reversed/crossed drag saved
 * verbatim, plus a duplicate of the correct edge.
 *
 * These test the pure connection-hygiene helpers used by useWorkflowEvents'
 * onConnect / isValidConnection:
 *   - normalizeConnection forces the correct source-output -> target-input
 *     orientation, or rejects a genuinely invalid connection.
 *   - isDuplicateEdge blocks exact dups, an already-occupied target port, and
 *     the same node pair with swapped handles.
 */
import { normalizeConnection, isDuplicateEdge } from "@/hooks/useWorkflowEvents";
import type { Edge, Node } from "reactflow";
import type { NodeData } from "@/lib/types";

// image_input node: one OUTPUT port "output", no inputs.
const inputNode = (id: string): Node<NodeData> =>
    ({
        id,
        type: "input",
        position: { x: 0, y: 0 },
        data: {
            label: "Image Input",
            input_schema: undefined,
            output_schema: { output: { type: "image" } },
        },
    } as unknown as Node<NodeData>);

// sdxl_lora_train process node: multi-input (input/reg_images/avoid_images/
// captions) + one OUTPUT port "output".
const processNode = (id: string): Node<NodeData> =>
    ({
        id,
        type: "process",
        position: { x: 0, y: 200 },
        data: {
            label: "SDXL LoRA Train",
            input_schema: {
                input: { type: "image" },
                reg_images: { type: "image" },
                avoid_images: { type: "image" },
                captions: { type: "text" },
            },
            output_schema: { output: { type: "lora" } },
        },
    } as unknown as Node<NodeData>);

const operatorNode = (id: string): Node<NodeData> =>
    ({
        id,
        type: "if-else",
        position: { x: 0, y: 0 },
        data: { label: "If / Else" },
    } as unknown as Node<NodeData>);

describe("normalizeConnection (orientation hygiene)", () => {
    it("keeps a correct forward drag unchanged (output -> input)", () => {
        const src = inputNode("input-2");
        const tgt = processNode("process-1");
        const res = normalizeConnection(
            { source: "input-2", sourceHandle: "output", target: "process-1", targetHandle: "input" },
            src,
            tgt,
        );
        expect(res).toEqual({
            source: "input-2",
            sourceHandle: "output",
            target: "process-1",
            targetHandle: "input",
        });
    });

    it("REPAIRS the exact bug edge: crossed handles (sourceHandle=input/targetHandle=output)", () => {
        const src = inputNode("input-2");
        const tgt = processNode("process-1");
        // ReactFlow reported the handle ids crossed onto the wrong nodes.
        const res = normalizeConnection(
            { source: "input-2", sourceHandle: "input", target: "process-1", targetHandle: "output" },
            src,
            tgt,
        );
        expect(res).toEqual({
            source: "input-2",
            sourceHandle: "output",
            target: "process-1",
            targetHandle: "input",
        });
    });

    it("normalizes a fully reversed drag (source/target nodes swapped)", () => {
        // User dragged FROM the process input handle TO the input node's output.
        const reportedSource = processNode("process-1");
        const reportedTarget = inputNode("input-2");
        const res = normalizeConnection(
            { source: "process-1", sourceHandle: "input", target: "input-2", targetHandle: "output" },
            reportedSource,
            reportedTarget,
        );
        expect(res).toEqual({
            source: "input-2",
            sourceHandle: "output",
            target: "process-1",
            targetHandle: "input",
        });
    });

    it("preserves a specific multi-input target port (reg_images)", () => {
        const res = normalizeConnection(
            { source: "input-2", sourceHandle: "output", target: "process-1", targetHandle: "reg_images" },
            inputNode("input-2"),
            processNode("process-1"),
        );
        expect(res).toEqual({
            source: "input-2",
            sourceHandle: "output",
            target: "process-1",
            targetHandle: "reg_images",
        });
    });

    it("rejects an output -> output connection (no valid orientation)", () => {
        const res = normalizeConnection(
            { source: "input-2", sourceHandle: "output", target: "input-3", targetHandle: "output" },
            inputNode("input-2"),
            inputNode("input-3"),
        );
        expect(res).toBeNull();
    });

    it("rejects a self-loop", () => {
        const res = normalizeConnection(
            { source: "process-1", sourceHandle: "output", target: "process-1", targetHandle: "input" },
            processNode("process-1"),
            processNode("process-1"),
        );
        expect(res).toBeNull();
    });

    it("accepts an operator node's custom source handle without schema validation", () => {
        const res = normalizeConnection(
            { source: "if-1", sourceHandle: "true", target: "process-1", targetHandle: "input" },
            operatorNode("if-1"),
            processNode("process-1"),
        );
        expect(res).toEqual({
            source: "if-1",
            sourceHandle: "true",
            target: "process-1",
            targetHandle: "input",
        });
    });
});

describe("isDuplicateEdge", () => {
    const existing: Edge[] = [
        { id: "e1", source: "input-2", target: "process-1", sourceHandle: "output", targetHandle: "input" },
    ];

    it("flags the exact same wire", () => {
        expect(
            isDuplicateEdge(
                { source: "input-2", sourceHandle: "output", target: "process-1", targetHandle: "input" },
                existing,
            ),
        ).toBe(true);
    });

    it("flags a normalized garbage edge colliding with the correct one (same target port)", () => {
        // The repaired bug edge lands on (process-1, input) — already occupied.
        expect(
            isDuplicateEdge(
                { source: "input-2", sourceHandle: "output", target: "process-1", targetHandle: "input" },
                existing,
            ),
        ).toBe(true);
    });

    it("flags the same node pair wired with handles swapped", () => {
        expect(
            isDuplicateEdge(
                { source: "process-1", sourceHandle: "input", target: "input-2", targetHandle: "output" },
                existing,
            ),
        ).toBe(true);
    });

    it("allows a distinct wire into a different target port", () => {
        expect(
            isDuplicateEdge(
                { source: "input-2", sourceHandle: "output", target: "process-1", targetHandle: "reg_images" },
                existing,
            ),
        ).toBe(false);
    });
});
