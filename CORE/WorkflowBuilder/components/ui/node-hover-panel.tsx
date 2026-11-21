"use client";

import React, {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  ReactNode,
} from "react";
import { createPortal } from "react-dom";

/**
 * Minimal hover-reveal panel with hand-rolled positioning.
 *
 * Background: we tried five different knob combinations on Radix HoverCard
 * (`avoidCollisions=false`, `sticky="always"`, controlled open state,
 * parent-key remounts, wrapping Content in Portal) and the popover still
 * rendered far from its trigger — sometimes anchored to canvas-center —
 * inside the React Flow + ScrollArea layout. Rather than keep guessing at
 * Floating UI internals, we own the positioning ourselves.
 *
 * How it works:
 *   1. Wrap the trigger in a block-level div that we attach a ref to.
 *   2. On hover/focus, measure `trigger.getBoundingClientRect()` → x = right +
 *      sideOffset, y = top (for `side="right"`).
 *   3. Render the panel into `document.body` via Portal with
 *      `position: fixed; left: Xpx; top: Ypx`. No transform for
 *      positioning — `left`/`top` are viewport-absolute and can't be
 *      trapped by transformed ancestors the way Radix's transform-based
 *      approach could.
 *   4. Recompute on scroll (capture phase so it catches inner scrolls)
 *      and resize so the panel stays stuck to the trigger.
 *
 * Identical UX to the HoverCard it replaces: openDelay, closeDelay, stays
 * open while hovering the panel itself.
 */

// Keep at least this many px between the panel and the viewport edges.
const VIEWPORT_MARGIN = 8;

// Pull y up so a panel of height `h` fits above the viewport bottom; never
// above the top margin. A panel taller than the viewport is capped by maxH.
function clampY(top: number, h: number): number {
  const vh = window.innerHeight;
  const eff = Math.min(h, vh - VIEWPORT_MARGIN * 2);
  return Math.max(VIEWPORT_MARGIN, Math.min(top, vh - eff - VIEWPORT_MARGIN));
}

export interface NodeHoverPanelProps {
  /** The thing that triggers the hover (usually a Button). */
  trigger: ReactNode;
  /** The panel body. */
  children: ReactNode;
  /** Placement relative to the trigger. Only "right" is wired at first. */
  side?: "right";
  /** Gap between trigger and panel in px. */
  sideOffset?: number;
  /** Delay before opening on hover-enter (ms). */
  openDelay?: number;
  /** Delay before closing on hover-leave (ms). */
  closeDelay?: number;
  /** Tailwind classes on the panel container. Override w-64 default, etc. */
  panelClassName?: string;
}

