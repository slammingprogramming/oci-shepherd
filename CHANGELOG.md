# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-08-29

### Added

- Initial release: `obtain` mode (cycles availability domains, exponential
  backoff with jitter on HTTP 429, classifies capacity/rate-limit/fatal
  errors) and `monitor` mode, with automatic systemd handoff in both
  directions and reboot-safe resume via a state file.
- `selftest` mode and a mocked end-to-end test harness
  (`test/run_local_test.py`, `test/fake_oci`) requiring no live OCI account.
- systemd units for both modes, `config.example.yaml`, and full README
  documentation.

[Unreleased]: https://github.com/slammingprogramming/oci-shepherd/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/slammingprogramming/oci-shepherd/releases/tag/v0.1.0
