# Replacement image and source provenance review

## Result: owner-run source comparison successful

The owner supplied these Cloud Shell verification results:

| Evidence | Result |
|---|---|
| Replacement build | `fc1ca075-570b-45da-b588-977423c49f0d` |
| Build / Docker step | Both `SUCCESS`, owner verified |
| Published image digest | `sha256:35a1e032472e06e4b21c55996b46faa0090deef3a4bdee710f27ddcb85e33116` |
| Artifact Registry comparison | Exact digest match, owner verified |
| Build identity | Expected dedicated build account, owner verified |
| Source bucket | Expected recovery source bucket, owner verified |
| Submitted / resolved source | Same bucket, object and generation, owner supplied |
| Source generation | `1793365313077688` |
| Source archive SHA-256 | `84d69e14c3934d8965edd6c734417710816cb20a615d713841bc21c68612519e` |
| Source archive MD5 | `40a096287045897d5c441ef6b88968fe` |
| Uploaded archive contents | Owner-run verifier succeeded; 19 files matched |
| Link from consumed source bytes to pinned Git commit | Owner-verified match to `34cc3d46b662a9f0560c3ff846d68b327d52e4b8` |

Codex has no GCP identity or permitted GCP API egress and has not independently
queried the Build or registry. The supplied non-secret excerpts are recorded in
`source-evidence.json`, explicitly labelled as owner-provided evidence rather than
a signed attestation or independently fetched API response. Recording this evidence
does not enable setup. The owner subsequently downloaded the exact resolved GCS
generation in authenticated Cloud Shell and reported
`ARCHIVE_CONTENTS_MATCH_PINNED_APPLICATION`, **19 matched files**, the pinned commit,
exact build/image identities and archive SHA-256 above. This successful owner-run
result is recorded under `owner_run_verification` in `source-evidence.json`.
Codex has not downloaded or inspected that live archive, and no independently
verified signed provenance/attestation is claimed. The source-content check is
complete on that stated evidence basis; it is no longer a pending user action.

The supplied `source.storageSource` exactly equals
`sourceProvenance.resolvedStorageSource`. The `fileHashes` key identifies that same
object generation. Both base64 hash values decode to the expected MD5/SHA-256
lengths; URL-safe and standard base64 are accepted. The given Docker arguments,
`DOCKER_BUILDKIT=1`, substitutions and service account match pinned `cloudbuild.yaml`
after the reviewed recovery substitutions. The archive SHA-256 above is a different
digest from the published container image digest; they must not be interchanged.

The owner-run comparison supplies the file-content comparison in addition to the
precise archive identity. The tag and hash by themselves were never used as proof.

## What the repository audit establishes

The application pin is `34cc3d46b662a9f0560c3ff846d68b327d52e4b8`.
All 46 original files still match that commit's Git blob hashes, enforced by
`application-baseline.json` and offline tests.

