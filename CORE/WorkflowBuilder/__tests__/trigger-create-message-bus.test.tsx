/**
 * PF 4.9 — TriggerCreateDialog supports message_bus triggers.
 *
 * The backend accepts trigger_type "message_bus" (models.py WorkflowTriggerType)
 * with config validated by services/triggers/validator.py::_validate_message_bus
 * (provider enum + non-empty topic), plus a create-time provider gate in
 * services/workflow_triggers_service.py that refuses providers without a live
 * subscriber (streaming.IMPLEMENTED_MESSAGE_BUS_PROVIDERS = redis/kafka/mqtt)
 * and a kafka topic check (streaming.validate_kafka_topic).
 *
 * Asserted here:
 *  - selecting Message bus renders the transport/topic/broker/auth fields and
 *    never offers nats/rabbitmq;
 *  - submit builds the exact config the validator accepts for kafka and mqtt;
 *  - a missing required field (topic) blocks submit with a visible error;
 *  - no raw credential field is rendered — broker auth is a stored-secret
 *    REFERENCE (config.secret_ref), never a password value.
 */

import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { TriggerCreateDialog } from "@/components/TriggerCreateDialog";

const baseProps = {
  open: true,
  onOpenChange: jest.fn(),
  workflowId: 42,
  workflowName: "Demo workflow",
  accessToken: "test-token",
  apiUrl: "http://localhost:8000/api/v1",
};

// Records the POST body the dialog sends so we can assert the exact config
// shape, and answers 200 so the create path completes.
let lastBody: any = null;

function mockFetch() {
  lastBody = null;
  (global as any).fetch = jest.fn((url: string, opts: any) => {
    if (String(url).endsWith("/road/triggers/")) lastBody = JSON.parse(opts.body);
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ ok: true }),
    });
  });
}

async function openDialog(overrides: any = {}) {
  mockFetch();
  await act(async () => {
    render(
      <TriggerCreateDialog {...baseProps} {...overrides} onCreated={jest.fn()} />,
    );
  });
}

function saveButton() {
  return screen.queryByText(/^save$/i) as any;
}

function inputPlaceholders() {
  return Array.from(document.querySelectorAll("input")).map((el) =>
    el.getAttribute("placeholder") || ""
  );
}

async function expectVisibleText(re: RegExp) {
  await waitFor(() => expect(screen.getAllByText(re).length).toBeGreaterThan(0), {
    timeout: 500,
  });
}

describe("TriggerCreateDialog message_bus (PF 4.9)", () => {
  it("selecting Message bus shows transport/topic/broker/auth fields", async () => {
    await openDialog({ initialType: "message_bus" });

    await expectVisibleText(/transport/i);
    await expectVisibleText(/topic/i);
    await expectVisibleText(/broker url/i);
    await expectVisibleText(/broker auth/i);

    // nats/rabbitmq have no subscriber server-side — the create gate refuses
    // them, so the dialog must not offer them.
    expect(screen.queryByText(/^nats$/)).toBeNull();
    expect(screen.queryByText(/^rabbitmq$/)).toBeNull();
  });

  it("kafka submit builds the config _validate_message_bus accepts", async () => {
    const onCreated = jest.fn();
    mockFetch();
    await act(async () => {
      render(
        <TriggerCreateDialog
          {...baseProps}
          initialType="message_bus"
          initialBusProvider="kafka"
          initialBusTopic="mod-arrivals"
          initialBusUrl="kafka://broker.example:9092"
          onCreated={onCreated}
        />,
      );
    });

    fireEvent.click(saveButton());
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("message_bus"));

    expect(lastBody.trigger_type).toBe("message_bus");
    expect(lastBody.configuration).toEqual({
      provider: "kafka",
      topic: "mod-arrivals",
      url: "kafka://broker.example:9092",
      tls: true,
      consumer_timeout_ms: 10000,
    });
  });

  it("mqtt submit builds the config the mqtt subscriber consumes", async () => {
    const onCreated = jest.fn();
    mockFetch();
    await act(async () => {
      render(
        <TriggerCreateDialog
          {...baseProps}
          initialType="message_bus"
          initialBusProvider="mqtt"
          initialBusTopic="warehouse/+/arrivals"
          initialBusUrl="mqtt://broker.example:1883"
          initialBusAuth="secret"
          initialBusSecretRef="mqtt-broker-creds"
          onCreated={onCreated}
        />,
      );
    });

    fireEvent.click(saveButton());
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("message_bus"));

    expect(lastBody.trigger_type).toBe("message_bus");
    expect(lastBody.configuration).toEqual({
      provider: "mqtt",
      topic: "warehouse/+/arrivals",
      url: "mqtt://broker.example:1883",
      secret_ref: "mqtt-broker-creds",
    });
  });

  it("missing required topic blocks submit with a visible error", async () => {
    const onCreated = jest.fn();
    mockFetch();
    await act(async () => {
      render(
        <TriggerCreateDialog
          {...baseProps}
          initialType="message_bus"
          onCreated={onCreated}
        />,
      );
    });

    fireEvent.click(saveButton());
    await expectVisibleText(/topic is required/i);
    expect(onCreated).not.toHaveBeenCalled();
    // The POST never happened.
    expect(lastBody).toBe(null);
  });

  it("renders no raw credential field — auth is a stored-secret reference", async () => {
    await openDialog({ initialType: "message_bus", initialBusAuth: "secret" });

    const placeholders = inputPlaceholders();
    expect(
      placeholders.some((p) => /password|credential|secret$/i.test(p.trim()))
    ).toBe(false);
    // The only auth input is the reference field (placeholder names a ref).
    expect(placeholders.some((p) => /creds|reference/i.test(p))).toBe(true);
    await expectVisibleText(/secret reference/i);
  });
});
