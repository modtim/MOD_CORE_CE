"use client";

import React, { useCallback, useState } from "react";
import { Copy, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * CopyLogButton — a shared copy control for every raw-log / stdout / stderr /
 * container-log surface in the WorkflowBuilder, so each log view has its own
 * copy affordance (not just the Process Queue progress log). Reuses the same
 * UX as WorkflowQueueDialog's "Copy log": async Clipboard API with a legacy
 * execCommand fallback for insecure-origin / older browsers, and a brief
 * "Copied" confirmation.
 *
 * `getText` is evaluated at click time so it always copies the current log
 * text (logs may still be streaming when the button first renders).
 */
export interface CopyLogButtonProps {
  getText: string | (() => string);
  label?: string;
  copiedLabel?: string;
  className?: string;
  size?: "sm" | "default" | "lg" | "icon";
  variant?: "default" | "outline" | "secondary" | "ghost";
  title?: string;
}

export function CopyLogButton({
  getText,
  label = "Copy",
  copiedLabel = "Copied",
  className,
  size = "sm",
  variant = "outline",
  title = "Copy the full log to the clipboard",
}: CopyLogButtonProps): React.ReactElement {
  const [copied, setCopied] = useState(false);

  const onCopy = useCallback(async () => {
    const text = typeof getText === "function" ? getText() : getText;
    if (!text) return;
    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — leave the button unchanged */
    }
  }, [getText]);

  return (
    <Button
      type="button"
      variant={variant}
      size={size}
      onClick={onCopy}
      className={cn("h-7", className)}
      title={title}
    >
      {copied ? (
        <>
          <Check className="h-3.5 w-3.5 mr-1" />
          {copiedLabel}
        </>
      ) : (
        <>
          <Copy className="h-3.5 w-3.5 mr-1" />
          {label}
        </>
      )}
    </Button>
  );
}

export default CopyLogButton;
