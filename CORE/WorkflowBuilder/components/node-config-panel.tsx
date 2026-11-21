// In WorkflowBuilder/components/node-config-panel.tsx

"use client";
import { wbNavLabel } from "@/lib/product-names";
import { apiUrl } from "@/lib/runtime-config";

import { useCallback, useEffect, useState } from "react";
import { usePollingBackoff } from "@/hooks/usePollingBackoff";
import { pollErrorForResponse } from "@/lib/poller";
import { authenticatedFetch } from "@/lib/api-client";
import { getApiUrl } from "@/lib/urls";
import { X, Clock, DollarSign, Activity, ChevronDown, ChevronRight, Settings, FileOutput, FileInput, Tag, KeyRound, Plus, Trash2, Loader2, ChevronsUpDown, Check, RefreshCw, Globe } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Command, CommandInput, CommandList, CommandEmpty, CommandGroup, CommandItem } from "@/components/ui/command";

// Credits are disabled by default (CREDITS_ENABLED=false on the server).
// This flag hides credit-related UI. When credits are re-enabled, this
// should be fetched from /platform/features at app startup instead.
const CREDITS_UI_ENABLED = false;
import { Button, buttonVariants } from "@/components/ui/button";
import { CopyLogButton } from "@/components/ui/copy-log-button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { WorkflowNode } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { OAuthConnectionPicker } from "@/components/oauth-connection-picker";
import { toast } from "@/components/ui/use-toast";
import { cn } from "@/lib/utils";
import { CostChip, ProcessingChip, VariantStatusBadges } from "./node-chips";
import EgressBadge from "@/components/egress-badge";
import { EgressStateBadge } from "@/components/egress-badge";

interface NodeConfigPanelProps {
  node: WorkflowNode;
  updateNodeData: (nodeId: string, data: any, opts?: { commit?: boolean }) => void;
  onClose: () => void;
  accessToken: string | null;
  pollingRunId?: number | null;
  // Run-control on the selected run (Airflow-style), shown only when a run is
  // selected. Restart re-runs this node + downstream (reusing upstream); pause
  // makes the run stop and wait when it reaches this node.
  selectedRunId?: number | null;
  isPaused?: boolean;
  onRestartFromNode?: (graphNodeId: string) => void;
  onPauseNode?: (graphNodeId: string) => void;
  onResumeNode?: (graphNodeId: string) => void;
}

// Maps for user-friendly display
const TYPE_EXAMPLES: { [key: string]: string } = {
  image: "PNG, JPG, TIFF",
  model: "OBJ, FBX, GLB, GLTF",
  mesh: "OBJ, FBX, PLY",
  video: "MP4, MOV, AVI",
  audio: "MP3, WAV, FLAC",
  nerf_model: "NeRF checkpoint files",
  "3d_model": "OBJ, FBX, GLB",
  nft: "Blockchain token",
  pointcloud: "PLY, PCD, LAS",
  texture: "PNG, JPG, EXR",
};

const FILE_MODE_LABELS: { [key: string]: string } = {
  single: "Single file only",
  multiple: "Multiple files",
  both: "Single files or batches",
  batch: "Batch processing",
  directory: "Entire folder",
};

// Parse options from description like "(low, medium, high)"
const parseOptionsFromDescription = (description: string): string[] | null => {
  const match = description.match(/\(([^)]+)\)/);
  if (match) {
    const options = match[1].split(',').map(s => s.trim());
    if (options.length >= 2 && options.every(o => o.length < 30)) {
      return options;
    }
  }
  return null;
};

// Detect if a number field should use a slider based on description
const getSliderRange = (description: string, defaultVal: number): { min: number; max: number; step: number } | null => {
  // Check for explicit ranges like "1-100", "0.0-1.0", "(1-31"
  const rangeMatch = description.match(/(\d+(?:\.\d+)?)\s*[-–to]+\s*(\d+(?:\.\d+)?)/);
  if (rangeMatch) {
    const min = parseFloat(rangeMatch[1]);
    const max = parseFloat(rangeMatch[2]);
    const step = max <= 1 ? 0.1 : 1;
    return { min, max, step };
  }
  // Quality fields often 1-100
  if (description.toLowerCase().includes('quality') && defaultVal >= 1 && defaultVal <= 100) {
    return { min: 1, max: 100, step: 1 };
  }
  // Strength/threshold 0-1 range
  if ((description.toLowerCase().includes('strength') || description.toLowerCase().includes('threshold')) && defaultVal <= 1) {
    return { min: 0, max: 1, step: 0.1 };
  }
  return null;
};

