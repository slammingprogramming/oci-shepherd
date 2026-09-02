#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
oci-shepherd

Repeatedly attempts to launch an OCI Always Free compute instance (cycling
through availability domains between attempts) until it succeeds, then
switches to a monitor mode that watches the instance and re-triggers the
obtain step if it ever disappears.

All OCI API interaction goes through the official `oci` CLI via subprocess.
This script never reads, stores, or transmits OCI credentials itself - the
`oci` CLI's own authentication (~/.oci/config or OCI_CLI_* environment
variables) is used exactly as it would be if you ran `oci` by hand.

See README.md for full documentation and config.example.yaml for the config
reference.

Copyright (C) 2026 the oci-shepherd contributors.

This program is free software: you can redistribute it and/or modify it
under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or (at your
option) any later version. This program is distributed WITHOUT ANY
WARRANTY; see the GNU Affero General Public License for details. You
should have received a copy of the license along with this program in the
LICENSE file; if not, see <https://www.gnu.org/licenses/>.
"""

import argparse
import json
import random
import shutil
import subprocess
import sys
import time
import shlex
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print(
        "ERROR: the 'PyYAML' package is required but not installed.\n"
        "Install it with:\n"
        "    python3 -m pip install pyyaml\n",
        file=sys.stderr,
    )
    sys.exit(1)


REQUIRED_CONFIG_FIELDS = [
    "region",
    "compartment_id",
    "shape",
    "subnet_id",
    "availability_domains",
    "display_name",
    "boot_volume_size_in_gbs",
    "log_file",
    "state_file",
    "obtain_retry_interval_seconds",
    "monitor_check_interval_seconds",
]

FLEX_SHAPE_SUFFIX = ".Flex"


class ConfigError(Exception):
    """Raised for any problem with the config file that the user must fix."""


class FatalSetupError(Exception):
    """Raised for environment problems (missing CLI, bad auth, etc.)."""


# --------------------------------------------------------------------------
# Logging - structured JSON Lines to a file, human-readable line to stdout
# (stdout is captured by journald when run under systemd).
# --------------------------------------------------------------------------

class ActivityLog:
    def __init__(self, log_file_path):
        self.path = Path(log_file_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event, **fields):
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
        }
        record.update(fields)
        line = json.dumps(record, sort_keys=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        # Mirror a compact human-readable line to stdout/journald.
        summary = " ".join(f"{k}={v}" for k, v in fields.items())
        print(f"[{record['timestamp']}] {event} {summary}", flush=True)


# --------------------------------------------------------------------------
# Config loading and validation
# --------------------------------------------------------------------------

def load_config(config_path):
    path = Path(config_path).expanduser()
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(
            f"Config file is not valid YAML ({path}): {e}\n\n"
            "This is often caused by inconsistent leading whitespace - e.g. "
            "uncommenting a line like '# image_id: ...' by deleting only the "
            "'#' and leaving a stray leading space behind (' image_id: ...'), "
            "which YAML reads as an indentation error. Check the line number "
            "above for a stray leading space or tab."
        )

    if not isinstance(config, dict):
        raise ConfigError(f"Config file must be a YAML mapping of settings: {path}")

    missing = [f for f in REQUIRED_CONFIG_FIELDS if config.get(f) in (None, "", [])]
    if missing:
        raise ConfigError(
            "Config file is missing required field(s): "
            + ", ".join(missing)
            + f"\nSee config.example.yaml for the full reference. ({path})"
        )

    if not str(config["compartment_id"]).startswith(("ocid1.compartment.", "ocid1.tenancy.")):
        raise ConfigError(
            "compartment_id does not look like a compartment OCID "
            "(expected it to start with 'ocid1.compartment.', or "
            "'ocid1.tenancy.' if you're launching into the root compartment)."
        )
    if not str(config["subnet_id"]).startswith("ocid1.subnet."):
        raise ConfigError(
            "subnet_id does not look like a subnet OCID (expected it to "
            "start with 'ocid1.subnet.')."
        )

    image_id = config.get("image_id")
    image_os = config.get("image_operating_system")
    image_os_version = config.get("image_operating_system_version")
    if not image_id and not (image_os and image_os_version):
        raise ConfigError(
            "Config must set either 'image_id' (a specific image OCID) or "
            "both 'image_operating_system' and 'image_operating_system_version' "
            "so an image can be resolved."
        )
    if image_id and not str(image_id).startswith("ocid1.image."):
        raise ConfigError(
            "image_id does not look like an image OCID (expected it to "
            "start with 'ocid1.image.')."
        )

    ssh_key_path = config.get("ssh_public_key_path")
    ssh_key_inline = config.get("ssh_public_key")
    if not ssh_key_path and not ssh_key_inline:
        raise ConfigError(
            "Config must set either 'ssh_public_key_path' or 'ssh_public_key' "
            "so the launched instance is reachable over SSH."
        )
    if ssh_key_path:
        key_path = Path(ssh_key_path).expanduser()
        if not key_path.is_file():
            raise ConfigError(f"ssh_public_key_path does not exist: {key_path}")

    shape = config["shape"]
    if shape.endswith(FLEX_SHAPE_SUFFIX):
        shape_config = config.get("shape_config") or {}
        if "ocpus" not in shape_config or "memory_in_gbs" not in shape_config:
            raise ConfigError(
                f"Shape '{shape}' is a Flex shape and requires 'shape_config' "
                "with 'ocpus' and 'memory_in_gbs' set in the config file."
            )

    # Defaults for optional fields.
    config.setdefault("profile", None)
    config.setdefault("assign_public_ip", True)
    config.setdefault("obtain_inter_ad_delay_seconds", 5)
    config.setdefault("wait_for_running_seconds", 600)
    config.setdefault("rate_limit_backoff_base_seconds", 30)
    config.setdefault("rate_limit_backoff_max_seconds", 900)
    config.setdefault("rate_limit_max_consecutive_retries", 8)
    config.setdefault("obtain_systemd_unit", "oci-shepherd-obtain.service")
    config.setdefault("monitor_systemd_unit", "oci-shepherd-monitor.service")

    return config


# --------------------------------------------------------------------------
# oci CLI wrapper
# --------------------------------------------------------------------------

def check_oci_cli_installed():
    if shutil.which("oci") is None:
        raise FatalSetupError(
            "The OCI CLI ('oci') was not found on PATH.\n"
            "Install it following Oracle's instructions:\n"
            "  https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm\n"
            "and confirm it works with:\n"
            "    oci --version"
        )


def base_oci_args(config):
    args = ["oci", "--region", config["region"]]
    if config.get("profile"):
        args += ["--profile", config["profile"]]
    return args


def run_oci_json(args, timeout=120):
    """Run an `oci` CLI command and return (returncode, parsed_json_or_None, raw_stdout, raw_stderr)."""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        raise FatalSetupError("The OCI CLI ('oci') was not found on PATH.")
    except subprocess.TimeoutExpired:
        return 1, None, "", f"Command timed out after {timeout}s: {' '.join(args)}"

    parsed = None
    if proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError:
            parsed = None
    return proc.returncode, parsed, proc.stdout, proc.stderr


def check_oci_auth(config, log):
    """Preflight check: confirm the CLI is installed and can authenticate."""
    check_oci_cli_installed()
    args = base_oci_args(config) + [
        "iam", "availability-domain", "list",
        "--compartment-id", config["compartment_id"],
    ]
    rc, parsed, stdout, stderr = run_oci_json(args, timeout=60)
    if rc != 0:
        raise FatalSetupError(
            "The OCI CLI could not authenticate / list availability domains.\n"
            "This usually means ~/.oci/config is missing or invalid, or the\n"
            "OCI_CLI_* environment variables are not set. Confirm 'oci' works\n"
            "manually first, e.g.:\n"
            "    oci iam availability-domain list --compartment-id "
            f"{config['compartment_id']}\n\n"
            f"CLI stderr:\n{stderr.strip()}"
        )
    return parsed


def resolve_availability_domains(config, log):
    ad_config = config["availability_domains"]
    if isinstance(ad_config, str):
        ad_config = [ad_config]
    if len(ad_config) == 1 and str(ad_config[0]).strip().lower() == "all":
        parsed = check_oci_auth(config, log)
        ads = [item["name"] for item in parsed.get("data", [])]
        if not ads:
            raise FatalSetupError(
                "No availability domains were returned for this compartment/region."
            )
        return ads
    return list(ad_config)


def resolve_image_id(config, log):
    if config.get("image_id"):
        return config["image_id"]

    args = base_oci_args(config) + [
        "compute", "image", "list",
        "--compartment-id", config["compartment_id"],
        "--operating-system", config["image_operating_system"],
        "--operating-system-version", str(config["image_operating_system_version"]),
        "--shape", config["shape"],
        "--sort-by", "TIMECREATED",
        "--sort-order", "DESC",
    ]
    rc, parsed, stdout, stderr = run_oci_json(args, timeout=60)
    if rc != 0 or not parsed or not parsed.get("data"):
        raise FatalSetupError(
            "Could not resolve an image OCID from image_operating_system="
            f"{config['image_operating_system']!r} / "
            f"image_operating_system_version={config['image_operating_system_version']!r} "
            f"for shape {config['shape']!r}.\n"
            "Set 'image_id' explicitly instead.\n"
            f"CLI stderr:\n{stderr.strip()}"
        )
    image_id = parsed["data"][0]["id"]
    log.write("image_resolved", image_id=image_id)
    return image_id


def read_ssh_public_key(config):
    if config.get("ssh_public_key"):
        return config["ssh_public_key"].strip()
    key_path = Path(config["ssh_public_key_path"]).expanduser()
    return key_path.read_text(encoding="utf-8").strip()


def build_launch_args(config, ad, image_id, ssh_key):
    args = base_oci_args(config) + [
        "compute", "instance", "launch",
        "--availability-domain", ad,
        "--compartment-id", config["compartment_id"],
        "--shape", config["shape"],
        "--subnet-id", config["subnet_id"],
        "--image-id", image_id,
        "--display-name", config["display_name"],
        "--boot-volume-size-in-gbs", str(config["boot_volume_size_in_gbs"]),
        "--assign-public-ip", "true" if config.get("assign_public_ip", True) else "false",
        "--metadata", json.dumps({"ssh_authorized_keys": ssh_key}),
        "--wait-for-state", "RUNNING",
        "--max-wait-seconds", str(config["wait_for_running_seconds"]),
    ]
    shape = config["shape"]
    if shape.endswith(FLEX_SHAPE_SUFFIX):
        shape_config = config["shape_config"]
        args += [
            "--shape-config",
            json.dumps(
                {
                    "ocpus": shape_config["ocpus"],
                    "memoryInGBs": shape_config["memory_in_gbs"],
                }
            ),
        ]
    return args


# --------------------------------------------------------------------------
# Outcome classification for launch attempts
# --------------------------------------------------------------------------

RATE_LIMIT_MARKERS = ["TooManyRequests", "429", "Too many requests"]
CAPACITY_MARKERS = [
    "OutOfCapacity",
    "Out of host capacity",
    "out of capacity",
    "LimitExceeded",
    "InternalError",
    "InsufficientCapacity",
]
FATAL_MARKERS = [
    "NotAuthenticated",
    "NotAuthorized",
    "Could not find config file",
    "InvalidParameter",
    "config file not found",
]


def classify_failure(stdout, stderr):
    text = f"{stdout}\n{stderr}"
    for marker in FATAL_MARKERS:
        if marker in text:
            return "fatal"
    for marker in RATE_LIMIT_MARKERS:
        if marker in text:
            return "rate_limited"
    for marker in CAPACITY_MARKERS:
        if marker in text:
            return "capacity_error"
    return "unknown_error"


def truncate(text, limit=500):
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "... (truncated)"


# --------------------------------------------------------------------------
# State file (shares the obtained instance's OCID between obtain <-> monitor)
# --------------------------------------------------------------------------

def write_state(config, instance_id, ad):
    state_path = Path(config["state_file"]).expanduser()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "instance_id": instance_id,
        "availability_domain": ad,
        "shape": config["shape"],
        "obtained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def read_state(config):
    state_path = Path(config["state_file"]).expanduser()
    if not state_path.is_file():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def clear_state(config):
    state_path = Path(config["state_file"]).expanduser()
    if state_path.is_file():
        state_path.unlink()


# --------------------------------------------------------------------------
# systemd handoff helpers
# --------------------------------------------------------------------------

def systemctl_start(unit_name, log):
    try:
        subprocess.run(["systemctl", "start", unit_name], check=False, timeout=30)
        log.write("systemd_handoff", target_unit=unit_name, action="start")
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.write(
            "systemd_handoff_failed",
            target_unit=unit_name,
            error=str(e),
            hint="Not running under systemd, or systemctl unavailable; "
                 "start the other unit manually.",
        )


# --------------------------------------------------------------------------
# Obtain mode
# --------------------------------------------------------------------------

def try_resume_from_state(config, log):
    """
    If a state file from a previous successful obtain already points at a
    live instance (e.g. this process restarted, or the host rebooted),
    hand off straight to monitor instead of launching a redundant instance.
    """
    state = read_state(config)
    if not state or not state.get("instance_id"):
        return None
    instance_id = state["instance_id"]
    lifecycle_state, error = get_instance_state(config, instance_id)
    if lifecycle_state in ("RUNNING", "STARTING", "PROVISIONING"):
        log.write("resume_existing_instance", instance_id=instance_id, lifecycle_state=lifecycle_state)
        systemctl_start(config["monitor_systemd_unit"], log)
        return instance_id
    log.write(
        "stale_state_discarded",
        instance_id=instance_id,
        lifecycle_state=lifecycle_state,
        detail=truncate(error or ""),
    )
    clear_state(config)
    return None


def obtain_loop(config, log, one_pass=False):
    check_oci_cli_installed()

    resumed = try_resume_from_state(config, log)
    if resumed:
        return resumed

    ads = resolve_availability_domains(config, log)
    log.write("obtain_start", shape=config["shape"], availability_domains=ads)

    image_id = resolve_image_id(config, log)
    ssh_key = read_ssh_public_key(config)

    while True:
        for ad in ads:
            args = build_launch_args(config, ad, image_id, ssh_key)
            consecutive_rate_limits = 0

            # Inner retry loop: keeps retrying *this same* AD while the API
            # is rate-limiting us, up to rate_limit_max_consecutive_retries,
            # before giving up on this AD for the current pass.
            while True:
                log.write("launch_attempt", availability_domain=ad, shape=config["shape"])
                rc, parsed, stdout, stderr = run_oci_json(
                    args, timeout=config["wait_for_running_seconds"] + 60
                )

                if rc == 0 and parsed and parsed.get("data", {}).get("id"):
                    instance_id = parsed["data"]["id"]
                    log.write(
                        "launch_success",
                        availability_domain=ad,
                        shape=config["shape"],
                        instance_id=instance_id,
                    )
                    write_state(config, instance_id, ad)
                    systemctl_start(config["monitor_systemd_unit"], log)
                    return instance_id

                outcome = classify_failure(stdout, stderr)
                detail = truncate(stderr or stdout or "no output")

                if outcome == "fatal":
                    log.write(
                        "launch_fatal_error",
                        availability_domain=ad,
                        shape=config["shape"],
                        detail=detail,
                    )
                    raise FatalSetupError(
                        "The OCI CLI returned an authentication/configuration error "
                        "that will not be fixed by retrying. Fix the underlying issue "
                        f"and restart.\n\nDetail: {detail}"
                    )

                if outcome == "rate_limited":
                    consecutive_rate_limits += 1
                    if consecutive_rate_limits > config["rate_limit_max_consecutive_retries"]:
                        log.write(
                            "rate_limit_giving_up_on_ad",
                            availability_domain=ad,
                            shape=config["shape"],
                            consecutive_rate_limits=consecutive_rate_limits,
                        )
                        break  # move on to the next AD instead of retrying forever
                    backoff = min(
                        config["rate_limit_backoff_base_seconds"] * (2 ** (consecutive_rate_limits - 1)),
                        config["rate_limit_backoff_max_seconds"],
                    )
                    backoff *= 1 + random.uniform(0, 0.3)  # jitter
                    log.write(
                        "rate_limited",
                        availability_domain=ad,
                        shape=config["shape"],
                        detail=detail,
                        backoff_seconds=round(backoff, 1),
                        consecutive_rate_limits=consecutive_rate_limits,
                    )
                    time.sleep(backoff)
                    continue  # retry the same AD after backing off

                log.write(
                    "launch_failed",
                    availability_domain=ad,
                    shape=config["shape"],
                    outcome=outcome,
                    detail=detail,
                )
                break  # move on to the next AD

            if not one_pass:
                time.sleep(config["obtain_inter_ad_delay_seconds"])

        if one_pass:
            log.write("obtain_pass_complete_no_capacity", shape=config["shape"])
            return None

        log.write(
            "obtain_pass_complete",
            shape=config["shape"],
            next_retry_seconds=config["obtain_retry_interval_seconds"],
        )
        time.sleep(config["obtain_retry_interval_seconds"])


# --------------------------------------------------------------------------
# Monitor mode
# --------------------------------------------------------------------------

def get_instance_state(config, instance_id):
    args = base_oci_args(config) + [
        "compute", "instance", "get", "--instance-id", instance_id,
    ]
    rc, parsed, stdout, stderr = run_oci_json(args, timeout=60)
    if rc != 0:
        if "NotAuthorizedOrNotFound" in stderr or "404" in stderr:
            return None, stderr
        return "ERROR", stderr
    return parsed["data"]["lifecycle-state"], None


def monitor_loop(config, log, max_iterations=None):
    check_oci_cli_installed()
    state = read_state(config)
    if not state or not state.get("instance_id"):
        raise FatalSetupError(
            f"No instance recorded in state file ({config['state_file']}). "
            "Run 'obtain' mode first, or check state_file/obtain results."
        )
    instance_id = state["instance_id"]
    log.write("monitor_start", instance_id=instance_id, shape=config["shape"])

    iterations = 0
    while True:
        lifecycle_state, error = get_instance_state(config, instance_id)

        if lifecycle_state in ("RUNNING",):
            log.write("monitor_check", instance_id=instance_id, lifecycle_state=lifecycle_state)
        elif lifecycle_state in ("STARTING", "PROVISIONING"):
            log.write("monitor_check", instance_id=instance_id, lifecycle_state=lifecycle_state)
        elif lifecycle_state is None or lifecycle_state in ("TERMINATED", "TERMINATING"):
            log.write(
                "instance_lost",
                instance_id=instance_id,
                lifecycle_state=lifecycle_state,
                detail=truncate(error or ""),
            )
            clear_state(config)
            systemctl_start(config["obtain_systemd_unit"], log)
            return
        else:
            log.write(
                "monitor_check_error",
                instance_id=instance_id,
                lifecycle_state=lifecycle_state,
                detail=truncate(error or ""),
            )

        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            return
        time.sleep(config["monitor_check_interval_seconds"])


# --------------------------------------------------------------------------
# Self-test: exercises the logging/classification paths without calling the
# real OCI CLI or systemctl. Useful for verifying the tool end-to-end when
# no live OCI account/credentials are available.
# --------------------------------------------------------------------------

def selftest(log_file):
    log = ActivityLog(log_file)
    print(f"Running self-test, writing log lines to {log_file}\n")

    # Simulated capacity error.
    stdout = ""
    stderr = (
        '{"code": "InternalError", "message": '
        '"Out of host capacity for shape VM.Standard.A1.Flex in this '
        'availability domain."}'
    )
    outcome = classify_failure(stdout, stderr)
    assert outcome == "capacity_error", outcome
    log.write(
        "launch_failed",
        availability_domain="EXAMPLE-AD-1",
        shape="VM.Standard.A1.Flex",
        outcome=outcome,
        detail=truncate(stderr),
    )

    # Simulated rate limit.
    stderr_429 = '{"code": "TooManyRequests", "message": "429 Too many requests"}'
    outcome = classify_failure("", stderr_429)
    assert outcome == "rate_limited", outcome
    log.write(
        "rate_limited",
        availability_domain="EXAMPLE-AD-2",
        shape="VM.Standard.A1.Flex",
        detail=truncate(stderr_429),
        backoff_seconds=34.2,
        consecutive_rate_limits=1,
    )

    # Simulated success.
    log.write(
        "launch_success",
        availability_domain="EXAMPLE-AD-3",
        shape="VM.Standard.A1.Flex",
        instance_id="ocid1.instance.oc1..aaaaaaaaexampleexampleexample",
    )

    print(f"\nSelf-test complete. Inspect the log file at: {log_file}")


# --------------------------------------------------------------------------
# CLI entry point
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Obtain and keep alive an OCI Always Free compute instance."
    )
    parser.add_argument(
        "mode", choices=["obtain", "monitor", "selftest"],
        help="'obtain': retry launch until success, then hand off to monitor. "
             "'monitor': watch an obtained instance, hand off to obtain if lost. "
             "'selftest': exercise logging/classification with no live OCI calls.",
    )
    parser.add_argument(
        "--config", default="/etc/oci-shepherd/config.yaml",
        help="Path to the YAML config file (default: %(default)s)",
    )
    parser.add_argument(
        "--one-pass", action="store_true",
        help="obtain mode only: try every availability domain once and exit "
             "instead of looping forever. Useful for testing.",
    )
    parser.add_argument(
        "--log-file",
        help="selftest mode only: where to write the sample JSONL log "
             "(default: ./selftest.jsonl)",
    )
    args = parser.parse_args()

    if args.mode == "selftest":
        selftest(args.log_file or "selftest.jsonl")
        return

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    log = ActivityLog(config["log_file"])

    try:
        if args.mode == "obtain":
            obtain_loop(config, log, one_pass=args.one_pass)
        elif args.mode == "monitor":
            monitor_loop(config, log)
    except FatalSetupError as e:
        log.write("fatal_error", detail=str(e))
        print(f"FATAL ERROR: {e}", file=sys.stderr)
        sys.exit(3)
    except KeyboardInterrupt:
        log.write("interrupted")
        sys.exit(130)


if __name__ == "__main__":
    main()
