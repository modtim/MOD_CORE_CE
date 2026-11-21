"use client";
import { apiUrl } from "@/lib/runtime-config";
import { getAccessToken } from "@/lib/api-client";

import { memo, useState, useEffect, lazy, Suspense } from "react";
import { nodeStateBorderClass } from "@/lib/node-state-style";
import { nodeAriaLabel } from "@/lib/canvas-a11y";
import { Handle, Position, type NodeProps } from "reactflow";
import { createPortal } from "react-dom";
import { Database, ImageIcon, FileIcon, FileCode, Loader2, Film, Box } from "lucide-react";

const Model3DViewerModal = lazy(() =>
  import('../model3d-thumbnail').then(m => ({ default: m.Model3DViewerModal }))
);

const VIDEO_EXTS = new Set(['mp4', 'mkv', 'avi', 'mov', 'webm']);
const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'avif', 'tif', 'tiff', 'tga', 'exr', 'hdr']);
// Formats a browser can actually paint in an <img>. tif/tiff/tga/exr/hdr are
// images but no mainstream browser decodes them, so an <img> pointed at their
// presigned URL always fails and reads as a missing preview — show a labeled
// placeholder for those instead.
const BROWSER_IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'avif']);
const MODEL3D_EXTS = new Set(['obj', 'fbx', 'glb', 'gltf', 'ply', 'stl', 'dae', 'usd', 'usdz']);
const TEXT_EXTS = new Set(['json', 'csv', 'txt', 'yaml', 'yml', 'xml', 'toml', 'ini', 'md', 'py', 'sh', 'js', 'ts', 'html', 'css', 'go', 'rs', 'rb', 'java', 'c', 'cpp', 'sql', 'log', 'dockerfile']);
import type { NodeData } from "@/lib/types";
import { getColorForType } from "@/lib/color-utils";
import { NodeChips } from "../node-chips";
import { NodeStackOverlay } from "./node-stack-overlay";

// Input nodes always have blue border (their original color)
const getStatusColor = (): string => {
  return "border-blue-500";
};

const getStatusBgColor = (status?: string): string => {
  switch (status) {
    case "running":
      return "bg-green-400";
    case "success":
      return "bg-green-500";
    case "failure":
      return "bg-red-500";
    case "queued":
      return "bg-purple-500";
    default:
      return "bg-gray-300";
  }
};

const getSchemaEntries = (schema: { [key: string]: any } | undefined, legacyTypeKey: 'input_type' | 'output_type') => {
    if (!schema) return [];

    if (schema[legacyTypeKey]) {
        if (schema[legacyTypeKey] === 'NONE') return [];

        const types = schema[legacyTypeKey].split(',').map((t: string) => t.trim());
        const name = legacyTypeKey.replace('_type', '');

        return types.map((type: string, index: number) => ({
            name: types.length > 1 ? `${name}_${index}` : name,
            type,
        }));
    }

    return Object.entries(schema).map(([name, props]) => ({
        name,
        type: typeof props === 'object' && props !== null && props.type ? props.type : 'ANY',
    }));
};

