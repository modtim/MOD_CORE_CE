"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";

interface NumberedEditorProps {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  className?: string;
  /** Minimum height in Tailwind utility form, e.g., "min-h-[200px]" */
  minHeight?: string;
  /** Max height before the editor scrolls internally. Defaults to
   *  "max-h-[480px]" so a long paste gets a visible scrollbar rather
   *  than pushing the wizard's Next/Cancel buttons off-screen. */
  maxHeight?: string;
  /** Line number the syntax check flagged — highlighted yellow in the gutter. */
  errorLine?: number | null;
  disabled?: boolean;
  /** Forwarded to the underlying textarea. Used by the New Node wizard
   *  for paste-driven language detection (#18, launch-day-followup-2). */
  onPaste?: (e: React.ClipboardEvent<HTMLTextAreaElement>) => void;
}

/**
 * Minimal textarea + synced line-number gutter. No syntax highlighting,
 * no keyboard wizardry — just so a "Syntax error — line 7" message is
 * actually findable in a multi-screen paste.
 *
 * Why not Monaco/CodeMirror: not installed in the app's bundle and a
 * 600KB drop just for gutter numbers is overkill. The gutter is a
 * sibling div whose scrollTop mirrors the textarea's. Works across
 * every browser we care about.
 */
export function NumberedEditor({
  value,
  onChange,
  placeholder,
  className = "",
  minHeight = "min-h-[320px]",
  maxHeight = "max-h-[480px]",
  errorLine,
  disabled,
  onPaste,
}: NumberedEditorProps) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const gutterInnerRef = useRef<HTMLDivElement | null>(null);
  // Count newlines; +1 because an empty buffer is still "line 1".
  const lineCount = Math.max(1, value.split("\n").length);
  const [lines, setLines] = useState<number[]>(() =>
    Array.from({ length: lineCount }, (_, i) => i + 1)
  );

  useEffect(() => {
    setLines(Array.from({ length: lineCount }, (_, i) => i + 1));
  }, [lineCount]);

  // Sync scroll between gutter and textarea via CSS translate. We can't
  // use `gutter.scrollTop = textarea.scrollTop` because the gutter is
  // overflow:hidden (scrollbar would ruin the layout) and you can't set
  // scrollTop on an overflow:hidden element. translateY works on any
  // element and keeps the line numbers locked to their code lines.
  useLayoutEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    const onScroll = () => {
      if (gutterInnerRef.current) {
        gutterInnerRef.current.style.transform = `translateY(-${ta.scrollTop}px)`;
      }
    };
    ta.addEventListener("scroll", onScroll, { passive: true });
    return () => ta.removeEventListener("scroll", onScroll);
  }, []);

  // When an error line is set, scroll it into view so the user doesn't
  // have to hunt for the flagged number in a long paste.
  useEffect(() => {
    if (!errorLine || !textareaRef.current) return;
    const ta = textareaRef.current;
    const cs = window.getComputedStyle(ta);
    const lineHeight = parseFloat(cs.lineHeight || "20");
    ta.scrollTop = Math.max(0, (errorLine - 2) * lineHeight);
    if (gutterInnerRef.current) {
      gutterInnerRef.current.style.transform = `translateY(-${ta.scrollTop}px)`;
    }
  }, [errorLine]);

  return (
    // Both children own their size via min-h + max-h classes passed in
    // from the caller. The wrapper's overflow-hidden clips anything
    // that slips past. Children share an identical size so the textarea
    // rows match the gutter's visible line-number count 1:1.
    <div className={`flex border rounded-md overflow-hidden ${className}`}>
      {/* Gutter outer — sized the same as the textarea so clipping
          occurs at the editor boundary, not at content length. */}
      <div
        className={`bg-gray-50 text-gray-400 text-right font-mono text-xs select-none overflow-hidden py-2 pr-1 pl-2 flex-shrink-0 ${minHeight} ${maxHeight}`}
        style={{ minWidth: "3rem", lineHeight: "20px" }}
        aria-hidden
      >
        <div ref={gutterInnerRef} style={{ willChange: "transform" }}>
          {lines.map((n) => (
            <div
              key={n}
              style={{ lineHeight: "20px" }}
              className={
                errorLine === n
                  ? "bg-red-100 text-red-700 font-semibold rounded-sm px-1"
                  : undefined
              }
            >
              {n}
            </div>
          ))}
        </div>
      </div>
      {/* Textarea — same min-h/max-h as the gutter so the visible
          textarea height matches the gutter's clip region. */}
      <textarea
        ref={textareaRef}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onPaste={onPaste}
        placeholder={placeholder}
        disabled={disabled}
        spellCheck={false}
        className={`flex-1 font-mono text-xs outline-none p-2 whitespace-pre bg-white resize-none ${minHeight} ${maxHeight}`}
        style={{ lineHeight: "20px", tabSize: 4, overflowY: "scroll" }}
      />
    </div>
  );
}
