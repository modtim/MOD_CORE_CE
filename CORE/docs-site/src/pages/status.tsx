import {useEffect, useState, type JSX} from 'react';
import Layout from '@theme/Layout';
import Heading from '@theme/Heading';
import styles from './status.module.css';

/**
 * Public status page (GSEC FX.69 p3, owner decision Q71=C).
 *
 * Fetches GET /v1/public/status from the API (measured numbers only).
 * Q71=C: this page runs on the same site as MOD, so it is unavailable
 * during a site-wide outage — the footer says so and no wording here
 * promises its availability. Wording rules: "supports" / "designed to";
 * never compliant/certified/guarantee/audited; no SLA promises beyond
 * the measured numbers shown.
 *
 * Origin: the docs site and the API live on different subdomains
 * (docs.* vs api.*). The API origin is derived at runtime by swapping
 * the first host label for "api" (works on any base domain, no env
 * var needed); a same-origin attempt is made first in case the site is
 * fronted by a single origin. On total failure the page degrades to
 * "status unavailable" — it never shows fabricated state.
 * The endpoint itself sends Access-Control-Allow-Origin: * (public by
 * design, no credentials), so the cross-origin fetch succeeds.
 */

interface DailyBar {
  date: string;
  uptime_pct: number | null;
}

interface ComponentStatus {
  name: string;
  state: 'up' | 'down' | 'stale' | string;
  last_checked_at: string | null;
  latency_ms: number | null;
  uptime_90d_pct: number | null;
}

interface MaintenanceWindow {
  start_at: string;
  end_at: string;
  announced_at: string | null;
  reason: string | null;
}

interface Incident {
  component: string | null;
  kind: string;
  start_at: string;
  end_at: string;
  down_minutes: number | null;
  reason: string | null;
}

interface StatusData {
  generated_at: string;
  overall: 'operational' | 'degraded' | 'disruption' | 'unknown' | string;
  components: ComponentStatus[];
  daily_90d: Record<string, DailyBar[]>;
  maintenance: MaintenanceWindow[];
  incidents: Incident[];
}

const STATE_LABEL: Record<string, string> = {
  up: 'Operational',
  down: 'Disruption',
  stale: 'No recent data',
};

const OVERALL_LABEL: Record<string, string> = {
  operational: 'All systems operational',
  degraded: 'Partial degradation',
  disruption: 'Service disruption',
  unknown: 'Status unknown (no probe data yet)',
};

function candidateApiOrigins(): string[] {
  if (typeof window === 'undefined') return [];
  const {protocol, hostname, host} = window.location;
  const out: string[] = [];
  // 1. Same origin (single-front deployments / dev).
  if (host && !/:(\d+)$/.test(host)) out.push(`${protocol}//${host}`);
  // 2. api. subdomain derived from the current host's parent domain.
  const isIp = /^[0-9.]+$/.test(hostname) || hostname === 'localhost';
  if (!isIp) {
    const parts = hostname.split('.');
    if (parts.length >= 3 && parts[0] !== 'api') {
      out.push(`${protocol}//api.${parts.slice(1).join('.')}`);
    }
  }
  return out;
}

