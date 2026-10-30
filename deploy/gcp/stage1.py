"""Reviewed bootstrap proposal. No mutation unless explicitly invoked with apply."""

import json
import sys

from preflight import PROJECT, REGION, Context, api, gc

LABEL = {"deployment": "synthetic-quality-phase1"}
PERMS = {"storage.objects.create", "storage.objects.get"}
PROTECTED_BUCKETS = frozenset({"gcp-idq-data-984592102391"})


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def resources(c):
    planned_buckets(c)
    specs = []
    for name in (c.data, c.source):
        specs.append(
            (
                "bucket " + name,
                "https://storage.googleapis.com/storage/v1/b/" + name,
                [
                    "storage",
                    "buckets",
                    "create",
                    "gs://" + name,
                    "--location=" + REGION,
                    "--default-storage-class=STANDARD",
                    "--uniform-bucket-level-access",
                    "--public-access-prevention",
                    "--soft-delete-duration=7d",
                ],
                "bucket",
            )
        )
    specs.append(
        (
            "repository " + c.repository,
            f"https://artifactregistry.googleapis.com/v1/projects/{PROJECT}/locations/{REGION}/repositories/{c.repository}",
            [
                "artifacts",
                "repositories",
                "create",
                c.repository,
                "--location=" + REGION,
                "--repository-format=docker",
                "--immutable-tags",
                "--disable-vulnerability-scanning",
                "--labels=deployment=synthetic-quality-phase1",
            ],
            "repository",
        )
    )
    specs.append(
        (
            "custom role",
            "https://iam.googleapis.com/v1/" + c.role,
            [
                "iam",
                "roles",
                "create",
                "syntheticQualityObjects",
                "--stage=GA",
                "--title=Synthetic quality immutable objects",
                "--permissions=storage.objects.create,storage.objects.get",
            ],
            "role",
        )
    )
    return specs


def planned_buckets(c):
    require(
        c.data == f"synthetic-quality-{c.number}-data-as1"
        and c.source == f"synthetic-quality-{c.number}-source-as1",
        "Planned bucket names changed; separate naming/reuse review required",
    )
    require(
        not PROTECTED_BUCKETS.intersection((c.data, c.source)),
        "Protected existing bucket must not enter bootstrap or IAM paths",
    )


def validate(c, kind, d, require_label=True):
    if kind == "bucket":
        require(str(d.get("projectNumber")) == c.number, "Bucket belongs to another project")
        require(
            d.get("location") == "ASIA-SOUTH1" and d.get("storageClass") == "STANDARD",
            "Bucket location/class mismatch",
        )
        iam = d.get("iamConfiguration", {})
        require(
            iam.get("uniformBucketLevelAccess", {}).get("enabled") is True, "Uniform access missing"
        )
        require(iam.get("publicAccessPrevention") == "enforced", "Public access prevention missing")
        require(
            str(d.get("softDeletePolicy", {}).get("retentionDurationSeconds")) == "604800",
            "Soft-delete mismatch",
        )
        require(
            not d.get("versioning", {}).get("enabled") and not d.get("retentionPolicy"),
            "Versioning/retention requires review",
        )
        require(
            not d.get("billing", {}).get("requesterPays")
            and not d.get("autoclass", {}).get("enabled"),
            "Unexpected billing/autoclass",
        )
        require(
            not d.get("encryption", {}).get("defaultKmsKeyName")
            and not d.get("lifecycle", {}).get("rule"),
            "Unexpected encryption/lifecycle",
        )
        if require_label:
            require(
                all(d.get("labels", {}).get(k) == v for k, v in LABEL.items()),
                "Existing unmarked bucket: stop for ownership review; do not adopt automatically",
            )
    elif kind == "repository":
        require(
            d.get("name") == f"projects/{PROJECT}/locations/{REGION}/repositories/{c.repository}",
            "Repository name mismatch",
        )
        require(
            d.get("format") == "DOCKER" and d.get("mode") == "STANDARD_REPOSITORY",
            "Repository format/mode mismatch",
        )
        require(d.get("dockerConfig", {}).get("immutableTags") is True, "Immutable tags missing")
        require(
            d.get("vulnerabilityScanningConfig", {}).get("enablementConfig") == "DISABLED",
            "Scanning not explicitly disabled",
        )
        require(
            not d.get("kmsKeyName") and not d.get("cleanupPolicies"),
            "Repository encryption/cleanup requires review",
        )
        require(
            all(d.get("labels", {}).get(k) == v for k, v in LABEL.items()),
            "Existing unmarked repository: stop for ownership review",
        )
    else:
        require(d.get("name") == c.role and not d.get("deleted"), "Role name/deletion mismatch")
        require(
            set(d.get("includedPermissions", [])) == PERMS and d.get("stage") == "GA",
            "Existing custom role permissions/stage mismatch",
        )


def binding_specs(c):
    planned_buckets(c)
    return [
        (
            ["storage", "buckets", "get-iam-policy", "gs://" + c.data],
            ["storage", "buckets", "add-iam-policy-binding", "gs://" + c.data],
            c.runtime,
            c.role,
        ),
        (
            ["storage", "buckets", "get-iam-policy", "gs://" + c.source],
            ["storage", "buckets", "add-iam-policy-binding", "gs://" + c.source],
            c.build,
            "roles/storage.objectViewer",
        ),
        (
            ["artifacts", "repositories", "get-iam-policy", c.repository, "--location=" + REGION],
            [
                "artifacts",
                "repositories",
                "add-iam-policy-binding",
                c.repository,
                "--location=" + REGION,
            ],
            c.build,
            "roles/artifactregistry.writer",
        ),
        (
            ["projects", "get-iam-policy", PROJECT],
            ["projects", "add-iam-policy-binding", PROJECT],
            c.build,
            "roles/logging.logWriter",
        ),
    ]