export const InputNode = memo(({ data, isConnectable, id }: NodeProps<NodeData>) => {
    const outputs = getSchemaEntries(data.output_schema, 'output_type');
    const statusColor = nodeStateBorderClass(data, getStatusColor());

    // Thumbnail state for selected file
    const [thumbnailUrl, setThumbnailUrl] = useState<string | null>(null);
    const selectedFiles = data.selectedFiles as any[] | undefined;
    const hasFiles = selectedFiles && selectedFiles.length > 0;
    const isRunning = data.status === 'running';

    // Detect file type from first selected file
    const firstFileName = hasFiles ? (selectedFiles![0].name || '') : '';
    const firstFileExt = firstFileName.split('.').pop()?.toLowerCase() || '';
    const isVideo = VIDEO_EXTS.has(firstFileExt);
    const is3D = MODEL3D_EXTS.has(firstFileExt);
    const isText = TEXT_EXTS.has(firstFileExt);
    const isPdf = firstFileExt === 'pdf';
    const isImageFile = IMAGE_EXTS.has(firstFileExt);
    const isRenderableImage = isImageFile && BROWSER_IMAGE_EXTS.has(firstFileExt);
    const isNonRenderableImage = isImageFile && !isRenderableImage;

    // True when a presigned image URL failed to load in the <img> — show a
    // placeholder instead of silently falling back to the generic file chrome.
    const [thumbFailed, setThumbFailed] = useState(false);

    // Text preview state
    const [textPreview, setTextPreview] = useState<string | null>(null);
    const [fullText, setFullText] = useState<string | null>(null);
    const [textViewerOpen, setTextViewerOpen] = useState(false);

    // 3D viewer state
    const [viewerOpen, setViewerOpen] = useState(false);
    const [modelUrl, setModelUrl] = useState<string | null>(null);

    const openViewer = () => {
      if (!hasFiles) return;
      const firstFile = selectedFiles![0];
      if (modelUrl) { setViewerOpen(true); return; }
      const API_URL = apiUrl();
      const token = getAccessToken();
      if (token && firstFile?.id) {
        fetch(`${API_URL}/assets/${firstFile.id}/download`, {
          headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
        })
          .then(r => r.ok ? r.json() : null)
          .then(result => {
            if (result?.download_url) { setModelUrl(result.download_url); setViewerOpen(true); }
          })
          .catch(() => {});
      }
    };

    useEffect(() => {
      setThumbFailed(false);
      let cancelled = false;
      if (!hasFiles) {
        setThumbnailUrl(null);
        return;
      }
      // Use the previewAssets if available (set after node completes)
      if (data.previewAssets && data.previewAssets.length > 0) {
        setThumbnailUrl(data.previewAssets[0].url);
        return;
      }
      // Get a presigned URL for the first selected file
      const firstFile = selectedFiles![0];
      if (firstFile?.id) {
        const API_URL = apiUrl();
        const token = getAccessToken();
        if (token) {
          // For text files, fetch content for preview
          if (isText) {
            fetch(`${API_URL}/assets/${firstFile.id}/text-content`, {
              headers: { Authorization: `Bearer ${token}` },
            })
              .then(r => r.ok ? r.text() : null)
              .then(text => {
                if (text) {
                  setTextPreview(text.substring(0, 300));
                  setFullText(text);
                }
              })
              .catch(() => {});
            return;
          }
          // For PDFs, use the preview-url endpoint (no Content-Disposition:
          // attachment) so the iframe renders the PDF inline instead of the
          // browser treating it as a download.
          if (isPdf) {
            fetch(`${API_URL}/assets/${firstFile.id}/preview-url`, {
              headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
            })
              .then(r => r.ok ? r.json() : null)
              .then(result => {
                if (result?.url) setThumbnailUrl(result.url);
              })
              .catch(() => {});
            return;
          }
          // For 3D files, try server-generated thumbnail first
          if (is3D) {
            fetch(`${API_URL}/assets/${firstFile.id}/thumbnail`, {
              headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
            })
              .then(r => r.ok ? r.json() : null)
              .then(result => {
                if (result?.url) setThumbnailUrl(result.url);
              })
              .catch(() => {});
          } else if (isNonRenderableImage) {
            // Browser can't paint the raw file (tif/tiff/tga/exr/hdr), but the
            // API produces a browser-paintable thumbnail for it — Pillow WebP
            // for tif/tiff/tga, hdr-converter tone-mapped PNG for exr/hdr — via
            // GET /cogs/{id}/thumbnail. Poll a few times so an async conversion
            // resolves into a real preview instead of a permanent placeholder.
            let attempts = 0;
            const loadConverterThumb = () => {
              fetch(`${API_URL}/assets/${firstFile.id}/thumbnail`, {
                headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
              })
                .then(async r => {
                  if (cancelled) return;
                  if (r.status === 202) {
                    if (attempts++ < 10) setTimeout(loadConverterThumb, 3000);
                    return;
                  }
                  if (!r.ok) return;
                  const result = await r.json().catch(() => null);
                  if (!cancelled && result?.url) setThumbnailUrl(result.url);
                })
                .catch(() => {});
            };
            loadConverterThumb();
            return () => { cancelled = true; };
          } else {
            // Images and videos use the download endpoint
            fetch(`${API_URL}/assets/${firstFile.id}/download`, {
              headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
            })
              .then(r => r.ok ? r.json() : null)
              .then(result => {
                if (result?.download_url) setThumbnailUrl(result.download_url);
              })
              .catch(() => {});
          }
        }
      }
      return () => { cancelled = true; };
    }, [hasFiles, data.previewAssets, selectedFiles]);

  // text-foreground — ReactFlow's stylesheet paints `.react-flow__node-input`
  // (this node's type is "input") with color:#222, which every uncoloured label
  // inherits; in dark mode the card turns dark and that #222 text goes
  // black-on-black. Setting an explicit theme-aware foreground colour on the
  // root neutralises the inherited #222 so text stays readable in both themes
  // (dark on white in light mode, light on dark card in dark mode) — the same
  // effective colour the process node already gets for free.
  const nodeAriaLabelValue = nodeAriaLabel("input", data.label);

  return (
    <div data-testid={`canvas-node-${id}`} role="group" aria-label={nodeAriaLabelValue} className={`shadow-md rounded-lg bg-white text-foreground border-2 ${statusColor} w-[17rem] transition-colors duration-300 relative overflow-hidden`}>
      <NodeStackOverlay nodeKind={(data as any)?.node_kind} requiresStack={(data as any)?.requires_stack} />
      {/* Status indicator */}
      <div className={`absolute top-1 right-1 w-4 h-4 rounded ${getStatusBgColor(data.status)} border-2 border-white shadow-sm transition-colors duration-300 z-10 ${isRunning ? 'animate-pulse' : ''}`}></div>

        {/* Header bar */}
        <div className="px-3 pt-6 pb-2">
          <div className="flex items-center gap-2">
            <div className="rounded-full w-6 h-6 flex-shrink-0 flex items-center justify-center bg-blue-100 text-blue-500">
              <Database className="h-3 w-3" />
            </div>
            <div className="text-xs font-bold truncate">{data.label || "Input"}</div>
          </div>
          {data.nickname && <div className="text-[11px] text-gray-500 truncate mt-0.5 pl-8">{data.nickname}</div>}
        </div>

        {/* Preview area — always visible */}
        <div className="w-full h-36 bg-gray-50 border-t border-b border-gray-100 relative flex items-center justify-center">
            {thumbnailUrl && isVideo ? (
                /* Video preview */
                <>
                    <video
                        src={thumbnailUrl}
                        className={`w-full h-full object-cover ${isRunning ? 'opacity-60' : ''}`}
                        muted
                        loop
                        autoPlay
                        playsInline
                    />
                    {isRunning && (
                        <div className="absolute inset-0 flex items-center justify-center">
                            <div className="flex items-center gap-1.5 bg-black/60 text-white text-xs font-medium px-3 py-1.5 rounded-full">
                                <Loader2 className="w-3 h-3 animate-spin" />
                                Validating...
                            </div>
                        </div>
                    )}
                </>
            ) : thumbnailUrl && is3D ? (
                /* 3D thumbnail — static image with View 3D button */
                <>
                    <img
                        src={thumbnailUrl}
                        alt="3D model"
                        className={`w-full h-full object-cover ${isRunning ? 'opacity-60' : ''}`}
                        onError={() => setThumbnailUrl(null)}
                    />
                    <button
                        onClick={(e) => { e.stopPropagation(); openViewer(); }}
                        className="absolute bottom-1 right-1 bg-indigo-600/90 text-white text-[8px] font-bold px-1.5 py-0.5 rounded z-10 hover:bg-indigo-700"
                    >
                        View 3D
                    </button>
                    {isRunning && (
                        <div className="absolute inset-0 flex items-center justify-center">
                            <div className="flex items-center gap-1.5 bg-black/60 text-white text-xs font-medium px-3 py-1.5 rounded-full">
                                <Loader2 className="w-3 h-3 animate-spin" />
                                Validating...
                            </div>
                        </div>
                    )}
                </>
            ) : thumbnailUrl && isPdf ? (
                /* PDF preview — embed via iframe so it renders inline */
                <>
                    <iframe
                        src={thumbnailUrl}
                        className={`w-full h-full ${isRunning ? 'opacity-60' : ''}`}
                        title="PDF preview"
                    />
                    {isRunning && (
                        <div className="absolute inset-0 flex items-center justify-center">
                            <div className="flex items-center gap-1.5 bg-black/60 text-white text-xs font-medium px-3 py-1.5 rounded-full">
                                <Loader2 className="w-3 h-3 animate-spin" />
                                Validating...
                            </div>
                        </div>
                    )}
                </>
            ) : (isImageFile && (thumbFailed || (isNonRenderableImage && !thumbnailUrl))) ? (
                /* Browser-unpaintable image with no converter thumbnail yet, or
                   an image whose presigned URL failed to load — labeled
                   placeholder instead of a blank tile. Once the converter
                   thumbnail resolves, thumbnailUrl is set and the <img> branch
                   below paints the real preview. */
                <div className="flex flex-col items-center gap-1 text-gray-400">
                    <ImageIcon className="w-8 h-8" />
                    <span className="text-[10px] uppercase font-semibold">
                        {isNonRenderableImage ? firstFileExt : 'Preview unavailable'}
                    </span>
                </div>
            ) : thumbnailUrl && !thumbFailed ? (
                /* Image preview */
                <>
                    <img
                        src={thumbnailUrl}
                        alt="Input file"
                        className={`w-full h-full object-cover ${isRunning ? 'opacity-60' : ''}`}
                        onError={() => setThumbFailed(true)}
                    />
                    {isRunning && (
                        <div className="absolute inset-0 flex items-center justify-center">
                            <div className="flex items-center gap-1.5 bg-black/60 text-white text-xs font-medium px-3 py-1.5 rounded-full">
                                <Loader2 className="w-3 h-3 animate-spin" />
                                Validating...
                            </div>
                        </div>
                    )}
                </>
            ) : textPreview ? (
                /* Text file preview */
                <div className="w-full h-full bg-[#111827] p-2 overflow-hidden" style={{ fontFamily: 'monospace', fontSize: '7px', lineHeight: '1.3', color: '#9ca3af', whiteSpace: 'pre' }}>
                    {textPreview.split('\n').slice(0, 8).join('\n')}
                </div>
            ) : hasFiles ? (
                /* Has files selected but no image/video preview */
                <div className="flex flex-col items-center gap-1.5 text-gray-400">
                    {is3D ? (
                        <Box className="w-8 h-8 text-indigo-400" />
                    ) : isVideo ? (
                        <Film className="w-8 h-8 text-blue-400" />
                    ) : (
                        <FileIcon className="w-6 h-6" />
                    )}
                    <span className="text-[10px]">
                        {selectedFiles!.length} file{selectedFiles!.length > 1 ? 's' : ''} selected
                    </span>
                    {selectedFiles!.length <= 3 && (
                        <div className="text-[9px] text-gray-300 text-center px-2">
                            {selectedFiles!.map((f: any) => f.name).join(', ')}
                        </div>
                    )}
                    {is3D && (
                        <button
                            onClick={(e) => { e.stopPropagation(); openViewer(); }}
                            className="text-[9px] bg-indigo-500 text-white px-2 py-0.5 rounded font-semibold hover:bg-indigo-600"
                        >
                            View 3D
                        </button>
                    )}
                </div>
            ) : (
                /* No files selected */
                <div className="flex flex-col items-center gap-1 text-gray-300">
                    <ImageIcon className="w-6 h-6" />
                    <span className="text-[10px]">No file selected</span>
                </div>
            )}
        </div>

        {/* Footer — file name or description */}
        <div className="px-3 py-1.5 text-[10px] text-gray-400 truncate">
            {hasFiles
                ? (selectedFiles!.length === 1 ? selectedFiles![0].name : `${selectedFiles!.length} files`)
                : (data.description || 'Select a file to begin')
            }
        </div>

        {/* LDF-3 §3 — credit + processing chips, bottom-right corner */}
        <div className="absolute bottom-1 right-1 z-10 pointer-events-none">
            <NodeChips
                cost={data.cost_in_credits}
                gpuVariants={data.gpu_variants}
            />
        </div>

        {outputs.map((output: { name: string; type: string }, index: number) => (
          <Handle
            key={output.name}
            type="source"
            position={Position.Bottom}
            id={output.name}
            title={`Output: ${output.name} (${output.type})`}
            style={{
              left: `${(100 / (outputs.length + 1)) * (index + 1)}%`,
              backgroundColor: getColorForType(output.type),
              width: '14px',
              height: '14px',
              zIndex: 20,
            }}
            isConnectable={isConnectable}
          />
        ))}

        {/* 3D Viewer Modal */}
        {viewerOpen && modelUrl && (
            <Suspense fallback={null}>
                <Model3DViewerModal
                    url={modelUrl}
                    ext={firstFileExt}
                    cogId={hasFiles ? selectedFiles![0].id : undefined}
                    accessToken={getAccessToken()}
                    onClose={() => setViewerOpen(false)}
                    onPreviewSaved={(newUrl) => setThumbnailUrl(newUrl)}
                />
            </Suspense>
        )}
    </div>
  );
});

InputNode.displayName = "InputNode";
