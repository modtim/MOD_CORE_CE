"use client";

/**
 * wizard-rebuild §10a — read-only View Build Plan panel.
 *
 * Renders every section of a BuildSpec's BuildPlan so users can
 * understand what their submission produced before it builds:
 *
 *   - Runtime: language + version + package manager + cuda
 *   - Hardware: gpu_required + cuda_version + variant fan-out
 *   - Install steps: ordered list with provenance deep-links
 *   - System deps: apt package chips
 *   - Env vars: name=default rows
 *   - Input/Output ports (§3 multi-port): type + name per port
 *   - Entrypoint: exec-form rendering
 *   - Applied deltas (§7): chronological history of LLM-proposed
 *     field changes that landed via plan-delta repair
 *
 * Provenance: each install_step / system_dep that came from the
 * §3c docs extraction carries an `extracted_from` chunk ref with
 * source file + line range. The panel renders that as a
 * "📖 README.md:74" deep link.
 *
 * No mutation — purely informational. The wizard's Edit Node UI
 * is where users change anything.
 */
import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, BookOpen, ShieldCheck } from "lucide-react";


type ChunkRef = {
  source_file?: string;
  start_line?: number;
  end_line?: number;
  url?: string;
};


type EnvVar = {
  name: string;
  default?: string | null;
  description?: string;
};


type Port = {
  name: string;
  type: string;
  description?: string;
};


type Delta = {
  field: string;
  value: unknown;
  reason: string;
  applied_at?: string;
};


type BuildPlan = {
  mode: string;
  language: string;
  language_version?: string | null;
  package_manager?: string | null;
  workdir?: string;
  install_steps?: string[];
  system_deps?: string[];
  env_vars?: EnvVar[];
  input_ports?: Port[];
  output_ports?: Port[];
  entrypoint?: string[];
  cuda_version?: string | null;
  gpu_required?: boolean;
  from_image_ref?: string | null;
  from_digest?: string | null;
  ports?: number[];
  confidence?: number;
  weak_fields?: string[];
  applied_deltas?: Delta[];
  // Provenance per field. The §3 extractor populates
  // `extracted_from` as a list of chunk refs covering all
  // fields. v1 panel shows one badge per chunk.
  extracted_from?: ChunkRef[];
};


