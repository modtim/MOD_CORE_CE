"use client"

// Re-export from the canonical toast store. This file used to be a
// byte-for-byte COPY of components/ui/use-toast.ts, which meant it
// instantiated its own independent module-level `memoryState`/
// `listeners` singleton. components/ui/toaster.tsx (the app's one
// mounted <Toaster/>) subscribed to *this* module's useToast(), while
// every toast() caller in the app (useWorkflowApi.ts, node-config-panel.tsx,
// workflow-builder.tsx, etc.) imported toast() from
// "@/components/ui/use-toast" instead — a totally disconnected store.
// Toasts dispatched from that side never reached the rendered Toaster.
// Re-exporting here guarantees a single shared singleton.
export { useToast, toast } from "@/components/ui/use-toast"