export function NodeHoverPanel({
  trigger,
  children,
  side = "right",
  sideOffset = 12,
  openDelay = 250,
  closeDelay = 80,
  panelClassName = "w-80",
}: NodeHoverPanelProps) {
  const triggerRef = useRef<HTMLDivElement | null>(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [maxH, setMaxH] = useState<number | null>(null);

  const openTimer = useRef<number | undefined>(undefined);
  const closeTimer = useRef<number | undefined>(undefined);

  const cancelTimers = () => {
    if (openTimer.current) window.clearTimeout(openTimer.current);
    if (closeTimer.current) window.clearTimeout(closeTimer.current);
  };

  const scheduleOpen = () => {
    if (closeTimer.current) window.clearTimeout(closeTimer.current);
    if (open) return;
    openTimer.current = window.setTimeout(() => setOpen(true), openDelay);
  };

  const scheduleClose = () => {
    if (openTimer.current) window.clearTimeout(openTimer.current);
    closeTimer.current = window.setTimeout(() => setOpen(false), closeDelay);
  };

  // Clean up on unmount so a stranded timer can't open a popover for a
  // trigger that no longer exists (matters when the parent list re-filters
  // during search).
  useEffect(() => cancelTimers, []);

  // Compute position on open, and keep tracking on scroll/resize so the
  // panel doesn't desynchronize from its trigger as the user interacts.
  //
  // We measure the INNER TRIGGER ELEMENT (the Button) via a child query
  // rather than the wrapper div. The wrapper uses display:block w-full
  // but React Flow / flex reflows can leave it with zero width in some
  // cases; the inner Button has a reliable, Tailwind-enforced box.
  useLayoutEffect(() => {
    if (!open || !triggerRef.current) return;
    const compute = () => {
      const wrap = triggerRef.current;
      if (!wrap) return;
      const r = wrap.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) return;

      // Find the nearest ancestor that CLIPS horizontally (overflow-hidden
      // or overflow-x-hidden). That's the true VISIBLE right edge for the
      // trigger, even if the trigger's own rect overflows past it.
      //
      // Why this matters: Radix ScrollArea wraps its content in a
      // `display: table` div with `min-width: 100%`, which grows to the
      // widest intrinsic child. Node buttons with long descriptions using
      // `truncate` (which sets white-space:nowrap) have intrinsic widths
      // equal to the full un-truncated text, so after enough cards are
      // rendered the internal table balloons beyond the sidebar. The
      // ScrollArea's outer Root clips the overflow visually, but
      // getBoundingClientRect returns the overflowing width. Clamping to
      // the clipping ancestor's right edge gives us the correct
      // "next to the visible trigger" position every time.
      let clipRight = r.right;
      let cur: Element | null = wrap.parentElement;
      while (cur) {
        const cs = window.getComputedStyle(cur);
        if (cs.overflow === "hidden" || cs.overflowX === "hidden") {
          const cr = cur.getBoundingClientRect();
          if (cr.right < clipRight) clipRight = cr.right;
          break;
        }
        cur = cur.parentElement;
      }

      // Clamp vertically so a card low in the list can't push the (fixed,
      // unscrollable) panel off-screen. Height is 0 on first open (panel
      // not rendered yet); the follow-up layout effect re-clamps then.
      const h = panelRef.current?.getBoundingClientRect().height ?? 0;
      setPos({ x: clipRight + sideOffset, y: clampY(r.top, h) });
    };
    compute();
    // Use capture so inner scrollers (ScrollArea viewport, etc.) fire too.
    window.addEventListener("scroll", compute, true);
    window.addEventListener("resize", compute);
    return () => {
      window.removeEventListener("scroll", compute, true);
      window.removeEventListener("resize", compute);
    };
  }, [open, side, sideOffset]);

  // Second pass, once the panel is in the DOM: measure it, cap its height to
  // the viewport (contents then scroll inside the panel), and pull y up so
  // the bottom edge stays visible. Only re-sets pos when y moves by >1px so
  // this can't loop.
  useLayoutEffect(() => {
    if (!open || !pos || !panelRef.current) return;
    const h = panelRef.current.getBoundingClientRect().height;
    const vh = window.innerHeight;
    setMaxH(h > vh - VIEWPORT_MARGIN * 2 ? vh - VIEWPORT_MARGIN * 2 : null);
    const y = clampY(pos.y, h);
    if (Math.abs(y - pos.y) > 1) setPos({ x: pos.x, y });
  }, [open, pos?.x, pos?.y]);

  // The trigger wrapper is a block-level div so it inherits flex-column
  // full-width behavior from the library's layout. Its ref is what we
  // measure against — the inner Button's rect is effectively this div's
  // rect because we have no padding/margin.
  return (
    <div
      ref={triggerRef}
      style={{ display: "block", width: "100%" }}
      onMouseEnter={scheduleOpen}
      onMouseLeave={scheduleClose}
      onFocus={scheduleOpen}
      onBlur={scheduleClose}
    >
      {trigger}
      {open && pos && typeof document !== "undefined" &&
        createPortal(
          <div
            ref={panelRef}
            onMouseEnter={() => {
              // keep open while cursor is on the panel itself
              if (closeTimer.current) window.clearTimeout(closeTimer.current);
            }}
            onMouseLeave={scheduleClose}
            style={{
              position: "fixed",
              left: pos.x,
              top: pos.y,
              zIndex: 50,
              ...(maxH ? { maxHeight: maxH, overflowY: "auto" as const } : {}),
            }}
            className={`rounded-md border bg-popover p-4 text-popover-foreground shadow-md outline-none ${panelClassName}`}
          >
            {children}
          </div>,
          document.body,
        )}
    </div>
  );
}