def workload_policy_check(c, bindings, read):
    # This checks direct bindings. Parent grants/deny/PAB/conditions remain
    # an administrator review gate.
    for b in bindings:
        require(
            not ({"allUsers", "allAuthenticatedUsers"} & set(b.get("members", []))),
            "Public binding requires review",
        )
        for sa in (c.build, c.runtime):
            if "serviceAccount:" + sa not in b.get("members", []):
                continue
            allowed = set()
            if read[0] == "projects" and sa == c.build:
                allowed = {"roles/logging.logWriter"}
            elif read[0] == "storage" and read[-1] == "gs://" + c.data and sa == c.runtime:
                allowed = {c.role}
            elif read[0] == "storage" and read[-1] == "gs://" + c.source and sa == c.build:
                allowed = {"roles/storage.objectViewer"}
            elif read[0] == "artifacts" and sa == c.build:
                allowed = {"roles/artifactregistry.writer"}
            require(
                b.get("role") in allowed and not b.get("condition"),
                "Unexpected broad/conditional workload grant: " + json.dumps(b),
            )


def inspect(c, complete=False):
    missing = []
    for name, url, command, kind in resources(c):
        d = api("GET", url, missing=True)
        if d is None:
            print("MISSING:", name)
            missing.append((name, url, command, kind))
        else:
            validate(c, kind, d)
            print("MATCH:", name)
    if complete:
        require(not missing, "Bootstrap incomplete; run approved Stage 1 first")
    return missing


def ensure_resource(c, spec):
    """Create only missing resources. Label only a bucket just created by this call."""
    planned_buckets(c)
    name, url, command, kind = spec
    if kind == "bucket":
        bucket = url.rsplit("/", 1)[-1]
        require(
            bucket in (c.data, c.source) and bucket not in PROTECTED_BUCKETS,
            "Bucket is outside the approved planned names",
        )
        require(
            command[:4] == ["storage", "buckets", "create", "gs://" + bucket],
            "Bucket creation command does not match its inspected target",
        )
    d = api("GET", url, missing=True)
    if d is None:
        # Failure/409/lost acknowledgement stops here: never label on create failure.
        gc(*command)
        d = api("GET", url)
        if kind == "bucket":
            require(d.get("name") == bucket, "Created bucket name mismatch")
            validate(c, kind, d, require_label=False)
            meta = str(d.get("metageneration", ""))
            require(
                meta.isdigit() and int(meta) > 0,
                "Bucket metageneration unavailable; stop before labeling",
            )
            labels = dict(d.get("labels", {}))
            labels.update(LABEL)
            # Compatibility fix: installed Cloud Shell rejected --labels on create.
            # This PATCH is reachable only after successful creation in this call.
            api("PATCH", url + "?ifMetagenerationMatch=" + meta, {"labels": labels})
            d = api("GET", url)
    # Existing unlabeled resources still fail here and are never adopted/repaired.
    validate(c, kind, d)
    print("VERIFIED:", name)


def iam(c, apply=False, require_bindings=False):
    for read, write, sa, role in binding_specs(c):
        d = gc(*read, json_output=True)
        bindings = d.get("bindings", [])
        workload_policy_check(c, bindings, read)
        member = "serviceAccount:" + sa
        found = any(
            b.get("role") == role and member in b.get("members", []) and not b.get("condition")
            for b in bindings
        )
        if not found:
            print("MISSING BINDING:", role, member)
            if apply:
                gc(*write, "--member=" + member, "--role=" + role, "--condition=None")
                new = gc(*read, json_output=True)
                require(
                    any(
                        b.get("role") == role
                        and member in b.get("members", [])
                        and not b.get("condition")
                        for b in new.get("bindings", [])
                    ),
                    "Binding not visible yet; wait and inspect, do not broaden roles",
                )
            elif require_bindings:
                raise RuntimeError("Required binding missing")
        else:
            print("MATCH BINDING:", role, member)


def main():
    require(
        len(sys.argv) == 2 and sys.argv[1] in ("check", "apply"),
        "Usage: python3 stage1.py check|apply",
    )
    c = Context()
    c.check_policies()
    print(
        "PROTECTED EXISTING INVENTORY (excluded from bootstrap/IAM/cleanup):",
        ", ".join(sorted(PROTECTED_BUCKETS)),
    )
    missing = inspect(c)
    job = api(
        "GET",
        f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{c.job}",
        missing=True,
    )
    if job is not None:
        print("EXISTING JOB (never changed by Stage 1):", json.dumps(job, indent=2))
    # Inspect all existing resource policies before creating or granting anything.
    for read, _, _, _ in binding_specs(c):
        if read[0] == "storage" and any(
            read[-1] == "gs://" + x[0].removeprefix("bucket ") for x in missing if x[3] == "bucket"
        ):
            continue
        if read[0] == "artifacts" and any(x[3] == "repository" for x in missing):
            continue
        workload_policy_check(c, gc(*read, json_output=True).get("bindings", []), read)
    if sys.argv[1] == "check":
        print(
            "READ-ONLY CHECK FINISHED. Review policies, parent IAM/deny/PAB, quotas, "
            "ownership, budget; no resources changed."
        )
        return
    require(
        input("After separate Stage 1 approval and policy/quota review, type BOOTSTRAP: ")
        == "BOOTSTRAP",
        "Approval acknowledgement missing",
    )
    for spec in missing:
        # Recheck immediately. A concurrent create never becomes an overwrite.
        ensure_resource(c, spec)
    iam(c, apply=True)
    inspect(c, complete=True)
    iam(c, require_bindings=True)
    print("STAGE 1 COMPLETE. No builds, jobs or executions created.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as e:
        sys.exit("STOP: " + str(e))
