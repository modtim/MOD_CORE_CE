"use client";

import React, { useState, useCallback } from "react";
import { Plus, Trash2, Star, StarOff, FileCode, Languages } from "lucide-react";

// A "file" the user pastes into the New Node wizard. One wizard run
// can now hold N files — e.g. a Python orchestrator that calls an R
// script via subprocess. The Containerizer's adapter runs per
// (file × variant) so each file gets language-appropriate rewrites
// for each GPU backend.
export interface WizardSourceFile {
  id: string;              // client-only, used as React key + for remove/rename
  filename: string;        // e.g. "main.py", "analyze.R"
  body: string;
  language?: string;       // optional override; detected server-side if empty
  is_entrypoint: boolean;  // exactly one file should be the entrypoint
}

// Languages the adapter knows about. Keep in sync with the Python
// module's _EXT_LANG / VARIANT_GUIDANCE keys. Unknown languages fall
// through to "unknown" and go through the passthrough prompt.
const KNOWN_LANGUAGES = [
  "python", "r", "julia", "rust", "cpp", "shell", "javascript",
  "go", "csharp", "swift", "kotlin", "php",
] as const;

// Lightweight extension-based guess for the UI's language badge. The
// server runs its own detection on /generate-artifacts; this is just a
// hint so the user sees "python" in the dropdown without clicking it.
function guessLanguage(filename: string): string {
  const dot = filename.lastIndexOf(".");
  if (dot === -1) return "unknown";
  const ext = filename.slice(dot).toLowerCase();
  const map: Record<string, string> = {
    ".py": "python", ".ipynb": "python",
    ".r": "r", ".R": "r",
    ".jl": "julia",
    ".rs": "rust",
    ".cpp": "cpp", ".cc": "cpp", ".cu": "cpp", ".h": "cpp", ".hpp": "cpp",
    ".sh": "shell", ".bash": "shell",
    ".js": "javascript", ".ts": "javascript", ".mjs": "javascript",
    ".go": "go",
    ".cs": "csharp",
    ".swift": "swift",
    ".kt": "kotlin", ".kts": "kotlin",
    ".php": "php",
  };
  return map[ext] || "unknown";
}

let _idSeq = 0;
function freshId() { _idSeq += 1; return `f${Date.now()}_${_idSeq}`; }

export function makeEmptyFile(filename = "main.py"): WizardSourceFile {
  return {
    id: freshId(),
    filename,
    body: "",
    language: "",
    is_entrypoint: true,
  };
}

interface Props {
  files: WizardSourceFile[];
  onChange: (next: WizardSourceFile[]) => void;
}

