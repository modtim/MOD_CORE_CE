/**
 * BUG 2400LR-g4-addnode: a keyboard-added palette node must get the SAME
 * react-flow type as a dragged one (`nodeType`, e.g. "process"), even when the
 * palette payload also carries a definition `type` such as "Image". Otherwise
 * React Flow finds no nodeTypes["Image"] and renders its default node, not the
 * process card.
 */
import { renderHook, act } from "@testing-library/react";

jest.mock("reactflow", () => ({
    addEdge: jest.fn(),
    useReactFlow: () => ({
        screenToFlowPosition: (p: { x: number; y: number }) => p,
        project: (p: { x: number; y: number }) => p,
    }),
}));
jest.mock("@/components/ui/use-toast", () => ({ toast: jest.fn() }));

import { useWorkflowEvents } from "@/hooks/useWorkflowEvents";

const paletteDef = {
    id: 7,
    name: "Image Resize",
    label: "Image Resize",
    nodeType: "process",
    type: "Image",
    input_schema: {},
    output_schema: {},
};

describe("keyboard add vs drag add: react-flow node type", () => {
    it("keyboard add of { nodeType: 'process', type: 'Image' } yields type 'process', same as the drag payload", () => {
        let added: any[] = [];
        const setNodes = (u: any) => {
            added = typeof u === "function" ? u([]) : u;
        };
        const { result } = renderHook(() =>
            useWorkflowEvents([], [], setNodes as any, jest.fn() as any, [] as any, () => {})
        );
        (result.current.reactFlowWrapper as any).current = {
            getBoundingClientRect: () => ({ left: 0, top: 0, width: 200, height: 100 }),
        };

        act(() => result.current.addNodeAtCenter(paletteDef));
        const keyboardNode = added[0];

        // Drag path: NodeLibrary.onDragStart payload fed through onDrop.
        const payload = JSON.stringify({ ...paletteDef, type: paletteDef.nodeType });
        act(() =>
            result.current.onDrop({
                preventDefault: () => {},
                clientX: 100,
                clientY: 50,
                dataTransfer: { getData: () => payload },
            } as any)
        );
        const dragNode = added[0];

        expect(keyboardNode.type).toBe("process");
        expect(keyboardNode.type).toBe(dragNode.type);
        expect(keyboardNode.id.startsWith("process-")).toBe(true);
    });
});
