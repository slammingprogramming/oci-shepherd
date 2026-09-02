## What does this change?

## Why?

## Checklist

- [ ] `python3 -m py_compile oci_shepherd.py` passes
- [ ] `python3 oci_shepherd.py selftest --log-file ./selftest.jsonl` passes
- [ ] `python3 test/run_local_test.py` passes
- [ ] If I added/renamed/removed a config field: `config.example.yaml` and
      the README's config reference table are both updated to match
      `oci_shepherd.py`
- [ ] If I touched the systemd units: `systemd-analyze verify
      systemd/*.service` passes (if you have a systemd host to test on)
- [ ] I agree this contribution is licensed under this repo's
      AGPL-3.0-or-later license (see CONTRIBUTING.md)
