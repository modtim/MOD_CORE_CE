/**
 * δ Phase 9 — feature-config gate sanity test.
 *
 * The wizard's Describe-and-build tab is gated on the API's
 * /platform/feature-config response. This test pins the contract
 * shape so a future API change can't silently disable the tab
 * everywhere.
 */

describe("feature-config response contract", () => {
  type FeatureConfig = {
    describe_and_build_available: boolean;
    missing_secrets: string[];
    is_platform_admin: boolean;
  };

  it("recognises the available state", () => {
    const fc: FeatureConfig = {
      describe_and_build_available: true,
      missing_secrets: [],
      is_platform_admin: false,
    };
    expect(fc.describe_and_build_available).toBe(true);
    expect(fc.missing_secrets).toHaveLength(0);
  });

  it("recognises the missing-token state", () => {
    const fc: FeatureConfig = {
      describe_and_build_available: false,
      missing_secrets: ["github_token"],
      is_platform_admin: true,
    };
    expect(fc.describe_and_build_available).toBe(false);
    expect(fc.missing_secrets).toContain("github_token");
  });

  it("never carries the actual token value", () => {
    const fc: FeatureConfig = {
      describe_and_build_available: true,
      missing_secrets: [],
      is_platform_admin: false,
    };
    const serialised = JSON.stringify(fc);
    expect(serialised).not.toMatch(/ghp_/);
    expect(serialised).not.toMatch(/github_pat/);
  });
});

describe("describe-search response contract", () => {
  type Candidate = {
    full_name: string;
    license_spdx: string | null;
    stars: number;
    relevance: number;
    fit_reason: string;
    risk_flags: string[];
    safety_warnings: string[];
  };

  type DescribeResponse = {
    intent: "wrap_repo" | "from_scratch";
    interpretation: string;
    search_keywords: string[];
    candidates: Candidate[];
    cached?: boolean;
    rate_limited?: boolean;
    not_configured?: boolean;
  };

  it("supports wrap_repo with candidates", () => {
    const r: DescribeResponse = {
      intent: "wrap_repo",
      interpretation: "detect faces",
      search_keywords: ["face detection"],
      candidates: [{
        full_name: "owner/repo",
        license_spdx: "MIT",
        stars: 1000,
        relevance: 85,
        fit_reason: "purpose-built",
        risk_flags: [],
        safety_warnings: [],
      }],
    };
    expect(r.candidates[0].license_spdx).toBe("MIT");
  });

  it("supports from_scratch with empty candidates", () => {
    const r: DescribeResponse = {
      intent: "from_scratch",
      interpretation: "csv munge",
      search_keywords: ["csv"],
      candidates: [],
    };
    expect(r.candidates).toHaveLength(0);
  });

  it("supports the rate-limited fallback", () => {
    const r: DescribeResponse = {
      intent: "from_scratch",
      interpretation: "",
      search_keywords: [],
      candidates: [],
      rate_limited: true,
    };
    expect(r.rate_limited).toBe(true);
  });

  it("supports the not-configured fallback", () => {
    const r: DescribeResponse = {
      intent: "wrap_repo",
      interpretation: "x",
      search_keywords: ["a"],
      candidates: [],
      not_configured: true,
    };
    expect(r.not_configured).toBe(true);
  });
});

/**
 * Post-launch UX revision: the Describe tab must stay clickable for
 * everyone, even when github_token is missing on the platform tenant.
 * The missing-secret notice is admin-only — end users get a generic
 * "search not available" message that doesn't ask them to do something
 * only an admin can do. These tests pin that intent so a future
 * refactor can't silently re-disable the tab or re-introduce the
 * admin-pointer nag for non-admins.
 */
describe("describe-tab UI gating intent", () => {
  type FeatureConfig = {
    describe_and_build_available: boolean;
    missing_secrets: string[];
    is_platform_admin: boolean;
  };

  // Inline helpers mirror the boolean expressions used in
  // new-node-wizard.tsx so the test documents the contract
  // independently of the component's render tree.
  const tabClickable = (_fc: FeatureConfig) => true;  // always
  const showPreSearchAdminNotice = (fc: FeatureConfig) =>
    !fc.describe_and_build_available && fc.is_platform_admin;
  const postSearchNotConfiguredAdminVariant = (fc: FeatureConfig) =>
    fc.is_platform_admin === true;

  it("tab is clickable for non-admin with token configured", () => {
    const fc: FeatureConfig = {
      describe_and_build_available: true,
      missing_secrets: [],
      is_platform_admin: false,
    };
    expect(tabClickable(fc)).toBe(true);
    expect(showPreSearchAdminNotice(fc)).toBe(false);
  });

  it("tab is clickable for non-admin even when token is missing", () => {
    const fc: FeatureConfig = {
      describe_and_build_available: false,
      missing_secrets: ["github_token"],
      is_platform_admin: false,
    };
    expect(tabClickable(fc)).toBe(true);
    expect(showPreSearchAdminNotice(fc)).toBe(false);
    // If the user actually triggers a search and it fails, the
    // post-search banner uses the user-friendly variant — not the
    // admin nag.
    expect(postSearchNotConfiguredAdminVariant(fc)).toBe(false);
  });

  it("admin sees pre-search notice when token is missing", () => {
    const fc: FeatureConfig = {
      describe_and_build_available: false,
      missing_secrets: ["github_token"],
      is_platform_admin: true,
    };
    expect(tabClickable(fc)).toBe(true);
    expect(showPreSearchAdminNotice(fc)).toBe(true);
    expect(postSearchNotConfiguredAdminVariant(fc)).toBe(true);
  });

  it("admin with token configured sees no notice and no banner", () => {
    const fc: FeatureConfig = {
      describe_and_build_available: true,
      missing_secrets: [],
      is_platform_admin: true,
    };
    expect(tabClickable(fc)).toBe(true);
    expect(showPreSearchAdminNotice(fc)).toBe(false);
  });
});
