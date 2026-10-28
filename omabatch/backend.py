"""Read-only discovery and strict, explicit pacman removal plans.

No shell, recursive removal, dependency bypass, or automatic confirmation.
The only privileged command is pacman itself, inside an interactive terminal.
"""

from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

PACMAN = "/usr/bin/pacman"
NAME = re.compile(r"[a-zA-Z0-9@_+][a-zA-Z0-9@_.+\-]*\Z")
ANCHORS = """base bash coreutils filesystem glibc pacman archlinux-keyring
systemd systemd-sysvcompat util-linux sudo polkit dbus networkmanager iwd
wpa_supplicant linux linux-lts linux-zen linux-hardened linux-firmware
mkinitcpio dracut grub limine efibootmgr cryptsetup lvm2 mdadm btrfs-progs
e2fsprogs dosfstools xfsprogs intel-ucode amd-ucode mesa nvidia nvidia-open
hyprland uwsm sddm greetd waybar quickshell pipewire wireplumber
xdg-desktop-portal xdg-desktop-portal-hyprland xdg-desktop-portal-gtk
python python-gobject gtk4 alacritty foot kitty ghostty xterm
xdg-terminal-exec openssh omarchy omarchy-settings""".split()
PATTERNS = ("omarchy*", "hypr*", "systemd*", "linux*", "nvidia*",
            "mkinitcpio*", "limine*", "grub*", "xdg-desktop-portal*")


class SafetyError(RuntimeError):
    pass


def run(args, *, allowed=(0,)):
    result = subprocess.run(args, text=True, capture_output=True,
                            env={**os.environ, "LC_ALL": "C"}, timeout=90)
    if result.returncode not in allowed:
        raise SafetyError(result.stderr.strip() or result.stdout.strip()
                          or f"Command failed ({result.returncode}): {args[0]}")
    return result


def parse_info(text):
    """Parse pacman's C-locale, wrapped -Qi records; preserve full details."""
    records = {}
    for block in text.strip().split("\n\n"):
        fields = {}
        key = None
        for line in block.splitlines():
            match = re.match(r"^([^ :][^:]*?)\s+: (.*)$", line)
            if match:
                key, value = match.groups()
                fields[key.rstrip()] = value.strip()
                key = key.rstrip()
            elif key and line.startswith(" "):
                fields[key] += " " + line.strip()
        if "Name" in fields:
            fields["_raw"] = block
            records[fields["Name"]] = fields
    if not records:
        raise SafetyError("No installed package information was returned.")
    for name, fields in records.items():
        if not NAME.fullmatch(name) or not {"Version", "Depends On", "Provides", "Groups", "Install Reason"} <= fields.keys():
            raise SafetyError("Incomplete or unrecognised pacman package information.")
    return records


def dep_name(value):
    return re.split(r"[<>=]", value, maxsplit=1)[0]


def protected_packages(records, extra_anchors=()):
    """Conservatively protect every provider in essential dependency closures."""
    providers = {}
    for name, info in records.items():
        for provided in [name, *info.get("Provides", "").split()]:
            if provided != "None":
                providers.setdefault(dep_name(provided), set()).add(name)
    reasons = {}
    pending = []
    for name, info in records.items():
        if (name in ANCHORS or name in extra_anchors or any(fnmatch.fnmatchcase(name, p) for p in PATTERNS)
                or "base" in info.get("Groups", "").split()):
            reasons[name] = "Core system / desktop / OmaBatch runtime"
            pending.append(name)
    while pending:
        name = pending.pop()
        for dependency in records[name].get("Depends On", "").split():
            if dependency == "None":
                continue
            candidates = providers.get(dep_name(dependency), set())
            if not candidates:
                raise SafetyError(f"Cannot resolve protected dependency {dependency} for {name}.")
            for candidate in candidates:
                if candidate not in reasons:
                    reasons[candidate] = f"Required by protected package {name}"
                    pending.append(candidate)
    return reasons


@dataclass
class Inventory:
    records: dict
    foreign: set
    protected: dict
    fingerprint: str