async function fetchStatus(): Promise<StatusData> {
  const origins = candidateApiOrigins();
  let lastErr: unknown = null;
  for (const origin of origins) {
    try {
      const resp = await fetch(`${origin}/v1/public/status`, {
        credentials: 'omit',
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      return (await resp.json()) as StatusData;
    } catch (err) {
      lastErr = err;
    }
  }
  throw lastErr ?? new Error('no API origin candidates');
}

function formatTime(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

function barClass(pct: number | null): string {
  if (pct === null) return styles['bar--none'];
  if (pct >= 99.5) return styles['bar--full'];
  if (pct >= 95) return styles['bar--warn'];
  return styles['bar--bad'];
}

function ComponentCard({
  comp,
  bars,
}: {
  comp: ComponentStatus;
  bars: DailyBar[];
}) {
  const dotClass =
    comp.state === 'up'
      ? styles['dot--up']
      : comp.state === 'down'
        ? styles['dot--down']
        : styles['dot--stale'];
  return (
    <div className={styles.componentRow} style={{display: 'block'}}>
      <div style={{display: 'flex', justifyContent: 'space-between'}}>
        <span>
          <span className={`${styles.dot} ${dotClass}`} />
          <span className={styles.componentName}>{comp.name}</span>{' '}
          <span className={styles.meta}>
            — {STATE_LABEL[comp.state] ?? comp.state}
          </span>
        </span>
        <span className={styles.meta}>
          last check {formatTime(comp.last_checked_at)}
          {comp.latency_ms !== null ? ` · ${comp.latency_ms} ms` : ''}
          {comp.uptime_90d_pct !== null
            ? ` · 90-day uptime ${comp.uptime_90d_pct.toFixed(2)}%`
            : ''}
        </span>
      </div>
      <div
        className={styles.bars}
        aria-label={`90-day uptime for ${comp.name}`}>
        {bars.map(bar => (
          <span
            key={bar.date}
            className={`${styles.bar} ${barClass(bar.uptime_pct)}`}
            title={`${bar.date}: ${
              bar.uptime_pct === null
                ? 'no data'
                : `${bar.uptime_pct.toFixed(2)}% uptime`
            }`}
          />
        ))}
      </div>
    </div>
  );
}

function StatusContent() {
  const [data, setData] = useState<StatusData | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchStatus()
      .then(d => {
        if (!cancelled) setData(d);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return (
      <div className={styles.unavailable}>
        <h2>Status unavailable</h2>
        <p>
          We could not reach the status service right now. Please try again
          later; incident details will appear here once the status service
          responds.
        </p>
      </div>
    );
  }

  if (!data) {
    return <p className={styles.muted}>Loading status…</p>;
  }

  return (
    <div>
      <div className={`${styles.banner} ${styles[`banner--${data.overall}`]}`}>
        {OVERALL_LABEL[data.overall] ?? data.overall}
        <div className={styles.muted} style={{fontWeight: 400}}>
          as of {formatTime(data.generated_at)}
        </div>
      </div>

      <section className={styles.section}>
        <Heading as="h2" className={styles.sectionTitle}>
          Components
        </Heading>
        {data.components.length === 0 ? (
          <p className={styles.muted}>No probe components recorded yet.</p>
        ) : (
          data.components.map(comp => (
            <ComponentCard
              key={comp.name}
              comp={comp}
              bars={data.daily_90d[comp.name] ?? []}
            />
          ))
        )}
      </section>

      <section className={styles.section}>
        <Heading as="h2" className={styles.sectionTitle}>
          Planned maintenance
        </Heading>
        {data.maintenance.length === 0 ? (
          <p className={styles.muted}>No announced maintenance is scheduled.</p>
        ) : (
          <ul className={styles.list}>
            {data.maintenance.map(w => (
              <li key={`${w.start_at}-${w.end_at}`} className={styles.listItem}>
                {formatTime(w.start_at)} – {formatTime(w.end_at)}
                {w.reason ? <span> — {w.reason}</span> : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className={styles.section}>
        <Heading as="h2" className={styles.sectionTitle}>
          Incident history (90 days)
        </Heading>
        {data.incidents.length === 0 ? (
          <p className={styles.muted}>
            No incidents recorded in the last 90 days.
          </p>
        ) : (
          <ul className={styles.list}>
            {data.incidents.map((inc, i) => (
              <li key={`${inc.start_at}-${i}`} className={styles.listItem}>
                <strong>
                  {inc.kind === 'maintenance'
                    ? 'Scheduled maintenance'
                    : `Disruption — ${inc.component ?? 'site'}`}
                </strong>
                : {formatTime(inc.start_at)} – {formatTime(inc.end_at)}
                {inc.down_minutes !== null ? (
                  <span className={styles.muted}>
                    {' '}
                    ({inc.down_minutes} min of measured downtime)
                  </span>
                ) : null}
                {inc.kind === 'maintenance' && inc.reason ? (
                  <span> — {inc.reason}</span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      <p className={styles.footerNote}>
        Figures shown are measured uptime from system probes and are provided
        as measured — they are not a service-level agreement. This page runs on
        the same site as MOD; like the site itself, it is not available while
        the whole site is down.
      </p>
    </div>
  );
}

export default function StatusPage(): JSX.Element {
  return (
    <Layout title="Status" description="MOD service status">
      <div className={styles.page}>
        <Heading as="h1" className={styles.title}>
          Service Status
        </Heading>
        <p className={styles.sub}>
          Live status of the MOD platform, measured service uptime, announced
          maintenance and the incident history.
        </p>
        <StatusContent />
      </div>
    </Layout>
  );
}
