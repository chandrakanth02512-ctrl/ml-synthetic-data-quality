# Replacement image and source provenance review

## Result: image identity recorded; consumed source identity not yet established

The owner supplied these Cloud Shell verification results:

| Evidence | Result |
|---|---|
| Replacement build | `fc1ca075-570b-45da-b588-977423c49f0d` |
| Build / Docker step | Both `SUCCESS`, owner verified |
| Published image digest | `sha256:35a1e032472e06e4b21c55996b46faa0090deef3a4bdee710f27ddcb85e33116` |
| Artifact Registry comparison | Exact digest match, owner verified |
| Build identity | Expected dedicated build account, owner verified |
| Source bucket | Expected recovery source bucket, owner verified |
| Resolved source object/generation and source bytes | Not supplied; unverified |
| Link from consumed source bytes to pinned Git commit | Not established |

Codex has no GCP identity or permitted GCP API egress and has not independently
queried the Build or registry. No raw Build provenance, source archive or attestation
for this build is available in the workspace/repository. Recording the owner's
verified image identity does not mark source verification complete or enable setup.

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

## Exact read-only Cloud Shell evidence request

From an already authorized Cloud Shell, inspect only this existing build:

```bash
gcloud builds describe fc1ca075-570b-45da-b588-977423c49f0d \
  --project=gcp-intelligent-data-quality --region=asia-south1 \
  --format='json(id,projectId,status,serviceAccount,source,sourceProvenance,steps.name,steps.status,steps.args,substitutions,results.images,options.sourceProvenanceHash)'
```

Do not share credentials, token output or unrelated private build settings.
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
5. If using a GCS source archive, retrieve only the exact resolved object generation
   with authorized read access; preserve the original object and receipts. Compare
   its hash to source provenance when available, then compare the regular-file
   bytes/inventory to the pinned Git blobs and `.gcloudignore` allowlist. Inspect
   the archive without extracting untrusted links/paths or executing its contents.
   Extra, missing, duplicate or changed inputs require review, not adoption.

Generation-specific object reads/local downloads do not modify GCP resources but
may incur small storage read/transfer charges. No download has been performed here.
Do not substitute the latest object generation or a new checkout for the consumed
archive. A fresh checkout verifies the reference side of a comparison only.

Google's [Cloud Build API definition](https://github.com/googleapis/googleapis/blob/master/google/devtools/cloudbuild/v1/cloudbuild.proto)
defines resolved storage generations and source hashes. `fileHashes` is populated
only when source-provenance hashing is requested; an archive source gets an archive
hash. The pinned configuration does not request `sourceProvenanceHash`, so missing
hashes may be expected and must be reported as unavailable. They are not evidence
that the source matched, and do not authorize another build.

If metadata, immutable source generation, permissions or trusted provenance is
insufficient, leave source identity unverified and stop before activation. Even
matching source bytes do not imply a fully reproducible image: the original
Docker base tag and downloaded package artifacts are not all hash-pinned.
