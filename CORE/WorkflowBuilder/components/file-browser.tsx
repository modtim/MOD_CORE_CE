"use client";
import { apiUrl } from "@/lib/runtime-config";
import { authenticatedFetch, getAccessToken, tryRefreshToken } from "@/lib/api-client";

import React, { useState, useEffect, lazy, Suspense } from "react";
import { createPortal } from "react-dom";
import { DataLabelChip } from "./data-label";

// Full 3D viewer modal — lazy loaded, only when user clicks a 3D file
const SplatViewerModal = lazy(() =>
  import('./splat-viewer').then(m => ({ default: m.SplatViewerModal }))
);
const Model3DViewerModal = lazy(() =>
  import(/* webpackChunkName: "model3d" */ './model3d-thumbnail').then(m => ({ default: m.Model3DViewerModal }))
);

// D72 (FIX-1 FILEBROWSER-USE-AUTHENTICATED-FETCH): the upload XHR cannot go
// through authenticatedFetch (progress events need an XHR), so it refreshes
// BEFORE it starts when the access token's JWT `exp` is within 60 s — a token
// minted 4 minutes earlier (the Dashboard hand-off) would otherwise 401
// outright. Pure + exported so the jest test can pin the 60 s decision.
export function looksExpired(token: string | null): boolean {
  if (!token) return false;
  const parts = token.split('.');
  if (parts.length < 2) return false;
  try {
    const payload = JSON.parse(atob(parts[1]));
    const exp = Number(payload?.exp);
    if (!Number.isFinite(exp)) return false;
    return exp * 1000 - Date.now() < 60_000;
  } catch {
    // Undecodable token: assume fresh and let the 401 retry handle it.
    return false;
  }
}

interface FileItem {
  id: string;
  name: string;
  cog_type: string;
  parent_id: string | null;
  // GY.C7 — data classification label (cogs.data_classification, API `Cog`
  // schema). Absent/legacy payloads default to 'internal' in the chip.
  data_classification?: string | null;
}

const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'tif', 'tiff', 'tga', 'exr', 'hdr']);
// Subset of IMAGE_EXTS that a browser can actually paint in an <img> tag.
// tif/tiff/tga/exr/hdr are "images" but no mainstream browser decodes them, so
// pointing an <img> at their presigned URL always fails and reads as a blank/
// missing preview. For those we show a labeled placeholder instead of a doomed
// <img> (and never fire the download request for a preview that can't render).
const BROWSER_IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'avif']);
const VIDEO_EXTS = new Set(['mp4', 'mkv', 'avi', 'mov', 'webm']);
const MODEL3D_EXTS = new Set(['obj', 'fbx', 'glb', 'gltf', 'ply', 'stl', 'dae', 'usd', 'usdz']);
const TEXT_EXTS = new Set(['json', 'csv', 'txt', 'yaml', 'yml', 'xml', 'toml', 'ini', 'cfg', 'conf', 'env', 'md', 'py', 'sh', 'bash', 'js', 'ts', 'jsx', 'tsx', 'html', 'css', 'go', 'rs', 'rb', 'java', 'c', 'cpp', 'h', 'cs', 'php', 'sql', 'log', 'dockerfile', 'makefile']);
const PDF_EXTS = new Set(['pdf']);
const SPLAT_EXTS = new Set(['splat', 'ksplat']);
const POINTCLOUD_EXTS = new Set(['pcd', 'las', 'laz', 'xyz']);
const AUDIO_EXTS = new Set(['mp3', 'wav', 'flac', 'ogg', 'aac', 'm4a']);
// CAD split (FIX-IGES-FOLLOWUPS / FIX-CAD-IGES-COMPLETE): the 3d-thumbnail-gen
// worker and the CAD converter read STEP/STP only (cadquery has no IGES reader),
// so only step/stp take the server THUMBNAIL path. IGES/IGS stay recognised as
// CAD for the preview/recognition side (the Dashboard preview reads them
// client-side through the real OCCT parser); in the picker they show the
// labelled placeholder instead of a doomed thumbnail request.
const CAD_THUMB_EXTS = new Set(['step', 'stp']);
const CAD_PREVIEW_EXTS = new Set(['step', 'stp', 'iges', 'igs']);
const FONT_EXTS = new Set(['ttf', 'otf', 'woff', 'woff2']);
const ARCHIVE_EXTS = new Set(['zip', 'tar', 'gz', 'tgz', 'rar', '7z']);

// Raster images for which the API precomputes a small WebP thumbnail
// (precompute-image-thumbnails). For these the picker requests
// GET /cogs/{id}/thumbnail (tiny, fast) instead of downloading the FULL image,
// falling back to /download only if no thumbnail exists yet. Kept in sync with
// utils/thumbnails.RASTER_THUMBNAIL_EXTS on the API. svg is vector (already
// tiny) and stays on the full-object path.
const THUMBNAILABLE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp']);

// Per-session cache of resolved preview URLs, keyed by cog id. Survives
// component unmount/remount so re-opening the picker (or scrolling a tile out
// and back) does NOT refetch. Presigned URLs live 15 min server-side; we treat
// a cached entry as good for 10 min and re-resolve after that.
const PREVIEW_URL_CACHE = new Map<string, { url: string; exp: number }>();
const PREVIEW_URL_TTL_MS = 10 * 60 * 1000;
function getCachedPreviewUrl(cogId: string): string | null {
  const entry = PREVIEW_URL_CACHE.get(cogId);
  if (entry && entry.exp > Date.now()) return entry.url;
  if (entry) PREVIEW_URL_CACHE.delete(cogId);
  return null;
}
function setCachedPreviewUrl(cogId: string, url: string): void {
  PREVIEW_URL_CACHE.set(cogId, { url, exp: Date.now() + PREVIEW_URL_TTL_MS });
}

const LANG_LABELS: Record<string, string> = {
  py: 'Python', js: 'JavaScript', ts: 'TypeScript', json: 'JSON', yaml: 'YAML', yml: 'YAML',
  xml: 'XML', html: 'HTML', css: 'CSS', sh: 'Shell', go: 'Go', rs: 'Rust', rb: 'Ruby',
  java: 'Java', c: 'C', cpp: 'C++', cs: 'C#', php: 'PHP', sql: 'SQL', csv: 'CSV',
  md: 'Markdown', txt: 'Text', log: 'Log', dockerfile: 'Dockerfile', makefile: 'Makefile',
};

function TextViewerModal({ text, filename, ext, onClose }: { text: string; filename: string; ext: string; onClose: () => void }) {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onClose]);

  let display = text;
  if (ext === 'json') {
    try { display = JSON.stringify(JSON.parse(text), null, 2); } catch (e) { /* raw */ }
  }

  const lines = display.split('\n');
  const lang = LANG_LABELS[ext] || ext.toUpperCase();

  return createPortal(
    <div
      style={{ position: 'fixed', inset: 0, zIndex: 9999, backgroundColor: 'rgba(0,0,0,0.9)', display: 'flex', flexDirection: 'column' }}
      onClick={onClose}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 16px', color: 'white' }} onClick={e => e.stopPropagation()}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span style={{ fontSize: '13px', fontWeight: 600 }}>{filename}</span>
          <span style={{ background: '#374151', color: '#9ca3af', padding: '2px 8px', borderRadius: '4px', fontSize: '11px', fontWeight: 600 }}>{lang}</span>
          <span style={{ color: '#6b7280', fontSize: '11px' }}>{lines.length} lines</span>
        </div>
        <div style={{ display: 'flex', gap: '8px' }}>
          <button
            onClick={() => navigator.clipboard.writeText(text)}
            style={{ background: 'rgba(255,255,255,0.1)', border: 'none', borderRadius: '6px', padding: '4px 12px', cursor: 'pointer', color: '#9ca3af', fontSize: '12px' }}
          >
            Copy
          </button>
          <button
            onClick={onClose}
            style={{ background: 'rgba(255,255,255,0.1)', border: 'none', borderRadius: '6px', padding: '4px 12px', cursor: 'pointer', color: 'white', fontSize: '12px' }}
          >
            Close
          </button>
        </div>
      </div>
      <div style={{ flex: 1, margin: '0 4px 4px 4px', overflow: 'auto' }} onClick={e => e.stopPropagation()}>
        <pre style={{
          background: '#111827', color: '#e5e7eb', padding: '16px', margin: 0, height: '100%',
          fontSize: '13px', lineHeight: '1.5', fontFamily: 'ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace', tabSize: 4,
        }}>
          {lines.map((line, i) => (
            <div key={i}>
              <span style={{ color: '#4b5563', userSelect: 'none', display: 'inline-block', width: '3.5em', textAlign: 'right', marginRight: '1em' }}>{i + 1}</span>
              {line}
            </div>
          ))}
        </pre>
      </div>
    </div>,
    document.body
  );
}

