import React from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import WizardErrorBoundary from "../components/wizard-error-boundary";

let shouldThrow = true;

const FragileChild = () => {
    if (shouldThrow) {
        throw new Error("boom");
    }
    return <span data-testid="child">alive</span>;
};

describe("WizardErrorBoundary — error card + reload", () => {
    let errorSpy: jest.SpyInstance;

    beforeEach(() => {
        errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    });

    afterEach(() => jest.restoreAllMocks());

    it("renders child until a child throws", () => {
        render(
            <WizardErrorBoundary>
                <span data-testid="child">ok</span>
            </WizardErrorBoundary>,
        );
        expect(screen.getByTestId("child")).toHaveTextContent("ok");
    });

    it("shows error card text and reload button when child throws", () => {
        const TestThrower = () => {
            throw new Error("kaboom");
        };

        render(
            <WizardErrorBoundary>
                <TestThrower />
            </WizardErrorBoundary>,
        );

        // exact copy from wizard-error-boundary.tsx:58-62
        expect(screen.getByText("The wizard hit a glitch")).toBeInTheDocument();
        expect(screen.getByText(/Something in the current step errored/)).toBeInTheDocument();

        // exact copy from wizard-error-boundary.tsx:64-66
        expect(screen.getByText("kaboom")).toBeInTheDocument();

        // exact copy from wizard-error-boundary.tsx:70-72
        expect(screen.getByText("Reload this step")).toBeInTheDocument();
    });

    it("handleReset re-mounts children once the child stops throwing", async () => {
        shouldThrow = true;

        render(
            <WizardErrorBoundary>
                <FragileChild />
            </WizardErrorBoundary>,
        );

        // The throw happens inside React's render pass, so the boundary sees it.
        expect(screen.getByText("The wizard hit a glitch")).toBeInTheDocument();
        expect(screen.getByText(/boom/)).toBeInTheDocument();

        shouldThrow = false;
        await userEvent.click(screen.getByRole("button", { name: "Reload this step" }));

        expect(screen.queryByText("The wizard hit a glitch")).not.toBeInTheDocument();
        expect(screen.getByTestId("child")).toHaveTextContent("alive");
    });
});
