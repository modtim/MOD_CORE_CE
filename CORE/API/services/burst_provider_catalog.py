"""Static credential catalog for the tenant burst cloud accounts (GH.8c).

The Dashboard's System -> "Cloud accounts" Connect form used to take a
free-text "provider" and one opaque credential blob: a typo'd provider name
only failed later at probe time, and tenants had no idea which keys their
credential JSON needed. This module is the source of truth for which
providers exist and which credential fields each one takes, so the API can
validate the provider name at create/patch time (422 with the known list)
and the Dashboard can render a per-provider credential form.

The field keys are derived EXACTLY from each adapter's factory — the
canonical names, with the aliases the factories accept at runtime NOT
surfaced here:

  * `burst_provider_ec2._ec2_factory`        — provider_key, secret_key, region
  * `burst_provider_azure._azure_factory`    — client_id, client_secret,
                                               tenant_id, subscription_id,
                                               resource_group, region
  * `burst_provider_gcp._gcp_factory`        — provider_key (SA email),
                                               secret_key (SA JSON), project,
                                               datacenter (zone)
  * `burst_provider_lambda._lambda_factory`  — api_key, ssh_key_names (list),
                                               region
  * `burst_provider_oci._oci_factory`        — tenancy, user, fingerprint,
                                               key_content, compartment_id,
                                               availability_domain, region
  * `burst_provider_runpod._runpod_factory`  — api_key, region (the cloud
                                               instance: SECURE by default)

The catalog carries metadata ONLY (labels, secret flags, required flags,
help text, region hints) — never a credential value — so it is safe to
serve to any tenant admin via GET /burst/providers.
"""
from __future__ import annotations

from typing import Any

import services.burst_provider as burst_provider

# Field dict shape: {key, label, secret, required, multiline, help[, list]}
#   secret    -> the Dashboard renders a password input
#   multiline -> a textarea (e.g. GCP service-account JSON, OCI PEM key)
#   list      -> the factory expects a list[str]; the Dashboard splits the
#                text input on commas into a JSON array (only Lambda's
#                ssh_key_names uses this)