function getFileExt(name: string): string {
  const parts = name.split('.');
  return parts.length > 1 ? parts[parts.length - 1].toLowerCase() : '';
}

function isPreviewableImage(name: string): boolean {
  return IMAGE_EXTS.has(getFileExt(name));
}

// Thumbnail component that lazy-loads a presigned URL for image files
export function FileThumbnail({ item, accessToken }: { item: FileItem; accessToken: string | null }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [viewerOpen, setViewerOpen] = useState(false);
  const [modelUrl, setModelUrl] = useState<string | null>(null);

  const isFolder = item.cog_type?.includes("folder") ?? false;
  const ext = getFileExt(item.name || '');
  const isImage = IMAGE_EXTS.has(ext);
  const isRenderableImage = isImage && BROWSER_IMAGE_EXTS.has(ext);
  const isNonRenderableImage = isImage && !isRenderableImage;
  const isVideo = VIDEO_EXTS.has(ext);
  const is3D = MODEL3D_EXTS.has(ext);
  const isText = TEXT_EXTS.has(ext);
  const isPDF = PDF_EXTS.has(ext);
  const isSplat = SPLAT_EXTS.has(ext);
  const isPointCloud = POINTCLOUD_EXTS.has(ext);
  const isAudio = AUDIO_EXTS.has(ext);
  const isCAD = CAD_PREVIEW_EXTS.has(ext);          // recognition (placeholder/label)
  const isCADThumb = CAD_THUMB_EXTS.has(ext);       // server thumbnail path (step/stp only)
  const isFont = FONT_EXTS.has(ext);
  const isArchive = ARCHIVE_EXTS.has(ext);
  const isPLY = ext === 'ply'; // PLY can be mesh OR splat — show both options
  // True while a converter/worker-produced thumbnail is still being generated
  // for a type whose ONLY preview is that produced image (browser-unpaintable
  // rasters like tif/tga/exr/hdr, and CAD). Drives a spinner instead of a
  // permanent placeholder while the server-side task runs. 3D keeps its own
  // icon-until-ready render, so it isn't seeded here.
  const [thumbPending, setThumbPending] = useState<boolean>(() => (isImage && !isRenderableImage) || isCADThumb);
  const [splatViewerOpen, setSplatViewerOpen] = useState(false);
  const [textPreview, setTextPreview] = useState<string | null>(null);
  const [fullText, setFullText] = useState<string | null>(null);
  const [textViewerOpen, setTextViewerOpen] = useState(false);

  useEffect(() => {
    if (isFolder || (!isImage && !isVideo && !is3D && !isText && !isPDF && !isSplat && !isPointCloud && !isAudio && !isCADThumb) || !accessToken || !item.id) return;

    let cancelled = false;
    let pollTimer: ReturnType<typeof setTimeout> | null = null;
    const API_URL = apiUrl();

    // Server-produced thumbnail path. These types have NO browser-native
    // preview of the raw object — their real preview is an image produced by a
    // converter/worker (or, for tif/tiff/tga, Pillow) and served through
    // GET /cogs/{id}/thumbnail:
    //   - 3D meshes (obj/fbx/glb/stl/step-family via MODEL3D_EXTS) → 3d-thumbnail-gen PNG
    //   - browser-unpaintable rasters (tif/tiff/tga)               → in-process WebP
    //   - HDR / EXR                                                → hdr-converter tone-mapped PNG
    //   - CAD step/stp ONLY (iges/igs are preview-only: no worker reader)  → 3d-thumbnail-gen PNG
    // The endpoint returns 200+{url} when the thumbnail is ready, 202 while the
    // converter task is still running (we poll a few times so an async convert
    // resolves into a real preview instead of a permanent placeholder), or a
    // 200+{status:"unavailable",url:null} when no preview is available (no url →
    // fall through to the type placeholder). 404 is reserved for unknown ids.
    if (is3D || isNonRenderableImage || isCADThumb) {
      const fetchServerThumbnail = (attempt: number) => {
        fetch(`${API_URL}/assets/${item.id}/thumbnail`, {
          headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
        })
          .then(async r => {
            if (cancelled) return;
            if (r.status === 202) {
              // Converter queued/running — retry with backoff before giving up.
              if (attempt < 10) {
                pollTimer = setTimeout(() => fetchServerThumbnail(attempt + 1), 3000);
              } else {
                setThumbPending(false);
              }
              return;
            }
            if (!r.ok) { setThumbPending(false); return; }
            const data = await r.json().catch(() => null);
            if (!cancelled && data?.url) setUrl(data.url);
            if (!cancelled) setThumbPending(false);
          })
          .catch(() => { if (!cancelled) setThumbPending(false); });
      };
      fetchServerThumbnail(0);
      return () => { cancelled = true; if (pollTimer) clearTimeout(pollTimer); };
    }

    // For text files, fetch content via API (avoids CORS issues with presigned URLs)
    if (isText) {
      fetch(`${API_URL}/assets/${item.id}/text-content`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      })
        .then(r => r.ok ? r.text() : null)
        .then(text => {
          if (!cancelled && text) {
            setTextPreview(text.substring(0, 300));
            setFullText(text);
          }
        })
        .catch(() => {});
      return () => { cancelled = true; };
    }

    // For audio files, get the download URL
    if (isAudio) {
      fetch(`${API_URL}/assets/${item.id}/preview-url`, {
        headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
      })
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (!cancelled && data?.url) setUrl(data.url);
        })
        .catch(() => {});
      return () => { cancelled = true; };
    }

    // For point cloud files, get the download URL
    if (isPointCloud) {
      fetch(`${API_URL}/assets/${item.id}/preview-url`, {
        headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
      })
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (!cancelled && data?.url) setUrl(data.url);
        })
        .catch(() => {});
      return () => { cancelled = true; };
    }

    // For splat files, get the download URL for SuperSplat viewer
    if (isSplat) {
      fetch(`${API_URL}/assets/${item.id}/preview-url`, {
        headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
      })
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (!cancelled && data?.url) setUrl(data.url);
        })
        .catch(() => {});
      return () => { cancelled = true; };
    }

    // For PDFs, get the preview URL (no attachment header) for iframe display
    if (isPDF) {
      fetch(`${API_URL}/assets/${item.id}/preview-url`, {
        headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
      })
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (!cancelled && data?.url) setUrl(data.url);
        })
        .catch(() => {});
      return () => { cancelled = true; };
    }

    // Serve any previously-resolved preview URL from the session cache first so
    // re-opening the picker doesn't refetch (thumbnail OR full image).
    const cachedUrl = getCachedPreviewUrl(item.id);
    if (cachedUrl) {
      setUrl(cachedUrl);
      return () => { cancelled = true; };
    }

    const setResolved = (u: string) => {
      if (cancelled) return;
      setCachedPreviewUrl(item.id, u);
      setUrl(u);
    };

    // Full-image fallback (also the primary path for video / svg, which have no
    // precomputed thumbnail).
    const fetchFullImage = () =>
      fetch(`${API_URL}/assets/${item.id}/download`, {
        headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
      })
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (!cancelled && data?.download_url) setResolved(data.download_url);
        })
        .catch(() => {
          if (!cancelled) setFailed(true);
        });

    // For raster images, prefer the precomputed thumbnail (small + fast). The
    // API generates+caches it on first request; on a miss (202/404) fall back
    // to the full image so previews never regress.
    if (THUMBNAILABLE_EXTS.has(ext)) {
      fetch(`${API_URL}/assets/${item.id}/thumbnail`, {
        headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
      })
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (cancelled) return;
          if (data?.url) { setResolved(data.url); return; }
          return fetchFullImage();
        })
        .catch(() => { if (!cancelled) fetchFullImage(); });
      return () => { cancelled = true; };
    }

    // Video / svg / other renderable: full object as before.
    fetchFullImage();

    return () => { cancelled = true; };
  }, [item.id, isFolder, isImage, isVideo, is3D, isText, isPDF, isSplat, isPointCloud, isAudio, isCAD, accessToken]);

  if (isFolder) {
    return <div style={{ fontSize: '32px', lineHeight: 1 }}>📁</div>;
  }

  // PDF preview — render via iframe with the inline preview-url. Must come
  // BEFORE the generic image fallback below, which would otherwise stuff the
  // PDF URL into an <img> tag, fail to load, and fall through to the file icon.
  if (isPDF && url) {
    return (
      <iframe
        src={url}
        title={item.name || 'PDF preview'}
        style={{
          width: '100%',
          height: '80px',
          border: 'none',
          borderRadius: '4px',
          backgroundColor: '#f3f4f6',
        }}
      />
    );
  }

  if (isImage && url && !failed) {
    // A resolved preview URL. For renderable rasters this is the WebP thumbnail
    // (or full image); for browser-unpaintable formats (tif/tga/exr/hdr) it is
    // the converter/Pillow-produced browser-paintable thumbnail — either way an
    // <img> can paint it.
    return (
      <img
        src={url}
        alt={item.name || 'file'}
        style={{
          width: '100%',
          height: '80px',
          objectFit: 'cover',
          borderRadius: '4px',
          backgroundColor: '#f3f4f6',
        }}
        onError={() => setFailed(true)}
      />
    );
  }

  if (isImage && !failed && (isRenderableImage || thumbPending)) {
    // Renderable image still resolving its URL, OR a browser-unpaintable format
    // whose converter thumbnail is still being generated (poll in flight).
    return (
      <div style={{ width: '100%', height: '80px', backgroundColor: '#f3f4f6', borderRadius: '4px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <span style={{ fontSize: '12px', color: '#9ca3af' }}>...</span>
      </div>
    );
  }

  if (isImage) {
    // A browser-unpaintable format with NO converter preview available, or a
    // renderable image whose presigned URL failed to load. Show a labeled
    // placeholder instead of a blank tile so the user still sees the file.
    return (
      <div style={{
        width: '100%', height: '80px', background: '#f3f4f6', borderRadius: '4px',
        display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: '2px',
      }}>
        <span style={{ fontSize: '22px', lineHeight: 1 }}>🖼️</span>
        <span style={{ fontSize: '8px', color: '#9ca3af', fontWeight: 600, textTransform: 'uppercase' }}>
          {isNonRenderableImage ? ext : 'No preview'}
        </span>
      </div>
    );
  }

  if (isVideo && url) {
    return (
      <video
        src={url}
        style={{
          width: '100%',
          height: '80px',
          objectFit: 'cover',
          borderRadius: '4px',
          backgroundColor: '#111',
        }}
        muted
        playsInline
        onMouseEnter={(e) => (e.target as HTMLVideoElement).play().catch(() => {})}
        onMouseLeave={(e) => { const v = e.target as HTMLVideoElement; v.pause(); v.currentTime = 0; }}
      />
    );
  }

  if (isVideo) {
    return <div style={{ fontSize: '32px', lineHeight: 1 }}>🎬</div>;
  }

  if (is3D) {
    const openViewer = (e: React.MouseEvent) => {
      e.stopPropagation();
      // Fetch the actual model URL for the viewer
      if (!modelUrl && accessToken) {
        const API_URL = apiUrl();
        fetch(`${API_URL}/assets/${item.id}/download`, {
          headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
        })
          .then(r => r.ok ? r.json() : null)
          .then(data => {
            if (data?.download_url) {
              setModelUrl(data.download_url);
              setViewerOpen(true);
            }
          })
          .catch(() => {});
      } else if (modelUrl) {
        setViewerOpen(true);
      }
    };

    return (
      <>
        <div style={{ position: 'relative', width: '100%', height: '80px' }}>
          {url ? (
            /* Has server-generated thumbnail — show as static image */
            <img
              src={url}
              alt={item.name || '3D model'}
              style={{
                width: '100%',
                height: '80px',
                objectFit: 'cover',
                borderRadius: '4px',
              }}
              onError={() => setFailed(true)}
            />
          ) : (
            /* No thumbnail yet — show icon */
            <div
              style={{
                width: '100%',
                height: '80px',
                backgroundColor: '#1a1a2e',
                borderRadius: '4px',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                gap: '4px',
              }}
            >
              <span style={{ fontSize: '28px', lineHeight: 1 }}>🧊</span>
              <span style={{ fontSize: '9px', color: '#6366f1', fontWeight: 600, textTransform: 'uppercase' }}>{ext}</span>
            </div>
          )}
          {/* View buttons */}
          <div style={{ position: 'absolute', bottom: '4px', right: '4px', display: 'flex', gap: '3px' }}>
            {isPLY && (
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  // Fetch URL for splat viewer if not already loaded
                  if (!url && accessToken) {
                    const API_URL = apiUrl();
                    fetch(`${API_URL}/assets/${item.id}/preview-url`, {
                      headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
                    }).then(r => r.ok ? r.json() : null).then(data => {
                      if (data?.url) { setUrl(data.url); setSplatViewerOpen(true); }
                    }).catch(() => {});
                  } else {
                    setSplatViewerOpen(true);
                  }
                }}
                style={{
                  backgroundColor: 'rgba(139,92,246,0.9)', color: 'white', border: 'none',
                  borderRadius: '4px', padding: '2px 6px', fontSize: '8px', fontWeight: 600, cursor: 'pointer',
                }}
              >
                Splat
              </button>
            )}
            <button
              onClick={openViewer}
              style={{
                backgroundColor: 'rgba(99,102,241,0.9)', color: 'white', border: 'none',
                borderRadius: '4px', padding: '2px 6px', fontSize: '8px', fontWeight: 600, cursor: 'pointer',
              }}
            >
              3D
            </button>
          </div>
        </div>
        {viewerOpen && modelUrl && (
          <Suspense fallback={null}>
            <Model3DViewerModal
              url={modelUrl}
              ext={ext}
              cogId={item.id}
              accessToken={accessToken}
              onClose={() => setViewerOpen(false)}
              onPreviewSaved={(newUrl: string) => setUrl(newUrl)}
            />
          </Suspense>
        )}
        {splatViewerOpen && (
          <Suspense fallback={null}>
            <SplatViewerModal
              url={url || modelUrl || ''}
              filename={item.name}
              cogId={item.id}
              accessToken={getAccessToken()}
              onClose={() => setSplatViewerOpen(false)}
              onPreviewSaved={(newUrl: string) => setUrl(newUrl)}
            />
          </Suspense>
        )}
      </>
    );
  }

  if (isCAD) {
    // CAD parts: step/stp get a rendered PNG thumbnail from the 3d-thumbnail-gen
    // worker via GET /cogs/{id}/thumbnail (spinner while pending). iges/igs are
    // preview-only here — no worker reader exists — so they show the labelled
    // ⚙️ tile with the ext (the Dashboard preview still opens them via OCCT).
    if (url && !failed) {
      return (
        <img
          src={url}
          alt={item.name || 'CAD model'}
          style={{ width: '100%', height: '80px', objectFit: 'cover', borderRadius: '4px', backgroundColor: '#1c1917' }}
          onError={() => setFailed(true)}
        />
      );
    }
    return (
      <div style={{
        width: '100%', height: '80px', background: '#1c1917', borderRadius: '4px',
        display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: '4px',
      }}>
        <span style={{ fontSize: '24px', lineHeight: 1 }}>⚙️</span>
        <span style={{ fontSize: '9px', color: '#f59e0b', fontWeight: 600, textTransform: 'uppercase' }}>
          {thumbPending ? '…' : ext}
        </span>
      </div>
    );
  }

  if (isAudio) {
    return (
      <div style={{
        width: '100%', height: '80px', background: '#1e1b4b', borderRadius: '4px',
        display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: '2px',
        position: 'relative',
      }}>
        <span style={{ fontSize: '20px', lineHeight: 1 }}>🎵</span>
        <span style={{ fontSize: '8px', color: '#a78bfa', fontWeight: 600, textTransform: 'uppercase' }}>{ext}</span>
        {url && (
          <audio
            src={url}
            controls
            preload="metadata"
            onClick={(e) => e.stopPropagation()}
            style={{ width: '90%', height: '24px', position: 'absolute', bottom: '4px' }}
          />
        )}
      </div>
    );
  }

  if (isPointCloud) {
    return (
      <>
        <div style={{ position: 'relative', width: '100%', height: '80px' }}>
          <div style={{
            width: '100%', height: '80px', background: '#0c1222', borderRadius: '4px',
            display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: '4px',
          }}>
            <span style={{ fontSize: '24px', lineHeight: 1 }}>✦</span>
            <span style={{ fontSize: '9px', color: '#60a5fa', fontWeight: 600, textTransform: 'uppercase' }}>{ext}</span>
          </div>
          {url && (
            <button
              onClick={(e) => { e.stopPropagation(); setViewerOpen(true); }}
              style={{
                position: 'absolute', bottom: '4px', right: '4px',
                backgroundColor: 'rgba(59,130,246,0.9)', color: 'white', border: 'none',
                borderRadius: '4px', padding: '2px 6px', fontSize: '9px', fontWeight: 600, cursor: 'pointer',
              }}
            >
              View
            </button>
          )}
        </div>
        {viewerOpen && url && (
          <Suspense fallback={null}>
            <Model3DViewerModal
              url={url}
              ext={ext}
              cogId={item.id}
              accessToken={accessToken}
              onClose={() => setViewerOpen(false)}
              onPreviewSaved={(newUrl: string) => setUrl(newUrl)}
            />
          </Suspense>
        )}
      </>
    );
  }

  if (isSplat) {
    return (
      <>
        <div style={{ position: 'relative', width: '100%', height: '80px' }}>
          <div style={{
            width: '100%', height: '80px', background: '#0f172a', borderRadius: '4px',
            display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: '4px',
          }}>
            <span style={{ fontSize: '24px', lineHeight: 1 }}>✨</span>
            <span style={{ fontSize: '9px', color: '#a78bfa', fontWeight: 600 }}>SPLAT</span>
          </div>
          {url && (
            <button
              onClick={(e) => { e.stopPropagation(); setViewerOpen(true); }}
              aria-label="View splat"
              style={{
                position: 'absolute', bottom: '4px', right: '4px',
                backgroundColor: 'rgba(139,92,246,0.9)', color: 'white', border: 'none',
                borderRadius: '4px', padding: '2px 6px', fontSize: '9px', fontWeight: 600, cursor: 'pointer',
              }}
            >
              View
            </button>
          )}
        </div>
        {viewerOpen && url && (
          <Suspense fallback={null}>
            <SplatViewerModal
              url={url || ''}
              filename={item.name}
              cogId={item.id}
              accessToken={getAccessToken()}
              onClose={() => setViewerOpen(false)}
              onPreviewSaved={(newUrl: string) => setUrl(newUrl)}
            />
          </Suspense>
        )}
      </>
    );
  }

  if (isPDF) {
    return (
      <>
        <div style={{ position: 'relative', width: '100%', height: '80px' }}>
          <div style={{
            width: '100%', height: '80px', background: '#fef2f2', borderRadius: '4px',
            display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: '4px',
          }}>
            <span style={{ fontSize: '28px', lineHeight: 1 }}>📕</span>
            <span style={{ fontSize: '9px', color: '#dc2626', fontWeight: 600 }}>PDF</span>
          </div>
          {url && (
            <button
              onClick={(e) => { e.stopPropagation(); setViewerOpen(true); }}
              style={{
                position: 'absolute', bottom: '4px', right: '4px',
                backgroundColor: 'rgba(220,38,38,0.9)', color: 'white', border: 'none',
                borderRadius: '4px', padding: '2px 6px', fontSize: '9px', fontWeight: 600, cursor: 'pointer',
              }}
            >
              View
            </button>
          )}
        </div>
        {viewerOpen && url && createPortal(
          <div
            style={{ position: 'fixed', inset: 0, zIndex: 9999, backgroundColor: 'rgba(0,0,0,0.9)', display: 'flex', flexDirection: 'column' }}
            onClick={() => setViewerOpen(false)}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 16px', color: 'white' }} onClick={e => e.stopPropagation()}>
              <span style={{ fontSize: '13px', fontWeight: 600 }}>{item.name}</span>
              <button onClick={() => setViewerOpen(false)} style={{ background: 'rgba(255,255,255,0.1)', border: 'none', borderRadius: '6px', padding: '4px 12px', cursor: 'pointer', color: 'white', fontSize: '12px' }}>Close</button>
            </div>
            <div style={{ flex: 1, margin: '0 4px 4px 4px' }} onClick={e => e.stopPropagation()}>
              <iframe src={`${apiUrl()}/assets/${item.id}/pdf-view?token=${encodeURIComponent(getAccessToken() || '')}`} style={{ width: '100%', height: '100%', border: 'none', borderRadius: '4px', background: 'white' }} />
            </div>
          </div>,
          document.body
        )}
      </>
    );
  }

  if (isText) {
    const preview = textPreview || '';
    const lines = preview.split('\n').slice(0, 5).join('\n');

    const openTextViewer = (e: React.MouseEvent) => {
      e.stopPropagation();
      if (fullText) {
        setTextViewerOpen(true);
      } else if (accessToken) {
        // Fetch full text if not loaded yet
        const API_URL = apiUrl();
        fetch(`${API_URL}/assets/${item.id}/text-content`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        })
          .then(r => r.ok ? r.text() : null)
          .then(text => {
            if (text) {
              setFullText(text);
              setTextViewerOpen(true);
            }
          })
          .catch(() => {});
      }
    };

    return (
      <>
        <div style={{ position: 'relative', width: '100%', height: '80px' }}>
          <div style={{
            width: '100%', height: '80px', backgroundColor: '#111827', borderRadius: '4px',
            padding: '6px', overflow: 'hidden', fontFamily: 'monospace', fontSize: '7px',
            lineHeight: '1.3', color: '#9ca3af', whiteSpace: 'pre',
          }}>
            {lines || '...'}
          </div>
          <button
            onClick={openTextViewer}
            style={{
              position: 'absolute', bottom: '4px', right: '4px',
              backgroundColor: 'rgba(99,102,241,0.9)', color: 'white', border: 'none',
              borderRadius: '4px', padding: '2px 6px', fontSize: '9px', fontWeight: 600, cursor: 'pointer',
            }}
          >
            View
          </button>
        </div>
        {textViewerOpen && fullText && (
          <TextViewerModal
            text={fullText}
            filename={item.name || 'file'}
            ext={ext}
            onClose={() => setTextViewerOpen(false)}
          />
        )}
      </>
    );
  }

  if (isFont) {
    return (
      <div style={{
        width: '100%', height: '80px', background: '#1a1a2e', borderRadius: '4px',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: '28px', color: '#e5e7eb', fontFamily: 'serif',
      }}>
        Aa
      </div>
    );
  }

  if (isArchive) {
    return (
      <div style={{
        width: '100%', height: '80px', background: '#1c1917', borderRadius: '4px',
        display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: '4px',
      }}>
        <span style={{ fontSize: '24px', lineHeight: 1 }}>📦</span>
        <span style={{ fontSize: '9px', color: '#a3a3a3', fontWeight: 600, textTransform: 'uppercase' }}>{ext}</span>
      </div>
    );
  }

  return <div style={{ fontSize: '32px', lineHeight: 1 }}>📄</div>;
}

