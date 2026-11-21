/**
 * Regression test for notifications-bell auto-mode routing.
 *
 * Bug: notifications-bell hardcoded the legacy global build-queue
 * localStorage key (`mod.buildQueue.v1`) while workflow-builder migrated
 * to a per-user key (`mod.buildQueue.v1.user.<sub>`). When auto-mode
 * fired the "Phase 1 complete" alert and the user clicked it,
 * `findBuildRowForNotification` read from the empty legacy key, returned
 * null, and the bell fell through to wizard-resume — letting the user
 * accidentally re-dispatch Phase 2 by clicking Build a second time.
 *
 * The fix threads the per-user storage key (derived from the JWT) into
 * `findBuildRowForNotification`. These tests pin the contract so the
 * routing can't silently regress to the legacy global key again.
 */

import { findBuildRowForNotification, decideNotificationRoute } from "@/components/notifications-bell";
import {
  buildQueueStorageKey,
  deriveBuildQueueUserId,
  LEGACY_GLOBAL_BUILDS_KEY,
} from "@/lib/build-queue-namespace";

function makeJwt(payload: Record<string, unknown>): string {
  const head = Buffer.from(JSON.stringify({ alg: "HS256" })).toString("base64url");
  const body = Buffer.from(JSON.stringify(payload)).toString("base64url");
  return `${head}.${body}.signature-not-verified`;
}

const TIM_TOKEN = makeJwt({ sub: "tim-1234", email: "admin@example.com" });
const TIM_KEY = buildQueueStorageKey(deriveBuildQueueUserId(TIM_TOKEN));

beforeEach(() => {
  window.localStorage.clear();
});

describe("findBuildRowForNotification — per-user key routing", () => {
  it("finds the build row when read from the matching per-user key", () => {
    window.localStorage.setItem(
      TIM_KEY,
      JSON.stringify([
        { taskId: 100, nodeName: "trellis-img-to-3d", phase: "generation" },
        { taskId: 101, nodeName: "trellis-img-to-3d", phase: "build" },
      ]),
    );
    expect(findBuildRowForNotification(100, "", TIM_KEY)).toBe(101);
  });

  it("matches build_only rows (single-step repo flow) too", () => {
    window.localStorage.setItem(
      TIM_KEY,
      JSON.stringify([
        { taskId: 200, nodeName: "trellis-img-to-3d", phase: "generation" },
        { taskId: 201, nodeName: "trellis-img-to-3d", phase: "build_only" },
      ]),
    );
    expect(findBuildRowForNotification(200, "", TIM_KEY)).toBe(201);
  });

  it("falls back to nodeName from notification message when resumeId is null", () => {
    window.localStorage.setItem(
      TIM_KEY,
      JSON.stringify([
        { taskId: 301, nodeName: "trellis-img-to-3d", phase: "build" },
      ]),
    );
    expect(findBuildRowForNotification(null, "trellis-img-to-3d", TIM_KEY)).toBe(301);
  });

  it("returns null when reading from the LEGACY GLOBAL key but rows live under the per-user key", () => {
    // This is the exact regression: the bell used to call
    // findBuildRowForNotification with the legacy global key, so even
    // though rows existed under the per-user key, the lookup missed.
    window.localStorage.setItem(
      TIM_KEY,
      JSON.stringify([
        { taskId: 400, nodeName: "trellis-img-to-3d", phase: "generation" },
        { taskId: 401, nodeName: "trellis-img-to-3d", phase: "build" },
      ]),
    );
    expect(findBuildRowForNotification(400, "", LEGACY_GLOBAL_BUILDS_KEY)).toBeNull();
    // Confirm the per-user key DOES find it (test sanity).
    expect(findBuildRowForNotification(400, "", TIM_KEY)).toBe(401);
  });

  it("returns null when the resumeId points at a row that has no sibling build", () => {
    window.localStorage.setItem(
      TIM_KEY,
      JSON.stringify([
        { taskId: 500, nodeName: "lonely-gen", phase: "generation" },
      ]),
    );
    expect(findBuildRowForNotification(500, "", TIM_KEY)).toBeNull();
  });

  it("returns null when the storage key is empty / unset", () => {
    expect(findBuildRowForNotification(123, "", TIM_KEY)).toBeNull();
  });

  it("returns null when the persisted blob is malformed JSON", () => {
    window.localStorage.setItem(TIM_KEY, "not-json{");
    expect(findBuildRowForNotification(123, "", TIM_KEY)).toBeNull();
  });

  it("returns null when the persisted blob is JSON but not an array", () => {
    window.localStorage.setItem(TIM_KEY, JSON.stringify({ taskId: 1 }));
    expect(findBuildRowForNotification(1, "", TIM_KEY)).toBeNull();
  });

  // ----------------------------------------------------------------
  // generationTaskId direct-match: the Phase-1 row gets MORPHED into
  // a Phase-2 row by applyTransitionToBuild — its taskId becomes the
  // workflow_run_id and its phase flips to "build". A notification
  // for the original generation task still carries the gen taskId
  // in its link_url's resume= param. Without the direct-match path,
  // the bell falls through to the nodeName fallback (which often
  // doesn't match because notification messages have extra wording).
  // ----------------------------------------------------------------

  it("finds the morphed Phase-2 row by generationTaskId even with no nodeName match", () => {
    window.localStorage.setItem(
      TIM_KEY,
      JSON.stringify([
        {
          // Morphed row: taskId is now workflow_run_id, phase "build",
          // generationTaskId preserves the original Phase-1 task id.
          taskId: 9, // workflow_run_id
          nodeName: "Trellis2 Image to 3D",
          phase: "build",
          generationTaskId: 100, // original phase-1 task
        },
      ]),
    );
    // Notification keys off the GEN taskId (100). Without the new
    // matcher, this would fall through to nodeName lookup and miss
    // because the bell only has n.message ("artifacts ready for…")
    // not the exact nodeName. With the matcher, we find row 9.
    expect(findBuildRowForNotification(100, "", TIM_KEY)).toBe(9);
  });

  it("finds the morphed build_only row by generationTaskId", () => {
    // Repo (singleStep) flow: phase is "build_only" and gen taskId
    // is preserved by the addBuild call path.
    window.localStorage.setItem(
      TIM_KEY,
      JSON.stringify([
        {
          taskId: 9,
          nodeName: "Trellis2 Image to 3D",
          phase: "build_only",
          generationTaskId: 100,
        },
      ]),
    );
    expect(findBuildRowForNotification(100, "", TIM_KEY)).toBe(9);
  });

  it("generationTaskId match wins over nodeName fallback for the same notification", () => {
    // Two rows: an OLD finished build with a matching nodeName, and a
    // newer morphed row with the matching generationTaskId. The newer
    // row should win — the notification's gen taskId is the precise
    // anchor.
    window.localStorage.setItem(
      TIM_KEY,
      JSON.stringify([
        { taskId: 7, nodeName: "trellis-img", phase: "build" },
        {
          taskId: 9,
          nodeName: "trellis-img",
          phase: "build",
          generationTaskId: 100,
        },
      ]),
    );
    expect(findBuildRowForNotification(100, "trellis-img", TIM_KEY)).toBe(9);
  });

  it("falls back to nodeName lookup if no row carries the gen taskId", () => {
    // Older builds (pre-fix) don't have generationTaskId tagged.
    // The nodeName fallback still has to work for them.
    window.localStorage.setItem(
      TIM_KEY,
      JSON.stringify([
        { taskId: 7, nodeName: "trellis-img", phase: "build" },
      ]),
    );
    expect(findBuildRowForNotification(100, "trellis-img", TIM_KEY)).toBe(7);
  });
});

