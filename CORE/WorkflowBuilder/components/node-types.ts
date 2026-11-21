import { InputNode } from "./nodes/input-node";
import { OutputNode } from "./nodes/output-node";
import { ProcessNode } from "./nodes/process-node";
import { ConditionalNode } from "./nodes/conditional-node";
import { CodeNode } from "./nodes/code-node";
import { IfElseNode } from "./nodes/if-else-node";
import { SwitchNode } from "./nodes/switch-node";
import { SplitNode } from "./nodes/split-node";
import CustomEdge from "./custom-edge";
import type { NodeTypes, EdgeTypes } from "reactflow";

export const nodeTypes: NodeTypes = {
  input: InputNode,
  output: OutputNode,
  process: ProcessNode,
  conditional: ConditionalNode,
  code: CodeNode,
  "if-else": IfElseNode,
  switch: SwitchNode,
  split: SplitNode,
};

export const edgeTypes: EdgeTypes = {
  custom: CustomEdge,
};