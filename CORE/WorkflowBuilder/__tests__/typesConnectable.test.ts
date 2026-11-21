import { typesConnectable } from "@/hooks/useWorkflowEvents";

describe("typesConnectable (canvas port compatibility)", () => {
  it("allows exact matches (case-insensitive)", () => {
    expect(typesConnectable("image", "image")).toBe(true);
    expect(typesConnectable("IMAGE", "image")).toBe(true);
  });

  it("treats ANY / NONE / empty as wildcards", () => {
    expect(typesConnectable("ANY", "pdf")).toBe(true);
    expect(typesConnectable("image", "any")).toBe(true);
    expect(typesConnectable("NONE", "pdf")).toBe(true);
    expect(typesConnectable("", "pdf")).toBe(true);
    expect(typesConnectable(undefined, "pdf")).toBe(true);
  });

  it("lets a generic file container feed a concrete format (the Resource Input -> PDF to Image case)", () => {
    expect(typesConnectable("resource", "pdf")).toBe(true);
    expect(typesConnectable("file", "pdf")).toBe(true);
    expect(typesConnectable("RESOURCE", "PDF")).toBe(true);
    // and the reverse: a concrete output feeding a generic-file consumer
    expect(typesConnectable("image", "resource")).toBe(true);
  });

  it("still rejects two distinct concrete formats", () => {
    expect(typesConnectable("image", "audio")).toBe(false);
    expect(typesConnectable("pdf", "mesh")).toBe(false);
  });
});