The recovery implementation reviewed at
[`570202ae4437b3bdb81b244059bdce9579707f2d`](https://github.com/chandrakanth02512-ctrl/ml-synthetic-data-quality/blob/570202ae4437b3bdb81b244059bdce9579707f2d/deploy/gcp/stage2_recovery.py)
clones into a new directory, checks out the application pin detached, checks the
clean working tree and current main, then submits that checkout. The pinned
`.gcloudignore` permits only the application/build allowlist and excludes Git
metadata, deployment scripts, tests and private files. These code checks support
the intended submission process; they do not prove which archive this live build
actually consumed. No recovery receipt was modified or used as a substitute for
server-side source evidence.

The `_TAG` revision suffix, successful Docker step, correct bucket/account and
matching output digest do not individually or together prove source contents.

## Optional read-only reproduction of the completed comparison

From an already authorized Cloud Shell, inspect only this existing build:

```bash
gcloud builds describe fc1ca075-570b-45da-b588-977423c49f0d \
  --project=gcp-intelligent-data-quality --region=asia-south1 \
  --format='json(id,projectId,status,serviceAccount,source,sourceProvenance,steps.name,steps.status,steps.args,substitutions,results.images,options.sourceProvenanceHash)'
```

The owner already supplied the requested source fields. A fresh read may confirm
they remain consistent and inspect any omitted entrypoint, directory, additional
steps or secrets settings; stop on unexpected behavior. Do not share credentials,
token output or unrelated private build settings.
Require the expected project/build/result digest, and inspect:

1. `serviceAccount` should identify
   `projects/gcp-intelligent-data-quality/serviceAccounts/synthetic-quality-build@gcp-intelligent-data-quality.iam.gserviceaccount.com`.
2. `source.storageSource` should use
   `synthetic-quality-984592102391-source-as1` and the recovery object namespace
   `source/recovery-6f75c5dd-cc3a-4d47-9c9d-43f51fa3c4ba/`.
3. `sourceProvenance.resolvedStorageSource` should identify the exact consumed
   bucket, object and generation. Inspect any source hashes and available trusted
   provenance linking that input to this image digest. Do not infer a Git commit
   from the object name or tag.
4. Verify the single Docker step's arguments against pinned `cloudbuild.yaml`,
   including context, platform and image name after substitutions. Inspect any
   unexpected step/entrypoint/source behavior before proceeding.
5. Retrieve only the exact resolved GCS object generation
   with authorized read access; preserve the original object and receipts. Compare
   its hash to source provenance when available, then compare the regular-file
   bytes/inventory to the pinned Git blobs and `.gcloudignore` allowlist. Inspect
   the archive without extracting untrusted links/paths or executing its contents.
   Extra, missing, duplicate or changed inputs require review, not adoption.

Use a fresh Cloud Shell directory so no existing checkout, archive or receipt is
overwritten. These commands retrieve GitHub review code, read the existing GCS
object and perform an offline comparison; they do not build, deploy or enable IAM.
They are review instructions, not commands executed by Codex:

```bash
set -euo pipefail
review_dir="$(mktemp -d "$HOME/synthetic-quality-source-review.XXXXXX")"
git clone --branch deploy/github-oidc --single-branch \
  https://github.com/chandrakanth02512-ctrl/ml-synthetic-data-quality.git \
  "$review_dir/repository"
cd "$review_dir/repository"
git log -1 --format='%H %s'
# Inspect this checkout against the reviewed PR commit before proceeding.

gcloud storage cp \
  'gs://synthetic-quality-984592102391-source-as1/source/recovery-6f75c5dd-cc3a-4d47-9c9d-43f51fa3c4ba/1793365312.461269-5cb68052b40b42e7b5c96280d2951453.tgz#1793365313077688' \
  "$review_dir/source.tgz" --project=gcp-intelligent-data-quality

python3 deploy/oidc/verify_source_archive.py "$review_dir/source.tgz"
```

Expected result: `ARCHIVE_CONTENTS_MATCH_PINNED_APPLICATION`, pinned application
commit `34cc3d46b662a9f0560c3ff846d68b327d52e4b8`, exact build/image/source identities,
the archive SHA-256 above and **19 matched files**. The verifier first checks the
supplied provenance and compressed archive SHA-256 (plus MD5 consistency), then
compares every allowed regular file to the pinned Git blob. It rejects extra,
missing or duplicate inputs, unsafe paths, links/devices/sparse entries, executable
or special file modes, hidden trailing data and oversized compressed/decompressed
contents. It never extracts, executes, downloads, writes evidence, changes config
or reads recovery receipts. No source/archive success is claimed from unit fixtures.

The successful JSON result has now been reported by the owner. If repeating the
check for a new review, a `STOP`, permission failure, missing
generation, hash mismatch or different inventory leaves source identity unverified.
Do not repair the archive, use a newer generation, change the pin, retry a build,
set activation flags or weaken verification. Preserve the downloaded evidence.

Generation-specific object reads/local downloads do not modify GCP resources but
may incur small storage read/transfer charges. No live archive download was
performed by Codex. The owner performed the real download/comparison; synthetic
test archives are unrelated to that live archive and do not substitute for it.
Do not substitute the latest object generation or a new checkout for the consumed
archive. A fresh checkout verifies the reference side of a comparison only.

Google's [Cloud Build API definition](https://github.com/googleapis/googleapis/blob/master/google/devtools/cloudbuild/v1/cloudbuild.proto)
defines resolved storage generations and source hashes. `fileHashes` is populated
only when source-provenance hashing is requested; an archive source gets an archive
hash. Hashes are available in the owner's supplied output and are used here. The
stored configuration's lack of an explicit hash option does not negate the returned
hashes and does not require a configuration change or another build.

If metadata, immutable source generation, permissions or trusted provenance is
insufficient, leave source identity unverified and stop before activation. Even
matching source-file bytes do not imply a fully reproducible image: the original
Docker base tag and downloaded package artifacts are not all hash-pinned.
