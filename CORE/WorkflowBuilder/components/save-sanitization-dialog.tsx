"use client";

// Per-save sanitization prompt (issue #21). Surfaces when the user
// hits Save on a workflow whose nodes carry per-run user input
// (uploaded files, parameter overrides, flag selections, etc.).
// Three exits:
//   • Save as-is — current behaviour; stamps a per-workflow
//     suppression flag so the prompt doesn't re-fire on subsequent
//     saves UNLESS the user-input set materially changes.
//   • Save a clean copy — strips every user-input key from each
//     node's `data` and saves under "<name> (clean)" as a fresh
//     workflow; the original is untouched.
//   • Cancel — closes the dialog, no save dispatched.

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

interface Props {
  open: boolean;
  /** How many nodes carry at least one user-input key. Drives the
   *  description copy ("3 nodes carry your personal inputs…"). */
  affectedCount: number;
  onSaveAsIs: () => void;
  onSaveCleanCopy: () => void;
  onCancel: () => void;
  /** Disable buttons while a save call is in flight so the user
   *  can't double-click. */
  busy?: boolean;
}

export default function SaveSanitizationDialog({
  open,
  affectedCount,
  onSaveAsIs,
  onSaveCleanCopy,
  onCancel,
  busy = false,
}: Props) {
  return (
    <Dialog open={open} onOpenChange={(v) => !v && !busy && onCancel()}>
      <DialogContent className="max-w-[520px]">
        <DialogHeader>
          <DialogTitle>Save workflow with personal data?</DialogTitle>
          <DialogDescription>
            {affectedCount === 1
              ? "1 node in this workflow carries inputs you keyed in (uploaded file, parameter override, or selected flag)."
              : `${affectedCount} nodes in this workflow carry inputs you keyed in (uploaded files, parameter overrides, or selected flags).`}
            {" "}If you plan to share or reuse this workflow as a template,
            consider saving a clean copy with those values stripped.
          </DialogDescription>
        </DialogHeader>

        <div className="text-xs text-muted-foreground space-y-1.5 border-l-2 border-amber-300 bg-amber-50 px-3 py-2 rounded-r">
          <p>
            <span className="font-semibold">Save as-is</span> keeps your inputs
            in this workflow so re-runs work without re-entering them. Won't ask
            again on this workflow unless you add new user inputs later.
          </p>
          <p>
            <span className="font-semibold">Save a clean copy</span> creates a
            new workflow named with a "(clean)" suffix that has every personal
            input stripped — safe to share.
          </p>
        </div>

        <DialogFooter className="gap-2 sm:gap-2">
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button variant="outline" onClick={onSaveCleanCopy} disabled={busy}>
            Save a clean copy
          </Button>
          <Button onClick={onSaveAsIs} disabled={busy}>
            Save as-is
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
