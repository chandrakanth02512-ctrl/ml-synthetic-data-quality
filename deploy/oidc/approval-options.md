# Approval choices for a sole repository owner

**Review only. The workflows, provider trust, approval guard and configuration gates
retain their current policy. No GitHub environment settings were changed.**

The current design requires a reviewer other than the person dispatching each
workflow. With `prevent_self_review: true`, the sole owner cannot dispatch and then
approve that same run. GitHub required reviewers need at least read access, and
one listed reviewer approval is sufficient; multiple listed reviewers do not
establish an all-reviewers quorum. Separate `gcp-deploy` and `gcp-execute`
environments still gate the two operations independently.

| Choice | Protection and tradeoff | Change needed before use |
|---|---|---|
| Add a distinct trusted reviewer (recommended) | Preserves independent review and the current guard. A second repository owner is unnecessary; use the minimum reviewer access the repository supports. | Owner selects an eligible reviewer and configures both environments. No weakening of `prevent_self_review`. |
| Explicit single-owner approval | Owner dispatches and separately approves each environment. Retains a deliberate second confirmation, but supplies no independent person and cannot protect against compromise of that owner account. | Separately approve a code/test policy change allowing self-review plus environment changes. Keep required owner review, disabled administrator bypass, exact-main restrictions and separate dispatches. Current code rejects this choice. |
| Independently managed external approval | A custom environment protection rule can consult an external approval system. Useful when that system supplies real independent authorization. Requires support, trusted integration, availability and possible cost review. | Separate design and owner approval; review any credential needs against the no-long-lived-credential requirement. Current guard still requires native non-self reviewers and does not implement this alternative. |
| Keep automation disabled | Continue review and future explicitly approved keyless Cloud Shell operations while no independent reviewer or revised policy is selected. | No approval-policy change now. Any later cloud operation still needs its own approval. |

For the single-owner option, retain strong account authentication, reviewed main
changes, exact digest/UID confirmations, limited short-lived identities and no
automatic triggers. Those controls reduce risk but do not create independent
approval. A wait timer provides a delay, not human authorization. Administrator
bypass is not a substitute for the chosen approval process.

Required-reviewer, environment and custom-protection features depend on repository
visibility, plan and integration support. The repository was observed as public;
actual environment settings/features remain unverified because the GitHub connector
does not expose that endpoint. Verify availability before selecting a design.

No choice is applied by this PR update. Both bypass attestations remain `false`,
and `enabled` remains `false`. Select and explicitly approve a policy before any
future code/settings change; setup/merge, deployment and execution remain separate.

Sources reviewed: GitHub's [environment protection reference](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)
and [deployment review procedure](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/review-deployments).
Their current source was read from the official `github/docs` repository during
this review; no environment configuration was performed.
