# oci-shepherd

[![CI](https://github.com/slammingprogramming/oci-shepherd/actions/workflows/ci.yml/badge.svg)](https://github.com/slammingprogramming/oci-shepherd/actions/workflows/ci.yml)
[![License: AGPL v3 or later](https://img.shields.io/badge/License-AGPL%20v3%20or%20later-blue.svg)](LICENSE)

Automates getting and keeping an Oracle Cloud Infrastructure (OCI) "Always
Free" compute instance, for any Always Free-eligible shape you configure
(AMD `VM.Standard.E2.1.Micro`, Ampere `VM.Standard.A1.Flex`, or anything
else your tenancy allows).

Today that's the whole tool - obtain and keep alive one Always Free
instance. The name and the underlying design (wrap the official `oci` CLI
rather than reimplement its API) are deliberately not scoped to just
capacity/free-tier, so the same tool can grow into other OCI-CLI-driven
automation over time without a rename.

It does two jobs today:

1. **Obtain** - repeatedly try to launch the shape you configured, cycling
   through every availability domain (AD) in the region between attempts,
   until one succeeds. OCI's Always Free capacity (especially A1/ARM) is
   frequently exhausted, and capacity is AD-specific, so rotating through
   every AD maximizes your odds.
2. **Monitor** - once an instance is running, periodically confirm it's
   still there. If OCI ever reclaims it (Always Free instances can be
   reclaimed for prolonged inactivity, among other reasons), automatically
   switch back into obtain mode.

The transition between the two modes is automatic - you don't run anything
by hand after the initial setup.

**Out of scope:** this tool's job ends the moment the compute instance
exists and is `RUNNING`. It does not touch the instance's OS, networking,
or any software on it.

## How it works

Everything talks to OCI exclusively through the official `oci` CLI
(`oci compute instance launch`, `oci iam availability-domain list`,
`oci compute instance get`), invoked as subprocesses. This tool never reads,
stores, generates, or transmits OCI credentials - if `oci` already works
when you run it by hand (`~/.oci/config`, or `OCI_CLI_*` environment
variables), it works here with zero extra setup.

Two systemd units carry out the two modes:

- `oci-shepherd-obtain.service` - runs `oci_shepherd.py obtain`.
  Internally loops through your configured availability domains, sleeping
  and retrying, until a launch succeeds. On success it writes the new
  instance's OCID to a state file and runs
  `systemctl start oci-shepherd-monitor.service`, then exits.
- `oci-shepherd-monitor.service` - runs `oci_shepherd.py monitor`.
  Polls the instance recorded in the state file. If it ever finds the
  instance gone (terminated/reclaimed), it clears the state file, runs
  `systemctl start oci-shepherd-obtain.service`, and exits.

Only `oci-shepherd-obtain.service` needs to be enabled at boot. It checks
its state file on every start (including after a host reboot): if that
file already points at a live instance, it hands off straight to the
monitor unit instead of launching a redundant one.

Every attempt - success or failure, in either mode - is appended to a
[JSON Lines](https://jsonlines.org/) log file, one JSON object per line,
so you have a durable, greppable record instead of just console output
that scrolls away.

## Requirements

- Python 3.8+
- [PyYAML](https://pypi.org/project/PyYAML/): `python3 -m pip install pyyaml`
- The [OCI CLI](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm),
  already authenticated (`oci setup config`, or `OCI_CLI_*` env vars).
  Confirm it works before touching this tool:
  ```bash
  oci iam availability-domain list --compartment-id <your compartment OCID>
  ```
- A Linux host with systemd, for the packaged units (the Python script
  itself has no Linux-specific dependency, but the unit files do).
- An existing VCN + subnet in the target region/compartment (this tool
  does not create networking - Always Free includes 2 VCNs to use).
- An SSH key pair (only the public key is needed by this tool).

## Install

```bash
sudo mkdir -p /opt/oci-shepherd /etc/oci-shepherd
sudo cp oci_shepherd.py /opt/oci-shepherd/
sudo cp config.example.yaml /etc/oci-shepherd/config.yaml
sudo nano /etc/oci-shepherd/config.yaml   # fill in your values - see Config reference below

sudo cp systemd/oci-shepherd-obtain.service /etc/systemd/system/
sudo cp systemd/oci-shepherd-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload

# Only enable/start the obtain unit - it starts the monitor unit for you
# on success, and hands off back and forth automatically after that.
sudo systemctl enable --now oci-shepherd-obtain.service
```

Watch progress:

```bash
journalctl -u oci-shepherd-obtain.service -f
tail -f /var/log/oci-shepherd/activity.jsonl
```

Once obtained, `systemctl status oci-shepherd-monitor.service` should show
it active, and `oci-shepherd-obtain.service` will show as inactive (exited
cleanly) until it's needed again.

### Running as a non-root user instead

The unit files default to root, mainly so the tool can call `systemctl
start` on the other unit without extra permission plumbing, and so it
picks up `/root/.oci/config`. If you'd rather not run as root, both unit
files have commented-out `User=`/`Environment=HOME=` lines - uncomment
them, point `HOME` at that user's home directory (so `~/.oci/config`
resolves correctly), and grant that user passwordless `systemctl
start`/`stop` on both units (via a polkit rule or a narrow sudoers entry).
Without that grant, the automatic handoff between obtain and monitor will
fail (harmlessly logged as `systemd_handoff_failed`) and you'll need to
start the other unit yourself.

## Config reference

The config file is YAML. See [`config.example.yaml`](config.example.yaml)
for a fully-commented version with fake placeholder values. Every field
below is exactly what `oci_shepherd.py`'s `load_config()` reads -
if you add a field here, add it there too, and vice versa.

| Field | Required | Description |
|---|---|---|
| `region` | yes | OCI region, e.g. `us-ashburn-1`. |
| `profile` | no | OCI CLI profile name from `~/.oci/config`. Default profile if unset. |
| `compartment_id` | yes | Compartment OCID to launch into. |
| `shape` | yes | Compute shape, e.g. `VM.Standard.A1.Flex` or `VM.Standard.E2.1.Micro`. |
| `shape_config.ocpus` / `shape_config.memory_in_gbs` | required for `.Flex` shapes | OCPU/RAM sizing; stay within your tenancy's Always Free limits. |
| `availability_domains` | yes | `["all"]` to auto-discover every AD in the region, or an explicit list of AD names. |
| `subnet_id` | yes | Subnet OCID for the instance's VNIC. |
| `assign_public_ip` | no (default `true`) | Assign an ephemeral public IP. |
| `image_id` | one of this or the two below | Exact image OCID to launch. |
| `image_operating_system` / `image_operating_system_version` | one of this pair or `image_id` | Resolved to the newest matching image if `image_id` is unset. |
| `ssh_public_key_path` | one of this or `ssh_public_key` | Path to an SSH public key file. |
| `ssh_public_key` | one of this or `ssh_public_key_path` | SSH public key, inline. |
| `display_name` | yes | Instance display name. |
| `boot_volume_size_in_gbs` | yes | Boot volume size in GB. |
| `obtain_retry_interval_seconds` | yes | Delay after a full pass through all ADs before starting another pass. |
| `obtain_inter_ad_delay_seconds` | no (default `5`) | Delay between trying each AD within one pass. |
| `wait_for_running_seconds` | no (default `600`) | How long to wait for a launched instance to reach `RUNNING` before treating the attempt as failed. |
| `monitor_check_interval_seconds` | yes | How often monitor mode polls the instance's state. |
| `rate_limit_backoff_base_seconds` | no (default `30`) | Starting backoff delay on HTTP 429. |
| `rate_limit_backoff_max_seconds` | no (default `900`) | Backoff delay cap. |
| `rate_limit_max_consecutive_retries` | no (default `8`) | Consecutive 429s tolerated on one AD before moving to the next AD. |
| `log_file` | yes | Path to the JSON Lines activity log (parent dir auto-created). |
| `state_file` | yes | Path where the obtained instance's OCID is recorded (parent dir auto-created). |
| `obtain_systemd_unit` | no (default `oci-shepherd-obtain.service`) | Unit name monitor mode starts when the instance is lost. |
| `monitor_systemd_unit` | no (default `oci-shepherd-monitor.service`) | Unit name obtain mode starts on success. |

## Testing without a live OCI account

```bash
python3 oci_shepherd.py selftest --log-file ./selftest.jsonl
```

This exercises the outcome-classification and logging code paths with
canned "capacity error", "rate limited", and "success" responses - no `oci`
CLI or network access required - and prints/writes the resulting log
lines so you can see exactly what each outcome looks like.

For a fuller dry run that exercises the actual `obtain_loop`/`monitor_loop`
functions (AD resolution, image resolution, launch, state file handoff,
systemd handoff) against a scripted fake instead of the real OCI CLI, see
[`test/run_local_test.py`](test/run_local_test.py) and
[`test/fake_oci`](test/fake_oci) (a minimal fake `oci` CLI you can put
ahead of the real one on `PATH` for manual testing on a Linux box).

## Troubleshooting

### "Why is this taking so long?"

This is expected, not a bug. Always Free Ampere A1 capacity is well known
in the OCI community to be scarce and highly region/AD-dependent - reports
of retry tools like this one running anywhere from a few minutes to
several weeks (occasionally longer) before landing capacity are common.
See [hitrov/oci-arm-host-capacity](https://github.com/hitrov/oci-arm-host-capacity)
and its issue tracker for a large collection of real-world timing reports
across regions. There is no way to promise a completion time - the tool's
job is to keep trying efficiently (every AD, with sane backoff) for as
long as it takes, not to guarantee a specific wait.

Things that actually help:
- Set `availability_domains: ["all"]` so every AD in the region is tried,
  not just one.
- Consider trying multiple regions (run a separate instance of this tool,
  with a separate config/log/state file, per region) if your workload
  doesn't require a specific one.
- The AMD `VM.Standard.E2.1.Micro` shape is generally far less contested
  than `VM.Standard.A1.Flex` - if ARM isn't a hard requirement, it will
  typically succeed much faster.

### Rate limiting (HTTP 429)

OCI's API rate-limits aggressive retry loops, which a tool like this one
will inevitably run into over a long obtain phase. When that happens you'll
see log lines like:

```json
{"event": "rate_limited", "availability_domain": "...", "detail": "{\"code\": \"TooManyRequests\", ...}", "backoff_seconds": 41.7, "consecutive_rate_limits": 2, ...}
```

This is expected and handled: the tool backs off exponentially (with
jitter) starting at `rate_limit_backoff_base_seconds`, capped at
`rate_limit_backoff_max_seconds`, and retries the same AD rather than
failing outright. A tight loop with no backoff is a known way to make rate
limiting worse and get temporarily throttled harder - that's specifically
what this backoff avoids. If you see `rate_limit_giving_up_on_ad` after
`rate_limit_max_consecutive_retries` consecutive 429s, the tool has moved
on to the next AD rather than waiting forever on one; it will come back to
that AD on the next pass.

### "OCI CLI ('oci') was not found on PATH"

Install the OCI CLI and confirm `oci --version` works for the same user/
environment this tool runs as (root, by default, per the systemd units).

### "The OCI CLI could not authenticate / list availability domains"

Your `~/.oci/config` (or `OCI_CLI_*` environment variables) aren't set up,
or are set up for a different user than the one running the systemd unit.
Confirm manually first:

```bash
oci iam availability-domain list --compartment-id <compartment OCID>
```

If that fails, fix it with `oci setup config` before touching this tool -
this tool has no credential-handling of its own to debug.

### Config errors

`load_config()` validates the config file before doing anything else and
fails with a specific message (missing file, invalid YAML, missing
required field, malformed-looking OCID, missing SSH key, etc.) rather
than a raw stack trace. If you see a Python traceback instead, please
treat it as a bug in this tool and open an issue/PR.

### The obtain unit keeps restarting every 60 seconds

Check `journalctl -u oci-shepherd-obtain.service` for a `fatal_error` log
line - this means the tool hit something retrying won't fix (bad auth,
missing config field, etc.), exited non-zero, and systemd restarted it per
`Restart=on-failure`. Fix the underlying config/auth issue; the loop is
intentional insurance against transient crashes, not the tool's normal
retry mechanism (normal capacity retries are logged as `launch_failed` /
`obtain_pass_complete` and do not exit the process).

## Contributing

Bug reports, PRs, and questions are welcome - see
[CONTRIBUTING.md](CONTRIBUTING.md) for how to run this tool's tests
without needing a live OCI account, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
for community expectations. Found a security issue? See
[SECURITY.md](SECURITY.md) instead of a public issue.

## License

Copyright (C) 2026 the oci-shepherd contributors.

Licensed under the GNU Affero General Public License, version 3 or (at
your option) any later version - see [LICENSE](LICENSE) for the full
text. In short: you're free to use, modify, and redistribute this tool,
including running a modified version as a network service, provided that
a modified network-service version also makes its source available to the
users interacting with it over the network (the "AGPL" difference from
plain GPL). This tool ships with zero credential-handling code of its own
by design (see "How it works" above) - that stays true regardless of how
you deploy or modify it.
