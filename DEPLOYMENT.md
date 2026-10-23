# Fork deployment branch

`gobeyondpty/swoop:deploy` is the consolidated branch used by our servers. It
contains upstream `main`, our focused upstream pull requests, and the additional
runtime features used by ndcterm. `fork/main` also includes this consolidated
history. Upstream PR branches remain focused on their individual changes.

Before promoting a new deployment commit:

1. Merge the intended PR work into `deploy`, preserving all currently deployed
   commits. Resolve conflicts against the complete runtime, including diagnostics,
   response observers, retained selector pricing, transport deadlines, airport
   alternatives, currency, QPX filters, calendars, and price insights.
2. Install the updated package metadata, run `make check`, and run the relevant
   live RPC canaries. Keep fixtures free of live tokens and session data.
3. Push `deploy` and synchronize `fork/main`. Pin ndcterm's
   `apps/swoop-service/requirements.txt` to the full immutable deployment SHA,
   update its checked CI supply bundle/manifest, and run its producer tests.
4. Ship through ndcterm's normal develop integration and production release
   workflow. Verify the installed SHA on both servers; the package version alone
   does not identify fork commits.

Do not pin a server to an individual upstream PR branch. Do not replace the pin
with a moving branch name: the branch is where changes are consolidated, and the
immutable commit is the reproducible release artifact.
