"use client";
import { apiUrl } from "@/lib/runtime-config";

import React, { Suspense, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Canvas, useLoader } from "@react-three/fiber";
import { OrbitControls, Center, useGLTF, Environment, Grid } from "@react-three/drei";
import * as THREE from "three";
import { OBJLoader } from "three/examples/jsm/loaders/OBJLoader.js";
import { FBXLoader } from "three/examples/jsm/loaders/FBXLoader.js";
import { PLYLoader } from "three/examples/jsm/loaders/PLYLoader.js";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { X } from "lucide-react";

// ---------------------------------------------------------------------------
// Error boundary — catches model-load failures that r3f throws (connection
// refused, 404, CORS).  Without this the r3f render loop throws a promise
// that Next.js turns into "Application error".
// ---------------------------------------------------------------------------
class ModelLoadErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: boolean }
> {
  state = { error: false } as { error: boolean };

  static getDerivedStateFromError(): { error: boolean } {
    return { error: true };
  }
  render() {
    if (this.state.error) {
      return (
        <div
          style={{
            width: "100%",
            height: "100%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "#6366f1",
          }}
        >
          Model unavailable
        </div>
      );
    }
    return this.props.children;
  }
}

// Auto-rotating wrapper (for thumbnails only)
function AutoRotate({ children }: { children: React.ReactNode }) {
  const ref = React.useRef<THREE.Group>(null);

  React.useEffect(() => {
    let id: number;
    const animate = () => {
      if (ref.current) {
        ref.current.rotation.y += 0.01;
      }
      id = requestAnimationFrame(animate);
    };
    id = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(id);
  }, []);

  return <group ref={ref}>{children}</group>;
}

// Model loaders — synchronous hook calls.  Errors (connection refused, 404,
// CORS) throw from the hook and are caught by the error boundary wrapping
// the Canvas.  On success the model renders normally through Suspense.
function GLBModel({ url }: { url: string }) {
  const gltf = useGLTF(url);
  return <Center><primitive object={gltf.scene.clone()} /></Center>;
}

function OBJModel({ url }: { url: string }) {
  const obj = useLoader(OBJLoader, url);
  return <Center><primitive object={obj.clone()} /></Center>;
}

function FBXModel({ url }: { url: string }) {
  const fbx = useLoader(FBXLoader, url);
  return <Center><primitive object={fbx} /></Center>;
}

function GeometryModel({ url, loader }: { url: string; loader: typeof PLYLoader | typeof STLLoader }) {
  const geometry = useLoader(loader, url);
  useEffect(() => { geometry.computeVertexNormals(); }, [geometry]);
  return (
    <Center>
      <mesh geometry={geometry}>
        <meshStandardMaterial color="#888" side={THREE.DoubleSide} />
      </mesh>
    </Center>
  );
}

function ModelScene({ url, ext }: { url: string; ext: string }) {
  const e = ext.toLowerCase();
  return (
    <>
      {(e === 'glb' || e === 'gltf') && <GLBModel url={url} />}
      {e === 'obj' && <OBJModel url={url} />}
      {e === 'fbx' && <FBXModel url={url} />}
      {e === 'ply' && <GeometryModel url={url} loader={PLYLoader} />}
      {e === 'stl' && <GeometryModel url={url} loader={STLLoader} />}
    </>
  );
}

