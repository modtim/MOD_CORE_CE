"use client";

// GSEC FX.54 p4b — the surface a WB tab shows 2 minutes before the SSO idle
// timeout (see hooks/use-idle-warning for the scheduling). Radix alert-dialog
// primitives: role alertdialog, focus lands on "Stay signed in" (the alert
// dialog auto-focuses its Cancel element, which is the safe no-action choice
// here), Esc = Stay, and the countdown re-renders once a second.

import React, { useState } from "react";
import {
    AlertDialog,
    AlertDialogAction,
    AlertDialogCancel,
    AlertDialogContent,
    AlertDialogDescription,
    AlertDialogFooter,
    AlertDialogHeader,
    AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { formatCountdown } from "@/hooks/use-idle-warning";

interface IdleWarningDialogProps {
    // The hook drives this: open=true at deadline-120s, false on stay/sign-out.
    open?: boolean;
    // Seconds until the deadline; re-rendered every tick while open.
    secondsLeft?: number;
    // "Stay signed in" — the hook runs the existing token refresh and re-arms.
    onStay?: () => void;
    // "Sign out" — the hook runs the existing logout and disarms.
    onSignOut?: () => void;
}

export function IdleWarningDialog(props: IdleWarningDialogProps) {
    const { open = false, secondsLeft = 0, onStay, onSignOut } = props;

    const stay = () => { if (onStay) onStay(); };
    const signOut = () => { if (onSignOut) onSignOut(); };

    return (
        <AlertDialog open={open}>
            <AlertDialogContent
                onEscapeKeyDown={(event) => {
                    // Esc = Stay signed in (the safe default). Claim the event
                    // so the alert dialog never dismisses itself silently.
                    event.preventDefault();
                    stay();
                }}
            >
                <AlertDialogHeader>
                    <AlertDialogTitle>Still here?</AlertDialogTitle>
                </AlertDialogHeader>
                <AlertDialogDescription id="idle-warning-message">
                    You will be signed out in {formatCountdown(secondsLeft)}.
                </AlertDialogDescription>
                <AlertDialogFooter>
                    {/* Cancel is the element the alert dialog auto-focuses, so
                        "Stay signed in" sits here: keyboard users act without
                        hunting for the other button. */}
                    <AlertDialogCancel
                        onClick={stay}
                        aria-label="Stay signed in"
                    >
                        Stay signed in
                    </AlertDialogCancel>
                    <AlertDialogAction
                        onClick={signOut}
                        aria-label="Sign out now"
                    >
                        Sign out
                    </AlertDialogAction>
                </AlertDialogFooter>
            </AlertDialogContent>
        </AlertDialog>
    );
}

export default IdleWarningDialog;
