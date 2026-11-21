"use client";
import { apiUrl } from "@/lib/runtime-config";

import { useEffect, useMemo, useState } from "react";
import { X, Loader2, FileCode, AlertCircle } from "lucide-react";

/**
 * P9n.2 — Per-variant kernel-port diff viewer.
 *
 * Modal that fetches /woodsman/generation-artifacts/{taskId}/port-diff
 * and renders the kernel_porter's transforms: which files got rewritten,
 * what the before/after bodies look like, and what manual cleanup items
 * the porter flagged for the LLM or the user. Gives users visibility
 * into what hipify-clang / dpct / openvino actually did to their repo
 * before they trust the built image.
 *
 * The layout is intentionally simple: variant tabs along the top, file
 * list in a sidebar on the left, and a two-column before/after body on
 * the right. No smart line-diff algorithm in this pass — unified text
 * is enough to spot the changes when the porter's edits are localized
 * (which they almost always are). Richer diff rendering can swap in a
 * line-diff library later without changing the endpoint contract.
 */

type Transform = {
  filename: string;
  new_filename: string | null;
  before: string;
  after: string;
  delete: boolean;
};

type VariantDiff = {
  tool: string | null;
  compatibility: string | null;
  incompat_reason: string | null;
  transforms: Transform[];
  manual_cleanup_needed: string[];
  unsupported_apis: string[];
  dockerfile_env: Record<string, string>;
  requirements_patches: { add: string[]; remove: string[] };
};

type PortDiffResponse = {
  task_id: number;
  variants: Record<string, VariantDiff>;
};

type Props = {
  open: boolean;
  onClose: () => void;
  taskId: number;
  accessToken: string | null;
};