// Refresh Models button for nodes with api_source + secrets (e.g., NIM Bridge)
const RefreshModelsButton = ({
  secretRefs,
  accessToken,
  onModelsLoaded,
  onError,
}: {
  secretRefs: {secret_name: string, env_var: string}[];
  accessToken: string | null;
  onModelsLoaded: (models: any[]) => void;
  onError: (message: string) => void;
}) => {
  const [status, setStatus] = useState<'idle' | 'loading' | 'success' | 'error'>('idle');
  const [errorMsg, setErrorMsg] = useState('');

  const handleRefresh = async () => {
    if (!accessToken) { setErrorMsg('Not authenticated'); setStatus('error'); return; }
    setStatus('loading');
    setErrorMsg('');

    const API_URL = apiUrl();
    const endpointRef = secretRefs.find(r => r.env_var === 'NIM_ENDPOINT');
    const keyRef = secretRefs.find(r => r.env_var === 'NIM_API_KEY');

    if (!endpointRef?.secret_name) {
      setErrorMsg('Map the NIM_ENDPOINT secret first, then refresh.');
      setStatus('error');
      onError('No NIM_ENDPOINT secret mapped');
      return;
    }

    try {
      const params = new URLSearchParams({ endpoint_secret: endpointRef.secret_name });
      if (keyRef?.secret_name) params.set('key_secret', keyRef.secret_name);

      const resp = await fetch(`${API_URL}/nim/catalog/by-secrets?${params}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });

      if (!resp.ok) {
        const errText = await resp.text().catch(() => resp.statusText);
        setErrorMsg(`Server returned ${resp.status}: ${errText.slice(0, 200)}`);
        setStatus('error');
        onError(errText);
        return;
      }

      const models = await resp.json();
      if (!Array.isArray(models) || models.length === 0) {
        setErrorMsg('No models found. Check that your NIM_ENDPOINT URL is correct and the server is running.');
        setStatus('error');
        onError('Empty catalog');
        return;
      }

      setStatus('success');
      onModelsLoaded(models);
      setTimeout(() => setStatus('idle'), 3000);
    } catch (e: any) {
      setErrorMsg(`Connection failed: ${e.message || 'Unknown error'}. Is the NIM server reachable?`);
      setStatus('error');
      onError(e.message);
    }
  };

  return (
    <div className="mt-3 space-y-2">
      <button
        onClick={handleRefresh}
        disabled={status === 'loading'}
        className="w-full py-2 px-3 bg-indigo-600 text-white text-xs rounded-md hover:bg-indigo-700 transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
      >
        {status === 'loading' && <Loader2 className="w-3 h-3 animate-spin" />}
        {status === 'loading' ? 'Connecting to NIM server...' :
         status === 'success' ? 'Models loaded!' :
         'Save & Refresh Models'}
      </button>
      {status === 'error' && errorMsg && (
        <div className="p-2 bg-red-50 border border-red-200 rounded text-xs text-red-700">
          {errorMsg}
        </div>
      )}
    </div>
  );
};

// Interactive control for a single extra_variable
// A number input that types like a normal text field. Controlled number inputs
// that re-parse/clamp on every keystroke are unusable: clearing the field
// (`""` -> NaN) snaps back to the old value, leading zeros / partial values
// (`"1."`, `"0"`) get coerced mid-typing, a min/max clamp jumps the value while
// you type, and re-deriving the string from a number moves the caret. This keeps
// the RAW string in local state while the field is focused/being edited, allows
// empty and partial input with no coercion, and only parses + clamps + commits
// to a number on blur or Enter. It also refuses to re-hydrate from the incoming
// prop while the field is being edited, so a poll/refresh can't clobber typing.
const NumberField = ({
  value,
  isInt,
  min,
  max,
  step,
  clampOnCommit,
  onCommit,
  className,
  ariaLabel,
}: {
  value: any;
  isInt: boolean;
  min?: number;
  max?: number;
  step?: number;
  clampOnCommit?: (v: number) => number;
  onCommit: (v: number) => void;
  className?: string;
  ariaLabel?: string;
}) => {
  const propStr = (value === null || value === undefined) ? '' : String(value);
  const [draft, setDraft] = useState<string>(propStr);
  const [editing, setEditing] = useState(false);

  // Re-sync from the prop ONLY when the field isn't being edited. While editing
  // (focused / has unsaved keystrokes) we never overwrite the user's input.
  useEffect(() => {
    if (!editing) setDraft(propStr);
  }, [propStr, editing]);

  const commit = () => {
    setEditing(false);
    const trimmed = draft.trim();
    if (trimmed === '') {
      // Empty stays empty — don't force a number. Fall back to current prop.
      setDraft(propStr);
      return;
    }
    const parsed = isInt ? parseInt(trimmed, 10) : parseFloat(trimmed);
    if (isNaN(parsed)) {
      setDraft(propStr);
      return;
    }
    const finalVal = clampOnCommit ? clampOnCommit(parsed) : parsed;
    setDraft(String(finalVal));
    onCommit(finalVal);
  };

  return (
    <Input
      type="number"
      aria-label={ariaLabel}
      value={draft}
      min={min}
      max={max}
      step={step}
      onFocus={() => setEditing(true)}
      onChange={(e) => {
        setEditing(true);
        setDraft(e.target.value);
      }}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          commit();
          (e.target as HTMLInputElement).blur();
        }
      }}
      className={className}
    />
  );
};

// In-node picker for a per-tenant "named artifact" — a saved RAG index or LoRA
// adapter. Declared on an extra_variable in the node's configuration_schema as:
//     { "type": "named_ref", "ref_type": "rag" | "lora", "description": "..." }
// (the registry agent tags RAG Query's RAG_NAME as ref_type "rag" and Brand SDXL
// Generate's LORA_NAME as ref_type "lora"). It fetches
//     GET {API}/named-artifacts?type=<ref_type>   ->  [{name, type, ref, ...}]
// tenant-scoped via the user's bearer token, and offers the returned `name`s in a
// searchable dropdown with a refresh button and a free-text "type a name" fallback
// for artifacts not built yet / manual entry. The chosen (or typed) NAME string is
// stored as the value. Degrades gracefully: if the registry endpoint 404s or the
// fetch fails (not live yet), it falls back to a plain text input so the node still
// works. Poll-safe: the search box keeps its own local text and never re-hydrates
// from the incoming value prop, so a background refresh can't clobber typing.
const REF_TYPE_LABELS: Record<string, { one: string; many: string }> = {
  rag: { one: "RAG", many: "RAGs" },
  lora: { one: "LoRA", many: "LoRAs" },
};

const NamedRefField = ({
  displayName,
  description,
  refType,
  value,
  defaultValue,
  onChange,
  accessToken,
  refreshKey,
}: {
  displayName: string;
  description: string;
  refType: string;
  value: any;
  defaultValue: any;
  onChange: (value: any) => void;
  accessToken?: string | null;
  refreshKey?: number;
}) => {
  const labels = REF_TYPE_LABELS[refType] || { one: "artifact", many: "artifacts" };
  const currentValue = value ?? defaultValue ?? "";

  const [items, setItems] = useState<{ name: string; description?: string }[]>([]);
  const [status, setStatus] = useState<"idle" | "loading" | "loaded" | "error">("idle");
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [reloadTick, setReloadTick] = useState(0);

  useEffect(() => {
    if (!accessToken) { setStatus("idle"); return; }
    const API_URL = apiUrl();
    let cancelled = false;
    setStatus("loading");
    fetch(`${API_URL}/named-artifacts?type=${encodeURIComponent(refType)}`, {
      headers: { Authorization: `Bearer ${accessToken}`, Accept: "application/json" },
    })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((data) => {
        if (cancelled) return;
        const rows = Array.isArray(data) ? data : [];
        setItems(
          rows
            .filter((r: any) => r && typeof r.name === "string")
            .map((r: any) => ({ name: r.name, description: r.description })),
        );
        setStatus("loaded");
      })
      .catch(() => { if (!cancelled) setStatus("error"); });
    return () => { cancelled = true; };
  }, [refType, accessToken, refreshKey, reloadTick]);

  // Graceful degrade: endpoint missing / failed (404 or network) OR not signed in
  // — render a plain text input so the field still works and nothing breaks.
  if (status === "error" || (!accessToken && status !== "loading")) {
    return (
      <div className="space-y-2 py-2">
        <Label className="text-sm font-medium">{displayName}</Label>
        <Input
          type="text"
          value={currentValue || ""}
          onChange={(e) => onChange(e.target.value)}
          placeholder={`Enter ${labels.one} name`}
          className="w-full"
        />
        <p className="text-xs text-muted-foreground">
          {description || `Name of the ${labels.one} to use.`}
          {status === "error" ? ` (Saved ${labels.many} unavailable — type a name.)` : ""}
        </p>
      </div>
    );
  }

  const isLoading = status === "loading";
  const typed = query.trim();
  const matches = typed
    ? items.filter((it) => it.name.toLowerCase().includes(typed.toLowerCase()))
    : items;
  const exactExists = items.some((it) => it.name === typed);

  const commit = (nameVal: string) => {
    onChange(nameVal);
    setOpen(false);
    setQuery("");
  };

  return (
    <div className="space-y-2 py-2">
      <Label className="text-sm font-medium">{displayName}</Label>
      <div className="flex items-center gap-2">
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <Button
              variant="outline"
              role="combobox"
              aria-expanded={open}
              className="flex-1 justify-between text-xs font-normal h-9"
            >
              <span className="truncate">
                {currentValue ||
                  (isLoading
                    ? `Loading saved ${labels.many}...`
                    : `Select or type a ${labels.one} name...`)}
              </span>
              <ChevronsUpDown className="ml-2 h-3 w-3 shrink-0 opacity-50" />
            </Button>
          </PopoverTrigger>
          <PopoverContent className="w-[--radix-popover-trigger-width] p-0" align="start">
            {/* Own filtering (shouldFilter=false) so the free-text "Use ..." row
                and the empty/loading states behave predictably. */}
            <Command shouldFilter={false}>
              <CommandInput
                placeholder={`Search or type a ${labels.one} name...`}
                className="text-xs"
                value={query}
                onValueChange={setQuery}
              />
              <CommandList className="max-h-64">
                {isLoading ? (
                  <div className="py-4 text-center text-xs text-muted-foreground">
                    Loading saved {labels.many}...
                  </div>
                ) : (
                  <>
                    <CommandEmpty>
                      <div className="px-2 py-1.5 text-xs text-muted-foreground">
                        {items.length === 0
                          ? `No saved ${labels.many} yet, or type a name.`
                          : `No match — type a name to use it.`}
                      </div>
                    </CommandEmpty>
                    {typed && !exactExists && (
                      <CommandGroup>
                        <CommandItem
                          value={`__use__${typed}`}
                          onSelect={() => commit(typed)}
                          className="text-xs"
                        >
                          <Plus className="mr-2 h-3 w-3" />
                          Use &quot;{typed}&quot;
                        </CommandItem>
                      </CommandGroup>
                    )}
                    {matches.length > 0 && (
                      <CommandGroup heading={`Saved ${labels.many}`}>
                        {matches.map((it) => (
                          <CommandItem
                            key={it.name}
                            value={it.name}
                            onSelect={() => commit(it.name)}
                            className="text-xs"
                          >
                            <Check
                              className={`mr-2 h-3 w-3 ${
                                currentValue === it.name ? "opacity-100" : "opacity-0"
                              }`}
                            />
                            <span className="truncate">{it.name}</span>
                          </CommandItem>
                        ))}
                      </CommandGroup>
                    )}
                  </>
                )}
              </CommandList>
            </Command>
          </PopoverContent>
        </Popover>
        <Button
          type="button"
          variant="outline"
          size="icon"
          className="h-9 w-9 shrink-0"
          title={`Refresh saved ${labels.many}`}
          aria-label={`Refresh saved ${labels.many}`}
          disabled={isLoading}
          onClick={() => setReloadTick((t) => t + 1)}
        >
          <RefreshCw className={`h-3.5 w-3.5 ${isLoading ? "animate-spin" : ""}`} />
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        {description || `Pick a saved ${labels.one} or type a name that will be built.`}
      </p>
    </div>
  );
};

export const VariableControl = ({
  name,
  config,
  value,
  onChange,
  accessToken,
  onSideEffect,
  refreshKey,
  cachedModels,
}: {
  name: string;
  config: any;
  value: any;
  onChange: (value: any, batchUpdates?: { [key: string]: any }) => void;
  accessToken?: string | null;
  onSideEffect?: (updates: { [key: string]: any }) => void;
  refreshKey?: number;
  cachedModels?: any[];
}) => {
  const displayName = name.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  const description = config.description || '';
  const defaultValue = config.default;
  const type = config.type;
  const currentValue = value ?? defaultValue;
  const [comboOpen, setComboOpen] = useState(false);

  // API-sourced select: use cached models (from RefreshModelsButton) or fetch from API
  const [apiOptions, setApiOptions] = useState<any[]>([]);
  const apiSource = config.api_source;

  // Use cached models from per-server refresh if available
  useEffect(() => {
    if (cachedModels && cachedModels.length > 0) {
      setApiOptions(cachedModels);
      return;
    }
    if (!apiSource || !accessToken) return;
    const API_URL = apiUrl();
    setApiOptions([]); // Clear to show loading state
    fetch(`${API_URL}${apiSource}`, {
      headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
    })
      .then(r => r.ok ? r.json() : [])
      .then(data => setApiOptions(Array.isArray(data) ? data : []))
      .catch(() => setApiOptions([]));
  }, [apiSource, accessToken, refreshKey, cachedModels]);

  // Check for enum field first (preferred), then fall back to parsing from description
  const enumValues = config.enum;
  const enumLabels = config.enum_labels;
  const hasEnum = Array.isArray(enumValues) && enumValues.length > 0;

  // For string fields: check enum first, then parse from description
  const options = hasEnum ? enumValues.map(String) : (type === 'string' ? parseOptionsFromDescription(description) : null);

  // Check if number field should use slider. Prefer explicit schema
  // min/max/step (so a node can declare a range like 1000–50000000) and fall
  // back to parsing the description text.
  const sliderRange = (type === 'integer' || type === 'number')
    ? ((config.min != null && config.max != null)
        ? { min: Number(config.min), max: Number(config.max), step: Number(config.step ?? 1) }
        : getSliderRange(description, defaultValue))
    : null;

  // API-sourced select dropdown (e.g. NIM model catalog)
  // Always render as a dropdown when api_source is set — never fall through to text input
  if (apiSource) {
    if (apiOptions.length === 0) {
      return (
        <div className="space-y-2 py-2">
          <Label className="text-sm font-medium">{displayName}</Label>
          <Select disabled>
            <SelectTrigger className="w-full" aria-label={displayName}>
              <SelectValue placeholder="Loading models..." />
            </SelectTrigger>
            <SelectContent />
          </Select>
          <p className="text-xs text-muted-foreground">
            {accessToken ? "Fetching available models..." : "Sign in to load available models"}
          </p>
        </div>
      );
    }
  }
  if (apiSource && apiOptions.length > 0) {
    const labelField = config.api_label_field || 'name';
    const valueField = config.api_value_field || 'id';
    const groupField = config.api_group_field;
    const sideEffectMap = config.api_side_effects;

    const handleSelect = (val: string) => {
      let sideEffects: { [key: string]: any } | undefined;
      if (sideEffectMap) {
        const selected = apiOptions.find((o: any) => String(o[valueField]) === val);
        if (selected) {
          sideEffects = {};
          for (const [targetField, sourceField] of Object.entries(sideEffectMap)) {
            if (selected[sourceField as string] !== undefined) {
              sideEffects[targetField] = selected[sourceField as string];
            }
          }
        }
      }
      onChange(val, sideEffects);
      setComboOpen(false);
    };

    const selectedLabel = apiOptions.find((o: any) => String(o[valueField]) === String(currentValue))?.[labelField] || '';

    // Group options for display
    const grouped = groupField
      ? apiOptions.reduce((acc: any, item: any) => {
          const group = item[groupField] || 'Other';
          if (!acc[group]) acc[group] = [];
          acc[group].push(item);
          return acc;
        }, {})
      : { 'All': apiOptions };

    return (
      <div className="space-y-2 py-2">
        <Label className="text-sm font-medium">{displayName}</Label>
        <Popover open={comboOpen} onOpenChange={setComboOpen}>
          <PopoverTrigger asChild>
            <Button variant="outline" role="combobox" className="w-full justify-between text-xs font-normal h-9">
              <span className="truncate">{selectedLabel || `Select ${displayName.toLowerCase()}...`}</span>
              <ChevronsUpDown className="ml-2 h-3 w-3 shrink-0 opacity-50" />
            </Button>
          </PopoverTrigger>
          <PopoverContent className="w-[--radix-popover-trigger-width] p-0" align="start">
            <Command>
              <CommandInput placeholder={`Search ${apiOptions.length} models...`} className="text-xs" />
              <CommandList className="max-h-64">
                <CommandEmpty>No models found.</CommandEmpty>
                {Object.entries(grouped).map(([group, items]: [string, any]) => (
                  <CommandGroup key={group} heading={groupField ? group : undefined}>
                    {items.map((item: any) => (
                      <CommandItem
                        key={item[valueField]}
                        value={`${item[labelField]} ${item[valueField]}`}
                        onSelect={() => handleSelect(String(item[valueField]))}
                        className="text-xs"
                      >
                        <Check className={`mr-2 h-3 w-3 ${String(currentValue) === String(item[valueField]) ? 'opacity-100' : 'opacity-0'}`} />
                        {item[labelField]}
                      </CommandItem>
                    ))}
                  </CommandGroup>
                ))}
              </CommandList>
            </Command>
          </PopoverContent>
        </Popover>
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>
    );
  }

  // Named artifact picker (RAG index / LoRA adapter) — a dropdown populated live
  // from the tenant's saved artifacts, with a free-text fallback. Handled by a
  // dedicated component so its fetch hooks obey the rules of hooks.
  if (type === 'named_ref') {
    return (
      <NamedRefField
        displayName={displayName}
        description={description}
        refType={config.ref_type || 'rag'}
        value={value}
        defaultValue={defaultValue}
        onChange={onChange}
        accessToken={accessToken}
        refreshKey={refreshKey}
      />
    );
  }

  // Boolean - Switch
  if (type === 'boolean') {
    return (
      <div className="flex items-center justify-between py-2">
        <div className="space-y-0.5">
          <Label className="text-sm font-medium">{displayName}</Label>
          <p className="text-xs text-muted-foreground">{description}</p>
        </div>
        <Switch
          aria-label={displayName}
          checked={currentValue === true || currentValue === 'true'}
          onCheckedChange={onChange}
        />
      </div>
    );
  }

  // Field with enum options - Select dropdown (works for string, integer, number types)
  if (options) {
    // Helper to get display label for an option
    const getOptionLabel = (opt: string) => {
      if (enumLabels && enumLabels[opt]) {
        return enumLabels[opt];
      }
      // Fall back to capitalizing the option
      return opt.charAt(0).toUpperCase() + opt.slice(1).replace(/_/g, ' ');
    };

    // An EMPTY-string default means "use the effective default" (the schema's
    // empty_means hint, e.g. 'code128'). Show that as an explicit first option so
    // the select never looks blank. Radix SelectItem cannot take value '', so a
    // sentinel is used; choosing it stores '' (the saved value is unchanged).
    const DEFAULT_OPTION = '__default__';
    const showDefaultOption = defaultValue === '';
    const defaultOptionLabel = typeof config.empty_means === 'string' && config.empty_means
      ? `Default (${config.empty_means})`
      : 'Default';

    return (
      <div className="space-y-2 py-2">
        <Label className="text-sm font-medium">{displayName}</Label>
        <Select
          value={showDefaultOption && currentValue === '' ? DEFAULT_OPTION : String(currentValue ?? defaultValue)}
          onValueChange={(val) => {
            if (val === DEFAULT_OPTION) {
              onChange('');
              return;
            }
            // Convert back to original type if needed
            if (type === 'integer') {
              onChange(parseInt(val));
            } else if (type === 'number') {
              onChange(parseFloat(val));
            } else {
              onChange(val);
            }
          }}
        >
          <SelectTrigger className="w-full" aria-label={displayName}>
            <SelectValue placeholder={`Select ${displayName.toLowerCase()}`} />
          </SelectTrigger>
          <SelectContent>
            {showDefaultOption && (
              <SelectItem key={DEFAULT_OPTION} value={DEFAULT_OPTION}>
                {defaultOptionLabel}
              </SelectItem>
            )}
            {options.map((opt) => (
              <SelectItem key={opt} value={opt}>
                {getOptionLabel(opt)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>
    );
  }

  // Number with slider range — a slider for quick adjustment PLUS a number box
  // to type an exact value (override). Wide ranges (>=1000x, e.g. 1k–50M) use a
  // log scale so the slider stays usable across orders of magnitude.
  if ((type === 'integer' || type === 'number') && sliderRange) {
    const isInt = type === 'integer';
    const useLog = sliderRange.min > 0 && sliderRange.max / sliderRange.min >= 1000;
    const clampVal = (v: number) => Math.min(sliderRange.max, Math.max(sliderRange.min, v));
    const cvNum = Number(currentValue);
    const cvSafe = Number.isFinite(cvNum) ? cvNum : sliderRange.min;
    const toSlider = (v: number) => (useLog ? Math.log10(clampVal(v || sliderRange.min)) : clampVal(v));
    const fromSlider = (s: number) => {
      const v = useLog ? Math.pow(10, s) : s;
      return clampVal(isInt ? Math.round(v) : v);
    };
    const sMin = useLog ? Math.log10(sliderRange.min) : sliderRange.min;
    const sMax = useLog ? Math.log10(sliderRange.max) : sliderRange.max;
    const sStep = useLog ? 0.01 : sliderRange.step;
    const fmt = (n: number) => (isInt ? Math.round(n).toLocaleString() : String(n));
    return (
      <div className="space-y-3 py-2">
        <div className="flex items-center justify-between gap-2">
          <Label className="text-sm font-medium">{displayName}</Label>
          <NumberField
            value={currentValue}
            isInt={isInt}
            min={sliderRange.min}
            max={sliderRange.max}
            step={sliderRange.step}
            clampOnCommit={clampVal}
            onCommit={onChange}
            ariaLabel={displayName}
            className="w-36 h-8 text-right font-mono text-sm"
          />
        </div>
        <Slider
          value={[toSlider(cvSafe)]}
          min={sMin}
          max={sMax}
          step={sStep}
          onValueChange={([s]) => onChange(fromSlider(s))}
          className="w-full"
        />
        <div className="flex justify-between text-xs text-muted-foreground">
          <span>{fmt(sliderRange.min)}</span>
          <span>{description.replace(/\([^)]+\)/, '').trim()}</span>
          <span>{fmt(sliderRange.max)}</span>
        </div>
      </div>
    );
  }

  // Number without slider - regular input
  if (type === 'integer' || type === 'number') {
    return (
      <div className="space-y-2 py-2">
        <Label className="text-sm font-medium">{displayName}</Label>
        <NumberField
          value={currentValue}
          isInt={type === 'integer'}
          onCommit={onChange}
          ariaLabel={displayName}
          className="w-full"
        />
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>
    );
  }

  // Code editor for type: "code"
  if (type === 'code') {
    const language = config.language || 'python';
    return (
      <div className="space-y-2 py-2">
        <div className="flex items-center justify-between">
          <Label className="text-sm font-medium">{displayName}</Label>
          <span className="text-xs text-muted-foreground bg-gray-100 px-2 py-0.5 rounded">{language}</span>
        </div>
        <Textarea
          value={currentValue || ''}
          onChange={(e) => onChange(e.target.value)}
          placeholder={defaultValue || `Enter your ${language} code here...`}
          className="w-full font-mono text-sm min-h-[300px] bg-gray-900 text-green-400 border-gray-700"
          style={{ tabSize: 4 }}
        />
        <p className="text-xs text-muted-foreground">{description}</p>
      </div>
    );
  }

  // String - text input (default)
  return (
    <div className="space-y-2 py-2">
      <Label className="text-sm font-medium">{displayName}</Label>
      <Input
        type="text"
        value={currentValue || ''}
        onChange={(e) => onChange(e.target.value)}
        placeholder={defaultValue || `Enter ${displayName.toLowerCase()}`}
        className="w-full"
      />
      <p className="text-xs text-muted-foreground">{description}</p>
    </div>
  );
};

// Friendly schema display component
const FriendlySchemaDisplay = ({
  inputSchema,
  outputSchema,
  configSchema,
  metadata,
  extraVariableValues,
  onExtraVariableChange,
  accessToken,
  cachedModels,
}: {
  inputSchema?: any;
  outputSchema?: any;
  configSchema?: any;
  metadata?: any;
  extraVariableValues?: { [key: string]: any };
  onExtraVariableChange?: (key: string, value: any, batchUpdates?: { [key: string]: any }) => void;
  accessToken?: string | null;
  cachedModels?: any[];
}) => {
  const [showAdvanced, setShowAdvanced] = useState(false);

  // Dynamic I/O: side effects (e.g., NIM model selection) can override input/output types
  const dynamicInputMode = extraVariableValues?.['INPUT_MODE'] || extraVariableValues?.['input_mode'];
  const dynamicOutputMode = extraVariableValues?.['OUTPUT_MODE'] || extraVariableValues?.['output_mode'];

  // Use dynamic mode if available, fall back to static schema
  const effectiveInputType = dynamicInputMode || inputSchema?.input_type;
  const effectiveOutputType = dynamicOutputMode || outputSchema?.output_type;

  // Check if there's meaningful content
  const hasInput = effectiveInputType && effectiveInputType !== 'NONE';
  const hasOutput = effectiveOutputType && effectiveOutputType !== 'NONE';
  const hasMetadata = metadata?.accepts_metadata === true;

  // Object-form named ports: {portName: {type, file_mode, label}}. Legacy
  // single-port schemas expose input_type/output_type and are handled above;
  // multi-channel nodes (e.g. SDXL LoRA Train: Image / Regularization / Avoid-
  // Negative / Captions) declare each channel as its own object entry, which
  // the legacy Input/Output summary can't show. List them here with their human
  // labels so the negative + caption channels are named in the panel too.
  const objectPortEntries = (schema: any) =>
    (schema && !schema.input_type && !schema.output_type)
      ? Object.entries(schema).filter(([, v]) => v && typeof v === 'object') as [string, any][]
      : [];
  const objectInputs = objectPortEntries(inputSchema);
  const objectOutputs = objectPortEntries(outputSchema);
  const hasObjectPorts = objectInputs.length > 0 || objectOutputs.length > 0;

  // Parse extra_variables - handle both object format and legacy string format
  let extraVars: { [key: string]: any } | null = null;
  if (configSchema?.extra_variables && configSchema.extra_variables !== 'NONE') {
    if (typeof configSchema.extra_variables === 'object') {
      extraVars = configSchema.extra_variables;
    }
  }
  // FLAT CONFIGURATION_SCHEMA (BUG.WB-NODE-VARS-UI, owner 11-07): half the seeded
  // node defs declare variables at the TOP level of configuration_schema
  // (CORE/API/node_definitions_domains/image_ops.py:492 IMAGE_RESIZE_SPEC ->
  // :515 "configuration_schema": { IMAGE_OP, WIDTH, HEIGHT, KEEP_ASPECT, MAX_SIDE })
  // instead of wrapping them in "extra_variables". The dispatch side already
  // reads that shape (CORE/API/services/node_config_env_defaults.py:162
  // flat_env_defaults + :38 _IGNORED_KEYS), so the node RUNS with its defaults
  // while the panel showed no controls at all. Mirror the server's key rules
  // here: SCREAMING_SNAKE key, value is a variable-declaration object, and the
  // JSON-Schema scaffolding keys are never variables (fail closed - a schema
  // with no declared variables renders no Options block).
  if (!extraVars && configSchema && typeof configSchema === 'object') {
    const flatVars: { [key: string]: any } = {};
    for (const key in configSchema) {
      if (!/^[A-Z][A-Z0-9_]{0,63}$/.test(key)) continue;
      if (key === 'EXTRA_VARIABLES' || key === 'TYPE' || key === 'PROPERTIES' ||
          key === 'REQUIRED' || key === 'DEFAULT' || key === 'ENUM' ||
          key === 'ITEMS' || key === 'SCHEMA' || key === 'TITLE' ||
          key === 'DESCRIPTION') continue;
      const entry = configSchema[key];
      if (!entry || typeof entry !== 'object') continue;
      flatVars[key] = entry;
    }
    if (Object.keys(flatVars).length > 0) extraVars = flatVars;
  }
  const hasOptions = extraVars && Object.keys(extraVars).length > 0;

  // If nothing meaningful, show minimal message
  if (!hasInput && !hasOutput && !hasOptions && !hasMetadata && !hasObjectPorts) {
    return (
      <div className="text-sm text-muted-foreground italic">
        No additional configuration needed
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Object-form named input channels (multi-channel nodes) */}
      {objectInputs.length > 0 && (
        <div className="flex items-start gap-3 p-3 rounded-lg bg-blue-50">
          <FileInput className="h-5 w-5 text-blue-600 mt-0.5" />
          <div className="min-w-0">
            <div className="font-medium text-sm">Inputs</div>
            <ul className="mt-1 space-y-1">
              {objectInputs.map(([key, cfg]) => (
                <li key={key} className="text-sm text-muted-foreground">
                  <span className="font-medium text-foreground">{cfg?.label || key}</span>
                  <span className="text-xs ml-1">
                    ({cfg?.type || 'any'}{cfg?.file_mode ? `, ${FILE_MODE_LABELS[cfg.file_mode] || cfg.file_mode}` : ''})
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      {/* Object-form named output channels */}
      {objectOutputs.length > 0 && (
        <div className="flex items-start gap-3 p-3 rounded-lg bg-green-50">
          <FileOutput className="h-5 w-5 text-green-600 mt-0.5" />
          <div className="min-w-0">
            <div className="font-medium text-sm">Outputs</div>
            <ul className="mt-1 space-y-1">
              {objectOutputs.map(([key, cfg]) => (
                <li key={key} className="text-sm text-muted-foreground">
                  <span className="font-medium text-foreground">{cfg?.label || key}</span>
                  <span className="text-xs ml-1">({cfg?.type || 'any'})</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      {/* Input Section */}
      {hasInput && (
        <div className={`flex items-start gap-3 p-3 rounded-lg ${dynamicInputMode ? 'bg-blue-100 border border-blue-200' : 'bg-blue-50'}`}>
          <FileInput className="h-5 w-5 text-blue-600 mt-0.5" />
          <div>
            <div className="font-medium text-sm">Input {dynamicInputMode && <span className="text-xs font-normal text-blue-600">(model-specific)</span>}</div>
            <div className="text-sm text-muted-foreground">
              Accepts: {effectiveInputType?.charAt(0).toUpperCase() + effectiveInputType?.slice(1)} files
              {TYPE_EXAMPLES[effectiveInputType] && (
                <span className="text-xs ml-1">({TYPE_EXAMPLES[effectiveInputType]})</span>
              )}
            </div>
            {inputSchema?.file_mode && inputSchema.file_mode !== 'NONE' && (
              <div className="text-xs text-muted-foreground">
                {FILE_MODE_LABELS[inputSchema.file_mode] || inputSchema.file_mode}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Output Section */}
      {hasOutput && (
        <div className={`flex items-start gap-3 p-3 rounded-lg ${dynamicOutputMode ? 'bg-green-100 border border-green-200' : 'bg-green-50'}`}>
          <FileOutput className="h-5 w-5 text-green-600 mt-0.5" />
          <div>
            <div className="font-medium text-sm">Output {dynamicOutputMode && <span className="text-xs font-normal text-green-600">(model-specific)</span>}</div>
            <div className="text-sm text-muted-foreground">
              {effectiveOutputType?.charAt(0).toUpperCase() + effectiveOutputType?.slice(1)} files
              {TYPE_EXAMPLES[effectiveOutputType] && (
                <span className="text-xs ml-1">({TYPE_EXAMPLES[effectiveOutputType]})</span>
              )}
            </div>
            {outputSchema?.file_mode && outputSchema.file_mode !== 'NONE' && (
              <div className="text-xs text-muted-foreground">
                {FILE_MODE_LABELS[outputSchema.file_mode] || outputSchema.file_mode}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Metadata Section */}
      {hasMetadata && (
        <div className="flex items-center gap-3 p-3 bg-purple-50 rounded-lg">
          <Tag className="h-5 w-5 text-purple-600" />
          <div>
            <div className="font-medium text-sm">Metadata</div>
            <div className="text-sm text-muted-foreground">
              {metadata.metadata_required ? 'Requires metadata tags' : 'Can include metadata tags (optional)'}
            </div>
          </div>
        </div>
      )}

      {/* Options Section - Interactive Controls */}
      {hasOptions && (
        <div className="p-3 bg-orange-50 rounded-lg">
          <div className="flex items-center gap-2 mb-3">
            <Settings className="h-5 w-5 text-orange-600" />
            <div className="font-medium text-sm">Options</div>
          </div>
          <div className="space-y-1 divide-y divide-orange-100">
            {Object.entries(extraVars!).map(([key, config]) => (
              <VariableControl
                key={key}
                name={key}
                config={config}
                value={extraVariableValues?.[key]}
                onChange={(val, batchUpdates) => onExtraVariableChange?.(key, val, batchUpdates)}
                accessToken={accessToken}
                refreshKey={extraVariableValues?._refreshKey}
                cachedModels={(config as any)?.api_source === '/nim/catalog' ? cachedModels : undefined}
              />
            ))}
          </div>
        </div>
      )}

      {/* Advanced Details Toggle */}
      <button
        onClick={() => setShowAdvanced(!showAdvanced)}
        className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
      >
        {showAdvanced ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        Advanced Details
      </button>

      {/* Advanced Details Content */}
      {showAdvanced && (
        <div className="p-3 border rounded-md bg-gray-50 text-xs font-mono space-y-3">
          {inputSchema && Object.keys(inputSchema).length > 0 && (
            <div>
              <div className="text-muted-foreground font-semibold mb-1">Input Schema</div>
              {Object.entries(inputSchema).map(([key, value]) => (
                <div key={key} className="flex justify-between">
                  <span className="text-muted-foreground">{key}:</span>
                  <span>{String(value)}</span>
                </div>
              ))}
            </div>
          )}
          {outputSchema && Object.keys(outputSchema).length > 0 && (
            <div>
              <div className="text-muted-foreground font-semibold mb-1">Output Schema</div>
              {Object.entries(outputSchema).map(([key, value]) => (
                <div key={key} className="flex justify-between">
                  <span className="text-muted-foreground">{key}:</span>
                  <span>{String(value)}</span>
                </div>
              ))}
            </div>
          )}
          {configSchema && Object.keys(configSchema).length > 0 && (
            <div>
              <div className="text-muted-foreground font-semibold mb-1">Configuration Schema</div>
              {Object.entries(configSchema).map(([key, value]) => (
                <div key={key} className="flex justify-between">
                  <span className="text-muted-foreground">{key}:</span>
                  <span className="truncate max-w-[200px]">{typeof value === 'object' ? JSON.stringify(value) : String(value)}</span>
                </div>
              ))}
            </div>
          )}
          {metadata && Object.keys(metadata).length > 0 && (
            <div>
              <div className="text-muted-foreground font-semibold mb-1">Metadata</div>
              {Object.entries(metadata).map(([key, value]) => (
                <div key={key} className="flex justify-between">
                  <span className="text-muted-foreground">{key}:</span>
                  <span>{String(value)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

// Legacy helper component (kept for backwards compatibility)
const SchemaDisplay = ({ title, schema }: { title: string, schema?: { [key: string]: any } }) => {
  if (!schema || Object.keys(schema).length === 0) return null;
  return (
    <div className="space-y-2">
      <Label className="font-semibold">{title}</Label>
      <div className="p-2 border rounded-md bg-gray-50 text-xs font-mono">
        {Object.entries(schema).map(([key, value]) => (
          <div key={key} className="flex justify-between">
            <span className="text-muted-foreground">{key}:</span>
            <span>{String(value)}</span>
          </div>
        ))}
      </div>
    </div>
  );
};


// wizard-rebuild §6 verdicts plus §9 `untested_awaiting_hardware`
// (sample-test was speculative-queued; no matching accelerator
// hardware is online yet, so the variant is "ready in principle"
// but un-verifiable today).
type VariantVerdict =
  | "verified"
  | "marginal"
  | "drift"
  | "failed"
  | "incompatible"
  | "untested"
  | "untested_awaiting_hardware";
type VariantRow = {
  variant: string;
  score: number | null;
  verdict: VariantVerdict;
  scored_against_variant: string | null;
  output_type_at_test: string | null;
  scored_at: string | null;
};
type VariantVerificationPayload = {
  node_definition_id: number;
  reference_variant: string;
  variants: VariantRow[];
  pending_variants?: string[];
};

const verdictBadgeClass: Record<VariantVerdict, string> = {
  verified: "bg-emerald-100 text-emerald-800 border-emerald-200",
  marginal: "bg-amber-100 text-amber-800 border-amber-200",
  drift: "bg-amber-100 text-amber-800 border-amber-200",
  failed: "bg-red-100 text-red-800 border-red-200",
  incompatible: "bg-gray-200 text-gray-700 border-gray-300",
  untested: "bg-gray-100 text-gray-600 border-gray-200",
  // §9 — speculative variant that's waiting on hardware. Treated
  // visually like a soft warning so users know it's neither
  // verified nor broken.
  untested_awaiting_hardware: "bg-sky-100 text-sky-800 border-sky-200",
};


// §9 — friendly per-variant "awaiting X hardware" label. The
// scheduler refuses to route real workloads to a variant in this
// state until the heartbeat-drain hook claims it + the sample-test
// finally runs.
const HARDWARE_HINT_FOR_VARIANT: Record<string, string> = {
  cuda: "awaiting NVIDIA hardware",
  rocm: "awaiting AMD hardware",
  intel: "awaiting Intel hardware",
  npu: "awaiting NPU hardware",
  cpu: "awaiting any worker",
};

/**
 * wizard-rebuild §5 — drift-reference selector + per-variant verdict
 * chips. Shown in the node-config panel for Nodes with > 1 variant.
 */
const DriftReferenceSection = ({
  nodeDefinitionId,
  accessToken,
}: {
  nodeDefinitionId: number | null;
  accessToken?: string | null;
}) => {
  const onError = (msg: string) => {
    // No top-level error sink in NodeConfigPanel — log to console
    // so devs see issues. UI surfaces a "Loading…" → empty state on
    // failure; this is informational only.
    if (typeof window !== "undefined") {
      // eslint-disable-next-line no-console
      console.warn(`[DriftReferenceSection] ${msg}`);
    }
  };
  const [data, setData] = useState<VariantVerificationPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [patching, setPatching] = useState(false);

  useEffect(() => {
    if (!nodeDefinitionId || !accessToken) return;
    const API_URL = apiUrl();
    let cancelled = false;
    setLoading(true);
    fetch(
      `${API_URL}/nodes/${nodeDefinitionId}/variant-verification`,
      { headers: { Authorization: `Bearer ${accessToken}` } },
    )
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status}`))))
      .then((j: VariantVerificationPayload) => {
        if (!cancelled) setData(j);
      })
      .catch((e) => {
        if (!cancelled) onError(`variant-verification load: ${e}`);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [nodeDefinitionId, accessToken]);

  // §6 — poll every 5s while there are pending variants so the chip
  // strip flips from "verifying X" to verdict-of-X as background
  // sample-tests complete. Stops polling once everything reported.
  useEffect(() => {
    if (!accessToken || !nodeDefinitionId) return;
    const pending = data?.pending_variants || [];
    if (pending.length === 0) return;
    const API_URL = apiUrl();
    const handle = setInterval(() => {
      fetch(
        `${API_URL}/nodes/${nodeDefinitionId}/variant-verification`,
        { headers: { Authorization: `Bearer ${accessToken}` } },
      )
        .then((r) => (r.ok ? r.json() : null))
        .then((j) => {
          if (j) setData(j as VariantVerificationPayload);
        })
        .catch(() => undefined);
    }, 5000);
    return () => clearInterval(handle);
  }, [data?.pending_variants?.length, accessToken, nodeDefinitionId]);

  if (!nodeDefinitionId) return null;
  if (loading && !data) {
    return (
      <div className="p-3 border rounded-md bg-gray-50 space-y-2">
        <Label className="font-semibold text-xs uppercase tracking-wide text-muted-foreground">
          Variant verification
        </Label>
        <p className="text-xs text-muted-foreground">Loading…</p>
      </div>
    );
  }
  if (!data || data.variants.length === 0) return null;

  // Only verified / marginal / drift variants can be picked as the
  // drift reference. Failed / incompatible / untested can't —
  // they'd give meaningless or zero scores to peers.
  const pickable = data.variants.filter(
    (v) => v.verdict === "verified" || v.verdict === "marginal" || v.verdict === "drift",
  );

  const onChangeReference = async (newRef: string) => {
    if (!accessToken || newRef === data.reference_variant) return;
    const API_URL = apiUrl();
    setPatching(true);
    try {
      const resp = await fetch(
        `${API_URL}/nodes/${nodeDefinitionId}/reference-variant`,
        {
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${accessToken}`,
          },
          body: JSON.stringify({ reference_variant: newRef }),
        },
      );
      if (!resp.ok) {
        const text = await resp.text();
        if (onError) onError(`reference-variant PATCH: ${resp.status} ${text}`);
        return;
      }
      // Re-fetch to surface the recomputed verdicts.
      const refetch = await fetch(
        `${API_URL}/nodes/${nodeDefinitionId}/variant-verification`,
        { headers: { Authorization: `Bearer ${accessToken}` } },
      );
      if (refetch.ok) setData(await refetch.json());
    } finally {
      setPatching(false);
    }
  };

  return (
    <div className="p-3 border rounded-md bg-gray-50 space-y-3">
      <Label className="font-semibold text-xs uppercase tracking-wide text-muted-foreground">
        Variant verification
      </Label>

      <div className="space-y-1">
        <Label className="text-xs">Drift reference</Label>
        <select
          aria-label="Drift reference"
          value={data.reference_variant}
          disabled={patching}
          onChange={(e) => onChangeReference(e.target.value)}
          className="w-full text-sm border rounded px-2 py-1 bg-white disabled:opacity-50"
        >
          {pickable.map((v) => (
            <option key={v.variant} value={v.variant}>
              {v.variant}
              {v.variant === "hero" ? " (default)" : ""}
            </option>
          ))}
        </select>
        <p className="text-xs text-muted-foreground">
          Other variants are scored against this one. Switching
          re-scores in place; no rebuilds.
        </p>
      </div>

      <div className="space-y-1">
        <Label className="text-xs">Variants</Label>
        <div className="flex flex-wrap gap-1.5">
          {data.variants.map((v) => {
            // §9 — when the variant is waiting on hardware, use the
            // friendlier "awaiting X hardware" hint instead of the
            // raw verdict string.
            const awaiting = v.verdict === "untested_awaiting_hardware";
            const hardwareHint = awaiting
              ? (HARDWARE_HINT_FOR_VARIANT[v.variant] || "awaiting hardware")
              : null;
            return (
              <span
                key={v.variant}
                className={`text-xs px-2 py-0.5 rounded border ${verdictBadgeClass[v.verdict] || verdictBadgeClass.untested}`}
                title={
                  awaiting
                    ? `${v.variant}: speculative variant — scheduler won't route real workloads until matching hardware joins the cluster`
                    : v.score !== null
                    ? `score ${v.score.toFixed(3)} vs ${v.scored_against_variant || "self"}`
                    : `verdict: ${v.verdict}`
                }
              >
                {v.variant}
                {awaiting
                  ? ` · ${hardwareHint}`
                  : v.verdict !== "verified" ? ` · ${v.verdict}` : ""}
              </span>
            );
          })}
          {/* §6 — pending chips: variants the build is targeting but
              whose sample-test hasn't reported yet. Render as
              "verifying" with a soft outline so users see live
              progress without refreshing. */}
          {(data.pending_variants || []).map((v) => (
            <span
              key={`pending-${v}`}
              className="text-xs px-2 py-0.5 rounded border bg-sky-50 text-sky-700 border-sky-200 animate-pulse"
              title="Sample-test still running"
            >
              {v} · verifying
            </span>
          ))}
        </div>
      </div>
    </div>
  );
};