def inventory():
    raw = run([PACMAN, "-Qi"]).stdout
    records = parse_info(raw)
    foreign_result = run([PACMAN, "-Qmq"], allowed=(0, 1))
    if foreign_result.returncode == 1 and foreign_result.stderr.strip():
        raise SafetyError(foreign_result.stderr.strip())
    foreign = set(foreign_result.stdout.split())
    if not foreign <= records.keys():
        raise SafetyError("Package database changed during discovery. Refresh and try again.")
    # Also protect actual file owners: alternate/custom kernels, compositors,
    # network services and the running Python interpreter need not use stock names.
    critical = [Path(p) for p in ("/usr/bin/pacman", "/usr/bin/sudo", sys.executable,
                "/usr/bin/Hyprland", "/usr/bin/NetworkManager", "/usr/lib/systemd/systemd")]
    critical += list(Path("/usr/lib/modules").glob("*/vmlinuz"))
    critical += list(Path("/usr/lib/modules").glob("*/pkgbase"))
    critical = sorted({str(p.resolve()) for p in critical if p.exists()})
    owners = run([PACMAN, "-Qqo", "--", *critical], allowed=(0, 1)) if critical else None
    extra = set(owners.stdout.split()) if owners else set()
    if not extra <= records.keys():
        raise SafetyError("Package database changed. Refresh and try again.")
    fingerprint = hashlib.sha256(raw.encode()).hexdigest()
    return Inventory(records, foreign, protected_packages(records, extra), fingerprint)


def validate_names(names, inv):
    if not isinstance(names, list) or not names or len(names) > 10000:
        raise SafetyError("Choose at least one package.")
    if any(not isinstance(n, str) or not NAME.fullmatch(n) for n in names):
        raise SafetyError("Invalid package name.")
    if len(set(names)) != len(names):
        raise SafetyError("Duplicate package names.")
    missing = set(names) - inv.records.keys()
    blocked = set(names) & inv.protected.keys()
    if missing or blocked:
        raise SafetyError("Unavailable or protected packages: " + ", ".join(sorted(missing | blocked)))
    return sorted(names)


def preview(names, inv):
    names = validate_names(names, inv)
    result = run([PACMAN, "-R", "--print-format", "%n", "--", *names])
    actual = result.stdout.splitlines()
    if sorted(actual) != names:
        raise SafetyError("Pacman's proposed removal differs from your selection. Nothing was removed.")
    return {"schema": 1, "created": time.time(), "fingerprint": inv.fingerprint,
            "packages": names, "warnings": result.stderr.strip()}


def revalidate(plan, inv):
    if (not isinstance(plan, dict) or plan.get("schema") != 1
            or not isinstance(plan.get("created"), (float, int))
            or not 0 <= time.time() - plan["created"] <= 900):
        raise SafetyError("Review expired or invalid. Return to OmaBatch and review again.")
    if plan.get("fingerprint") != inv.fingerprint:
        raise SafetyError("Installed packages changed since review. Refresh and review again.")
    return preview(plan.get("packages"), inv)


def save_plan(plan):
    directory = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "omabatch"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="review-", suffix=".json", dir=directory)
    with os.fdopen(fd, "w") as stream:
        json.dump(plan, stream)
    return path


def terminal_command(script, plan):
    args = [sys.executable, str(script), "--execute", str(plan)]
    for terminal, flags in [("alacritty", ["-e"]), ("foot", []),
                            ("kitty", []), ("xterm", ["-e"])]:
        executable = shutil.which(terminal)
        if executable:
            return [executable, *flags, *args]
    raise SafetyError("Install alacritty, foot, kitty or xterm for the final pacman confirmation.")


def load_plan(path):
    # Refuse symlinks, other users' files and group/world-writable review files.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_size > 1024 * 1024):
            raise SafetyError("Unsafe review file.")
        return json.load(stream)


def execute_plan(path):
    """Called only in a real terminal, as the desktop user, never as root."""
    if os.geteuid() == 0 or not sys.stdin.isatty():
        raise SafetyError("Run OmaBatch as your normal desktop user in an interactive terminal.")
    plan = load_plan(path)
    validated = revalidate(plan, inventory())
    names = validated["packages"]
    print("\nOmaBatch • reviewed removal\n")
    print("\n".join("  " + name for name in names))
    print("\nOnly these packages will be requested. Dependencies and personal files are kept.")
    print("Pacman will show the final transaction and ask you to confirm. Answer n to cancel.\n", flush=True)
    # Plain -R cannot cascade or add dependencies to the removal list.
    # --confirm overrides any preceding noninteractive default; no shell involved.
    command = ["/usr/bin/sudo", PACMAN, "-R", "--confirm", "--", *names]
    result = subprocess.run(command, env={**os.environ, "LC_ALL": "C"}, check=False)
    print("\nRemoval completed." if result.returncode == 0 else
          "\nPacman did not complete successfully. Read the output above; refresh OmaBatch.")
    input("\nPress Enter to close this window… ")
    return result.returncode
