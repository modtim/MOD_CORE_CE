"use client";

// Left-hand Services list for the stack builder.
//
// A controlled column: the parent owns the `services` array and the
// current selection; this component only renders rows + fires intent
// callbacks (select / add / remove). "Add service" mints a fresh row
// via `blankService` (Group 0 contract) with a unique default name so
// two clicks never collide.

import React from "react";
import { Plus, Trash2, Layers } from "lucide-react";
import { ServiceSpec, blankService } from "./types";

export interface ServiceListProps {
  services: ServiceSpec[];
  selectedIndex: number;
  onSelect: (index: number) => void;
  onAdd: (service: ServiceSpec) => void;
  onRemove: (index: number) => void;
}

// A default name that doesn't clash with existing rows: service-1,
// service-2, … skipping any already taken.
function nextServiceName(services: ServiceSpec[]): string {
  const taken = new Set(services.map((s) => s.name));
  let n = services.length + 1;
  let candidate = `service-${n}`;
  while (taken.has(candidate)) {
    n += 1;
    candidate = `service-${n}`;
  }
  return candidate;
}

export function ServiceList({
  services,
  selectedIndex,
  onSelect,
  onAdd,
  onRemove,
}: ServiceListProps) {
  return (
    <div className="flex flex-col h-full min-w-[180px]">
      <div className="flex items-center justify-between px-1 pb-2 flex-shrink-0">
        <span className="text-xs font-semibold uppercase tracking-wide text-gray-500">
          Services
        </span>
        <span className="text-[10px] text-gray-400">{services.length}</span>
      </div>

      <div className="flex-1 overflow-y-auto space-y-1 pr-0.5">
        {services.length === 0 ? (
          <p className="text-[11px] text-gray-400 px-1 py-2">
            No services yet. Add one to start composing the stack.
          </p>
        ) : (
          services.map((svc, i) => {
            const selected = i === selectedIndex;
            return (
              <div
                key={i}
                role="button"
                tabIndex={0}
                aria-selected={selected}
                onClick={() => onSelect(i)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onSelect(i);
                  }
                }}
                className={
                  "group flex items-center gap-2 rounded-md border px-2.5 py-2 cursor-pointer transition-colors " +
                  (selected
                    ? "border-indigo-300 bg-indigo-50"
                    : "border-gray-200 bg-white hover:bg-gray-50")
                }
              >
                <Layers
                  className={
                    "h-3.5 w-3.5 flex-shrink-0 " +
                    (selected ? "text-indigo-600" : "text-gray-400")
                  }
                  aria-hidden="true"
                />
                <span
                  className={
                    "flex-1 truncate text-xs font-medium " +
                    (selected ? "text-indigo-800" : "text-gray-700")
                  }
                  title={svc.name || "(unnamed)"}
                >
                  {svc.name || "(unnamed)"}
                </span>
                {svc.expose && (
                  <span className="inline-flex items-center rounded-full bg-emerald-100 text-emerald-700 px-1.5 py-0.5 text-[9px] font-medium">
                    URL
                  </span>
                )}
                <button
                  type="button"
                  aria-label={`Remove ${svc.name || "service"}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    onRemove(i);
                  }}
                  className="opacity-0 group-hover:opacity-100 text-gray-400 hover:text-red-600 transition-opacity flex-shrink-0"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            );
          })
        )}
      </div>

      <button
        type="button"
        onClick={() => onAdd(blankService(nextServiceName(services)))}
        className="mt-2 flex items-center justify-center gap-1.5 rounded-md border border-dashed border-gray-300 px-2.5 py-2 text-xs font-medium text-gray-600 hover:border-indigo-400 hover:text-indigo-700 transition-colors flex-shrink-0"
      >
        <Plus className="h-3.5 w-3.5" />
        Add service
      </button>
    </div>
  );
}

export default ServiceList;