interface FileBrowserProps {
  isOpen: boolean;
  onClose: () => void;
  accessToken: string | null;
  onFileSelect?: (files: FileItem[]) => void;
  inputType?: string;
  allowMultiple?: boolean;
  nodeId?: string;
  isMetadata?: boolean;
}

export function FileBrowser({ isOpen, onClose, accessToken, onFileSelect, inputType, allowMultiple = false, nodeId, isMetadata = false }: FileBrowserProps) {
  const [items, setItems] = useState<FileItem[]>([]);
  const [currentFolder, setCurrentFolder] = useState<string | null>(null);
  const [breadcrumb, setBreadcrumb] = useState<Array<{ id: string; name: string }>>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [selectedFiles, setSelectedFiles] = useState<FileItem[]>([]);
  // Range-select anchor: id of the last item selected with a plain / ctrl click.
  // A subsequent shift+click selects the inclusive range from this anchor to the
  // clicked item (file-explorer semantics).
  const [anchorId, setAnchorId] = useState<string | null>(null);
  const [uploadProgress, setUploadProgress] = useState<number>(0);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [currentUploadFile, setCurrentUploadFile] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<'grid' | 'list'>(() => {
    if (typeof window !== 'undefined') {
      return (localStorage.getItem('wfbViewMode') as 'grid' | 'list') || 'grid';
    }
    return 'grid';
  });
  const [uploadQueue, setUploadQueue] = useState<number>(0);
  const [uploadedCount, setUploadedCount] = useState<number>(0);
  const [isCreatingFolder, setIsCreatingFolder] = useState(false);
  const [newFolderName, setNewFolderName] = useState('');
  const [deletingItemId, setDeletingItemId] = useState<string | null>(null);

  // One AbortController per load: a page that arrives after the folder changed,
  // the modal closed, or the component unmounted belongs to the old context and
  // must never write state.
  let activeLoadSignal: AbortController | null = null;

  // GOLDENFIX (2026-11-03): fetch's RequestInit wants an AbortSignal, not an
  // AbortController — the paging block used to pass the controller itself
  // (`signal,` shorthand at the fetch below), which RequestInit rejects with
  // "Failed to convert value to 'AbortSignal'", so EVERY loadFiles() threw and
  // the catch wiped the listing. Hand fetch a signal ONLY when the runtime
  // really gives us an AbortSignal; otherwise degrade to no-signal (the
  // isStale() page-arrival guards below still drop superseded pages).
  const usableAbortSignal = (controller: AbortController): AbortSignal | null =>
    typeof AbortSignal !== 'undefined' && controller.signal instanceof AbortSignal
      ? controller.signal
      : null;

  // Load files when modal opens or folder changes
  useEffect(() => {
    if (isOpen && accessToken) {
      loadFiles(currentFolder);
    }
    return () => {
      // Unmount / folder switch: any page still in flight belongs to the old
      // context, so cancel it before it can write state.
      activeLoadSignal?.abort();
      activeLoadSignal = null;
    };
  }, [isOpen, currentFolder, accessToken]);

  // Listen for workflow completion events to refresh file list
  useEffect(() => {
    const handleWorkflowCompleted = () => {
      // Refresh the current folder to show any new output files
      if (accessToken) {
        loadFiles(currentFolder);
      }
    };

    window.addEventListener('workflow-completed', handleWorkflowCompleted);
    return () => {
      window.removeEventListener('workflow-completed', handleWorkflowCompleted);
    };
  }, [accessToken, currentFolder]);

  const loadFiles = async (parentId: string | null) => {
    // A new load supersedes any load still in flight (folder switch while a
    // workflow-completed refresh is mid-paging): abort the old one first so its
    // late pages can't land in this folder.
    activeLoadSignal?.abort();
    const signal = new AbortController();
    activeLoadSignal = signal;
    // Stale = this load was aborted, or a newer load already replaced it.
    const isStale = () => signal !== activeLoadSignal || signal.aborted;
    // Only a real AbortSignal may reach fetch; on a runtime without one this
    // stays null and the fetch below omits the key (isStale() still guards
    // pages that arrive after the folder/modal changed).
    const sig = usableAbortSignal(signal);
    setIsLoading(true);
    try {
      const API_URL = apiUrl();
      const params = new URLSearchParams();
      if (parentId) params.set('parent_id', parentId);
      // In picker mode (onFileSelect present) the user is choosing a file to
      // USE — hide contentless FILE cogs (orphaned/failed uploads) so they
      // can't select one that resolves to nothing and fails the run deep in
      // the Input Validator. Pure browse/manage mode (no onFileSelect) still
      // shows everything so the user can see + delete a broken file.
      if (onFileSelect) params.set('selectable_inputs_only', 'true');
      const baseQs = params.toString();

      // GET /assets/ defaults to limit=50 (CORE/API/routers/cogs/assets_cogs_files.py:194-207)
      // and returns a BARE list — no total, no next-page indicator — so one fetch
      // silently hides every child past the 50-item cut (a folder with >50 entries
      // looked "empty"/"not found" to the user). Page through it with `offset`:
      // a page shorter than PAGE_LIMIT is the unambiguous terminator for a bare-list
      // endpoint. The cap keeps a broken/hostile server from spinning the browser
      // forever. Filter/sort below still runs on the FULL concatenated list.
      const PAGE_LIMIT = 50;
      const MAX_PAGES = 20;
      const data: any[] = [];
      let truncated = false;
      for (let page = 0; page < MAX_PAGES; page++) {
        const pageParams = new URLSearchParams(baseQs);
        pageParams.set('offset', String(page * PAGE_LIMIT));
        const qs = pageParams.toString();
        const url = qs ? `${API_URL}/assets/?${qs}` : `${API_URL}/assets/`;

        // D72 (FIX-1 FILEBROWSER-USE-AUTHENTICATED-FETCH): the listing used a raw
        // fetch carrying the in-memory accessToken prop, so at the 5-minute token
        // boundary it 401'ed with no refresh and the folder looked empty.
        // authenticatedFetch keeps our explicit header on the first try and, on a
        // 401, refreshes once and retries with the new token.
        const response = await authenticatedFetch(url, {
          ...(sig ? { signal: sig } : {}),
          headers: {
            Authorization: `Bearer ${accessToken}`,
            Accept: "application/json",
          },
        });

        // The folder may have changed / the modal closed while this page was in
        // flight — a page that arrives for the old context must not touch state.
        if (isStale()) return;
        if (!response.ok) throw new Error("Failed to fetch files");
        const pageData = await response.json();
        if (isStale()) return;
        data.push(...pageData);
        if (pageData.length < PAGE_LIMIT) break;
        // Last page we were willing to take was FULL: more children exist but we
        // stopped at the cap, so the listing is knowingly incomplete.
        if (page === MAX_PAGES - 1) truncated = true;
      }

      if (truncated) {
        console.warn(
          `File browser listing truncated at ${MAX_PAGES} pages (${data.length} items); ` +
          `this folder has more children than the paging cap fetches.`
        );
      }

      // Filter by file type if specified
      const shouldFilter = isMetadata || inputType;
      const filteredData = shouldFilter
        ? data.filter((item: any) => {
            const isFolder = item.cog_type.includes("folder");
            if (isFolder) return true;

            const extension = item.name.split('.').pop()?.toLowerCase();

            // Get allowed extensions based on context
            const allowedExtensions = getAllowedExtensions();

            // Unknown / generic input type (e.g. 'resource', 'file', 'pdf',
            // anything not in typeMap) returns an empty allowlist — treat
            // that as a wildcard and show all files. Without this a Resource
            // Input's file picker shows only folders. Metadata mode keeps
            // its strict allowlist (always non-empty for the metadata branch).
            if (!isMetadata && allowedExtensions.length === 0) return true;
            return extension ? allowedExtensions.includes(extension) : false;
          })
        : data;

      setItems(filteredData);
    } catch (error) {
      // An aborted fetch lands here too — a cancelled load stays silent.
      if (!isStale()) {
        console.error("Error loading files:", error);
        // GOLDENFIX: never wipe the listing on a failed reload — the previous
        // list is the truth we have (a failed FIRST load is still empty, since
        // `items` starts []). Wiping hid the just-created folder row.
      }
    } finally {
      if (!isStale()) setIsLoading(false);
    }
  };

  const handleItemClick = (item: FileItem, event?: React.MouseEvent) => {
    const isFolder = item.cog_type.includes("folder");
    if (isFolder) {
      setAnchorId(null);
      setCurrentFolder(item.id);
      setBreadcrumb([...breadcrumb, { id: item.id, name: item.name }]);
      return;
    }

    // Single-select mode: modifiers are irrelevant — one file at a time.
    if (!allowMultiple) {
      setSelectedFiles([item]);
      setAnchorId(item.id);
      return;
    }

    const isShift = event?.shiftKey ?? false;
    const isCtrl = (event?.metaKey || event?.ctrlKey) ?? false;

    // Shift+click: select the inclusive contiguous range of FILES between the
    // anchor and the clicked item (folders in the range are skipped). The range
    // is unioned with the existing selection so it composes with prior
    // ctrl/plain clicks, and never deselects what the user already picked.
    if (isShift && anchorId) {
      const anchorIdx = items.findIndex(i => i.id === anchorId);
      const clickIdx = items.findIndex(i => i.id === item.id);
      if (anchorIdx !== -1 && clickIdx !== -1) {
        // Avoid the browser's native text selection from shift-clicking.
        if (typeof window !== 'undefined') window.getSelection()?.removeAllRanges();
        const [lo, hi] = anchorIdx <= clickIdx ? [anchorIdx, clickIdx] : [clickIdx, anchorIdx];
        const rangeItems = items
          .slice(lo, hi + 1)
          .filter(i => !i.cog_type.includes("folder"));
        setSelectedFiles(prev => {
          const byId = new Map(prev.map(f => [f.id, f]));
          for (const r of rangeItems) byId.set(r.id, r);
          return Array.from(byId.values());
        });
        // Anchor stays put so the user can grow/shrink the range.
        return;
      }
    }

    // Plain click or ctrl/cmd+click: toggle this item and move the anchor here.
    const isSelected = selectedFiles.some(f => f.id === item.id);
    if (isSelected) {
      setSelectedFiles(selectedFiles.filter(f => f.id !== item.id));
    } else {
      setSelectedFiles([...selectedFiles, item]);
    }
    setAnchorId(item.id);
  };

  const handleBreadcrumbClick = (index: number) => {
    if (index === -1) {
      // Go to root
      setCurrentFolder(null);
      setBreadcrumb([]);
    } else {
      // Go to specific folder in breadcrumb
      const newBreadcrumb = breadcrumb.slice(0, index + 1);
      setBreadcrumb(newBreadcrumb);
      setCurrentFolder(newBreadcrumb[newBreadcrumb.length - 1].id);
    }
  };

  const handleSelectFiles = () => {
    if (selectedFiles.length > 0 && onFileSelect) {
      onFileSelect(selectedFiles);
      onClose();
      setSelectedFiles([]);
    }
  };

  // Get allowed file extensions based on input type
  const getAllowedExtensions = () => {
    // For metadata, allow common metadata formats
    if (isMetadata) {
      return ['json', 'xml', 'csv', 'txt', 'yaml', 'yml', 'toml', 'ini', 'md'];
    }

    if (!inputType) return [];

    const typeMap: { [key: string]: string[] } = {
      image: ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'tiff', 'webp', 'svg'],
      video: ['mp4', 'avi', 'mov', 'wmv', 'flv', 'mkv', 'webm'],
      model: ['obj', 'fbx', 'gltf', 'glb', 'usd', 'usda', 'usdc', 'blend', 'dae', 'stl', 'ply'],
      mesh: ['obj', 'fbx', 'gltf', 'glb', 'usd', 'usda', 'usdc', 'blend', 'dae', 'stl', 'ply'],
      text: ['txt', 'text', 'md', 'csv', 'json', 'xml', 'log', 'yaml', 'yml', 'ini', 'cfg', 'conf'],
      audio: ['mp3', 'wav', 'ogg', 'flac', 'aac', 'm4a', 'wma'],
    };
    return typeMap[inputType.toLowerCase()] || [];
  };

  // Get accept attribute for file input
  const getAcceptAttribute = () => {
    const extensions = getAllowedExtensions();
    if (extensions.length === 0) return undefined;
    return extensions.map(ext => `.${ext}`).join(',');
  };

  // Validate file type
  const validateFileType = (fileName: string): boolean => {
    const extensions = getAllowedExtensions();

    // If no extensions are specified, allow all files
    if (extensions.length === 0) return true;

    const fileExtension = fileName.split('.').pop()?.toLowerCase();
    return fileExtension ? extensions.includes(fileExtension) : false;
  };

  // Upload a single file
  //
  // D72 (FIX-1 FILEBROWSER-USE-AUTHENTICATED-FETCH): the upload cannot go through
  // authenticatedFetch (progress events need an XHR), so it keeps the XHR but now
  // (a) refreshes the token BEFORE the request starts when the JWT `exp` is within
  // 60 s, and (b) retries the XHR exactly once on a 401 with the refreshed token.
  // The old code sent the in-memory `accessToken` prop and gave up on 401, so an
  // upload started seconds past the 5-minute token lifetime failed outright.
  const uploadSingleFile = async (file: File, onComplete: (success: boolean) => void) => {
    if (!accessToken) return;

    setCurrentUploadFile(file.name);

    const API_URL = apiUrl();

    // Create form data
    const formData = new FormData();
    formData.append('file', file);
    formData.append('name', file.name);
    if (currentFolder) {
      formData.append('parent_id', currentFolder);
    }

    // Refresh BEFORE the XHR starts when the token is about to expire, so the
    // upload never opens with a token that is already dead.
    let token = accessToken;
    if (looksExpired(token)) {
      const fresh = await tryRefreshToken();
      if (fresh) token = fresh;
    }

    const sendUploadXhr = (attempt: number, bearer: string) => {
      // Use XMLHttpRequest for progress tracking
      const xhr = new XMLHttpRequest();

      xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) {
          const percentComplete = (e.loaded / e.total) * 100;
          setUploadProgress(percentComplete);
        }
      });

      xhr.addEventListener('load', () => {
        if (xhr.status === 201) {
          // Upload successful
          setUploadedCount(prev => prev + 1);
          onComplete(true);
          return;
        }

        // D72: a 401 means the token went stale mid-upload — refresh once and
        // retry the SAME file exactly once, never a second time.
        if (xhr.status === 401 && attempt === 0) {
          tryRefreshToken().then((t) => {
            if (!t) {
              onComplete(false);
              return;
            }
            sendUploadXhr(1, t);
          });
          return;
        }

        // Upload failed - parse error message
        let errorMessage = 'Upload failed';
        try {
          const response = JSON.parse(xhr.responseText);
          errorMessage = response.detail || errorMessage;
        } catch (e) {
          if (xhr.status === 403) {
            errorMessage = "You don't have permission to upload files to this folder";
          } else if (xhr.status === 400) {
            errorMessage = "Invalid upload request. Files must be uploaded to a folder.";
          }
        }
        setUploadError(`${file.name}: ${errorMessage}`);
        console.error('Upload failed:', errorMessage);
        onComplete(false);
      });

      xhr.addEventListener('error', () => {
        setUploadError(`${file.name}: Network error during upload`);
        console.error('Upload error');
        onComplete(false);
      });

      xhr.open('POST', `${API_URL}/assets/upload-file`);
      xhr.setRequestHeader('Authorization', `Bearer ${bearer}`);
      xhr.send(formData);
    };

    sendUploadXhr(0, token);
  };

  const handleFileUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files;
    if (!files || files.length === 0 || !accessToken) return;

    // Clear previous errors
    setUploadError(null);

    // Convert FileList to array
    const fileArray = Array.from(files);

    // Validate all file types
    const invalidFiles = fileArray.filter(file => !validateFileType(file.name));
    if (invalidFiles.length > 0) {
      const extensions = getAllowedExtensions();
      setUploadError(`Invalid file type(s): ${invalidFiles.map(f => f.name).join(', ')}. Allowed types: ${extensions.join(', ')}`);
      event.target.value = '';
      return;
    }

    // Set up upload state
    setIsUploading(true);
    setUploadQueue(fileArray.length);
    setUploadedCount(0);
    setUploadProgress(0);

    // Upload files sequentially
    for (let i = 0; i < fileArray.length; i++) {
      await new Promise<void>((resolve) => {
        uploadSingleFile(fileArray[i], (success) => {
          setUploadProgress(0); // Reset progress for next file
          resolve();
        });
      });
    }

    // All uploads complete
    setIsUploading(false);
    setCurrentUploadFile(null);
    setUploadQueue(0);
    setUploadedCount(0);

    // Reload files to show the new uploads
    loadFiles(currentFolder);

    // Clear the file input so the same file can be uploaded again
    event.target.value = '';
  };

  const handleCreateFolder = async () => {
    if (!newFolderName.trim() || !accessToken) return;

    try {
      const API_URL = apiUrl();
      const response = await authenticatedFetch(`${API_URL}/assets/`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${accessToken}`,
        },
        body: JSON.stringify({
          name: newFolderName.trim(),
          cog_type: currentFolder ? 'asset_folder' : 'master_folder',
          parent_id: currentFolder ? parseInt(currentFolder) : null
        })
      });

      if (!response.ok) {
        const error = await response.json();
        setUploadError(error.detail || 'Failed to create folder');
        return;
      }

      // Success - reload files and close dialog
      setIsCreatingFolder(false);
      setNewFolderName('');
      loadFiles(currentFolder);
    } catch (error) {
      console.error('Error creating folder:', error);
      setUploadError('Failed to create folder');
    }
  };

  const handleDeleteItem = async (itemId: string, itemName: string, event: React.MouseEvent) => {
    event.stopPropagation(); // Prevent folder navigation or file selection

    if (!confirm(`Are you sure you want to delete "${itemName}"?`)) {
      return;
    }

    setDeletingItemId(itemId);

    try {
      const API_URL = apiUrl();
      const response = await authenticatedFetch(`${API_URL}/assets/${itemId}`, {
        method: 'DELETE',
        headers: {
          'Authorization': `Bearer ${accessToken}`,
        },
      });

      if (!response.ok) {
        const error = await response.json();
        setUploadError(error.detail || 'Failed to delete item');
        return;
      }

      // Success - reload files
      loadFiles(currentFolder);
    } catch (error) {
      console.error('Error deleting item:', error);
      setUploadError('Failed to delete item');
    } finally {
      setDeletingItemId(null);
    }
  };

  if (!isOpen) return null;

  return (
    <div
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        backgroundColor: 'rgba(0, 0, 0, 0.5)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 50
      }}
      onClick={onClose}
    >
      <div
        className="bg-white"
        role="dialog"
        aria-modal="true"
        aria-labelledby="file-browser-title"
        style={{
          borderRadius: '8px',
          padding: '24px',
          maxWidth: '800px',
          width: '90%',
          maxHeight: '80vh',
          display: 'flex',
          flexDirection: 'column'
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
          <h2 id="file-browser-title" style={{ fontSize: '20px', fontWeight: 600 }}>
            {isMetadata ? 'Select Metadata Files' : 'Select Files'}
          </h2>
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
            <input
              type="file"
              id="file-upload-input"
              onChange={handleFileUpload}
              accept={getAcceptAttribute()}
              multiple={allowMultiple}
              style={{ display: 'none' }}
            />
            <button
              onClick={() => setIsCreatingFolder(true)}
              className="bg-white"
              style={{
                padding: '6px 12px',
                border: '1px solid #4f46e5',
                borderRadius: '4px',
                color: '#4f46e5',
                cursor: 'pointer',
                fontSize: '14px',
                fontWeight: 500
              }}
            >
              📁+ New Folder
            </button>
            <button
              onClick={() => {
                const input = document.getElementById('file-upload-input') as HTMLInputElement;
                if (input) {
                  input.value = ''; // Clear before opening to allow re-upload of same file
                  input.click();
                }
              }}
              disabled={isUploading || !currentFolder}
              // D72 (FIX-1 FILEBROWSER-USE-AUTHENTICATED-FETCH): a disabled chooser
              // used to say nothing — the user read it as a broken picker. The tooltip
              // now names the reason, and a small helper line below repeats it visibly
              // (tooltips are invisible to a11y tooling and to touch-only visitors).
              title={!currentFolder ? 'Open or create a folder first.' : ''}
              className="bg-white"
              style={{
                padding: '6px 12px',
                border: '1px solid #4f46e5',
                borderRadius: '4px',
                color: '#4f46e5',
                cursor: (isUploading || !currentFolder) ? 'not-allowed' : 'pointer',
                fontSize: '14px',
                fontWeight: 500,
                opacity: (isUploading || !currentFolder) ? 0.5 : 1
              }}
            >
              {isUploading ? 'Uploading...' : '📤 Upload'}
            </button>
            {/* D72: visible reason for the disabled chooser (see the title above). */}
            {!currentFolder ? (
              <span
                style={{
                  fontSize: '12px',
                  color: '#6b7280',
                  fontStyle: 'italic',
                  marginTop: '4px'
                }}
              >
                Open or create a folder first.
              </span>
            ) : null}
            <button
              onClick={onClose}
              aria-label="Close file browser"
              style={{
                background: 'none',
                border: 'none',
                fontSize: '24px',
                cursor: 'pointer',
                padding: '4px 8px'
              }}
            >
              ×
            </button>
          </div>
        </div>

        {/* Breadcrumb + View Toggle */}
        <div className="text-gray-600" style={{ marginBottom: '16px', fontSize: '14px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div>
          <button
            onClick={() => handleBreadcrumbClick(-1)}
            style={{
              background: 'none',
              border: 'none',
              color: '#4f46e5',
              cursor: 'pointer',
              padding: '4px 8px',
              textDecoration: breadcrumb.length === 0 ? 'none' : 'underline'
            }}
          >
            Home
          </button>
          {breadcrumb.map((folder, index) => (
            <span key={folder.id}>
              <span style={{ margin: '0 4px' }}>/</span>
              <button
                onClick={() => handleBreadcrumbClick(index)}
                style={{
                  background: 'none',
                  border: 'none',
                  color: '#4f46e5',
                  cursor: 'pointer',
                  padding: '4px 8px',
                  textDecoration: index === breadcrumb.length - 1 ? 'none' : 'underline'
                }}
              >
                {folder.name}
              </button>
            </span>
          ))}
          </div>
          {/* View mode toggle */}
          <div style={{ display: 'flex', gap: '2px', border: '1px solid #e5e7eb', borderRadius: '4px', padding: '2px' }}>
            <button
              onClick={() => { setViewMode('grid'); localStorage.setItem('wfbViewMode', 'grid'); }}
              style={{
                background: viewMode === 'grid' ? '#4f46e5' : 'transparent',
                color: viewMode === 'grid' ? 'white' : '#666',
                border: 'none', borderRadius: '3px', padding: '4px 6px', cursor: 'pointer',
                display: 'flex', alignItems: 'center',
              }}
              title="Grid view"
              aria-pressed={viewMode === 'grid'}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>
            </button>
            <button
              onClick={() => { setViewMode('list'); localStorage.setItem('wfbViewMode', 'list'); }}
              style={{
                background: viewMode === 'list' ? '#4f46e5' : 'transparent',
                color: viewMode === 'list' ? 'white' : '#666',
                border: 'none', borderRadius: '3px', padding: '4px 6px', cursor: 'pointer',
                display: 'flex', alignItems: 'center',
              }}
              title="List view"
              aria-pressed={viewMode === 'list'}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>
            </button>
          </div>
        </div>

        {/* Upload Error Message */}
        {uploadError && (
          <div style={{
            marginBottom: '16px',
            padding: '12px',
            backgroundColor: '#fee2e2',
            border: '1px solid #ef4444',
            borderRadius: '4px',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center'
          }}>
            <span style={{ fontSize: '14px', color: '#991b1b' }}>{uploadError}</span>
            <button
              onClick={() => setUploadError(null)}
              aria-label="Dismiss upload error"
              style={{
                background: 'none',
                border: 'none',
                fontSize: '18px',
                cursor: 'pointer',
                color: '#991b1b',
                padding: '0 4px'
              }}
            >
              ×
            </button>
          </div>
        )}

        {/* Create Folder Dialog */}
        {isCreatingFolder && (
          <div style={{
            marginBottom: '16px',
            padding: '16px',
            backgroundColor: '#f9fafb',
            border: '1px solid #d1d5db',
            borderRadius: '4px',
            color: '#111827'
          }}>
            <h3 style={{ fontSize: '16px', fontWeight: 600, marginBottom: '12px', color: '#111827' }}>Create New Folder</h3>
            <input
              type="text"
              value={newFolderName}
              onChange={(e) => setNewFolderName(e.target.value)}
              onKeyPress={(e) => {
                if (e.key === 'Enter') handleCreateFolder();
              }}
              placeholder="Enter folder name"
              autoFocus
              style={{
                width: '100%',
                padding: '8px',
                border: '1px solid #d1d5db',
                borderRadius: '4px',
                fontSize: '14px',
                marginBottom: '12px',
                color: '#111827',
                backgroundColor: '#ffffff'
              }}
            />
            <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
              <button
                onClick={() => {
                  setIsCreatingFolder(false);
                  setNewFolderName('');
                }}
                style={{
                  padding: '6px 12px',
                  border: '1px solid #d1d5db',
                  borderRadius: '4px',
                  cursor: 'pointer',
                  fontSize: '14px',
                  backgroundColor: '#ffffff',
                  color: '#374151'
                }}
              >
                Cancel
              </button>
              <button
                onClick={handleCreateFolder}
                disabled={!newFolderName.trim()}
                style={{
                  padding: '6px 12px',
                  border: 'none',
                  borderRadius: '4px',
                  background: newFolderName.trim() ? '#4f46e5' : '#cbd5e1',
                  color: '#ffffff',
                  cursor: newFolderName.trim() ? 'pointer' : 'not-allowed',
                  fontSize: '14px',
                  fontWeight: 500
                }}
              >
                Create
              </button>
            </div>
          </div>
        )}

        {/* Upload Progress Bar */}
        {isUploading && (
          <div style={{ marginBottom: '16px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
              <div className="text-gray-600" style={{ fontSize: '14px' }}>
                {uploadQueue > 1 ? (
                  <>
                    <span>Uploading {uploadedCount + 1} of {uploadQueue}...</span>
                    {currentUploadFile && (
                      <div className="text-gray-500" style={{ fontSize: '12px', marginTop: '2px' }}>{currentUploadFile}</div>
                    )}
                  </>
                ) : (
                  <span>Uploading{currentUploadFile ? `: ${currentUploadFile}` : '...'}</span>
                )}
              </div>
              <span className="text-gray-600" style={{ fontSize: '14px', fontWeight: 500 }}>{Math.round(uploadProgress)}%</span>
            </div>
            <div style={{ width: '100%', height: '8px', backgroundColor: '#e5e7eb', borderRadius: '4px', overflow: 'hidden' }}>
              <div
                style={{
                  width: `${uploadProgress}%`,
                  height: '100%',
                  backgroundColor: '#4f46e5',
                  transition: 'width 0.3s ease'
                }}
              />
            </div>
          </div>
        )}

        {/* File List */}
        <div style={{ flex: 1, overflowY: 'auto', border: '1px solid #e5e7eb', borderRadius: '4px', padding: '8px', minHeight: '300px', userSelect: 'none' }}>
          {isLoading ? (
            <div className="text-gray-600" style={{ padding: '40px', textAlign: 'center' }}>
              Loading...
            </div>
          ) : items.length === 0 ? (
            <div className="text-gray-600" style={{ padding: '40px', textAlign: 'center' }}>
              This folder is empty
            </div>
          ) : viewMode === 'list' ? (
            /* ---- LIST VIEW ---- */
            <div style={{ display: 'flex', flexDirection: 'column', gap: '1px' }}>
              {/* List header */}
              <div style={{
                display: 'grid', gridTemplateColumns: '1fr 80px 40px',
                padding: '4px 8px', fontSize: '11px', color: '#9ca3af', fontWeight: 600,
                borderBottom: '1px solid #e5e7eb', marginBottom: '4px',
              }}>
                <span>Name</span>
                <span>Type</span>
                <span></span>
              </div>
              {items.map((item) => {
                const isFolder = item.cog_type.includes("folder");
                const isSelected = selectedFiles.some(f => f.id === item.id);
                const isDeleting = deletingItemId === item.id;
                const ext = item.name.includes('.') ? item.name.split('.').pop()?.toUpperCase() || '' : '';
                return (
                  <div
                    key={item.id}
                    onClick={(e) => handleItemClick(item, e)}
                    style={{
                      display: 'grid', gridTemplateColumns: '1fr 80px 40px',
                      padding: '6px 8px', alignItems: 'center',
                      borderRadius: '4px', cursor: isDeleting ? 'not-allowed' : 'pointer',
                      backgroundColor: isSelected ? '#eef2ff' : 'transparent',
                      border: isSelected ? '1px solid #4f46e5' : '1px solid transparent',
                      opacity: isDeleting ? 0.5 : 1, transition: 'background-color 0.15s',
                    }}
                    onMouseEnter={(e) => { if (!isSelected) e.currentTarget.style.backgroundColor = '#f9fafb'; }}
                    onMouseLeave={(e) => { if (!isSelected) e.currentTarget.style.backgroundColor = isSelected ? '#eef2ff' : 'transparent'; }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
                      <span style={{ fontSize: '16px', flexShrink: 0 }}>{isFolder ? '📁' : '📄'}</span>
                      <span style={{ fontSize: '13px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={item.name}>
                        {item.name}
                      </span>
                      {/* GY.C7 — data-label chip (controlled labels only;
                          public/internal render subtly). */}
                      <DataLabelChip label={item.data_classification} subtleDefault={false} />
                    </div>
                    <span style={{ fontSize: '11px', color: '#9ca3af' }}>{isFolder ? 'Folder' : ext}</span>
                    <button
                      onClick={(e) => handleDeleteItem(item.id, item.name, e)}
                      disabled={isDeleting}
                      style={{
                        background: 'none', border: 'none', color: '#ef4444',
                        cursor: isDeleting ? 'not-allowed' : 'pointer', fontSize: '14px',
                        opacity: 0.6, padding: '0 4px',
                      }}
                      onMouseEnter={(e) => { e.currentTarget.style.opacity = '1'; }}
                      onMouseLeave={(e) => { e.currentTarget.style.opacity = '0.6'; }}
                      title="Delete"
                    >×</button>
                  </div>
                );
              })}
            </div>
          ) : (
            /* ---- GRID VIEW ---- */
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: '8px' }}>
              {items.map((item) => {
                const isFolder = item.cog_type.includes("folder");
                const isSelected = selectedFiles.some(f => f.id === item.id);
                const isDeleting = deletingItemId === item.id;
                return (
                  <div
                    key={item.id}
                    style={{
                      padding: '12px',
                      border: isSelected ? '2px solid hsl(var(--primary))' : '1px solid hsl(var(--border))',
                      borderRadius: '4px',
                      cursor: isDeleting ? 'not-allowed' : 'default',
                      display: 'flex',
                      flexDirection: 'column',
                      alignItems: 'center',
                      gap: '8px',
                      transition: 'background-color 0.2s',
                      backgroundColor: isSelected ? 'hsl(var(--accent) / 0.12)' : 'hsl(var(--card))',
                      color: 'hsl(var(--foreground))',
                      opacity: isDeleting ? 0.5 : 1,
                      position: 'relative',
                      overflow: 'hidden',
                      minWidth: 0
                    }}
                    onMouseEnter={(e) => {
                      if (!isSelected) e.currentTarget.style.backgroundColor = 'hsl(var(--muted))';
                    }}
                    onMouseLeave={(e) => {
                      if (!isSelected) e.currentTarget.style.backgroundColor = 'hsl(var(--card))';
                    }}
                  >
                    {/* 2400LR-b8z: delete is sibling of selectable area */}
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        handleDeleteItem(item.id, item.name, e);
                      }}
                      disabled={isDeleting}
                      aria-label={`Delete ${item.name}`}
                      style={{
                        position: 'absolute',
                        top: '4px',
                        right: '4px',
                        background: '#ef4444',
                        color: 'white',
                        border: 'none',
                        borderRadius: '4px',
                        width: '20px',
                        height: '20px',
                        fontSize: '12px',
                        cursor: isDeleting ? 'not-allowed' : 'pointer',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        opacity: 0.8,
                        transition: 'opacity 0.2s',
                        zIndex: 1
                      }}
                      onMouseEnter={(e) => {
                        e.currentTarget.style.opacity = '1';
                      }}
                      onMouseLeave={(e) => {
                        e.currentTarget.style.opacity = '0.8';
                      }}
                    >
                      ×
                    </button>

                    {/* Selectable area */}
                    <div
                      role="button"
                      tabIndex={0}
                      aria-label={item.name}
                      aria-pressed={isFolder ? undefined : isSelected}
                      onClick={(e) => handleItemClick(item, e)}
                      onKeyDown={(e) => {
                        if (e.target !== e.currentTarget) return;
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          handleItemClick(item, e as unknown as React.MouseEvent);
                        }
                      }}
                      style={{
                        padding: '12px',
                        cursor: 'pointer',
                        display: 'flex',
                        flexDirection: 'column',
                        alignItems: 'center',
                        gap: '8px',
                        width: '100%',
                        minHeight: 0
                      }}
                    >
                      <FileThumbnail item={item} accessToken={accessToken} />
                      <div style={{
                        fontSize: '12px',
                        textAlign: 'center',
                        wordBreak: 'break-word',
                        overflowWrap: 'break-word',
                        width: '100%',
                        maxWidth: '100%',
                        overflow: 'hidden',
                        hyphens: 'auto',
                        lineHeight: '1.3'
                      }} title={item.name}>
                      {item.name.replace(/([_\-./])/g, '$1\u200B')}
                      </div>
                      {/* GY.C7 \u2014 data-label chip (controlled labels only;
                          public/internal render subtly). */}
                      <DataLabelChip label={item.data_classification} subtleDefault={false} />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', marginTop: '16px' }}>
          <button
            onClick={onClose}
            className="bg-white"
            style={{
              padding: '8px 16px',
              border: '1px solid #ddd',
              borderRadius: '4px',
              cursor: 'pointer'
            }}
          >
            Cancel
          </button>
          <button
            onClick={handleSelectFiles}
            disabled={selectedFiles.length === 0}
            style={{
              padding: '8px 16px',
              border: 'none',
              borderRadius: '4px',
              background: selectedFiles.length === 0 ? '#cbd5e1' : '#4f46e5',
              color: 'white',
              cursor: selectedFiles.length === 0 ? 'not-allowed' : 'pointer',
              opacity: selectedFiles.length === 0 ? 0.6 : 1
            }}
          >
            Select
          </button>
        </div>
      </div>
    </div>
  );
}
