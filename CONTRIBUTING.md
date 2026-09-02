# Contributing to oci-shepherd

Thanks for considering a contribution. This is a small, single-file CLI
tool with a narrow scope by design (see "Explicitly out of scope" in the
[README](README.md)) - keep that in mind before proposing larger features.

## Reporting bugs

Open an issue with:

- What you ran (`obtain`/`monitor`/`selftest`) and your OCI region/shape
  (no need to share your actual OCIDs - fake/redact them).
- The relevant JSON Lines log entries (`log_file`), not just a description.
- Whether the failure came from the tool itself (a Python traceback) or
  from the `oci` CLI (a `ServiceError` JSON blob) - these need different
  fixes.

## Development setup

```bash
git clone <this repo>
cd oci-shepherd
python3 -m pip install -r requirements.txt
```

No live OCI account is required to work on this tool. Two testing paths
exist specifically so you don't need one:

```bash
# Exercises outcome classification + logging with canned responses.
python3 oci_shepherd.py selftest --log-file ./selftest.jsonl

# Exercises the full obtain_loop/monitor_loop functions (AD resolution,
# image resolution, launch, state file handoff, systemd handoff) against
# an in-process fake of the `oci` CLI.
python3 test/run_local_test.py
```

Run both, and `python3 -m py_compile oci_shepherd.py`, before
opening a PR. If you have access to a systemd host, also sanity-check the
unit files:

```bash
systemd-analyze verify systemd/oci-shepherd-obtain.service
systemd-analyze verify systemd/oci-shepherd-monitor.service
```

## Pull requests

- Keep the [README](README.md)'s config reference table in sync with
  `REQUIRED_CONFIG_FIELDS` / `config.setdefault(...)` calls in
  `oci_shepherd.py` if you add, rename, or remove a config field -
  and update `config.example.yaml` too. Docs/code drift on field names is
  the most common way this tool breaks silently for users.
- No new dependencies beyond PyYAML without discussion first - part of the
  point of this tool is that it has almost no attack surface/install
  burden beyond the OCI CLI itself.
- This project is licensed AGPL-3.0-or-later (see [LICENSE](LICENSE)); by
  submitting a PR you agree your contribution is licensed under the same
  terms.