export function VariantPortDiff({ open, onClose, taskId, accessToken }: Props) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [data, setData] = useState<PortDiffResponse | null>(null);
  const [activeVariant, setActiveVariant] = useState<string>("");
  const [activeFile, setActiveFile] = useState<string>("");

  useEffect(() => {
    if (!open || !accessToken) return;
    setLoading(true);
    setError(null);
    const API_URL = apiUrl();
    fetch(`${API_URL}/woodsman/generation-artifacts/${taskId}/port-diff`, {
      headers: { Authorization: `Bearer ${accessToken}` },
    })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<PortDiffResponse>;
      })
      .then((resp) => {
        setData(resp);
        const variants = Object.keys(resp.variants || {});
        if (variants.length) {
          setActiveVariant(variants[0]);
          const firstFile = resp.variants[variants[0]]?.transforms[0]?.filename || "";
          setActiveFile(firstFile);
        }
      })
      .catch((e) => setError(String(e.message || e)))
      .finally(() => setLoading(false));
  }, [open, taskId, accessToken]);

  const activeVariantData: VariantDiff | null = useMemo(
    () => (activeVariant && data?.variants[activeVariant]) || null,
    [activeVariant, data],
  );

  const activeTransform: Transform | null = useMemo(() => {
    if (!activeVariantData) return null;
    return activeVariantData.transforms.find((t) => t.filename === activeFile) || null;
  }, [activeVariantData, activeFile]);

  if (!open) return null;

  const variantNames = data ? Object.keys(data.variants).sort() : [];

  return (
    <div className="fixed inset-0 z-50 flex items-stretch justify-center bg-black/40">
      <div className="bg-white w-full max-w-7xl m-4 rounded-lg shadow-2xl flex flex-col">
        <div className="flex items-center justify-between border-b px-6 py-4">
          <div>
            <h2 className="text-lg font-semibold text-gray-800">
              Port diff viewer
            </h2>
            <p className="text-xs text-gray-500 mt-0.5">
              What the kernel porter changed for each variant. Inspect before
              trusting the built image.
            </p>
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-700 p-1"
            aria-label="Close"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {loading && (
          <div className="flex-1 flex items-center justify-center text-gray-500">
            <Loader2 className="w-5 h-5 animate-spin mr-2" /> Loading port data…
          </div>
        )}
        {error && (
          <div className="flex-1 flex items-center justify-center text-red-600 px-6 text-center">
            <AlertCircle className="w-5 h-5 mr-2" /> Failed to load port diff: {error}
          </div>
        )}
        {!loading && !error && variantNames.length === 0 && (
          <div className="flex-1 flex flex-col items-center justify-center text-gray-500 px-6 text-center gap-2">
            <FileCode className="w-10 h-10 text-gray-300" />
            <div>No port data for this build.</div>
            <div className="text-xs text-gray-400 max-w-md">
              The kernel porter only runs for repos with compiled CUDA kernels
              (deep-CUDA). Pure-PyTorch and CPU-only repos skip the pass —
              nothing to diff.
            </div>
          </div>
        )}

        {!loading && !error && variantNames.length > 0 && (
          <div className="flex-1 flex flex-col min-h-0">
            {/* Variant tabs */}
            <div className="border-b px-6 pt-2 flex gap-2 overflow-x-auto">
              {variantNames.map((v) => {
                const vd = data!.variants[v];
                const incompat = vd.compatibility === "incompatible";
                const active = v === activeVariant;
                return (
                  <button
                    key={v}
                    onClick={() => {
                      setActiveVariant(v);
                      const first = vd.transforms[0]?.filename || "";
                      setActiveFile(first);
                    }}
                    className={`px-3 py-2 text-sm font-medium border-b-2 transition-colors ${
                      active
                        ? "border-indigo-600 text-indigo-700"
                        : "border-transparent text-gray-500 hover:text-gray-700"
                    } ${incompat ? "text-red-600" : ""}`}
                    title={vd.incompat_reason || `Ported via ${vd.tool || "unknown tool"}`}
                  >
                    <span className="uppercase">{v}</span>
                    <span className="ml-2 text-[10px] font-mono text-gray-400">
                      {vd.tool || "—"}
                    </span>
                    {incompat && <span className="ml-1 text-[10px] text-red-500">(incompatible)</span>}
                  </button>
                );
              })}
            </div>

            {/* Per-variant body */}
            {activeVariantData && (
              <div className="flex-1 flex min-h-0">
                {/* File sidebar */}
                <div className="w-64 border-r bg-gray-50 overflow-y-auto">
                  {activeVariantData.incompat_reason && (
                    <div className="p-3 text-xs text-red-700 bg-red-50 border-b border-red-100">
                      <div className="font-semibold mb-1">Incompatible</div>
                      {activeVariantData.incompat_reason}
                    </div>
                  )}
                  {activeVariantData.transforms.length === 0 ? (
                    <div className="p-4 text-xs text-gray-400 italic">
                      No file transforms — porter ran but didn't touch any source.
                    </div>
                  ) : (
                    <ul>
                      {activeVariantData.transforms.map((t) => (
                        <li key={t.filename}>
                          <button
                            onClick={() => setActiveFile(t.filename)}
                            className={`w-full text-left px-3 py-2 text-xs font-mono border-b hover:bg-white ${
                              t.filename === activeFile ? "bg-white font-semibold text-indigo-700" : "text-gray-700"
                            }`}
                          >
                            <div className="truncate">{t.filename}</div>
                            {t.new_filename && t.new_filename !== t.filename && (
                              <div className="text-[10px] text-gray-400 truncate">
                                → {t.new_filename}
                              </div>
                            )}
                            {t.delete && (
                              <div className="text-[10px] text-red-500">deleted</div>
                            )}
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}

                  {/* Cleanup panel */}
                  {activeVariantData.manual_cleanup_needed.length > 0 && (
                    <div className="p-3 border-t bg-amber-50">
                      <div className="text-xs font-semibold text-amber-800 mb-1">
                        Manual cleanup ({activeVariantData.manual_cleanup_needed.length})
                      </div>
                      <ul className="text-[11px] text-amber-900 list-disc list-inside space-y-0.5">
                        {activeVariantData.manual_cleanup_needed.map((item, i) => (
                          <li key={i}>{item}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {activeVariantData.unsupported_apis.length > 0 && (
                    <div className="p-3 border-t bg-red-50">
                      <div className="text-xs font-semibold text-red-800 mb-1">
                        Unsupported APIs
                      </div>
                      <div className="text-[11px] font-mono text-red-900">
                        {activeVariantData.unsupported_apis.join(", ")}
                      </div>
                    </div>
                  )}
                </div>

                {/* Diff panes */}
                <div className="flex-1 flex flex-col min-h-0">
                  {activeTransform ? (
                    <>
                      <div className="px-4 py-2 border-b bg-gray-50 flex items-center justify-between text-xs">
                        <div className="font-mono text-gray-700">
                          {activeTransform.filename}
                          {activeTransform.new_filename &&
                            activeTransform.new_filename !== activeTransform.filename && (
                              <span className="text-gray-400"> → {activeTransform.new_filename}</span>
                            )}
                        </div>
                        {activeTransform.delete && (
                          <span className="text-red-600 font-semibold">DELETED</span>
                        )}
                      </div>
                      <div className="flex-1 grid grid-cols-2 min-h-0">
                        <div className="border-r flex flex-col min-h-0">
                          <div className="px-3 py-1.5 bg-red-50 text-xs font-semibold text-red-800 border-b">
                            Before
                          </div>
                          <pre className="flex-1 overflow-auto p-3 text-[11px] font-mono whitespace-pre text-gray-800 bg-white">
                            {activeTransform.before || "(empty)"}
                          </pre>
                        </div>
                        <div className="flex flex-col min-h-0">
                          <div className="px-3 py-1.5 bg-green-50 text-xs font-semibold text-green-800 border-b">
                            After
                          </div>
                          <pre className="flex-1 overflow-auto p-3 text-[11px] font-mono whitespace-pre text-gray-800 bg-white">
                            {activeTransform.delete ? "(file deleted)" : activeTransform.after || "(empty)"}
                          </pre>
                        </div>
                      </div>
                    </>
                  ) : (
                    <div className="flex-1 flex items-center justify-center text-gray-400 text-sm">
                      Select a file on the left to view its diff.
                    </div>
                  )}

                  {/* Variant metadata footer */}
                  {(Object.keys(activeVariantData.dockerfile_env).length > 0 ||
                    activeVariantData.requirements_patches.add.length > 0 ||
                    activeVariantData.requirements_patches.remove.length > 0) && (
                    <div className="border-t p-3 text-xs bg-gray-50 space-y-1">
                      {Object.keys(activeVariantData.dockerfile_env).length > 0 && (
                        <div>
                          <span className="font-semibold text-gray-700">Dockerfile env:</span>{" "}
                          <span className="font-mono text-gray-600">
                            {Object.entries(activeVariantData.dockerfile_env)
                              .map(([k, v]) => `${k}=${v}`)
                              .join(", ")}
                          </span>
                        </div>
                      )}
                      {(activeVariantData.requirements_patches.add.length > 0 ||
                        activeVariantData.requirements_patches.remove.length > 0) && (
                        <div>
                          <span className="font-semibold text-gray-700">Wheel swaps:</span>{" "}
                          {activeVariantData.requirements_patches.add.length > 0 && (
                            <span className="text-green-700 font-mono">
                              +{activeVariantData.requirements_patches.add.join(" +")}
                            </span>
                          )}{" "}
                          {activeVariantData.requirements_patches.remove.length > 0 && (
                            <span className="text-red-700 font-mono">
                              -{activeVariantData.requirements_patches.remove.join(" -")}
                            </span>
                          )}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