describe("decideNotificationRoute — Tim's regression: node_generation_complete must NOT reopen the wizard", () => {
  // Background: when a "Node artifacts ready for X" notification was
  // clicked and localStorage didn't surface the build row (cross-tab,
  // cleared cache, fresh browser), the bell would fall through to
  // onResumeGeneration and reopen the wizard at Preview. The system
  // was already past that step, so the user could re-confirm and
  // re-dispatch Phase 2 by mistake. The route MUST instead open the
  // build-queue panel.
  const baseArgs = {
    notificationType: "node_generation_complete",
    linkUrl: "#workflow-builder?resume=100",
    resumeId: 100,
    buildRowTaskId: null as number | null,
    dispatchedPhase2BuildTaskId: null as number | null,
  };

  it("expand-queue-row when localStorage has the build row", () => {
    const route = decideNotificationRoute({ ...baseArgs, buildRowTaskId: 9 });
    expect(route).toEqual({ kind: "expand-queue-row", taskId: 9 });
  });

  it("expand-queue-row when the post-Confirm-and-Build session log has it", () => {
    const route = decideNotificationRoute({
      ...baseArgs, dispatchedPhase2BuildTaskId: 11,
    });
    expect(route).toEqual({ kind: "expand-queue-row", taskId: 11 });
  });

  it("open-queue (NOT wizard-resume) when neither localStorage source has the row", () => {
    // This is Tim's regression. With the old code, this would have
    // routed to wizard-resume which is wrong because the system is
    // past the review step.
    const route = decideNotificationRoute({ ...baseArgs });
    expect(route).toEqual({ kind: "open-queue" });
  });

  it("open-queue even when resumeId is null", () => {
    const route = decideNotificationRoute({
      ...baseArgs,
      resumeId: null,
      linkUrl: null,
    });
    expect(route).toEqual({ kind: "open-queue" });
  });

  it("buildRowTaskId wins over dispatchedPhase2 even when both are present", () => {
    // The exact-row signal (localStorage queue) is more reliable than
    // the looser Confirm-and-Build session log; precedence is the
    // contract spec'd in the dispatcher.
    const route = decideNotificationRoute({
      ...baseArgs,
      buildRowTaskId: 9,
      dispatchedPhase2BuildTaskId: 11,
    });
    expect(route).toEqual({ kind: "expand-queue-row", taskId: 9 });
  });
});

