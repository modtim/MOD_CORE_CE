"use client";
import { apiUrl } from "@/lib/runtime-config";

import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

/**
 * Full-screen Gaussian Splat viewer using gsplat.js.
 * Renders directly in a Canvas — no iframe, no external service.
 */
export function SplatViewerModal({
  url,
  filename,
  cogId,
  accessToken,
  onClose,
  onPreviewSaved,
}: {
  url: string;
  filename?: string;
  cogId?: string;
  accessToken?: string | null;
  onClose: () => void;
  onPreviewSaved?: (newUrl: string) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [loading, setLoading] = useState(true);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const rendererRef = useRef<any>(null);
  const sceneRef = useRef<any>(null);
  const cameraRef = useRef<any>(null);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  useEffect(() => {
    if (!canvasRef.current) return;

    let animId: number;
    let disposed = false;

    const init = async () => {
      try {
        const SPLAT = await import("gsplat");

        if (disposed) return;

        const canvas = canvasRef.current!;
        const renderer = new SPLAT.WebGLRenderer(canvas);
        const scene = new SPLAT.Scene();
        const camera = new SPLAT.Camera();
        const controls = new SPLAT.OrbitControls(camera, canvas);

        rendererRef.current = renderer;
        sceneRef.current = scene;
        cameraRef.current = camera;

        await SPLAT.PLYLoader.LoadAsync(url, scene, (p: number) => {
          setProgress(Math.round(p * 100));
        });

        if (disposed) {
          renderer.dispose();
          return;
        }

        setLoading(false);

        const animate = () => {
          if (disposed) return;
          animId = requestAnimationFrame(animate);
          controls.update();
          renderer.render(scene, camera);
        };
        animate();
      } catch (err: any) {
        if (!disposed) {
          const msg = err.message || String(err);
          if (msg.includes('uchar') || msg.includes('Unsupported property')) {
            setError("Not a Gaussian Splat file. This PLY is a regular 3D mesh — use 'View as 3D Mesh' instead.");
          } else {
            setError(msg);
          }
          setLoading(false);
        }
      }
    };

    init();

    return () => {
      disposed = true;
      cancelAnimationFrame(animId);
      rendererRef.current?.dispose();
    };
  }, [url]);

  const handleSetPreview = async () => {
    const canvas = canvasRef.current;
    if (!canvas || !cogId || !accessToken) return;

    setSaving(true);
    try {
      rendererRef.current?.render(sceneRef.current, cameraRef.current);
      const dataUrl = canvas.toDataURL("image/png");
      const blob = await (await fetch(dataUrl)).blob();

      const formData = new FormData();
      formData.append("file", blob, "thumbnail.png");

      const API_URL = apiUrl();
      const resp = await fetch(`${API_URL}/assets/${cogId}/thumbnail`, {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
        body: formData,
      });

      if (resp.ok && onPreviewSaved) {
        const thumbResp = await fetch(`${API_URL}/assets/${cogId}/thumbnail`, {
          headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
        });
        if (thumbResp.ok) {
          const data = await thumbResp.json();
          if (data?.url) onPreviewSaved(data.url);
        }
      }
    } catch (e) {
      // silent
    } finally {
      setSaving(false);
    }
  };

  return createPortal(
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 9999,
        backgroundColor: "rgba(0,0,0,0.95)",
        display: "flex",
        flexDirection: "column",
      }}
      onClick={onClose}
    >
      {/* Header */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          padding: "6px 12px",
          color: "white",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <span style={{ fontSize: "13px", fontWeight: 600 }}>
          {filename || "Gaussian Splat"}
        </span>
        <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
          {cogId && accessToken && (
            <button
              onClick={handleSetPreview}
              disabled={saving || loading}
              style={{
                background: "rgba(139,92,246,0.8)",
                border: "none",
                borderRadius: "6px",
                padding: "4px 12px",
                cursor: saving ? "not-allowed" : "pointer",
                color: "white",
                fontSize: "12px",
                opacity: saving || loading ? 0.5 : 1,
              }}
            >
              {saving ? "Saving..." : "Set as Preview"}
            </button>
          )}
          <button
            onClick={onClose}
            style={{
              background: "rgba(255,255,255,0.1)",
              border: "none",
              borderRadius: "6px",
              padding: "4px 10px",
              cursor: "pointer",
              color: "white",
              display: "flex",
              alignItems: "center",
              gap: "4px",
              fontSize: "12px",
            }}
          >
            <X size={14} /> Close
          </button>
        </div>
      </div>

      {/* Canvas */}
      <div
        style={{ flex: 1, margin: "0 4px 4px 4px", position: "relative" }}
        onClick={(e) => e.stopPropagation()}
      >
        <canvas
          ref={canvasRef}
          style={{
            width: "100%",
            height: "100%",
            display: "block",
            background: "#111",
          }}
        />
        {loading && (
          <div
            style={{
              position: "absolute",
              inset: 0,
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              color: "#a78bfa",
              gap: "8px",
            }}
          >
            <span style={{ fontSize: "14px" }}>Loading Gaussian splat...</span>
            <span style={{ fontSize: "12px", color: "#6b7280" }}>
              {progress}%
            </span>
          </div>
        )}
        {error && (
          <div
            style={{
              position: "absolute",
              inset: 0,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "#ef4444",
              fontSize: "14px",
            }}
          >
            {error}
          </div>
        )}
      </div>

      {/* Controls hint */}
      <div
        style={{
          textAlign: "center",
          paddingBottom: "4px",
          color: "#6b7280",
          fontSize: "10px",
        }}
      >
        Drag to rotate &middot; Scroll to zoom &middot; Right-drag to pan
        &middot; Esc to close
      </div>
    </div>,
    document.body
  );
}