// Canvas-only operators (localNodes in workflow-builder.tsx / node-library.tsx).
// Their palette ids (997-999) are NOT their definitions — on a live DB those ids
// belong to unrelated real nodes (vector_upsert/vector_query/unreal_export), so
// the lookup must key on node.type, never the id. Human Approval (palette id 332,
// node_kind "human_approval") is skipped too: its seeded system row is hidden
// from tenants (and its id differs per install), so GET /nodes/{id} can only 404.
// FX.91 p2b — the approval state the palette badge and this panel share, in
// words. 'stale' is spelled out because it is the one-time contract made
// visible: the node's declaration changed after the admin approved it, so the
// gate refuses again until re-approved. Absent (pre-p2b payload) says nothing
// rather than claiming a decision was made.
const EGRESS_APPROVAL_LABELS: Record<string, string> = {
  approved: "approved for this tenant",
  required: "not approved yet — outbound use is not granted",
  stale: "approval is stale — the node's declared hosts changed since",
  not_required: "not required (declares no external hosts)",
};
function egressApprovalLabel(approval: any): string {
  if (!approval) return "approval state unavailable";
  return EGRESS_APPROVAL_LABELS[String(approval.state ?? "")]
    ?? String(approval.state ?? "unknown");
}

const LOCAL_OPERATOR_NODE_TYPES = new Set(["if-else", "switch", "split"]);

