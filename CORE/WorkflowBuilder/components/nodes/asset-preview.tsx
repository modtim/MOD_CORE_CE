"use client";

import React, { memo, Suspense, useState, useRef, useEffect } from "react";
import { Canvas } from "@react-three/fiber";
import { OrbitControls, Center, useGLTF, Environment } from "@react-three/drei";
import { Image, Box, Film, FileQuestion, ChevronLeft, ChevronRight, Maximize2, X, Music, FileText, FileType, Sparkles, Scan, Type, Package } from "lucide-react";
import * as THREE from "three";
import { OBJLoader } from "three/examples/jsm/loaders/OBJLoader.js";
import { FBXLoader } from "three/examples/jsm/loaders/FBXLoader.js";
import { PLYLoader } from "three/examples/jsm/loaders/PLYLoader.js";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { useLoader } from "@react-three/fiber";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------
export interface PreviewAsset {
  url: string;
  file_extension: string;
  category: "image" | "model3d" | "video" | "audio" | "text" | "pdf" | "splat" | "pointcloud" | "font" | "archive" | "other";
  content_hash: string;
  logical_name: string;
}

interface AssetPreviewProps {
  assets: PreviewAsset[];
  // FX.72 p4 (Q55=C): when the producing node is AI/generative, the preview
  // carries a real-text "AI-generated" badge (accessible, not colour-only).
  aiGenerated?: boolean;
}

// ---------------------------------------------------------------------------
// 3D Model loaders
// ---------------------------------------------------------------------------

function GLBModel({ url }: { url: string }) {
  const { scene } = useGLTF(url);
  return (
    <Center>
      <primitive object={scene.clone()} />
    </Center>
  );
}

function OBJModel({ url }: { url: string }) {
  const obj = useLoader(OBJLoader, url);
  return (
    <Center>
      <primitive object={obj.clone()} />
    </Center>
  );
}

function FBXModel({ url }: { url: string }) {
  const fbx = useLoader(FBXLoader, url);
  return (
    <Center>
      <primitive object={fbx} />
    </Center>
  );
}

function PLYModel({ url }: { url: string }) {
  const geometry = useLoader(PLYLoader, url);

  useEffect(() => {
    geometry.computeVertexNormals();
  }, [geometry]);

  return (
    <Center>
      <mesh geometry={geometry}>
        <meshStandardMaterial color="#888" side={THREE.DoubleSide} />
      </mesh>
    </Center>
  );
}

function STLModel({ url }: { url: string }) {
  const geometry = useLoader(STLLoader, url);

  useEffect(() => {
    geometry.computeVertexNormals();
  }, [geometry]);

  return (
    <Center>
      <mesh geometry={geometry}>
        <meshStandardMaterial color="#888" side={THREE.DoubleSide} />
      </mesh>
    </Center>
  );
}

function AutoModel({ url, ext }: { url: string; ext: string }) {
  const normalized = ext.toLowerCase().replace(".", "");

  switch (normalized) {
    case "glb":
    case "gltf":
      return <GLBModel url={url} />;
    case "obj":
      return <OBJModel url={url} />;
    case "fbx":
      return <FBXModel url={url} />;
    case "ply":
      return <PLYModel url={url} />;
    case "stl":
      return <STLModel url={url} />;
    default:
      return <GLBModel url={url} />;
  }
}

// ---------------------------------------------------------------------------
// Individual preview renderers
// ---------------------------------------------------------------------------

function Model3DPreview({ asset, expanded }: { asset: PreviewAsset; expanded: boolean }) {
  const size = expanded ? "h-64 w-full" : "h-24 w-full";

  return (
    <div className={`${size} rounded bg-gray-900 overflow-hidden`}>
      <Canvas camera={{ position: [0, 0, 3], fov: 50 }}>
        <ambientLight intensity={0.6} />
        <directionalLight position={[5, 5, 5]} intensity={0.8} />
        <Suspense fallback={null}>
          <AutoModel url={asset.url} ext={asset.file_extension} />
          <Environment preset="studio" />
        </Suspense>
        <OrbitControls enableZoom={expanded} enablePan={expanded} />
      </Canvas>
    </div>
  );
}

function ImagePreview({ asset, expanded }: { asset: PreviewAsset; expanded: boolean }) {
  const size = expanded ? "max-h-64" : "max-h-24";

  return (
    <div className="w-full flex items-center justify-center bg-gray-100 rounded overflow-hidden">
      <img
        src={asset.url}
        alt={asset.logical_name || "Output"}
        className={`${size} w-full object-contain`}
        loading="lazy"
      />
    </div>
  );
}

