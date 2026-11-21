"use client";

import React from "react";

interface Props {
  children: React.ReactNode;
  /** Called with the error when the boundary catches. Lets the parent
   *  surface a retry button / reset the wizard state. */
  onError?: (error: Error) => void;
}

interface State {
  error: Error | null;
}

/**
 * Last-line-of-defense for the New Node wizard. A single stray null-
 * reference inside a deeply-nested step used to take the whole dialog
 * down (blank white popup, no console error the user could act on).
 * This boundary catches render-time throws, shows a readable "something
 * broke" card with the actual error text, and offers a Reload button
 * that re-mounts the children — which in practice resets the wizard
 * step state without having to hard-refresh the page.
 *
 * Scoped to the wizard; not a global boundary.
 */
export default class WizardErrorBoundary extends React.Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // Log to console so devs can still see the stack trace. The user-
    // visible surface renders below.
    console.error("[wizard] render error:", error, info?.componentStack);
    this.props.onError?.(error);
  }

  handleReset = () => {
    this.setState({ error: null });
  };

  render() {
    if (this.state.error) {
      return (
        <div className="flex flex-col items-center justify-center min-h-[300px] p-8 text-center">
          <div className="w-12 h-12 rounded-full bg-red-100 flex items-center justify-center mb-4">
            <svg className="w-6 h-6 text-red-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                    d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
            </svg>
          </div>
          <h3 className="text-lg font-semibold text-gray-900 mb-2">The wizard hit a glitch</h3>
          <p className="text-sm text-gray-600 mb-1 max-w-md">
            Something in the current step errored while rendering. Your
            progress in other steps is preserved — click Reload to re-
            render this step.
          </p>
          <pre className="text-xs text-red-700 bg-red-50 border border-red-200 rounded p-3 mt-3 mb-4 max-w-xl overflow-auto text-left">
            {this.state.error.message || String(this.state.error)}
          </pre>
          <button
            onClick={this.handleReset}
            className="px-4 py-2 text-sm font-medium text-white bg-indigo-600 rounded hover:bg-indigo-700"
          >
            Reload this step
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
