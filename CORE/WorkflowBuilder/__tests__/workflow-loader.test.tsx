import React from "react";
import { render, screen, act } from "@testing-library/react";

import { Loading } from "../components/workflow-loader";

describe("WorkflowLoader.Loading stuck-state escape hatch", () => {
    beforeEach(() => {
        jest.useFakeTimers();
    });
    afterEach(() => {
        jest.useRealTimers();
    });

    it("shows just 'Loading...' on first paint, no retry yet", () => {
        render(<Loading />);
        expect(screen.getByText("Loading...")).toBeInTheDocument();
        expect(screen.queryByText("Retry")).toBeNull();
        expect(screen.queryByText(/longer than expected/i)).toBeNull();
    });

    it("surfaces the stuck-state copy + Retry button after 8s", () => {
        render(<Loading />);
        // Just before the 8s mark — nothing extra.
        act(() => {
            jest.advanceTimersByTime(7_999);
        });
        expect(screen.queryByText("Retry")).toBeNull();
        // Cross the threshold.
        act(() => {
            jest.advanceTimersByTime(2);
        });
        expect(screen.getByText(/longer than expected/i)).toBeInTheDocument();
        expect(screen.getByText("Retry")).toBeInTheDocument();
    });

    it("Retry button exists, is enabled, and is clickable", () => {
        // We can't spy on window.location.reload in jsdom (read-only
        // property), so the deeper "did reload fire" check lives in
        // the manual-test plan. Unit-side we pin that the affordance
        // is present and a real button (not a div pretending to be
        // one).
        render(<Loading />);
        act(() => {
            jest.advanceTimersByTime(8_001);
        });
        const retry = screen.getByText("Retry");
        expect(retry.tagName).toBe("BUTTON");
        expect((retry as HTMLButtonElement).disabled).toBe(false);
        // No throw on click is the second-best assertion we can make
        // without a writable window.location.
        expect(() => retry.click()).not.toThrow();
    });
});
