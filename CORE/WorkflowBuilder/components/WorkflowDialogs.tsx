"use client";
import { apiUrl } from "@/lib/runtime-config";

import React, { useState, useEffect } from 'react';
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Loader2, ArrowLeft, Trash2, RefreshCcw, AlertTriangle, CheckCircle, ChevronDown, MoreHorizontal, Pencil, Copy, Archive, ArchiveRestore } from 'lucide-react';
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { WorkflowSummary, WorkflowVersion, NodeDefinition, WorkflowValidateResult, WorkflowExport } from '@/lib/types';
import SaveSanitizationDialog from './save-sanitization-dialog';
import { ViolationList } from '@/lib/compliance-violations';
import { useEntitlements } from "@/hooks/use-entitlements";

interface WorkflowDialogsProps {
  isSaveDialogOpen: boolean;
  setSaveDialogOpen: (open: boolean) => void;
  saveDialogMode: "new" | "saveAs";
  newWorkflowName: string;
  setNewWorkflowName: (name: string) => void;
  handleSaveConfirm: () => void;

  isLoadDialogOpen: boolean;
  setLoadDialogOpen: (open: boolean) => void;
  dialogView: "workflows" | "versions";
  setDialogView: (view: "workflows" | "versions") => void;
  workflows: WorkflowSummary[];
  versions: WorkflowVersion[];
  currentWorkflow: WorkflowSummary | null;
  handleWorkflowSelection: (workflow: WorkflowSummary) => void;
  handleVersionSelection: (version: WorkflowVersion) => void;
  handleDeleteWorkflow?: (workflow: WorkflowSummary) => void;
  handleRenameWorkflow?: (workflow: WorkflowSummary, newName: string) => void;
  handleDuplicateWorkflow?: (workflow: WorkflowSummary) => void;
  handleArchiveWorkflow?: (workflow: WorkflowSummary) => void;
  handleUnarchiveWorkflow?: (workflow: WorkflowSummary) => void;
  archivedView?: boolean;
  toggleArchivedView?: () => void;
  workflowSearchTerm?: string;
  searchWorkflows?: (term: string) => void;
  handleDeleteVersion?: (version: WorkflowVersion) => void;

  isEditDialogOpen: boolean;
  setEditDialogOpen: (open: boolean) => void;
  isRefreshing: boolean;
  deletableNodes: NodeDefinition[];
  handleDeleteInitiation: (node: NodeDefinition) => void;
  handleRefreshAllNodes: () => void;
  accessToken?: string | null;

  isConfirmDeleteDialogOpen: boolean;
  setConfirmDeleteDialogOpen: (open: boolean) => void;
  nodeToDelete: NodeDefinition | null;
  setNodeToDelete: (node: NodeDefinition | null) => void;
  handleDeleteConfirm: () => void;

  isConfirmRefreshDialogOpen: boolean;
  setConfirmRefreshDialogOpen: (open: boolean) => void;
  handleRefreshConfirm: () => void;

  isMissingFileDialogOpen: boolean;
  setMissingFileDialogOpen: (open: boolean) => void;
  missingFileNodeName: string;

  isExecuting: boolean;

  // Import dialog
  isImportDialogOpen: boolean;
  importValidation: WorkflowValidateResult | null;
  pendingImportData: WorkflowExport | null;
  isImporting: boolean;
  handleImportConfirm: () => void;
  handleImportCancel: () => void;

  // Save sanitization (issue #21)
  isSanitizationDialogOpen: boolean;
  sanitizationAffectedCount: number;
  confirmSaveAsIs: () => void;
  confirmSaveCleanCopy: () => void;
  cancelSanitization: () => void;

  // FORTRESS 5.4 — compliance-violation display.
  complianceViolationsOpen: boolean;
  setComplianceViolationsOpen: (open: boolean) => void;
  pendingComplianceViolations: import('@/lib/compliance-violations').ComplianceViolation[];
}

const HW_TAG_LABELS: Record<string, string> = {
  cuda: 'CUDA', rocm: 'ROCm', intel: 'Intel', cpu: 'CPU',
  tensorrt: 'TensorRT', tpu: 'TPU', npu: 'NPU', groq: 'Groq',
  fpga: 'FPGA', metal: 'Metal', oneapi: 'oneAPI', vulkan: 'Vulkan',
};