function VideoPreview({ asset, expanded }: { asset: PreviewAsset; expanded: boolean }) {
  const size = expanded ? "max-h-64" : "max-h-24";

  return (
    <div className="w-full rounded overflow-hidden bg-black">
      <video
        src={asset.url}
        className={`${size} w-full object-contain`}
        controls={expanded}
        muted
        loop
        autoPlay={!expanded}
        playsInline
      />
    </div>
  );
}

function OtherPreview({ asset }: { asset: PreviewAsset }) {
  return (
    <div className="w-full h-16 flex items-center gap-2 bg-gray-50 rounded px-2">
      <FileQuestion className="h-5 w-5 text-gray-400 flex-shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="text-[10px] font-medium text-gray-700 truncate">
          {asset.logical_name || asset.content_hash.slice(0, 12)}
        </div>
        <div className="text-[9px] text-gray-400 uppercase">{asset.file_extension}</div>
      </div>
    </div>
  );
}

function AudioPreview({ asset }: { asset: PreviewAsset }) {
  return (
    <div className="w-full bg-gray-50 rounded p-2 flex flex-col items-center gap-1">
      <Music className="h-5 w-5 text-purple-400" />
      <audio src={asset.url} controls preload="metadata" className="w-full h-8" />
      <div className="text-[9px] text-gray-400 truncate max-w-full">
        {asset.logical_name || asset.file_extension.toUpperCase()}
      </div>
    </div>
  );
}

function TextPreview({ asset }: { asset: PreviewAsset }) {
  const [text, setText] = useState<string | null>(null);
  useEffect(() => {
    fetch(asset.url)
      .then(r => r.text())
      .then(t => setText(t.slice(0, 2048)))
      .catch(() => setText("(failed to load)"));
  }, [asset.url]);

  return (
    <div className="w-full h-20 bg-gray-900 rounded overflow-hidden p-1">
      <pre className="text-[7px] leading-tight text-gray-300 font-mono whitespace-pre-wrap overflow-hidden h-full">
        {text ?? "Loading..."}
      </pre>
    </div>
  );
}

function PDFPreview({ asset, expanded }: { asset: PreviewAsset; expanded: boolean }) {
  const size = expanded ? "h-64" : "h-20";
  return (
    <div className={`w-full ${size} rounded overflow-hidden border border-gray-200`}>
      <iframe src={asset.url} className="w-full h-full" title={asset.logical_name || "PDF"} />
    </div>
  );
}

function SplatPreview({ asset }: { asset: PreviewAsset }) {
  return (
    <div className="w-full h-16 flex items-center gap-2 bg-gray-50 rounded px-2">
      <Sparkles className="h-5 w-5 text-amber-400 flex-shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="text-[10px] font-medium text-gray-700 truncate">
          {asset.logical_name || "Gaussian Splat"}
        </div>
        <div className="text-[9px] text-gray-400 uppercase">{asset.file_extension}</div>
      </div>
    </div>
  );
}

function PointCloudPreview({ asset }: { asset: PreviewAsset }) {
  return (
    <div className="w-full h-16 flex items-center gap-2 bg-gray-50 rounded px-2">
      <Scan className="h-5 w-5 text-cyan-400 flex-shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="text-[10px] font-medium text-gray-700 truncate">
          {asset.logical_name || "Point Cloud"}
        </div>
        <div className="text-[9px] text-gray-400 uppercase">{asset.file_extension}</div>
      </div>
    </div>
  );
}

function FontPreview({ asset }: { asset: PreviewAsset }) {
  const fontId = `preview-font-${asset.content_hash.slice(0, 8)}`;
  useEffect(() => {
    const style = document.createElement('style');
    style.textContent = `@font-face { font-family: "${fontId}"; src: url("${asset.url}"); }`;
    document.head.appendChild(style);
    return () => { document.head.removeChild(style); };
  }, [asset.url, fontId]);

  return (
    <div className="w-full h-16 flex items-center justify-center bg-gray-50 rounded">
      <span style={{ fontFamily: `"${fontId}", sans-serif` }} className="text-2xl text-gray-700">
        AaBb 123
      </span>
    </div>
  );
}