// Multi-file input step. Renders a left list (rows: filename, lang
// badge, entrypoint star, delete) and a right-hand code editor for
// the active file's body. Simple text area — matches the existing
// wizard UX, no CodeMirror dependency pulled in for v1.
export default function SourceFilesPane({ files, onChange }: Props) {
  const [activeId, setActiveId] = useState<string>(() => files[0]?.id || "");

  const activeFile =
    files.find((f) => f.id === activeId) || files[0] || null;

  const patchFile = useCallback(
    (id: string, patch: Partial<WizardSourceFile>) => {
      onChange(
        files.map((f) => (f.id === id ? { ...f, ...patch } : f)),
      );
    },
    [files, onChange],
  );

  const handleAdd = useCallback(() => {
    // New files default to .py; user can rename. Keep existing
    // entrypoint (don't steal it) unless the list was empty.
    const hadEntrypoint = files.some((f) => f.is_entrypoint);
    const fresh = makeEmptyFile(`file${files.length + 1}.py`);
    if (hadEntrypoint) fresh.is_entrypoint = false;
    const next = [...files, fresh];
    onChange(next);
    setActiveId(fresh.id);
  }, [files, onChange]);

  const handleRemove = useCallback(
    (id: string) => {
      if (files.length <= 1) return; // always keep at least one file
      const removed = files.find((f) => f.id === id);
      const next = files.filter((f) => f.id !== id);
      // If we deleted the entrypoint, promote the first survivor.
      if (removed?.is_entrypoint && next.length > 0) {
        next[0] = { ...next[0], is_entrypoint: true };
      }
      onChange(next);
      if (activeId === id) setActiveId(next[0]?.id || "");
    },
    [files, onChange, activeId],
  );

  const handleSetEntrypoint = useCallback(
    (id: string) => {
      // Exactly one entrypoint at a time — toggling the current
      // entrypoint off is disallowed (the wizard requires one).
      onChange(files.map((f) => ({ ...f, is_entrypoint: f.id === id })));
    },
    [files, onChange],
  );

  const handleRename = useCallback(
    (id: string, newName: string) => {
      // Auto-update language guess when the extension changes and
      // the user didn't pick an explicit override yet.
      patchFile(id, { filename: newName });
    },
    [patchFile],
  );

  return (
    <div className="flex h-[480px] border rounded-lg overflow-hidden bg-white">
      {/* File list */}
      <div className="w-64 border-r bg-slate-50 flex flex-col">
        <div className="px-3 py-2 border-b flex items-center justify-between bg-white">
          <span className="text-xs font-semibold text-gray-600">
            Files ({files.length})
          </span>
          <button
            onClick={handleAdd}
            title="Add file"
            className="p-1 rounded hover:bg-slate-100 text-gray-600"
          >
            <Plus className="w-4 h-4" />
          </button>
        </div>
        <div className="flex-1 overflow-y-auto">
          {files.map((f) => {
            const lang = f.language || guessLanguage(f.filename);
            const active = f.id === activeId;
            return (
              <div
                key={f.id}
                onClick={() => setActiveId(f.id)}
                className={[
                  "px-3 py-2 border-b text-sm cursor-pointer flex items-center gap-2",
                  active ? "bg-indigo-50" : "hover:bg-slate-100",
                ].join(" ")}
              >
                <FileCode className="w-3.5 h-3.5 shrink-0 text-gray-500" />
                <span className="flex-1 truncate" title={f.filename}>
                  {f.filename || "(unnamed)"}
                </span>
                <span
                  className="text-[10px] px-1.5 py-0.5 rounded bg-slate-200 text-gray-700 uppercase tracking-wide"
                  title={`Detected language: ${lang}`}
                >
                  {lang}
                </span>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    handleSetEntrypoint(f.id);
                  }}
                  title={
                    f.is_entrypoint
                      ? "This is the entrypoint"
                      : "Make this the entrypoint"
                  }
                >
                  {f.is_entrypoint ? (
                    <Star className="w-3.5 h-3.5 text-amber-500 fill-amber-500" />
                  ) : (
                    <StarOff className="w-3.5 h-3.5 text-gray-400" />
                  )}
                </button>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    handleRemove(f.id);
                  }}
                  disabled={files.length <= 1}
                  title={files.length <= 1 ? "Cannot remove the last file" : "Remove"}
                  className="disabled:opacity-40"
                >
                  <Trash2 className="w-3.5 h-3.5 text-gray-400 hover:text-red-600" />
                </button>
              </div>
            );
          })}
        </div>
      </div>

      {/* Active file editor */}
      <div className="flex-1 flex flex-col">
        {activeFile ? (
          <>
            <div className="px-3 py-2 border-b bg-white flex items-center gap-2">
              <input
                type="text"
                value={activeFile.filename}
                onChange={(e) => handleRename(activeFile.id, e.target.value)}
                className="flex-1 px-2 py-1 text-sm border rounded focus:outline-none focus:ring-1 focus:ring-indigo-500"
                placeholder="filename.py"
              />
              <Languages className="w-4 h-4 text-gray-400" />
              <select
                value={activeFile.language || ""}
                onChange={(e) =>
                  patchFile(activeFile.id, { language: e.target.value })
                }
                className="text-xs border rounded px-2 py-1"
                title="Override auto-detected language"
              >
                <option value="">auto ({guessLanguage(activeFile.filename)})</option>
                {KNOWN_LANGUAGES.map((l) => (
                  <option key={l} value={l}>
                    {l}
                  </option>
                ))}
              </select>
            </div>
            <textarea
              value={activeFile.body}
              onChange={(e) =>
                patchFile(activeFile.id, { body: e.target.value })
              }
              className="flex-1 font-mono text-xs p-3 outline-none resize-none"
              placeholder="// paste your source here"
              spellCheck={false}
            />
          </>
        ) : (
          <div className="flex items-center justify-center h-full text-gray-400 text-sm">
            Click a file on the left to edit.
          </div>
        )}
      </div>
    </div>
  );
}
