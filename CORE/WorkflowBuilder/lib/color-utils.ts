// A mapping from data type names to specific colors.
const typeColorMapping: { [key: string]: string } = {
  // Primary Data Types
  IMAGE: "#3b82f6", // blue-500
  MESH: "#22c55e", // green-500
  TEXT: "#a855f7", // purple-500
  STRING: "#a855f7", // purple-500
  VIDEO: "#f97316", // orange-500
  AUDIO: "#ec4899", // pink-500
  NUMBER: "#14b8a6", // teal-500
  BOOLEAN: "#f59e0b", // amber-500

  // Complex Data Types
  JSON: "#6366f1", // indigo-500
  OBJECT: "#6366f1", // indigo-500
  FILE: "#0ea5e9", // sky-500
  DATAFRAME: "#8b5cf6", // violet-500

  // Generic/Wildcard
  ANY: "#d946ef", // fuchsia-500

  // Default
  default: "#6b7280", // gray-500
};

/**
 * Returns a specific color for a given data type.
 * @param type The input or output type string from a node's schema.
 * @returns A hex color code.
 */
export const getColorForType = (type?: string): string => {
  if (type && typeColorMapping[type.toUpperCase()]) {
    return typeColorMapping[type.toUpperCase()];
  }
  return typeColorMapping["default"];
};