function ArchivePreview({ asset }: { asset: PreviewAsset }) {
  return (
    <div className="w-full h-16 flex items-center gap-2 bg-gray-50 rounded px-2">
      <Package className="h-5 w-5 text-orange-400 flex-shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="text-[10px] font-medium text-gray-700 truncate">
          {asset.logical_name || "Archive"}
        </div>
        <div className="text-[9px] text-gray-400 uppercase">{asset.file_extension}</div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Category icon
// ---------------------------------------------------------------------------
function CategoryIcon({ category }: { category: string }) {
  switch (category) {
    case "image": return <Image className="h-3 w-3" />;
    case "model3d": return <Box className="h-3 w-3" />;
    case "video": return <Film className="h-3 w-3" />;
    case "audio": return <Music className="h-3 w-3" />;
    case "text": return <FileText className="h-3 w-3" />;
    case "pdf": return <FileType className="h-3 w-3" />;
    case "splat": return <Sparkles className="h-3 w-3" />;
    case "pointcloud": return <Scan className="h-3 w-3" />;
    case "font": return <Type className="h-3 w-3" />;
    case "archive": return <Package className="h-3 w-3" />;
    default: return <FileQuestion className="h-3 w-3" />;
  }
}

// ---------------------------------------------------------------------------
// Main AssetPreview component
// ---------------------------------------------------------------------------
export const AssetPreview = memo(({ assets, aiGenerated }: AssetPreviewProps) => {
  const [currentIndex, setCurrentIndex] = useState(0);
  const [expanded, setExpanded] = useState(false);

  if (!assets || assets.length === 0) return null;

  const asset = assets[currentIndex];
  const hasMultiple = assets.length > 1;

  const renderPreview = () => {
    switch (asset.category) {
      case "model3d": return <Model3DPreview asset={asset} expanded={expanded} />;
      case "image": return <ImagePreview asset={asset} expanded={expanded} />;
      case "video": return <VideoPreview asset={asset} expanded={expanded} />;
      case "audio": return <AudioPreview asset={asset} />;
      case "text": return <TextPreview asset={asset} />;
      case "pdf": return <PDFPreview asset={asset} expanded={expanded} />;
      case "splat": return <SplatPreview asset={asset} />;
      case "pointcloud": return <PointCloudPreview asset={asset} />;
      case "font": return <FontPreview asset={asset} />;
      case "archive": return <ArchivePreview asset={asset} />;
      default: return <OtherPreview asset={asset} />;
    }
  };

  return (
    <div className="mt-2 w-full" onClick={(e) => e.stopPropagation()}>
      {/* Preview area */}
      <div className="relative group">
        {renderPreview()}

        {/* Expand/collapse button */}
        {!["other", "splat", "pointcloud", "archive"].includes(asset.category) && (
          <button
            aria-label={expanded ? "Collapse preview" : "Expand preview"}
            onClick={(e) => { e.stopPropagation(); setExpanded(!expanded); }}
            className="absolute top-1 right-1 p-0.5 bg-black/50 rounded text-white opacity-0 group-hover:opacity-100 transition-opacity"
          >
            {expanded ? <X className="h-3 w-3" /> : <Maximize2 className="h-3 w-3" />}
          </button>
        )}
      </div>

      {/* Navigation + info bar */}
      <div className="flex items-center justify-between mt-1">
        <div className="flex items-center gap-1 text-[9px] text-gray-500 min-w-0">
          {aiGenerated && (
            <span
              title="Produced by an AI model — review before use"
              className="flex-shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded bg-violet-100 text-violet-700"
            >
              AI-generated
            </span>
          )}
          <CategoryIcon category={asset.category} />
          <span className="truncate max-w-[100px]">
            {asset.logical_name || asset.file_extension.toUpperCase()}
          </span>
        </div>

        {hasMultiple && (
          <div className="flex items-center gap-0.5">
            <button
              aria-label="Previous asset"
              onClick={(e) => {
                e.stopPropagation();
                setCurrentIndex((i) => (i - 1 + assets.length) % assets.length);
              }}
              className="p-0.5 rounded hover:bg-gray-200"
            >
              <ChevronLeft className="h-3 w-3 text-gray-500" />
            </button>
            <span className="text-[9px] text-gray-400">
              {currentIndex + 1}/{assets.length}
            </span>
            <button
              aria-label="Next asset"
              onClick={(e) => {
                e.stopPropagation();
                setCurrentIndex((i) => (i + 1) % assets.length);
              }}
              className="p-0.5 rounded hover:bg-gray-200"
            >
              <ChevronRight className="h-3 w-3 text-gray-500" />
            </button>
          </div>
        )}
      </div>
    </div>
  );
});

AssetPreview.displayName = "AssetPreview";