// ---------------------------------------------------------------------------
// Full-screen interactive 3D viewer modal
// ---------------------------------------------------------------------------
export function Model3DViewerModal({ url, ext, cogId, accessToken, onClose, onPreviewSaved }: {
  url: string;
  ext: string;
  cogId?: string;
  accessToken?: string | null;
  onClose: () => void;
  onPreviewSaved?: (newThumbnailUrl: string) => void;
}) {
  const canvasRef = React.useRef<HTMLCanvasElement | null>(null);
  const [saving, setSaving] = useState(false);

  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onClose]);

  const handleSetPreview = async () => {
    // Find the canvas element inside the viewer
    const container = document.querySelector('[data-3d-viewer]');
    const canvas = container?.querySelector('canvas');
    if (!canvas || !cogId || !accessToken) return;

    setSaving(true);
    try {
      const dataUrl = canvas.toDataURL('image/png');
      const blob = await (await fetch(dataUrl)).blob();

      const formData = new FormData();
      formData.append('file', blob, 'thumbnail.png');

      const API_URL = apiUrl();
      const resp = await fetch(`${API_URL}/assets/${cogId}/thumbnail`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${accessToken}` },
        body: formData,
      });

      if (resp.ok) {
        // Fetch the new thumbnail URL and notify parent
        if (onPreviewSaved) {
          try {
            const thumbResp = await fetch(`${API_URL}/assets/${cogId}/thumbnail`, {
              headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
            });
            if (thumbResp.ok) {
              const data = await thumbResp.json();
              if (data?.url) onPreviewSaved(data.url);
            }
          } catch (e) { /* parent will refresh on next load */ }
        }
      } else {
        alert('Failed to save preview');
      }
    } catch (e) {
      alert('Failed to save preview');
    } finally {
      setSaving(false);
    }
  };

  return createPortal(
    <div
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 9999,
        backgroundColor: 'rgba(0,0,0,0.85)',
        display: 'flex',
        flexDirection: 'column',
      }}
      onClick={onClose}
    >
      {/* Header */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          padding: '6px 12px',
          color: 'white',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <span style={{ fontSize: '14px', fontWeight: 600 }}>
          3D Viewer — {ext.toUpperCase()}
        </span>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          {cogId && accessToken && (
            <button
              onClick={handleSetPreview}
              disabled={saving}
              style={{
                background: 'rgba(99,102,241,0.8)',
                border: 'none',
                borderRadius: '8px',
                padding: '6px 12px',
                cursor: saving ? 'not-allowed' : 'pointer',
                color: 'white',
                display: 'flex',
                alignItems: 'center',
                gap: '4px',
                fontSize: '12px',
                opacity: saving ? 0.6 : 1,
              }}
            >
              {saving ? 'Saving...' : 'Set as Preview'}
            </button>
          )}
          <button
            onClick={onClose}
            style={{
              background: 'rgba(255,255,255,0.1)',
              border: 'none',
              borderRadius: '8px',
              padding: '6px 10px',
              cursor: 'pointer',
              color: 'white',
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
              fontSize: '12px',
            }}
          >
            <X size={14} /> Close
          </button>
        </div>
      </div>

      {/* 3D Canvas */}
      <div
        data-3d-viewer
        style={{ flex: 1, margin: '0 4px 4px 4px', overflow: 'hidden' }}
        onClick={(e) => e.stopPropagation()}
      >
        <ModelLoadErrorBoundary>
          <Suspense
            fallback={
              <div style={{ width: '100%', height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#6366f1' }}>
                Loading 3D model...
              </div>
            }
          >
            <Canvas
              camera={{ position: [0, 1.5, 4], fov: 50 }}
              style={{ width: '100%', height: '100%', background: '#111827' }}
              gl={{ antialias: true, preserveDrawingBuffer: true }}
            >
              <ambientLight intensity={0.5} />
              <directionalLight position={[5, 8, 5]} intensity={1} castShadow />
              <directionalLight position={[-3, 4, -5]} intensity={0.3} />
              <Environment preset="studio" />
              <Grid
                args={[20, 20]}
                cellSize={0.5}
                cellThickness={0.5}
                cellColor="#333"
                sectionSize={2}
                sectionThickness={1}
                sectionColor="#444"
                fadeDistance={15}
                fadeStrength={1}
                position={[0, -1, 0]}
              />
              <OrbitControls
                enablePan
                enableZoom
                enableRotate
                autoRotate={false}
                makeDefault
              />
              <ModelScene url={url} ext={ext} />
            </Canvas>
          </Suspense>
        </ModelLoadErrorBoundary>
      </div>

      {/* Controls hint */}
      <div style={{ textAlign: 'center', paddingBottom: '4px', color: '#6b7280', fontSize: '10px' }}>
        Drag to rotate &middot; Scroll to zoom &middot; Right-drag to pan &middot; Esc to close
      </div>
    </div>,
    document.body
  );
}

// ---------------------------------------------------------------------------
// Thumbnail component (click to open full viewer)
// ---------------------------------------------------------------------------
interface Model3DThumbnailProps {
  url: string;
  ext: string;
  height?: string;
  className?: string;
}

export function Model3DThumbnail({ url, ext, height = "80px", className = "" }: Model3DThumbnailProps) {
  const [viewerOpen, setViewerOpen] = useState(false);

  return (
    <>
      <div
        className={className}
        style={{
          width: "100%",
          height,
          background: "#1a1a2e",
          borderRadius: "4px",
          overflow: "hidden",
          cursor: "pointer",
          position: "relative",
        }}
        onClick={(e) => {
          e.stopPropagation();
          setViewerOpen(true);
        }}
      >
        <ModelLoadErrorBoundary>
          <Suspense
            fallback={
              <div style={{ width: "100%", height: "100%", display: "flex", alignItems: "center", justifyContent: "center" }}>
                <span style={{ fontSize: "9px", color: "#6366f1" }}>Loading 3D...</span>
              </div>
            }
          >
            <Canvas
              camera={{ position: [0, 0, 3], fov: 45 }}
              style={{ width: "100%", height: "100%" }}
              gl={{ preserveDrawingBuffer: true, antialias: true }}
            >
              <ambientLight intensity={0.6} />
              <directionalLight position={[5, 5, 5]} intensity={0.8} />
              <Environment preset="studio" />
              <AutoRotate>
                <ModelScene url={url} ext={ext} />
              </AutoRotate>
            </Canvas>
          </Suspense>
        </ModelLoadErrorBoundary>
        {/* Click hint overlay */}
        <div style={{
          position: 'absolute',
          bottom: '2px',
          right: '4px',
          fontSize: '8px',
          color: 'rgba(255,255,255,0.5)',
          pointerEvents: 'none',
        }}>
          Click to expand
        </div>
      </div>

      {viewerOpen && (
        <Model3DViewerModal url={url} ext={ext} onClose={() => setViewerOpen(false)} />
      )}
    </>
  );
}