export default function NodeConfigPanel({ node, updateNodeData, onClose, accessToken, pollingRunId, selectedRunId, isPaused, onRestartFromNode, onPauseNode, onResumeNode }: NodeConfigPanelProps) {
  const { data } = node;
  const isConfigurable = ['switch', 'split'].includes(node.type as string);
  const isInputNode = node.type === 'input';
  const [processingHistory, setProcessingHistory] = useState<any>(null);
  const [loadingHistory, setLoadingHistory] = useState(false);

  // Execution logs state
  const [executionInfo, setExecutionInfo] = useState<any>(null);
  const [containerLogs, setContainerLogs] = useState<string[]>([]);
  const [loadingLogs, setLoadingLogs] = useState(false);

  // FX.91 p2b [U] — "Network access": the node's DECLARED outbound domains
  // (p2a `egress_domains`) plus THIS tenant's approval state (p2c
  // `egress_approval`, attached server-side by
  // services/node_egress_approval.palette_egress_states on BOTH the palette
  // list and this single live-def GET). Hydrated from the live-def fetch
  // below — no extra request.
  const [egressInfo, setEgressInfo] = useState<any>(null);
  // Tenant-admin role for the Approve button. There is no role helper in
  // lib/ (grep isTenantAdmin/tenant_admin finds only the store dialog and
  // workflow-builder, each deriving it locally from GET /me), so this follows
  // that same established pattern: parent_user_id == null.
  const [isTenantAdminUser, setIsTenantAdminUser] = useState(false);
  // Two-step approve: the p2c POST is a tenant-wide, audited decision, so the
  // first click arms it and the confirm click fires it.
  const [egressApprovalArmed, setEgressApprovalArmed] = useState(false);
  const [egressApprovalBusy, setEgressApprovalBusy] = useState(false);
  const [egressApprovalResult, setEgressApprovalResult] = useState<string | null>(null);

  // Refresh configuration_schema from the LIVE node definition. The schema
  // stored on the node is a snapshot taken when it was dropped on the canvas,
  // so a later change to the node def (e.g. raising a slider's max to 50M)
  // never reaches nodes already in a saved workflow. Pull the current def and
  // sync it in, so existing nodes pick up new settings/bounds.
  useEffect(() => {
    const defId = (data as any)?.nodeDefinitionId;
    const isLocalOperator = LOCAL_OPERATOR_NODE_TYPES.has(node.type as string);
    const isHumanApproval = (data as any)?.node_kind === "human_approval";
    if (!defId || !accessToken || isLocalOperator || isHumanApproval) return;
    let cancelled = false;
    const API_URL = apiUrl();
    (async () => {
      try {
        const resp = await fetch(`${API_URL}/nodes/${defId}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        if (!resp.ok) return;
        const def = await resp.json();
        const live = def?.configuration_schema;
        if (!cancelled && live &&
            JSON.stringify(live) !== JSON.stringify(data.configuration_schema)) {
          // Programmatic schema sync on panel open — not a user edit, so it
          // must not create an undo/redo history entry.
          updateNodeData(node.id, { ...data, configuration_schema: live }, { commit: false });
        }
        // node-version-pinning — refresh the node's current version from the
        // live def so the panel/badge show "update available" accurately even
        // for a workflow saved before the node was rebuilt. Does NOT touch the
        // pin — updating stays an explicit user action.
        const curVer = (def as any)?.current_version;
        const curVerId = (def as any)?.current_node_version_id;
        if (!cancelled && typeof curVer === 'number' &&
            (curVer !== (data as any).currentNodeVersion ||
             curVerId !== (data as any).currentNodeVersionId)) {
          updateNodeData(node.id, {
            ...data,
            currentNodeVersion: curVer,
            currentNodeVersionId: curVerId,
          }, { commit: false });
        }
        // FX.103 p4 — refresh the deprecation/retirement lifecycle from the
        // live def so an existing workflow's node header badge tracks a node
        // that was deprecated/retired after the workflow was saved.
        // FX.91 p2b — the SAME live def carries the declared egress (p2a)
        // and this tenant's approval state (p2c enrichment), so the panel's
        // "Network access" section reads what the palette badge showed.
        if (!cancelled) {
          setEgressInfo({
            declared: Array.isArray((def as any)?.egress_domains)
              ? (def as any).egress_domains : [],
            approval: (def as any)?.egress_approval ?? null,
          });
        }
        const liveLifecycle = (def as any)?.lifecycle_state ?? (def as any)?.status;
        const liveRetireAt = (def as any)?.retire_at ?? (def as any)?.retire_after;
        if (!cancelled && (liveLifecycle || (data as any).lifecycleState)) {
          const normLifecycle =
            (liveLifecycle === 'deprecated' || liveLifecycle === 'retired')
              ? liveLifecycle : 'active';
          if (normLifecycle !== (data as any).lifecycleState ||
              String(liveRetireAt ?? '') !== String((data as any).retireAt ?? '')) {
            updateNodeData(node.id, {
              ...data,
              lifecycleState: normLifecycle,
              retireAt: liveRetireAt,
            }, { commit: false });
          }
        }
      } catch { /* non-fatal — fall back to the snapshot */ }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [(data as any)?.nodeDefinitionId, node.type, accessToken]);

  // FX.91 p2b — the role signal for the Approve button. GET /me -> parent_user_id
  // == null is the codebase's tenant-admin predicate (same read the store dialog
  // and workflow-builder use); a failed read means NOT admin, so the section
  // stays read-only rather than offering a button that would 403.
  useEffect(() => {
    if (!accessToken) return;
    let cancelled = false;
    (async () => {
      try {
        const resp = await fetch(`${apiUrl()}/me`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        if (cancelled) return;
        if (!resp.ok) { setIsTenantAdminUser(false); return; }
        const u = await resp.json();
        if (!cancelled) setIsTenantAdminUser(u && u.parent_user_id == null);
      } catch {
        if (!cancelled) setIsTenantAdminUser(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accessToken]);

  // The p2c decision itself: POST /nodes/{id}/egress-approval with the EXACT
  // set the panel just showed (the endpoint re-validates and refuses a set that
  // no longer parses, so a stale page cannot widen an approval).
  const approveNodeEgress = async () => {
    const defId = (data as any)?.nodeDefinitionId;
    if (!accessToken || !defId) return;
    const declared: string[] = egressInfo?.declared || [];
    setEgressApprovalBusy(true);
    const API_URL = apiUrl();
    try {
      const resp = await fetch(`${API_URL}/nodes/${defId}/egress-approval`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${accessToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ domains: declared }),
      });
      if (!resp.ok) {
        setEgressApprovalResult(`Approval failed (HTTP ${resp.status})`);
        return;
      }
      const result = await resp.json();
      setEgressApprovalResult(
        `Approved ${((result?.domains as any) || declared).length} domain(s) for this tenant`
      );
      // Re-read the live def so the section repaints green from the SAME
      // source the palette reads (no locally invented state).
      const def = await fetch(`${API_URL}/nodes/${defId}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (def.ok) {
        const d = await def.json();
        setEgressInfo({
          declared: Array.isArray((d as any)?.egress_domains)
            ? (d as any).egress_domains : declared,
          approval: (d as any)?.egress_approval ?? null,
        });
      }
    } catch {
      setEgressApprovalResult("Approval failed — the request did not complete");
    } finally {
      setEgressApprovalBusy(false);
      setEgressApprovalArmed(false);
    }
  };

  const [logsExpanded, setLogsExpanded] = useState(true);

  // Gate 5 — a human_approval node names WHO signs off. Without an assignee
  // the backend falls back to whoever triggered the run, which always lands on
  // someone but is rarely who the author meant.
  const isApprovalNode = (data as any)?.node_kind === 'human_approval';
  const [assignableUsers, setAssignableUsers] = useState<{ id: number; label: string }[]>([]);
  const [assigneeLoadError, setAssigneeLoadError] = useState<string | null>(null);
  useEffect(() => {
    if (!isApprovalNode || !accessToken) return;
    let cancelled = false;
    // Declared here, not read from an outer scope: every API_URL in this file
    // is local to the callback that uses it. Referencing one across scopes
    // threw a ReferenceError that the old bare `catch` swallowed, leaving an
    // empty dropdown and no clue anywhere. `next.config.mjs` sets
    // `typescript.ignoreBuildErrors`, so the container build could not have
    // caught it either.
    const API_URL = apiUrl();
    (async () => {
      try {
        const resp = await fetch(`${API_URL}/interventions/assignable-users`, {
          headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
        });
        if (!resp.ok) {
          if (!cancelled) setAssigneeLoadError(`Could not load users (${resp.status})`);
          return;
        }
        const rows = await resp.json();
        if (!cancelled) { setAssignableUsers(rows); setAssigneeLoadError(null); }
      } catch (err) {
        // Surfaced rather than swallowed. An empty picker looks like "this
        // tenant has no other users", which is a different and much more
        // misleading thing than "the request failed".
        if (!cancelled) setAssigneeLoadError((err as Error)?.message || 'Could not load users');
      }
    })();
    return () => { cancelled = true; };
  }, [isApprovalNode, accessToken]);

  // Secrets state
  const [availableSecrets, setAvailableSecrets] = useState<{id: number, name: string, description?: string, has_access?: boolean}[]>([]);
  // Secret names the user has already requested access to (so the button can
  // flip to "Requested ✓" without a refetch).
  const [requestedSecrets, setRequestedSecrets] = useState<Set<string>>(new Set());

  // Fire an access request for a secret the user selected but can't use.
  const requestSecretAccess = async (secretId: number, secretName: string) => {
    if (!accessToken) return;
    const API_URL = apiUrl();
    try {
      const resp = await fetch(`${API_URL}/secrets/${secretId}/request-access`, {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (resp.ok || resp.status === 400) {
        // 400 = already requested / already has access — either way, mark it.
        setRequestedSecrets(prev => new Set(prev).add(secretName));
      }
    } catch { /* ignore; user can retry */ }
  };
  const [secretsExpanded, setSecretsExpanded] = useState(false);
  // sz-4.9c — a ref is EITHER a named-secret binding {secret_name, env_var}
  // OR an OAuth-connection binding {env_var, oauth_connection_id} (no
  // secret_name; the server resolves the connection to a token at run time,
  // so the token never reaches the browser).
  const secretRefs: {secret_name?: string, env_var: string, oauth_connection_id?: number}[] =
    (data.secret_refs as any) || [];

  // generalized-stack-wake D12 — the "Managed stack" assignment control. Lists
  // the tenant's managed (persistent) stacks so the user can tie THIS node to
  // one (data.requires_stack) or leave it self-contained. Fetched through the
  // shared backoff poller (single-flight, backs off on 5xx, pauses when hidden),
  // enabled only while the panel is open + authed. The live per-node badge is
  // driven separately by the node-bindings poll; this list only powers the
  // dropdown + its status hints.
  const [managedStacks, setManagedStacks] = useState<{ name: string; status?: string }[]>([]);
  const [managedStacksError, setManagedStacksError] = useState<string | null>(null);
  const fetchManagedStacks = useCallback(async () => {
    const res = await authenticatedFetch(`${getApiUrl()}/stacks`);
    const err = pollErrorForResponse(res);
    if (err) throw err;
    const rows = await res.json();
    if (Array.isArray(rows)) {
      setManagedStacks(
        rows
          .filter((s: any) => s && typeof s.name === "string")
          .map((s: any) => ({ name: s.name as string, status: s.status as string | undefined })),
      );
      setManagedStacksError(null);
    }
  }, []);
  usePollingBackoff(
    fetchManagedStacks,
    {
      baseMs: 15000,
      enabled: !!accessToken,
      onStatus: (s) => {
        if (s === "backing-off" || s === "stopped") {
          setManagedStacksError("Could not load managed stacks");
        }
      },
    },
    [accessToken],
  );
  const currentRequiresStack: string = (data as any)?.requires_stack || "";


  // Accepted secrets declared by the DAG/node definition. sz-4.9c: a slot may
  // also declare oauth_providers — then its dropdown offers the tenant's live
  // OAuth connections instead of named secrets.
  const acceptedSecrets: {env_var: string, description?: string, required?: boolean, oauth_providers?: string[]}[] =
    data.custom_metadata?.accepted_secrets || [];

  // Fetch available secrets when section is expanded
  useEffect(() => {
    if (!secretsExpanded || !accessToken || availableSecrets.length > 0) return;
    const fetchSecrets = async () => {
      try {
        const API_URL = apiUrl();
        const resp = await fetch(`${API_URL}/secrets/?limit=100`, {
          headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json' },
        });
        if (resp.ok) {
          const secrets = await resp.json();
          setAvailableSecrets(secrets.map((s: any) => ({ id: s.id, name: s.name, description: s.description, has_access: s.has_access !== false })));
        }
      } catch (e) {
        console.error('Failed to fetch secrets:', e);
      }
    };
    fetchSecrets();
  }, [secretsExpanded, accessToken]);

  // Fetch processing history for selected files
  useEffect(() => {
    const fetchProcessingHistory = async () => {
      if (!isInputNode || !data.selectedFiles || data.selectedFiles.length === 0 || !accessToken) {
        return;
      }

      setLoadingHistory(true);

      try {
        // Fetch history for first selected file (as an example)
        const firstFile = data.selectedFiles[0];
        const API_URL = apiUrl();

        const response = await fetch(
          `${API_URL}/assets/${firstFile.id}/processing-history`,
          {
            headers: {
              Authorization: `Bearer ${accessToken}`,
              Accept: "application/json",
            },
          }
        );

        if (response.ok) {
          const historyData = await response.json();
          setProcessingHistory(historyData);
        }
      } catch (error) {
        console.error("Error fetching processing history:", error);
      } finally {
        setLoadingHistory(false);
      }
    };

    fetchProcessingHistory();
  }, [isInputNode, data.selectedFiles, accessToken]);

  // Fetch execution logs when node has a status (ran or is running)
  // Throws on transient failure of the primary detailed-log fetch so the shared
  // poller can back off; container logs stay best-effort. On-demand caller uses
  // .catch().
  const fetchExecutionInfo = useCallback(async () => {
    // Find the latest workflow run that contains this node
    const runId = pollingRunId || (window as any).__lastWorkflowRunId;
    if (!accessToken || !runId) return;
    const API_URL = apiUrl();

    try {
      setLoadingLogs(true);
      const resp = await fetch(`${API_URL}/road/workflow-runs/${runId}/detailed-log`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const err = pollErrorForResponse(resp);
      if (err) throw err;

      const detail = await resp.json();
      const nodeInfo = (detail.nodes || []).find((n: any) => n.node_id === node.id);
      if (nodeInfo) {
        setExecutionInfo(nodeInfo);

        // Fetch container logs if this is a worker task
        if (nodeInfo.worker_task?.task_id) {
          try {
            const logResp = await fetch(`${API_URL}/workers/tasks/${nodeInfo.worker_task.task_id}/container-logs`, {
              headers: { Authorization: `Bearer ${accessToken}` },
            });
            if (logResp.ok) {
              const logData = await logResp.json();
              setContainerLogs(logData.lines || []);
            }
          } catch (e) {
            // Container logs optional
          }
        }
      }
    } finally {
      setLoadingLogs(false);
    }
  }, [pollingRunId, node.id, accessToken]);

  // One-shot fetch whenever the panel's node/run/status changes (best-effort).
  useEffect(() => {
    const effectiveRunId = pollingRunId || (window as any).__lastWorkflowRunId;
    if (!accessToken || (!data.status && !effectiveRunId)) return;
    fetchExecutionInfo().catch(() => {});
  }, [data.status, pollingRunId, node.id, accessToken, fetchExecutionInfo]);

  // Poll during active execution (3s base) through the shared helper.
  usePollingBackoff(
    fetchExecutionInfo,
    { baseMs: 3000, enabled: !!(pollingRunId && data.status === 'running' && accessToken) },
    [pollingRunId, data.status, node.id, accessToken],
  );

  const handleOpenFileBrowser = (isMetadata: boolean = false) => {
    // Trigger custom event to open file browser modal
    const inputType = isMetadata ? undefined : data.output_schema?.output_type;
    const allowMultiple = isMetadata ? true : (data.output_schema?.file_mode !== 'single');

    const event = new CustomEvent('openFileBrowser', {
      detail: {
        nodeId: node.id,
        inputType,
        allowMultiple,
        isMetadata
      }
    });
    window.dispatchEvent(event);
  };


  return (
    <div className="h-full flex flex-col">
      <div className="flex justify-between items-center mb-4">
        <h2 className="text-lg font-semibold">Node Details</h2>
        <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close node configuration">
          <X className="h-4 w-4" />
        </Button>
      </div>

      {/* FX.91 p2b [U] — Network access: the node's DECLARED outbound domains
          (p2a `egress_domains`) plus THIS tenant's approval state (p2c
          `egress_approval`, hydrated from the live-def fetch above). Read-only
          for non tenant admins; a tenant admin also gets the two-step Approve
          that hits the existing p2c POST /nodes/{id}/egress-approval. */}
      {egressInfo && (
        <div className="p-3 bg-orange-50 rounded-lg">
          <div className="flex items-center gap-2 mb-2">
            <Globe className="h-5 w-5 text-orange-600" />
            <div className="font-medium text-sm">Network access</div>
          </div>
          {egressInfo.declared.length === 0 ? (
            <div className="text-xs text-muted-foreground">
              Declares no external hosts — its container runs on the internal-only,
              zero-outbound network (default-deny).
            </div>
          ) : (
            <>
              <div className="text-xs text-muted-foreground">
                Declares {egressInfo.declared.length} external host(s):
              </div>
              <ul className="mt-1 space-y-0.5">
                {egressInfo.declared.map((d: string) => (
                  <li key={d} className="text-xs text-muted-foreground">
                    <span className="font-mono">{d}</span>
                  </li>
                ))}
              </ul>
              <div className="mt-1 text-xs text-muted-foreground">
                Approval: {egressApprovalLabel(egressInfo.approval)}
                {egressInfo.approval?.gov_blocked ? (
                  <span className="text-red-700 font-semibold">
                    {" "}(blocked by government profile — not on the install's egress allowlist; a tenant approval does not change this)
                  </span>
                ) : null}
              </div>
            </>
          )}
          {egressInfo.declared.length > 0 && isTenantAdminUser && (
            <Button
              className="bg-orange-100 text-orange-800"
              onClick={egressApprovalArmed ? approveNodeEgress : () => setEgressApprovalArmed(true)}
              disabled={egressInfo.approval?.state === 'approved' || egressApprovalBusy}
            >
              {egressApprovalBusy && <Loader2 className="h-4 w-4 animate-spin" />}
              {egressApprovalArmed ? 'Confirm approve network access' : 'Approve network access'}
            </Button>
          )}
          {egressApprovalResult && (
            <div className="mt-1 text-xs text-muted-foreground">{egressApprovalResult}</div>
          )}
        </div>
      )}

      {/* Run control for this node on the selected run (Airflow-style). */}
      {selectedRunId != null && (onRestartFromNode || onPauseNode) && (
        <div className="flex flex-wrap gap-2 mb-4">
          {onRestartFromNode && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => onRestartFromNode(node.id)}
              title="Start a new run reusing unchanged upstream results and re-running this node + everything downstream (picks up your edits here)."
            >
              Restart from here
            </Button>
          )}
          {isPaused ? (
            onResumeNode && (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => onResumeNode(node.id)}
                title="Clear this pause point — the run continues past this node."
              >
                Resume here
              </Button>
            )
          ) : (
            onPauseNode && (
              <Button
                size="sm"
                variant="secondary"
                onClick={() => onPauseNode(node.id)}
                title="Pause the run when it reaches this node — it stops and waits here until you resume."
              >
                Pause here
              </Button>
            )
          )}
        </div>
      )}

      <div className="space-y-4 flex-1 overflow-y-auto pr-2">
        <div className="space-y-2">
          <Label htmlFor="label" className="font-semibold">Node Type</Label>
          <Input id="label" value={data.label || ""} readOnly className="bg-gray-100" />
        </div>

        <div className="space-y-2">
          <Label htmlFor="nickname" className="font-semibold">Nickname</Label>
          <Input
            id="nickname"
            value={data.nickname || ""}
            onChange={(e) => updateNodeData(node.id, { ...data, nickname: e.target.value })}
            placeholder="e.g., Hero Image Resize, Final Export..."
          />
        </div>

        {/* generalized-stack-wake D12 — tie THIS node instance to a managed
            stack (or leave it self-contained). Selecting a stack sets
            data.requires_stack, which persists into the workflow graph on save
            and drives the on-node overlay badge (taking precedence over the
            node_kind auto-binding). "None" clears it. */}
        <div className="space-y-2">
          <Label htmlFor="requires-stack" className="font-semibold">Managed stack</Label>
          <select
            id="requires-stack"
            data-testid="requires-stack-select"
            className="w-full text-sm rounded-md border bg-transparent px-3 py-2"
            value={currentRequiresStack}
            onChange={(e) => {
              const val = e.target.value;
              const next: any = { ...data };
              if (val) {
                next.requires_stack = val;
              } else {
                delete next.requires_stack;
              }
              updateNodeData(node.id, next);
            }}
          >
            <option value="">None (self-contained)</option>
            {/* Keep the current assignment selectable even if it's not (yet) in
                the fetched list, so an assignment to an undeployed/removed stack
                still shows rather than silently reverting to None. */}
            {currentRequiresStack &&
              !managedStacks.some((s) => s.name === currentRequiresStack) && (
                <option value={currentRequiresStack}>{currentRequiresStack} (unavailable)</option>
              )}
            {managedStacks.map((s) => (
              <option key={s.name} value={s.name}>
                {s.status ? `${s.name} — ${s.status}` : s.name}
              </option>
            ))}
          </select>
          {managedStacksError && (
            <p className="text-xs text-red-600">{managedStacksError}</p>
          )}
          <p className="text-xs text-muted-foreground">
            Tie this node to a managed (persistent) stack, or leave it
            self-contained. The node badge shows the stack&apos;s live state.
          </p>
        </div>

        {/* node-version-pinning — this node runs a pinned immutable build. Show
            the current version and let the user opt in to a newer one. Updating
            just rewrites pinnedNodeVersionId in the graph; it takes effect on
            the next run (create_workflow_run honors the changed pin). */}
        {typeof (data as any).currentNodeVersion === 'number' && (
          <div className="space-y-2">
            <Label className="font-semibold">Version</Label>
            {(data as any).pinnedNodeVersionId != null &&
             (data as any).currentNodeVersionId != null &&
             (data as any).pinnedNodeVersionId !== (data as any).currentNodeVersionId ? (
              <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 space-y-2">
                <div className="text-xs text-amber-700">
                  An updated build (v{(data as any).currentNodeVersion}) is available.
                  This node stays on its pinned build until you update.
                </div>
                <button
                  type="button"
                  onClick={() =>
                    updateNodeData(node.id, {
                      ...data,
                      pinnedNodeVersionId: (data as any).currentNodeVersionId,
                    })
                  }
                  className="text-xs font-semibold px-2.5 py-1 rounded bg-amber-600 text-white hover:bg-amber-700"
                >
                  Update to v{(data as any).currentNodeVersion}
                </button>
              </div>
            ) : (
              <div className="text-xs text-gray-500">
                Pinned to the current build (v{(data as any).currentNodeVersion}).
              </div>
            )}
          </div>
        )}

        {isApprovalNode && data.status === 'pending_action' && (
          <div className="space-y-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-3">
            <p className="text-sm font-semibold text-amber-800">
              This run is waiting on you
            </p>
            <p className="text-xs text-amber-700">
              {(data as any).instructions || 'Approve or reject to continue.'}
            </p>
            <button
              type="button"
              className="w-full text-xs font-medium px-3 py-2 rounded bg-amber-500 text-white hover:bg-amber-600"
              onClick={() => window.dispatchEvent(new CustomEvent('mod:approvals:open'))}
            >
              Review &amp; approve
            </button>
            <p className="text-[10px] text-amber-700/80">
              Opens {wbNavLabel('wb-nav-approvals', 'Pending Approvals')}, where the decision and its notes are made.
            </p>
          </div>
        )}

        {isApprovalNode && (
          <div className="space-y-4 rounded-md border border-amber-500/30 bg-amber-500/5 p-3">
            <div className="space-y-2">
              <Label htmlFor="approval-instructions" className="font-semibold">
                What is being approved?
              </Label>
              <Textarea
                id="approval-instructions"
                value={(data as any).instructions || ""}
                onChange={(e) => updateNodeData(node.id, { ...data, instructions: e.target.value })}
                placeholder="e.g., Confirm the QA pass before delivery"
              />
              <p className="text-xs text-muted-foreground">
                Shown to the approver and in the notification.
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="approval-assignee" className="font-semibold">Approver</Label>
              <select
                id="approval-assignee"
                className="w-full text-sm rounded-md border bg-transparent px-3 py-2"
                value={(data as any).assigned_to_user_id ?? ""}
                onChange={(e) => updateNodeData(node.id, {
                  ...data,
                  assigned_to_user_id: e.target.value ? parseInt(e.target.value, 10) : null,
                })}
              >
                <option value="">Whoever runs the workflow</option>
                {assignableUsers.map((u) => (
                  <option key={u.id} value={u.id}>{u.label}</option>
                ))}
              </select>
              {assigneeLoadError && (
                <p className="text-xs text-red-600">{assigneeLoadError}</p>
              )}
              <p className="text-xs text-muted-foreground">
                The run pauses here until this person approves or rejects. No
                worker is held while it waits.
              </p>
            </div>
          </div>
        )}

        <div className="space-y-2">
          <Label htmlFor="description" className="font-semibold">Description</Label>
          <Textarea
            id="description"
            value={data.description || ""}
            readOnly
            className="bg-gray-100"
            placeholder="No description available."
          />
        </div>

        <div className="flex items-center space-x-2">
            <Label className="font-semibold">Process Type:</Label>
            <Badge variant="secondary">{data.process_type || "N/A"}</Badge>
        </div>

        {data.accepted_formats && data.accepted_formats.length > 0 && (
        <div className="space-y-1">
            <Label className="font-semibold text-xs">Accepted Formats:</Label>
            <div className="flex flex-wrap gap-1">
                {data.accepted_formats.map((fmt: string) => (
                    <Badge key={fmt} variant="outline" className="text-[10px] uppercase">{fmt}</Badge>
                ))}
            </div>
            <p className="text-[10px] text-muted-foreground">Other formats will be auto-converted</p>
        </div>
        )}

        {/* LDF-3 §3.5 — Pricing section, above the inputs/outputs
            blocks. Surfaces the same chip pair the canvas card +
            library hover use. Always visible. */}
        <div className="p-3 border rounded-md bg-gray-50 space-y-2">
          <Label className="font-semibold text-xs uppercase tracking-wide text-muted-foreground">
            Pricing
          </Label>
          <div className="flex items-center gap-2">
            <CostChip cost={data.cost_in_credits} />
            <ProcessingChip gpuVariants={(data as any).gpu_variants} />
          </div>
          {/* GO.9 — per-accelerator verified/unverified/incompatible/
              not-recommended badges, mirrored from NodeDefinition.variant_status. */}
          {(data as any).variant_status && (
            <VariantStatusBadges variantStatus={(data as any).variant_status} />
          )}
        </div>

        {/* wizard-rebuild §5 — drift reference + per-variant verdict
            chips. Visible only when the Node has more than one
            variant in gpu_variants (single-variant Nodes have no
            drift to compare). */}
        {Object.keys((data as any).gpu_variants || {}).length > 1 && (
          <DriftReferenceSection
            nodeDefinitionId={(data as any).nodeDefinitionId || null}
            accessToken={accessToken}
          />
        )}

        {/* Node-specific configuration */}
        {isConfigurable && (
          <div className="space-y-2 border-t pt-4 mt-4">
            <Label htmlFor="outputCount" className="font-semibold">Number of Outputs</Label>
            <Input
              id="outputCount"
              type="number"
              value={data.outputCount || (node.type === 'switch' ? 3 : 2)}
              onChange={(e) => {
                const count = parseInt(e.target.value, 10);
                if (!isNaN(count) && count > 0) {
                  // Update the master node state
                  updateNodeData(node.id, { ...data, outputCount: count });
                }
              }}
              min="1"
            />
            <p className="text-xs text-muted-foreground">
              Sets the number of output paths for this node.
            </p>
          </div>
        )}

        {isInputNode && (
          <>
            <div className="space-y-2 border-t pt-4 mt-4">
              <Label className="font-semibold">Select Files</Label>
              <div className="flex flex-col gap-2">
                  <Button
                    variant="outline"
                    onClick={() => handleOpenFileBrowser(false)}
                  >
                    Choose Files
                  </Button>
                  {data.selectedFiles && data.selectedFiles.length > 0 && (
                    <div className="space-y-1">
                      <span className="text-xs text-muted-foreground">Selected files:</span>
                      <div className="flex flex-wrap gap-1">
                        {data.selectedFiles.map((file: any, index: number) => (
                          <Badge key={file.id || index} variant="secondary" className="text-xs">
                            {file.name}
                          </Badge>
                        ))}
                      </div>
                    </div>
                  )}
                  {(!data.selectedFiles || data.selectedFiles.length === 0) && (
                    <span className="text-sm text-muted-foreground">
                      No files selected
                    </span>
                  )}
              </div>
            </div>

            <div className="space-y-2 border-t pt-4 mt-4">
              <Label className="font-semibold">Select Metadata Files</Label>
              <div className="flex flex-col gap-2">
                  <Button
                    variant="outline"
                    onClick={() => handleOpenFileBrowser(true)}
                  >
                    Choose Metadata
                  </Button>
                  {data.selectedMetadata && data.selectedMetadata.length > 0 && (
                    <div className="space-y-1">
                      <span className="text-xs text-muted-foreground">Selected metadata files:</span>
                      <div className="flex flex-wrap gap-1">
                        {data.selectedMetadata.map((file: any, index: number) => (
                          <Badge key={file.id || index} variant="secondary" className="text-xs">
                            {file.name}
                          </Badge>
                        ))}
                      </div>
                    </div>
                  )}
                  {(!data.selectedMetadata || data.selectedMetadata.length === 0) && (
                    <span className="text-sm text-muted-foreground">
                      No metadata files selected
                    </span>
                  )}
              </div>
            </div>
          </>
        )}

        {/* Secrets — only shown when the node definition declares accepted_secrets */}
        {!isInputNode && acceptedSecrets.length > 0 && (
          <div className="border-t pt-3 mt-3">
            <button
              onClick={() => setSecretsExpanded(!secretsExpanded)}
              className="flex items-center justify-between w-full text-sm font-semibold text-gray-700 mb-2"
            >
              <span className="flex items-center gap-2">
                <KeyRound className="w-4 h-4" />
                Secrets
                {secretRefs.filter(r => r.secret_name || r.oauth_connection_id != null).length > 0 && (
                  <Badge variant="secondary" className="text-[10px] px-1.5">
                    {secretRefs.filter(r => r.secret_name || r.oauth_connection_id != null).length}/{acceptedSecrets.length}
                  </Badge>
                )}
              </span>
              {secretsExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
            </button>

            {secretsExpanded && (
              <div className="space-y-2">
                <p className="text-xs text-muted-foreground">
                  This node accepts secrets as environment variables. Select which secrets to provide.
                </p>

                {acceptedSecrets.map((slot) => {
                  const existing = secretRefs.find(r => r.env_var === slot.env_var);
                  const selectedSecret = availableSecrets.find(s => s.name === existing?.secret_name);
                  // The user picked a secret they can't use — warn now, not at run time.
                  const noAccess = !!selectedSecret && selectedSecret.has_access === false;
                  const alreadyRequested = !!selectedSecret && requestedSecrets.has(selectedSecret.name);
                  return (
                    <div key={slot.env_var} className="space-y-1">
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-mono text-gray-600 w-[40%] truncate" title={slot.env_var}>
                          {slot.env_var}
                          {slot.required && <span className="text-red-500 ml-0.5">*</span>}
                        </span>
                        <span className="text-xs text-gray-400">&larr;</span>
                        {slot.oauth_providers?.length ? (
                          <OAuthConnectionPicker
                            providers={slot.oauth_providers}
                            envVar={slot.env_var}
                            value={existing?.oauth_connection_id ?? null}
                            onChange={(connectionId) => {
                              // Replace this slot's ref (dropping any named-secret
                              // ref for the same env_var); null = "None".
                              const filtered = secretRefs.filter(r => r.env_var !== slot.env_var);
                              if (connectionId != null) {
                                filtered.push({ env_var: slot.env_var, oauth_connection_id: connectionId });
                              }
                              updateNodeData(node.id, { ...data, secret_refs: filtered });
                            }}
                            accessToken={accessToken}
                          />
                        ) : (
                        <Select
                          value={existing?.secret_name || ""}
                          onValueChange={(val) => {
                            const filtered = secretRefs.filter(r => r.env_var !== slot.env_var);
                            if (val && val !== "_none") {
                              filtered.push({ secret_name: val, env_var: slot.env_var });
                            }
                            updateNodeData(node.id, { ...data, secret_refs: filtered });
                          }}
                        >
                          <SelectTrigger aria-label={`Secret for ${slot.env_var}`} className={`w-[50%] h-8 text-xs ${noAccess ? 'border-amber-400' : ''}`}>
                            <SelectValue placeholder="None" />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="_none">None</SelectItem>
                            {availableSecrets.map((s) => (
                              <SelectItem key={s.id} value={s.name}>
                                {s.name}{s.has_access === false ? ' 🔒' : ''}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                        )}
                      </div>
                      {noAccess && (
                        <div className="ml-1 flex items-center gap-2 text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-2 py-1">
                          <span>You don&apos;t have access to <span className="font-mono">{selectedSecret!.name}</span>. It won&apos;t work until the owner grants it.</span>
                          {alreadyRequested ? (
                            <span className="font-medium text-amber-800 whitespace-nowrap">Requested ✓</span>
                          ) : (
                            <button
                              type="button"
                              className="font-medium text-primary hover:underline whitespace-nowrap"
                              onClick={() => requestSecretAccess(selectedSecret!.id, selectedSecret!.name)}
                            >
                              Request access
                            </button>
                          )}
                        </div>
                      )}
                      {slot.description && (
                        <p className="text-[10px] text-muted-foreground ml-1">{slot.description}</p>
                      )}
                    </div>
                  );
                })}

                {/* Save & Refresh button — triggers catalog/model refresh after secrets are configured */}
                <RefreshModelsButton
                  secretRefs={data.secret_refs || []}
                  accessToken={accessToken}
                  onModelsLoaded={(models) => {
                    updateNodeData(node.id, { ...data, _nimModels: models, _refreshKey: Date.now() });
                  }}
                  onError={(msg) => {
                    updateNodeData(node.id, { ...data, _refreshKey: Date.now() });
                  }}
                />
              </div>
            )}
          </div>
        )}

        <div className="border-t border-gray-200 my-4"></div>

        {/* User-friendly schema display with interactive controls */}
        <FriendlySchemaDisplay
          inputSchema={data.input_schema}
          outputSchema={data.output_schema}
          configSchema={data.configuration_schema}
          metadata={data.custom_metadata}
          extraVariableValues={data.extraVariables || {}}
          onExtraVariableChange={(key, value, batchUpdates) => {
            const merged = {
              ...(data.extraVariables || {}),
              [key]: value,
              ...(batchUpdates || {}),
            };
            updateNodeData(node.id, {
              ...data,
              extraVariables: merged,
            });
          }}
          accessToken={accessToken}
          cachedModels={data._nimModels}
        />

        {/* Processing History */}
        {isInputNode && data.selectedFiles && data.selectedFiles.length > 0 && (
          <>
            <div className="border-t border-gray-200 my-4"></div>
            <div className="space-y-2">
              <Label className="font-semibold flex items-center gap-2">
                <Activity className="h-4 w-4" />
                Processing History
              </Label>

              {loadingHistory && (
                <div className="p-4 border rounded-md bg-gray-50 text-sm text-center text-gray-500">
                  Loading history...
                </div>
              )}

              {!loadingHistory && processingHistory && (
                <div className="p-3 border rounded-md bg-gray-50 text-xs space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="font-semibold text-gray-700">
                      {processingHistory.original_filename}
                    </span>
                  </div>

                  <div className={`grid ${CREDITS_UI_ENABLED ? 'grid-cols-3' : 'grid-cols-2'} gap-2`}>
                    {CREDITS_UI_ENABLED && (
                    <div>
                      <div className="text-gray-500">Total Cost</div>
                      <div className="flex items-center gap-1 font-semibold">
                        <DollarSign className="h-3 w-3" />
                        {processingHistory.total_processing_cost} credits
                      </div>
                    </div>
                    )}
                    <div>
                      <div className="text-gray-500">Workflows</div>
                      <div className="font-semibold">{processingHistory.processing_count}</div>
                    </div>
                    <div>
                      <div className="text-gray-500">Last Processed</div>
                      <div className="flex items-center gap-1 font-semibold">
                        <Clock className="h-3 w-3" />
                        {processingHistory.last_processed_at
                          ? new Date(processingHistory.last_processed_at).toLocaleDateString()
                          : "Never"}
                      </div>
                    </div>
                  </div>

                  {processingHistory.processing_history &&
                    processingHistory.processing_history.length > 0 && (
                      <div className="space-y-2 pt-2 border-t">
                        <div className="font-semibold text-gray-700">Recent Workflows:</div>
                        {processingHistory.processing_history.slice(0, 3).map((run: any, idx: number) => (
                          <div key={idx} className="pl-2 border-l-2 border-blue-300 space-y-1">
                            <div className="flex items-center justify-between">
                              <span className="font-medium">{run.workflow_name || "Unnamed"}</span>
                              {CREDITS_UI_ENABLED && (
                              <Badge variant="secondary" className="text-xs">
                                {run.total_cost} credits
                              </Badge>
                              )}
                            </div>
                            <div className="text-gray-500">
                              {new Date(run.executed_at).toLocaleString()}
                            </div>
                            {run.nodes_executed && run.nodes_executed.length > 0 && (
                              <div className="text-gray-600">
                                {run.nodes_executed.length} nodes executed
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}

                  {processingHistory.derived_assets &&
                    processingHistory.derived_assets.length > 0 && (
                      <div className="space-y-1 pt-2 border-t">
                        <div className="font-semibold text-gray-700">Derived Assets:</div>
                        <div className="text-gray-600">
                          {processingHistory.derived_assets.length} file(s) created from this asset
                        </div>
                      </div>
                    )}
                </div>
              )}

              {!loadingHistory && !processingHistory && (
                <div className="p-4 border rounded-md bg-gray-50 text-sm text-center text-gray-500">
                  No processing history available
                </div>
              )}
            </div>
          </>
        )}

      {/* Execution Logs Section — shown for any node that has run data */}
      {(data.status || executionInfo) && (
        <>
          <div className="border-t pt-3 mt-3">
            <button
              onClick={() => setLogsExpanded(!logsExpanded)}
              className="flex items-center justify-between w-full text-sm font-semibold text-gray-700 mb-2"
            >
              <span className="flex items-center gap-2">
                <Activity className="w-4 h-4" />
                Execution Logs
              </span>
              {logsExpanded ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
            </button>

            {logsExpanded && (
              <div className="space-y-2">
                {/* Status + Duration */}
                <div className="flex items-center gap-2 text-xs">
                  <Badge variant={
                    data.status === 'success' ? 'default' :
                    data.status === 'failure' ? 'destructive' :
                    data.status === 'blocked' ? 'destructive' :
                    data.status === 'running' ? 'secondary' : 'outline'
                  }>
                    {data.status === 'blocked' ? 'blocked (upstream failed)' : data.status || 'unknown'}
                  </Badge>
                  {executionInfo?.duration_s != null && (
                    <span className="text-gray-500">{executionInfo.duration_s}s</span>
                  )}
                  {executionInfo?.type === 'passthrough' && (
                    <span className="text-gray-400">Passthrough</span>
                  )}
                </div>

                {/* Worker info */}
                {executionInfo?.worker_task && (
                  <div className="text-xs text-gray-500 space-y-0.5">
                    <div><span className="font-medium">Worker:</span> {executionInfo.worker_task.worker_name}</div>
                    <div><span className="font-medium">Task:</span> #{executionInfo.worker_task.task_id}</div>
                    {executionInfo.worker_task.processing_image && (
                      <div className="truncate"><span className="font-medium">Image:</span> {executionInfo.worker_task.processing_image}</div>
                    )}
                    <EgressBadge egress={executionInfo.worker_task.egress} />
                  </div>
                )}

                {/* Phase timing bar */}
                {executionInfo?.worker_task?.phases && (() => {
                  const phases = executionInfo.worker_task.phases;
                  const entries = Object.entries(phases).filter(([_, v]: any) => v > 0) as [string, number][];
                  if (entries.length === 0) return null;
                  const total = entries.reduce((sum, [_, v]) => sum + v, 0);
                  const colors: Record<string, string> = {
                    queue_s: 'bg-yellow-400', download_s: 'bg-blue-400', pull_s: 'bg-purple-400',
                    process_s: 'bg-green-500', upload_s: 'bg-indigo-400',
                  };
                  const labels: Record<string, string> = {
                    queue_s: 'Queue', download_s: 'Download', pull_s: 'Pull',
                    process_s: 'Process', upload_s: 'Upload',
                  };
                  return (
                    <div>
                      <div className="flex h-4 rounded overflow-hidden">
                        {entries.map(([phase, duration]) => {
                          const pct = Math.max((duration / total) * 100, 3);
                          return (
                            <div
                              key={phase}
                              className={`${colors[phase] || 'bg-gray-300'} relative`}
                              style={{ width: `${pct}%` }}
                              title={`${labels[phase] || phase}: ${duration}s`}
                            />
                          );
                        })}
                      </div>
                      <div className="flex flex-wrap gap-x-2 gap-y-0.5 mt-1 text-[10px] text-gray-500">
                        {entries.map(([phase, duration]) => (
                          <span key={phase} className="flex items-center gap-0.5">
                            <span className={`w-1.5 h-1.5 rounded-sm ${colors[phase] || 'bg-gray-300'}`}></span>
                            {labels[phase] || phase}: {duration}s
                          </span>
                        ))}
                      </div>
                    </div>
                  );
                })()}

                {/* Container Logs */}
                {loadingLogs && (
                  <div className="text-xs text-gray-400 animate-pulse">Loading logs...</div>
                )}
                {containerLogs.length > 0 && (
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">Container logs</span>
                      <CopyLogButton
                        getText={() => containerLogs.join('\n')}
                        title="Copy the container logs to the clipboard"
                      />
                    </div>
                    <pre className="p-2 bg-gray-900 text-green-400 text-[10px] rounded max-h-48 overflow-y-auto font-mono whitespace-pre-wrap leading-relaxed">
                      {containerLogs.join('\n')}
                    </pre>
                  </div>
                )}
                {!loadingLogs && containerLogs.length === 0 && executionInfo?.worker_task && (
                  <div className="text-xs text-gray-400">No container logs available</div>
                )}

                {/* Error message */}
                {data.status === 'failure' && executionInfo?.worker_task?.log_tail && (
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-[10px] font-semibold uppercase tracking-wide text-red-600">Error log</span>
                      <CopyLogButton
                        getText={() => executionInfo.worker_task.log_tail}
                        title="Copy the error log to the clipboard"
                      />
                    </div>
                    <pre className="p-2 bg-red-50 text-red-700 text-[10px] rounded max-h-32 overflow-y-auto font-mono whitespace-pre-wrap border border-red-200">
                      {executionInfo.worker_task.log_tail}
                    </pre>
                  </div>
                )}
              </div>
            )}
          </div>
        </>
      )}

      </div>
    </div>
  );
}