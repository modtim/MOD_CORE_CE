# Service builder components (Group E)

React/Next.js (App Router) components that make up the MOD stack builder —
the rebuilt Service Wizard's Services list + per-service editor + live cost
line. All are **controlled**: the parent (Group F's `stack-wizard-panel.tsx`)
owns the `ServiceSpec[]` and selection; these components render + emit intent.

Every component is strictly typed against the Group 0 contract in
[`types.ts`](./types.ts) (`ServiceSpec`, `ServiceSource`, `EstimateRequest`,
`EstimateResponse`, `blankService`) — no `any` for contract types. Each is a
named export (with a default export alias for convenience), and uses the app's
existing idioms: `authenticatedFetch` from `@/lib/api-client`, `getApiUrl` from
`@/lib/urls`, Tailwind utility classes, and `lucide-react` icons.

> **Typecheck note:** there is no `node_modules` in this checkout, so `tsc`/Jest
> cannot run here. Final typechecking happens in the Docker build — Group F
> integrates these into the panel and the `mod-workflow-builder` image build
> compiles the TypeScript.

## Components

### `ServiceList.tsx` — `ServiceList`
Left-hand list of services + "Add service". "Add" mints a fresh row via
`blankService(name)` with a collision-free default name.

```ts
interface ServiceListProps {
  services: ServiceSpec[];
  selectedIndex: number;
  onSelect: (index: number) => void;
  onAdd: (service: ServiceSpec) => void;   // receives the new blankService row
  onRemove: (index: number) => void;
}
```

### `ServiceSourcePicker.tsx` — `ServiceSourcePicker`
Picks the source kind (`image | node | git | script | site`) and collects that
kind's fields; emits an updated `ServiceSource`. Delegates the `node` kind to
`<NodePicker>`. Switching kind starts a clean source so stale fields never leak.

```ts
interface ServiceSourcePickerProps {
  source: ServiceSource;
  onChange: (source: ServiceSource) => void;
}
```

### `NodePicker.tsx` — `NodePicker`
Fetches the tenant's node library and lets the operator pick one; emits
`{ kind: "node", node_definition_id }`. Uses **`GET /node-definitions/?limit=1000&offset=0`**
(the same endpoint the node-library sidebar and graph-load fallback use).

```ts
interface NodePickerProps {
  value?: number;                            // currently selected node_definition_id
  onChange: (source: ServiceSource) => void;
}
```

### `ServiceEditor.tsx` — `ServiceEditor`
Right pane. Edits name, source (`<ServiceSourcePicker>`), env (key/value rows),
container ports, volumes, GPU (`vram_gb`), replicas, cpu / mem_gb, and the
**Expose publicly** toggle (helper text: magic URL via mesh + Caddy, drops
host-port publishing; shows an approximate URL preview). Parse-sensitive fields
use a local buffer so half-typed values aren't clobbered; the buffer resyncs
from props only on a genuine external change (e.g. a different service selected).

```ts
interface ServiceEditorProps {
  service: ServiceSpec;
  onChange: (service: ServiceSpec) => void;
}
```

### `CostLine.tsx` — `CostLine`
Live cost line. Builds the reservation vector from each service's
`cpu / mem_gb / gpu` and POSTs **`/persistent-stacks/estimate`** (debounced,
400 ms; out-of-order responses guarded). Renders per-service + total credits/hr
and projected/month. Handles the metering-disabled case (`metering_enabled:
false` → shows 0 with a "no charge" note).

```ts
interface CostLineProps {
  services: ServiceSpec[];
}
```

## Wiring (for Group F)
`ServiceList` (selection) → `ServiceEditor` for the selected service (`onChange`
writes back into the `services` array) → `CostLine` fed the whole `services`
array. `blankService` seeds new rows.
