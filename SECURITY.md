# Security Policy

## Scope

oci-shepherd deliberately has no credential-handling code of its
own - it shells out to the official `oci` CLI, which does all
authentication using its own standard mechanisms (`~/.oci/config` or
`OCI_CLI_*` environment variables). If you find a way for this tool to
leak, log, or mishandle credentials, that is very much in scope.

Also in scope:

- Command injection via config values (shape, display name, OCIDs, etc.)
  into the `oci` CLI invocations this tool builds.
- The systemd units running with more privilege than documented/necessary.
- Anything that could cause this tool to launch or terminate instances the
  user did not intend.

Out of scope: vulnerabilities in the `oci` CLI itself, the OCI platform, or
the operating system/software running on an instance this tool launched -
report those to Oracle.

## Reporting a vulnerability

Please open a GitHub issue for non-sensitive reports. Include:

- The config (redact real OCIDs/tenancy details - shape/structure is fine)
  and command that triggers the issue.
- What you expected vs. what happened.

### Sensitive reports (anything you don't want public before a fix exists)

Don't put any vulnerability details in a public issue or in first contact.
Instead:

1. Open a GitHub issue that states only that you have a security report
   and want to coordinate privately - no vulnerability details, just that
   an issue exists.
2. Contact me on [SimpleX Chat](https://smp14.simplex.im/a#3gZ-zeHs4QrFZKLAN0o3SC_XQJXhj1eYBVTO_c0FAtg).
3. We'll do a mutual public-key-signature exchange tied to that GitHub
   issue - each of us signs a message referencing the issue - so you can
   confirm you're actually talking to the maintainer, and I can confirm
   you control the GitHub account that opened it.
4. Once that verification checks out both ways, we'll handle the full
   report over SimpleX.

You only need to do this verification once. After that, my SimpleX
profile from that exchange is your standing, verified channel to me -
message me there directly for any future report and I'll be able to
attribute it to your verified identity without repeating the process.

If you'd rather not use SimpleX, a regular public GitHub issue is fine for
anything that doesn't need private handling.

There's no bug bounty here - this is a small community tool - but reports
are taken seriously and a fix or mitigation will be prioritized.
