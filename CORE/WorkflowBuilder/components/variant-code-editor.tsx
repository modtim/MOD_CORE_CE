"use client";

/**
 * 23.1a.s2 — view / edit ONE variant's code for a node.
 *
 * GET  /v1/nodes/{id}/source   → { node_id, name, is_system, editable,
 *                                  current_revision_no, revisions, hero,
 *                                  variants: {variant: {dockerfile, files,
 *                                  origin}} }
 * POST /v1/nodes/{id}/source   → body { hero?, source_overrides_by_variant,
 *                                  message? } → { revision_no, origin }
 *
 * The router has no `variant` query parameter (checked in
 * CORE/API/routers/node_code_edit.py:198 — the handler takes only node_id),
 * so the whole source document is read once and the requested variant is
 * selected client-side. A `?variant=` suffix would be accepted and ignored.
 *
 * The POST REPLACES the whole `source_overrides_by_variant` map
 * (node_code_edit.py:243), so saving one variant re-sends the other
 * variants' existing overrides unchanged — otherwise editing cuda would
 * silently revert rocm.
 */

import { useEffect, useState } from "react";
import CodeEditor from "@/components/code-editor";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { authenticatedFetch } from "@/lib/api-client";

interface VariantSourceEntry {
  dockerfile: string | null;
  files: { filename: string; content: string }[];
  origin: string;
}

interface VariantCodeEditorProps {
  nodeId: number;
  apiUrl: string;
  variant: string;
  /** "dockerfile" or one of the variant's source filenames. */
  filename: string;
  open: boolean;
  onClose: () => void;
  /** Fired after a successful save so the parent can refresh verdicts. */
  onSaved?: (revisionNo: number | null) => void;
}

function readEntry(raw: unknown): VariantSourceEntry {
  const entry = (raw ?? {}) as Record<string, unknown>;
  const files = Array.isArray(entry.files) ? entry.files : [];
  return {
    dockerfile: entry.dockerfile === null || entry.dockerfile === undefined
      ? null
      : String(entry.dockerfile),
    files: files
      .map((f: unknown) => {
        const file = (f ?? {}) as Record<string, unknown>;
        return { filename: String(file.filename ?? ""), content: String(file.content ?? "") };
      })
      .filter((f) => f.filename !== ""),
    origin: String(entry.origin ?? "generated"),
  };
}

export default function VariantCodeEditor({
  nodeId,
  apiUrl,
  variant,
  filename,
  open,
  onClose,
  onSaved,
}: VariantCodeEditorProps) {
  const [doc, setDoc] = useState<Record<string, unknown> | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [content, setContent] = useState<string>("");
  const [dirty, setDirty] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const resp = await authenticatedFetch(`${apiUrl}/nodes/${nodeId}/source`);
        if (cancelled) return;
        if (!resp?.ok) throw new Error(String(resp?.status ?? "failed"));

        const body = await resp.json();
        if (cancelled) return;

        setDoc(body ?? {});
        setLoadError(null);

        const entry = readEntry((body?.variants ?? {})[variant]);
        if (filename === "dockerfile") {
          setContent(entry.dockerfile ?? "");
        } else {
          const file = entry.files.find((f) => f.filename === filename);
          setContent(file ? file.content : "");
        }
      } catch (e) {
        if (cancelled) return;
        setDoc(null);
        setLoadError(e instanceof Error ? e.message : "Could not read the source.");
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [apiUrl, nodeId, variant, filename]);

  const editable = doc !== null && doc.editable === true;
  // The POST only carries per-variant FILE overrides — a variant's Dockerfile
  // comes from the build snapshot and is regenerated, so there is no channel
  // to hand-edit it. Showing a save button for it would promise a save that
  // cannot happen.
  const canSave = editable && filename !== "dockerfile";

  const save = async () => {
    if (!canSave || !dirty || doc === null) return;
    setSaveError(null);

    const variants = (doc.variants ?? {}) as Record<string, unknown>;
    // The POST REPLACES the whole override map, so every other variant's
    // existing edits are re-sent unchanged or they would vanish.
    const overrides: Record<string, { filename: string; content: string }[]> = {};
    for (const [name, raw] of Object.entries(variants)) {
      const entry = readEntry(raw);
      if (entry.origin !== "edited") continue;
      overrides[name] = entry.files;
    }
    const files = overrides[variant] ?? [];
    const replaced = files.map((f) => (f.filename === filename ? { filename, content } : f));
    overrides[variant] =
      replaced.some((f) => f.filename === filename)
        ? replaced
        : [...replaced, { filename, content }];

    try {
      const resp = await authenticatedFetch(
        `${apiUrl}/nodes/${nodeId}/source`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ source_overrides_by_variant: overrides }),
        },
      );
      if (!resp?.ok) {
        const raw = await resp.text().catch(() => "");
        // FastAPI wraps HTTPException(detail=...) as {"detail": {...}}.
        let code = "";
        try {
          code = String(JSON.parse(raw)?.detail?.code ?? "");
        } catch {
          /* keep raw */
        }
        if (code === "must_fork") {
          throw new Error("This is a platform node — make your own copy before editing.");
        }
        throw new Error(raw || "Save failed");
      }
      const body = await resp.json();
      setSaved(true);
      setDirty(false);
      onSaved?.(body?.revision_no === null || body?.revision_no === undefined
        ? null
        : Number(body.revision_no));
    } catch (e) {
      // The textarea keeps the unsaved text so the user can fix and retry;
      // the error line says nothing changed.
      setSaveError(e instanceof Error ? e.message : "Save failed");
    }
  };

  if (!open) return null;

  if (loadError) {
    return (
      <Dialog open onOpenChange={(v) => !v && onClose()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Source unavailable</DialogTitle>
            <DialogDescription>
              The node&apos;s source could not be read, so nothing here can be trusted or edited.
            </DialogDescription>
          </DialogHeader>
          <div className="bg-red-100 text-red-800 rounded p-2">{`GET /nodes/${nodeId}/source failed: ${loadError}`}</div>
          <DialogFooter />
        </DialogContent>
      </Dialog>
    );
  }

  if (doc === null) {
    return (
      <Dialog open onOpenChange={(v) => !v && onClose()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Loading {variant} source…</DialogTitle>
          </DialogHeader>
          <DialogFooter />
        </DialogContent>
      </Dialog>
    );
  }

  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{`${variant} · ${filename}`}</DialogTitle>
          <DialogDescription>
            {editable
              ? "Editing this one variant. Saving creates a new source revision."
              : "Platform node — view only. Make your own copy to edit."}
          </DialogDescription>
        </DialogHeader>

        <CodeEditor
          value={content}
          onChange={(next) => {
            if (next === content) return;
            setContent(next);
            setDirty(true);
          }}
          language={filename.endsWith(".sh") ? "shell" : "dockerfile"}
        />

        {saveError ? (
          <div className="bg-red-100 text-red-800 rounded p-2" role="alert">
            {`Not saved: ${saveError}`}
          </div>
        ) : null}
        {saved && !dirty ? <div className="bg-emerald-100 text-emerald-800 rounded p-2">Saved.</div> : null}
        {dirty && !saveError ? <div className="bg-amber-100 text-amber-800 rounded p-2">Unsaved changes.</div> : null}

        <DialogFooter>
          {canSave ? (
            <button
              type="button"
              onClick={save}
              className="bg-emerald-600 text-white rounded px-3 py-1"
            >
              Save
            </button>
          ) : (
            <span className="text-muted-foreground">
              {filename === "dockerfile"
                ? "Dockerfiles are regenerated by the build — not editable here."
                : "Not editable."}
            </span>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
