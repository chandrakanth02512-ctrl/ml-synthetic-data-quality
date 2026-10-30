"""Read-only GitHub approval-policy checks before any GCP credential request.

An existing environment name alone does not prove that approval rules exist.
Missing/unsupported policy evidence or HTTP failures stop rather than weaken gates.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import quote

from settings import REPO

ENVIRONMENTS = {"deploy": "gcp-deploy", "execute": "gcp-execute"}
API_BASE = "https://api.github.com/repos/" + REPO


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Unexpected GitHub API redirect; stop for repository review")


def get_json(url, token):
    if not url.startswith(API_BASE + "/environments/"):
        raise RuntimeError("GitHub request outside approval-policy allowlist")
    if not token or any(c.isspace() for c in token):
        raise RuntimeError("Read-only GitHub token unavailable for approval-policy inspection")
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"GitHub approval-policy HTTP {error.code}; missing/inaccessible protection is blocking"
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise RuntimeError("GitHub approval-policy inspection incomplete; stop") from None
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected GitHub approval-policy response")
    return data


def validate_environment(environment, rules, expected_name, owner_attested=False):
    if environment.get("name") != expected_name:
        raise RuntimeError("GitHub environment identity differs from the approval gate")
    reviewers = [
        r for r in environment.get("protection_rules", []) if r.get("type") == "required_reviewers"
    ]
    if len(reviewers) != 1 or not reviewers[0].get("reviewers"):
        raise RuntimeError("A nonempty required-reviewers rule is mandatory")
    if reviewers[0].get("prevent_self_review") is not True:
        raise RuntimeError("Prevent self-review must be enabled; use a different required reviewer")
    bypass = environment.get("can_admins_bypass")
    if bypass is True:
        raise RuntimeError("Environment admin bypass must be disabled")
    if bypass is None and owner_attested is not True:
        raise RuntimeError(
            "API omitted admin-bypass status; explicit owner attestation is required"
        )
    if bypass is not None and bypass is not False:
        raise RuntimeError("Unexpected admin-bypass status; protection is not verified")
    branch_policy = environment.get("deployment_branch_policy", {})
    if (
        branch_policy.get("protected_branches") is not False
        or branch_policy.get("custom_branch_policies") is not True
    ):
        raise RuntimeError("Environment must use explicit selected-branch policies")
    if len(rules) != 1 or rules[0].get("name") != "main" or rules[0].get("type") != "branch":
        raise RuntimeError(
            "Exactly one explicit main branch policy is required; tags are not allowed"
        )
    return environment.get("can_admins_bypass") is None


def branch_rules(url, token, getter=get_json):
    result = []
    page = 1
    expected_total = None
    while True:
        data = getter(url + f"?per_page=100&page={page}", token)
        entries = data.get("branch_policies")
        total = data.get("total_count")
        if not isinstance(entries, list) or type(total) is not int or total < 0:
            raise RuntimeError("GitHub branch-policy inventory incomplete")
        if expected_total is None:
            expected_total = total
        if total != expected_total or len(entries) > 100:
            raise RuntimeError("GitHub branch-policy inventory changed during inspection")
        result.extend(entries)
        if len(result) >= total:
            if len(result) != total:
                raise RuntimeError("GitHub branch-policy inventory inconsistent")
            return result
        if not entries or page >= 100:
            raise RuntimeError("GitHub branch-policy pagination incomplete")
        page += 1


def verify_gate(action, env=None, getter=get_json, owner_attested=False):
    environment_name = ENVIRONMENTS.get(action)
    if environment_name is None:
        raise RuntimeError("Unknown approval-gate action")
    token = (os.environ if env is None else env).get("GH_TOKEN", "")
    url = API_BASE + "/environments/" + quote(environment_name, safe="")
    environment = getter(url, token)
    rules = branch_rules(url + "/deployment-branch-policies", token, getter)
    unknown_bypass = validate_environment(
        environment, rules, environment_name, owner_attested=owner_attested
    )
    print(
        "PASS: separate GitHub approval gate, non-self reviewer, and exact main branch:",
        environment_name,
    )
    if unknown_bypass:
        print(
            "OWNER ATTESTATION: API omitted admin-bypass status; disabled bypass is "
            "owner-attested in reviewed configuration, not independently API-verified."
        )


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise RuntimeError("Usage: verify_gate.py deploy|execute")
        verify_gate(sys.argv[1])
    except (RuntimeError, ValueError, OSError) as error:
        sys.exit("STOP: " + str(error))