describe("decideNotificationRoute — failures + other notification types preserve wizard-resume", () => {
  it("node_generation_failed routes to wizard-resume so the user can inspect the error", () => {
    const route = decideNotificationRoute({
      notificationType: "node_generation_failed",
      linkUrl: "#workflow-builder?resume=200",
      resumeId: 200,
      buildRowTaskId: null,
      dispatchedPhase2BuildTaskId: null,
    });
    expect(route).toEqual({ kind: "wizard-resume", taskId: 200 });
  });

  it("unknown notification type with resumeId still uses wizard-resume", () => {
    const route = decideNotificationRoute({
      notificationType: "some_future_type",
      linkUrl: "#workflow-builder?resume=300",
      resumeId: 300,
      buildRowTaskId: null,
      dispatchedPhase2BuildTaskId: null,
    });
    expect(route).toEqual({ kind: "wizard-resume", taskId: 300 });
  });

  it("notification with no resumeId falls back to link-url-hash", () => {
    const route = decideNotificationRoute({
      notificationType: "some_announcement",
      linkUrl: "#changelog",
      resumeId: null,
      buildRowTaskId: null,
      dispatchedPhase2BuildTaskId: null,
    });
    expect(route).toEqual({ kind: "link-url-hash", hash: "changelog" });
  });

  it("notification with neither resumeId nor a hash link is a noop", () => {
    const route = decideNotificationRoute({
      notificationType: "some_announcement",
      linkUrl: null,
      resumeId: null,
      buildRowTaskId: null,
      dispatchedPhase2BuildTaskId: null,
    });
    expect(route).toEqual({ kind: "noop" });
  });

  it("non-hash link_url is treated as noop (we don't navigate to external URLs from the bell)", () => {
    const route = decideNotificationRoute({
      notificationType: null,
      linkUrl: "https://example.com/external",
      resumeId: null,
      buildRowTaskId: null,
      dispatchedPhase2BuildTaskId: null,
    });
    expect(route).toEqual({ kind: "noop" });
  });
});

describe("decideNotificationRoute — conversational-project-builder §6.1 checkpoint hand-off", () => {
  // A project checkpoint that enters awaiting-user emits an in-app
  // notification (no email) of type project_waiting_for_user with
  // link_url `#project/<session id>`. Clicking the bell must navigate to
  // the conversational project view — never the wizard/queue paths — so
  // the user lands where they can test the piece and give feedback.
  it("project_waiting_for_user routes to the project view via link-url-hash", () => {
    const route = decideNotificationRoute({
      notificationType: "project_waiting_for_user",
      linkUrl: "#project/abc-123",
      resumeId: null,
      buildRowTaskId: null,
      dispatchedPhase2BuildTaskId: null,
    });
    expect(route).toEqual({ kind: "link-url-hash", hash: "project/abc-123" });
  });

  it("a project checkpoint notification never re-opens the wizard or dispatches a build", () => {
    const route = decideNotificationRoute({
      notificationType: "project_waiting_for_user",
      linkUrl: "#project/abc-123",
      resumeId: null,
      buildRowTaskId: null,
      dispatchedPhase2BuildTaskId: null,
    });
    expect(route.kind).not.toBe("wizard-resume");
    expect(route.kind).not.toBe("open-queue");
    expect(route.kind).not.toBe("expand-queue-row");
  });
});

describe("findBuildRowForNotification — user-scoping", () => {
  it("does not surface another user's build rows when both keys exist", () => {
    const otherToken = makeJwt({ sub: "other-user-9999" });
    const otherKey = buildQueueStorageKey(deriveBuildQueueUserId(otherToken));
    expect(otherKey).not.toBe(TIM_KEY);

    // Other user has a build row for the same nodeName.
    window.localStorage.setItem(
      otherKey,
      JSON.stringify([
        { taskId: 999, nodeName: "shared-name", phase: "build" },
      ]),
    );
    // Tim looks up "shared-name" with HIS storage key — should miss.
    expect(findBuildRowForNotification(null, "shared-name", TIM_KEY)).toBeNull();
    // Other user's lookup with their own key still finds it.
    expect(findBuildRowForNotification(null, "shared-name", otherKey)).toBe(999);
  });
});