function EditNodesContent({ deletableNodes, handleDeleteInitiation, accessToken, isOpen }: {
  deletableNodes: NodeDefinition[];
  handleDeleteInitiation: (node: NodeDefinition) => void;
  accessToken: string | null;
  isOpen: boolean;
}) {
  const [enabledTags, setEnabledTags] = useState<string[]>([]);
  const [variantData, setVariantData] = useState<Record<number, Record<string, string>>>({});
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [search, setSearch] = useState('');
  const [showUnbuiltOnly, setShowUnbuiltOnly] = useState(false);

  // Per-node category editor state (manual-test fix 2026-05-22).
  // `editedTypes[id]` is the dirty input value; `savingType` tracks
  // the in-flight PATCH so we can disable the Save button + show a
  // spinner. `savedFlash` flips true briefly after a successful save
  // so the row gets a green confirmation badge.
  const [editedTypes, setEditedTypes] = useState<Record<number, string>>({});
  const [savingType, setSavingType] = useState<Record<number, boolean>>({});
  const [savedFlash, setSavedFlash] = useState<Record<number, boolean>>({});
  // Reflects what's actually persisted on each node — initialized
  // from the deletableNodes list and updated optimistically after
  // each successful PATCH so the read shows the canonical form the
  // server returned (e.g. user typed "BEST" → server stored "Best").
  const [serverTypes, setServerTypes] = useState<Record<number, string>>({});

  // 2400LR-b6g — CE ships no /accelerators/node-variants route: CORE/API/routers/
  // gpu_settings.py is DENIED in open-core/manifest.yaml:1289 (+ :1537 for
  // gpu_settings*.py), so the fetch 404s there and trips the e2e assertClean
  // guard on every dialog open. SAME mechanism as the 2400UI120 data-markings
  // CE fix (components/data-marking-banner.tsx:78-121 — the entitlements block's
  // positively-resolved edition + the fetch gate that waits for the FIRST
  // resolution, with the bounded 5s fallback mirroring assistant-panel.tsx:283-289).
  // FAIL-OPEN: skipped ONLY on a positive edition "ce"; unresolved/errored leaves
  // edition null -> paid behaviour (same calls, same timing) is untouched.
  const { edition, loading } = useEntitlements();
  const ceEdition = edition === "ce";
  const [variantsGateOpen, setVariantsGateOpen] = useState(false);

  useEffect(() => {
    if (ceEdition) return;
    if (!loading) setVariantsGateOpen(true);
    const fallback = setTimeout(() => setVariantsGateOpen(true), 5000);
    return () => clearTimeout(fallback);
  }, [loading, ceEdition]);

  useEffect(() => {
    if (!isOpen || !accessToken || !variantsGateOpen) return;
    const API_URL = apiUrl();
    const load = async () => {
      try {
        const resp = await fetch(`${API_URL}/accelerators/node-variants`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        if (resp.ok) {
          const data = await resp.json();
          setEnabledTags(data.enabled_tags || data.enabled_backends || []);
          const vmap: Record<number, Record<string, string>> = {};
          for (const n of (data.nodes || [])) {
            vmap[n.id] = n.gpu_variants || {};
          }
          setVariantData(vmap);
        }
      } catch (e) {
        console.error('Failed to load node variants:', e);
      }
    };
    load();
  }, [isOpen, accessToken, variantsGateOpen]);

  const toggleExpand = (id: number) => {
    setExpanded(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  // Existing categories derived from the loaded node list — used as
  // datalist options so the user can pick from their taxonomy
  // instead of forking a new one. Excludes the "Custom" catch-all.
  const existingCategories = Array.from(new Set(
    deletableNodes
      .map(n => (serverTypes[n.id] ?? n.type) || '')
      .filter(t => t && t !== 'Custom')
  )).sort();

  const saveNodeType = async (node: NodeDefinition) => {
    if (!accessToken) return;
    const value = (editedTypes[node.id] ?? '').trim();
    if (!value) return;
    setSavingType(prev => ({ ...prev, [node.id]: true }));
    try {
      const API_URL = apiUrl();
      const resp = await fetch(`${API_URL}/nodes/${node.id}`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${accessToken}`,
        },
        body: JSON.stringify({ type: value }),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        alert(err.detail || `Update failed: HTTP ${resp.status}`);
        return;
      }
      const updated = await resp.json();
      // Server may have canonicalized the value ("BEST" → "Best") —
      // reflect that.
      setServerTypes(prev => ({ ...prev, [node.id]: updated.type || value }));
      setEditedTypes(prev => {
        const next = { ...prev };
        delete next[node.id];
        return next;
      });
      setSavedFlash(prev => ({ ...prev, [node.id]: true }));
      // Notify the rest of the app (node-library palette, recommended
      // panel, anything else watching) so the new category propagates
      // without the user having to refresh the page — same event the
      // delete + create flows fire (manual-test-driven fix
      // 2026-05-22).
      window.dispatchEvent(new Event("mod:nodes:changed"));
      window.setTimeout(() => {
        setSavedFlash(prev => {
          const next = { ...prev };
          delete next[node.id];
          return next;
        });
      }, 1800);
    } catch (e) {
      alert(`Update failed: ${e}`);
    } finally {
      setSavingType(prev => {
        const next = { ...prev };
        delete next[node.id];
        return next;
      });
    }
  };

  const getNodeStatus = (node: NodeDefinition) => {
    if (node.process_type?.toUpperCase() === 'CPU') return { text: 'CPU only', color: 'text-gray-500' };
    const variants = variantData[node.id] || {};
    const missing = enabledTags.filter(t => !variants[t]);
    if (missing.length === 0) return { text: 'Built for all hardware', color: 'text-green-600' };
    return { text: `Missing: ${missing.map(t => HW_TAG_LABELS[t] || t).join(', ')}`, color: 'text-amber-600' };
  };

  const filteredNodes = deletableNodes.filter(node => {
    if (search && !node.name.toLowerCase().includes(search.toLowerCase())) return false;
    if (showUnbuiltOnly) {
      if (node.process_type?.toUpperCase() === 'CPU') return false;
      const variants = variantData[node.id] || {};
      const missing = enabledTags.filter(t => !variants[t]);
      if (missing.length === 0) return false;
    }
    return true;
  });

  return (
    <div>
      <div className="flex items-center gap-3 mb-3">
        <Input
          placeholder="Search nodes..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="h-8 text-sm"
        />
        <label className="flex items-center gap-1.5 text-xs text-gray-600 whitespace-nowrap cursor-pointer">
          <input type="checkbox" checked={showUnbuiltOnly} onChange={(e) => setShowUnbuiltOnly(e.target.checked)} className="rounded" />
          Unbuilt only
        </label>
      </div>
      <ScrollArea className="h-80 w-full">
        <div className="space-y-2 pr-4">
          {filteredNodes.length === 0 && (
            <p className="text-sm text-gray-400 py-8 text-center">
              {search ? 'No nodes match your search.' : 'No nodes with missing variants.'}
            </p>
          )}
          {filteredNodes.map(node => {
          const isExpanded = expanded.has(node.id);
          const status = getNodeStatus(node);
          const variants = variantData[node.id] || {};
          const isCPU = node.process_type?.toUpperCase() === 'CPU';
          const missing = isCPU ? [] : enabledTags.filter(t => !variants[t]);

          return (
            <div key={node.id} className="border rounded-lg overflow-hidden">
              <button
                onClick={() => toggleExpand(node.id)}
                className="w-full flex items-center justify-between p-3 hover:bg-gray-50 transition-colors text-left"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-sm truncate">{node.name}</span>
                    {(node as any).calibration_status === 'calibrated' && (
                      <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-blue-100 text-blue-700">Calibrated</span>
                    )}
                    {(node as any).calibration_status === 'calibrating' && (
                      <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-yellow-100 text-yellow-700">
                        Calibrating {(node as any).calibration_progress?.completed || 0}/{(node as any).calibration_progress?.total || 9}
                      </span>
                    )}
                  </div>
                  <div className={`text-xs ${status.color} mt-0.5`}>{status.text}</div>
                </div>
                <ChevronDown className={`w-4 h-4 text-gray-400 shrink-0 ml-2 transition-transform ${isExpanded ? 'rotate-180' : ''}`} />
              </button>
              {isExpanded && (
                <div className="border-t p-3 bg-gray-50 space-y-1.5">
                  {/* Editable Category (manual-test fix 2026-05-22).
                      User types a new value or picks from datalist;
                      Save patches the NodeDefinition. Server folds
                      case so "BEST" / "best" / "Best" collapse onto
                      the canonical existing form. */}
                  <div className="flex items-center gap-2 text-xs pb-2 mb-1 border-b border-gray-200">
                    <span className="font-medium text-gray-600 w-20 shrink-0">Category</span>
                    <Input
                      value={editedTypes[node.id] ?? (serverTypes[node.id] ?? node.type ?? '')}
                      onChange={(e) => setEditedTypes(prev => ({ ...prev, [node.id]: e.target.value }))}
                      list={`edit-categories-${node.id}`}
                      placeholder="Pick or type a category"
                      className="h-7 text-xs flex-1"
                    />
                    <datalist id={`edit-categories-${node.id}`}>
                      {existingCategories.map((c) => (
                        <option key={c} value={c} />
                      ))}
                    </datalist>
                    <Button
                      size="sm" variant="outline"
                      className="h-7 text-xs px-3"
                      disabled={
                        !!savingType[node.id]
                        || !(editedTypes[node.id] ?? '').trim()
                        || (editedTypes[node.id] ?? '').trim() === (serverTypes[node.id] ?? node.type ?? '')
                      }
                      onClick={() => saveNodeType(node)}
                    >
                      {savingType[node.id] ? (
                        <Loader2 className="w-3 h-3 animate-spin" />
                      ) : savedFlash[node.id] ? (
                        <span className="text-green-700">Saved</span>
                      ) : (
                        'Save'
                      )}
                    </Button>
                  </div>
                  {enabledTags.map(tag => {
                    const img = variants[tag];
                    return (
                      <div key={tag} className="flex items-center justify-between text-xs gap-2">
                        <span className="font-medium text-gray-600 w-20 shrink-0">{HW_TAG_LABELS[tag] || tag}</span>
                        {img ? (
                          <span className="px-2 py-0.5 rounded-full bg-green-100 text-green-800 font-medium truncate max-w-[200px]" title={img}>{img.split('/').pop()}</span>
                        ) : (
                          <div className="flex items-center gap-2">
                            <span className="px-2 py-0.5 rounded-full bg-red-100 text-red-800 font-medium">Not built</span>
                            <Button variant="outline" size="sm" className="h-6 text-[10px] px-2"
                              onClick={async () => {
                                try {
                                  const API_URL = apiUrl();
                                  const resp = await fetch(`${API_URL}/nodes/${node.id}/build-variant/${tag}`, {
                                    method: 'POST',
                                    headers: { Authorization: `Bearer ${accessToken}` },
                                  });
                                  if (resp.ok) {
                                    alert(`Build started for "${node.name}" (${HW_TAG_LABELS[tag] || tag}). Check back shortly.`);
                                  } else {
                                    const err = await resp.json().catch(() => ({}));
                                    alert(err.detail || `Build failed: HTTP ${resp.status}`);
                                  }
                                } catch (e) { alert(`Build request failed: ${e}`); }
                              }}>
                              Build
                            </Button>
                          </div>
                        )}
                      </div>
                    );
                  })}
                  <div className="flex items-center gap-2 pt-2 border-t mt-2 flex-wrap">
                    {missing.length > 0 && (
                      <Button size="sm" className="h-7 text-xs"
                        onClick={async () => {
                          const API_URL = apiUrl();
                          let started = 0;
                          for (const tag of missing) {
                            try {
                              const resp = await fetch(`${API_URL}/nodes/${node.id}/build-variant/${tag}`, {
                                method: 'POST',
                                headers: { Authorization: `Bearer ${accessToken}` },
                              });
                              if (resp.ok) started++;
                            } catch {}
                          }
                          alert(`Started ${started}/${missing.length} variant builds for "${node.name}".`);
                        }}>
                        Build All Missing
                      </Button>
                    )}
                    {!isCPU && (
                      <Button variant="outline" size="sm" className="h-7 text-xs"
                        disabled={(node as any).calibration_status === 'calibrating'}
                        onClick={async () => {
                          if (!accessToken) return;
                          const API_URL = apiUrl();
                          try {
                            const resp = await fetch(`${API_URL}/nodes/${node.id}/calibrate`, {
                              method: 'POST',
                              headers: { Authorization: `Bearer ${accessToken}` },
                            });
                            if (resp.ok) {
                              const data = await resp.json();
                              alert(`Calibration started: ${data.tasks_dispatched} tasks dispatched`);
                            } else {
                              const err = await resp.json().catch(() => ({}));
                              alert(err.detail || 'Failed to start calibration');
                            }
                          } catch (e) { alert('Failed to start calibration'); }
                        }}>
                        {(node as any).calibration_status === 'calibrating'
                          ? `Calibrating ${(node as any).calibration_progress?.completed || 0}/${(node as any).calibration_progress?.total || 9}`
                          : 'Calibrate'}
                      </Button>
                    )}
                    <Button variant="destructive" size="sm" className="h-7 text-xs" onClick={() => handleDeleteInitiation(node)}>
                      <Trash2 className="w-3 h-3 mr-1" /> Delete
                    </Button>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </ScrollArea>
    </div>
  );
}

/** One row in the Load-workflow list: a Load button plus an Actions menu
 *  (Rename / Duplicate / Archive / Delete). Destructive actions (Archive,
 *  Delete) go through an AlertDialog confirm; Rename uses a small inline
 *  input dialog. Each row owns its own menu/dialog state. */
const WorkflowLoadRow: React.FC<{
  wf: WorkflowSummary;
  onSelect: (wf: WorkflowSummary) => void;
  onRename?: (wf: WorkflowSummary, newName: string) => void;
  onDuplicate?: (wf: WorkflowSummary) => void;
  onArchive?: (wf: WorkflowSummary) => void;
  onUnarchive?: (wf: WorkflowSummary) => void;
  onDelete?: (wf: WorkflowSummary) => void;
}> = ({ wf, onSelect, onRename, onDuplicate, onArchive, onUnarchive, onDelete }) => {
  const [renameOpen, setRenameOpen] = useState(false);
  const [renameValue, setRenameValue] = useState(wf.name);
  const [archiveConfirmOpen, setArchiveConfirmOpen] = useState(false);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);

  const hasMenu = !!(onRename || onDuplicate || onArchive || onUnarchive || onDelete);

  return (
    <div className="flex items-center gap-2">
      <Button variant="outline" className="flex-1 justify-start" onClick={() => onSelect(wf)}>{wf.name}</Button>
      {hasMenu && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="icon"
              className="shrink-0 text-gray-400 hover:text-gray-700"
              onClick={(e) => e.stopPropagation()}
              aria-label={`Actions for ${wf.name}`}
            >
              <MoreHorizontal className="h-4 w-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-44">
            {onRename && (
              <DropdownMenuItem onSelect={() => { setRenameValue(wf.name); setRenameOpen(true); }}>
                <Pencil className="h-4 w-4 mr-2" />Rename
              </DropdownMenuItem>
            )}
            {onDuplicate && (
              <DropdownMenuItem onSelect={() => onDuplicate(wf)}>
                <Copy className="h-4 w-4 mr-2" />Duplicate
              </DropdownMenuItem>
            )}
            {onArchive && (
              <DropdownMenuItem onSelect={() => setArchiveConfirmOpen(true)}>
                <Archive className="h-4 w-4 mr-2" />Archive
              </DropdownMenuItem>
            )}
            {onUnarchive && (
              <DropdownMenuItem onSelect={() => onUnarchive(wf)}>
                <ArchiveRestore className="h-4 w-4 mr-2" />Restore
              </DropdownMenuItem>
            )}
            {onDelete && (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  className="text-red-600 focus:text-red-600"
                  onSelect={() => setDeleteConfirmOpen(true)}
                >
                  <Trash2 className="h-4 w-4 mr-2" />Delete
                </DropdownMenuItem>
              </>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      )}

      {/* Rename dialog */}
      <Dialog open={renameOpen} onOpenChange={setRenameOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Rename workflow</DialogTitle>
            <DialogDescription>Enter a new name for &ldquo;{wf.name}&rdquo;.</DialogDescription>
          </DialogHeader>
          <div className="py-4">
            <Label htmlFor={`rename-${wf.id}`}>Name</Label>
            <Input
              id={`rename-${wf.id}`}
              value={renameValue}
              autoFocus
              onChange={(e) => setRenameValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && renameValue.trim()) {
                  onRename?.(wf, renameValue);
                  setRenameOpen(false);
                }
              }}
            />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRenameOpen(false)}>Cancel</Button>
            <Button
              disabled={!renameValue.trim() || renameValue.trim() === wf.name}
              onClick={() => { onRename?.(wf, renameValue); setRenameOpen(false); }}
            >
              Rename
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Archive confirm */}
      <AlertDialog open={archiveConfirmOpen} onOpenChange={setArchiveConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Archive &ldquo;{wf.name}&rdquo;?</AlertDialogTitle>
            <AlertDialogDescription>
              Archiving hides this workflow from the list without deleting it. You can restore it later.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => onArchive?.(wf)}>Archive</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Delete (entire workflow) confirm */}
      <AlertDialog open={deleteConfirmOpen} onOpenChange={setDeleteConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete &ldquo;{wf.name}&rdquo;?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently deletes the entire workflow and all of its versions. This cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-red-600 hover:bg-red-700"
              onClick={() => onDelete?.(wf)}
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
};

/** One row in the versions list: load the version, or delete just this
 *  version (with a confirm). */
const VersionLoadRow: React.FC<{
  version: WorkflowVersion;
  onSelect: (version: WorkflowVersion) => void;
  onDelete?: (version: WorkflowVersion) => void;
}> = ({ version, onSelect, onDelete }) => {
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  return (
    <div className="flex items-center gap-2">
      <Button variant="outline" className="flex-1 justify-start" onClick={() => onSelect(version)}>
        Version {version.version_number}
      </Button>
      {onDelete && (
        <>
          <Button
            variant="ghost"
            size="icon"
            className="shrink-0 text-gray-400 hover:text-red-600 hover:bg-red-50"
            onClick={(e) => { e.stopPropagation(); setDeleteConfirmOpen(true); }}
            aria-label={`Delete version ${version.version_number}`}
            title="Delete this version"
          >
            <Trash2 className="h-4 w-4" />
          </Button>
          <AlertDialog open={deleteConfirmOpen} onOpenChange={setDeleteConfirmOpen}>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Delete version {version.version_number}?</AlertDialogTitle>
                <AlertDialogDescription>
                  This deletes only this version, not the whole workflow. Versions with run history,
                  or the workflow&rsquo;s only remaining version, can&rsquo;t be deleted.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction
                  className="bg-red-600 hover:bg-red-700"
                  onClick={() => onDelete(version)}
                >
                  Delete version
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </>
      )}
    </div>
  );
};

export const WorkflowDialogs: React.FC<WorkflowDialogsProps> = ({
  isSaveDialogOpen, setSaveDialogOpen, saveDialogMode, newWorkflowName, setNewWorkflowName, handleSaveConfirm,
  isLoadDialogOpen, setLoadDialogOpen, dialogView, setDialogView, workflows, versions, currentWorkflow, handleWorkflowSelection, handleVersionSelection, handleDeleteWorkflow,
  handleRenameWorkflow, handleDuplicateWorkflow, handleArchiveWorkflow, handleUnarchiveWorkflow, archivedView, toggleArchivedView, workflowSearchTerm, searchWorkflows, handleDeleteVersion,
  isEditDialogOpen, setEditDialogOpen, isRefreshing, deletableNodes, handleDeleteInitiation, handleRefreshAllNodes, accessToken,
  isConfirmDeleteDialogOpen, setConfirmDeleteDialogOpen, nodeToDelete, setNodeToDelete, handleDeleteConfirm,
  isConfirmRefreshDialogOpen, setConfirmRefreshDialogOpen, handleRefreshConfirm,
  isMissingFileDialogOpen, setMissingFileDialogOpen, missingFileNodeName,
  isExecuting,
  isImportDialogOpen, importValidation, pendingImportData, isImporting, handleImportConfirm, handleImportCancel,
  isSanitizationDialogOpen, sanitizationAffectedCount, confirmSaveAsIs, confirmSaveCleanCopy, cancelSanitization,
  complianceViolationsOpen, setComplianceViolationsOpen, pendingComplianceViolations,
}) => {
  return (
    <>
      {/* Save sanitization prompt (issue #21) — fires before either
          handleSaveVersion or handleSaveConfirm dispatches when nodes
          carry user-input data and the per-workflow suppression flag
          isn't satisfied. Three exits map to confirmSaveAsIs /
          confirmSaveCleanCopy / cancelSanitization. */}
      <SaveSanitizationDialog
        open={isSanitizationDialogOpen}
        affectedCount={sanitizationAffectedCount}
        onSaveAsIs={confirmSaveAsIs}
        onSaveCleanCopy={confirmSaveCleanCopy}
        onCancel={cancelSanitization}
      />
      {/* FORTRESS 5.4 — structured compliance-violation display. Shown
          when the compose-time policy gate (workflow_cud.py §FORTRESS 5.2)
          rejects a save with HTTP 422. */}
      <ViolationList
        violations={pendingComplianceViolations}
        open={complianceViolationsOpen}
        onOpenChange={setComplianceViolationsOpen}
      />
      {/* Processing Dialog */}
      <Dialog open={isExecuting}>
        <DialogContent className="sm:max-w-[425px]" hideCloseButton>
            <DialogHeader>
                <DialogTitle>Processing Workflow</DialogTitle>
                <DialogDescription>
                    Your workflow is being executed. Please wait.
                </DialogDescription>
            </DialogHeader>
            <div className="flex items-center justify-center p-8">
                <Loader2 className="h-16 w-16 animate-spin" />
            </div>
        </DialogContent>
      </Dialog>

      {/* Save Dialog */}
      <Dialog open={isSaveDialogOpen} onOpenChange={setSaveDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{saveDialogMode === "new" ? "Save New Workflow" : "Save As New Workflow"}</DialogTitle>
            <DialogDescription>Enter a name for this workflow.</DialogDescription>
          </DialogHeader>
          <div className="py-4">
            <Label htmlFor="name">Name</Label>
            <Input id="name" value={newWorkflowName} onChange={(e) => setNewWorkflowName(e.target.value)} />
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setSaveDialogOpen(false)}>Cancel</Button>
            <Button onClick={handleSaveConfirm}>Save</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Load Dialog */}
      <Dialog open={isLoadDialogOpen} onOpenChange={setLoadDialogOpen}>
          <DialogContent>
              <DialogHeader>
                  <div className="flex items-center">
                  {dialogView === "versions" && (
                      <Button variant="ghost" size="icon" className="mr-2" aria-label="Back to workflows" onClick={() => setDialogView("workflows")}>
                          <ArrowLeft className="h-4 w-4" />
                      </Button>
                  )}
                  <DialogTitle>{dialogView === "workflows" ? (archivedView ? "Archived Workflows" : "Load Workflow") : `Versions for ${currentWorkflow?.name}`}</DialogTitle>
                  {dialogView === "workflows" && toggleArchivedView && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="ml-auto shrink-0 text-xs text-gray-500 hover:text-gray-800"
                        onClick={() => toggleArchivedView()}
                      >
                        {archivedView
                          ? (<><ArrowLeft className="h-3.5 w-3.5 mr-1.5" />Active</>)
                          : (<><Archive className="h-3.5 w-3.5 mr-1.5" />Archived</>)}
                      </Button>
                  )}
                  </div>
                  <DialogDescription>
                    {dialogView === "workflows"
                      ? (archivedView ? "Restore an archived workflow to the active list" : "Select a workflow to load")
                      : "Select a version to load"}
                  </DialogDescription>
              </DialogHeader>
              {dialogView === "workflows" && (
                  <div className="py-1">
                      <Input
                        aria-label="Search workflows"
                        placeholder="Search workflows…"
                        value={workflowSearchTerm}
                        onChange={(e) => searchWorkflows && searchWorkflows(e.target.value)}
                      />
                  </div>
              )}
              <ScrollArea className="h-72 w-full rounded-md border">
                  <div className="p-4 grid gap-4">
                      {dialogView === "workflows"
                          ? (workflows.length === 0
                              ? (<p className="text-sm text-gray-500 text-center py-8">{workflowSearchTerm ? `No workflows match "${workflowSearchTerm}".` : archivedView ? "No archived workflows." : "No workflows yet."}</p>)
                              : workflows.map((wf) => (
                                  <WorkflowLoadRow
                                    key={wf.id}
                                    wf={wf}
                                    onSelect={handleWorkflowSelection}
                                    onRename={archivedView ? undefined : handleRenameWorkflow}
                                    onDuplicate={archivedView ? undefined : handleDuplicateWorkflow}
                                    onArchive={archivedView ? undefined : handleArchiveWorkflow}
                                    onUnarchive={archivedView ? handleUnarchiveWorkflow : undefined}
                                    onDelete={handleDeleteWorkflow}
                                  />
                                )))
                          : versions.map((v) => (
                              <VersionLoadRow
                                key={v.id}
                                version={v}
                                onSelect={handleVersionSelection}
                                onDelete={handleDeleteVersion}
                              />
                            ))
                      }
                  </div>
              </ScrollArea>
          </DialogContent>
      </Dialog>

      {/* Edit/Delete Nodes Dialog */}
      <Dialog open={isEditDialogOpen} onOpenChange={setEditDialogOpen}>
          <DialogContent className="max-w-3xl">
              {isRefreshing && <div className="absolute inset-0 bg-white/80 flex items-center justify-center z-10"><Loader2 className="h-12 w-12 animate-spin" /></div>}
              <DialogHeader>
                <div className="flex items-center justify-between">
                  <div>
                    <DialogTitle>Edit Node Definitions</DialogTitle>
                    <DialogDescription>Manage nodes, hardware variants, and refresh from metadata files</DialogDescription>
                  </div>
                  <Button size="sm" onClick={handleRefreshAllNodes} className="shrink-0"><RefreshCcw className="h-4 w-4 mr-2" />Refresh All</Button>
                </div>
              </DialogHeader>
              <EditNodesContent
                deletableNodes={deletableNodes}
                handleDeleteInitiation={handleDeleteInitiation}
                accessToken={accessToken || null}
                isOpen={isEditDialogOpen}
              />
          </DialogContent>
      </Dialog>

      {/* Confirm Delete Dialog */}
      <AlertDialog open={isConfirmDeleteDialogOpen} onOpenChange={setConfirmDeleteDialogOpen}>
          <AlertDialogContent>
              <AlertDialogHeader><AlertDialogTitle>Are you sure?</AlertDialogTitle></AlertDialogHeader>
              <AlertDialogDescription>This will permanently delete the "{nodeToDelete?.name}" node definition.</AlertDialogDescription>
              <AlertDialogFooter>
                  <AlertDialogCancel onClick={() => setNodeToDelete(null)}>Cancel</AlertDialogCancel>
                  <AlertDialogAction onClick={handleDeleteConfirm}>Delete</AlertDialogAction>
              </AlertDialogFooter>
          </AlertDialogContent>
      </AlertDialog>

      {/* Confirm Refresh Dialog */}
      <AlertDialog open={isConfirmRefreshDialogOpen} onOpenChange={setConfirmRefreshDialogOpen}>
          <AlertDialogContent>
              <AlertDialogHeader><AlertDialogTitle>Refresh All Nodes?</AlertDialogTitle></AlertDialogHeader>
              <AlertDialogDescription>This will delete all custom nodes and re-fetch them. Are you sure?</AlertDialogDescription>
              <AlertDialogFooter>
                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                  <AlertDialogAction onClick={handleRefreshConfirm}>Refresh</AlertDialogAction>
              </AlertDialogFooter>
          </AlertDialogContent>
      </AlertDialog>

      {/* Missing File Dialog */}
      <AlertDialog open={isMissingFileDialogOpen} onOpenChange={setMissingFileDialogOpen}>
          <AlertDialogContent>
              <AlertDialogHeader><AlertDialogTitle>Input File Required</AlertDialogTitle></AlertDialogHeader>
              <AlertDialogDescription>Please provide a file for the "{missingFileNodeName}" node before executing the workflow.</AlertDialogDescription>
              <AlertDialogFooter>
                  <AlertDialogAction onClick={() => setMissingFileDialogOpen(false)}>OK</AlertDialogAction>
              </AlertDialogFooter>
          </AlertDialogContent>
      </AlertDialog>

      {/* Import Workflow Dialog */}
      <Dialog open={isImportDialogOpen} onOpenChange={(open) => !open && handleImportCancel()}>
          <DialogContent className="max-w-md">
              {isImporting && <div className="absolute inset-0 bg-white/80 flex items-center justify-center z-10"><Loader2 className="h-12 w-12 animate-spin" /></div>}
              <DialogHeader>
                  <DialogTitle>Import Workflow</DialogTitle>
                  <DialogDescription>
                      {pendingImportData ? `Import "${pendingImportData.name}"` : 'Review import details'}
                  </DialogDescription>
              </DialogHeader>

              {importValidation && (
                  <div className="py-4 space-y-4">
                      {/* Workflow Info */}
                      {pendingImportData && (
                          <div className="p-3 bg-gray-50 rounded-md">
                              <p className="font-medium">{pendingImportData.name}</p>
                              {pendingImportData.description && (
                                  <p className="text-sm text-gray-600 mt-1">{pendingImportData.description}</p>
                              )}
                              <p className="text-sm text-gray-500 mt-2">
                                  {pendingImportData.required_nodes.length} node type(s) required
                              </p>
                          </div>
                      )}

                      {/* Compatibility Status */}
                      {importValidation.valid ? (
                          <div className="flex items-start gap-3 p-3 bg-green-50 border border-green-200 rounded-md">
                              <CheckCircle className="h-5 w-5 text-green-600 mt-0.5" />
                              <div>
                                  <p className="font-medium text-green-800">Ready to Import</p>
                                  <p className="text-sm text-green-700">All required nodes are available.</p>
                              </div>
                          </div>
                      ) : (
                          <div className="flex items-start gap-3 p-3 bg-amber-50 border border-amber-200 rounded-md">
                              <AlertTriangle className="h-5 w-5 text-amber-600 mt-0.5" />
                              <div>
                                  <p className="font-medium text-amber-800">Missing Nodes</p>
                                  <p className="text-sm text-amber-700 mb-2">
                                      The following node types are not available in your account:
                                  </p>
                                  <ul className="text-sm text-amber-700 list-disc pl-4">
                                      {importValidation.missing_nodes.map(node => (
                                          <li key={node}>{node}</li>
                                      ))}
                                  </ul>
                                  <p className="text-sm text-amber-700 mt-2">
                                      The workflow will be imported but may not execute correctly.
                                  </p>
                              </div>
                          </div>
                      )}
                  </div>
              )}

              <DialogFooter>
                  <Button variant="outline" onClick={handleImportCancel}>Cancel</Button>
                  <Button onClick={handleImportConfirm} disabled={isImporting}>
                      {isImporting ? 'Importing...' : 'Import Workflow'}
                  </Button>
              </DialogFooter>
          </DialogContent>
      </Dialog>
    </>
  );
};