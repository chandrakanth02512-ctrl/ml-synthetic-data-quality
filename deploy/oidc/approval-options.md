# Approval choices for a sole repository owner

**Design approved; live activation disabled. No GitHub settings were changed.**
The owner explicitly approved the solo-owner policy as a design choice only and
accepted its lack of independent review. Configuration now records `solo-owner`
and its design authorization, while deployment/execution remain disabled.

The previous independent mode required a reviewer other than the dispatcher. With
`prevent_self_review: true`, the sole owner cannot approve their own dispatch.
That mode remains supported but is no longer the selected design.
GitHub required reviewers need at least read access, and
one listed reviewer approval is sufficient; multiple listed reviewers do not
establish an all-reviewers quorum. Separate `gcp-deploy` and `gcp-execute`
environments still gate the two operations independently.

| Choice | Protection and tradeoff | Change needed before use |
|---|---|---|
| Explicit single-owner approval (selected design for this personal project) | Existing owner dispatches and separately approves each environment. Retains a deliberate second confirmation, but supplies no independent person and cannot protect against compromise of that owner account. | Design approval is recorded; live settings still require separate approval. Keep only the exact owner as required reviewer, disabled administrator bypass, protected main, exact-main restrictions and separate dispatches. No second account is required. |
| Add a distinct trusted reviewer | Stronger independent review. A second repository owner is unnecessary; use the minimum reviewer access the repository supports. | Owner selects an eligible reviewer and configures both environments. No weakening of `prevent_self_review`. |
| Independently managed external approval | A custom environment protection rule can consult an external approval system. Useful when that system supplies real independent authorization. Requires support, trusted integration, availability and possible cost review. | Separate design and owner approval; review any credential needs against the no-long-lived-credential requirement. Current guard still requires native non-self reviewers and does not implement this alternative. |
| Keep automation disabled | Continue review and future explicitly approved keyless Cloud Shell operations while no independent reviewer or revised policy is selected. | No approval-policy change now. Any later cloud operation still needs its own approval. |

For the single-owner option, retain strong account authentication, reviewed main
changes, exact digest/UID confirmations, limited short-lived identities and no
automatic triggers. Those controls reduce risk but do not create independent
approval. A wait timer provides a delay, not human authorization. Administrator
bypass is not a substitute for the chosen approval process.

## Prepared implementation, not authorization

`config.json` now records the owner's approved design below, with both bypass
attestations still `false`. This authorization is for the design, not live settings,
IAM, a merge, deployment or execution:

```json
{
  "approval_mode": "solo-owner",
  "solo_owner_approval_authorized": true,
  "enabled": false
}
```

After this approved code/config choice, live settings and activation remain
separate approvals. Each environment must have exactly one required User reviewer
with numeric ID `341606319`, `prevent_self_review: false`, administrator bypass
disabled and exactly one selected branch rule `main`. The independent mode still
requires non-self review. No mode accepts absent reviewers or bypass-enabled rules.

The dispatcher must be owner ID `341606319` in workflow/runtime and planned WIF CEL.
Only `run_attempt == 1` is allowed; a rerun is rejected. Solo mode additionally
checks protected main and its current commit before requesting GCP credentials.
The manual action/digest and execution UID confirmations, separate environments,
two limited identities, shared non-cancelling concurrency and no write retries
remain enforced. Environment approval and owner confirmation occur separately for
deployment and execution. Existing credentials/receipts are never used as approval.

For a solo-maintained protected main, require PR-based changes and passing CI,
block force-push/deletion and explicitly review before merging. Requiring a second
person's PR approval would also deadlock a sole owner; zero mandatory peer reviews
can be used with owner review/merge and required CI. This provides owner governance,
not independent review. No branch protection or environment setting was changed.

Required-reviewer, environment and custom-protection features depend on repository
visibility, plan and integration support. The repository was observed as public;
actual environment settings/features remain unverified because the GitHub connector
does not expose that endpoint. Verify availability before activating the design.

No live policy is activated by this PR update. Both bypass attestations remain
`false`, and `enabled` remains `false`. The next step is the read-only preflight;
future live settings, setup/merge, deployment and execution need separate approval.

Sources reviewed: GitHub's [environment protection reference](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
and [deployment review procedure](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/review-deployments).
Their current source was read from the official `github/docs` repository during
this review; no environment configuration was performed.