CATALOG: dict[str, dict[str, Any]] = {
    "ec2": {
        "label": "AWS EC2",
        "region_hint": "us-east-1",
        "fields": [
            {
                "key": "provider_key", "label": "Access key ID",
                "secret": False, "required": True, "multiline": False,
                "help": "The AWS access key ID (the id side of the key pair).",
            },
            {
                "key": "secret_key", "label": "Secret access key",
                "secret": True, "required": True, "multiline": False,
                "help": "The AWS secret access key — never shown again after saving.",
            },
            {
                "key": "region", "label": "Region",
                "secret": False, "required": False, "multiline": False,
                "help": "AWS region to launch burst workers in (default us-east-1).",
            },
        ],
    },
    "azure": {
        "label": "Microsoft Azure",
        "region_hint": "eastus",
        "fields": [
            {
                "key": "client_id", "label": "Client (application) ID",
                "secret": False, "required": True, "multiline": False,
                "help": "The Azure AD application (client) ID.",
            },
            {
                "key": "client_secret", "label": "Client secret",
                "secret": True, "required": True, "multiline": False,
                "help": "The Azure AD client secret — never shown again after saving.",
            },
            {
                "key": "tenant_id", "label": "Tenant (directory) ID",
                "secret": False, "required": True, "multiline": False,
                "help": "The Azure AD tenant / directory ID.",
            },
            {
                "key": "subscription_id", "label": "Subscription ID",
                "secret": False, "required": True, "multiline": False,
                "help": "The Azure subscription burst VMs are billed to.",
            },
            {
                "key": "resource_group", "label": "Resource group",
                "secret": False, "required": True, "multiline": False,
                "help": "The resource group MOD creates and deletes burst VMs in. "
                        "MOD refuses to launch without one.",
            },
            {
                "key": "region", "label": "Region",
                "secret": False, "required": False, "multiline": False,
                "help": "ARM region (default eastus).",
            },
        ],
    },
    "gcp": {
        "label": "Google Cloud (GCE)",
        "region_hint": "us-central1-a",
        "fields": [
            {
                "key": "provider_key", "label": "Service account email",
                "secret": False, "required": True, "multiline": False,
                "help": "e.g. burst@my-project.iam.gserviceaccount.com",
            },
            {
                "key": "secret_key", "label": "Service account JSON key",
                "secret": True, "required": True, "multiline": True,
                "help": "Paste the full JSON key contents of the service account — "
                        "never shown again after saving.",
            },
            {
                "key": "project", "label": "Project ID",
                "secret": False, "required": True, "multiline": False,
                "help": "The GCP project burst VMs and accelerators are created in.",
            },
            {
                "key": "datacenter", "label": "Zone",
                "secret": False, "required": False, "multiline": False,
                "help": "GCE zone (default us-central1-a).",
            },
        ],
    },
    "lambda": {
        "label": "Lambda Cloud",
        "region_hint": "us-east-1",
        "fields": [
            {
                "key": "api_key", "label": "API key",
                "secret": True, "required": True, "multiline": False,
                "help": "The Lambda Cloud API key — never shown again after saving.",
            },
            {
                "key": "ssh_key_names", "label": "SSH key names",
                "secret": False, "required": True, "multiline": False, "list": True,
                "help": "Comma-separated SSH key names from your Lambda Cloud account; "
                        "required by every launch call.",
            },
            {
                "key": "region", "label": "Region",
                "secret": False, "required": False, "multiline": False,
                "help": "Lambda region (default us-east-1).",
            },
        ],
    },
    "oci": {
        "label": "Oracle Cloud (OCI)",
        "region_hint": "us-ashburn-1",
        "fields": [
            {
                "key": "tenancy", "label": "Tenancy OCID",
                "secret": False, "required": True, "multiline": False,
                "help": "e.g. ocid1.tenancy.oc1..",
            },
            {
                "key": "user", "label": "User OCID",
                "secret": False, "required": True, "multiline": False,
                "help": "e.g. ocid1.user.oc1..",
            },
            {
                "key": "fingerprint", "label": "API key fingerprint",
                "secret": True, "required": True, "multiline": False,
                "help": "The fingerprint of the API signing key (from your OCI profile).",
            },
            {
                "key": "key_content", "label": "Private key (PEM)",
                "secret": True, "required": True, "multiline": True,
                "help": "Paste the private key contents themselves (not a file path) — "
                        "never shown again after saving.",
            },
            {
                "key": "compartment_id", "label": "Compartment OCID",
                "secret": False, "required": True, "multiline": False,
                "help": "The compartment burst instances are launched in; every OCI "
                        "compute call is compartment-scoped.",
            },
            {
                "key": "availability_domain", "label": "Availability domain",
                "secret": False, "required": True, "multiline": False,
                "help": "e.g. ABJ1:AD-1 — OCI launches are AD-scoped.",
            },
            {
                "key": "region", "label": "Region",
                "secret": False, "required": False, "multiline": False,
                "help": "OCI home region (default us-ashburn-1).",
            },
        ],
    },
    "runpod": {
        "label": "RunPod",
        "region_hint": "SECURE",
        "fields": [
            {
                "key": "api_key", "label": "API key",
                "secret": True, "required": True, "multiline": False,
                "help": "The RunPod API key — never shown again after saving.",
            },
            {
                "key": "region", "label": "Region",
                "secret": False, "required": False, "multiline": False,
                "help": "RunPod region value passed to the adapter (default "
                        "SECURE). It does NOT choose the cloud type: the cloud "
                        "type comes from the account config's cloud_instance "
                        "(SECURE unless the account config says community/spot).",
            },
            {
                "key": "gpu_type", "label": "GPU type",
                "secret": False, "required": False, "multiline": False,
                "help": "Preferred pod GPU type (e.g. A10); MOD picks the "
                        "instance type from the account's limits otherwise.",
            },
            {
                "key": "max_lifetime_min", "label": "Max pod lifetime (minutes)",
                "secret": False, "required": False, "multiline": False,
                "help": "Max pod lifetime (minutes). The pod's own lifetime flag "
                        "is set from this value, and the session is also stopped "
                        "by the account limit limits.max_lifetime_hours (default "
                        "12 hours).",
            },
        ],
    },
    "fake": {
        "label": "Fake (dev/test)",
        "region_hint": "fake-1",
        "fields": [
            {
                "key": "region", "label": "Region",
                "secret": False, "required": False, "multiline": False,
                "help": "In-memory deterministic provider for dev and tests.",
            },
        ],
    },
}


def list_providers(include_dev: bool = False) -> list[dict[str, Any]]:
    """The catalog entries as plain dicts ({name, label, fields,
    region_hint}), in CATALOG order, restricted to provider names ACTUALLY
    registered in `burst_provider._PROVIDERS` (a catalog entry whose adapter
    failed to register is never surfaced). The dev-only `fake` provider
    appears only when `include_dev` is true.

    Returns metadata only — no credential value can be in this data."""
    out: list[dict[str, Any]] = []
    for name, entry in CATALOG.items():
        if name not in burst_provider._PROVIDERS:
            continue
        if name == "fake" and not include_dev:
            continue
        out.append({"name": name, **entry})
    return out