export function BuildPlanPanel({ plan }: { plan: BuildPlan }) {
  const [open, setOpen] = useState<Record<string, boolean>>({
    runtime: true,
    hardware: true,
    install: true,
    env: true,
    ports: true,
    entrypoint: true,
    deltas: true,
  });

  const toggle = (k: string) =>
    setOpen((s) => ({ ...s, [k]: !s[k] }));

  const Section = ({
    id, title, children,
  }: { id: string; title: string; children: React.ReactNode }) => (
    <div className="border rounded-md mb-2 bg-card">
      <button
        type="button"
        onClick={() => toggle(id)}
        className="w-full flex items-center justify-between px-3 py-2 text-sm font-semibold hover:bg-muted/50"
      >
        <span>{title}</span>
        {open[id] ? (
          <ChevronDown className="h-4 w-4" />
        ) : (
          <ChevronRight className="h-4 w-4" />
        )}
      </button>
      {open[id] && (
        <div className="px-3 pb-3 text-sm space-y-2">{children}</div>
      )}
    </div>
  );

  const ProvenanceBadge = ({ ref }: { ref: ChunkRef }) => {
    if (!ref?.source_file) return null;
    const line = ref.start_line
      ? `:${ref.start_line}${ref.end_line && ref.end_line !== ref.start_line ? `-${ref.end_line}` : ""}`
      : "";
    const label = `${ref.source_file}${line}`;
    if (ref.url) {
      return (
        <a
          href={ref.url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-[10px] text-sky-700 hover:underline"
          title="View on GitHub"
        >
          <BookOpen className="h-3 w-3" /> {label}
        </a>
      );
    }
    return (
      <span className="inline-flex items-center gap-1 text-[10px] text-muted-foreground" title="extracted from docs">
        <BookOpen className="h-3 w-3" /> {label}
      </span>
    );
  };

  return (
    <div className="space-y-2">
      <Section id="runtime" title="Runtime">
        <dl className="grid grid-cols-[120px_1fr] gap-y-1 text-xs">
          <dt className="text-muted-foreground">mode</dt>
          <dd>{plan.mode}</dd>
          <dt className="text-muted-foreground">language</dt>
          <dd>
            {plan.language}
            {plan.language_version ? ` ${plan.language_version}` : ""}
          </dd>
          <dt className="text-muted-foreground">package manager</dt>
          <dd>{plan.package_manager || "—"}</dd>
          <dt className="text-muted-foreground">workdir</dt>
          <dd>{plan.workdir || "—"}</dd>
          {typeof plan.confidence === "number" && (
            <>
              <dt className="text-muted-foreground">plan confidence</dt>
              <dd>
                {(plan.confidence * 100).toFixed(0)}%
                {plan.weak_fields && plan.weak_fields.length > 0 && (
                  <span className="ml-2 text-amber-700 text-[10px]">
                    weak: {plan.weak_fields.join(", ")}
                  </span>
                )}
              </dd>
            </>
          )}
        </dl>
      </Section>

      <Section id="hardware" title="Hardware">
        <dl className="grid grid-cols-[120px_1fr] gap-y-1 text-xs">
          <dt className="text-muted-foreground">gpu required</dt>
          <dd>{plan.gpu_required ? "yes" : "no"}</dd>
          <dt className="text-muted-foreground">cuda version</dt>
          <dd>{plan.cuda_version || "—"}</dd>
          {plan.from_image_ref && (
            <>
              <dt className="text-muted-foreground">FROM</dt>
              <dd className="font-mono text-[11px] break-all">
                {plan.from_image_ref}
                {plan.from_digest && (
                  <span
                    className="ml-2 inline-flex items-center gap-1 text-emerald-700 text-[10px]"
                    title="Pinned digest — reproducible rebuilds"
                  >
                    <ShieldCheck className="h-3 w-3" /> pinned
                  </span>
                )}
              </dd>
            </>
          )}
        </dl>
      </Section>

      {plan.install_steps && plan.install_steps.length > 0 && (
        <Section id="install" title={`Install steps (${plan.install_steps.length})`}>
          <ol className="space-y-1 list-decimal list-inside text-xs font-mono">
            {plan.install_steps.map((s, i) => (
              <li key={i} className="break-all">
                {s}
              </li>
            ))}
          </ol>
          {plan.system_deps && plan.system_deps.length > 0 && (
            <div className="mt-2">
              <div className="text-[10px] text-muted-foreground uppercase tracking-wide mb-1">
                System deps
              </div>
              <div className="flex flex-wrap gap-1">
                {plan.system_deps.map((d) => (
                  <span
                    key={d}
                    className="text-[11px] px-1.5 py-0.5 rounded border bg-gray-50"
                  >
                    {d}
                  </span>
                ))}
              </div>
            </div>
          )}
          {plan.extracted_from && plan.extracted_from.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {plan.extracted_from.map((c, i) => (
                <ProvenanceBadge key={i} ref={c} />
              ))}
            </div>
          )}
        </Section>
      )}

      {plan.env_vars && plan.env_vars.length > 0 && (
        <Section id="env" title={`Env vars (${plan.env_vars.length})`}>
          <ul className="text-xs font-mono space-y-0.5">
            {plan.env_vars.map((e) => (
              <li key={e.name}>
                {e.name}={e.default ?? "<unset>"}
                {e.description && (
                  <span className="ml-2 text-muted-foreground">
                    — {e.description}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </Section>
      )}

      {((plan.input_ports && plan.input_ports.length > 0) ||
        (plan.output_ports && plan.output_ports.length > 0)) && (
        <Section id="ports" title="Input / output ports">
          <div className="grid grid-cols-2 gap-3 text-xs">
            <div>
              <div className="text-[10px] text-muted-foreground uppercase tracking-wide">
                Inputs
              </div>
              {(plan.input_ports || []).map((p) => (
                <div key={p.name} className="font-mono text-[11px]">
                  {p.name}: {p.type}
                </div>
              ))}
            </div>
            <div>
              <div className="text-[10px] text-muted-foreground uppercase tracking-wide">
                Outputs
              </div>
              {(plan.output_ports || []).map((p) => (
                <div key={p.name} className="font-mono text-[11px]">
                  {p.name}: {p.type}
                </div>
              ))}
            </div>
          </div>
        </Section>
      )}

      {plan.entrypoint && plan.entrypoint.length > 0 && (
        <Section id="entrypoint" title="Entrypoint">
          <div className="font-mono text-xs bg-muted/40 px-2 py-1 rounded">
            ENTRYPOINT {JSON.stringify(plan.entrypoint)}
          </div>
        </Section>
      )}

      {plan.applied_deltas && plan.applied_deltas.length > 0 && (
        <Section
          id="deltas"
          title={`Applied deltas (${plan.applied_deltas.length})`}
        >
          <ul className="text-xs space-y-1">
            {plan.applied_deltas.map((d, i) => (
              <li key={i} className="border-l-2 border-primary pl-2">
                <span className="font-mono text-[11px]">{d.field}</span>
                <span className="text-muted-foreground"> — {d.reason}</span>
                {d.applied_at && (
                  <span className="ml-2 text-[10px] text-muted-foreground">
                    {new Date(d.applied_at).toLocaleString()}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}
