"""Owner Cloud Shell review: GET and read-only testIamPermissions only.

No keys, impersonation, configuration changes, API enablement or workload calls.
Tokens stay in memory. No I/O on import; all live operations require the CLI.
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

from bootstrap import gc, owner_context
from job_control import NoRedirect

VERSIONS = {
    "iam.googleapis.com": ("/v1/", "/v2/", "/v3/"),
    "cloudresourcemanager.googleapis.com": ("/v1/", "/v3/"),
    "orgpolicy.googleapis.com": ("/v2/",),
    "serviceusage.googleapis.com": ("/v1/", "/v1beta1/"),
    "artifactregistry.googleapis.com": ("/v1/",),
    "run.googleapis.com": ("/v2/",),
    "accesscontextmanager.googleapis.com": ("/v1/",),
}
TEST_HOSTS = {
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "run.googleapis.com",
}


def validate_request(method, url, permissions):
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc not in VERSIONS
        or not parsed.path.startswith(VERSIONS.get(parsed.netloc, ()))
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise RuntimeError("URL outside read-only review allowlist")
    if method == "GET":
        if permissions or (
            ":" in parsed.path
            and not (
                parsed.netloc == "orgpolicy.googleapis.com"
                and parsed.path.endswith(":getEffectivePolicy")
            )
        ):
            raise RuntimeError("GET accepts only resource reads, not action endpoints")
    elif method == "POST":
        if (
            parsed.netloc not in TEST_HOSTS
            or not parsed.path.endswith(":testIamPermissions")
            or parsed.query
            or not permissions
            or any(not isinstance(p, str) or not p or p.count(".") < 2 for p in permissions)
        ):
            raise RuntimeError("POST is restricted to read-only testIamPermissions")
    else:
        raise RuntimeError("Mutation methods are forbidden")


def request(method, url, permissions=None):
    validate_request(method, url, permissions)
    token = gc("auth", "print-access-token")
    body = None if method == "GET" else json.dumps({"permissions": permissions}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=45) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read()).get("error", {})
        except (ValueError, AttributeError):
            detail = {}
        reasons = sorted(
            {
                x.get("reason")
                for x in detail.get("details", [])
                if isinstance(x, dict) and x.get("reason")
            }
        )
        raise RuntimeError(
            f"READ BLOCKED: HTTP {error.code} / {detail.get('status', 'UNKNOWN')} / {reasons}. "
            "Do not infer absence from denied/disabled access; no changes made."
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise RuntimeError("Read transport/response failure; outcome not verified") from None
    if not isinstance(result, dict):
        raise RuntimeError("Unexpected read response")
    return result


def read_pages(url, field):
    items, seen, token = [], set(), None
    while True:
        page_url = url
        if token:
            page_url += ("&" if "?" in url else "?") + urllib.parse.urlencode({"pageToken": token})
        page = request("GET", page_url)
        values = page.get(field, [])
        if not isinstance(values, list):
            raise RuntimeError("Invalid paginated inventory")
        items.extend(values)
        token = page.get("nextPageToken")
        if not token:
            return {field: items, "inventory_complete": True}
        if token in seen:
            raise RuntimeError("Repeated pagination token; inventory incomplete")
        seen.add(token)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("get", "test"))
    parser.add_argument("url")
    parser.add_argument("--permissions", nargs="+")
    parser.add_argument("--pages", help="List field whose nextPageToken must be fully traversed")
    args = parser.parse_args()
    method = "GET" if args.operation == "get" else "POST"
    validate_request(method, args.url, args.permissions)
    if args.pages and method != "GET":
        raise RuntimeError("Pagination is restricted to GET")
    owner_context()  # Existing active human account, explicit project; no writes or login.
    result = (
        read_pages(args.url, args.pages)
        if args.pages
        else request(method, args.url, args.permissions)
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as error:
        sys.exit("STOP: " + str(error))
