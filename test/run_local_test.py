#!/usr/bin/env python3
"""
Local verification harness (not part of the shipped tool).

Monkeypatches subprocess.run inside oci_shepherd so the obtain and
monitor loops can be exercised end-to-end -- AD resolution, image
resolution, launch, classification of capacity/rate-limit/success outcomes,
state file handoff -- without a real OCI CLI or network access. Useful on
dev machines (e.g. Windows) where the OCI CLI and systemd aren't available.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import oci_shepherd as w  # noqa: E402

CALL_COUNTS = {}


def fake_run(args, **kwargs):
    class Result:
        pass

    r = Result()
    r.stdout = ""
    r.stderr = ""
    r.returncode = 0

    if args[0] == "systemctl":
        return r  # simulate a successful handoff

    if "availability-domain" in args and "list" in args:
        r.stdout = json.dumps({"data": [
            {"name": "EXAMPLE:US-ASHBURN-AD-1"},
            {"name": "EXAMPLE:US-ASHBURN-AD-2"},
            {"name": "EXAMPLE:US-ASHBURN-AD-3"},
        ]})
        return r

    if "image" in args and "list" in args:
        r.stdout = json.dumps({"data": [{"id": "ocid1.image.oc1..aaaaaaaafaketest"}]})
        return r

    if "instance" in args and "launch" in args:
        ad = args[args.index("--availability-domain") + 1]
        if "AD-1" in ad:
            r.returncode = 1
            r.stderr = json.dumps({
                "code": "InternalError",
                "message": "Out of host capacity for shape in this availability domain.",
            })
            return r
        if "AD-2" in ad:
            n = CALL_COUNTS.get(ad, 0) + 1
            CALL_COUNTS[ad] = n
            if n == 1:
                r.returncode = 1
                r.stderr = json.dumps({"code": "TooManyRequests", "message": "429 Too many requests"})
                return r
        r.stdout = json.dumps({"data": {
            "id": "ocid1.instance.oc1..aaaaaaaafakeinstance",
            "lifecycle-state": "RUNNING",
        }})
        return r

    if "instance" in args and "get" in args:
        r.stdout = json.dumps({"data": {"lifecycle-state": "RUNNING"}})
        return r

    r.returncode = 1
    r.stderr = f"unhandled fake args: {args}"
    return r


def main():
    subprocess.run = fake_run  # patch globally; only this process is affected
    w.check_oci_cli_installed = lambda: None  # skip PATH check; CLI presence is out of scope here

    tmpdir = Path(tempfile.mkdtemp(prefix="oci-shepherd-test-"))
    config = {
        "region": "us-ashburn-1",
        "compartment_id": "ocid1.compartment.oc1..aaaaaaaaexampleexampleexample",
        "profile": None,
        "shape": "VM.Standard.A1.Flex",
        "shape_config": {"ocpus": 4, "memory_in_gbs": 24},
        "availability_domains": ["all"],
        "subnet_id": "ocid1.subnet.oc1..aaaaaaaaexampleexampleexample",
        "image_id": None,
        "image_operating_system": "Canonical Ubuntu",
        "image_operating_system_version": "22.04",
        "assign_public_ip": True,
        "display_name": "test-instance",
        "boot_volume_size_in_gbs": 50,
        "ssh_public_key": "ssh-ed25519 AAAAExampleKeyOnly test@example",
        "ssh_public_key_path": None,
        "obtain_retry_interval_seconds": 1,
        "obtain_inter_ad_delay_seconds": 0,
        "monitor_check_interval_seconds": 1,
        "wait_for_running_seconds": 30,
        "rate_limit_backoff_base_seconds": 1,
        "rate_limit_backoff_max_seconds": 2,
        "rate_limit_max_consecutive_retries": 8,
        "log_file": str(tmpdir / "activity.jsonl"),
        "state_file": str(tmpdir / "state.json"),
        "obtain_systemd_unit": "oci-shepherd-obtain.service",
        "monitor_systemd_unit": "oci-shepherd-monitor.service",
    }

    log = w.ActivityLog(config["log_file"])

    print("=== Running obtain_loop(one_pass=True) against mocked oci CLI ===\n")
    instance_id = w.obtain_loop(config, log, one_pass=True)
    print(f"\nobtain_loop returned instance_id={instance_id}")

    print("\n=== State file contents ===")
    print((tmpdir / "state.json").read_text())

    print("=== Running monitor_loop for 2 iterations against mocked oci CLI ===\n")
    w.monitor_loop(config, log, max_iterations=2)

    print("\n=== Full JSONL log ===")
    print((tmpdir / "activity.jsonl").read_text())

    # Sanity assertions.
    lines = [json.loads(l) for l in (tmpdir / "activity.jsonl").read_text().splitlines()]
    events = [l["event"] for l in lines]
    assert "launch_failed" in events, "expected a capacity-error launch_failed event"
    assert "rate_limited" in events, "expected a rate_limited event"
    assert "launch_success" in events, "expected a launch_success event"
    assert "monitor_check" in events, "expected a monitor_check event"
    print("\nAll sanity assertions passed.")


if __name__ == "__main__":
    main()
